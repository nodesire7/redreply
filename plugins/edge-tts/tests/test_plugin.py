"""插件黑盒协议测试：真实 Python 子进程，语音服务由本地假 CLI 代替。"""

from __future__ import annotations

import base64
import json
import os
import re
import runpy
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import venv
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1]

# 只模拟 CLI 的协议和进程行为，不调用 Edge，不将测试字节称为可播放音频。
FAKE_CLI = r'''
import json
import os
import sys
import time
from pathlib import Path

for handle in (sys.stdout, sys.stderr):
	handle.reconfigure(encoding="utf-8", errors="replace")

root = Path(__file__).resolve().parent
arguments = sys.argv[1:]

def option(*names):
	for name in names:
		if name in arguments:
			return arguments[arguments.index(name) + 1]
	return None

if "--version" in arguments:
	print("TTS CLI test fixture")
	sys.exit(0)

if "--list-voices" in arguments:
	locale = option("--locale") or ""
	voices = [
		{"ShortName": "zh-CN-XiaoxiaoNeural", "Gender": "Female", "Locale": "zh-CN"},
		{"ShortName": "zh-CN-YunxiNeural", "Gender": "Male", "Locale": "zh-CN"},
		{"ShortName": "en-US-JennyNeural", "Gender": "Female", "Locale": "en-US"},
	]
	voices = [voice for voice in voices if voice["Locale"].lower().startswith(locale.lower())]
	print(json.dumps({"ok": True, "count": len(voices), "voices": voices}, ensure_ascii=False))
	sys.exit(0)

text = sys.stdin.buffer.read().decode("utf-8")
capture = {"stdin": text, "argv": arguments}
(root / ("capture-" + str(os.getpid()) + ".json")).write_text(
	json.dumps(capture, ensure_ascii=False), encoding="utf-8"
)
if text == "__SLEEP__":
	(root / ("started-" + str(os.getpid()))).write_text("started", encoding="ascii")
	time.sleep(5)
if text == "__HOLD__":
	(root / ("started-" + str(os.getpid()))).write_text("started", encoding="ascii")
	deadline = time.monotonic() + 5
	while not (root / "release-hold").exists() and time.monotonic() < deadline:
		time.sleep(0.02)
if text == "__FAIL__":
	print("服务的中文诊断，只能留在错误结果中。", file=sys.stderr)
	print(json.dumps({"ok": False, "error": "模拟服务失败"}, ensure_ascii=False))
	sys.exit(1)
if text == "__BAD_JSON__":
	print("not a JSON result")
	sys.exit(0)
if text == "__CRASH__":
	raise RuntimeError("模拟异常")

output = Path(option("--output", "-o"))
output.parent.mkdir(parents=True, exist_ok=True)
if text != "__NO_FILE__":
	audio = b"" if text == "__EMPTY_AUDIO__" else b"ID3-test-transport:" + text.encode("utf-8")
	output.write_bytes(audio)
print(json.dumps({
	"ok": True,
	"voice": option("--voice", "-v") or "zh-CN-XiaoxiaoNeural",
	"total": 1,
	"completed": 1,
	"failed": 0,
	"unprocessed": 0,
	"results": [{"ok": True, "output": str(output)}],
}, ensure_ascii=False))
'''
FAKE_CLI = (
	"ALIASES = {'晓晓': 'zh-CN-XiaoxiaoNeural', '云希': 'zh-CN-YunxiNeural'}\n\n"
	"def main():\n"
	+ "\n".join("\t" + line for line in FAKE_CLI.splitlines())
	+ '\n\nif __name__ == "__main__":\n\tmain()\n'
)


