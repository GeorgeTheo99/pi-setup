"""Offline CLI contracts. No package installation, service mutation or provider calls."""
import argparse
import importlib.machinery
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import urllib.error

import pytest

ROOT = Path(__file__).resolve().parents[1]
loader = importlib.machinery.SourceFileLoader("pi_shared_cli", str(ROOT / "bin/pi-shared"))
spec = importlib.util.spec_from_loader(loader.name, loader)
cli = importlib.util.module_from_spec(spec)
loader.exec_module(cli)


def options(**kwargs):
    values = dict(mode="later", local=False, omlx=None, omlx_url=None, recovery="skip",
                  gateway_url=None, gateway_key_file=None, allow_private_http=None,
                  without_browser=True, with_search=False, with_omnigent=False, update=False, yes=True, plan=False)
    return argparse.Namespace(**(values | kwargs))


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("PI_SETUP_CODE_ROOT", raising=False)
    monkeypatch.delenv("PI_SHARED_AGENT_DIR", raising=False)
    for name in ("PI_SHARED_BOOTSTRAP_LAUNCHERS", "PI_SHARED_DIRECT_LAUNCHERS", "PI_SHARED_LS99_EXTRAS", "PI_SHARED_LAUNCHERS_OUT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(cli.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/fake/bin/" + name)
    monkeypatch.setattr(sys, "stdin", io.StringIO())
    calls = []
    monkeypatch.setattr(cli, "run", lambda args, **kw: calls.append((list(map(str, args)), kw)))
    monkeypatch.setattr(cli.update_support, "omlx_version", lambda: "fixture-omlx")
    return home, calls


def test_help_and_plan_are_side_effect_free_subprocess(tmp_path):
    home = tmp_path / "empty"
    home.mkdir()
    env = {"HOME": str(home), "PATH": os.environ["PATH"], "PYTHONDONTWRITEBYTECODE": "1"}
    for args in (["--help"], ["setup", "--help"], ["--version"],
                 ["setup", "--plan", "--mode", "both", "--omlx", "install", "--recovery", "guide"]):
        result = subprocess.run([sys.executable, str(ROOT / "bin/pi-shared"), *args],
                                env=env, text=True, capture_output=True, timeout=10)
        assert result.returncode == 0, result.stderr
        assert list(home.iterdir()) == []


def test_default_noninteractive_plan_is_direct(isolated, capsys):
    assert cli.setup(options(mode=None, plan=True)) == 0
    assert "Model access: direct" in capsys.readouterr().out
    assert isolated[1] == []
    assert list(isolated[0].iterdir()) == []


@pytest.mark.parametrize("mode,expected", [
    ("later", ["pi-shared", "browser-worker"]),
    ("cloud", ["pi-shared", "model-gateway", "browser-worker"]),
    ("local", ["pi-shared", "model-gateway", "browser-worker"]),
    ("both", ["pi-shared", "model-gateway", "browser-worker"]),
])
def test_selected_modules_and_receipt(isolated, mode, expected):
    home, calls = isolated
    assert cli.setup(options(mode=mode, without_browser=False)) == 0
    receipt = json.loads(cli.state_path().read_text())
    assert receipt["modules"] == expected
    assert receipt["code_root"] == str(home / ".local/share/pi-shared/modules")
    assert receipt["status"] == "module-checks-passed"
    assert stat.S_IMODE(cli.state_path().stat().st_mode) == 0o600
    assert len(calls) == 1
    assert calls[0][0] == [str(ROOT / "install.sh"), "--minimal", *[f"--with-{m}" for m in expected[1:]]]


