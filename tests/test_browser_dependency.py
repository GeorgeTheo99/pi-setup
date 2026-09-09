"""Isolated client wiring tests; no daemon, browser or credentials used."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

HELPER = Path(__file__).resolve().parents[1] / "lib/configure_browser_worker.py"


@pytest.fixture
def fixture(tmp_path):
    worker = tmp_path / "worker with spaces"
    (worker / "scripts").mkdir(parents=True)
    data = tmp_path / "private data"
    (data / "tokens").mkdir(parents=True)
    token = data / "tokens/pi-production"
    token.write_text("TEST_ONLY_PRODUCTION_TOKEN_NOT_TO_BE_COPIED")
    token.chmod(0o600)
    control = worker / "scripts/browser-worker"
    control.write_text('''#!/bin/sh
if [ "$1" = verify ]; then exit "${VERIFY_STATUS:-0}"; fi
if [ "$1" = env ]; then
  printf 'PORT=19890\\nDATA_DIR=%s\\n' "$TEST_DATA"
  [ -z "${TEST_PRODUCTION_TOKEN_FILE:-}" ] || printf 'PRODUCTION_TOKEN_FILE=%s\\n' "$TEST_PRODUCTION_TOKEN_FILE"
  exit 0
fi
exit 9
''')
    control.chmod(0o755)
    config = tmp_path / "config.json"
    original = {"websearchMcpUrl": "http://127.0.0.1:8891/mcp", "browserWorkerEnabled": False, "other": [1, 2]}
    config.write_text(json.dumps(original))
    env = {**os.environ, "TEST_DATA": str(data)}
    return worker, data, config, original, env


def run(fixture, **extra):
    worker, _, config, _, env = fixture
    return subprocess.run([sys.executable, str(HELPER), "--worker-root", str(worker), "--config", str(config)],
                          env={**env, **extra}, capture_output=True, text=True, timeout=15)


def test_verified_worker_wires_paths_not_token_and_preserves_research(fixture):
    _, data, config, original, _ = fixture
    result = run(fixture)
    assert result.returncode == 0, result.stderr
    configured = json.loads(config.read_text())
    assert configured == {**original, "browserWorkerEnabled": True,
                          "browserWorkerMcpUrl": "http://127.0.0.1:19890/mcp",
                          "browserWorkerTokenFile": str(data / "tokens/pi-production")}
    assert "TEST_ONLY_PRODUCTION_TOKEN" not in config.read_text() + result.stdout + result.stderr
    before = config.stat().st_mtime_ns
    assert run(fixture).returncode == 0
    assert config.stat().st_mtime_ns == before


def test_existing_custom_production_token_path_is_wired(fixture):
    _, data, config, _, _ = fixture
    custom = data / "tokens/custom-pi"
    (data / "tokens/pi-production").rename(custom)
    result = run(fixture, TEST_PRODUCTION_TOKEN_FILE=str(custom))
    assert result.returncode == 0, result.stderr
    assert json.loads(config.read_text())["browserWorkerTokenFile"] == str(custom)


def test_failed_verify_does_not_select_broken_worker(fixture):
    config = fixture[2]
    before = config.read_bytes()
    result = run(fixture, VERIFY_STATUS="7")
    assert result.returncode == 1
    assert "verify exited 7" in result.stderr
    assert config.read_bytes() == before


@pytest.mark.parametrize("kind", ["missing", "world-readable", "symlink"])
def test_production_token_must_exist_and_be_private(fixture, kind):
    _, data, config, _, _ = fixture
    before = config.read_bytes()
    token = data / "tokens/pi-production"
    if kind == "missing":
        token.unlink()
    elif kind == "world-readable":
        token.chmod(0o644)
    else:
        real = token.with_name("real")
        token.rename(real)
        token.symlink_to(real)
    result = run(fixture)
    assert result.returncode == 1
    assert config.read_bytes() == before


def test_conflicting_custom_worker_is_not_overwritten(fixture):
    config = fixture[2]
    config.write_text(json.dumps({"browserWorkerMcpUrl": "http://127.0.0.1:29990/mcp"}))
    before = config.read_bytes()
    result = run(fixture)
    assert result.returncode == 1
    assert "selects a different worker" in result.stderr
    assert config.read_bytes() == before


@pytest.mark.parametrize("value", ["[]", "null", "{malformed"])
def test_bad_research_config_is_not_overwritten(fixture, value):
    config = fixture[2]
    config.write_text(value)
    assert run(fixture).returncode == 1
    assert config.read_text() == value


def test_symlinked_research_config_is_not_replaced(fixture):
    config = fixture[2]
    target = config.with_name("real.json")
    config.rename(target)
    config.symlink_to(target)
    before = target.read_bytes()
    result = run(fixture)
    assert result.returncode == 1
    assert "symlink" in result.stderr
    assert config.is_symlink() and target.read_bytes() == before
