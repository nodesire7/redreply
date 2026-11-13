#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""文字转语音 CLI。Python 3.10+；依赖：edge-tts>=7.3.1,<8。"""

from __future__ import annotations

import argparse
import asyncio
import codecs
import importlib.metadata
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

VERSION = "1.0.0"
FORMATS = ("mp3", "wav", "flac", "ogg")
ALIASES = {
	"晓晓": "zh-CN-XiaoxiaoNeural",
	"晓伊": "zh-CN-XiaoyiNeural",
	"云希": "zh-CN-YunxiNeural",
	"云健": "zh-CN-YunjianNeural",
	"云夏": "zh-CN-YunxiaNeural",
	"云扬": "zh-CN-YunyangNeural",
	"晓北": "zh-CN-liaoning-XiaobeiNeural",
	"晓妮": "zh-CN-shaanxi-XiaoniNeural",
	"晓臻": "zh-TW-HsiaoChenNeural",
	"晓雨": "zh-TW-HsiaoYuNeural",
	"云哲": "zh-TW-YunJheNeural",
	"晓佳": "zh-HK-HiuGaaiNeural",
	"晓曼": "zh-HK-HiuMaanNeural",
	"云龙": "zh-HK-WanLungNeural",
	"jenny": "en-US-JennyNeural",
	"guy": "en-US-GuyNeural",
	"nanami": "ja-JP-NanamiNeural",
	"keita": "ja-JP-KeitaNeural",
}


def reference_catalog():
	"""参考样本是静态目录；不把它当成 Edge 当前可用名单。"""
	path = Path(__file__).resolve().parent / "voice_reference.json"
	if not path.is_file():
		return {"voices": []}
	return json.loads(path.read_text(encoding="utf-8"))


for _sample in reference_catalog()["voices"]:
	_name, _voice = _sample["name"], _sample["id"]
	ALIASES[_name.casefold()] = _voice
	ALIASES[_name.replace("（", "(").replace("）", ")").casefold()] = _voice
	if "（" in _name:
		_base, _region = _name.split("（", 1)
		ALIASES.setdefault(_base.casefold(), _voice)
		ALIASES[(_region.rstrip("）") + _base).casefold()] = _voice
	ALIASES[_voice.casefold()] = _voice


class ToolError(Exception):
	"""可以直接展示给用户的错误。"""


class CLIParser(argparse.ArgumentParser):
	def error(self, message):
		raise ToolError(f"参数错误：{message}；使用 --help 查看帮助。")


def integer(minimum, maximum=None):
	def parse(value):
		try:
			number = int(value)
		except ValueError:
			raise argparse.ArgumentTypeError("必须是整数") from None
		if number < minimum or (maximum is not None and number > maximum):
			limit = f"{minimum}～{maximum}" if maximum is not None else f">= {minimum}"
			raise argparse.ArgumentTypeError(f"数值必须为 {limit}")
		return number
	return parse


def prosody(unit, minimum=None):
	def parse(value):
		if not re.fullmatch(r"[+-]?\d+(?:" + re.escape(unit) + r")?", value, re.I):
			raise argparse.ArgumentTypeError(f"请输入整数或带 {unit} 的整数")
		number = int(re.sub(re.escape(unit) + r"$", "", value, flags=re.I))
		if minimum is not None and number < minimum:
			raise argparse.ArgumentTypeError(f"本工具要求数值 >= {minimum}")
		return f"{number:+d}{unit}"
	return parse


