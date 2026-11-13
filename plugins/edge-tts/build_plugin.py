#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成并核对 RedLang 脚本包，打包时只包含明确列出的运行文件。"""

from __future__ import annotations

import argparse
import html
import json
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FILES = (
	"app.json", "script.json", "redreply_plugin.py", "tts_cli.py",
	"setup_venv.py", "setup_env.cmd", "一键创建环境.bat", "tts.cmd",
	"requirements.txt", "config.example.json", "example_text.txt", "README.md",
	"voice_reference.json", "voice_samples.html",
)


def raw(text):
	return "【@" + text + "】"


def command(name, *args):
	return "【" + name + ("@" + "@".join(args) if args else "") + "】"


def variable(name):
	return command("变量", "edge_tts_" + name)


def assign(name, value):
	return command("定义变量", "edge_tts_" + name, value)


def field(name):
	return command("取元素", variable("result"), name)


def error(message):
	return command("对象", "ok", "假", "error", raw(message))


def request_body():
	payload = command("转字节集", command("转文本", command("参数", "1")))
	windows = raw('".venv/Scripts/python.exe" -I "redreply_plugin.py" --request-base64 "')
	posix = raw('".venv/bin/python" -I "redreply_plugin.py" --request-base64 "')
	launch = assign("command", command("判等", command("目录分隔符"), "/", windows, posix))
	launch += assign("result", command("JSON解析", command("命令行", variable("command") + command("BASE64编码", variable("payload")) + raw('"'))))
	launch += command(
		"判等", command("取类型", variable("result")), "O",
		error("插件启动失败或输出格式错误，请在本机插件目录运行 setup_env.cmd；Linux 使用 python3 setup_venv.py。详细错误见运行日志。"),
		variable("result"),
	)
	return assign("payload", payload) + command(
		"判真", command("计算", command("取长度", variable("payload")) + ">5000"),
		launch, error("请求超过 5000 字节，请缩短文案和参数。"),
	)


def display_body():
	audio = command("语音", command("BASE64解码", field("audio_base64")))
	sample = command("语音", field("sample_url"))
	text = command("CQ转义", field("text"))
	success = command("判等", field("kind"), "audio", command("判等", field("kind"), "sample", text, sample), audio)
	return assign("result", command("参数", "1")) + command("判真", field("ok"), command("CQ转义", "语音生成失败：" + field("error")), success)


def entry(name, keyword, code, match="完全匹配", trigger="群、私聊触发"):
	return {
		"name": name, "description": name,
		"content": {"关键词": keyword, "匹配方式": match, "触发方式": trigger, "code": code},
	}


def scripts():
	arguments = [command("参数", str(i)) for i in range(1, 6)]
	request = command("对象", "action", "synthesize", "text", arguments[0], "voice", arguments[1], "rate", arguments[2], "pitch", arguments[3], "volume", arguments[4])
	init = command("定义命令", "EdgeTTS请求", request_body())
	init += command("定义命令", "EdgeTTS展示", display_body())
	init += command("定义命令", "EdgeTTS", command("EdgeTTS请求", request))
	init += command("定义命令", "EdgeTTS语音", command("EdgeTTS展示", command("EdgeTTS", *arguments)))
	def action(name, text=None):
		parts = ["action", name]
		if text is not None:
			parts += ["text", command("CQ反转义", text)]
		return command("EdgeTTS展示", command("EdgeTTS请求", command("对象", *parts)))
	help_text = raw("语音 你好\n语音 云希|你好\n语音 云希|+20%|-5Hz|0%|你好\n语音人声 all 1：全部实时人声\n语音人声 zh 1：中文人声\n语音样本：参考页全部 28 种声音\n语音示例 晓涵：播放参考页现成样本\n语音试听 晓晓：实时生成一段试听\n语音状态：检查本机环境")
	return [
		entry("Edge TTS 初始化", "", init, trigger="框架初始化"),
		entry("Edge TTS 语音", "语音 ", action("chat", command("子关键词")), match="前缀匹配"),
		entry("Edge TTS 朗读", "朗读 ", action("chat", command("子关键词")), match="前缀匹配"),
		entry("Edge TTS 人声", "语音人声", action("voices", command("子关键词")), match="前缀匹配"),
		entry("Edge TTS 状态", "语音状态", action("status")),
		entry("Edge TTS 样本目录", "语音样本", action("samples")),
		entry("Edge TTS 参考示例", "语音示例 ", action("sample", command("子关键词")), match="前缀匹配"),
		entry("Edge TTS 实时试听", "语音试听 ", action("preview", command("子关键词")), match="前缀匹配"),
		entry("Edge TTS 帮助", "语音帮助", help_text),
		entry("Edge TTS 空输入帮助", "语音", help_text),
		entry("Edge TTS 朗读帮助", "朗读", help_text),
		entry("Edge TTS 脚本错误", "", raw("语音插件执行失败，请查看红色问答运行日志，并在本机插件目录重新运行 setup_env.cmd 检查环境。"), trigger="脚本错误"),
	]


