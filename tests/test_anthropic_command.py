"""Packaged pi-anthropic command must route only to an enabled native shortcut."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def machine(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    pkg = tmp_path / "pkg" / "bin"
    pkg.mkdir(parents=True)
    for name in ("pi", "pi-anthropic"):
        shutil.copy2(ROOT / "bin" / name, pkg / name)
    upstream = tmp_path / "upstream"
    upstream.write_text('#!/bin/sh\necho STOCK "$*"\n')
    upstream.chmod(0o755)
    env = {**os.environ, "HOME": str(home), "PI_UPSTREAM_BIN": str(upstream),
           "PYTHONDONTWRITEBYTECODE": "1"}
    return home, pkg, env


def launch(machine, *argv):
    _, pkg, env = machine
    return subprocess.run([sys.executable, str(pkg / "pi-anthropic"), *argv],
                          env=env, capture_output=True, text=True, timeout=15)


def test_unconfigured_command_fails_closed_instead_of_sending_prompt(machine):
    result = launch(machine, "hello")
    assert result.returncode == 1
    assert "direct launcher is unavailable" in result.stderr
    assert "STOCK" not in result.stdout


@pytest.mark.parametrize("enabled", [True, False])
def test_packaged_command_checks_native_route_and_forwards_arguments(machine, enabled):
    home, _, _ = machine
    shared = home / "code/pi-shared/bin"
    shared.mkdir(parents=True)
    launcher = shared / "pi-launch"
    routes = ([{"alias": "anthropic", "provider": "anthropic", "gateway": False}]
              if enabled else [{"alias": "openai", "provider": "openai-codex", "gateway": False}])
    launcher.write_text(f"#!{sys.executable}\nimport json, sys\n"
                        f"if sys.argv[1:4] == ['models', '--direct', '--json']:\n"
                        f"    print({json.dumps(json.dumps({'models': routes}))})\n"
                        "else:\n    print(json.dumps(sys.argv[1:]))\n")
    launcher.chmod(0o755)
    capabilities = shared.parent / "lib/pi-launcher-capabilities.json"
    capabilities.parent.mkdir()
    capabilities.write_text('{"modelsCommand":1}')
    receipt = home / ".config/pi-shared/setup.json"
    receipt.parent.mkdir(parents=True)
    receipt.write_text(json.dumps({"version": 2, "status": "module-checks-passed",
                                   "modules": ["pi-shared"], "code_root": str(home / "code")}))
    receipt.chmod(0o600)
    probe = subprocess.run([sys.executable, str(machine[1] / "pi"), "models", "--direct", "--json"],
                           env=machine[2], capture_output=True, text=True, timeout=15)
    assert probe.returncode == 0, probe.stderr
    result = launch(machine, "--thinking", "high")
    if enabled:
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout) == ["anthropic", "--thinking", "high"]
    else:
        assert result.returncode == 1
        assert "shortcut is disabled" in result.stderr
        assert not result.stdout
