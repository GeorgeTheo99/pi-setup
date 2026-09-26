"""Direct gateway orchestration contracts; no external traffic or services."""
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from test_cli import ROOT, cli, isolated, options
from test_update import args as update_args, VERSIONS


@pytest.fixture
def gateway(isolated, monkeypatch):
    home, calls = isolated
    key = home / "existing key"
    key.write_text("synthetic-gateway-credential\n")
    key.chmod(0o600)
    monkeypatch.setattr(cli.update_support, "runtime_versions", lambda: VERSIONS)
    monkeypatch.setattr(cli.update_support, "revisions", lambda root, modules: {m: "a" * 40 for m in modules})
    # Any accidental oMLX/network/hardware path must fail even in this stubbed workflow.
    def forbidden(*a, **kw):
        pytest.fail("Unexpected probe/network/service operation")
    monkeypatch.setattr(cli, "discover_models", forbidden)
    monkeypatch.setattr(cli, "hardware", forbidden)
    monkeypatch.setattr(cli.socket, "create_connection", forbidden)
    monkeypatch.setattr(cli.urllib.request, "build_opener", forbidden)
    return home, calls, key


def remote(key, **kw):
    return options(**(dict(mode="existing-gateway", gateway_url="https://gateway.example/v1/",
                          gateway_key_file=str(key)) | kw))


def test_setup_connects_only_after_bootstrap_and_records_references(gateway, capsys):
    home, calls, key = gateway
    cli.setup(remote(key, without_browser=False))
    assert len(calls) == 2
    assert calls[0][0] == [str(ROOT / "install.sh"), "--minimal", "--with-browser-worker"]
    assert calls[1][0] == [str(home / ".local/share/pi-shared/modules/pi-shared/bin/pi-gateway"),
                           "connect", "--url", "https://gateway.example", "--key-file", str(key),
                           "--cli-out", str(home / ".pi/launcher.json")]
    data = cli.read_state()
    assert data["modules"] == ["pi-shared", "browser-worker"]
    assert data["omlx"] is None and data["services"] == {}
    assert data["external_gateway"] == dict(url="https://gateway.example", key_file=str(key), allow_private_http=False)
    output = capsys.readouterr().out
    for text in ("GET /v1/models/canonical", "no inference", "No local gateway or oMLX", str(key)):
        assert text in output
    assert key.read_text().strip() not in output + cli.state_path().read_text() + repr(calls)


def test_private_http_opt_in_is_forwarded_and_remembered(gateway):
    _, calls, key = gateway
    cli.setup(remote(key, gateway_url="http://100.64.1.2:9111/v1", allow_private_http=True))
    assert calls[-1][0][-1] == "--allow-private-http"
    assert cli.read_state()["external_gateway"]["url"] == "http://100.64.1.2:9111"
    calls.clear()
    cli.setup(options(mode=None))
    assert calls[-1][0][-1] == "--allow-private-http"


@pytest.mark.parametrize("extra", [dict(gateway_url=None), dict(gateway_key_file=None),
    dict(gateway_key_file="relative/key"), dict(gateway_url="http://100.64.1.2:9111"),
    dict(gateway_url="https://user:private@gateway.example"), dict(gateway_url="https://gateway.example?key=private"),
    dict(gateway_url="https://gateway.example/#private"), dict(gateway_url="https://gateway.example/other"),
    dict(gateway_url="https://gateway.example\nprivate"), dict(gateway_url="https://gateway.example:99999"),
    dict(gateway_url="file:///private"), dict(gateway_url="http://8.8.8.8", allow_private_http=True),
    dict(gateway_url="http://gateway.example", allow_private_http=True),
    dict(local=True), dict(omlx="guide"), dict(omlx_url="http://127.0.0.1/v1")])
def test_incompatible_or_unsafe_options_fail_before_commands(gateway, extra):
    _, calls, key = gateway
    with pytest.raises(RuntimeError):
        cli.setup(remote(key, **extra))
    assert calls == [] and not cli.state_path().exists()


@pytest.mark.parametrize("extra", [dict(gateway_url="https://gateway.example"), dict(gateway_key_file="/private/key"), dict(allow_private_http=True)])
def test_gateway_options_cannot_leak_into_other_modes(isolated, extra):
    with pytest.raises(RuntimeError, match="require --mode existing-gateway"):
        cli.setup(options(**extra))
    assert not isolated[1]


