"""バージョン同期スクリプトのテスト"""

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_REAL_ROOT = ROOT
_FILES = [
    "pyproject.toml",
    "scripts/sync_version.py",
    "scripts/check_version_sync.py",
    "lightroom_sdk/__init__.py",
    "lightroom_sdk/plugin/Info.lua",
    "lightroom_sdk/plugin/PluginInit.lua",
    ".claude-plugin/marketplace.json",
    "plugin/.claude-plugin/plugin.json",
]


@pytest.fixture(autouse=True)
def _tmp_root(tmp_path, monkeypatch):
    """Run everything against a byte-exact temp copy so pytest never rewrites tracked files."""
    for rel in _FILES:
        dst = tmp_path / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(_REAL_ROOT / rel, dst)
    monkeypatch.setitem(globals(), "ROOT", tmp_path)


def _read_pyproject_version() -> str:
    try:
        import tomllib
    except ModuleNotFoundError:
        import tomli as tomllib  # type: ignore[no-redef]

    with open(ROOT / "pyproject.toml", "rb") as f:
        data = tomllib.load(f)
    return data["project"]["version"]


def test_sync_version_updates_init_py(tmp_path):
    """pyproject.toml のバージョンが __init__.py に反映されること"""
    # Run sync
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "sync_version.py")], capture_output=True, text=True, cwd=str(ROOT)
    )
    assert result.returncode == 0

    version = _read_pyproject_version()
    init_content = (ROOT / "lightroom_sdk" / "__init__.py").read_text()
    assert f'__version__ = "{version}"' in init_content


def test_sync_version_updates_plugin_init_lua():
    """PluginInit.lua の version が更新されること"""
    subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "sync_version.py")], capture_output=True, text=True, cwd=str(ROOT)
    )

    version = _read_pyproject_version()
    lua_content = (ROOT / "lightroom_sdk" / "plugin" / "PluginInit.lua").read_text()
    assert f'version = "{version}"' in lua_content


def test_sync_version_updates_info_lua():
    """Info.lua の VERSION が更新されること"""
    subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "sync_version.py")], capture_output=True, text=True, cwd=str(ROOT)
    )

    version = _read_pyproject_version()
    parts = version.split(".")
    info_content = (ROOT / "lightroom_sdk" / "plugin" / "Info.lua").read_text()
    m = re.search(r"VERSION\s*=\s*\{\s*major=(\d+),\s*minor=(\d+),\s*revision=(\d+)", info_content)
    assert m is not None
    assert m.group(1) == parts[0]
    assert m.group(2) == parts[1]
    assert m.group(3) == parts[2]


def test_sync_version_idempotent():
    """同じバージョンで2回実行しても問題ないこと"""
    for _ in range(2):
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "sync_version.py")], capture_output=True, text=True, cwd=str(ROOT)
        )
        assert result.returncode == 0


def test_check_version_sync_all_match():
    """sync 後に check が成功すること"""
    # First sync
    subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "sync_version.py")], capture_output=True, text=True, cwd=str(ROOT)
    )
    # Then check
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check_version_sync.py")], capture_output=True, text=True, cwd=str(ROOT)
    )
    assert result.returncode == 0
    assert "All versions in sync" in result.stdout


def test_sync_version_updates_marketplace_json():
    """marketplace.json の version が更新されること"""
    subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "sync_version.py")], capture_output=True, text=True, cwd=str(ROOT)
    )

    version = _read_pyproject_version()
    data = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text())
    assert data["plugins"][0]["version"] == version


def test_sync_version_updates_plugin_json():
    """plugin.json の version が更新されること"""
    subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "sync_version.py")], capture_output=True, text=True, cwd=str(ROOT)
    )

    version = _read_pyproject_version()
    data = json.loads((ROOT / "plugin" / ".claude-plugin" / "plugin.json").read_text())
    assert data["version"] == version


def test_check_version_sync_mismatch():
    """不一致がある場合 exit code 1 + 不一致箇所の出力"""
    # Temporarily break __init__.py version
    init_path = ROOT / "lightroom_sdk" / "__init__.py"
    original = init_path.read_text()
    init_path.write_text(original.replace(_read_pyproject_version(), "0.0.0-broken"))

    try:
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "check_version_sync.py")],
            capture_output=True,
            text=True,
            cwd=str(ROOT),
        )
        assert result.returncode == 1
        assert "mismatch" in result.stdout.lower()
    finally:
        # Restore
        init_path.write_text(original)
