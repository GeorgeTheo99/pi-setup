"""Composition permutations and preservation; no services or credentials."""
from itertools import combinations
import json
import pytest
import os
import shutil
from pathlib import Path
from test_cli import cli, isolated, options
from test_direct_setup import direct
from test_install_contract import setup as shell_setup, _git, _exe, _install


def test_default_setup_is_direct(direct):
    cli.setup(options(mode=None))
    assert cli.read_state()["model_access"] == ["direct"]


@pytest.mark.parametrize("count", range(5))
def test_all_capability_subsets(count):
    access = cli.model_access
    for selected in combinations(access.CHOICES, count):
        conflict = "existing-gateway" in selected and bool(set(selected) & {"model-gateway", "omlx"})
        if conflict:
            with pytest.raises(RuntimeError, match="refusing to mix"):
                access.resolve("later", selected, None)
        else:
            mode, actual = access.resolve("later", selected, None)
            assert set(selected) <= set(actual)
            assert ("model-gateway" in actual) == (mode in {"cloud", "local", "both"})
            assert ("existing-gateway" in actual) == (mode == "existing-gateway")


@pytest.mark.parametrize("extra,expected", [(["model-gateway"], ["direct", "model-gateway"]),
    (["omlx"], ["direct", "model-gateway", "omlx"])])
def test_add_to_direct_and_repeat_preserves_composition(direct, monkeypatch, extra, expected):
    home, calls = direct
    cli.setup(options(mode=None))
    monkeypatch.setattr(cli.update_support, "service_environment", lambda _: {})
    calls.clear()
    cli.setup(options(mode=None, access=extra))
    state = cli.read_state()
    assert state["model_access"] == expected
    assert state["settings"]["PI_SHARED_DIRECT_ONLY"] == "0"
    assert state["settings"]["PI_SHARED_DIRECT_ENABLED"] == "1"
    assert state["settings"]["PI_SHARED_ENABLE_GATEWAY"] == "1"
    assert "model-gateway" in state["modules"]
    cli.setup(options(mode=None))
    assert cli.read_state()["model_access"] == expected
    assert cli.read_state()["settings"] == state["settings"]


def test_add_direct_to_gateway_preserves_services_and_custom_source(isolated, monkeypatch):
    home, calls = isolated
    key = home / "brave-key"
    key.write_text("synthetic-brave-key\n")
    key.chmod(0o600)
    cli.setup(options(mode="cloud", without_browser=False, with_search=True, brave_key_file=str(key)))
    original = cli.read_state()
    monkeypatch.setattr(cli.update_support, "service_environment", lambda _: {"MODEL_GATEWAY_CONFIG": "/saved/custom.yaml"})
    cli.setup(options(mode=None, access=["direct"]))
    state = cli.read_state()
    assert set(state["modules"]) == set(original["modules"])
    assert state["model_access"] == ["direct", "model-gateway"]
    assert calls[-1][1]["env"]["MODEL_GATEWAY_CONFIG"] == "/saved/custom.yaml"


def test_direct_plus_remote_does_not_read_local_catalog(direct):
    home, calls = direct
    cli.setup(options(mode=None))
    key = home / "key";key.write_text("test-only");key.chmod(0o600)
    cli.setup(options(mode=None, access=["existing-gateway"], gateway_url="https://gateway.example", gateway_key_file=str(key)))
    state = cli.read_state()
    assert state["model_access"] == ["direct", "existing-gateway"]
    assert "model-gateway" not in state["modules"]
    assert calls[-2][1]["env"]["PI_SHARED_ALIASES"].endswith("launcher.gateway-aliases.json")
    assert "connect" in calls[-1][0]


@pytest.mark.parametrize("change", [lambda d: d.update(model_access=["unknown"]),
    lambda d: d.update(model_access=["direct", "direct"]),
    lambda d: d.update(model_access=["direct", "model-gateway"]),
    lambda d: d["settings"].update(PI_SHARED_DIRECT_ENABLED="0")])
def test_corrupt_composition_rejected(direct, change):
    cli.setup(options(mode=None))
    data = cli.read_state();change(data);cli.write_state(data)
    with pytest.raises(RuntimeError):cli.read_state()


def test_plain_repair_keeps_all_saved_modules_and_omnigent(isolated, monkeypatch):
    key = isolated[0] / "brave-key"
    key.write_text("synthetic-brave-key\n")
    key.chmod(0o600)
    cli.setup(options(mode="cloud", without_browser=False, with_search=True, with_omnigent=True,
                      brave_key_file=str(key)))
    before = cli.read_state()
    monkeypatch.setattr(cli.update_support, "service_environment", lambda _: {})
    cli.setup(options(mode=None, without_browser=None, with_omnigent=None))
    after = cli.read_state()
    assert set(after["modules"]) == set(before["modules"])
    assert after["omnigent"] is True


def test_plain_repair_does_not_add_deselected_browser(isolated, monkeypatch):
    cli.setup(options(mode="cloud", without_browser=True))
    monkeypatch.setattr(cli.update_support, "service_environment", lambda _: {})
    cli.setup(options(mode=None, without_browser=None))
    assert cli.read_state()["modules"] == ["pi-shared", "model-gateway"]