def samples_html(catalog):
	escape = html.escape
	rows = []
	for voice in catalog["voices"]:
		status = "核对时已列出" if voice["edge_listed_at_check"] else "核对时未列出"
		rows.append(
			"<tr><td>" + escape(voice["name"]) + "</td><td><code>" + escape(voice["id"]) +
			"</code></td><td>" + status + "</td><td><audio controls preload=\"none\" src=\"" +
			escape(voice["sample_url"], quote=True) + "\"></audio></td></tr>"
		)
	return """<!doctype html>
<html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Edge TTS · 28 种参考声音</title>
<style>body{font:16px/1.65 system-ui,sans-serif;margin:32px auto;max-width:1150px;padding:0 20px;color:#192435;background:#f7f9fc}h1{margin-bottom:8px}input{font:inherit;padding:10px;width:min(90%,500px);border:1px solid #bbc7d9;border-radius:6px}table{width:100%;border-collapse:collapse;background:white;margin-top:24px}td,th{text-align:left;padding:12px;border-bottom:1px solid #dae2ed}code{font-size:13px}audio{width:230px;height:36px}.scroll{overflow:auto}a{color:#185abc}</style>
<h1>28 种参考声音</h1><p>这些播放器播放原参考页的现成 MP3。自定义文字能否合成，请用插件“语音人声”查询当前服务；下表是一次核对快照，不是永久承诺。</p>
<p>音频按需从参考项目加载，需联网。<a href=""" + '"' + escape(catalog["source_page"], quote=True) + '"' + ">原始试听页</a> · 核对时间：" + escape(catalog["retrieved_at"]) + """</p>
<label>筛选名称或 ID <input id="filter" type="search" placeholder="例如：晓涵、吴语、zh-HK"></label>
<div class="scroll"><table><thead><tr><th>名称</th><th>完整人声 ID</th><th>Edge 目录状态</th><th>参考样本</th></tr></thead><tbody>""" + "".join(rows) + """</tbody></table></div>
<script>document.getElementById('filter').addEventListener('input',function(){const q=this.value.toLowerCase();document.querySelectorAll('tbody tr').forEach(r=>r.hidden=!r.textContent.toLowerCase().includes(q));});document.querySelectorAll('audio').forEach(a=>a.addEventListener('play',()=>document.querySelectorAll('audio').forEach(b=>{if(a!==b)b.pause();})));</script></html>
"""


def main():
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("--check", action="store_true", help="只核对已提交的生成文件")
	parser.add_argument("--output", type=Path, help="生成可解压安装的 ZIP")
	args = parser.parse_args()
	generated = {"script.json": json.dumps(scripts(), ensure_ascii=False, indent="\t") + "\n"}
	catalog_path = ROOT / "voice_reference.json"
	if catalog_path.is_file():
		generated["voice_samples.html"] = samples_html(json.loads(catalog_path.read_text(encoding="utf-8")))
	for name, content in generated.items():
		path = ROOT / name
		if args.check:
			if not path.is_file() or path.read_text(encoding="utf-8") != content:
				parser.error(f"{name} 未更新，请先运行 build_plugin.py。")
		else:
			path.write_text(content, encoding="utf-8", newline="\n")
	if args.output:
		args.output.parent.mkdir(parents=True, exist_ok=True)
		with zipfile.ZipFile(args.output, "w", zipfile.ZIP_DEFLATED) as archive:
			for name in FILES:
				archive.write(ROOT / name, "EdgeTTS/" + name)
			archive.write(ROOT.parents[1] / "LICENSE", "EdgeTTS/LICENSE")
		print(args.output)
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
