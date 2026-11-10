"""pi-jobs onboarding contracts: temporary homes, a loopback stub server, no real services."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import threading
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import pi_jobs_setup as jobs  # noqa: E402

TOKEN = "pjt_" + "a" * 43
OTHER = "pjt_" + "b" * 43
REMOTE = "https://studio.example.ts.net/pi-jobs"
real_get = jobs._get  # the home fixture forbids network calls; server tests restore it


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr("builtins.input", forbidden)
    monkeypatch.setattr(jobs.search_setup.getpass, "getpass", forbidden)
    monkeypatch.setattr(jobs, "_get", forbidden)
    return home


def forbidden(*args, **kwargs):
    pytest.fail("Unexpected prompt, credential read, or network operation")


def private(path, text):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    path.write_text(text)
    path.chmod(0o600)
    return path


def collect(*, previous=None, interactive=False, choose=forbidden, **kwargs):
    return jobs.collect(SimpleNamespace(**kwargs), previous or {}, interactive, choose)


def client(home):
    return json.loads((home / ".config/pi-jobs/client.json").read_text())


def test_saved_selections_are_exact_and_secret_free():
    for selection in ({"kind": "skip"}, {"kind": "local", "url": jobs.LOCAL_URL}, {"kind": "remote", "url": REMOTE}):
        jobs.validate_saved(selection)
    for bad in ({"kind": "local", "url": "http://127.0.0.1:9999"}, {"kind": "remote", "url": REMOTE, "token": TOKEN},
                {"kind": "remote", "url": REMOTE + "/"}, {"kind": "remote", "url": "http://studio.example"},
                {"kind": "skip", "url": None}, {"kind": "other"}, []):
        with pytest.raises(RuntimeError):
            jobs.validate_saved(bad)


def test_urls_allow_plain_http_only_on_loopback_and_tailnet():
    for url in ("https://jobs.example/pi-jobs/", "http://127.0.0.1:8790", "http://localhost:8790",
                "http://100.125.111.48/pi-jobs", "http://studio.example.ts.net/pi-jobs", "http://[fd7a:115c:a1e0::1]"):
        assert jobs._url(url) == url.rstrip("/")
    for url in ("http://jobs.example", "http://192.168.1.8", "http://100.128.0.1", "ftp://x",
                "https://user:pw@x.example", "https://x.example/?q=1", "https://x.example/#f", "not a url"):
        with pytest.raises(RuntimeError):
            jobs._url(url)


def test_defaults_flags_and_saved_precedence(home):
    assert collect() == ({"kind": "skip"}, {})
    assert collect(pi_jobs="local") == ({"kind": "local", "url": jobs.LOCAL_URL}, {})
    saved = {"pi_jobs": {"kind": "local", "url": jobs.LOCAL_URL}}
    assert collect(previous=saved) == (saved["pi_jobs"], {})
    assert collect(previous=saved, pi_jobs="skip") == ({"kind": "skip"}, {})
    with pytest.raises(RuntimeError, match="requires --pi-jobs <server URL>"):
        collect(pi_jobs="local", pi_jobs_token_file="/x")


def test_plan_reads_no_files_and_prompts_for_nothing(home):
    selection, secrets = collect(pi_jobs=REMOTE + "/", pi_jobs_token_file="/does/not/exist", plan=True)
    assert selection == {"kind": "remote", "url": REMOTE} and secrets == {}
    assert collect(pi_jobs=REMOTE, plan=True, interactive=True) == ({"kind": "remote", "url": REMOTE}, {})


def test_remote_token_file_must_be_private_and_valid(home):
    path = private(home / "token", TOKEN + "\n")
    assert collect(pi_jobs=REMOTE, pi_jobs_token_file=str(path))[1] == {"token": TOKEN}
    path.chmod(0o644)
    with pytest.raises(RuntimeError, match="0600"):
        collect(pi_jobs=REMOTE, pi_jobs_token_file=str(path))
    private(path, "not-a-token")
    with pytest.raises(RuntimeError, match="pi-jobs API token"):
        collect(pi_jobs=REMOTE, pi_jobs_token_file=str(path))


def test_remote_without_a_token_needs_a_file_unless_one_is_saved_for_that_url(home):
    with pytest.raises(RuntimeError, match="--pi-jobs-token-file"):
        collect(pi_jobs=REMOTE)
    private(home / ".config/pi-jobs/client.json", json.dumps({"url": REMOTE, "token": TOKEN}))
    assert collect(pi_jobs=REMOTE) == ({"kind": "remote", "url": REMOTE}, {})
    with pytest.raises(RuntimeError, match="--pi-jobs-token-file"):
        collect(pi_jobs="https://other.example/pi-jobs")


def test_guided_remote_asks_for_url_then_hidden_token(home, monkeypatch):
    answers = iter([REMOTE])
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    monkeypatch.setattr(jobs.search_setup.getpass, "getpass", lambda prompt: TOKEN)
    menus = []

    def choose(question, choices):
        menus.append((question, set(choices)))
        return "remote" if question.startswith("Scheduled") else "hidden"

    assert collect(interactive=True, choose=choose) == ({"kind": "remote", "url": REMOTE}, {"token": TOKEN})
    assert menus[0] == ("Scheduled agent jobs (pi-jobs):", {"local", "remote", "skip"})


def test_guided_keep_returns_the_saved_selection(home):
    saved = {"kind": "remote", "url": REMOTE}
    assert collect(previous={"pi_jobs": saved}, interactive=True, choose=lambda q, c: "keep") == (saved, {})


@pytest.fixture
def server(monkeypatch):
    """A loopback pi-jobs stub: health is public, /api/session needs TOKEN."""
    state = {"apiVersion": 1, "requests": []}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            state["requests"].append((self.path, self.headers.get("Authorization")))
            if self.path == "/pi-jobs/api/health":
                body, status = {"ok": True, "apiVersion": state["apiVersion"]}, 200
            elif self.path == "/pi-jobs/api/session" and self.headers.get("Authorization") == f"Bearer {TOKEN}":
                body, status = {"token": {"name": "laptop"}}, 200
            else:
                body, status = {"error": "Sign in with an API token"}, 401
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    state["url"] = f"http://127.0.0.1:{httpd.server_address[1]}/pi-jobs"
    yield state
    httpd.shutdown()


def test_apply_probes_then_writes_a_private_client_config(home, server, monkeypatch):
    monkeypatch.setattr(jobs, "_get", real_get)
    selection = {"kind": "remote", "url": server["url"]}
    jobs.apply(selection, {"token": TOKEN})
    assert client(home) == {"url": server["url"], "token": TOKEN}
    assert (home / ".config/pi-jobs/client.json").stat().st_mode & 0o777 == 0o600
    assert (home / ".config/pi-jobs").stat().st_mode & 0o777 == 0o700
    assert [path for path, _ in server["requests"]] == ["/pi-jobs/api/health", "/pi-jobs/api/session"]
    assert server["requests"][0][1] is None, "health is probed without the token"

    # Re-applying with the saved token changes nothing.
    jobs.apply(selection, {})
    assert list((home / ".config/pi-jobs").glob("*.bak")) == []


def test_apply_refuses_a_rejected_token_or_wrong_api_version_without_writing(home, server, monkeypatch):
    monkeypatch.setattr(jobs, "_get", real_get)
    selection = {"kind": "remote", "url": server["url"]}
    with pytest.raises(RuntimeError, match="rejected the token"):
        jobs.apply(selection, {"token": OTHER})
    server["apiVersion"] = 2
    with pytest.raises(RuntimeError, match="API version 1"):
        jobs.apply(selection, {"token": TOKEN})
    assert not (home / ".config/pi-jobs/client.json").exists()


def test_switching_servers_keeps_the_previous_client_config(home, server, monkeypatch):
    monkeypatch.setattr(jobs, "_get", real_get)
    private(home / ".config/pi-jobs/client.json", json.dumps({"url": "http://127.0.0.1:8790", "token": OTHER}))
    jobs.apply({"kind": "remote", "url": server["url"]}, {"token": TOKEN})
    assert client(home)["url"] == server["url"]
    [backup] = (home / ".config/pi-jobs").glob("client.json.*.bak")
    assert json.loads(backup.read_text())["token"] == OTHER
    assert backup.stat().st_mode & 0o777 == 0o600


def test_local_and_skip_apply_write_nothing(home):
    jobs.apply({"kind": "local", "url": jobs.LOCAL_URL}, {})
    jobs.apply({"kind": "skip"}, {})
    with pytest.raises(RuntimeError, match="Invalid pending"):
        jobs.apply({"kind": "local", "url": jobs.LOCAL_URL}, {"token": TOKEN})
    assert not (home / ".config").exists()


def test_configure_and_describe_never_write_or_show_tokens(home, capsys):
    jobs.configure({"kind": "remote", "url": REMOTE})
    assert "is missing" in capsys.readouterr().out
    private(home / ".config/pi-jobs/client.json", json.dumps({"url": "http://127.0.0.1:8790", "token": TOKEN}))
    jobs.configure({"kind": "remote", "url": REMOTE})
    assert "not the selected" in capsys.readouterr().out
    for selection in ({"kind": "skip"}, {"kind": "local", "url": jobs.LOCAL_URL}, {"kind": "remote", "url": REMOTE}):
        jobs.describe(selection)
    out = capsys.readouterr().out
    assert TOKEN not in out and "pi-jobs token create" in out and REMOTE in out

