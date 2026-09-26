"""Questionnaire tests: terminal choices, precedence, reruns, and no writes."""
import io
import sys

import pytest

from test_cli import cli, isolated, options


def wizard_options(**overrides):
    return options(**(dict(mode=None, yes=False, without_browser=None, with_omnigent=None,
                          recovery=None, search=None, search_url=None, search_key_file=None,
                          brave_key_file=None, search_port=None, access=[]) | overrides))


def answers(monkeypatch, values):
    iterator = iter(values)
    prompts = []
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    def answer(prompt):
        prompts.append(prompt)
        return next(iterator)
    monkeypatch.setattr("builtins.input", answer)
    return prompts


def test_fresh_wizard_covers_components_before_approval(isolated, monkeypatch, capsys):
    answers(monkeypatch, ["later", "skip", "skip", "skip", "skip", "n"])
    assert cli.setup(wizard_options()) == 0
    output = capsys.readouterr().out
    for label in ("Model connection", "Public browser", "Omnigent", "recovery", "Search", "Setup plan"):
        assert label.lower() in output.lower()
    assert list(isolated[0].iterdir()) == []
    assert isolated[1] == []


def test_explicit_choices_do_not_get_reasked(isolated, monkeypatch):
    prompts = answers(monkeypatch, ["n"])
    args = options(mode="later", yes=False, search="skip")
    assert cli.setup(args) == 0
    assert prompts == ["Apply this plan? [y/N] "]
    assert isolated[1] == []


def test_plan_never_prompts_even_on_a_terminal(isolated, monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: pytest.fail("plan must not prompt"))
    args = wizard_options()
    args.plan = True
    assert cli.setup(args) == 0
    assert list(isolated[0].iterdir()) == []


def test_cancel_at_first_question_is_read_only(isolated, monkeypatch):
    answers(monkeypatch, ["cancel"])
    with pytest.raises(KeyboardInterrupt):
        cli.setup(wizard_options())
    assert list(isolated[0].iterdir()) == []


def test_saved_components_preserved_without_reinstall_question(isolated, monkeypatch):
    cli.setup(options(without_browser=False, with_omnigent=True))
    previous = cli.read_state()
    args = wizard_options()
    answers(monkeypatch, ["keep"])
    cli.collect_components(args, previous, True)
    assert args.without_browser is False
    assert args.with_omnigent is True
    assert args.recovery == "skip"
    assert args.access == []


def test_gateway_can_include_direct_providers(isolated, monkeypatch):
    args = wizard_options()
    answers(monkeypatch, ["existing-gateway", "yes", "skip", "skip"])
    cli.collect_components(args, None, True)
    mode, access = cli.model_access.resolve(args.mode, args.access, None)
    assert mode == "existing-gateway"
    assert access == ["direct", "existing-gateway"]


def test_saved_model_access_addition_is_composable(isolated, monkeypatch):
    args = wizard_options()
    previous = {"mode": "direct", "modules": ["pi-shared"], "recovery": "skip"}
    answers(monkeypatch, ["model-gateway", "keep", "skip", "skip"])
    cli.collect_components(args, previous, True)
    mode, access = cli.model_access.resolve("direct", args.access, previous)
    assert mode == "cloud"
    assert access == ["direct", "model-gateway"]


def test_approved_local_search_provisions_before_installer_and_remembers(isolated, monkeypatch):
    import json
    from pathlib import Path
    home, calls = isolated
    source = home / "brave-source"
    source.write_text("synthetic-questionnaire-token\n")
    source.chmod(0o600)
    original = cli.run
    def installer(command, **kwargs):
        env = kwargs["env"]
        key = Path(env["LOCAL_SEARCH_DATA_DIR"]) / "brave_key"
        assert key.read_text() == source.read_text()
        assert key.stat().st_mode & 0o777 == 0o600
        assert source.read_text().strip() not in repr(command) + repr(env)
        original(command, **kwargs)
    monkeypatch.setattr(cli, "run", installer)
    assert cli.setup(options(search="local", brave_key_file=str(source))) == 0
    receipt = cli.read_state()
    assert receipt["search"]["kind"] == "local"
    assert source.read_text().strip() not in json.dumps(receipt)
    monkeypatch.setattr(cli.update_support, "service_environment", lambda _: {})
    assert cli.setup(options(mode=None)) == 0
    assert cli.read_state()["search"] == receipt["search"]


def test_declined_local_search_does_not_copy_key(isolated, monkeypatch):
    home, calls = isolated
    source = home / "brave-source"
    source.write_text("synthetic-questionnaire-token\n")
    source.chmod(0o600)
    answers(monkeypatch, ["n"])
    assert cli.setup(options(yes=False, search="local", brave_key_file=str(source))) == 0
    assert list(home.iterdir()) == [source]
    assert calls == []


def test_explicit_browser_addition_and_omnigent_optout(isolated):
    cli.setup(options(without_browser=True, with_omnigent=True))
    cli.setup(options(mode=None, without_browser=False, with_omnigent=False))
    receipt = cli.read_state()
    assert "browser-worker" in receipt["modules"]
    assert receipt["omnigent"] is False


def test_existing_search_survives_setup_and_module_update(isolated, monkeypatch):
    import json
    from test_update import args as update_args, VERSIONS
    home, calls = isolated
    monkeypatch.setattr(cli.update_support, "runtime_versions", lambda: VERSIONS)
    monkeypatch.setattr(cli.update_support, "revisions", lambda *a: {})
    cli.setup(options(search="existing", search_url="https://search.example/mcp"))
    assert "local_web_search" not in cli.read_state()["modules"]
    selected = cli.read_state()["search"]
    cli.setup(options(mode=None))
    assert cli.read_state()["search"] == selected
    config = home / ".pi/research/config.json"
    original = cli.run
    def installer(command, **kwargs):
        config.write_text(json.dumps({"mcpUrl": "https://overlay.example/mcp", "browserEnabled": True}))
        original(command, **kwargs)
    monkeypatch.setattr(cli, "run", installer)
    cli.update(update_args())
    assert json.loads(config.read_text()) == {"websearchMcpUrl": selected["url"], "browserEnabled": True}


def test_search_plan_warns_about_environment_overrides_without_values(isolated, monkeypatch, capsys):
    monkeypatch.setenv("SEARCH_MCP_URL", "https://override.example/mcp?token=do-not-print")
    monkeypatch.setenv("SEARCH_MCP_API_KEY", "private-override-value")
    cli.setup(options(plan=True, search="existing", search_url="https://search.example/mcp"))
    output = capsys.readouterr().out
    assert "SEARCH_MCP_URL" in output and "SEARCH_MCP_API_KEY" in output
    assert "override.example" not in output and "private-override-value" not in output
    assert list(isolated[0].iterdir()) == []
