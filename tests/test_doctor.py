"""Exit-contract tests with fake tools; no live services, shells or models."""
import os
from pathlib import Path
import subprocess
import sys

DOCTOR = Path(__file__).resolve().parents[1] / "bin/doctor"


def executable(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n" + body + "\n")
    path.chmod(0o755)


def fixture(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    root = tmp_path / "code"
    helpers = root / "pi-shared/bin"
    executable(helpers / "pi-shared-check-deps", "exit 0")
    executable(helpers / "pi-profile-check", 'echo "probe $*"; exit "${PROBE_RC:-0}"')
    bin_dir = tmp_path / "bin"
    executable(bin_dir / "pi", 'echo test-version; exit "${PI_RC:-0}"')
    env = {"HOME": str(home), "PI_SETUP_CODE_ROOT": str(root),
           "PATH": str(bin_dir) + ":/usr/bin:/bin"}
    return env, helpers


def run(env, *args):
    return subprocess.run([sys.executable, str(DOCTOR), *args], env=env, capture_output=True,
                          text=True, timeout=20)


def test_stock_path_without_timeout_uses_explicit_probe(tmp_path):
    env, _ = fixture(tmp_path)
    result = run(env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "probe --agent-dir" in result.stdout
    assert "--list-models" not in result.stdout


def test_profile_failure_propagates_even_without_matching_error_text(tmp_path):
    env, _ = fixture(tmp_path)
    result = run({**env, "PROBE_RC": "42"})
    assert result.returncode == 1
    assert "Check exited 42" in result.stdout


def test_broken_pi_version_is_failure(tmp_path):
    env, _ = fixture(tmp_path)
    result = run({**env, "PI_RC": "7"})
    assert result.returncode == 1
    assert "Check exited 7" in result.stdout


def test_missing_helper_is_failure(tmp_path):
    env, helpers = fixture(tmp_path)
    (helpers / "pi-profile-check").unlink()
    result = run(env)
    assert result.returncode == 1
    assert "Missing pi-shared health helpers" in result.stdout


def test_missing_optional_browser_is_warning_not_verified_readiness(tmp_path):
    env, helpers = fixture(tmp_path)
    (helpers / "pi-browser-check").write_text('print("WARN: browser-worker unavailable; configure its token and endpoint")\nraise SystemExit(2)\n')
    result = run(env)
    assert result.returncode == 0
    assert "browser-worker unavailable" in result.stdout
    assert "READY" not in result.stdout


def test_broken_browser_checker_is_not_optional_success(tmp_path):
    env, helpers = fixture(tmp_path)
    (helpers / "pi-browser-check").write_text('raise SystemExit(9)\n')
    result = run(env)
    assert result.returncode == 1
    assert "Check exited 9" in result.stdout
