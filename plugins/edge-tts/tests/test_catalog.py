"""静态参考目录、别名兼容与样本/实时试听分流；全部测试不访问网络。"""

from __future__ import annotations

import asyncio
import base64
import copy
import importlib.util
import json
import re
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

PLUGIN = Path(__file__).resolve().parents[1]


def load_module(name, filename):
	spec = importlib.util.spec_from_file_location(name, PLUGIN / filename)
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


class ReferenceCatalogTests(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		cls.cli = load_module("_catalog_test_cli", "tts_cli.py")
		cls.runner = load_module("_catalog_test_runner", "redreply_plugin.py")
		cls.catalog = cls.cli.reference_catalog()

	def test_catalog_has_28_unique_references(self):
		voices = self.catalog["voices"]
		self.assertEqual(len(voices), 28)
		for field in ("id", "name", "sample_url"):
			self.assertEqual(len({item[field] for item in voices}), 28, field)
		for item in voices:
			self.assertIsInstance(item["edge_listed_at_check"], bool)
			self.assertRegex(
				item["sample_url"],
				r"^https://jangviktor-web\.github\.io/novel2voice/scripts/voice_samples/[A-Za-z0-9_-]+\.mp3$",
			)

	def test_previous_chinese_aliases_keep_their_meaning(self):
		expected = {
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
		}
		for name, identifier in expected.items():
			with self.subTest(name=name):
				self.assertEqual(self.cli.ALIASES[name], identifier)

	def test_reference_spelling_aliases_are_supported(self):
		expected = {
			"晓依": "zh-CN-XiaoyiNeural",
			"晓蔓": "zh-HK-HiuMaanNeural",
			"晓晨": "zh-TW-HsiaoChenNeural",
		}
		for name, identifier in expected.items():
			with self.subTest(name=name):
				self.assertEqual(self.cli.ALIASES[name], identifier)
				self.assertEqual(self.runner.voice_id(name, self.cli.ALIASES), identifier)

	def test_wu_yunzhe_does_not_replace_taiwan_yunjhe(self):
		self.assertEqual(self.cli.ALIASES["云哲"], "zh-TW-YunJheNeural")
		for name in ("云哲（吴语）", "云哲(吴语)", "吴语云哲"):
			with self.subTest(name=name):
				self.assertEqual(self.cli.ALIASES[name], "wuu-CN-YunzheNeural")

	def test_static_listing_does_not_load_network_engine(self):
		args = self.cli.build_parser().parse_args(["--list-reference-voices", "--json"])
		with patch.object(self.cli, "load_engine", side_effect=AssertionError("静态目录不得加载联网引擎")):
			result = asyncio.run(self.cli.run(args))
		self.assertTrue(result["ok"])
		self.assertEqual(len(result["reference_voices"]), 28)
		self.assertEqual(result["source_page"], self.catalog["source_page"])

	def test_static_listing_runs_without_site_packages(self):
		result = subprocess.run(
			[sys.executable, "-I", "-S", str(PLUGIN / "tts_cli.py"), "--list-reference-voices", "--json"],
			capture_output=True, timeout=10,
		)
		self.assertEqual(result.returncode, 0, result.stderr)
		self.assertEqual(result.stderr, b"")
		payload = json.loads(result.stdout)
		self.assertTrue(payload["ok"])
		self.assertEqual(len(payload["reference_voices"]), 28)

	def test_every_reference_name_returns_its_fixed_sample_url(self):
		with patch.object(self.runner, "run_cli", side_effect=AssertionError("现成样本不得实时合成")):
			for item in self.catalog["voices"]:
				with self.subTest(name=item["name"]):
					result = self.runner.handle_request({"action": "sample", "text": item["name"]}, root=PLUGIN)
					self.assertTrue(result["ok"], result)
					self.assertEqual(result["kind"], "sample")
					self.assertEqual(result["voice"], item["id"])
					self.assertEqual(result["sample_url"], item["sample_url"])
					self.assertNotIn("audio_base64", result)

	def test_sample_action_rejects_arbitrary_remote_urls(self):
		with patch.object(self.runner, "run_cli", side_effect=AssertionError("不得调用CLI")):
			result = self.runner.handle_request({"action": "sample", "text": "https://example.com/file.mp3"}, root=PLUGIN)
		self.assertFalse(result["ok"])
		self.assertIn("error", result)

	def test_altered_catalog_cannot_redirect_sample_host(self):
		catalog = copy.deepcopy(self.catalog)
		catalog["voices"][0]["sample_url"] = "https://example.com/not-the-reference.mp3"
		fake_cli = types.SimpleNamespace(
			ALIASES=self.cli.ALIASES,
			reference_catalog=lambda: catalog,
		)
		with self.assertRaises(self.runner.PluginError):
			self.runner.reference_samples({"action": "sample", "text": catalog["voices"][0]["name"]}, fake_cli)

	def test_samples_catalog_is_text_and_does_not_synthesize(self):
		with patch.object(self.runner, "run_cli", side_effect=AssertionError("目录不得实时合成")):
			result = self.runner.handle_request({"action": "samples"}, root=PLUGIN)
		self.assertTrue(result["ok"], result)
		self.assertEqual(result["kind"], "text")
		self.assertEqual(result["count"], 28)
		self.assertIn("核对时未列出", result["text"])
		self.assertIn("云哲（吴语）", result["text"])

	def test_preview_performs_synthesis_with_resolved_voice(self):
		captured = {}
		def fake_run_cli(root, arguments, text, config):
			captured.update(root=root, arguments=arguments, text=text, config=config)
			output = Path(arguments[arguments.index("--output") + 1])
			output.write_bytes(b"ID3-preview-transport-fixture")
			return {"ok": True}
		with tempfile.TemporaryDirectory(prefix="tts_preview_") as folder:
			root = Path(folder)
			for name in ("tts_cli.py", "voice_reference.json"):
				shutil.copy2(PLUGIN / name, root / name)
			with patch.object(self.runner, "run_cli", side_effect=fake_run_cli) as run_mock:
				result = self.runner.handle_request({"action": "preview", "text": "晓依"}, root=root)
			self.assertTrue(result["ok"], result)
			self.assertEqual(result["kind"], "audio")
			self.assertEqual(result["voice"], "zh-CN-XiaoyiNeural")
			self.assertEqual(base64.b64decode(result["audio_base64"]), b"ID3-preview-transport-fixture")
			self.assertEqual(run_mock.call_count, 1)
			self.assertTrue(captured["text"])
			self.assertIn("zh-CN-XiaoyiNeural", captured["arguments"])
			self.assertNotIn("sample_url", result)
			self.assertEqual(list(root.glob(".edge-tts-*")), [])


if __name__ == "__main__":
	unittest.main(verbosity=2)