@pytest.mark.parametrize("kind", ["missing", "directory", "symlink", "hardlink", "readable", "executable"])
def test_unsafe_key_file_stops_before_installer_or_receipt(gateway, kind):
    home, calls, key = gateway
    if kind == "missing":
        key.unlink()
    elif kind == "directory":
        key.unlink(); key.mkdir()
    elif kind == "symlink":
        target = home / "target"; key.rename(target); key.symlink_to(target)
    elif kind == "hardlink":
        (home / "other-link").hardlink_to(key)
    else:
        key.chmod(0o644 if kind == "readable" else 0o700)
    with pytest.raises(RuntimeError, match="key file"):
        cli.setup(remote(key))
    assert not calls and not cli.state_path().exists()


def test_plan_never_probes_even_missing_key_or_executables(gateway, monkeypatch):
    home, calls, key = gateway
    key.unlink()
    monkeypatch.setattr(cli.shutil, "which", lambda _: pytest.fail("executable probe"))
    monkeypatch.setattr(cli.existing_gateway, "check_key_file", lambda _: pytest.fail("key probe"))
    assert cli.setup(remote(key, plan=True)) == 0
    assert not calls and not cli.state_path().exists()
    assert list(home.iterdir()) == []


def test_guided_connection_and_cancel_is_safe(gateway, monkeypatch, capsys):
    from test_questionnaire import answers
    _, calls, key = gateway
    answers(monkeypatch, ["https://gateway.example", str(key), "skip", "no"])
    assert cli.setup(options(mode=None, access=["existing-gateway"], yes=False, guided=True)) == 0
    assert "Model access: direct, existing-gateway" in capsys.readouterr().out
    assert not calls and not cli.state_path().exists()


@pytest.mark.parametrize("answer", ["cancel", "eof"])
def test_connection_prompt_cancellation(gateway, monkeypatch, answer):
    _, calls, _ = gateway
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    def respond(_):
        if answer == "eof":
            raise EOFError
        return answer
    monkeypatch.setattr("builtins.input", respond)
    monkeypatch.setattr(cli.setup_menu, "require_terminal", lambda: None)
    with pytest.raises((KeyboardInterrupt, EOFError)):
        cli.setup(options(mode="existing-gateway", yes=False, guided=True))
    assert not calls and not cli.state_path().exists()


def test_connect_failure_leaves_incomplete_receipt_and_retry_reuses_connection(gateway, monkeypatch):
    _, calls, key = gateway
    real_run = cli.run
    def execute(command, **kw):
        real_run(command, **kw)
        if "connect" in command:
            assert cli.read_state(allow_incomplete=True)["status"] == "incomplete"
            raise subprocess.CalledProcessError(9, "pi-gateway")
    monkeypatch.setattr(cli, "run", execute)
    with pytest.raises(subprocess.CalledProcessError):
        cli.setup(remote(key))
    assert cli.read_state(allow_incomplete=True)["external_gateway"]["key_file"] == str(key)
    with pytest.raises(RuntimeError, match="did not complete"):
        cli.status()
    monkeypatch.setattr(cli, "run", real_run)
    cli.setup(options(mode=None))
    assert cli.read_state()["status"] == "module-checks-passed"


def test_rerun_preserves_custom_saved_paths_and_reconnects(gateway, monkeypatch):
    home, calls, key = gateway
    overrides = {"PI_SHARED_CLI_OUT": str(home / "custom/cli.json"),
                 "PI_SETUP_CODE_ROOT": str(home / "custom/modules"),
                 "PI_SHARED_AGENT_DIR": str(home / "custom/agent")}
    for name, value in overrides.items():
        monkeypatch.setenv(name, value)
    cli.setup(remote(key))
    before = cli.read_state()
    for name in overrides:
        monkeypatch.delenv(name)
    calls.clear()
    cli.setup(options(mode=None))
    after = cli.read_state()
    assert all(after[k] == before[k] for k in ("code_root", "agent_dir", "cli_file", "external_gateway", "settings"))
    assert calls[-1][0][1] == "connect"
    assert calls[-1][0][-1] == overrides["PI_SHARED_CLI_OUT"]


