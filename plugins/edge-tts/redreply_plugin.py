#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""RedReply 附加包入口：受限 JSON 请求，ASCII JSON 结果，无消息发送副作用。"""

from __future__ import annotations

import base64
import binascii
import contextlib
import importlib.util
import json
import math
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MAX_REQUEST_BYTES = 5000
MAX_AUDIO_BYTES = 8 * 1024 * 1024
PAGE_SIZE = 20
DEFAULTS = {
	"voice": "晓晓", "rate": 0, "pitch": 0, "volume": 0,
	"timeout": 12, "retries": 1, "max_chars": 1000, "max_concurrent": 2,
}
HELP = (
	"语音 你好\n语音 云希|你好\n"
	"语音 云希|+20%|-5Hz|0%|你好\n"
	"语音人声 all 1：全部实时人声，第 1 页\n"
	"语音人声 zh 1：中文人声；可直接使用列表里的完整 ID。\n"
	"语音状态：检查本插件环境。\n"
	"语音样本：查看全部 28 个参考声音。\n"
	"语音示例 晓涵：播放参考页的现成样本。\n"
	"语音试听 晓晓：实时合成一段试听。"
)
DEPENDENCY_PROBE = (
	"import edge_tts,importlib.metadata;from edge_tts import SubMaker;"
	"v=importlib.metadata.version('edge-tts');"
	"n=tuple(int(x) for x in v.split('.')[:3]);"
	"assert (7,3,1)<=n<(8,);"
	"assert hasattr(SubMaker,'feed') and hasattr(SubMaker,'get_srt');print(v)"
)


class PluginError(Exception):
	pass


def fail(message):
	return {"ok": False, "error": str(message)}


def integer(value, name, minimum, maximum):
	if isinstance(value, bool) or not re.fullmatch(r"[+-]?\d+", str(value)):
		raise PluginError(f"{name} 必须是整数。")
	number = int(value)
	if not minimum <= number <= maximum:
		raise PluginError(f"{name} 必须在 {minimum}～{maximum} 之间。")
	return number


def prosody(value, name, unit, minimum, maximum):
	value = str(value).strip()
	if not re.fullmatch(r"[+-]?\d+(?:" + re.escape(unit) + r")?", value, re.I):
		raise PluginError(f"{name} 需要整数，可带单位 {unit}。")
	number = integer(re.sub(re.escape(unit) + r"$", "", value, flags=re.I), name, minimum, maximum)
	return f"{number:+d}{unit}"


def load_config(root):
	config = dict(DEFAULTS)
	path = root / "config.json"
	if path.is_file():
		if path.stat().st_size > 16384:
			raise PluginError("config.json 过大，请检查配置。")
		try:
			custom = json.loads(path.read_text(encoding="utf-8-sig"))
		except (ValueError, OSError) as exc:
			raise PluginError(f"config.json 无法读取：{exc}") from exc
		if not isinstance(custom, dict) or set(custom) - set(DEFAULTS):
			raise PluginError("config.json 含未知配置项，请参考 config.example.json。")
		config.update(custom)
	for name, low, high in (("timeout", 2, 60), ("retries", 0, 2), ("max_chars", 1, 1200), ("max_concurrent", 1, 8)):
		config[name] = integer(config[name], name, low, high)
	return config


def load_cli(root):
	# -I 不将包目录加入 sys.path；显式从本包文件加载纯标准库的 CLI 定义。
	spec = importlib.util.spec_from_file_location("_edge_tts_cli", root / "tts_cli.py")
	if spec is None or spec.loader is None:
		raise PluginError("缺少 tts_cli.py，请完整安装附加包。")
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


def voice_id(value, aliases):
	if not isinstance(value, str):
		raise PluginError("人声必须是中文别名或完整人声 ID。")
	value = value.strip()
	resolved = aliases.get(value.casefold(), value)
	if not re.fullmatch(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]+)+Neural", resolved):
		raise PluginError("人声名称不正确，请使用“语音人声”查询完整 ID。")
	return resolved


