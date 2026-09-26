"""Focused existing-gateway input and URL safety; no real setup or network."""
import sys

import pytest

from test_cli import cli, isolated, options
from test_direct_setup import direct
from test_questionnaire import answers, wizard_options


URL_PROMPT = "Existing gateway base URL (HTTPS preferred; no credentials): "
KEY_PROMPT = "Absolute path to your existing private gateway key file (0600; not the key): "
HTTP_PROMPT = "HTTP sends the gateway key without TLS. Trust this private/Tailscale endpoint? [y/N] "


@pytest.mark.parametrize("guided", [False, True])
@pytest.mark.parametrize("selection", ["with", "mode", "url"])
def test_focused_inputs_then_approval(isolated, monkeypatch, capsys, guided, selection):
    flags = {"access": ["existing-gateway"]} if selection == "with" else (
        {"mode": "existing-gateway"} if selection == "mode" else {"gateway_url": "https://gateway.example/model-gateway/v1/"})
    values = ([] if selection == "url" else ["https://gateway.example/model-gateway/v1/"]) + ["/private/missing.key", "no"]
    prompts = answers(monkeypatch, values)
    if not guided:
        monkeypatch.setattr(cli.setup_menu, "require_terminal", lambda: pytest.fail("plain input needs no curses"))
        monkeypatch.setattr(cli.setup_menu, "choose", lambda *a: pytest.fail("no component menus"))
    assert cli.setup(wizard_options(guided=guided, **flags)) == 0
    assert prompts == ([] if selection == "url" else [URL_PROMPT]) + [KEY_PROMPT, "Apply this plan?" if guided else "Apply this plan? [y/N] "]
    output = capsys.readouterr().out
    assert "Modules: pi-shared, browser-worker" in output
    assert "GET /model-gateway/v1/models/canonical" in output
    if selection != "mode":
        assert "Model access: direct, existing-gateway" in output
    assert not list(isolated[0].iterdir()) and not isolated[1]


def test_fresh_guided_remote_keeps_direct_without_other_questions(isolated, monkeypatch, capsys):
    prompts = answers(monkeypatch, ["existing-gateway", "https://gateway.example", "/private/key", "no"])
    assert cli.setup(wizard_options()) == 0
    assert prompts == ["Model connection:", URL_PROMPT, KEY_PROMPT, "Apply this plan?"]
    assert "Model access: direct, existing-gateway" in capsys.readouterr().out
    assert not list(isolated[0].iterdir()) and not isolated[1]


@pytest.mark.parametrize("flags", [dict(yes=True), dict(plan=True), dict(plan=True, guided=True), {}])
@pytest.mark.parametrize("missing", ["url", "key"])
def test_no_missing_input_prompts_in_unattended_paths(isolated, monkeypatch, flags, missing):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: bool(flags))
    monkeypatch.setattr(sys.stdout, "isatty", lambda: bool(flags))
    monkeypatch.setattr("builtins.input", lambda _: pytest.fail("unattended input"))
    connection = {"gateway_key_file": "/private/key"} if missing == "url" else {"gateway_url": "https://gateway.example"}
    with pytest.raises(RuntimeError, match="requires --gateway-url and --gateway-key-file"):
        cli.setup(options(**(dict(mode=None, access=["existing-gateway"], yes=False) | connection | flags)))
    assert not list(isolated[0].iterdir()) and not isolated[1]


@pytest.mark.parametrize("extra", [{"mode": mode} for mode in ("direct", "later", "cloud", "local", "both")]
                         + [dict(local=True), dict(omlx="guide"), dict(access=["model-gateway"]), dict(access=["omlx"])])
def test_url_conflicts_fail_before_component_collection(isolated, monkeypatch, extra):
    monkeypatch.setattr(cli, "collect_components", lambda *a: pytest.fail("must reject before collection"))
    with pytest.raises(RuntimeError, match="conflicts"):
        cli.setup(options(**(dict(mode=None, gateway_url="https://gateway.example") | extra)))
    assert not list(isolated[0].iterdir()) and not isolated[1]


def test_url_is_additive_over_saved_direct_selection(direct, monkeypatch):
    home, calls = direct
    monkeypatch.setattr(cli.update_support, "service_environment", lambda _: {})
    cli.setup(options(mode=None, without_browser=False, with_omnigent=True, recovery="guide",
                      search="existing", search_url="https://search.example/mcp"))
    saved = cli.read_state()
    calls.clear()
    prompts = answers(monkeypatch, ["/private/key", "no"])
    args = wizard_options(gateway_url="https://gateway.example", guided=True)
    assert cli.setup(args) == 0
    assert args.access == ["existing-gateway"]
    assert args.model_access == ["direct", "existing-gateway"]
    assert prompts == [KEY_PROMPT, "Apply this plan?"]
    assert args.without_browser is False and args.with_omnigent is True
    assert args.recovery == "guide" and args.search_selection == saved["search"]
    assert cli.read_state() == saved and not calls