def build_parser():
	p = CLIParser(
		description="文字转语音：多语言人声、语速/音调/音量、字幕、批量转换。",
		formatter_class=argparse.RawDescriptionHelpFormatter,
		allow_abbrev=False,
		epilog=(
			"示例：\n"
			'  python tts_cli.py -t "你好，欢迎使用。" -v 晓晓 -o hello.mp3\n'
			'  python tts_cli.py -f 文案.txt -v 云希 --rate=-15 --pitch=-5 --srt\n'
			"  python tts_cli.py --list-voices --locale zh --gender female\n"
			"  python tts_cli.py --batch 文案.txt --out-dir output --srt\n"
			"  python tts_cli.py -f 文案.txt -o 朗读.wav --sample-rate 48000\n"
			"负数建议使用等号：--rate=-15 或 --rate=-15%。\n"
			"默认直连；联网请求会将输入文字发送到在线语音服务。"
		),
	)
	source = p.add_mutually_exclusive_group(required=True)
	source.add_argument("-t", "--text", help="直接传入文字")
	source.add_argument("-f", "--file", type=Path, help="读取整个文本文件")
	source.add_argument("--stdin", action="store_true", help="从标准输入读取文字")
	source.add_argument("--batch", type=Path, help="文本文件中每个非空行生成一个音频")
	source.add_argument("-l", "--list-voices", action="store_true", help="在线列出人声")
	source.add_argument("--list-reference-voices", action="store_true", help="列出参考页 28 个样本及 URL（静态目录，不代表当前可合成）")
	p.add_argument("-v", "--voice", default="晓晓", help="人声完整 ID 或中文别名，默认：晓晓")
	p.add_argument("--rate", type=prosody("%", -99), default="+0%", help="语速相对变化，例：20、-15、+20%%")
	p.add_argument("--volume", type=prosody("%", -100), default="+0%", help="音量相对变化，例：10、-30")
	p.add_argument("--pitch", type=prosody("Hz"), default="+0Hz", help="音调相对变化（Hz），例：5、-10")
	p.add_argument("-o", "--output", type=Path, help="单文件输出路径，默认：output.mp3")
	p.add_argument("--out-dir", type=Path, help="批量输出目录，默认：output")
	p.add_argument("--format", choices=FORMATS, help="输出格式；单文件默认按扩展名识别，批量默认 mp3")
	p.add_argument("--encoding", default="auto", help="输入编码，默认 auto：BOM / UTF-8 / GB18030")
	p.add_argument("--srt", action="store_true", help="同时生成同名 UTF-8 SRT 字幕")
	p.add_argument("--boundary", choices=("sentence", "word"), default="sentence", help="字幕按句或按词，默认 sentence")
	p.add_argument("--overwrite", action="store_true", help="允许覆盖已经存在的输出文件")
	p.add_argument("--continue-on-error", action="store_true", help="批量模式遇到单项失败后继续")
	p.add_argument("--sample-rate", type=int, choices=(8000, 16000, 22050, 24000, 32000, 44100, 48000), help="转码采样率；需要 FFmpeg")
	p.add_argument("--channels", type=int, choices=(1, 2), help="转码声道数；需要 FFmpeg")
	p.add_argument("--bitrate", help="MP3/OGG 转码码率，例如 128k；需要 FFmpeg")
	p.add_argument("--ffmpeg", help="FFmpeg 可执行文件路径；默认查找脚本目录和 PATH")
	p.add_argument("--locale", help="筛选人声的语言前缀，例如 zh、zh-CN、en-US")
	p.add_argument("--gender", choices=("female", "male", "女", "男"), help="筛选人声性别")
	p.add_argument("--filter", default="", help="按人声 ID、名称等关键词筛选")
	p.add_argument("--connect-timeout", type=integer(1), default=10, help="网络连接超时秒数，默认 10")
	p.add_argument("--read-timeout", type=integer(1), default=60, help="等待下一段网络数据的超时秒数，默认 60")
	p.add_argument("--timeout", type=integer(0), default=0, help="每次网络尝试的总超时秒数，0 表示不限；人声列表默认最多 70 秒")
	p.add_argument("--retries", type=integer(0, 10), default=2, help="网络失败后额外重试次数，默认 2")
	p.add_argument("--proxy", help="显式 HTTP/HTTPS 代理，例如 http://127.0.0.1:7890；默认直连")
	p.add_argument("--json", action="store_true", help="只输出一个 JSON 结果，便于程序调用")
	p.add_argument("--version", action="version", version=f"TTS CLI {VERSION}")
	return p


