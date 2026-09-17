"""Offline contracts only. The live native smoke is a separate, explicit command."""
import importlib.util
import json
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location("omnigent_smoke", Path(__file__).with_name("omnigent_native_smoke.py"))
smoke = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(smoke)


def test_fixture_drops_credentials_commands_and_real_endpoint(tmp_path):
    (tmp_path / "models.json").write_text(json.dumps({"providers": {"gateway": {
        "apiKey": "!DO_NOT_EXECUTE", "headers": {"secret": "DO_NOT_COPY"},
        "baseUrl": "https://production.invalid/v1", "models": [
            {"id": "fixture-model", "apiKey": "DO_NOT_COPY", "headers": {"secret": "DO_NOT_COPY"}}
        ]}}}))
    before = (tmp_path / "models.json").read_bytes()
    fixture = smoke.fixture_model(tmp_path, "gateway", "fixture-model")
    serialized = json.dumps(fixture)
    assert "DO_NOT" not in serialized and "production.invalid" not in serialized
    assert fixture["providers"]["gateway"]["baseUrl"] == "http://127.0.0.1:9/v1"
    assert (tmp_path / "models.json").read_bytes() == before
    assert not (tmp_path / "auth.json").exists()


def test_missing_model_fails_before_processes_start(tmp_path):
    (tmp_path / "models.json").write_text('{"providers": {}}')
    with pytest.raises(ValueError, match="not declared"):
        smoke.fixture_model(tmp_path, "gateway", "missing")


def test_environment_does_not_forward_live_selectors_or_credentials(tmp_path, monkeypatch):
    for key in ["OPENAI_API_KEY", "DATABRICKS_TOKEN", "OMNIGENT_PI_PATH", "PI_CODING_AGENT_DIR",
                "OMNIGENT_RUNNER_ENV_PASSTHROUGH", "NODE_OPTIONS", "PYTHONPATH", "HTTP_PROXY"]:
        monkeypatch.setenv(key, "DO_NOT_FORWARD")
    env = smoke.isolated_env(tmp_path, "/fixture/bin:/usr/bin:/bin")
    assert "DO_NOT_FORWARD" not in env.values()
    assert env["HOME"] == str(tmp_path)
    assert env["OMNIGENT_DATA_DIR"].startswith(str(tmp_path))
    assert env["OMNIGENT_CONFIG_HOME"].startswith(str(tmp_path))
    assert env["DATABRICKS_CONFIG_FILE"].startswith(str(tmp_path))
    assert env["OMNIGENT_AUTH_ENABLED"] == "0"
    assert env["OMNIGENT_LOCAL_SINGLE_USER"] == "1"


def test_restricted_path_excludes_unrelated_installed_clis(tmp_path, monkeypatch):
    installed = tmp_path / "installed"
    installed.mkdir()
    executables = {}
    for name in ["pi", "omnigent", "tmux", "node", "claude", "dbexec"]:
        target = installed / name
        target.write_text("fixture executable; never run\n")
        if name in ["pi", "omnigent", "tmux", "node"]:
            executables[name] = str(target)
    monkeypatch.setenv("PATH", str(installed))
    home = tmp_path / "home"
    home.mkdir()
    path = smoke.restricted_path(home, executables)
    assert str(installed) not in path.split(":")
    assert "/usr/local/bin" not in path.split(":")
    assert "/opt/homebrew/bin" not in path.split(":")
    assert sorted(p.name for p in (home / "bin").iterdir()) == ["node", "omnigent", "pi", "tmux"]
    assert all((home / "bin" / name).resolve() == Path(target) for name, target in executables.items())


def test_native_argv_pins_model_and_offline_without_a_prompt():
    args = smoke.launch_args("/fixture/omnigent", "http://127.0.0.1:34567", "gateway", "fixture-model", Path("/tmp/probe.ts"))
    assert args == ["/usr/bin/script", "-q", "/dev/null", "/fixture/omnigent", "pi",
                    "--server", "http://127.0.0.1:34567", "--", "--provider", "gateway",
                    "--model", "fixture-model", "--offline", "--extension", "/tmp/probe.ts"]


def valid_report(tmp_path):
    return {"mode": "tui", "omnigent": True, "profile": str(tmp_path / ".pi/agent"),
            "executable": "/fixture/pi", "provider": "gateway", "model": "fixture-model",
            "packages_loaded": [True, True], "basic_tools": True, "bridge_command": True,
            "inference_tested": False}


def test_structured_native_report_accepts_exact_contract(tmp_path):
    smoke.validate_report(valid_report(tmp_path), tmp_path, "/fixture/pi", "gateway", "fixture-model", 2)


@pytest.mark.parametrize("field,value", [("mode", "rpc"), ("model", "wrong"), ("provider", "wrong"),
    ("packages_loaded", [True, False]), ("packages_loaded", [True]), ("bridge_command", False),
    ("profile", "/live/profile"), ("executable", "/wrong/pi"), ("basic_tools", False)])
def test_structured_report_rejects_partial_or_wrong_launch(tmp_path, field, value):
    report = valid_report(tmp_path)
    report[field] = value
    with pytest.raises(ValueError, match="did not match"):
        smoke.validate_report(report, tmp_path, "/fixture/pi", "gateway", "fixture-model", 2)


def test_fixture_process_snapshot_only_includes_own_target_and_descendants(tmp_path, monkeypatch):
    records = tmp_path / ".omnigent/daemons"
    records.mkdir(parents=True)
    (records / "own.json").write_text(json.dumps({"server_url": "http://127.0.0.1:12345", "pid": 100}))
    (records / "other.json").write_text(json.dumps({"server_url": "http://127.0.0.1:9999", "pid": 200}))
    (tmp_path / "tmp").mkdir()
    monkeypatch.setattr(smoke, "process_rows", lambda: {100: (1, "S"), 101: (100, "S"), 102: (101, "S"), 200: (1, "S")})
    assert smoke.fixture_pids(tmp_path, "http://127.0.0.1:12345", "/unused/tmux", []) == {100, 101, 102}


def test_cleanup_accepts_exited_or_zombie_processes_without_signalling(monkeypatch):
    monkeypatch.setattr(smoke, "process_rows", lambda: {100: (1, "Z")})
    assert smoke.wait_stopped({100, 101}) is True


def test_cleanup_does_not_claim_success_or_signal_when_processes_remain(monkeypatch):
    monkeypatch.setattr(smoke, "process_rows", lambda: {100: (1, "S")})
    times = iter([0, 20])
    monkeypatch.setattr(smoke.time, "monotonic", lambda: next(times))
    assert smoke.wait_stopped({100}) is False


def test_smoke_never_uses_global_server_stop_or_default_port():
    source = Path(smoke.__file__).read_text()
    assert '"host", "stop", "--server", server' in source
    assert '[paths["omnigent"], "server", "stop"' not in source
    assert 'sock.bind(("127.0.0.1", 0))' in source
