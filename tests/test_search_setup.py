"""Search onboarding contracts: temporary homes, no network or service operations."""
import importlib.util
import json
import os
from pathlib import Path
import socket
from types import SimpleNamespace
import warnings

import pytest


SPEC = importlib.util.spec_from_file_location("search_setup", Path(__file__).resolve().parents[1] / "lib/search_setup.py")
search = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(search)
TOKEN = "synthetic-search-test-token"


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr("builtins.input", forbidden)
    monkeypatch.setattr(search.getpass, "getpass", forbidden)
    return home


def forbidden(*args, **kwargs):
    pytest.fail("Unexpected prompt, credential read, or network operation")


def private(path, text=TOKEN):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    path.write_text(text)
    path.chmod(0o600)
    return path


def collect(home, *, previous=None, env=None, interactive=False, choose=forbidden, **kwargs):
    return search.collect(SimpleNamespace(**kwargs), previous or {}, env if env is not None else {},
                          home / "modules", interactive, choose)


def snapshot(home):
    return {str(p.relative_to(home)): (p.stat().st_mode, p.read_bytes() if p.is_file() else None)
            for p in home.rglob("*")}


def existing(home, **kwargs):
    return collect(home, search="existing", search_url="https://search.example/mcp", **kwargs)


def test_fresh_local_collect_is_read_only_and_receipt_has_no_secrets(home, capsys):
    source = private(home / "source/key")
    before = snapshot(home)
    env = {}
    selected, secrets = collect(home, search="local", brave_key_file=str(source), env=env)
    assert selected == {"kind": "local", "url": "http://127.0.0.1:8889/mcp",
                        "data_dir": str(home / ".local/share/pi-shared/search"), "port": 8889}
    assert secrets == {"brave_key": TOKEN}
    assert env == {"LOCAL_SEARCH_DATA_DIR": selected["data_dir"], "MCP_PORT": "8889"}
    assert snapshot(home) == before
    assert not (home / "modules").exists()
    search.describe(selected)
    assert TOKEN not in json.dumps(selected) + capsys.readouterr().out


def test_apply_local_private_files_env_and_idempotency(home):
    source = private(home / "source/key")
    selected, secrets = collect(home, search="local", brave_key_file=str(source), search_port=8999)
    env = {}
    search.apply(selected, secrets, env)
    target = Path(selected["data_dir"]) / "brave_key"
    config = home / ".pi/research/config.json"
    assert target.read_text() == TOKEN + "\n"
    assert target.stat().st_mode & 0o777 == 0o600
    assert target.parent.stat().st_mode & 0o777 == 0o700
    assert config.stat().st_mode & 0o777 == 0o600
    assert config.parent.stat().st_mode & 0o777 == 0o700
    assert json.loads(config.read_text()) == {"websearchMcpUrl": selected["url"]}
    assert env == {"LOCAL_SEARCH_DATA_DIR": selected["data_dir"], "MCP_PORT": "8999"}
    before = snapshot(home)
    search.apply(selected, secrets, env)
    assert snapshot(home) == before


def test_existing_reference_config_preserves_browser_and_scopes_auth(home, capsys):
    key = private(home / "keys/bearer")
    config = private(home / ".pi/research/config.json", json.dumps({
        "browser": {"url": "http://127.0.0.1:9000"}, "other": [1, 2],
        "browserMcpUrl": "http://127.0.0.1:9000/mcp", "mcpUrl": "http://old/mcp"}))
    before = snapshot(home)
    selected, secrets = existing(home, search_key_file=str(key))
    assert secrets == {} and snapshot(home) == before
    assert "web_search" in capsys.readouterr().out
    search.apply(selected, secrets, {})
    data = json.loads(config.read_text())
    assert data == {"browser": {"url": "http://127.0.0.1:9000"}, "other": [1, 2],
                    "browserMcpUrl": "http://127.0.0.1:9000/mcp",
                    "websearchMcpUrl": selected["url"], "websearchMcpKeyFile": str(key),
                    "websearchMcpKeyUrl": selected["url"]}
    assert TOKEN not in config.read_text() + json.dumps(selected)


@pytest.mark.parametrize("kind", ["local", "existing"])
def test_switch_to_unauthenticated_removes_old_auth_only(home, kind):
    config = private(home / ".pi/research/config.json", json.dumps({
        "browserMcpUrl": "http://browser/mcp", "websearchMcpKeyFile": "old-key",
        "websearchMcpKeyUrl": "https://old/mcp", "mcpUrl": "old", "unrelated": True}))
    selected, secrets = (collect(home, search="local", with_search=True) if kind == "local" else existing(home))
    search.apply(selected, secrets, {})
    assert json.loads(config.read_text()) == {"browserMcpUrl": "http://browser/mcp", "unrelated": True,
                                              "websearchMcpUrl": selected["url"]}