def venv_python(root):
	return root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


@contextlib.contextmanager
def request_slot(root, maximum):
	"""OS 文件锁随进程退出释放；不同聊天进程共享有限个槽位。"""
	directory = root / "runtime"
	directory.mkdir(exist_ok=True)
	locked = None
	for slot in range(maximum):
		handle = (directory / f"slot-{slot}.lock").open("a+b")
		try:
			if os.name == "nt":
				import msvcrt
				handle.seek(0, os.SEEK_END)
				if handle.tell() == 0:
					handle.write(b"0")
					handle.flush()
				handle.seek(0)
				msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
			else:
				import fcntl
				fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
			locked = handle
			break
		except OSError:
			handle.close()
	if locked is None:
		raise PluginError("当前语音任务较多，请稍后重试。")
	try:
		yield
	finally:
		# 关闭描述符释放锁；保留锁文件，避免删除/重建 inode 导致跨进程锁失效。
		locked.close()


def child_environment():
	env = dict(os.environ)
	for key in list(env):
		if key.upper() in ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV"):
			env.pop(key, None)
	env["PYTHONIOENCODING"] = "utf-8"
	return env


def run_cli(root, arguments, text, config):
	python = venv_python(root)
	if not python.is_file():
		raise PluginError("插件环境尚未创建，请在插件目录运行 setup_env.cmd（Linux：python3 setup_venv.py）。")
	command = [
		str(python), "-I", str(root / "tts_cli.py"), *arguments,
		"--timeout", str(max(1, config["timeout"] - 2)),
		"--connect-timeout", str(min(5, config["timeout"])),
		"--retries", str(config["retries"]), "--json",
	]
	result = subprocess.run(
		command, input=text, capture_output=True, text=True, encoding="utf-8",
		errors="replace", cwd=root, env=child_environment(), shell=False,
		timeout=config["timeout"],
		creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
	)
	try:
		payload = json.loads(result.stdout)
	except ValueError as exc:
		raise PluginError("语音工具未返回有效 JSON，请在本机使用 tts.cmd --help 检查依赖。") from exc
	if not isinstance(payload, dict):
		raise PluginError("语音工具返回格式不正确。")
	if result.returncode or payload.get("ok") is not True:
		detail = payload.get("error")
		if not isinstance(detail, str) or not detail:
			rows = payload.get("results", [])
			detail = next((row.get("error") for row in rows if isinstance(row, dict) and row.get("error")), None) if isinstance(rows, list) else None
		raise PluginError(str(detail or "语音服务未返回成功结果，请检查网络与人声 ID。")[:400])
	return payload


def chat_request(text, aliases):
	if not isinstance(text, str):
		raise PluginError("朗读内容必须是文本。")
	text = text.strip()
	request = {"action": "synthesize", "text": text}
	first, separator, remaining = text.partition("|")
	first = first.strip()
	if separator and (first.casefold() in aliases or re.fullmatch(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]+)+Neural", first)):
		request["voice"] = first
		parts = remaining.split("|", 3)
		if len(parts) == 4:
			request.update(zip(("rate", "pitch", "volume", "text"), parts))
		else:
			request["text"] = remaining
	return request


