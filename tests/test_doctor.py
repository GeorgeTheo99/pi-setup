"""Exit-contract tests with fake tools; no live services, shells or models."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

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
    settings = home / ".pi/agent/settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"packages": [str(helpers.parent)]}))
    bin_dir = tmp_path / "bin"
    executable(bin_dir / "pi", 'echo test-version; exit "${PI_RC:-0}"')
    executable(bin_dir / "uv", 'test "$*" = "python find --no-python-downloads 3.12" || exit 97; '
               'test "$UV_PYTHON_DOWNLOADS" = never || exit 97; '
               'test "$UV_OFFLINE" = true || exit 97; '
               'echo /fixture/python3.12; exit "${PYTHON_FIND_RC:-0}"')
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


def test_selected_worker_with_missing_control_script_is_not_optional_success(tmp_path):
    env, helpers = fixture(tmp_path)
    worker_root = Path(env["PI_SETUP_CODE_ROOT"]) / "browser-worker"
    worker_root.mkdir()
    (helpers / "pi-browser-check").write_text('print("WARN: unavailable")\nraise SystemExit(2)\n')
    result = run(env)
    assert result.returncode == 1
    assert "control script is missing/non-executable" in result.stdout


def test_broken_browser_checker_is_not_optional_success(tmp_path):
    env, helpers = fixture(tmp_path)
    (helpers / "pi-browser-check").write_text('raise SystemExit(9)\n')
    result = run(env)
    assert result.returncode == 1
    assert "Check exited 9" in result.stdout


def gateway_fixture(env):
    root, home = Path(env["PI_SETUP_CODE_ROOT"]), Path(env["HOME"])
    executable(root / "model-gateway/bin/model-gateway",
               'test "$1" = verify || exit 97; echo gateway-verify; exit "${GATEWAY_RC:-0}"')
    launchers = home / ".pi/generated/pi-launchers.zsh"
    launchers.parent.mkdir(parents=True)
    launchers.write_text("pi-list() { :; }\n")
    (home / ".zshrc").write_text('echo MUST_NOT_EXECUTE_RC >&2\nsource "$HOME/.pi/generated/pi-launchers.zsh"\n')
    profile = home / ".pi-omlx/agent/settings.json"
    profile.parent.mkdir(parents=True)
    profile.write_text("{}")
    executable(Path(env["PATH"].split(":")[0]) / "zsh",
               'test "$1" = -n || exit 97; echo launcher-syntax; exit "${ZSH_RC:-0}"')
    return launchers


def test_fresh_default_services_do_not_require_unselected_catalog(tmp_path):
    env, helpers = fixture(tmp_path)
    root = Path(env["PI_SETUP_CODE_ROOT"])
    executable(root / "model-gateway/bin/model-gateway", 'test "$1" = verify || exit 97; echo gateway-verify')
    executable(root / "browser-worker/scripts/browser-worker", 'test "$1" = verify || exit 97; echo WORKER_VERIFY')
    (helpers / "pi-browser-check").write_text('print("READY: fixture inventory")\n')
    result = run(env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "gateway-verify" in result.stdout and "WORKER_VERIFY" in result.stdout
    assert "Generated model catalog not selected" in result.stdout
    assert "--require-models" not in result.stdout
    result = run(env, "--require-catalog")
    assert result.returncode == 1 and "Missing launchers" in result.stdout


@pytest.mark.parametrize("minimal", [False, True])
def test_management_only_launcher_checks_profiles_without_requiring_models(tmp_path, minimal):
    env, helpers = fixture(tmp_path)
    launchers = gateway_fixture(env)
    launchers.write_text(
        "# Generated by pi-shared/bin/pi-catalog — do not hand-edit.\n"
        "# Pi catalog state: unconfigured (management-only).\npi-list() { :; }\n")
    env["PI_SHARED_BOOTSTRAP_LAUNCHERS"] = "1"
    executable(helpers / "pi-profile-check", 'echo "probe $*"; case "$*" in *--require-models*) exit 9 ;; esac; exit 0')
    args = ["--module", "pi-shared"] if minimal else []
    result = run(env, *args)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "launcher-syntax" in result.stdout
    assert "Gateway catalog unconfigured" in result.stdout
    assert "--require-models" not in result.stdout
    assert run(env, *args, "--require-catalog").returncode == 1


def test_required_management_launchers_cannot_be_silently_missing(tmp_path):
    env, _ = fixture(tmp_path)
    result = run({**env, "PI_SHARED_BOOTSTRAP_LAUNCHERS": "1"}, "--module", "pi-shared")
    assert result.returncode == 1
    assert "Missing launchers" in result.stdout


@pytest.mark.parametrize("prerequisite", ["missing-uv", "missing-python"])
def test_missing_browser_prerequisites_never_invoke_provisioning_operator(tmp_path, prerequisite):
    env, helpers = fixture(tmp_path)
    worker = Path(env["PI_SETUP_CODE_ROOT"]) / "browser-worker/scripts/browser-worker"
    executable(worker, 'echo MUST_NOT_INVOKE_OPERATOR; exit 0')
    (helpers / "pi-browser-check").write_text('print("READY: fixture inventory")\n')
    if prerequisite == "missing-uv":
        (Path(env["PATH"].split(":")[0]) / "uv").unlink()
    else:
        env["PYTHON_FIND_RC"] = "1"
    result = run(env, "--module", "browser-worker")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "MUST_NOT_INVOKE_OPERATOR" not in result.stdout


def test_gateway_checks_persisted_service_verifier_and_both_profiles_without_shell_startup(tmp_path):
    env, helpers = fixture(tmp_path)
    gateway_fixture(env)
    result = run(env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "gateway-verify" in result.stdout and "launcher-syntax" in result.stdout
    assert f"probe --agent-dir {Path(env['HOME']) / '.pi/agent'}" in result.stdout
    assert f"probe --agent-dir {Path(env['HOME']) / '.pi-omlx/agent'}" in result.stdout
    assert "--require-models" in result.stdout
    assert "MUST_NOT_EXECUTE_RC" not in result.stdout + result.stderr


@pytest.mark.parametrize("failure", ["missing-launchers", "bad-syntax", "service-failure", "generated-profile-failure"])
def test_gateway_failures_do_not_report_verified_readiness(tmp_path, failure):
    env, helpers = fixture(tmp_path)
    launchers = gateway_fixture(env)
    if failure == "missing-launchers":
        launchers.unlink()
    elif failure == "bad-syntax":
        env["ZSH_RC"] = "7"
    elif failure == "service-failure":
        env["GATEWAY_RC"] = "8"
    else:
        executable(helpers / "pi-profile-check", 'case "$*" in *--require-models*) exit 9 ;; esac; exit 0')
    result = run(env)
    assert result.returncode == 1, result.stdout + result.stderr
    if failure == "bad-syntax":
        assert "Launcher syntax and shell hook configured" not in result.stdout


def test_selected_modules_skip_broken_browser_gateway_and_search(tmp_path):
    env, helpers = fixture(tmp_path)
    gateway_fixture(env)
    env["GATEWAY_RC"] = "9"
    root = Path(env["PI_SETUP_CODE_ROOT"])
    executable(root / "local_web_search/scripts/local-search", "echo SHOULD_NOT_PROBE; exit 9")
    executable(root / "browser-worker/scripts/browser-worker", "echo SHOULD_NOT_PROBE; exit 9")
    (helpers / "pi-browser-check").write_text('print("SHOULD_NOT_PROBE"); raise SystemExit(9)\n')
    config = Path(env["HOME"]) / ".pi/research/config.json"
    config.parent.mkdir(parents=True)
    config.write_text("{malformed")
    result = run(env, "--module", "pi-shared")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "browser-worker not selected" in result.stdout
    assert "SHOULD_NOT_PROBE" not in result.stdout
    assert "gateway-verify" not in result.stdout and "--require-models" not in result.stdout


def test_overlay_discovery_does_not_execute_or_impose_profile_packages(tmp_path):
    env, _ = fixture(tmp_path)
    gateway_fixture(env)
    overlay = Path(env["PI_SETUP_CODE_ROOT"]) / "pi-databricks"
    executable(overlay / "bin/doctor", "echo OVERLAY_CHECK; exit 9")
    (overlay / "manifest.fragment.yaml").write_text("modules: {}\n")
    result = run(env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OVERLAY_CHECK" not in result.stdout and str(overlay) not in result.stdout
    result = run(env, "--overlay", str(overlay))
    assert result.returncode == 1
    assert "OVERLAY_CHECK" in result.stdout and "Overlay doctor failed" in result.stdout
    assert f"--expect-package {overlay.resolve()}" in result.stdout


def test_shared_profile_override_fallback_is_used_by_real_probe(tmp_path):
    env, helpers = fixture(tmp_path)
    profile = Path(env["HOME"]) / "custom-profile"
    profile.mkdir()
    (profile / "settings.json").write_text(json.dumps({"packages": [{"source": str(helpers.parent)}]}))
    result = run({**env, "PI_SHARED_AGENT_DIR": str(profile)}, "--module", "pi-shared")
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"probe --agent-dir {profile.resolve()}" in result.stdout
    assert f"probe --agent-dir {Path(env['HOME']) / '.pi/agent'}" not in result.stdout


@pytest.mark.parametrize("mode", ["ready", "failed-verify", "failed-client", "disabled", "missing", "invalid-selection"])
def test_explicit_browser_selection_and_deselection(tmp_path, mode):
    env, helpers = fixture(tmp_path)
    worker = Path(env["PI_SETUP_CODE_ROOT"]) / "browser-worker"
    if mode != "missing":
        executable(worker / "scripts/browser-worker",
                   'test "$1" = verify || exit 97; echo WORKER_VERIFY; exit "${WORKER_RC:-0}"')
    (helpers / "pi-browser-check").write_text(
        'import os\nprint(os.environ.get("CLIENT_MESSAGE", "READY"))\n'
        'raise SystemExit(int(os.environ.get("CLIENT_RC", "0")))\n')
    if mode == "failed-verify":
        env["WORKER_RC"] = "9"
    elif mode == "failed-client":
        env["CLIENT_RC"] = "2"
    elif mode in ("disabled", "invalid-selection"):
        config = Path(env["HOME"]) / ".pi/research/config.json"
        config.parent.mkdir(parents=True)
        config.write_text(json.dumps({"browserWorkerEnabled": False if mode == "disabled" else "false"}))
        env["CLIENT_MESSAGE"] = "DISABLED"
        env["WORKER_RC"] = "9"  # Deselection must not verify the daemon.
    result = run(env, "--module", "browser-worker")
    assert result.returncode == (0 if mode in ("ready", "disabled") else 1), result.stdout + result.stderr
    if mode == "disabled":
        assert "DISABLED" in result.stdout and "WORKER_VERIFY" not in result.stdout


def test_empty_successful_pi_version_is_still_a_failure(tmp_path):
    env, _ = fixture(tmp_path)
    executable(Path(env["PATH"].split(":")[0]) / "pi", "exit 0")
    result = run(env)
    assert result.returncode == 1 and "--version failed" in result.stdout


@pytest.mark.parametrize("reply,exit_code,expected", [("PI_OK", 0, 0), ("not PI_OK", 0, 1), ("PI_OK", 9, 1)])
def test_smoke_opt_in_requires_exact_response_and_success_using_fake_launcher(tmp_path, reply, exit_code, expected):
    env, _ = fixture(tmp_path)
    launchers = Path(env["HOME"]) / ".pi/generated/pi-launchers.zsh"
    launchers.parent.mkdir(parents=True)
    launchers.write_text("# fixture only\n")
    executable(Path(env["PATH"].split(":")[0]) / "zsh",
               f'test "$1" = -f || exit 97; echo "{reply}"; exit {exit_code}')
    result = run(env, "--module", "pi-shared", "--smoke-model", "fixture")
    assert result.returncode == expected, result.stdout + result.stderr


@pytest.mark.parametrize("flag", ["--module", "--overlay"])
def test_empty_selection_values_fail_before_checks_or_overlay_execution(tmp_path, flag):
    env, _ = fixture(tmp_path)
    result = run(env, flag, "")
    assert result.returncode == 2
    assert "require nonempty values" in result.stderr
    assert result.stdout == ""


def test_invalid_browser_config_is_not_reported_as_explicitly_disabled(tmp_path):
    env, _ = fixture(tmp_path)
    config = Path(env["HOME"]) / ".pi/research/config.json"
    config.parent.mkdir(parents=True)
    config.write_text("{malformed")
    result = run(env)
    assert result.returncode == 1
    assert "Invalid browser selection configuration" in result.stdout
    assert "DISABLED" not in result.stdout