def test_skip_does_not_touch_even_unsafe_or_invalid_config(home):
    config = private(home / ".pi/research/config.json", "not JSON")
    before = snapshot(home)
    selected, secrets = collect(home, search="skip")
    search.apply(selected, secrets, {})
    assert snapshot(home) == before and config.read_text() == "not JSON"
    assert selected == {"kind": "skip", "url": None}


@pytest.mark.parametrize("legacy", [False, True])
def test_plan_does_not_read_secrets_prompt_or_write(home, monkeypatch, legacy):
    monkeypatch.setattr(search, "_read_file", forbidden)
    monkeypatch.setattr(search, "_hidden", forbidden)
    before = snapshot(home)
    selected, secrets = collect(home, search="local", with_search=legacy, plan=True, interactive=True,
                                brave_key_file=str(home / "not-yet-created"),
                                env={"LOCAL_SEARCH_INSTALL_CONFIG": str(home / "config")})
    assert selected["kind"] == "local" and secrets == {}
    assert snapshot(home) == before
    selected, secrets = existing(home, plan=True, interactive=True, search_key_file=str(home / "missing"))
    assert secrets == {} and selected["key_file"] == str(home / "missing")
    assert snapshot(home) == before


def test_plan_missing_choice_is_noninteractive_skip(home):
    assert collect(home, plan=True, interactive=True) == ({"kind": "skip", "url": None}, {})


def test_new_local_requires_key_legacy_allows_component_validation(home):
    with pytest.raises(RuntimeError, match="Brave key"):
        collect(home, search="local")
    assert collect(home, with_search=True)[1] == {}
    assert collect(home, previous={"modules": ["local_web_search"]})[0]["kind"] == "local"
    assert not list(home.iterdir())


@pytest.mark.parametrize("location", ["env", "repo"])
def test_reuses_existing_data_and_persisted_port(home, location):
    data = home / ("custom-data" if location == "env" else "modules/local_web_search/data")
    private(data / "brave_key")
    private(home / "modules/local_web_search/data/install.env", "IGNORED=unused\nMCP_PORT=9012\n")
    env = {"LOCAL_SEARCH_DATA_DIR": str(data)} if location == "env" else {}
    selected, secrets = collect(home, search="local", env=env)
    assert selected["data_dir"] == str(data) and selected["port"] == 9012 and secrets == {}
    assert env["MCP_PORT"] == "9012"


def test_custom_install_config_and_explicit_port_precedence(home):
    data = home / "data"
    private(data / "brave_key")
    config = private(home / "config/install.env", "MCP_PORT=9013\n")
    env = {"LOCAL_SEARCH_DATA_DIR": str(data), "LOCAL_SEARCH_INSTALL_CONFIG": str(config)}
    assert collect(home, search="local", env=env)[0]["port"] == 9013
    env["MCP_PORT"] = "9014"
    assert collect(home, search="local", env=env)[0]["port"] == 9014
    assert collect(home, search="local", env=env, search_port=9015)[0]["port"] == 9015


def test_install_config_is_data_not_executed(home):
    data = home / "data"
    private(data / "brave_key")
    private(home / "modules/local_web_search/data/install.env", f'MCP_PORT="$(touch {home}/pwned)"\n')
    with pytest.raises(RuntimeError, match="port"):
        collect(home, search="local", env={"LOCAL_SEARCH_DATA_DIR": str(data)})
    assert not (home / "pwned").exists()


@pytest.mark.parametrize("kind", ["local", "existing", "skip"])
def test_saved_rerun_and_explicit_keep(home, kind):
    if kind == "local":
        key = private(home / "data/brave_key")
        saved = {"kind": kind, "url": "http://127.0.0.1:9321/mcp", "data_dir": str(key.parent), "port": 9321}
    elif kind == "existing":
        key = private(home / "keys/bearer")
        saved = {"kind": kind, "url": "https://example.test/custom/mcp", "key_file": str(key)}
    else:
        saved = {"kind": kind, "url": None}
    assert collect(home, previous={"search": saved}) == (saved, {})
    answers = iter(["keep"])
    def choose(question, choices):
        answer = next(answers)
        assert answer in choices
        return answer
    assert collect(home, previous={"search": saved}, interactive=True, choose=choose) == (saved, {})