def synthesize(request, root, config, cli):
	text = request.get("text", "")
	if not isinstance(text, str) or not text.strip():
		raise PluginError("请输入要朗读的文字，例如：语音 你好。")
	text = text.strip()
	if len(text) > config["max_chars"]:
		raise PluginError(f"朗读文字最多 {config['max_chars']} 个字符，请拆分后重试。")
	def option(name):
		value = request.get(name)
		return config[name] if value is None or value == "" else value
	voice = voice_id(option("voice"), cli.ALIASES)
	rate = prosody(option("rate"), "语速", "%", -99, 200)
	pitch = prosody(option("pitch"), "音调", "Hz", -100, 100)
	volume = prosody(option("volume"), "音量", "%", -100, 100)
	with request_slot(root, config["max_concurrent"]):
		with tempfile.TemporaryDirectory(prefix=".edge-tts-", dir=root) as folder:
			output = Path(folder) / "speech.mp3"
			run_cli(root, [
				"--stdin", "--encoding", "utf-8", "--voice", voice,
				f"--rate={rate}", f"--pitch={pitch}", f"--volume={volume}",
				"--output", str(output),
			], text, config)
			if not output.is_file() or not 0 < output.stat().st_size <= MAX_AUDIO_BYTES:
				raise PluginError("语音文件为空或超过 8 MiB，请缩短文字后重试。")
			audio = base64.b64encode(output.read_bytes()).decode("ascii")
	return {"ok": True, "kind": "audio", "audio_base64": audio, "voice": voice}


def list_voices(request, root, config, cli):
	query = request.get("text", "")
	if not isinstance(query, str):
		raise PluginError("人声查询条件必须是文本。")
	parts = query.split()
	if len(parts) > 2:
		raise PluginError("用法：语音人声 all 1，或：语音人声 zh-CN 1。")
	locale = request.get("locale", parts[0] if parts else "all")
	page = integer(request.get("page", parts[1] if len(parts) == 2 else 1), "页码", 1, 10000)
	if not isinstance(locale, str) or not re.fullmatch(r"all|[A-Za-z]{2,3}(?:-[A-Za-z0-9]+)*", locale, re.I):
		raise PluginError("语言使用 all、zh、zh-CN、en-US 等代码。")
	arguments = ["--list-voices"]
	if locale.lower() != "all":
		arguments.extend(("--locale", locale))
	with request_slot(root, config["max_concurrent"]):
		payload = run_cli(root, arguments, None, config)
	voices = payload.get("voices")
	if not isinstance(voices, list) or not all(isinstance(item, dict) and isinstance(item.get("ShortName"), str) for item in voices):
		raise PluginError("语音服务返回的人声列表格式不正确。")
	voices.sort(key=lambda item: item["ShortName"])
	pages = max(1, math.ceil(len(voices) / PAGE_SIZE))
	if page > pages:
		raise PluginError(f"页码超出范围，当前共 {pages} 页。")
	aliases = {}
	for key, value in cli.ALIASES.items():
		if not key.isascii():
			aliases.setdefault(value, key)
	lines = [f"实时人声：{locale}，共 {len(voices)} 种，第 {page}/{pages} 页"]
	for item in voices[(page - 1) * PAGE_SIZE:page * PAGE_SIZE]:
		short = item["ShortName"]
		label = aliases.get(short, "")
		lines.append(f"{short} | {item.get('Gender', '')}" + (f" | {label}" if label else ""))
	if not voices:
		lines.append("该语言当前没有可用人声。")
	lines.append("调用：语音 完整ID|你好；下一页：语音人声 " + locale + " " + str(min(page + 1, pages)))
	return {"ok": True, "kind": "text", "text": "\n".join(lines), "count": len(voices), "page": page, "pages": pages}


def reference_samples(request, cli):
	catalog = cli.reference_catalog()
	voices = catalog["voices"]
	if request["action"] == "sample":
		voice = voice_id(request.get("text", ""), cli.ALIASES)
		item = next((item for item in voices if item["id"] == voice), None)
		if item is None:
			raise PluginError("参考页没有这个声音的样本；可用“语音试听 完整ID”实时生成。")
		url = item["sample_url"]
		if not re.fullmatch(r"https://jangviktor-web\.github\.io/novel2voice/scripts/voice_samples/[A-Za-z0-9_-]+\.mp3", url):
			raise PluginError("参考样本地址不正确，请重新安装目录文件。")
		return {"ok": True, "kind": "sample", "sample_url": url, "voice": voice}
	lines = [f"参考页共 {len(voices)} 个现成样本（与实时合成目录不同）："]
	for item in voices:
		state = "已列出" if item["edge_listed_at_check"] else "未列出"
		lines.append(f"{item['name']} | {item['id']} | 核对时{state}")
	lines.extend((
		"核对时间：" + catalog.get("retrieved_at", ""),
		"播放现成样本：语音示例 晓涵；当前可合成列表：语音人声 all 1。",
		catalog.get("source_page", ""),
	))
	return {"ok": True, "kind": "text", "text": "\n".join(lines), "count": len(voices)}


