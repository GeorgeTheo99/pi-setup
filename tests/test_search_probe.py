"""Existing search preflight: mocked/loopback HTTP only, never provider calls."""
import io
import json
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from email.message import Message
from types import SimpleNamespace

import pytest

from test_search_setup import search, home, existing, private, snapshot, forbidden, TOKEN


def tools():
    return [{"name": name, "inputSchema": {"type": "object", "properties": {
        text: {"type": "string"}, count: {"type": "integer"}}, "required": [text]}}
        for name, text, count in [("web_search", "query", "num_results"),
                                  ("web_fetch", "url", "max_chars")]]


def envelope(result=None):
    return {"jsonrpc": "2.0", "id": "pi-search-setup-0",
            "result": result if result is not None else {"tools": tools()}}


class Response(io.BytesIO):
    def __init__(self, data, content_type="application/json", session=None):
        super().__init__(data if isinstance(data, bytes) else json.dumps(data).encode())
        self.headers = Message()
        self.headers["Content-Type"] = content_type
        if session is not None:
            self.headers["Mcp-Session-Id"] = session


def transport(monkeypatch, responses):
    requests = []
    responses = iter(responses)
    def open_request(request, timeout):
        assert timeout == 10
        requests.append(request)
        result = next(responses)
        if isinstance(result, Exception):
            raise result
        return result
    def opener(*handlers):
        assert any(isinstance(h, search._NoRedirect) for h in handlers)
        assert any(isinstance(h, search.urllib.request.ProxyHandler) and not h.proxies for h in handlers)
        return SimpleNamespace(open=open_request)
    monkeypatch.setattr(search.urllib.request, "build_opener", opener)
    # Exercise inventory contracts in-process; real worker deadline/cleanup is
    # covered separately below using loopback servers and blocking DNS/connect.
    def run_worker(command, *, input, text, capture_output, timeout, check):
        assert command[0] == search.sys.executable
        assert command[1:] == [str(search.Path(search.__file__).resolve()), "--probe-existing"]
        assert TOKEN not in repr(command)
        assert text and capture_output and not check and timeout == search._PROBE_TIMEOUT
        try:
            search._read_inventory(*json.loads(input))
        except RuntimeError as exc:
            return SimpleNamespace(returncode=1, stdout=json.dumps(str(exc)), stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    monkeypatch.setattr(search.subprocess, "run", run_worker)
    return requests


@pytest.mark.parametrize("auth", [False, True])
def test_inventory_only_direct_json_request(home, monkeypatch, capsys, auth):
    requests = transport(monkeypatch, [Response(envelope())])
    search._probe_existing("https://search.example/mcp", TOKEN if auth else None)
    assert len(requests) == 1
    request = requests[0]
    assert request.full_url == "https://search.example/mcp" and request.method == "POST"
    assert json.loads(request.data) == {"jsonrpc": "2.0", "id": "pi-search-setup-0",
                                        "method": "tools/list", "params": {}}
    assert request.get_header("Accept") == "application/json"
    assert request.get_header("Content-type") == "application/json"
    assert request.get_header("Authorization") == ("Bearer " + TOKEN if auth else None)
    assert request.get_header("Mcp-session-id") is None
    assert request.get_header("Cookie") is None
    output = capsys.readouterr().out
    assert "NOT tested" in output and "no tool calls" in output and TOKEN not in output
    assert not list(home.iterdir())


def test_paginated_inventory(home, monkeypatch):
    first = envelope({"tools": tools()[:1], "nextCursor": "next"})
    second = {**envelope({"tools": tools()[1:]}), "id": "pi-search-setup-1"}
    requests = transport(monkeypatch, [Response(first), Response(second)])
    search._probe_existing("https://search.example/mcp")
    assert [json.loads(r.data)["params"] for r in requests] == [{}, {"cursor": "next"}]


@pytest.mark.parametrize("damage,match", [
    ("missing-search", "missing required tool web_search"),
    ("missing-fetch", "missing required tool web_fetch"),
    ("duplicate", "duplicate web_search"),
    ("missing-schema", "object inputSchema"),
    ("array-schema", "object inputSchema"),
    ("required-extra", "does not send"),
    ("required-malformed", "invalid argument schema"),
    ("query-type", "query as string"),
    ("count-type", "num_results as integer"),
    ("missing-count", "num_results as integer"),
    ("url-type", "url as string"),
    ("max-chars-type", "max_chars as integer"),
    ("ref", "Cannot verify"),
    ("composition", "Cannot verify"),
])
def test_incompatible_inventory(home, monkeypatch, damage, match):
    inventory = tools()
    schema = inventory[0]["inputSchema"]
    if damage == "missing-search":
        inventory.pop(0)
    elif damage == "missing-fetch":
        inventory.pop()
    elif damage == "duplicate":
        inventory.append(inventory[0])
    elif damage == "missing-schema":
        inventory[0].pop("inputSchema")
    elif damage == "array-schema":
        schema["type"] = "array"
    elif damage == "required-extra":
        schema["required"].append("api_key")
    elif damage == "required-malformed":
        schema["required"] = [None]
    elif damage == "query-type":
        schema["properties"]["query"]["type"] = "integer"
    elif damage == "count-type":
        schema["properties"]["num_results"]["type"] = ["string", "null"]
    elif damage == "missing-count":
        schema["properties"].pop("num_results")
    elif damage in {"url-type", "max-chars-type"}:
        inventory[1]["inputSchema"]["properties"]["url" if damage == "url-type" else "max_chars"]["type"] = "boolean"
    elif damage == "ref":
        schema["properties"]["query"]["$ref"] = "#/secret"
    else:
        schema["allOf"] = []
    transport(monkeypatch, [Response(envelope({"tools": inventory}))])
    with pytest.raises(RuntimeError, match=match):
        search._probe_existing("https://search.example/mcp")


def test_numeric_union_and_optional_extra_arguments(home, monkeypatch):
    inventory = tools()
    inventory[0]["inputSchema"]["properties"].update(
        num_results={"type": ["number", "null"]}, optional={"type": "string"})
    transport(monkeypatch, [Response(envelope({"tools": inventory}))])
    search._probe_existing("https://search.example/mcp")


@pytest.mark.parametrize("response,match", [
    (Response(b"data: secret\n\n", "text/event-stream"), "SSE"),
    (Response(envelope(), session=TOKEN), "session-free"),
    (Response(b"<html>secret</html>", "text/html"), "application/json"),
    (Response(b"not json"), "invalid JSON"),
    (Response(b"x" * 1048577), "size limit"),
    (Response([]), "JSON-RPC"),
    (Response({**envelope(), "id": "other"}), "JSON-RPC"),
    (Response({**envelope(), "jsonrpc": "1.0"}), "JSON-RPC"),
    (Response({**envelope(), "error": {"message": TOKEN + " initialize first"}}), "initialize/session"),
    (Response(envelope({"tools": [None]})), "invalid tool inventory"),
    (Response(envelope({"tools": "not a list"})), "invalid tool inventory"),
    (Response(envelope({"tools": [], "nextCursor": 1})), "pagination cursor"),
    (TimeoutError(TOKEN), "reachability"),
    (search.urllib.error.URLError(TOKEN), "reachability"),
    *[(search.urllib.error.HTTPError("https://secret.example", status, TOKEN, {}, io.BytesIO(TOKEN.encode())),
       "authentication failed" if status in {401, 403} else f"HTTP {status}")
      for status in [401, 403, 400, 404, 405, 406, 500, 302]],
])
def test_transport_errors_are_safe_and_atomic(home, monkeypatch, response, match, capsys):
    config = private(home / ".pi/research/config.json", '{"browser": true, "websearchMcpUrl": "old"}')
    selected, _ = existing(home, plan=True, search_key_file=str(home / ".pi/research/new-key"))
    before = snapshot(home)
    transport(monkeypatch, [response])
    with pytest.raises(RuntimeError, match=match) as exc:
        search.apply(selected, {"search_key": TOKEN}, {})
    assert TOKEN not in str(exc.value) + capsys.readouterr().out
    assert snapshot(home) == before and config.exists()


def test_redirect_handler_refuses_forwarding(home):
    with pytest.raises(RuntimeError, match="redirects are disabled"):
        search._NoRedirect().redirect_request(None, None, 307, TOKEN, {}, "https://other.example")


@pytest.mark.parametrize("repeat", [True, False])
def test_pagination_is_bounded(home, monkeypatch, repeat):
    pages = [{**envelope({"tools": [], "nextCursor": "same" if repeat else str(i)}),
              "id": f"pi-search-setup-{i}"} for i in range(10)]
    requests = transport(monkeypatch, [Response(page) for page in pages])
    with pytest.raises(RuntimeError, match="repeated pagination" if repeat else "10-page"):
        search._probe_existing("https://search.example/mcp")
    assert len(requests) == (2 if repeat else 10)


def test_missing_tools_do_not_create_config_or_pending_key(home, monkeypatch):
    selected, _ = existing(home, plan=True, search_key_file=str(home / ".pi/research/new-key"))
    transport(monkeypatch, [Response(envelope({"tools": []}))])
    with pytest.raises(RuntimeError, match="missing required tool"):
        search.apply(selected, {"search_key": TOKEN}, {})
    assert not list(home.iterdir())


def test_plan_and_configure_never_start_probe_worker(home, monkeypatch):
    monkeypatch.setattr(search.subprocess, "Popen", forbidden)
    monkeypatch.setattr(search, "_read_inventory", forbidden)
    selected, secrets = existing(home, plan=True)
    search.describe(selected)
    assert secrets == {} and not list(home.iterdir())
    search.configure(selected)


@pytest.mark.parametrize("failure", [
    OSError(TOKEN),
    SimpleNamespace(returncode=-9, stdout=TOKEN, stderr=TOKEN),
    SimpleNamespace(returncode=1, stdout=TOKEN, stderr=TOKEN),
])
def test_worker_failures_do_not_expose_diagnostics(home, monkeypatch, failure):
    def fail(*args, **kwargs):
        if isinstance(failure, Exception):
            raise failure
        return failure
    monkeypatch.setattr(search.subprocess, "run", fail)
    with pytest.raises(RuntimeError, match="reachability") as exc:
        search._probe_existing("https://search.example/mcp", TOKEN)
    assert TOKEN not in str(exc.value)


@contextmanager
def loopback_inventory(mode):
    stop = threading.Event()
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append((request, self.headers.get("Authorization")))
            try:
                if mode == "headers":
                    # No inactivity timeout fires: bytes keep arriving, but the
                    # headers never finish within the overall budget.
                    self.connection.sendall(b"HTTP/1.1 200 OK\r\nX-Slow: ")
                    while not stop.wait(0.04):
                        self.connection.sendall(b"x")
                    return
                response = envelope()
                if mode == "pages":
                    if stop.wait(0.22):
                        return
                    response = {**envelope({"tools": [], "nextCursor": str(len(requests))}),
                                "id": request["id"]}
                elif mode == "error":
                    response = {**envelope(), "error": {"message": TOKEN}}
                body = json.dumps(response).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if mode == "body":
                    for byte in body:
                        if stop.wait(0.04):
                            break
                        self.wfile.write(bytes([byte]))
                        self.wfile.flush()
                else:
                    self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = False
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02})
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/mcp", requests
    finally:
        stop.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        assert not thread.is_alive()


