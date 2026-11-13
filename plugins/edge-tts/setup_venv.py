#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""创建并检查 TTS 工具的独立虚拟环境。Python 代码使用 Tab 缩进。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"
VENV_PYTHON = VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
REQUIREMENTS = ROOT / "requirements.txt"
STATE = VENV / ".tts-environment.json"

RUNTIME_PROBE = (
	"import json,sys;"
	"print(json.dumps({'version':list(sys.version_info[:3]),"
	"'prefix':sys.prefix,'base_prefix':sys.base_prefix}))"
)
DEPENDENCY_PROBE = (
	"import importlib.metadata,edge_tts;"
	"from edge_tts import SubMaker;"
	"v=importlib.metadata.version('edge-tts');"
	"n=tuple(int(x) for x in v.split('.')[:3]);"
	"assert (7,3,1)<=n<(8,), 'edge-tts version out of range';"
	"assert hasattr(SubMaker,'feed') and hasattr(SubMaker,'get_srt');"
	"print(v)"
)


class SetupError(Exception):
	pass


def child_environment():
	"""只影响子进程，避免外部 pip 配置把依赖安装到全局或其他目录。"""
	env = dict(os.environ)
	proxy_names = {"http_proxy", "https_proxy", "all_proxy", "ws_proxy", "wss_proxy", "no_proxy"}
	for name in list(env):
		if name.upper().startswith("PIP_") or name.upper() in ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV") or name.lower() in proxy_names:
			env.pop(name, None)
	env["PIP_CONFIG_FILE"] = os.devnull
	env["PIP_REQUIRE_VIRTUALENV"] = "true"
	env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
	env["PYTHONNOUSERSITE"] = "1"
	env["NO_PROXY"] = "*"
	return env


def run(command, env, capture=False):
	try:
		return subprocess.run(
			command, cwd=ROOT, env=env, check=False,
			stdout=subprocess.PIPE if capture else None,
			stderr=subprocess.PIPE if capture else None,
			encoding="utf-8", errors="replace",
		)
	except OSError as exc:
		raise SetupError(f"无法运行程序：{command[0]}。{exc}") from exc


def normalized(path):
	return os.path.normcase(os.path.realpath(path))


def probe_runtime(env):
	config = {}
	for line in (VENV / "pyvenv.cfg").read_text(encoding="utf-8").splitlines():
		if "=" in line:
			key, value = line.split("=", 1)
			config[key.strip().lower()] = value.strip().lower()
	if config.get("include-system-site-packages") != "false":
		raise SetupError("已有 .venv 未隔离全局依赖。请将它改名保留，再重新创建独立环境。")
	result = run([str(VENV_PYTHON), "-I", "-c", RUNTIME_PROBE], env, capture=True)
	if result.returncode:
		raise SetupError("已有 .venv 无法运行。请先把该 .venv 文件夹改名保留，再重新双击创建环境。")
	try:
		info = json.loads(result.stdout)
	except (ValueError, TypeError) as exc:
		raise SetupError("虚拟环境检查未返回有效结果。") from exc
	if tuple(info["version"]) < (3, 10):
		raise SetupError("已有虚拟环境的 Python 低于 3.10，需要使用较新 Python 重新创建。")
	if normalized(info["prefix"]) != normalized(VENV) or normalized(info["prefix"]) == normalized(info["base_prefix"]):
		raise SetupError("检测到解释器没有使用本工具的 .venv，已停止安装。")
	return ".".join(map(str, info["version"]))


def load_state():
	try:
		value = json.loads(STATE.read_text(encoding="utf-8"))
		return value if isinstance(value, dict) else {}
	except (OSError, ValueError):
		return {}