def test_setup_and_status_print_one_footer_with_child_notes(isolated, capsys, monkeypatch):
    calls = isolated[1]

    def child(args, **kw):
        calls.append((list(map(str, args)), kw))
        with open(kw["env"]["PI_SETUP_NOTES_FILE"], "a") as notes:
            notes.write("CHILD LIMITATION NOT tested\n")
    monkeypatch.setattr(cli, "run", child)
    assert cli.setup(options()) == 0
    out = capsys.readouterr().out
    env = calls[0][1]["env"]
    assert env["PI_SETUP_VERBOSE"] == "0" and not Path(env["PI_SETUP_NOTES_FILE"]).exists()
    assert "Modules\n  pi-shared  -  installed\n" in out
    assert out.count("Not verified:") == 1
    assert "  - CHILD LIMITATION NOT tested\n" in out and "recovery readiness are separate" in out
    calls.clear()
    assert cli.status(verbose=True) == 0
    out = capsys.readouterr().out
    assert calls[0][1]["env"]["PI_SETUP_VERBOSE"] == "1"
    assert out.count("Not verified:") == 1 and "  - CHILD LIMITATION NOT tested\n" in out
    assert "provider/model/recovery readiness is not implied" in out


def test_omnigent_setup_is_opt_in_and_remembered(isolated):
    home, calls = isolated
    assert cli.setup(options(with_omnigent=True)) == 0
    assert "--with-omnigent" in calls[0][0]
    assert cli.read_state()["omnigent"] is True
    calls.clear()
    cli.status()
    assert "--require-omnigent" in calls[0][0]


def test_omnigent_status_can_check_without_changing_saved_selection(isolated):
    home, calls = isolated
    assert cli.setup(options()) == 0
    before = cli.state_path().read_bytes()
    calls.clear()
    cli.status(require_omnigent=True)
    assert "--require-omnigent" in calls[0][0]
    assert cli.state_path().read_bytes() == before


def test_omnigent_custom_profile_fails_before_installation(isolated, monkeypatch):
    home, calls = isolated
    monkeypatch.setenv("PI_SHARED_AGENT_DIR", str(home / "alternate"))
    with pytest.raises(RuntimeError, match="standard ~/.pi/agent"):
        cli.setup(options(with_omnigent=True))
    assert calls == [] and not cli.state_path().exists()


def test_invalid_omnigent_receipt_flag_is_rejected(isolated):
    home, calls = isolated
    cli.setup(options())
    data = cli.read_state()
    data["omnigent"] = "true"
    cli.write_state(data)
    calls.clear()
    with pytest.raises(RuntimeError, match="Invalid setup receipt"):
        cli.status()
    assert calls == []


def test_fresh_setup_enables_management_and_direct_launchers(isolated):
    cli.setup(options())
    env = isolated[1][0][1]["env"]
    assert env["PI_SHARED_BOOTSTRAP_LAUNCHERS"] == "1"
    assert env["PI_SHARED_DIRECT_LAUNCHERS"] == "1"
    assert json.loads(cli.state_path().read_text())["bootstrap_launchers"] is True
    cli.status()
    assert isolated[1][-1][1]["env"]["PI_SHARED_BOOTSTRAP_LAUNCHERS"] == "1"


@pytest.mark.parametrize("choice", ["existing", "legacy", "canonical-opt-out", "legacy-opt-out"])
def test_setup_preserves_launcher_preferences(isolated, monkeypatch, choice):
    if choice in {"existing", "legacy"}:
        suffix = "generated/pi-launchers.zsh" if choice == "existing" else "model-gateway/pi-launchers.zsh"
        path = isolated[0] / ".pi" / suffix
        path.parent.mkdir(parents=True)
        path.write_text("existing choices")
    else:
        monkeypatch.setenv("PI_SHARED_DIRECT_LAUNCHERS" if choice == "canonical-opt-out" else "PI_SHARED_LS99_EXTRAS", "0")
    cli.setup(options())
    env = isolated[1][0][1]["env"]
    assert env.get("PI_SHARED_DIRECT_LAUNCHERS") != "1"