def record_workers(monkeypatch, stall=None):
    processes = []
    popen = search.subprocess.Popen

    def start(command, **kwargs):
        assert TOKEN not in repr(command)
        if stall:
            # Block before touching the network, inside the actual worker.
            # DNS in particular is not reliably interruptible by Python signals.
            code = ("import runpy,socket,sys,time; "
                    f"socket.{stall}=lambda *a,**kw: time.sleep(30); "
                    "sys.argv=sys.argv[1:]; runpy.run_path(sys.argv[0],run_name='__main__')")
            command = [command[0], "-c", code, *command[1:]]
        process = popen(command, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(search.subprocess, "Popen", start)
    return processes


@pytest.mark.parametrize("mode", ["headers", "body", "pages"])
def test_real_http_has_one_deadline_and_reaps_worker(home, monkeypatch, capsys, mode):
    monkeypatch.setattr(search, "_PROBE_TIMEOUT", 0.8)
    processes = record_workers(monkeypatch)
    with loopback_inventory(mode) as (url, requests):
        selected = {"kind": "existing", "url": url,
                    "key_file": str(home / ".pi/research/new-key")}
        before = snapshot(home)
        started = time.monotonic()
        with pytest.raises(RuntimeError, match="overall inventory deadline") as exc:
            search.apply(selected, {"search_key": TOKEN}, {})
        elapsed = time.monotonic() - started
        assert 0.7 <= elapsed < 1.8
        assert len(processes) == 1 and processes[0].returncode is not None
        assert all(stream.closed for stream in
                   (processes[0].stdin, processes[0].stdout, processes[0].stderr))
        assert TOKEN not in str(exc.value) + capsys.readouterr().out
        assert snapshot(home) == before
        assert len(requests) >= (2 if mode == "pages" else 1)
        assert all(request["method"] == "tools/list" for request, _ in requests)


@pytest.mark.parametrize("stall", ["getaddrinfo", "create_connection"])
def test_deadline_covers_blocked_dns_and_connection(home, monkeypatch, stall):
    monkeypatch.setattr(search, "_PROBE_TIMEOUT", 0.3)
    processes = record_workers(monkeypatch, stall)
    started = time.monotonic()
    with pytest.raises(RuntimeError, match="overall inventory deadline"):
        search._probe_existing("http://127.0.0.1:1/mcp", TOKEN)
    assert time.monotonic() - started < 1.3
    assert len(processes) == 1 and processes[0].returncode is not None


@pytest.mark.parametrize("mode", ["success", "error"])
def test_real_worker_success_and_sanitized_error(home, monkeypatch, capsys, mode):
    processes = record_workers(monkeypatch)
    with loopback_inventory(mode) as (url, requests):
        if mode == "error":
            with pytest.raises(RuntimeError, match="initialize/session") as exc:
                search._probe_existing(url, TOKEN)
            assert TOKEN not in str(exc.value)
        else:
            search._probe_existing(url, TOKEN)
            assert "NOT tested" in capsys.readouterr().out
        assert len(requests) == 1
        assert requests[0][0]["method"] == "tools/list"
        assert requests[0][1] == "Bearer " + TOKEN
    assert len(processes) == 1 and processes[0].returncode == (1 if mode == "error" else 0)
    assert TOKEN not in capsys.readouterr().out
