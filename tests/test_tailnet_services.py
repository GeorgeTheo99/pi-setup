"""Optional search ingress ownership, update preservation and teardown."""
import copy
import plistlib

import pytest
from test_cli import cli, isolated, options
from test_update import saved, args
import teardown


def install_search(saved, *, remote=True):
    home, _, data = saved
    agents = home / "Library/LaunchAgents"
    agents.mkdir(parents=True, exist_ok=True)
    data["modules"].append("local_web_search")
    root = data["code_root"] + "/local_web_search/mcp-websearch"
    local = {"Label": "com.local.mcp-websearch", "WorkingDirectory": root,
             "ProgramArguments": [root + "/.venv/bin/python", root + "/http_server.py"],
             "EnvironmentVariables": {"MCP_PORT": "8889", "WEBSEARCH_PROVIDER_STACK": "brave",
                                      "LOCAL_SEARCH_DATA_DIR": str(home / "search-data")},
             "StandardOutPath": str(home / "logs/mcp-websearch.log"),
             "StandardErrorPath": str(home / "logs/mcp-websearch.log")}
    local_path = agents / "com.local.mcp-websearch.plist"
    local_path.write_bytes(plistlib.dumps(local)); local_path.chmod(0o600)
    path = cli.update_support.tailnet_path()
    ingress = copy.deepcopy(local)
    ingress["Label"] = cli.update_support.TAILNET_LABEL
    ingress["ProgramArguments"].append("--tailnet")
    ingress["EnvironmentVariables"].update(MCP_PORT="18891", MCP_TAILNET_HOST="search.tail123.ts.net")
    if remote:
        path.write_bytes(plistlib.dumps(ingress)); path.chmod(0o600)
    data["services"] = cli.update_support.service_snapshot(data["modules"], data["settings"])
    cli.write_state(data)
    return local_path, path, ingress


def test_default_search_environment_remains_supported(saved):
    install_search(saved, remote=False)
    env = cli.update_support.service_environment(saved[2])
    assert env["MCP_PORT"] == "8889"
    assert env["MCP_TAILNET_HOST"] == ""
    assert cli.update_support.TAILNET_SERVICE not in saved[2]["services"]


def test_tailnet_snapshot_receipt_and_update_preserve_separate_ports(saved, monkeypatch):
    install_search(saved)
    assert cli.read_state()["services"] == saved[2]["services"]
    env = cli.update_support.service_environment(saved[2])
    assert env["MCP_PORT"] == "8889"
    assert env["MCP_TAILNET_PORT"] == "18891"
    assert env["MCP_TAILNET_HOST"] == "search.tail123.ts.net"
    monkeypatch.setenv("MCP_TAILNET_HOST", "attacker.tail123.ts.net")
    assert cli.update(args()) == 0
    actual = saved[1][0][1]["env"]
    assert actual["MCP_TAILNET_HOST"] == "search.tail123.ts.net"
    assert actual["MCP_TAILNET_PORT"] == "18891"
    assert actual["MCP_PORT"] == "8889"


def test_calling_shell_cannot_enable_remote_during_update(saved, monkeypatch):
    install_search(saved, remote=False)
    monkeypatch.setenv("MCP_TAILNET_HOST", "search.tail123.ts.net")
    assert cli.update(args()) == 0
    assert saved[1][0][1]["env"]["MCP_TAILNET_HOST"] == ""


@pytest.mark.parametrize("host", ["", "newhost.tail123.ts.net"])
def test_explicit_setup_plan_can_change_ingress_without_mutation(saved, monkeypatch, capsys, host):
    install_search(saved)
    monkeypatch.setenv("MCP_TAILNET_HOST", host)
    before = cli.state_path().read_bytes()
    assert cli.setup(options(with_search=True, plan=True)) == 0
    assert f"Search tailnet ingress: {host or 'disabled'}" in capsys.readouterr().out
    assert not saved[1]
    assert cli.state_path().read_bytes() == before


def test_teardown_reinstall_defaults_to_disabled_despite_retained_config(isolated, monkeypatch, capsys):
    home, calls = isolated
    config = home / ".local/share/pi-shared/modules/local_web_search/data/install.env"
    config.parent.mkdir(parents=True)
    config.write_text("MCP_TAILNET_HOST=retained.tail123.ts.net\nMCP_TAILNET_PORT=18891\n")
    config.chmod(0o600)
    monkeypatch.delenv("MCP_TAILNET_HOST", raising=False)
    # No receipt is the post-teardown state. Retained config is intentional.
    assert cli.setup(options(with_search=True)) == 0
    assert calls[0][1]["env"]["MCP_TAILNET_HOST"] == ""
    assert "Search tailnet ingress: disabled" in capsys.readouterr().out
    assert "retained.tail123.ts.net" in config.read_text()


def test_local_only_update_cannot_inherit_stale_install_config(saved, monkeypatch):
    install_search(saved, remote=False)
    config = saved[0] / "retained.env"
    config.write_text("MCP_TAILNET_HOST=retained.tail123.ts.net\n")
    config.chmod(0o600)
    saved[2]["settings"]["LOCAL_SEARCH_INSTALL_CONFIG"] = str(config)
    cli.write_state(saved[2])
    assert cli.update(args()) == 0
    assert saved[1][0][1]["env"]["MCP_TAILNET_HOST"] == ""


def test_teardown_includes_remote_before_local(saved):
    local, remote, _ = install_search(saved)
    plan = teardown.build(saved[2])
    services = [a["path"] for a in plan.actions if a.get("service")]
    assert services == [remote, local]


def test_teardown_allows_retry_when_remote_already_removed(saved):
    _, remote, _ = install_search(saved)
    remote.unlink()
    plan = teardown.build(saved[2])
    assert (remote, cli.update_support.TAILNET_LABEL) in plan.absent_services


def test_unrecorded_remote_blocks_update_and_teardown(saved):
    install_search(saved)
    del saved[2]["services"][cli.update_support.TAILNET_SERVICE]
    for action in (cli.update_support.service_environment, teardown.build):
        with pytest.raises(RuntimeError, match="Unrecorded tailnet ingress"):
            action(saved[2])


def test_changed_remote_identity_blocks_update_and_teardown(saved):
    _, remote, ingress = install_search(saved)
    ingress["ProgramArguments"] = ["/other/program"]
    remote.write_bytes(plistlib.dumps(ingress))
    for action in (cli.update_support.service_environment, teardown.build):
        with pytest.raises(RuntimeError, match="ownership changed"):
            action(saved[2])


@pytest.mark.parametrize("change", ["args", "host", "port", "data", "custom", "label"])
def test_snapshot_rejects_wrong_or_unsafe_ingress(saved, change):
    _, remote, ingress = install_search(saved)
    if change == "args":
        ingress["ProgramArguments"] = ["/other/program"]
    elif change == "label":
        ingress["Label"] = "other.label"
    else:
        key, value = {"host": ("MCP_TAILNET_HOST", "example.com"),
                      "port": ("MCP_PORT", "8889"), "data": ("LOCAL_SEARCH_DATA_DIR", "/other"),
                      "custom": ("SECRET_API_KEY", "not-a-real-key")}[change]
        ingress["EnvironmentVariables"][key] = value
    remote.write_bytes(plistlib.dumps(ingress))
    with pytest.raises(RuntimeError, match="[Tt]ailnet"):
        cli.update_support.service_snapshot(saved[2]["modules"], saved[2]["settings"])