@pytest.mark.parametrize("mode", ["later", "cloud", "local", "both"])
def test_remote_cannot_silently_be_dropped_or_mixed(gateway, mode):
    _, calls, key = gateway
    cli.setup(remote(key)); calls.clear()
    before = cli.state_path().read_bytes()
    with pytest.raises(RuntimeError, match="already selected"):
        cli.setup(options(mode=mode))
    assert cli.state_path().read_bytes() == before and not calls


def test_cannot_add_remote_over_previously_selected_local_gateway(gateway):
    _, calls, key = gateway
    cli.setup(options(mode="cloud")); calls.clear()
    before = cli.state_path().read_bytes()
    with pytest.raises(RuntimeError, match="previously selected"):
        cli.setup(remote(key))
    assert cli.state_path().read_bytes() == before and not calls


def test_update_and_status_check_offline_without_remote_ownership(gateway, monkeypatch, capsys):
    home, calls, key = gateway
    monkeypatch.setenv("PI_SHARED_CLI_OUT", str(home / "custom/cli.json"))
    cli.setup(remote(key, gateway_url="http://100.64.1.2:9111", allow_private_http=True))
    before = cli.read_state()
    calls.clear()
    monkeypatch.setenv("PI_SHARED_CLI_OUT", "/wrong")
    monkeypatch.setenv("PI_SETUP_CODE_ROOT", "/wrong")
    cli.update(update_args())
    assert calls[0][0] == [str(ROOT / "install.sh"), "--update-changed", "--only", "pi-shared"]
    assert calls[0][1]["env"]["PI_SHARED_CLI_OUT"] == before["cli_file"]
    assert calls[1][0] == list(map(str, cli.existing_gateway.command(before, "check")))
    after = cli.read_state()
    assert after["external_gateway"] == before["external_gateway"]
    assert after["services"] == {} and after["cli_file"] == before["cli_file"]
    calls.clear()
    cli.status()
    assert calls[0][0] == [str(ROOT / "bin/doctor"), "--module", "pi-shared"]
    assert calls[1][0][1] == "check"
    assert "remote reachability, authentication and inference NOT tested" in capsys.readouterr().out
    assert not any("connect" in cmd or "brew" in cmd for cmd, _ in calls)


def test_failed_offline_update_check_stays_incomplete(gateway, monkeypatch):
    _, _, key = gateway
    cli.setup(remote(key))
    real_run = cli.run
    def execute(command, **kw):
        real_run(command, **kw)
        if "check" in command:
            raise subprocess.CalledProcessError(4, "pi-gateway")
    monkeypatch.setattr(cli, "run", execute)
    with pytest.raises(subprocess.CalledProcessError):
        cli.update(update_args())
    data = cli.read_state(allow_incomplete=True)
    assert data["status"] == "incomplete" and data["operation"] == "update"
    assert data["external_gateway"]["key_file"] == str(key)


def test_update_plan_does_not_check_or_write_connection(gateway):
    _, calls, key = gateway
    cli.setup(remote(key)); calls.clear()
    before = cli.state_path().read_bytes()
    key.unlink()
    cli.update(update_args(plan=True))
    assert not calls and cli.state_path().read_bytes() == before


@pytest.mark.parametrize("change", [lambda d: d.update(external_gateway=None),
    lambda d: d.pop("external_gateway"), lambda d: d["modules"].append("model-gateway"),
    lambda d: d.update(omlx="install"), lambda d: d["external_gateway"].update(allow_private_http="yes"),
    lambda d: d["external_gateway"].update(key_file="relative"),
    lambda d: d["external_gateway"].update(url="https://private@gateway.example"),
    lambda d: d["settings"].update(PI_SHARED_CLI_OUT="/different")])
def test_invalid_remote_receipts_fail_before_commands(gateway, change):
    _, calls, key = gateway
    cli.setup(remote(key))
    data = cli.read_state(); change(data); cli.write_state(data); calls.clear()
    with pytest.raises(RuntimeError):
        cli.status()
    assert not calls