def test_keep_unauthenticated_saved_endpoint_does_not_ask_for_auth(home):
    saved = {"kind": "existing", "url": "https://example.test/mcp"}
    answers = iter(["keep"])
    assert collect(home, previous={"search": saved}, interactive=True,
                   choose=lambda *_: next(answers)) == (saved, {})


def test_endpoint_change_never_reuses_saved_bearer(home):
    key = private(home / "keys/bearer")
    saved = {"kind": "existing", "url": "https://old.example/mcp", "key_file": str(key)}
    selected, secrets = existing(home, previous={"search": saved})
    assert "key_file" not in selected and secrets == {}


@pytest.mark.parametrize("credential", ["file", "hidden"])
def test_interactive_local_credential_collection_no_write(home, monkeypatch, credential, capsys):
    source = private(home / "source/key")
    answers = iter(["local", credential])
    monkeypatch.setattr("builtins.input", lambda _: str(source))
    monkeypatch.setattr(search.getpass, "getpass", lambda _: TOKEN)
    before = snapshot(home)
    selected, secrets = collect(home, interactive=True, choose=lambda *_: next(answers))
    assert secrets == {"brave_key": TOKEN} and snapshot(home) == before
    assert search.BRAVE_SIGNUP in capsys.readouterr().out
    assert TOKEN not in json.dumps(selected)


def test_interactive_local_reuse_does_not_read_hidden_input(home):
    key = private(home / "data/brave_key")
    selected, secrets = collect(home, search="local", interactive=True, choose=lambda *_: "reuse",
                                env={"LOCAL_SEARCH_DATA_DIR": str(key.parent)})
    assert selected["data_dir"] == str(key.parent) and secrets == {}


def test_interactive_remote_http_rejected_before_hidden_token_prompt(home):
    with pytest.raises(RuntimeError, match="HTTPS"):
        collect(home, search="existing", search_url="http://example.test/mcp", interactive=True,
                choose=lambda *_: "hidden")
    assert not list(home.iterdir())


def test_interactive_existing_hidden_token_is_pending_until_apply(home, monkeypatch):
    answers = iter(["existing", "hidden"])
    monkeypatch.setattr("builtins.input", lambda _: "https://example.test/mcp")
    monkeypatch.setattr(search.getpass, "getpass", lambda _: TOKEN)
    selected, secrets = collect(home, interactive=True, choose=lambda *_: next(answers))
    assert selected["key_file"] == str(home / ".pi/research/search_key")
    assert not list(home.iterdir()) and secrets == {"search_key": TOKEN}
    search.apply(selected, secrets, {})
    assert Path(selected["key_file"]).read_text() == TOKEN + "\n"
    assert TOKEN not in (home / ".pi/research/config.json").read_text()


@pytest.mark.parametrize("stage", ["choice", "path", "hidden"])
@pytest.mark.parametrize("failure", [EOFError, KeyboardInterrupt])
def test_cancellation_is_read_only(home, monkeypatch, stage, failure):
    def fail(*_):
        raise failure
    monkeypatch.setattr("builtins.input", fail)
    monkeypatch.setattr(search.getpass, "getpass", fail)
    choose = fail if stage == "choice" else lambda *_: "file" if stage == "path" else "hidden"
    with pytest.raises(failure):
        collect(home, search=None if stage == "choice" else "local", interactive=True, choose=choose)
    assert not list(home.iterdir())


@pytest.mark.parametrize("stage", ["choice", "path", "hidden"])
def test_cancel_word_is_read_only(home, monkeypatch, stage):
    monkeypatch.setattr("builtins.input", lambda _: "cancel")
    monkeypatch.setattr(search.getpass, "getpass", lambda _: "cancel")
    choose = lambda *_: "cancel" if stage == "choice" else "file" if stage == "path" else "hidden"
    with pytest.raises(KeyboardInterrupt):
        collect(home, search="local", interactive=True, choose=choose)
    assert not list(home.iterdir())


def test_getpass_warning_fails_before_echo_fallback(home, monkeypatch):
    def fallback(_):
        warnings.warn("Cannot control echo", search.getpass.GetPassWarning)
        pytest.fail("Echo fallback reached")
    monkeypatch.setattr(search.getpass, "getpass", fallback)
    with pytest.raises(RuntimeError, match="Hidden input"):
        collect(home, search="local", interactive=True, choose=lambda *_: "hidden")
    assert not list(home.iterdir())