def test_failed_first_setup_can_be_retried_without_missing_ownership(isolated, monkeypatch):
    original = cli.run
    monkeypatch.setattr(cli, "run", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("fixture failure")))
    with pytest.raises(RuntimeError, match="fixture failure"):
        cli.setup(options(mode="cloud", without_browser=False))
    assert cli.read_state(allow_incomplete=True)["status"] == "incomplete"
    monkeypatch.setattr(cli, "run", original)
    cli.setup(options(mode=None, without_browser=False))
    assert cli.read_state()["status"] == "module-checks-passed"


def test_legacy_v1_receipt_can_be_reconciled(isolated):
    home, _ = isolated
    cli.write_state({"version":1,"status":"module-checks-passed","code_root":str(home/"modules"),
                     "agent_dir":str(home/".pi/agent"),"modules":["pi-shared"],"mode":"later"})
    cli.setup(options(mode="later"))
    assert cli.read_state()["version"] == 2


def test_failed_setup_does_not_adopt_preexisting_services(isolated, monkeypatch):
    existing = {"model-gateway": {"label": "unowned-fixture"}}
    monkeypatch.setattr(cli.update_support, "service_snapshot", lambda *a: existing)
    monkeypatch.setattr(cli, "run", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("before install")))
    with pytest.raises(RuntimeError, match="before install"):
        cli.setup(options(mode="cloud"))
    assert cli.read_state(allow_incomplete=True)["services"] == {}


@pytest.mark.parametrize("phase", ["before-install", "before-start"])
def test_partial_omlx_setup_retries_unfinished_steps(isolated, monkeypatch, phase):
    _, calls = isolated
    monkeypatch.setattr(cli, "hardware", lambda: None)
    monkeypatch.setattr(cli, "guard_fresh_omlx", lambda: None)
    discovered = []
    monkeypatch.setattr(cli, "wait_omlx", discovered.append)
    original = cli.run
    def fail(args, **kwargs):
        strings = list(map(str, args))
        if (phase == "before-install" and strings[0].endswith("install.sh")) or (
                phase == "before-start" and strings[:3] == ["brew", "services", "start"]):
            raise RuntimeError("interrupted fixture")
        return original(args, **kwargs)
    monkeypatch.setattr(cli, "run", fail)
    with pytest.raises(RuntimeError, match="interrupted fixture"):
        cli.setup(options(mode="local", omlx="install"))
    state = cli.read_state(allow_incomplete=True)
    assert state.get("omlx_phase") == ("installed" if phase == "before-start" else None)
    calls.clear()
    monkeypatch.setattr(cli, "run", original)
    cli.setup(options(mode=None))
    commands = [args for args, _ in calls]
    assert any(args[:3] == ["brew", "services", "start"] for args in commands)
    assert any(args[:2] == ["brew", "install"] for args in commands) == (phase == "before-install")
    assert discovered == ["http://127.0.0.1:8000/v1"]
    assert cli.read_state()["omlx_phase"] == "ready"


def test_composition_reuses_owned_omlx_with_authentication(isolated, monkeypatch):
    home, calls = isolated
    monkeypatch.setattr(cli, "hardware", lambda: None)
    monkeypatch.setattr(cli, "guard_fresh_omlx", lambda: None)
    checks = []
    monkeypatch.setattr(cli, "wait_omlx", lambda url, **kw: checks.append(kw))
    owned = {"omlx": {"path": str(home / "omlx.plist"), "identity": "a" * 64}}
    monkeypatch.setattr(cli.update_support, "service_snapshot", lambda *a: owned)
    monkeypatch.setattr(cli.update_support, "service_environment", lambda _: {})
    cli.setup(options(mode="local", omlx="install"))
    calls.clear(); checks.clear()
    cli.setup(options(mode=None, access=["direct"]))
    assert checks == [{"allow_auth_required": True}]
    assert not any(args[0] == "brew" for args, _ in calls)
    assert cli.read_state()["model_access"] == ["direct", "model-gateway", "omlx"]


@pytest.mark.skipif(not os.environ.get("PI_SHARED_TEST_ROOT"), reason="requires shared source")
def test_actual_shell_gateway_capability_handshake(shell_setup):
    fx = shell_setup
    origin = fx["root"].parent / "shared-origin"
    shared = Path(os.environ["PI_SHARED_TEST_ROOT"])
    shutil.copy2(shared/"bin/pi-shared-install", origin/"bin/pi-shared-install")
    _exe(origin/"install.sh", '#!/bin/sh\n[ "$1" = --enable-gateway ] || exit 19\nprintf forwarded > "$HOME/gateway-flag"\n')
    fakebin = fx["root"].parent/"fakebin"
    (fakebin/"sed").symlink_to(shutil.which("sed"))
    _git(fx,origin,"add",".");_git(fx,origin,"commit","-qm","gateway capability fixture")
    result = _install(fx, PI_SHARED_ENABLE_GATEWAY="1")
    assert result.returncode == 0, result.stdout + result.stderr
    assert (fx["home"]/"gateway-flag").read_text() == "forwarded"


def test_old_shared_installer_rejected_before_install(shell_setup):
    fx = shell_setup
    result = _install(fx, PI_SHARED_ENABLE_GATEWAY="1")
    assert result.returncode != 0
    assert "lacks --enable-gateway" in result.stderr