def test_search_update_flags_and_custom_root(isolated, monkeypatch):
    root = isolated[0] / "custom modules"
    monkeypatch.setenv("PI_SETUP_CODE_ROOT", str(root))
    cli.setup(options(with_search=True, update=True))
    assert isolated[1][0][0][-2:] == ["--with-local_web_search", "--update"]
    assert isolated[1][0][1]["env"]["PI_SETUP_CODE_ROOT"] == str(root)


def test_failed_installer_leaves_incomplete_receipt_and_stops(isolated, monkeypatch):
    def fail(args, **kwargs):
        raise subprocess.CalledProcessError(7, args)
    monkeypatch.setattr(cli, "run", fail)
    with pytest.raises(subprocess.CalledProcessError):
        cli.setup(options())
    assert json.loads(cli.state_path().read_text())["status"] == "incomplete"
    with pytest.raises(RuntimeError, match="did not complete"):
        cli.status()


def test_status_checks_saved_selection_and_profile_not_current_environment(isolated, monkeypatch):
    agent = isolated[0] / "profile with spaces"
    monkeypatch.setenv("PI_SHARED_AGENT_DIR", str(agent))
    cli.setup(options())
    monkeypatch.setenv("PI_SETUP_CODE_ROOT", "/wrong")
    monkeypatch.setenv("PI_CODING_AGENT_DIR", "/wrong-profile")
    cli.status()
    args, kwargs = isolated[1][-1]
    assert args == [str(ROOT / "bin/doctor"), "--module", "pi-shared"]
    assert kwargs["env"]["PI_CODING_AGENT_DIR"] == str(agent)
    assert kwargs["env"]["PI_SETUP_CODE_ROOT"] != "/wrong"


@pytest.mark.parametrize("extra", [dict(local=True, mode="both"), dict(omlx="install"),
    dict(mode="local", omlx="existing"), dict(mode="local", omlx="guide", omlx_url="http://127.0.0.1:8000/v1")])
def test_inconsistent_choices_fail_before_mutation(isolated, extra):
    with pytest.raises(RuntimeError):
        cli.setup(options(**extra))
    assert not isolated[1]
    assert not cli.state_path().exists()


def test_local_alias_defaults_to_guidance_never_installs_omlx(isolated, capsys):
    cli.setup(options(mode=None, local=True))
    assert len(isolated[1]) == 1
    assert "Local inference remains PENDING" in capsys.readouterr().out


def test_explicit_existing_omlx_discovers_but_does_not_start_service(isolated, monkeypatch):
    seen = []
    monkeypatch.setattr(cli, "discover_models", seen.append)
    cli.setup(options(mode="local", omlx="existing", omlx_url="http://127.0.0.1:9110/v1"))
    assert seen == ["http://127.0.0.1:9110/v1"]
    assert len(isolated[1]) == 1


def test_omlx_install_is_explicit_guarded_and_checked(isolated, monkeypatch):
    seen = []
    monkeypatch.setattr(cli, "hardware", lambda: seen.append("hardware"))
    monkeypatch.setattr(cli, "guard_fresh_omlx", lambda: seen.append("guard"))
    monkeypatch.setattr(cli, "discover_models", seen.append)
    cli.setup(options(mode="local", omlx="install"))
    assert seen == ["hardware", "guard", "http://127.0.0.1:8000/v1"]
    assert isolated[1][1:][0][0] == ["brew", "tap", "jundot/omlx", "https://github.com/jundot/omlx"]
    assert isolated[1][-1][0] == ["brew", "services", "start", "jundot/omlx/omlx"]


def test_omlx_failure_never_claims_completed_setup(isolated, monkeypatch):
    monkeypatch.setattr(cli, "hardware", lambda: None)
    monkeypatch.setattr(cli, "guard_fresh_omlx", lambda: None)
    times = iter([0, 61])
    monkeypatch.setattr(cli.time, "monotonic", lambda: next(times))
    monkeypatch.setattr(cli, "discover_models", lambda url: (_ for _ in ()).throw(RuntimeError("down")))
    with pytest.raises(RuntimeError, match="startup discovery failed"):
        cli.setup(options(mode="local", omlx="install"))
    assert json.loads(cli.state_path().read_text())["status"] == "incomplete"