def read_text(args):
	if args.text is not None:
		text = args.text
	else:
		if args.stdin:
			data = sys.stdin.buffer.read()
		else:
			data = (args.file or args.batch).expanduser().read_bytes()
		encoding = args.encoding
		if encoding.lower() == "auto":
			if data.startswith((codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)):
				encoding = "utf-32"
			elif data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
				encoding = "utf-16"
			else:
				try:
					text = data.decode("utf-8-sig")
				except UnicodeDecodeError:
					encoding = "gb18030"
				else:
					encoding = None
		if encoding:
			try:
				text = data.decode(encoding)
			except (LookupError, UnicodeDecodeError) as exc:
				raise ToolError("无法解码文本，请用 --encoding 指定正确编码，如 utf-8 或 gb18030。") from exc
	# 去除 XML 不接受的控制字符；保留换行和制表符。
	text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text).lstrip("\ufeff")
	if not text.strip():
		raise ToolError("输入文字为空。")
	return text if args.batch else text.strip()


@contextmanager
def network_environment():
	"""只影响当前 CLI 进程；避免库的 trust_env 自动读取环境或 Windows 注册表代理。"""
	names = {"http_proxy", "https_proxy", "all_proxy", "ws_proxy", "wss_proxy", "no_proxy"}
	saved = {key: value for key, value in os.environ.items() if key.lower() in names}
	for key in saved:
		del os.environ[key]
	os.environ["NO_PROXY"] = "*"
	try:
		yield
	finally:
		os.environ.pop("NO_PROXY", None)
		os.environ.update(saved)


def find_ffmpeg(value):
	if value:
		found = shutil.which(value) or str(Path(value).expanduser())
	else:
		local = Path(__file__).resolve().parent / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
		found = str(local) if local.is_file() else shutil.which("ffmpeg")
	if not found or not Path(found).is_file():
		raise ToolError("该输出设置需要 FFmpeg。请放到脚本目录、加入 PATH，或使用 --ffmpeg 指定路径。")
	return str(Path(found).resolve())


def plan_jobs(args):
	if args.locale or args.gender or args.filter:
		raise ToolError("--locale、--gender、--filter 仅用于 --list-voices。")
	if args.batch and args.output:
		raise ToolError("批量模式请用 --out-dir，不要使用 --output。")
	if not args.batch and (args.out_dir or args.continue_on_error):
		raise ToolError("--out-dir 和 --continue-on-error 仅用于 --batch。")
	if args.bitrate and not re.fullmatch(r"[1-9]\d*k", args.bitrate):
		raise ToolError("--bitrate 使用类似 64k、128k、192k 的格式。")
	text = read_text(args)
	if args.batch:
		fmt = args.format or "mp3"
		directory = (args.out_dir or Path("output")).expanduser()
		lines = [(line_no, line.strip()) for line_no, line in enumerate(text.splitlines(), 1) if line.strip()]
		jobs = [(index, line_no, line, directory / f"{index:03d}.{fmt}") for index, (line_no, line) in enumerate(lines, 1)]
	else:
		output = (args.output or Path("output." + (args.format or "mp3"))).expanduser()
		fmt = output.suffix.lower().lstrip(".")
		if args.format and args.format != fmt:
			raise ToolError("--format 与输出文件扩展名不一致。")
		jobs = [(1, None, text, output)]
	if fmt not in FORMATS:
		raise ToolError("输出扩展名必须为 .mp3、.wav、.flac 或 .ogg。")
	if args.bitrate and fmt in ("wav", "flac"):
		raise ToolError("WAV/FLAC 不使用 --bitrate；请删除此参数。")
	args.output_format = fmt
	args.transcode = fmt != "mp3" or any((args.sample_rate, args.channels, args.bitrate))
	args.ffmpeg_path = find_ffmpeg(args.ffmpeg) if args.transcode else None
	source = (args.file or args.batch)
	source = source.expanduser().resolve() if source else None
	planned = []
	for index, line_no, content, output in jobs:
		output = output.absolute()
		subtitle = output.with_suffix(".srt") if args.srt else None
		for target in (output, subtitle):
			if target is None:
				continue
			if target.is_symlink() or (source is not None and target.resolve() == source):
				raise ToolError(f"输出路径不能是输入文件或符号链接：{target}")
			if target.exists() and (not target.is_file() or not args.overwrite):
				raise ToolError(f"输出已存在或不是普通文件：{target}。覆盖文件需加 --overwrite。")
		planned.append((index, line_no, content, output, subtitle))
	return planned