def test_subprocess_plan_with_missing_key_is_read_only(tmp_path):
    home = tmp_path / "home"; home.mkdir()
    source = tmp_path / "source"
    (source / "bin").mkdir(parents=True)
    shutil.copy2(ROOT / "bin/pi-shared", source / "bin/pi-shared")
    shutil.copytree(ROOT / "lib", source / "lib", ignore=shutil.ignore_patterns("__pycache__"))
    result = subprocess.run([sys.executable, str(source / "bin/pi-shared"), "setup", "--plan",
                             "--mode", "existing-gateway", "--gateway-url", "https://gateway.example/v1",
                             "--gateway-key-file", str(home / "missing")],
                            env={"HOME": str(home), "PATH": str(tmp_path)}, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert list(home.iterdir()) == []
    assert not list(source.rglob("__pycache__"))


def test_subprocess_setup_rerun_update_status_with_offline_workflow_stubs(tmp_path):
    home = tmp_path / "home"; home.mkdir()
    source = tmp_path / "source"; (source / "bin").mkdir(parents=True)
    shutil.copy2(ROOT / "bin/pi-shared", source / "bin/pi-shared")
    shutil.copytree(ROOT / "lib", source / "lib", ignore=shutil.ignore_patterns("__pycache__"))
    tools = tmp_path / "tools"; tools.mkdir()
    key = home / "key"; key.write_text("synthetic-test-token\n"); key.chmod(0o600)
    modules = home / "custom/modules"
    config = home / "custom/launcher.json"
    log = tmp_path / "calls.jsonl"
    env = {"HOME": str(home), "PATH": str(tools), "CALLS": str(log),
           "PI_SETUP_CODE_ROOT": str(modules), "PI_SHARED_CLI_OUT": str(config),
           "PI_SETUP_NO_SHELL_RC": "1"}

    def script(path, body):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"#!{sys.executable}\n" + body)
        path.chmod(0o755)

    for tool in ("pi", "node", "npm", "git", "uv", "python3"):
        script(tools / tool, "print('fixture-version')\n")
    header = "import json, os, sys\nfrom pathlib import Path\n"
    record = "with open(os.environ['CALLS'], 'a') as f: f.write(json.dumps(sys.argv) + '\\n')\n"
    script(source / "install.sh", header + record +
           "p = Path(os.environ['PI_SHARED_CLI_OUT'])\n"
           "p.parent.mkdir(parents=True, exist_ok=True)\n"
           "if not p.exists(): p.write_text('{}')\n")
    script(source / "bin/doctor", header + record)
    script(modules / "pi-shared/bin/pi-gateway", header + record +
           "action = sys.argv[1]\n"
           "flags = dict(zip(sys.argv[2::2], sys.argv[3::2]))\n"
           "p = Path(flags['--cli-out'])\n"
           "data = json.loads(p.read_text())  # bootstrap must precede connect\n"
           "expected = {'url': flags['--url'], 'key_file': flags['--key-file']}\n"
           "if action == 'connect': p.write_text(json.dumps(expected))\n"
           "elif action == 'check': assert data == expected\n"
           "else: raise AssertionError('unexpected gateway operation')\n")

    def invoke(*args):
        result = subprocess.run([sys.executable, str(source / "bin/pi-shared"), *args],
                                env=env, capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, result.stdout + result.stderr

    # This suite exercises macOS orchestration, not platform emulation.
    if sys.platform != "darwin":
        pytest.skip("Public setup applies on macOS only")
    invoke("setup", "--mode", "existing-gateway", "--gateway-url", "https://gateway.example/v1",
           "--gateway-key-file", str(key), "--without-browser", "--yes")
    before = config.read_bytes()
    env.pop("PI_SETUP_CODE_ROOT"); env.pop("PI_SHARED_CLI_OUT")
    invoke("setup", "--mode", "existing-gateway", "--without-browser", "--yes")
    invoke("update", "--modules-only")
    invoke("status")
    assert config.read_bytes() == before
    receipt = json.loads((home / ".config/pi-shared/setup.json").read_text())
    assert receipt["external_gateway"] == dict(url="https://gateway.example", key_file=str(key), allow_private_http=False)
    assert receipt["cli_file"] == str(config) and receipt["services"] == {}
    commands = [json.loads(line) for line in log.read_text().splitlines()]
    gateway_actions = [c[1] for c in commands if Path(c[0]).name == "pi-gateway"]
    assert gateway_actions == ["connect", "connect", "check", "check"]
    assert "model-gateway" not in receipt["modules"]
    assert key.read_text().strip() not in log.read_text()
