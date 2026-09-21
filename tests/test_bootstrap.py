"""Package bootstrap (bin/pi) contract: stock Pi before setup, trusted shared
launcher afterwards. No provider calls, no shell startup, no network.

The bootstrap is exercised as a subprocess with an isolated HOME, a fake stock
`PI_UPSTREAM_BIN`, and (optionally) a fake shared checkout providing
`bin/pi-launch`. Both exec targets are stubs that print their argv/environment so
the test can assert the routing decision and the environment the bootstrap
hands to the delegate.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import subprocess
import sys

import pytest

BOOTSTRAP = Path(__file__).resolve().parents[1] / "bin/pi"


def _exe(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n" + body + "\n")
    path.chmod(0o755)


@pytest.fixture
def env(tmp_path):
    """Isolated HOME, a fake stock runtime, and helpers to build a shared checkout."""
    home = tmp_path / "home"
    home.mkdir()
    upstream = tmp_path / "pkg" / "pi-upstream-stock"
    _exe(upstream, 'printf "UPSTREAM argv=[%s]\\n" "$*"')
    base = {"HOME": str(home), "PATH": "/usr/bin:/bin",
            "PI_UPSTREAM_BIN": str(upstream), "PYTHONDONTWRITEBYTECODE": "1"}
    return {"home": home, "tmp": tmp_path, "upstream": upstream, "env": base}


def run(env, *args, **overrides):
    merged = {**env["env"], **{k: str(v) for k, v in overrides.items()}}
    return subprocess.run([sys.executable, str(BOOTSTRAP), *args],
                          env=merged, capture_output=True, text=True, timeout=15)


def shared_checkout(env, *, code_root=None):
    """A trusted shared checkout at <code_root>/pi-shared with an executable pi-launch."""
    code_root = code_root or (env["tmp"] / "code")
    launch = code_root / "pi-shared" / "bin" / "pi-launch"
    _exe(launch, 'printf "LAUNCH argv=[%s] config=[%s]\\n" "$*" "$PI_LAUNCHER_CONFIG"')
    return code_root


def write_receipt(env, data, *, mode=0o600):
    path = env["home"] / ".config" / "pi-shared" / "setup.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))
    path.chmod(mode)
    return path


# --- Before setup: no receipt, no configured package ------------------------

def test_ordinary_argv_execs_stock_before_setup(env):
    r = run(env, "--help", "some-arg")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "UPSTREAM argv=[--help some-arg]"


def test_stock_pi_list_reaches_upstream_unchanged(env):
    # `pi list` is the stock package command and must never be intercepted.
    r = run(env, "list")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "UPSTREAM argv=[list]"


@pytest.mark.parametrize("command", ["models", "--launcher-list"])
def test_launcher_flags_before_setup_are_a_clear_actionable_error(env, command):
    r = run(env, command)
    assert r.returncode == 1
    assert "launcher support is not installed" in r.stderr
    assert "pi-shared setup" in r.stderr
    assert "UPSTREAM" not in r.stdout


# --- Environment / PI_UPSTREAM_BIN contract ---------------------------------

def test_missing_upstream_env_fails_without_exec(env):
    r = run(env, "--help", PI_UPSTREAM_BIN="")
    assert r.returncode == 1
    assert "absolute" in r.stderr and "UPSTREAM" not in r.stdout


def test_relative_upstream_is_rejected(env):
    r = run(env, "--help", PI_UPSTREAM_BIN="relative/pi")
    assert r.returncode == 1
    assert "absolute" in r.stderr


def test_non_executable_upstream_is_rejected(env):
    target = env["tmp"] / "not-exec"
    target.write_text("plain")
    r = run(env, "--help", PI_UPSTREAM_BIN=target)
    assert r.returncode == 1
    assert "stock Pi runtime" in r.stderr


def test_upstream_pointing_at_bootstrap_itself_is_refused(env):
    r = run(env, "--help", PI_UPSTREAM_BIN=BOOTSTRAP.resolve())
    assert r.returncode == 1
    assert "stock Pi runtime" in r.stderr and "UPSTREAM" not in r.stdout


# --- After setup: receipt delegates to the trusted shared launcher ----------

def test_receipt_delegates_to_shared_launcher_with_ordinary_argv(env):
    code_root = shared_checkout(env)
    cli_file = env["home"] / ".pi/launcher.json"
    write_receipt(env, {"version": 2, "status": "module-checks-passed",
                        "modules": ["pi-shared"], "code_root": str(code_root),
                        "cli_file": str(cli_file)})
    r = run(env, "sonnet", "--foo")
    assert r.returncode == 0, r.stderr
    # NO extra transport separator: ordinary Pi argv reaches pi-launch verbatim.
    assert r.stdout.strip() == f"LAUNCH argv=[sonnet --foo] config=[{cli_file}]"


@pytest.mark.parametrize("command", ["models", "--launcher-list"])
def test_launcher_flags_after_setup_reach_shared_launcher(env, command):
    code_root = shared_checkout(env)
    cli_file = env["home"] / ".pi/launcher.json"
    write_receipt(env, {"version": 2, "status": "module-checks-passed",
                        "modules": ["pi-shared"], "code_root": str(code_root),
                        "cli_file": str(cli_file)})
    r = run(env, command)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == f"LAUNCH argv=[--launcher-list] config=[{cli_file}]"


@pytest.mark.parametrize("flag", ["--help", "-h"])
def test_models_help_translates_for_older_shared_launchers(env, flag):
    code_root = shared_checkout(env)
    write_receipt(env, {"version": 2, "status": "module-checks-passed",
                        "modules": ["pi-shared"], "code_root": str(code_root)})
    result = run(env, "models", flag)
    assert result.returncode == 0, result.stderr
    assert "LAUNCH argv=[--launcher-help]" in result.stdout


def test_models_with_extra_args_never_reaches_stock(env):
    result = run(env, "models", "unexpected")
    assert result.returncode == 1 and "Usage: pi models" in result.stderr
    assert not result.stdout


@pytest.mark.parametrize("args", [("--", "models"), ("--system-prompt", "models")])
def test_models_literal_and_option_value_reach_stock(env, args):
    result = run(env, *args)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"UPSTREAM argv=[{' '.join(args)}]"


def test_receipt_without_cli_file_delegates_without_forcing_launcher_config(env):
    # Legacy receipts (no cli_file) still delegate; pi-launch uses its own default.
    code_root = shared_checkout(env)
    write_receipt(env, {"version": 1, "status": "module-checks-passed",
                        "modules": ["pi-shared"], "code_root": str(code_root)})
    r = run(env, "opus")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "LAUNCH argv=[opus] config=[]"


def test_incomplete_receipt_status_still_delegates(env):
    # A resumable in-progress install must not silently fall back to stock.
    code_root = shared_checkout(env)
    write_receipt(env, {"version": 2, "status": "incomplete",
                        "modules": ["pi-shared"], "code_root": str(code_root),
                        "cli_file": str(env["home"] / ".pi/launcher.json")})
    r = run(env, "haiku")
    assert r.returncode == 0, r.stderr
    assert r.stdout.startswith("LAUNCH argv=[haiku]")


@pytest.mark.parametrize("mode", ["direct", "cloud"])
@pytest.mark.parametrize("explicit", [False, True])
def test_direct_uses_saved_native_profile_without_overriding_explicit_profile(env, mode, explicit):
    code_root = shared_checkout(env)
    _exe(code_root / "pi-shared/bin/pi-launch", 'printf "%s" "$PI_CODING_AGENT_DIR"')
    profile = env["home"] / "custom-native-profile"
    write_receipt(env, {"version": 2, "status": "module-checks-passed", "mode": mode,
                       "modules": ["pi-shared"], "code_root": str(code_root),
                       "agent_dir": str(profile)})
    overrides = {"PI_CODING_AGENT_DIR": str(env["home"] / "explicit-profile")} if explicit else {}
    result = run(env, "openai", **overrides)
    assert result.returncode == 0, result.stderr
    assert result.stdout == overrides.get("PI_CODING_AGENT_DIR", str(profile) if mode == "direct" else "")


# --- Unsafe / invalid receipts ----------------------------------------------

def test_world_readable_receipt_is_refused(env):
    shared_checkout(env)
    write_receipt(env, {"version": 2, "status": "module-checks-passed",
                        "modules": ["pi-shared"], "code_root": str(env["tmp"] / "code")},
                  mode=0o644)
    r = run(env, "sonnet")
    assert r.returncode == 1
    assert "owner-only regular file" in r.stderr
    assert "LAUNCH" not in r.stdout and "UPSTREAM" not in r.stdout


def test_symlinked_receipt_is_refused(env):
    shared_checkout(env)
    real = env["home"] / "elsewhere.json"
    real.write_text(json.dumps({"version": 2, "status": "module-checks-passed",
                                "modules": ["pi-shared"], "code_root": str(env["tmp"] / "code")}))
    receipt = env["home"] / ".config" / "pi-shared" / "setup.json"
    receipt.parent.mkdir(parents=True)
    receipt.symlink_to(real)
    r = run(env, "sonnet")
    assert r.returncode == 1
    assert "owner-only regular file" in r.stderr


def test_invalid_receipt_shape_is_refused_not_ignored(env):
    shared_checkout(env)
    write_receipt(env, {"version": 2, "status": "bogus", "modules": ["pi-shared"],
                        "code_root": str(env["tmp"] / "code")})
    r = run(env, "sonnet")
    assert r.returncode == 1
    assert "invalid setup receipt" in r.stderr
    assert "LAUNCH" not in r.stdout and "UPSTREAM" not in r.stdout


def test_receipt_missing_pi_shared_module_is_refused(env):
    shared_checkout(env)
    write_receipt(env, {"version": 2, "status": "module-checks-passed",
                        "modules": ["model-gateway"], "code_root": str(env["tmp"] / "code")})
    r = run(env, "sonnet")
    assert r.returncode == 1
    assert "invalid setup receipt" in r.stderr


def test_relative_code_root_in_receipt_is_refused(env):
    write_receipt(env, {"version": 2, "status": "module-checks-passed",
                        "modules": ["pi-shared"], "code_root": "relative/code"})
    r = run(env, "sonnet")
    assert r.returncode == 1
    assert "absolute" in r.stderr


def test_group_writable_shared_checkout_is_refused(env):
    code_root = shared_checkout(env)
    (code_root / "pi-shared" / "bin").chmod(0o775)
    write_receipt(env, {"version": 2, "status": "module-checks-passed",
                        "modules": ["pi-shared"], "code_root": str(code_root),
                        "cli_file": str(env["home"] / ".pi/launcher.json")})
    r = run(env, "sonnet")
    assert r.returncode == 1
    assert "unsafe shared launcher" in r.stderr


def test_missing_launcher_in_configured_checkout_falls_back_to_stock(env):
    # Receipt configured, but the checkout has no executable pi-launch: the
    # bootstrap must not fail closed for ordinary argv — stock Pi still works.
    code_root = env["tmp"] / "code"
    (code_root / "pi-shared" / "bin").mkdir(parents=True)
    write_receipt(env, {"version": 2, "status": "module-checks-passed",
                        "modules": ["pi-shared"], "code_root": str(code_root)})
    r = run(env, "--version")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "UPSTREAM argv=[--version]"


# --- Settings.json delegation fallback (source installs, no receipt) --------

def test_conventional_path_alone_is_not_authority_to_delegate(env):
    # A checkout at the conventional modules path is ignored unless the profile
    # explicitly configures it as a Pi package.
    candidate = env["home"] / ".local/share/pi-shared/modules"
    shared_checkout(env, code_root=candidate)
    r = run(env, "--help")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "UPSTREAM argv=[--help]"


def test_configured_package_delegates_without_a_receipt(env):
    candidate = env["home"] / ".local/share/pi-shared/modules"
    shared_checkout(env, code_root=candidate)
    settings = env["home"] / ".pi/agent/settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"packages": [{"source": str(candidate / "pi-shared")}]}))
    r = run(env, "sonnet")
    assert r.returncode == 0, r.stderr
    # No receipt cli_file: pi-launch uses its own default config.
    assert r.stdout.strip() == "LAUNCH argv=[sonnet] config=[]"


def test_unrelated_configured_package_does_not_delegate(env):
    candidate = env["home"] / ".local/share/pi-shared/modules"
    shared_checkout(env, code_root=candidate)
    settings = env["home"] / ".pi/agent/settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"packages": [{"source": "/some/unrelated/place"}]}))
    r = run(env, "--help")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "UPSTREAM argv=[--help]"