@pytest.mark.parametrize("url", ["ftp://example/mcp", "https://user:secret@example/mcp", "https://example?",
    "https://example/#secret", "https://example/mcp?token=secret", "https://example/\n", "https://example/\x1b",
    "https://example/%0a", "https://example:99999/mcp", "https://example:0/mcp", "https://[invalid/mcp",
    "https://example\\evil/mcp", "https://example%2fother/mcp", "https:///mcp", "not-a-url"])
def test_rejects_unsafe_urls_without_echoing_input(home, url):
    with pytest.raises(RuntimeError) as exc:
        collect(home, search="existing", search_url=url, plan=True)
    assert url not in str(exc.value) and "secret" not in str(exc.value)
    assert not list(home.iterdir())


@pytest.mark.parametrize("url", ["http://example.test/mcp", "http://192.168.1.1/mcp", "http://100.64.1.1/mcp"])
def test_authenticated_remote_http_rejected_even_in_plan(home, url):
    with pytest.raises(RuntimeError, match="HTTPS"):
        collect(home, search="existing", search_url=url, search_key_file=str(home / "missing"), plan=True)
    # No bearer token: explicit ordinary HTTP endpoint remains supported.
    assert collect(home, search="existing", search_url=url)[0]["url"] == url


@pytest.mark.parametrize("url", ["http://127.0.0.1:8889/mcp", "http://localhost:8889/mcp",
                                  "http://[::1]:8889/mcp", "https://example.test/custom/mcp"])
def test_authenticated_secure_or_loopback_url(home, url):
    key = private(home / "keys/bearer")
    assert collect(home, search="existing", search_url=url, search_key_file=str(key))[0]["url"] == url


@pytest.mark.parametrize("damage", ["symlink", "ancestor-symlink", "dangling-symlink", "hardlink", "directory",
                                   "readable", "executable", "writable-parent", "foreign-owner"])
def test_unsafe_credential_paths_fail_closed(home, monkeypatch, damage):
    key = private(home / "keys/key")
    if damage in {"symlink", "dangling-symlink"}:
        key.unlink()
        key.symlink_to(home / "target")
    elif damage == "ancestor-symlink":
        alias = home / "alias"
        alias.symlink_to(key.parent, target_is_directory=True)
        key = alias / key.name
    elif damage == "hardlink":
        os.link(key, home / "other")
    elif damage == "directory":
        key.unlink(); key.mkdir()
    elif damage in {"readable", "executable"}:
        key.chmod(0o644 if damage == "readable" else 0o700)
    elif damage == "writable-parent":
        key.parent.chmod(0o777)
    elif damage == "foreign-owner":
        real = os.fstat
        def foreign(fd):
            info = real(fd)
            return SimpleNamespace(st_mode=info.st_mode, st_uid=os.getuid() + 1,
                                   st_nlink=info.st_nlink, st_size=info.st_size)
        monkeypatch.setattr(search.os, "fstat", foreign)
    with pytest.raises(RuntimeError):
        collect(home, search="local", brave_key_file=str(key))
    assert not (home / ".local").exists()


@pytest.mark.parametrize("path", ["relative/key", "~/key", "/tmp/../key", "/tmp/key\n"])
def test_rejects_non_absolute_or_unsafe_paths_in_plan(home, path):
    with pytest.raises(RuntimeError):
        collect(home, search="local", brave_key_file=path, plan=True)
    with pytest.raises(RuntimeError):
        existing(home, search_key_file=path, plan=True)


def test_apply_revalidates_key_reference_after_collection(home):
    key = private(home / "keys/bearer")
    selected, secrets = existing(home, search_key_file=str(key))
    key.chmod(0o644)
    with pytest.raises(RuntimeError):
        search.apply(selected, secrets, {})
    assert not (home / ".pi").exists()


def test_apply_never_overwrites_different_key(home):
    key = private(home / "data/brave_key", "original")
    source = private(home / "source/key")
    selected, secrets = collect(home, search="local", brave_key_file=str(source),
                                env={"LOCAL_SEARCH_DATA_DIR": str(key.parent)})
    before = snapshot(home)
    with pytest.raises(RuntimeError, match="different value"):
        search.apply(selected, secrets, {})
    assert snapshot(home) == before