def load_engine():
	try:
		import edge_tts
	except ImportError as exc:
		raise ToolError('缺少依赖，请运行：python -m pip install -U "edge-tts>=7.3.1,<8"') from exc
	version = importlib.metadata.version("edge-tts")
	numbers = tuple(int(part) for part in version.split(".")[:3] if part.isdigit())
	if numbers < (7, 3, 1) or numbers >= (8,):
		raise ToolError(f'当前 edge-tts {version} 不在支持范围，请运行：python -m pip install -U "edge-tts>=7.3.1,<8"')
	return edge_tts


async def retry_network(factory, args, engine):
	import aiohttp
	retryable = (TimeoutError, asyncio.TimeoutError, aiohttp.ClientError, engine.exceptions.NoAudioReceived, engine.exceptions.WebSocketError)
	for attempt in range(args.retries + 1):
		try:
			return await factory(), attempt + 1
		except retryable:
			if attempt == args.retries:
				raise
			await asyncio.sleep(min(2 ** attempt, 8))


async def fetch_voices(args, engine):
	async def request():
		return await asyncio.wait_for(
			engine.list_voices(proxy=args.proxy),
			timeout=args.timeout or (args.connect_timeout + args.read_timeout),
		)
	voices, attempts = await retry_network(request, args, engine)
	gender = {"女": "female", "男": "male"}.get(args.gender, args.gender)
	selected = []
	for voice in voices:
		if args.locale and not voice.get("Locale", "").lower().startswith(args.locale.lower()):
			continue
		if gender and voice.get("Gender", "").lower() != gender:
			continue
		if args.filter and args.filter.lower() not in json.dumps(voice, ensure_ascii=False).lower():
			continue
		selected.append(voice)
	selected.sort(key=lambda item: item.get("ShortName", ""))
	return {"ok": True, "count": len(selected), "attempts": attempts, "voices": selected}


async def convert_audio(source, target, args):
	codecs_by_format = {"mp3": "libmp3lame", "wav": "pcm_s16le", "flac": "flac", "ogg": "libvorbis"}
	command = [args.ffmpeg_path, "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-i", str(source), "-map", "0:a:0", "-vn"]
	command += ["-c:a", codecs_by_format[args.output_format]]
	if args.sample_rate:
		command += ["-ar", str(args.sample_rate)]
	if args.channels:
		command += ["-ac", str(args.channels)]
	if args.bitrate:
		command += ["-b:a", args.bitrate]
	command += ["-f", args.output_format, str(target)]
	options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
	process = await asyncio.create_subprocess_exec(
		*command, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE, **options
	)
	try:
		_, stderr = await process.communicate()
	except BaseException:
		if process.returncode is None:
			process.kill()
			await process.wait()
		raise
	if process.returncode:
		message = stderr.decode("utf-8", errors="replace").strip()[-1500:]
		raise ToolError(f"FFmpeg 转码失败：{message}")
	if not target.is_file() or target.stat().st_size == 0:
		raise ToolError("FFmpeg 未生成有效的音频文件。")