def setup(args):
	if sys.version_info < (3, 10):
		raise SetupError("请先安装 Python 3.10 或更新版本，推荐使用 Python 3.12。")
	if not REQUIREMENTS.is_file() or not (ROOT / "tts_cli.py").is_file():
		raise SetupError("工具文件不完整，请完整解压 ZIP 后再运行，不要直接在压缩包内运行。")
	for name, value in (("--index-url", args.index_url), ("--proxy", args.proxy)):
		if value:
			url = urlsplit(value)
			if url.scheme not in ("http", "https") or not url.hostname:
				raise SetupError(f"{name} 需要有效的 http:// 或 https:// 地址。")
	if VENV.is_symlink():
		raise SetupError(".venv 不能是符号链接，请使用本工具目录中的独立环境。")
	env = child_environment()
	fingerprint = hashlib.sha256(REQUIREMENTS.read_bytes()).hexdigest()
	state = load_state()
	if state.get("project_root") and normalized(state["project_root"]) != normalized(ROOT):
		raise SetupError("工具目录已移动。请把现有 .venv 改名保留，再重新运行安装脚本创建当前目录的环境。")

	print(f"工具目录：{ROOT}", flush=True)
	print("[1/3] 检查或创建虚拟环境……", flush=True)
	if VENV.exists():
		if not VENV.is_dir():
			raise SetupError(".venv 已存在但不是文件夹，请先处理同名文件。")
		if not (VENV / "pyvenv.cfg").is_file() or not VENV_PYTHON.is_file():
			raise SetupError("已有 .venv 不完整；请先将它改名保留，再重新运行。安装程序不会删除已有文件夹。")
	else:
		result = run([sys.executable, "-I", "-m", "venv", str(VENV)], env)
		if result.returncode:
			raise SetupError("创建虚拟环境失败。请检查文件夹写入权限及 Python 的 venv/ensurepip 组件。")
	version = probe_runtime(env)
	print(f"虚拟环境 Python：{version}", flush=True)
	pip_command = [str(VENV_PYTHON), "-I", "-m", "pip"]
	if run(pip_command + ["--version"], env, capture=True).returncode:
		print("正在为虚拟环境补齐 pip……", flush=True)
		if run([str(VENV_PYTHON), "-I", "-m", "ensurepip", "--upgrade"], env).returncode:
			raise SetupError("无法为虚拟环境安装 pip，请检查当前 Python 安装。")

	dependency_result = run([str(VENV_PYTHON), "-I", "-c", DEPENDENCY_PROBE], env, capture=True)
	healthy = dependency_result.returncode == 0
	if healthy:
		healthy = run(pip_command + ["check"], env, capture=True).returncode == 0
	unchanged = state.get("requirements_sha256") == fingerprint
	if healthy and unchanged and not args.upgrade:
		print("[2/3] 现有依赖已就绪，直接复用，不重新下载。", flush=True)
	else:
		print("[2/3] 正在向 .venv 安装依赖，需要联网……", flush=True)
		command = pip_command + [
			"--require-virtualenv", "install", "--disable-pip-version-check",
			"--no-input", "--retries", "2", "--timeout", "20",
			"--index-url", args.index_url, "-r", str(REQUIREMENTS),
		]
		if args.proxy:
			command += ["--proxy", args.proxy]
		if args.upgrade:
			command.append("--upgrade")
		if run(command, env).returncode:
			raise SetupError("依赖安装失败，详细错误见上方。检查网络后再次运行即可继续安装。")

	print("[3/3] 验证依赖和 CLI……", flush=True)
	checks = (
		[str(VENV_PYTHON), "-I", "-c", DEPENDENCY_PROBE],
		pip_command + ["check"],
		[str(VENV_PYTHON), "-I", str(ROOT / "tts_cli.py"), "--version"],
	)
	for command in checks:
		if run(command, env, capture=True).returncode:
			raise SetupError("安装后的检查未通过，请重新运行；如需更新依赖，可运行 setup_env.cmd --upgrade。")
	payload = {"project_root": str(ROOT), "requirements_sha256": fingerprint, "python_version": version}
	temporary = STATE.with_suffix(".tmp")
	temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
	os.replace(temporary, STATE)
	print("\n环境已就绪。以后使用 tts.cmd，自动调用本目录的虚拟环境。", flush=True)
	print('PowerShell：.\\tts.cmd -t "你好，欢迎使用。" -v 晓晓 -o hello.mp3', flush=True)
	print("查询人声：.\\tts.cmd --list-voices --locale zh", flush=True)
	print("完整帮助：.\\tts.cmd --help", flush=True)
	return 0


def main():
	for stream in (sys.stdout, sys.stderr):
		if hasattr(stream, "reconfigure"):
			stream.reconfigure(encoding="utf-8", errors="replace")
	parser = argparse.ArgumentParser(description="创建并安装 TTS CLI 的独立 .venv 虚拟环境")
	parser.add_argument("--upgrade", action="store_true", help="明确要求更新已安装依赖")
	parser.add_argument("--index-url", default="https://pypi.org/simple", help="pip 软件源，默认 PyPI 官方源")
	parser.add_argument("--proxy", help="安装依赖时显式使用 HTTP/HTTPS 代理；默认直连")
	args = parser.parse_args()
	try:
		return setup(args)
	except KeyboardInterrupt:
		print("\n安装已取消。", file=sys.stderr)
		return 130
	except (SetupError, OSError) as exc:
		print(f"\n错误：{exc}", file=sys.stderr)
		return 1


if __name__ == "__main__":
	sys.exit(main())