def test_remote_retains_explicit_unrelated_flags(isolated, monkeypatch, capsys):
    prompts = answers(monkeypatch, ["no"])
    args = wizard_options(gateway_url="https://gateway.example", gateway_key_file="/private/key",
                          without_browser=True, with_omnigent=True, recovery="guide",
                          search="existing", search_url="https://search.example/mcp")
    assert cli.setup(args) == 0
    assert prompts == ["Apply this plan?"]
    assert args.without_browser is True and args.with_omnigent is True
    assert args.recovery == "guide" and args.search_selection["url"] == "https://search.example/mcp"
    assert "Modules: pi-shared\n" in capsys.readouterr().out
    assert not list(isolated[0].iterdir()) and not isolated[1]


@pytest.mark.parametrize("consent", ["yes", "no", ""])
def test_private_http_requires_focused_consent(isolated, monkeypatch, consent):
    prompts = answers(monkeypatch, ["http://100.64.1.2/model-gateway/v1", "/private/key", consent, "no"])
    args = wizard_options(guided=False, access=["existing-gateway"])
    if consent == "yes":
        assert cli.setup(args) == 0
        assert prompts[-1] == "Apply this plan? [y/N] "
    else:
        with pytest.raises(RuntimeError, match="HTTP requires"):
            cli.setup(args)
    assert prompts[:3] == [URL_PROMPT, KEY_PROMPT, HTTP_PROMPT]
    assert not list(isolated[0].iterdir()) and not isolated[1]


@pytest.mark.parametrize("endpoint,saved_consent", [
    ("http://100.64.1.2/model-gateway/v1/", True),
    ("http://100.64.1.2/other", False),
    ("http://100.64.1.3/model-gateway", False),
])
def test_http_consent_is_scoped_to_saved_endpoint(isolated, monkeypatch, endpoint, saved_consent):
    key = isolated[0] / "key"
    key.write_text("synthetic-fixture-key\n")
    key.chmod(0o600)
    cli.setup(options(mode=None, gateway_url="http://100.64.1.2/model-gateway",
                      gateway_key_file=str(key), allow_private_http=True))
    saved = cli.state_path().read_bytes()
    isolated[1].clear()
    prompts = answers(monkeypatch, ["no"] if saved_consent else ["yes", "no"])
    assert cli.setup(wizard_options(guided=False, gateway_url=endpoint)) == 0
    assert prompts == ([] if saved_consent else [HTTP_PROMPT]) + ["Apply this plan? [y/N] "]
    assert cli.state_path().read_bytes() == saved and not isolated[1]


@pytest.mark.parametrize("suffix,base", [
    ("", ""), ("/", ""), ("/v1", ""), ("/v1/", ""),
    ("/model-gateway", "/model-gateway"), ("/model-gateway/", "/model-gateway"),
    ("/model-gateway/v1", "/model-gateway"), ("/model-gateway/v1/", "/model-gateway"),
    ("/one/two_3.~-X/v1", "/one/two_3.~-X"), ("/v1beta", "/v1beta"),
    ("/.../v1", "/..."),
])
def test_safe_prefix_normalization(suffix, base):
    data = cli.existing_gateway.connection("https://gateway.example" + suffix, "/private/key")
    assert data["url"] == "https://gateway.example" + base
    cli.existing_gateway.validate_saved(data)


@pytest.mark.parametrize("suffix", ["//", "/v1//", "/model-gateway//", "/one//two", "/one//v1",
    "/v1/v1", "/model-gateway/v1/v1/",
    "/.", "/..", "/one/../two", "/./v1", "/one/%2e%2e", "/one%2Ftwo", "/bad\\path",
    "/white space", "/line\nfeed", "/tab\t", "/nonasciié", "/query?x=y", "/fragment#x",
    "/semicolon;x", "/colon:x", "/at@x", "/bracket[x]"])
def test_unsafe_prefix_rejected_without_echo(suffix):
    with pytest.raises(RuntimeError) as error:
        cli.existing_gateway.connection("https://gateway.example" + suffix, "/private/key")
    assert "gateway.example" not in str(error.value)
