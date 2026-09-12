"""Updates use saved choices and owned services; all provisioning is stubbed."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import plistlib
import subprocess

import pytest
from test_cli import cli, isolated

VERSIONS = {"node": "v25", "python3": "Python 3.12", "uv": "uv 0.11"}


@pytest.fixture
def saved(isolated, monkeypatch):
    home, calls = isolated
    data = {"version": 2, "status": "module-checks-passed", "operation": "setup",
            "modules": ["pi-shared"], "code_root": str(home / "modules"),
            "agent_dir": str(home / "agent"), "settings": {"PI_SETUP_NO_SHELL_RC": "1"},
            "launchers_file": str(home / "custom/launchers.zsh"), "bootstrap_launchers": True,
            "omlx": None, "runtime_versions": VERSIONS,
            "revisions": {"pi-shared": "a" * 40}, "services": {}}
    cli.write_state(data)
    monkeypatch.setattr(cli.update_support, "runtime_versions", lambda: VERSIONS)
    monkeypatch.setattr(cli.update_support, "revisions", lambda root, modules: {m: "b" * 40 for m in modules})
    return home, calls, data


def args(**kw):
    return argparse.Namespace(**({"plan": False, "modules_only": True} | kw))


def service(saved, name="model-gateway", env=None):
    home, _, data = saved
    path = home / "Library/LaunchAgents/com.local.model-gateway.plist"
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {"Label": "com.local.model-gateway", "WorkingDirectory": data["code_root"] + "/model-gateway",
                "ProgramArguments": ["/owned/python", "main.py"], "EnvironmentVariables": env or {},
                "StandardOutPath": str(home / "private/custom.log"), "StandardErrorPath": str(home / "private/custom.log")}
    path.write_bytes(plistlib.dumps(document)); path.chmod(0o600)
    data["modules"].append(name)
    data["revisions"][name] = "a" * 40
    data["services"][name] = {"path": str(path), "identity": cli.update_support.identity(document)}
    cli.write_state(data)
    return path, document


def test_plan_is_read_only(saved):
    before = cli.state_path().read_bytes()
    assert cli.update(args(plan=True)) == 0
    assert not saved[1]
    assert cli.state_path().read_bytes() == before
    assert not (cli.state_path().parent / "update.lock").exists()


def test_legacy_receipt_requires_one_time_capture(saved):
    saved[2]["version"] = 1
    cli.write_state(saved[2])
    with pytest.raises(RuntimeError, match="older receipt"):
        cli.update(args())
    assert not saved[1]


def test_update_uses_saved_selection_and_paths_not_calling_environment(saved, monkeypatch):
    monkeypatch.setenv("PI_SETUP_CODE_ROOT", "/unselected")
    monkeypatch.setenv("PI_SHARED_AGENT_DIR", "/wrong")
    monkeypatch.setenv("PI_SHARED_DIRECT_LAUNCHERS", "1")
    monkeypatch.setenv("MODEL_GATEWAY_FORCE", "1")
    assert cli.update(args()) == 0
    command, options = saved[1][0]
    assert command[-3:] == ["--update-changed", "--only", "pi-shared"]
    env = options["env"]
    assert env["PI_SETUP_CODE_ROOT"] == saved[2]["code_root"]
    assert env["PI_SHARED_AGENT_DIR"] == saved[2]["agent_dir"]
    assert env["PI_SHARED_LAUNCHERS_OUT"] == saved[2]["launchers_file"]
    assert env["PI_SETUP_NO_SHELL_RC"] == "1"
    assert "PI_SHARED_DIRECT_LAUNCHERS" not in env and "MODEL_GATEWAY_FORCE" not in env
    assert json.loads(env["PI_SETUP_INSTALLED_REVISIONS"])["pi-shared"] == "a" * 40
    assert cli.read_state()["revisions"]["pi-shared"] == "b" * 40


def test_runtime_change_reapplies_selected_services(saved, monkeypatch):
    monkeypatch.setattr(cli.update_support, "runtime_versions", lambda: {"node": "new"})
    cli.update(args())
    assert json.loads(saved[1][0][1]["env"]["PI_SETUP_INSTALLED_REVISIONS"]) == {}


def test_failed_update_retains_old_applied_revisions_for_retry(saved, monkeypatch):
    real_run = cli.run
    monkeypatch.setattr(cli, "run", lambda *a, **kw: (_ for _ in ()).throw(subprocess.CalledProcessError(8, "installer")))
    with pytest.raises(subprocess.CalledProcessError):
        cli.update(args())
    data = cli.read_state(allow_incomplete=True)
    assert data["status"] == "incomplete" and data["operation"] == "update"
    assert data["revisions"]["pi-shared"] == "a" * 40
    monkeypatch.setattr(cli, "run", real_run)
    cli.update(args())
    assert cli.read_state()["status"] == "module-checks-passed"
    assert json.loads(saved[1][-1][1]["env"]["PI_SETUP_INSTALLED_REVISIONS"]) == {}  # repair after failed apply


def test_custom_gateway_paths_recovered_from_current_owned_plist(saved):
    env = {"MODEL_GATEWAY_CONFIG": "/private/config.yaml", "MODEL_GATEWAY_MODEL_INFO": "/private/models.json",
           "MODEL_GATEWAY_LEDGER_PATH": "/private/ledger.db", "MODEL_GATEWAY_PORT": "19111",
           "MODEL_GATEWAY_BACKUP_DIR": "/private/backups"}
    service(saved, env=env)
    cli.update(args())
    actual = saved[1][0][1]["env"]
    assert all(actual[k] == v for k, v in env.items())
    assert actual["MODEL_GATEWAY_LOG_FILE"].endswith("private/custom.log")
    assert saved[1][0][0][-1] == "pi-shared,model-gateway"


def test_replaced_service_owner_is_rejected_before_update(saved):
    path, doc = service(saved)
    doc["WorkingDirectory"] = "/other/manager"
    path.write_bytes(plistlib.dumps(doc))
    with pytest.raises(RuntimeError, match="ownership changed"):
        cli.update(args())
    assert not saved[1]


def test_unknown_service_environment_is_not_discarded_or_logged(saved):
    service(saved, env={"CUSTOM_CREDENTIAL": "private-value-never-log"})
    with pytest.raises(RuntimeError) as exc:
        cli.update(args())
    assert "private-value" not in str(exc.value)
    assert not saved[1]


def test_simultaneous_update_is_rejected(saved):
    with cli.update_lock():
        with pytest.raises(RuntimeError, match="Another setup/update"):
            cli.update(args())
    assert not saved[1]


def test_homebrew_update_reexecs_stable_opt_launcher_with_lock(saved, monkeypatch, tmp_path):
    prefix = tmp_path / "opt/pi-shared"
    root = prefix / "libexec"
    root.mkdir(parents=True)
    (root / "homebrew.json").write_text(json.dumps({"manager": "homebrew", "formula": cli.FORMULA}))
    monkeypatch.setattr(cli, "ROOT", root)
    monkeypatch.setattr(cli.subprocess, "check_output", lambda command, **kw: str(prefix) + "\n")
    seen = []
    def execute(path, command, env):
        fd = int(env["PI_SHARED_UPDATE_LOCK_FD"])
        assert os.fstat(fd).st_ino == (cli.state_path().parent / "update.lock").stat().st_ino
        assert os.get_inheritable(fd)
        seen.append((str(path), command))
    monkeypatch.setattr(cli.os, "execve", execute)
    assert cli.update(args(modules_only=False)) == 0
    assert [call[0] for call in saved[1]] == [["brew", "update"], ["brew", "upgrade", "--fetch-HEAD", cli.FORMULA]]
    assert seen == [(str(prefix / "bin/pi-shared"), [str(prefix / "bin/pi-shared"), "update", "--modules-only"])]
    assert cli.read_state(allow_incomplete=True)["status"] == "incomplete"


def test_source_update_fast_forwards_and_reexecs_new_code(saved, monkeypatch, tmp_path):
    root = tmp_path / "source"; root.mkdir()
    monkeypatch.setattr(cli, "ROOT", root)
    monkeypatch.setattr(cli.subprocess, "check_output", lambda command, **kw: str(root) if command[-1] == "--show-toplevel" else "")
    seen = []
    monkeypatch.setattr(cli.os, "execve", lambda path, command, env: seen.append(command))
    cli.update(args(modules_only=False))
    assert ["git", "-C", str(root), "pull", "--ff-only"] in [row[0] for row in saved[1]]
    assert seen == [[str(root / "bin/pi-shared"), "update", "--modules-only"]]


def owned_omlx(saved):
    home, _, data = saved
    plist = home / "Library/LaunchAgents/homebrew.mxcl.omlx.plist"
    plist.parent.mkdir(parents=True)
    document = {"Label": "homebrew.mxcl.omlx", "ProgramArguments": ["/owned/omlx", "serve"]}
    plist.write_bytes(plistlib.dumps(document)); plist.chmod(0o644)
    data["omlx"] = "install"
    data["omlx_version"] = "omlx 1"
    data["services"]["omlx"] = {"path": str(plist), "identity": cli.update_support.identity(document)}
    cli.write_state(data)
    settings = home / ".omlx/settings.json"; settings.parent.mkdir()
    settings.write_text(json.dumps({"server": {"host": "127.0.0.1", "port": 18000}}))


@pytest.mark.parametrize("changed", [False, True])
def test_owned_omlx_uses_current_port_and_restarts_only_changed_version(saved, monkeypatch, changed):
    owned_omlx(saved)
    calls = saved[1]
    versions = iter(["omlx 1", "omlx 2" if changed else "omlx 1"])
    monkeypatch.setattr(cli.update_support, "omlx_version", lambda: next(versions))
    seen = []
    monkeypatch.setattr(cli, "discover_models", lambda url, **kw: seen.append((url, kw)))
    cli.update(args())
    restarted = any(call[0][:3] == ["brew", "services", "restart"] for call in calls)
    assert restarted is changed
    assert seen == [("http://127.0.0.1:18000/v1", {"allow_auth_required": True})]


def test_failed_omlx_restart_is_retried_after_package_already_upgraded(saved, monkeypatch):
    owned_omlx(saved)
    monkeypatch.setattr(cli.update_support, "omlx_version", lambda: "omlx 2")
    monkeypatch.setattr(cli, "discover_models", lambda *a, **kw: None)
    restarts = []
    def execute(command, **kw):
        if list(map(str, command))[:3] == ["brew", "services", "restart"]:
            restarts.append(command)
            if len(restarts) == 1:
                raise subprocess.CalledProcessError(8, "restart")
    monkeypatch.setattr(cli, "run", execute)
    with pytest.raises(subprocess.CalledProcessError):
        cli.update(args())
    assert cli.read_state(allow_incomplete=True)["omlx_version"] == "omlx 1"
    cli.update(args())
    assert len(restarts) == 2
    assert cli.read_state()["omlx_version"] == "omlx 2"


def test_omlx_waits_for_delayed_start_without_restarting_again(saved, monkeypatch):
    owned_omlx(saved)
    monkeypatch.setattr(cli.update_support, "omlx_version", lambda: "omlx 2")
    monkeypatch.setattr(cli.time, "sleep", lambda _: None)
    attempts = []
    def delayed(url, **kw):
        attempts.append(url)
        assert cli.read_state(allow_incomplete=True)["omlx_version"] == "omlx 1"
        if len(attempts) < 3:
            raise RuntimeError("connection refused during startup")
    monkeypatch.setattr(cli, "discover_models", delayed)
    cli.update(args())
    assert len(attempts) == 3
    assert sum(call[0][:3] == ["brew", "services", "restart"] for call in saved[1]) == 1
    assert cli.read_state()["omlx_version"] == "omlx 2"


def test_wrong_homebrew_ownership_cannot_upgrade_another_installation(saved, monkeypatch, tmp_path):
    root = tmp_path / "package/libexec"; root.mkdir(parents=True)
    (root / "homebrew.json").write_text(json.dumps({"manager": "homebrew", "formula": cli.FORMULA}))
    monkeypatch.setattr(cli, "ROOT", root)
    monkeypatch.setattr(cli.subprocess, "check_output", lambda *a, **kw: "/different/package\n")
    with pytest.raises(RuntimeError, match="different installation"):
        cli.update(args(modules_only=False))
    assert not saved[1]