def handle_request(request, root=None):
	root = Path(root) if root is not None else ROOT
	try:
		if not isinstance(request, dict):
			raise PluginError("请求必须是 JSON 对象。")
		allowed = {"action", "text", "voice", "rate", "pitch", "volume", "locale", "page"}
		if set(request) - allowed:
			raise PluginError("请求含不支持的参数。")
		if len(json.dumps(request, ensure_ascii=False).encode("utf-8")) > MAX_REQUEST_BYTES:
			raise PluginError("请求过长，请缩短文案和参数。")
		action = request.get("action", "synthesize")
		if action == "help":
			return {"ok": True, "kind": "text", "text": HELP}
		config = load_config(root)
		cli = load_cli(root)
		if action in ("samples", "sample"):
			return reference_samples(request, cli)
		if action == "status":
			if not venv_python(root).is_file():
				raise PluginError("插件环境未创建，请运行 setup_env.cmd。")
			probe = subprocess.run(
				[str(venv_python(root)), "-I", "-c", DEPENDENCY_PROBE],
				capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5,
				cwd=root, env=child_environment(),
				creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
			)
			if probe.returncode:
				raise PluginError("虚拟环境依赖不完整，请在本机重新运行 setup_env.cmd。")
			return {"ok": True, "kind": "text", "text": f"Edge TTS 环境就绪，edge-tts {probe.stdout.strip()}。默认人声 {config['voice']}，总超时 {config['timeout']} 秒，并发 {config['max_concurrent']}。网络合成尚未在此检查中测试。"}
		if action == "chat":
			request = chat_request(request.get("text", ""), cli.ALIASES)
			action = "synthesize"
		if action == "preview":
			request = {"action": "synthesize", "voice": request.get("text", ""), "text": "你好，欢迎使用文字转语音。这是一段实时生成的人声试听。"}
			action = "synthesize"
		if action == "synthesize":
			return synthesize(request, root, config, cli)
		if action == "voices":
			return list_voices(request, root, config, cli)
		raise PluginError("不支持的操作。请发送“语音帮助”。")
	except subprocess.TimeoutExpired:
		return fail("语音请求超时，请稍后重试或缩短文字。")
	except Exception as exc:
		return fail(str(exc)[:400] or type(exc).__name__)


def main(argv=None):
	# 只接收固定 Base64 token，命令行中没有原始聊天文字或可控路径。
	arguments = list(sys.argv[1:] if argv is None else argv)
	try:
		if len(arguments) != 2 or arguments[0] != "--request-base64":
			raise PluginError("入口需要 --request-base64 参数，请通过插件调用。")
		if len(arguments[1]) > 4 * ((MAX_REQUEST_BYTES + 2) // 3):
			raise PluginError("请求过长，请缩短文案和参数。")
		data = base64.b64decode(arguments[1], validate=True)
		if len(data) > MAX_REQUEST_BYTES:
			raise PluginError("请求过长，请缩短文案和参数。")
		request = json.loads(data.decode("utf-8"))
		result = handle_request(request)
	except (ValueError, UnicodeError, binascii.Error, PluginError) as exc:
		result = fail(str(exc))
	# 旧版【命令行】在 Windows 按 GBK 解码，ASCII JSON 可在两种平台完整传输。
	print(json.dumps(result, ensure_ascii=True, separators=(",", ":")), flush=True)
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