def test_existing_omlx_guard_no_adoption(isolated):
    with pytest.raises(RuntimeError, match="Existing oMLX"):
        cli.guard_fresh_omlx()


@pytest.mark.parametrize("machine,version", [("x86_64", "15.0"), ("arm64", "14.9"), ("arm64", "")])
def test_hardware_rejects_unsupported_before_commands(isolated, monkeypatch, machine, version):
    monkeypatch.setattr(cli.platform, "machine", lambda: machine)
    monkeypatch.setattr(cli.platform, "mac_ver", lambda: (version, (), ""))
    with pytest.raises(RuntimeError):
        cli.hardware()


@pytest.mark.parametrize("url", ["http://localhost:8000/v1", "https://example.com/v1", "http://10.0.0.1/v1",
    "http://127.0.0.1/v1?key=secret", "http://u:p@127.0.0.1/v1", "http://127.0.0.1:99999/v1",
    "http://127.0.0.1/v1#x", "http://127.0.0.1/", "file:///etc/passwd"])
def test_discovery_rejects_non_loopback_or_secret_urls(url):
    with pytest.raises(argparse.ArgumentTypeError):
        cli.loopback_url(url)


@pytest.mark.parametrize("url", ["http://127.0.0.1:8000/v1", "http://[::1]:9110/v1/"])
def test_numeric_loopback_urls(url):
    assert cli.loopback_url(url) == url.rstrip("/")


def test_redirects_never_followed():
    with pytest.raises(RuntimeError, match="redirect"):
        cli.NoRedirect().redirect_request(None, None, 302, "", {}, "https://example.com")


@pytest.mark.parametrize("payload,valid", [({"data": []}, True), ({"data": [{"id": "model"}]}, True),
    ({"data": [{"id": 3}]}, False), ([], False), ({}, False)])
def test_discovery_validates_response(isolated, monkeypatch, payload, valid, capsys):
    class Reply(io.BytesIO):
        pass
    class Opener:
        def open(self, url, timeout):
            assert url == "http://127.0.0.1:8000/v1/models"
            return Reply(json.dumps(payload).encode())
    monkeypatch.setattr(cli.urllib.request, "build_opener", lambda *a: Opener())
    if valid:
        cli.discover_models("http://127.0.0.1:8000/v1")
        assert "NOT verified" in capsys.readouterr().out
    else:
        with pytest.raises(RuntimeError):
            cli.discover_models("http://127.0.0.1:8000/v1")


def test_truncated_http_response_becomes_actionable_error(isolated, monkeypatch):
    class Opener:
        def open(self, url, timeout):
            raise cli.http.client.IncompleteRead(b"partial", 10)
    monkeypatch.setattr(cli.urllib.request, "build_opener", lambda *a: Opener())
    with pytest.raises(RuntimeError, match="Could not read a valid oMLX"):
        cli.discover_models("http://127.0.0.1:8000/v1")


def test_recovery_guidance_never_executes_or_claims_ready(isolated, capsys):
    cli.setup(options(recovery="guide"))
    assert len(isolated[1]) == 1
    output = capsys.readouterr().out
    assert "not installed here" in output and "NOT ready" in output


def test_missing_prerequisite_stops_before_receipt(isolated, monkeypatch):
    monkeypatch.setattr(cli.shutil, "which", lambda name: None if name == "pi" else "/fake/" + name)
    with pytest.raises(RuntimeError, match="Missing pi"):
        cli.setup(options())
    assert not isolated[1] and not cli.state_path().exists()