def publish_files(pairs, overwrite, workspace):
	"""所有内容生成成功后才发布；普通提交错误时恢复本次已修改的输出。"""
	backups = {}
	published = []
	try:
		for number, (_, target) in enumerate(pairs):
			if target.is_symlink():
				raise ToolError(f"输出不能是符号链接：{target}")
			if target.exists():
				if not overwrite or not target.is_file():
					raise ToolError(f"输出已经存在：{target}；如需覆盖请加 --overwrite。")
				backup = workspace / f"backup-{number}"
				shutil.copy2(target, backup)
				backups[target] = backup
		for source, target in pairs:
			if overwrite:
				os.replace(source, target)
			elif os.name == "nt":
				os.rename(source, target)
			else:
				# POSIX rename 会覆盖同名文件，link 可确保并发时也不覆盖。
				os.link(source, target)
			published.append(target)
	except BaseException:
		for target in reversed(published):
			if target in backups:
				os.replace(backups[target], target)
			else:
				target.unlink(missing_ok=True)
		raise


async def synthesize(content, output, subtitle, args, engine):
	output.parent.mkdir(parents=True, exist_ok=True)
	with tempfile.TemporaryDirectory(prefix=".tts-", dir=output.parent) as folder:
		work = Path(folder)
		raw = work / "source.mp3"
		srt = work / "subtitle.srt"

		async def generate():
			# 每次重试重新建立实例、清空音频和字幕，避免重复拼接。
			maker = engine.SubMaker()
			stream = engine.Communicate(
				content, args.voice_id, rate=args.rate, volume=args.volume, pitch=args.pitch,
				boundary="SentenceBoundary" if args.boundary == "sentence" else "WordBoundary",
				proxy=args.proxy, connect_timeout=args.connect_timeout, receive_timeout=args.read_timeout,
			)
			with raw.open("wb") as handle:
				async for chunk in stream.stream():
					if chunk["type"] == "audio":
						handle.write(chunk["data"])
					elif subtitle and chunk["type"] in ("WordBoundary", "SentenceBoundary"):
						maker.feed(chunk)
			if raw.stat().st_size == 0:
				raise engine.exceptions.NoAudioReceived("服务未返回音频")
			if subtitle:
				subtitle_text = maker.get_srt()
				if not subtitle_text.strip():
					raise ToolError("服务返回了音频，但未返回字幕时间信息；可移除 --srt 后重试。")
				srt.write_text(subtitle_text, encoding="utf-8")

		async def request():
			if args.timeout:
				await asyncio.wait_for(generate(), timeout=args.timeout)
			else:
				await generate()

		_, attempts = await retry_network(request, args, engine)
		audio = raw
		if args.transcode:
			audio = work / ("converted." + args.output_format)
			await convert_audio(raw, audio, args)
		pairs = [(audio, output)]
		if subtitle:
			pairs.append((srt, subtitle))
		publish_files(pairs, args.overwrite, work)
	return attempts


def explain_error(exc):
	if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
		return "网络请求超时；请检查连接，或增大 --connect-timeout、--read-timeout、--timeout。"
	if isinstance(getattr(exc, "os_error", None), socket.gaierror):
		return f"无法解析语音服务域名 {getattr(exc, 'host', '')}；请检查 DNS 与网络连接。"
	if type(exc).__name__ == "ClientConnectorCertificateError":
		return "语音服务 TLS 证书验证失败；请检查系统时间与证书环境。"
	if type(exc).__name__ in ("ClientConnectorError", "ClientProxyConnectionError"):
		return f"无法连接 {getattr(exc, 'host', '语音服务')}；请检查网络或显式设置的代理。"
	if type(exc).__name__ == "NoAudioReceived":
		return "服务未返回音频。请检查人声 ID、文本与网络；可用 --list-voices 核对人声，或更新 edge-tts。"
	message = str(exc) or type(exc).__name__
	# 避免异常信息回显带用户名或密码的代理 URL。
	message = re.sub(r"(?i)(https?://)[^\s/@]+(?::[^\s/@]*)?@", r"\1***@", message)
	return message