@pytest.mark.parametrize("damage", ["symlink", "hardlink", "malformed", "array", "ancestor-symlink"])
def test_unsafe_config_prevents_key_provisioning(home, damage):
    config = private(home / ".pi/research/config.json", "{}")
    if damage == "symlink":
        config.unlink(); config.symlink_to(home / "outside")
    elif damage == "hardlink":
        os.link(config, home / "linked-config")
    elif damage == "ancestor-symlink":
        directory = config.parent
        directory.rename(home / "research")
        directory.symlink_to(home / "research", target_is_directory=True)
    else:
        config.write_text("{" if damage == "malformed" else "[]")
    source = private(home / "source/key")
    selected, secrets = collect(home, search="local", brave_key_file=str(source))
    with pytest.raises(RuntimeError):
        search.apply(selected, secrets, {})
    assert not Path(selected["data_dir"]).exists()


@pytest.mark.parametrize("extra", [dict(search="skip", with_search=True), dict(search="existing", with_search=True),
    dict(search="skip", search_url="https://example/mcp"), dict(search="local", search_key_file="/key"),
    dict(search="existing", brave_key_file="/key"), dict(search="skip", brave_key_file="/key"),
    dict(search="existing", search_port=9000), dict(search="skip", search_port=9000)])
def test_explicit_option_conflicts_even_in_plan(home, extra):
    with pytest.raises(RuntimeError):
        collect(home, plan=True, **extra)
    assert not list(home.iterdir())


@pytest.mark.parametrize("port", [0, 65536, -1, True, "wrong"])
def test_invalid_port(home, port):
    with pytest.raises(RuntimeError, match="port"):
        collect(home, search="local", plan=True, search_port=port)


@pytest.mark.parametrize("selection", [None, [], {"kind": "skip"}, {"kind": "skip", "url": None, "token": TOKEN},
    {"kind": "existing", "url": "https://example/mcp", "token": TOKEN},
    {"kind": "existing", "url": "http://example/mcp", "key_file": "/key"},
    {"kind": "existing", "url": "https://example/mcp", "key_file": None},
    {"kind": "local", "url": "http://127.0.0.1:8888/mcp", "port": 8889, "data_dir": "/data"},
    {"kind": "local", "url": "http://127.0.0.1:8889/mcp", "port": "8889", "data_dir": "/data"},
    {"kind": "local", "url": "http://127.0.0.1:8889/mcp", "port": 8889, "data_dir": "relative"}])
def test_strict_saved_shapes(selection):
    with pytest.raises(RuntimeError):
        search.validate_saved(selection)


def test_configure_reasserts_endpoint_after_installer_without_key_provisioning(home):
    selected, _ = collect(home, search="local", plan=True, search_port=9555)
    config = private(home / ".pi/research/config.json", json.dumps({"mcpUrl": "old", "browser": "retained"}))
    config.parent.chmod(0o755)
    config.chmod(0o644)
    search.configure(selected)
    assert json.loads(config.read_text()) == {"websearchMcpUrl": selected["url"], "browser": "retained"}
    assert config.parent.stat().st_mode & 0o777 == 0o700
    assert config.stat().st_mode & 0o777 == 0o600
    assert not Path(selected["data_dir"]).exists()


def test_configure_existing_checks_bearer_and_skip_is_noop(home):
    key = private(home / "keys/bearer")
    selected, _ = existing(home, search_key_file=str(key))
    search.configure(selected)
    key.unlink()
    before = snapshot(home)
    with pytest.raises(RuntimeError):
        search.configure(selected)
    search.configure({"kind": "skip", "url": None})
    assert snapshot(home) == before


def test_apply_tightens_existing_owned_data_directory(home):
    key = private(home / "data/brave_key")
    key.parent.chmod(0o755)
    selected, secrets = collect(home, search="local", env={"LOCAL_SEARCH_DATA_DIR": str(key.parent)})
    assert key.parent.stat().st_mode & 0o777 == 0o755  # collection is read-only
    search.apply(selected, secrets, {})
    assert key.parent.stat().st_mode & 0o777 == 0o700
    assert key.read_text() == TOKEN


def test_apply_rejects_dangling_local_key_symlink_even_in_legacy_mode(home):
    selected, secrets = collect(home, with_search=True)
    data = Path(selected["data_dir"])
    data.mkdir(parents=True, mode=0o700)
    (data / "brave_key").symlink_to(home / "missing")
    with pytest.raises(RuntimeError, match="symlink"):
        search.apply(selected, secrets, {})
    assert not (home / ".pi").exists()