class PluginProtocolTests(unittest.TestCase):
	def setUp(self):
		self.temp = tempfile.TemporaryDirectory(prefix="edge_中文 空格!_")
		self.root = Path(self.temp.name)
		self.runner = self.root / "redreply_plugin.py"
		shutil.copy2(PLUGIN / "redreply_plugin.py", self.runner)
		(self.root / "tts_cli.py").write_text(FAKE_CLI, encoding="utf-8")
		self.config = {
			"voice": "晓晓", "rate": 0, "pitch": 0, "volume": 0,
			"timeout": 12, "retries": 1, "max_chars": 1000, "max_concurrent": 2,
		}
		self.write_config()
		venv.EnvBuilder(with_pip=False).create(self.root / ".venv")
		self.python = self.root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
		self.running = []

	def tearDown(self):
		for process in self.running:
			if process.poll() is None:
				process.kill()
			process.communicate()
		self.temp.cleanup()

	def write_config(self):
		(self.root / "config.json").write_text(json.dumps(self.config, ensure_ascii=False), encoding="utf-8")

	def command(self, request):
		raw = json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
		token = base64.b64encode(raw).decode("ascii")
		return [str(self.python), "-I", str(self.runner), "--request-base64", token]

	def decode_result(self, result):
		self.assertEqual(result.stderr, b"", "runner 的 stderr 会污染旧宿主拼接后的 JSON")
		self.assertTrue(result.stdout.isascii(), "旧宿主按 GBK 解码，传输结果应保持 ASCII")
		decoded = json.loads(result.stdout)
		self.assertIsInstance(decoded, dict)
		self.assertIsInstance(decoded.get("ok"), bool)
		return decoded

	def call(self, request, timeout=10):
		result = subprocess.run(self.command(request), capture_output=True, timeout=timeout)
		return self.decode_result(result)

	def captures(self):
		return [
			json.loads(path.read_text(encoding="utf-8"))
			for path in self.root.glob("capture-*.json")
		]

	def assert_failure(self, result):
		self.assertFalse(result["ok"])
		self.assertIsInstance(result.get("error"), str)
		self.assertTrue(result["error"].strip())
		self.assertNotIn("audio_base64", result)

	def test_multiline_and_shell_characters_are_only_data(self):
		text = '第一行：@【不平衡\n" & | > < ^ %PATH% !变量! \\\\ ## 不应截断\n[CQ:at,qq=all]\n第二行😀'
		result = self.call({"action": "synthesize", "text": text})
		self.assertTrue(result["ok"], result)
		self.assertEqual(result["kind"], "audio")
		self.assertEqual(base64.b64decode(result["audio_base64"]), b"ID3-test-transport:" + text.encode("utf-8"))
		self.assertEqual(self.captures()[0]["stdin"], text)
		self.assertNotIn(text, self.captures()[0]["argv"])

	def test_cli_fixture_uses_utf8_even_with_cp1252_standard_streams(self):
		# 强制模拟英文 Windows 的初始标准流，防止 Linux 默认 UTF-8 掩盖夹具差异。
		bootstrap = (
			"import runpy, sys\n"
			"for handle in (sys.stdin, sys.stdout, sys.stderr):\n"
			"\thandle.reconfigure(encoding='cp1252', errors='strict')\n"
			"script = sys.argv[1]\n"
			"sys.argv = sys.argv[1:]\n"
			"runpy.run_path(script, run_name='__main__')\n"
		)
		output = self.root / "中文输出.mp3"
		for text in ("中文输入😀\n第二行", "__FAIL__"):
			with self.subTest(text=text):
				result = subprocess.run(
					[str(self.python), "-I", "-c", bootstrap, str(self.root / "tts_cli.py"), "--stdin", "--json", "--output", str(output)],
					input=text.encode("utf-8"), capture_output=True, timeout=10,
				)
				if text == "__FAIL__":
					self.assertEqual(result.returncode, 1)
					self.assertEqual(json.loads(result.stdout.decode("utf-8"))["error"], "模拟服务失败")
					self.assertIn("服务的中文诊断", result.stderr.decode("utf-8"))
				else:
					self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", errors="replace"))
					response = json.loads(result.stdout.decode("utf-8"))
					self.assertTrue(response["ok"])
					self.assertEqual(response["results"][0]["output"], str(output))
					self.assertEqual(output.read_bytes(), b"ID3-test-transport:" + text.encode("utf-8"))
					self.assertEqual(result.stderr, b"")

	def test_parameters_are_forwarded_as_arguments(self):
		result = self.call({
			"action": "synthesize", "text": "参数测试", "voice": "云希",
			"rate": -15, "pitch": 5, "volume": -20,
		})
		self.assertTrue(result["ok"], result)
		arguments = self.captures()[0]["argv"]
		joined = " ".join(arguments)
		self.assertIn("Yunxi", joined)
		self.assertIn("-15", joined)
		self.assertIn("-20", joined)
		self.assertIn("--stdin", arguments)
		self.assertIn("--json", arguments)

	def test_actual_host_shell_boundary_uses_only_ascii_base64(self):
		text = '你好😀\n" & echo INJECTED > injected.txt & rem " $(touch injected.txt) ' + chr(96) + 'touch injected.txt' + chr(96)
		token = self.command({"action": "synthesize", "text": text})[-1]
		# 使用发行包内的原始字面量，避免测试拼出的路径与实际 RedLang 命令不同。
		scripts = json.loads((PLUGIN / "script.json").read_text(encoding="utf-8"))
		builder = runpy.run_path(str(PLUGIN / "build_plugin.py"))
		self.assertEqual(scripts, builder["scripts"](), "script.json 必须与生成器一致")
		initialization = next(item["content"]["code"] for item in scripts if item["content"]["触发方式"] == "框架初始化")
		prefixes = [literal for literal in re.findall(r"【@([^】]*)】", initialization) if "--request-base64 " in literal]
		self.assertEqual(len(prefixes), 2, "应当各有一个 Windows 和 POSIX 原始命令前缀")
		prefix = next(value for value in prefixes if ("Scripts" in value) == (os.name == "nt"))
		suffix = re.search(re.escape("【BASE64编码@【变量@edge_tts_payload】】") + r"【@([^】]*)】", initialization)
		self.assertIsNotNone(suffix, "Base64 参数之后必须保留实际脚本的原始后缀")
		command = prefix + token + suffix.group(1)
		self.assertTrue(command.isascii())
		if os.name == "nt":
			batch = self.root / "host-command.cmd"
			batch.write_bytes(("@echo off\r\n" + command + "\r\n").encode("gbk"))
			process_args = ["cmd", "/c", str(batch)]
		else:
			process_args = ["sh", "-c", command]
		result = subprocess.run(process_args, cwd=self.root, capture_output=True, timeout=10)
		response = self.decode_result(result)
		self.assertTrue(response["ok"], response)
		self.assertEqual(json.loads(result.stdout.decode("gbk")), response)
		self.assertEqual(self.captures()[0]["stdin"], text)
		self.assertFalse((self.root / "injected.txt").exists())

	def test_full_non_default_locale_voice_is_accepted(self):
		result = self.call({"action": "synthesize", "text": "Bonjour", "voice": "fr-FR-DeniseNeural"})
		self.assertTrue(result["ok"], result)
		self.assertEqual(result["voice"], "fr-FR-DeniseNeural")
		self.assertIn("fr-FR-DeniseNeural", self.captures()[0]["argv"])

	def test_chat_parameters_preserve_remaining_pipes(self):
		result = self.call({"action": "chat", "text": "云希|+20%|-5Hz|0%|第一行\n第二行|保留竖线"})
		self.assertTrue(result["ok"], result)
		self.assertEqual(self.captures()[0]["stdin"], "第一行\n第二行|保留竖线")

	def test_disallowed_output_path_is_rejected(self):
		outside = self.root / "untrusted-output.mp3"
		result = self.call({"action": "synthesize", "text": "文本", "output": str(outside)})
		self.assert_failure(result)
		self.assertFalse(outside.exists())
		self.assertEqual(self.captures(), [])

	def test_boolean_prosody_is_rejected(self):
		self.assert_failure(self.call({"action": "synthesize", "text": "文本", "rate": True}))
		self.assertEqual(self.captures(), [])

	def test_partial_or_malformed_config_is_visible_error(self):
		(self.root / "config.json").write_text('{"voice":', encoding="utf-8")
		self.assert_failure(self.call({"action": "synthesize", "text": "文本"}))
		self.assertEqual(self.captures(), [])

	def test_empty_text_is_visible_error(self):
		self.assert_failure(self.call({"action": "synthesize", "text": " \n\t "}))
		self.assertEqual(self.captures(), [])

	def test_long_text_is_rejected_before_child(self):
		self.assert_failure(self.call({"action": "synthesize", "text": "字" * 1001}))
		self.assertEqual(self.captures(), [])

	def test_unknown_action_is_visible_error(self):
		self.assert_failure(self.call({"action": "install", "text": "pip install anything"}))
		self.assertEqual(self.captures(), [])

	def test_non_object_request_is_visible_error(self):
		self.assert_failure(self.call(["synthesize", "文本"]))

	def test_non_string_text_is_visible_error(self):
		self.assert_failure(self.call({"action": "synthesize", "text": {"code": "x"}}))
		self.assertEqual(self.captures(), [])

	def test_invalid_base64_is_single_json(self):
		command = [str(self.python), "-I", str(self.runner), "--request-base64", "%%%invalid%%%"]
		result = subprocess.run(command, capture_output=True, timeout=10)
		self.assert_failure(self.decode_result(result))

	def test_request_limit_is_checked_before_child(self):
		self.assert_failure(self.call({"action": "synthesize", "text": "x" * 5100}))
		self.assertEqual(self.captures(), [])

	def test_service_failure_and_stderr_are_visible_json(self):
		self.assert_failure(self.call({"action": "synthesize", "text": "__FAIL__"}))

	def test_malformed_child_output_is_visible_json(self):
		self.assert_failure(self.call({"action": "synthesize", "text": "__BAD_JSON__"}))

	def test_child_crash_is_visible_json(self):
		self.assert_failure(self.call({"action": "synthesize", "text": "__CRASH__"}))

	def test_missing_audio_is_visible_error(self):
		self.assert_failure(self.call({"action": "synthesize", "text": "__NO_FILE__"}))

	def test_empty_audio_is_visible_error(self):
		self.assert_failure(self.call({"action": "synthesize", "text": "__EMPTY_AUDIO__"}))

	def test_timeout_is_bounded_and_lock_is_released(self):
		self.config["timeout"] = 2
		self.config["max_concurrent"] = 1
		self.write_config()
		started = time.monotonic()
		self.assert_failure(self.call({"action": "synthesize", "text": "__SLEEP__"}, timeout=6))
		self.assertLess(time.monotonic() - started, 5)
		result = self.call({"action": "synthesize", "text": "超时后重试"})
		self.assertTrue(result["ok"], result)

	def test_parallel_processes_respect_shared_limit(self):
		for _ in range(2):
			process = subprocess.Popen(
				self.command({"action": "synthesize", "text": "__HOLD__"}),
				stdout=subprocess.PIPE, stderr=subprocess.PIPE,
			)
			self.running.append(process)
		deadline = time.monotonic() + 4
		while len(list(self.root.glob("started-*"))) < 2 and time.monotonic() < deadline:
			time.sleep(0.02)
		try:
			self.assertEqual(len(list(self.root.glob("started-*"))), 2, "两个持锁子进程未启动")
			self.assert_failure(self.call({"action": "synthesize", "text": "第三个请求"}))
		finally:
			(self.root / "release-hold").touch()
		for process in self.running:
			stdout, stderr = process.communicate(timeout=5)
			result = self.decode_result(subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr))
			self.assertTrue(result["ok"], result)
		result = self.call({"action": "synthesize", "text": "全部完成后"})
		self.assertTrue(result["ok"], result)

	def test_voices_are_filtered_and_return_text(self):
		result = self.call({"action": "voices", "locale": "zh", "page": 1})
		self.assertTrue(result["ok"], result)
		self.assertEqual(result["kind"], "text")
		self.assertIn("zh-CN", result["text"])
		self.assertNotIn("en-US-JennyNeural", result["text"])
		self.assertEqual(result["count"], 2)

	def test_temporary_audio_is_removed_after_result(self):
		result = self.call({"action": "synthesize", "text": "清理测试"})
		self.assertTrue(result["ok"], result)
		self.assertEqual(list(self.root.glob(".edge-tts-*")), [])
		self.assertTrue(base64.b64decode(result["audio_base64"]))

	def test_error_does_not_poison_next_request(self):
		self.config["max_concurrent"] = 1
		self.write_config()
		self.assert_failure(self.call({"action": "synthesize", "text": "__FAIL__"}))
		result = self.call({"action": "synthesize", "text": "下一条"})
		self.assertTrue(result["ok"], result)

	def test_missing_environment_has_clear_error(self):
		environment = self.root / ".venv"
		environment.rename(self.root / ".venv-saved")
		command = self.command({"action": "synthesize", "text": "缺少环境"})
		command[0] = sys.executable
		result = subprocess.run(command, capture_output=True, timeout=10)
		self.assert_failure(self.decode_result(result))


if __name__ == "__main__":
	unittest.main(verbosity=2)