async def run(args):
	if args.list_reference_voices:
		catalog = reference_catalog()
		return {"ok": True, "reference_voices": catalog["voices"], "source_page": catalog.get("source_page", ""), "checked_at": catalog.get("retrieved_at", "")}
	if args.proxy:
		proxy = urlsplit(args.proxy)
		if proxy.scheme not in ("http", "https") or not proxy.hostname:
			raise ToolError("--proxy 必须是有效的 http:// 或 https:// 代理地址。")
	jobs = None if args.list_voices else plan_jobs(args)
	engine = load_engine()
	args.voice_id = ALIASES.get(args.voice.casefold(), args.voice)
	with network_environment():
		if args.list_voices:
			return await fetch_voices(args, engine)
		results = []
		for index, line_no, content, output, subtitle in jobs:
			item = {"index": index, "line": line_no, "output": str(output), "subtitle": str(subtitle) if subtitle else None}
			try:
				attempts = await synthesize(content, output, subtitle, args, engine)
				item.update(ok=True, bytes=output.stat().st_size, attempts=attempts)
			except Exception as exc:
				item.update(ok=False, error=explain_error(exc))
			results.append(item)
			if not item["ok"] and not args.continue_on_error:
				break
	return {
		"ok": all(item["ok"] for item in results),
		"voice": args.voice_id, "rate": args.rate, "volume": args.volume, "pitch": args.pitch,
		"total": len(jobs), "completed": sum(item["ok"] for item in results),
		"failed": sum(not item["ok"] for item in results), "unprocessed": len(jobs) - len(results),
		"results": results,
	}


def show_result(result, as_json):
	if as_json:
		print(json.dumps(result, ensure_ascii=False))
		return
	if "error" in result:
		print("错误：" + result["error"], file=sys.stderr)
		return
	if "reference_voices" in result:
		print("参考样本目录；当前可合成人声请运行 --list-voices。")
		for voice in result["reference_voices"]:
			print(f"{voice['name']} | {voice['id']}\n  {voice['sample_url']}")
		print(f"共 {len(result['reference_voices'])} 个参考样本。")
		return
	if "voices" in result:
		lines = [f"{'人声 ID':<42}性别     语言"]
		for voice in result["voices"]:
			lines.append(f"{voice['ShortName']:<44}{voice['Gender']:<9}{voice['Locale']}")
		lines.append(f"共 {result['count']} 个匹配人声。使用 -v 指定完整 ID。")
	else:
		lines = []
		for item in result["results"]:
			if item["ok"]:
				lines.append(f"已生成：{item['output']}")
				if item["subtitle"]:
					lines.append(f"字幕：{item['subtitle']}")
			else:
				lines.append(f"第 {item['index']} 项失败：{item['error']}")
		lines.append(f"成功 {result['completed']} / {result['total']}，失败 {result['failed']}，未处理 {result['unprocessed']}。")
	print("\n".join(lines), file=sys.stdout if result["ok"] else sys.stderr)


def main(argv=None):
	arguments = sys.argv[1:] if argv is None else list(argv)
	as_json = "--json" in arguments
	for handle in (sys.stdout, sys.stderr):
		if hasattr(handle, "reconfigure"):
			handle.reconfigure(encoding="utf-8", errors="replace")
	exit_code = 1
	try:
		args = build_parser().parse_args(arguments)
	except ToolError as exc:
		show_result({"ok": False, "error": explain_error(exc)}, as_json)
		return 2
	try:
		result = asyncio.run(run(args))
		exit_code = 0 if result["ok"] else 1
	except KeyboardInterrupt:
		result = {"ok": False, "error": "操作已取消。"}
		exit_code = 130
	except Exception as exc:
		result = {"ok": False, "error": explain_error(exc)}
	show_result(result, as_json)
	return exit_code


if __name__ == "__main__":
	sys.exit(main())
