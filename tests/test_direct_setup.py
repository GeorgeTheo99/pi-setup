"""Native-provider setup policy; hermetic fixtures never contact a provider."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from test_cli import ROOT, cli, isolated, options
from test_install_contract import setup as installer_setup, _git, _install, _exe
from test_update import args as update_args, VERSIONS


def write_direct_config(path, shared, enabled=True):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"generation": {"args": ["--direct-only", "--shared-dir", str(shared),
                              "--direct-launchers" if enabled else "--no-direct-launchers"]}}))
    path.chmod(0o600)


@pytest.fixture
def direct(isolated, monkeypatch):
    home, calls = isolated
    original_run = cli.run

    def run(command, **kw):
        original_run(command, **kw)
        if Path(command[0]).name == "install.sh":
            env = kw["env"]
            write_direct_config(env["PI_SHARED_CLI_OUT"], Path(env["PI_SETUP_CODE_ROOT"]) / "pi-shared",
                                env.get("PI_SHARED_DIRECT_LAUNCHERS", "1") != "0")

    monkeypatch.setattr(cli, "run", run)
    monkeypatch.setattr(cli.update_support, "runtime_versions", lambda: VERSIONS)
    monkeypatch.setattr(cli.update_support, "revisions", lambda root, modules: {m: "a" * 40 for m in modules})
    for name in ("discover_models", "hardware", "guard_fresh_omlx"):
        monkeypatch.setattr(cli, name, lambda *a, **kw: pytest.fail("unexpected oMLX operation"))
    return home, calls


@pytest.mark.parametrize("browser,search", [(False, False), (True, False), (True, True)])
def test_direct_selection_receipt_and_status(direct, browser, search, capsys):
    home, calls = direct
    cli.setup(options(mode="direct", without_browser=not browser, with_search=search))
    data = cli.read_state()
    modules = ["pi-shared"] + (["browser-worker"] if browser else []) + (["local_web_search"] if search else [])
    assert data["version"] == 2 and data["mode"] == "direct"
    assert data["modules"] == modules
    assert data["settings"]["PI_SHARED_DIRECT_ONLY"] == "1"
    assert data["bootstrap_launchers"] is True
    assert data["omlx"] is None and "external_gateway" not in data
    assert data["services"] == {}
    assert calls[0][0] == [str(ROOT / "install.sh"), "--minimal", *[f"--with-{m}" for m in modules[1:]]]
    assert calls[0][1]["env"]["PI_SHARED_DIRECT_ONLY"] == "1"
    assert not (home / ".pi-omlx").exists()
    calls.clear()
    cli.status()
    assert calls[0][0] == [str(ROOT / "bin/doctor"), *[a for m in modules for a in ("--module", m)]]
    assert calls[0][1]["env"]["PI_SHARED_DIRECT_ONLY"] == "1"
    output = capsys.readouterr().out
    assert "native provider authentication and inference NOT tested" in output
    assert "/login" in output and "/model" in output


def test_direct_custom_paths_optout_rerun_and_update_persist(direct, monkeypatch):
    home, calls = direct
    settings = {"PI_SETUP_CODE_ROOT": str(home / "custom/modules"),
                "PI_SHARED_AGENT_DIR": str(home / "custom/agent"),
                "PI_SHARED_CLI_OUT": str(home / "custom/cli.json"),
                "PI_SHARED_DIRECT_LAUNCHERS": "0"}
    for key, value in settings.items():
        monkeypatch.setenv(key, value)
    cli.setup(options(mode="direct"))
    before = cli.read_state()
    for key in settings:
        monkeypatch.delenv(key)
    for mode in ("direct", None):
        cli.setup(options(mode=mode))
        after = cli.read_state()
        assert all(after[k] == before[k] for k in ("code_root", "agent_dir", "cli_file", "settings"))
    calls.clear()
    for key in settings:
        monkeypatch.setenv(key, "1" if key == "PI_SHARED_DIRECT_LAUNCHERS" else "/wrong")
    monkeypatch.setenv("PI_SHARED_DIRECT_ONLY", "0")
    cli.update(update_args())
    assert calls[0][0][-3:] == ["--update-changed", "--only", "pi-shared"]
    env = calls[0][1]["env"]
    assert all(env[k] == v for k, v in settings.items())
    assert env["PI_SHARED_DIRECT_ONLY"] == "1"
    after = cli.read_state()
    assert all(after[k] == before[k] for k in ("code_root", "agent_dir", "cli_file", "settings", "mode", "modules"))
    calls.clear()
    cli.status()
    assert calls[0][1]["env"]["PI_SHARED_CLI_OUT"] == settings["PI_SHARED_CLI_OUT"]
    assert json.loads(Path(before["cli_file"]).read_text())["generation"]["args"][-1] == "--no-direct-launchers"


@pytest.mark.parametrize("env", [{"PI_SHARED_BOOTSTRAP_LAUNCHERS": "0"}, {"PI_SHARED_CLI_OUT": ""}])
def test_direct_requires_cli_bootstrap_before_writes(isolated, monkeypatch, env):
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    with pytest.raises(RuntimeError, match="requires nonempty"):
        cli.setup(options(mode="direct"))
    assert not isolated[1] and list(isolated[0].iterdir()) == []


@pytest.mark.parametrize("extra", [dict(omlx="guide"), dict(omlx_url="http://127.0.0.1:8000/v1"),
                                    dict(gateway_url="https://gateway.example"), dict(local=True)])
def test_direct_rejects_gateway_options_before_writes(isolated, extra):
    with pytest.raises(RuntimeError):
        cli.setup(options(mode="direct", **extra))
    assert not isolated[1] and list(isolated[0].iterdir()) == []


@pytest.mark.parametrize("mode", ["cloud", "local", "both", "existing-gateway", "omlx-only"])
@pytest.mark.parametrize("incomplete", [False, True])
def test_gateway_and_omlx_migration_refused_before_mutation(isolated, mode, incomplete):
    home, calls = isolated
    if mode == "existing-gateway":
        key = home / "key"; key.write_text("synthetic-key"); key.chmod(0o600)
        cli.setup(options(mode=mode, gateway_url="https://gateway.example", gateway_key_file=str(key)))
    else:
        cli.setup(options(mode="later" if mode == "omlx-only" else mode))
    data = cli.read_state()
    if mode == "omlx-only":
        data["omlx"] = "existing"
        data["omlx_url"] = "http://127.0.0.1:8000/v1"
    if incomplete:
        data["status"] = "incomplete"
    cli.write_state(data)
    calls.clear()
    before = cli.state_path().read_bytes()
    lock = cli.state_path().parent / "update.lock"
    lock.unlink()
    for plan in (True, False):
        with pytest.raises(RuntimeError, match="previously selected"):
            cli.setup(options(mode="direct", plan=plan))
        assert not calls and cli.state_path().read_bytes() == before
        assert not lock.exists()


def test_older_shared_cannot_ignore_direct_policy(isolated):
    with pytest.raises(RuntimeError, match="Direct-only launcher verification failed"):
        cli.setup(options(mode="direct"))  # inert old installer never creates direct metadata
    assert cli.read_state(allow_incomplete=True)["status"] == "incomplete"
    with pytest.raises(RuntimeError, match="did not complete"):
        cli.status()


@pytest.mark.parametrize("change", [lambda d: d["settings"].pop("PI_SHARED_DIRECT_ONLY"),
    lambda d: d["settings"].update(PI_SHARED_DIRECT_ONLY="0"), lambda d: d.update(mode="later"),
    lambda d: d.update(bootstrap_launchers=False), lambda d: d.update(launcher_refresh=False),
    lambda d: d["modules"].append("model-gateway"), lambda d: d.update(omlx="guide"),
    lambda d: d.update(omlx_url="http://127.0.0.1:8000/v1"), lambda d: d.update(external_gateway=None),
    lambda d: d["settings"].update(PI_SHARED_CLI_OUT="/different"),
    lambda d: d["settings"].update(PI_SHARED_AGENT_DIR="/different"), lambda d: d.pop("cli_file")])
def test_inconsistent_receipts_fail_before_commands(direct, change):
    cli.setup(options(mode="direct"))
    data = cli.read_state(); change(data); cli.write_state(data)
    direct[1].clear()
    before = cli.state_path().read_bytes()
    for action in (cli.status, lambda: cli.update(update_args()), lambda: cli.setup(options(mode="direct"))):
        with pytest.raises(RuntimeError):
            action()
        assert not direct[1] and cli.state_path().read_bytes() == before


@pytest.mark.parametrize("generation", [None, {}, {"args": []}, {"args": ["--aliases", "/gateway"]},
    {"args": ["--direct-only", "--shared-dir", "/wrong", "--direct-launchers"]},
    {"args": ["--direct-only", "--shared-dir", "/wrong", "--direct-launchers", "--models-out", "/models"]}])
def test_status_and_update_refuse_lost_direct_metadata(direct, generation):
    cli.setup(options(mode="direct"))
    data = cli.read_state()
    Path(data["cli_file"]).write_text(json.dumps({"generation": generation}))
    direct[1].clear()
    before = cli.state_path().read_bytes()
    for action in (cli.status, lambda: cli.update(update_args())):
        with pytest.raises(RuntimeError, match="Direct-only launcher verification failed"):
            action()
        assert not direct[1] and cli.state_path().read_bytes() == before


def test_failed_update_policy_verification_retains_incomplete_receipt(direct, monkeypatch):
    cli.setup(options(mode="direct"))
    data = cli.read_state()
    monkeypatch.setattr(cli, "run", lambda *a, **kw: Path(data["cli_file"]).write_text('{}'))
    with pytest.raises(RuntimeError, match="Direct-only launcher verification failed"):
        cli.update(update_args())
    assert cli.read_state(allow_incomplete=True)["status"] == "incomplete"


def test_direct_plans_are_read_only_even_without_tools(tmp_path):
    home = tmp_path / "home"; home.mkdir()
    source = tmp_path / "source"; (source / "bin").mkdir(parents=True)
    shutil.copy2(ROOT / "bin/pi-shared", source / "bin/pi-shared")
    shutil.copytree(ROOT / "lib", source / "lib", ignore=shutil.ignore_patterns("__pycache__"))
    result = subprocess.run([sys.executable, str(source / "bin/pi-shared"), "setup", "--mode", "direct", "--plan"],
                            env={"HOME": str(home), "PATH": str(tmp_path)}, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert "Modules: pi-shared, browser-worker" in result.stdout
    assert "Direct-only policy" in result.stdout
    assert not list(home.iterdir()) and not list(source.rglob("__pycache__"))


def test_direct_update_plan_does_not_require_config_or_write(direct, monkeypatch):
    cli.setup(options(mode="direct"))
    Path(cli.read_state()["cli_file"]).unlink()
    direct[1].clear()
    before = cli.state_path().read_bytes()
    cli.update(update_args(plan=True))
    assert not direct[1] and cli.state_path().read_bytes() == before


def test_shared_policy_boolean_setting(isolated):
    for value in ("0", "1"):
        assert cli.update_support.remember_settings({"PI_SHARED_DIRECT_ONLY": value}) == {"PI_SHARED_DIRECT_ONLY": value}
    with pytest.raises(RuntimeError, match="Invalid boolean"):
        cli.update_support.remember_settings({"PI_SHARED_DIRECT_ONLY": "true"})


def install_direct_fixture(fx):
    (fx["root"] / "lib").mkdir()
    shutil.copy2(ROOT / "lib/direct_setup.py", fx["root"] / "lib/direct_setup.py")
    fx["env"].update(PI_SHARED_DIRECT_ONLY="1", PI_SHARED_BOOTSTRAP_LAUNCHERS="1")
    origin = fx["root"].parent / "shared-origin"
    _exe(origin / "install.sh", '''#!/usr/bin/env python3
import json, os
from pathlib import Path
p = Path(os.environ['PI_SHARED_CLI_OUT'])
p.parent.mkdir(parents=True, exist_ok=True)
p.write_text(json.dumps({'generation': {'args': ['--direct-only', '--shared-dir', str(Path.cwd()), '--no-direct-launchers']}}))
p.chmod(0o600)
''')
    _git(fx, origin, "add", ".")
    _git(fx, origin, "commit", "-qm", "direct support")


def test_installer_direct_policy_skips_recommended_gateway_and_refreshes(installer_setup):
    fx = installer_setup
    install_direct_fixture(fx)
    manifest = fx["root"] / "manifest.yaml"
    manifest.write_text(manifest.read_text() + "  model-gateway:\n    repo: /must-not-clone\n    ref: main\n    tier: recommended\n")
    for flags in ([], ["--update-changed", "--only", "pi-shared"]):
        result = _install(fx, *flags)
        assert result.returncode == 0, result.stdout + result.stderr
        assert (fx["home"] / "doctor-args").read_text().strip() == "--module pi-shared"
    assert not (Path(fx["env"]["PI_SETUP_CODE_ROOT"]) / "model-gateway").exists()


@pytest.mark.parametrize("flags,env", [(["--with-model-gateway"], {}),
    (["--only", "pi-shared,model-gateway"], {}), ([], {"PI_SHARED_BOOTSTRAP_LAUNCHERS": "0"}),
    ([], {"PI_SHARED_CLI_OUT": ""})])
def test_installer_direct_invalid_policy_fails_before_work(installer_setup, flags, env):
    fx = installer_setup
    result = _install(fx, *flags, PI_SHARED_DIRECT_ONLY="1", **env)
    assert result.returncode != 0
    assert "Direct-only setup" in result.stderr
    assert not Path(fx["env"]["PI_SETUP_CODE_ROOT"]).exists()
    assert not (fx["home"] / "doctor-args").exists()


def test_installer_unsupported_direct_preflight_never_runs_installer(installer_setup):
    fx = installer_setup
    origin = fx["root"].parent / "shared-origin"
    _exe(origin / "install.sh", '#!/bin/sh\necho mutated > "$HOME/installer-ran"\n')
    _exe(origin / "bin/pi-launch", '#!/bin/sh\n[ -z "${PI_UPSTREAM_BIN:-}" ] || exit 99\nexit 1\n')
    _git(fx, origin, "add", ".")
    _git(fx, origin, "commit", "-qm", "unsupported direct preflight")
    result = _install(fx, PI_SHARED_DIRECT_ONLY="1", PI_SHARED_BOOTSTRAP_LAUNCHERS="1", PI_UPSTREAM_BIN="/must-not-execute")
    assert result.returncode != 0 and "lacks compatible direct-only" in result.stderr
    assert not (fx["home"] / "installer-ran").exists()
    assert not (fx["home"] / "doctor-args").exists()


def test_installer_older_shared_fails_before_doctor(installer_setup):
    fx = installer_setup
    (fx["root"] / "lib").mkdir()
    shutil.copy2(ROOT / "lib/direct_setup.py", fx["root"] / "lib/direct_setup.py")
    result = _install(fx, PI_SHARED_DIRECT_ONLY="1")
    assert result.returncode != 0 and "direct-only policy" in result.stderr
    assert not (fx["home"] / "doctor-args").exists()


@pytest.mark.skipif(not os.environ.get("PI_SHARED_TEST_ROOT"), reason="opt-in sibling shared integration")
@pytest.mark.parametrize("enabled", ["0", "1"])
def test_real_shared_direct_setup_rerun_refresh_update_status(isolated, monkeypatch, enabled):
    home, calls = isolated
    shared = Path(os.environ["PI_SHARED_TEST_ROOT"]).resolve()
    for key in list(os.environ):
        if key.startswith(("PI_SHARED_", "PI_SETUP_", "PI_LAUNCHER_", "PI_OMLX_", "PI_DATABRICKS_")):
            monkeypatch.delenv(key)
    modules = home / "custom/modules"
    modules.mkdir(parents=True)
    (modules / "pi-shared").symlink_to(shared, target_is_directory=True)
    agent = home / "native-profile"
    agent.mkdir()
    native = {"auth.json": '{"test-provider":{"type":"api_key","key":"synthetic-only"}}',
              "models.json": '{"providers":{}}'}
    for name, content in native.items():
        (agent / name).write_text(content)
        (agent / name).chmod(0o600)
    (agent / "settings.json").write_text('{"defaultProvider":"test-provider","defaultModel":"keep-native"}')
    catalog = home / ".pi/model-gateway/aliases.json"
    catalog.parent.mkdir(parents=True)
    catalog.write_text("INVALID GATEWAY CATALOG MUST NOT BE READ")
    overrides = {"PI_SETUP_CODE_ROOT": str(modules), "PI_SHARED_AGENT_DIR": str(agent),
                 "PI_SHARED_CLI_OUT": str(home / "custom/launcher.json"), "PI_SHARED_DIRECT_LAUNCHERS": enabled}
    for key, value in overrides.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(cli.update_support, "runtime_versions", lambda: {})
    monkeypatch.setattr(cli.update_support, "revisions", lambda *a: {})

    def run(command, *, env=None):
        argv = list(map(str, command))
        calls.append(argv)
        if argv[0] == str(ROOT / "install.sh"):
            assert "model-gateway" not in " ".join(argv)
            argv = [str(shared / "bin/pi-shared-install"), "--no-deps"]
        elif argv[0] == str(ROOT / "bin/doctor"):
            # Full SDK imports/dependencies are covered by the shared suite.
            argv = [str(shared / "bin/pi-launch"), "--launcher-check"]
        result = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stdout + result.stderr

    monkeypatch.setattr(cli, "run", run)
    cli.setup(options(mode="direct"))
    before = cli.read_state()
    config = Path(before["cli_file"])
    generated = json.loads(config.read_text())
    assert generated["generation"]["args"] == ["--direct-only", "--shared-dir", str(shared),
                                               "--direct-launchers" if enabled == "1" else "--no-direct-launchers"]
    for key in overrides:
        monkeypatch.delenv(key)
    cli.setup(options(mode="direct"))
    cli.update(update_args())
    cli.status()
    run([shared / "bin/pi-launch", "--launcher-refresh"], env={**os.environ, "PI_LAUNCHER_CONFIG": str(config)})
    assert json.loads(config.read_text()) == generated
    assert all(cli.read_state()[k] == before[k] for k in ("code_root", "agent_dir", "cli_file", "settings"))
    assert not (home / ".pi-omlx").exists()
    for name, content in native.items():
        assert (agent / name).read_text() == content
    settings = json.loads((agent / "settings.json").read_text())
    assert settings["defaultProvider"] == "test-provider" and settings["defaultModel"] == "keep-native"
    assert catalog.read_text() == "INVALID GATEWAY CATALOG MUST NOT BE READ"