def test_interactive_cancel_and_eof_do_not_mutate(isolated, monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: "n")
    assert cli.setup(options(yes=False, search="skip")) == 0
    assert not cli.state_path().exists() and not isolated[1]
    monkeypatch.setattr("builtins.input", lambda _: (_ for _ in ()).throw(EOFError()))
    with pytest.raises(EOFError):
        cli.setup(options(yes=False, search="skip"))
    assert not cli.state_path().exists()


def test_state_symlink_is_not_overwritten(isolated):
    target = isolated[0] / "preserve"
    target.write_text("keep")
    path = cli.state_path()
    path.parent.mkdir(parents=True)
    path.symlink_to(target)
    with pytest.raises(RuntimeError, match="unsafe"):
        cli.setup(options())
    assert target.read_text() == "keep" and not isolated[1]


def test_state_parent_symlink_is_refused(isolated):
    target = isolated[0] / "target"
    target.mkdir()
    (isolated[0] / ".config").symlink_to(target)
    with pytest.raises(RuntimeError, match="symlinked"):
        cli.setup(options())
    assert not list(target.iterdir())


def test_invalid_receipt_never_runs_doctor(isolated):
    cli.write_state({"version": 1, "modules": ["untrusted-module"]})
    with pytest.raises(RuntimeError, match="Invalid"):
        cli.status()
    assert not isolated[1]


def test_local_pi_jobs_adds_the_module_and_is_remembered(isolated, monkeypatch, capsys):
    home, calls = isolated
    import plistlib

    def install(args, **kw):
        # Stand in for the pi-jobs installer: an owner-only LaunchAgent.
        calls.append((list(map(str, args)), kw))
        plist = home / "Library/LaunchAgents/com.local.pi-jobs.plist"
        plist.parent.mkdir(parents=True, exist_ok=True)
        plist.write_bytes(plistlib.dumps({"Label": "com.local.pi-jobs", "WorkingDirectory": "/x",
                                          "ProgramArguments": ["node", "src/main.ts"]}))
        plist.chmod(0o600)
    monkeypatch.setattr(cli, "run", install)
    assert cli.setup(options(pi_jobs="local")) == 0
    receipt = json.loads(cli.state_path().read_text())
    assert receipt["modules"] == ["pi-shared", "pi-jobs"]
    assert receipt["pi_jobs"] == {"kind": "local", "url": "http://127.0.0.1:8790"}
    assert receipt["services"]["pi-jobs"]["path"].endswith("com.local.pi-jobs.plist")
    assert calls[0][0] == [str(ROOT / "install.sh"), "--minimal", "--with-pi-jobs"]
    assert "pi-jobs: local server" in capsys.readouterr().out
    # A later setup that skips pi-jobs keeps the installed server rather than orphaning it.
    calls.clear()
    assert cli.setup(options(mode=None, pi_jobs="skip")) == 0
    receipt = json.loads(cli.state_path().read_text())
    assert "pi-jobs" in receipt["modules"] and receipt["pi_jobs"]["kind"] == "local"


def test_remote_pi_jobs_connects_before_install_and_stores_no_token(isolated, monkeypatch, capsys):
    home, calls = isolated
    token = "pjt_" + "z" * 43
    key = home / "pi-jobs.token"
    key.write_text(token + "\n")
    key.chmod(0o600)
    applied = []
    monkeypatch.setattr(cli.pi_jobs_setup, "apply", lambda selection, secrets: applied.append(
        (selection, dict(secrets), len(calls))))
    url = "https://studio.example.ts.net/pi-jobs"
    assert cli.setup(options(pi_jobs=url + "/", pi_jobs_token_file=str(key))) == 0
    assert applied == [({"kind": "remote", "url": url}, {"token": token}, 0)], "connect before install"
    receipt_text = cli.state_path().read_text()
    assert token not in receipt_text and str(key) not in receipt_text
    receipt = json.loads(receipt_text)
    assert receipt["pi_jobs"] == {"kind": "remote", "url": url} and "pi-jobs" not in receipt["modules"]
    assert calls[0][0] == [str(ROOT / "install.sh"), "--minimal"]
    assert token not in capsys.readouterr().out


