"""Package bootstrap contract: managed bare updates, otherwise stock Pi before
setup and trusted shared launcher afterwards. No provider calls or network.

The bootstrap is exercised as a subprocess with an isolated HOME, a fake stock
`PI_UPSTREAM_BIN`, its own fake updater, and (optionally) a fake shared checkout
providing `bin/pi-launch`. All exec targets print their argv/environment so
the test can assert the routing decision and the environment the bootstrap
hands to the delegate.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import shutil
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
    bootstrap = tmp_path / "pkg" / "bin" / "pi"
    bootstrap.parent.mkdir()
    shutil.copy2(BOOTSTRAP, bootstrap)
    updater = bootstrap.with_name("pi-shared")
    _exe(updater, 'printf "MANAGED argv=[%s]\\n" "$*"')
    base = {"HOME": str(home), "PATH": "/usr/bin:/bin",
            "PI_UPSTREAM_BIN": str(upstream), "PYTHONDONTWRITEBYTECODE": "1"}
    return {"home": home, "tmp": tmp_path, "upstream": upstream,
            "bootstrap": bootstrap, "updater": updater, "env": base}


def run(env, *args, **overrides):
    merged = {**env["env"], **{k: str(v) for k, v in overrides.items()}}
    return subprocess.run([sys.executable, str(env["bootstrap"]), *args],
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


# --- Managed bare update; explicit stock options retain their meaning -------

@pytest.mark.parametrize("receipt", ["absent", "configured", "incomplete", "malformed"])
def test_bare_update_uses_own_updater_before_launcher_or_receipt_processing(env, receipt):
    if receipt != "absent":
        code_root = shared_checkout(env)
        path = write_receipt(env, {"version": 2,
            "status": "incomplete" if receipt == "incomplete" else "module-checks-passed",
            "modules": ["pi-shared"], "code_root": str(code_root)})
        if receipt == "malformed":
            path.write_text("not json")  # The trusted updater owns receipt validation.
    result = run(env, "update")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "MANAGED argv=[update]"
    assert "Pi runtime and saved modules" in result.stderr


@pytest.mark.parametrize("upstream", ["", "relative/missing", "/nonexistent/pi-upstream"])
def test_bare_update_can_repair_unavailable_runtime(env, upstream):
    result = run(env, "update", PI_UPSTREAM_BIN=upstream)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "MANAGED argv=[update]"


def test_bare_update_preserves_exit_code_and_environment_without_path_lookup(env):
    _exe(env["updater"], 'printf "PROFILE=[%s] CONFIG=[%s]\\n" "$PI_CODING_AGENT_DIR" "$PI_LAUNCHER_CONFIG"; exit 23')
    decoy = env["tmp"] / "decoy-bin"
    _exe(decoy / "pi-shared", 'echo WRONG_UPDATER; exit 42')
    result = run(env, "update", PATH=f"{decoy}:/usr/bin:/bin",
                 PI_CODING_AGENT_DIR="/explicit/profile", PI_LAUNCHER_CONFIG="/explicit/config.json")
    assert result.returncode == 23
    assert result.stdout.strip() == "PROFILE=[/explicit/profile] CONFIG=[/explicit/config.json]"


@pytest.mark.parametrize("mutation", ["missing", "not-executable", "directory", "self-link"])
def test_bare_update_fails_closed_when_own_updater_is_unavailable(env, mutation):
    updater = env["updater"]
    if mutation == "not-executable":
        updater.chmod(0o644)
    else:
        updater.unlink()
        if mutation == "directory":
            updater.mkdir()
        elif mutation == "self-link":
            updater.symlink_to(env["bootstrap"])
    result = run(env, "update")
    assert result.returncode == 1
    assert "packaged pi-shared updater is unavailable" in result.stderr
    assert not result.stdout


def test_bare_update_resolves_bootstrap_symlink_to_its_own_updater(env):
    alias = env["tmp"] / "linked-bin" / "pi"
    alias.parent.mkdir()
    alias.symlink_to(env["bootstrap"])
    _exe(alias.with_name("pi-shared"), 'echo WRONG_UPDATER')
    env["bootstrap"] = alias
    result = run(env, "update")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "MANAGED argv=[update]"


@pytest.mark.parametrize("configured", [False, True])
@pytest.mark.parametrize("args", [
    ("update", "--extensions"), ("update", "--models"),
    ("update", "--extension", "npm:example"), ("update", "npm:example"),
    ("update", "--help"), ("update", "--self"), ("update", "--all"),
    ("update", "self"), ("update", "pi"), ("update", "--force"),
    ("update", "--plan"), ("--", "update"), ("--system-prompt", "update"),
])
def test_non_bare_update_argv_is_not_reinterpreted(env, args, configured):
    if configured:
        code_root = shared_checkout(env)
        write_receipt(env, {"version": 2, "status": "module-checks-passed",
            "modules": ["pi-shared"], "code_root": str(code_root)})
    result = run(env, *args)
    assert result.returncode == 0, result.stderr
    expected = f"{'LAUNCH' if configured else 'UPSTREAM'} argv=[{' '.join(args)}]"
    assert result.stdout.strip() == expected + (" config=[]" if configured else "")
    assert not result.stderr


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
    r = run(env, "--help", PI_UPSTREAM_BIN=env["bootstrap"].resolve())
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
    assert result.returncode == 1 and "launcher support is not installed" in result.stderr
    assert not result.stdout


def enable_models_command(code_root, content=None):
    path = code_root / "pi-shared/lib/pi-launcher-capabilities.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"modelsCommand": 1}) if content is None else content)
    return path


@pytest.mark.parametrize("args", [(), ("--json",), ("--local", "--verbose"), ("--cloud",),
                                 ("--direct", "--json"), ("--help",), ("-h",), ("unexpected",)])
def test_capable_launcher_owns_models_command_and_flags(env, args):
    code_root = shared_checkout(env)
    enable_models_command(code_root)
    write_receipt(env, {"version": 2, "status": "module-checks-passed",
                        "modules": ["pi-shared"], "code_root": str(code_root)})
    result = run(env, "models", *args)
    assert result.returncode == 0, result.stderr
    assert f"LAUNCH argv=[{' '.join(('models', *args))}]" in result.stdout
    assert "UPSTREAM" not in result.stdout


def test_capable_launcher_keeps_legacy_tsv_command(env):
    code_root = shared_checkout(env)
    enable_models_command(code_root)
    write_receipt(env, {"version": 2, "status": "module-checks-passed",
                        "modules": ["pi-shared"], "code_root": str(code_root)})
    result = run(env, "--launcher-list")
    assert result.returncode == 0 and "LAUNCH argv=[--launcher-list]" in result.stdout


@pytest.mark.parametrize("content", [None, "{}", '{"modelsCommand":true}', '{"modelsCommand":2}'])
def test_old_or_unknown_capability_rejects_new_flags_without_exec(env, content):
    code_root = shared_checkout(env)
    if content is not None:
        enable_models_command(code_root, content)
    write_receipt(env, {"version": 2, "status": "module-checks-passed",
                        "modules": ["pi-shared"], "code_root": str(code_root)})
    result = run(env, "models", "--json")
    assert result.returncode == 1 and "pi-shared update" in result.stderr
    assert not result.stdout
    result = run(env, "models")
    assert result.returncode == 0 and "LAUNCH argv=[--launcher-list]" in result.stdout


@pytest.mark.parametrize("mutation", ["malformed", "shape", "symlink", "writable", "parent-symlink"])
def test_unsafe_or_invalid_capabilities_do_not_execute(env, mutation):
    code_root = shared_checkout(env)
    path = enable_models_command(code_root)
    if mutation == "malformed":
        path.write_text("not json")
    elif mutation == "shape":
        path.write_text("[]")
    elif mutation == "symlink":
        target = path.with_name("target.json")
        path.rename(target)
        path.symlink_to(target)
    elif mutation == "parent-symlink":
        target = path.parent.with_name("elsewhere")
        path.parent.rename(target)
        path.parent.symlink_to(target, target_is_directory=True)
    else:
        path.chmod(0o666)
    write_receipt(env, {"version": 2, "status": "module-checks-passed",
                        "modules": ["pi-shared"], "code_root": str(code_root)})
    result = run(env, "models", "--json")
    assert result.returncode == 1 and not result.stdout


def test_unconfigured_capability_file_is_not_delegation_authority(env):
    candidate = env["home"] / ".local/share/pi-shared/modules"
    shared_checkout(env, code_root=candidate)
    enable_models_command(candidate)
    result = run(env, "models", "--json")
    assert result.returncode == 1 and "launcher support is not installed" in result.stderr
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


@pytest.mark.skipif(not os.environ.get("PI_SHARED_TEST_ROOT"), reason="opt-in sibling shared integration")
@pytest.mark.parametrize("mode", ["direct", "cloud"])
def test_real_shared_grouped_models_through_package_bootstrap(env, mode):
    source = Path(os.environ["PI_SHARED_TEST_ROOT"]).resolve()
    code_root = shared_checkout(env)
    shared = code_root / "pi-shared"
    (shared / "lib").mkdir()
    for name in ("bin/pi-launch", "lib/pi_cli.py", "lib/pi_catalog.py", "lib/pi-launcher-capabilities.json"):
        shutil.copy2(source / name, shared / name)
    env["env"]["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env["env"]["PATH"]
    config = env["home"] / ".pi/launcher.json"
    write_receipt(env, {"version": 2, "status": "module-checks-passed", "mode": mode,
                        "modules": ["pi-shared"], "code_root": str(code_root),
                        "cli_file": str(config), "agent_dir": str(env["home"] / "native")})
    if mode == "direct":
        assert run(env, "--launcher-init-direct").returncode == 0
    else:
        aliases = env["home"] / "aliases.json"
        aliases.write_text(json.dumps({
            "local-model": {"alias": "local", "name": "Local Model"},
            "cloud:remote": {"alias": "cloud", "name": "Cloud Model", "provider_model_id": "cloud-id",
                             "pi": {"aliases": ["synonym"]}},
        }))
        result = subprocess.run([sys.executable, str(shared / "lib/pi_catalog.py"), "--offline",
                                 "--aliases", str(aliases), "--cli-out", str(config),
                                 "--models-out", str(env["home"] / "gateway/models.json"),
                                 "--pi-agent-dir", str(env["home"] / "gateway"), "--direct-launchers"],
                                env=env["env"], capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr
    result = run(env, "models", "--json")
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    expected_groups = {"direct"} if mode == "direct" else {"local", "cloud", "direct"}
    assert {row["group"] for row in data["models"]} == expected_groups
    assert "DIRECT · subscription" in run(env, "models").stdout
    for group in ("local", "cloud", "direct"):
        filtered = run(env, "models", "--" + group, "--json")
        assert filtered.returncode == 0, filtered.stderr
        assert json.loads(filtered.stdout)["models"] == [r for r in data["models"] if r["group"] == group]
    assert "Route: openai-codex/" in run(env, "models", "--verbose").stdout
    assert "openai\topenai-codex/" in run(env, "--launcher-list").stdout
    assert run(env, "openai", "--default").returncode == 0
    defaults = json.loads(run(env, "models", "--json").stdout)
    assert [r["alias"] for r in defaults["models"] if r["default"]] == ["openai"]
    assert run(env, "models", "--invalid").returncode == 1


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