def test_invalid_saved_selection_and_pending_secrets_rejected(home):
    with pytest.raises(RuntimeError):
        collect(home, previous={"search": {"kind": "skip", "url": None, "token": TOKEN}})
    with pytest.raises(RuntimeError):
        search.apply({"kind": "skip", "url": None}, {"search_key": TOKEN}, {})
    selected, _ = existing(home)
    with pytest.raises(RuntimeError):
        search.apply(selected, {"search_key": TOKEN}, {})
    assert not list(home.iterdir())


def test_repository_install_config_recovers_custom_data_directory(home):
    data = home / "custom data"
    private(data / "brave_key")
    private(home / "modules/local_web_search/data/install.env",
            f"LOCAL_SEARCH_DATA_DIR={data}\nMCP_PORT=9017\n")
    selected, secrets = collect(home, search="local")
    assert selected["data_dir"] == str(data)
    assert selected["port"] == 9017
    assert secrets == {}


@pytest.mark.parametrize("name", ["Search Data", "Search#Data", "Search'Data", 'Search"Data', r"Search\Data", "Search=Data"])
def test_persisted_settings_match_literal_operator_format(home, name):
    data = home / name
    private(data / "brave_key")
    private(home / "modules/local_web_search/data/install.env",
            f"LOCAL_SEARCH_DATA_DIR={data}\nMCP_PORT=9018\nMCP_PORT=9999\n")
    selected, _ = collect(home, search="local")
    assert selected["data_dir"] == str(data)
    assert selected["port"] == 9018


@pytest.mark.parametrize("change", ["endpoint", "remove-auth", "replace-key", "keep-auth"])
def test_interactive_existing_selection_can_reconfigure_saved_connection(home, monkeypatch, change):
    old = private(home / "old/key")
    new = private(home / "new/key", "synthetic-replacement-token")
    saved = {"kind": "existing", "url": "https://old.example/mcp", "key_file": str(old)}
    if change != "keep-auth":
        old.unlink()  # Broken saved references must not prevent reconfiguration.
    inputs = iter(["https://new.example/mcp"] if change == "endpoint" else ["", str(new)])
    monkeypatch.setattr("builtins.input", lambda _: next(inputs))
    methods = {"endpoint": "none", "remove-auth": "none", "replace-key": "file", "keep-auth": "keep"}
    choices = iter(["existing", methods[change]])
    before = snapshot(home)
    selected, secrets = collect(home, previous={"search": saved}, interactive=True,
                               choose=lambda *_: next(choices))
    assert secrets == {} and snapshot(home) == before
    assert selected["url"] == ("https://new.example/mcp" if change == "endpoint" else saved["url"])
    if change in {"endpoint", "remove-auth"}:
        assert "key_file" not in selected
    else:
        assert selected["key_file"] == str(new if change == "replace-key" else old)


def test_explicit_existing_connection_flags_skip_saved_value_prompts(home):
    key = private(home / "key")
    saved = {"kind": "existing", "url": "https://old.example/mcp", "key_file": str(home / "missing")}
    selected, _ = collect(home, previous={"search": saved}, interactive=True, search="existing",
                          search_url="https://new.example/mcp", search_key_file=str(key))
    assert selected == {"kind": "existing", "url": "https://new.example/mcp", "key_file": str(key)}


@pytest.mark.parametrize("url", ["https://old.example/mcp", "https://new.example/mcp"])
def test_hidden_token_reconfiguration_applies_without_overwriting_original(home, monkeypatch, url):
    old = private(home / ".pi/research/search_key", "synthetic-old-token")
    saved = {"kind": "existing", "url": "https://old.example/mcp", "key_file": str(old)}
    search.apply(saved, {}, {})
    before = snapshot(home)
    monkeypatch.setattr("builtins.input", lambda _: url)
    monkeypatch.setattr(search.getpass, "getpass", lambda _: "synthetic-new-token")
    choices = iter(["existing", "hidden"])
    selected, secrets = collect(home, previous={"search": saved}, interactive=True,
                               choose=lambda *_: next(choices))
    assert snapshot(home) == before  # collection/declining still writes nothing
    target = Path(selected["key_file"])
    assert target != old and target.parent == old.parent
    assert not target.exists()
    search.apply(selected, secrets, {})
    assert target.read_text() == "synthetic-new-token\n"
    assert target.stat().st_mode & 0o777 == 0o600
    assert old.read_text() == "synthetic-old-token"
    config = json.loads((home / ".pi/research/config.json").read_text())
    assert config["websearchMcpKeyFile"] == str(target)
    assert config["websearchMcpKeyUrl"] == url
    assert collect(home, previous={"search": selected}) == (selected, {})