def test_receipt_rejects_local_pi_jobs_without_its_module(isolated):
    assert cli.setup(options(pi_jobs="local")) == 0
    receipt = json.loads(cli.state_path().read_text())
    receipt["modules"].remove("pi-jobs")
    cli.state_path().write_text(json.dumps(receipt))
    with pytest.raises(RuntimeError, match="go together"):
        cli.read_state()
    receipt["pi_jobs"] = {"kind": "remote", "url": "http://jobs.example"}
    cli.state_path().write_text(json.dumps(receipt))
    with pytest.raises(RuntimeError):
        cli.read_state()


def _pi_jobs_plist(home, working_directory="/x", env=None):
    import plistlib
    plist = home / "Library/LaunchAgents/com.local.pi-jobs.plist"
    plist.parent.mkdir(parents=True, exist_ok=True)
    plist.write_bytes(plistlib.dumps({"Label": "com.local.pi-jobs", "WorkingDirectory": working_directory,
                                      "ProgramArguments": ["node", "src/main.ts"],
                                      "EnvironmentVariables": env or {"PI_JOBS_PORT": "8790"}}))
    plist.chmod(0o600)
    return plist


def test_an_independent_pi_jobs_server_is_only_taken_over_explicitly(isolated, capsys):
    home, calls = isolated
    _pi_jobs_plist(home, "/Users/me/local_code/pi-jobs")
    with pytest.raises(RuntimeError, match="--pi-jobs-adopt"):
        cli.setup(options(pi_jobs="local"))
    assert calls == []
    assert cli.setup(options(pi_jobs="local", pi_jobs_adopt=True, plan=True)) == 0
    assert "take over the existing LaunchAgent (now running /Users/me/local_code/pi-jobs)" in capsys.readouterr().out
    assert cli.setup(options(pi_jobs="local", pi_jobs_adopt=True)) == 0
    receipt = json.loads(cli.state_path().read_text())
    assert receipt["services"]["pi-jobs"]["path"].endswith("com.local.pi-jobs.plist")
    # Once owned, later setups need no flag.
    assert cli.setup(options(mode=None, pi_jobs="local")) == 0


def test_a_managed_pi_jobs_server_stays_tracked_and_blocks_remote(isolated):
    home, calls = isolated
    _pi_jobs_plist(home)
    assert cli.setup(options(pi_jobs="local", pi_jobs_adopt=True)) == 0
    # An explicit mode change without pi-jobs flags keeps the module and its ownership.
    assert cli.setup(options(mode="later", pi_jobs="skip")) == 0
    receipt = json.loads(cli.state_path().read_text())
    assert "pi-jobs" in receipt["modules"] and receipt["pi_jobs"]["kind"] == "local"
    with pytest.raises(RuntimeError, match="setup-managed pi-jobs server"):
        cli.setup(options(mode=None, pi_jobs="https://studio.example.ts.net/pi-jobs"))


def test_update_refuses_to_drop_custom_pi_jobs_settings(isolated):
    home, calls = isolated
    plist = _pi_jobs_plist(home)
    assert cli.setup(options(pi_jobs="local", pi_jobs_adopt=True)) == 0
    data = cli.read_state()
    cli.update_support.service_environment(data)
    _pi_jobs_plist(home, env={"PI_JOBS_PORT": "8790", "PI_JOBS_REPO_ROOTS": "/code"})
    receipt = json.loads(cli.state_path().read_text())
    receipt["services"]["pi-jobs"]["identity"] = cli.update_support.identity(cli.update_support.read_service(plist, "pi-jobs"))
    cli.state_path().write_text(json.dumps(receipt))
    with pytest.raises(RuntimeError, match="pi-jobs has unsupported custom environment"):
        cli.update_support.service_environment(cli.read_state())
