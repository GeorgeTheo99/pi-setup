"""Standalone contract/security tests; never execute Peekaboo, download it or touch TCC."""
from contextlib import contextmanager
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import stat
import subprocess
import tempfile
import sys
import tarfile
from types import SimpleNamespace
import urllib.request

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
spec = importlib.util.spec_from_file_location("peekaboo_setup", ROOT / "lib/peekaboo_setup.py")
p = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p)


@pytest.fixture
def home(tmp_path, monkeypatch):
    tmp_path = tmp_path.resolve()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("PI_CODING_AGENT_DIR", raising=False)
    monkeypatch.setattr(p.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(p.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(p, "discover", lambda: None)
    return tmp_path


@pytest.fixture
def binary(home):
    binary = home / "peekaboo"
    binary.write_bytes(b"mock executable")
    binary.chmod(0o755)
    return binary


def args(action="plan", **kwargs):
    return SimpleNamespace(**({"action": action, "config": None, "binary": None,
                              "install": False, "yes": False, "expected_plan": None,
                              "mode": None, "bridge_socket": None,
                              "backend": "adapter", "agent_dir": None} | kwargs))


@contextmanager
def lock():
    yield


def operate(action="plan", **kwargs):
    return p.operate(args(action, **kwargs), lock)


def write_config(home, data):
    config = home / ".config/mcp/mcp.json"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(json.dumps(data))
    config.chmod(0o600)
    return config


def approve(**kwargs):
    plan, code = operate(**kwargs)
    assert code == 0, plan
    return operate("apply", yes=True, expected_plan=plan["planId"], **kwargs)


@pytest.fixture
def probes(monkeypatch):
    calls = []

    def run(command, timeout=15, *, native_only=False):
        calls.append([str(x) for x in command])
        if command[0] == "/usr/bin/codesign":
            assert command[command.index("-R") + 1].startswith("=anchor apple generic"), "codesign needs '=' for an inline requirement, otherwise it opens a file"
            return 0, b"", b""
        if command[-1] == "--version":
            return 0, b"Peekaboo 4.5.0 (main/abc, built: fixture)\n", b""
        if native_only:
            assert command[1:4] == ["permissions", "status", "--bridge-socket"]
            assert command[-1] == "--json" and len(command) == 6
        else:
            assert command[1:] == ["permissions", "status", "--no-remote", "--json"]
        return 0, permission_output(source="bridge" if native_only else "local"), b""

    monkeypatch.setattr(p, "run_bounded", run)
    return calls


def permission_output(granted=True, source="local", **extra):
    return json.dumps({"success": True, "data": {"source": source, "permissions": [
        {"name": name, "isRequired": True, "isGranted": granted, "grantInstructions": "secret must not be printed"}
        for name in ["Screen Recording", "Accessibility", "Event Synthesizing"]]}, **extra}).encode()


@pytest.fixture
def bridge_socket():
    # Short private path also fits macOS's sockaddr_un; not a real app socket.
    with tempfile.TemporaryDirectory(prefix="pb-", dir=str(Path("/tmp").resolve())) as temp:
        path = Path(temp) / "bridge.sock"
        with socket.socket(socket.AF_UNIX) as server:
            server.bind(str(path))
            path.chmod(0o600)
        yield path  # Deliberately not listening: presence is not readiness.


def test_contract_offline_missing(home, monkeypatch):
    def forbidden(*a, **kw):
        pytest.fail("offline operation attempted a side effect")

    monkeypatch.setattr(p.subprocess, "Popen", forbidden)
    monkeypatch.setattr(p.urllib.request, "build_opener", forbidden)
    monkeypatch.setattr(p, "write_config", forbidden)
    for action in ["plan", "status"]:
        report, code = p.operate(args(action), forbidden)
        assert code == 0 and report["ok"]
        assert report["schemaVersion"] == 2 and report["component"] == "peekaboo"
        assert report["action"] == action
        assert len(report["planId"]) == 64
        assert report["evidence"] == {
            "mode": "direct", "bridgeSocketPath": None,
            "bridgeSocketState": "not-applicable", "permissionSource": None,
            "binaryPath": None, "binaryPresent": False, "configuration": "missing",
            "runnable": "not-tested", "permissions": {"screenRecording": "unknown",
                "accessibility": "unknown", "eventSynthesizing": "unknown"},
            "mcp": "not-tested", "toolCount": None, "desktop": "not-tested"}
    assert list(home.iterdir()) == []


def test_plan_status_are_deterministic_and_never_execute(binary, monkeypatch):
    monkeypatch.setattr(p, "run_bounded", lambda *a: pytest.fail("executed in plan"))
    a, _ = operate(binary=str(binary))
    b, _ = operate("status", binary=str(binary))
    assert a["planId"] == b["planId"]
    assert a["evidence"]["binaryPresent"]
    assert a["evidence"]["runnable"] == "not-tested"


@pytest.mark.parametrize("yes,expected", [(False, None), (True, None), (True, "a" * 64)])
def test_apply_denial_no_lock_or_writes(binary, monkeypatch, yes, expected):
    called = []
    report, code = p.operate(args("apply", binary=str(binary), yes=yes, expected_plan=expected),
                             lambda: called.append(True))
    assert code and not report["ok"] and not called
    assert not (binary.parent / ".config").exists()


def test_apply_preserves_other_values_and_idempotency(home, binary, probes):
    original = {"setting": [1, {"secret": "do-not-print"}], "mcpServers": {
        "other": {"url": "https://example.invalid", "token": "secret"}}}
    config = write_config(home, original)
    report, code = approve(binary=str(binary))
    assert code == 0, report
    expected = original | {"mcpServers": original["mcpServers"] | {"peekaboo": p.entry(binary)}}
    assert json.loads(config.read_text()) == expected
    assert config.stat().st_mode & 0o777 == 0o600
    assert "secret" not in json.dumps(report)
    before = config.stat()
    report, code = approve()  # configured command is the only discovery source
    assert code == 0 and config.stat().st_ino == before.st_ino
    assert config.stat().st_mtime_ns == before.st_mtime_ns
    assert report["evidence"]["runnable"] == "yes"
    assert report["evidence"]["mcp"] == "not-tested"
    assert all(c[1] != "permissions" for c in probes)
    requirement = probes[0][-2]
    assert p.TEAM in requirement and p.IDENTIFIER in requirement and "anchor apple generic" in requirement
    assert "--strict" in probes[0]


@pytest.mark.parametrize("content", ["{bad", "[]", '{"mcpServers": []}',
    '{"x":1,"x":2}', '{"value":NaN}', '{"unrelated":1e999}', '{"unrelated":-1e999}',
    '{"mcpServers":{"peekaboo":null}}'])
def test_invalid_config_never_overwritten(home, binary, content):
    config = write_config(home, {})
    config.write_text(content)
    report, code = operate(binary=str(binary))
    assert code and not report["ok"]
    assert report["evidence"]["configuration"] in {"invalid", "conflict"}
    assert config.read_text() == content


@pytest.mark.parametrize("number", ["1e999", "-1e999"])
def test_overflowing_unrelated_number_cannot_be_rewritten(home, binary, probes, number):
    config = write_config(home, {})
    original = ('{"unrelated":{"value":' + number + '}}').encode()
    config.write_bytes(original)
    result, code = operate("apply", binary=str(binary), yes=True, expected_plan="a" * 64)
    assert code and not result["ok"]
    assert config.read_bytes() == original
    assert probes == []


def test_config_serializer_refuses_nonfinite_values(home, binary):
    config = home / "new.json"
    with pytest.raises(ValueError):
        p.write_config(config, {"unrelated": float("inf")}, {"entry": p.entry(binary), "configStamp": None})
    assert not config.exists()


@pytest.mark.parametrize("change", [{"args": ["mcp"]}, {"directTools": 0},
    {"requestTimeoutMs": 30000.0}, {"includeTools": []}, {"approveTools": []},
    {"excludeTools": ["foo"]}, {"env": {"KEY": "secret"}}, {"disabled": True}])
def test_conflicting_config_is_not_normalized(home, binary, change):
    config = write_config(home, {"mcpServers": {"peekaboo": p.entry(binary) | change}})
    original = config.read_bytes()
    report, code = operate(binary=str(binary))
    assert code and report["evidence"]["configuration"] == "conflict"
    assert "secret" not in json.dumps(report)
    assert config.read_bytes() == original


@pytest.mark.parametrize("name", ["Peekaboo", "another"])
def test_other_peekaboo_name_conflicts(home, binary, name):
    write_config(home, {"mcpServers": {name: p.entry(binary)}})
    report, code = operate()
    assert code and "Another Peekaboo" in report["errors"][0]


def test_binary_override_conflict(home, binary):
    write_config(home, {"mcpServers": {"peekaboo": p.entry(binary)}})
    report, code = operate(binary=str(home / "other"))
    assert code and report["evidence"]["configuration"] == "conflict"


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "directory", "fifo", "writable"])
def test_unsafe_config_targets(home, binary, kind):
    config = home / "config.json"
    target = home / "target"
    target.write_text("{}")
    if kind == "symlink":
        config.symlink_to(target)
    elif kind == "hardlink":
        os.link(target, config)
    elif kind == "directory":
        config.mkdir()
    elif kind == "fifo":
        os.mkfifo(config)
    else:
        config.write_text("{}")
        config.chmod(0o666)
    report, code = operate(binary=str(binary), config=str(config))
    assert code and not report["ok"]
    assert target.read_text() == "{}"


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "writable", "not-executable", "setuid"])
def test_unsafe_binary_targets(home, binary, kind):
    target = binary
    if kind == "symlink":
        target = home / "link"
        target.symlink_to(binary)
    elif kind == "hardlink":
        os.link(binary, home / "link")
    else:
        binary.chmod({"writable": 0o777, "not-executable": 0o600, "setuid": 0o4755}[kind])
    report, code = operate(binary=str(target))
    assert code and report["evidence"]["runnable"] == "not-tested"


@pytest.mark.parametrize("value", ["relative", "~/file", "/a/../file", "/a//file", "/a/\nfile"])
def test_invalid_paths(home, value):
    assert operate(config=value)[1] != 0
    assert operate(binary=value)[1] != 0


def test_symlinked_and_writable_parents(home, binary):
    target = home / "target"
    target.mkdir()
    link = home / "link"
    link.symlink_to(target)
    assert operate(binary=str(binary), config=str(link / "config"))[1]
    target.chmod(0o777)
    assert operate(binary=str(binary), config=str(target / "config"))[1]


def test_stale_plan_config_and_binary(home, binary, probes):
    config = write_config(home, {})
    plan, _ = operate(binary=str(binary))
    config.write_text('{"added": true}')
    report, code = operate("apply", binary=str(binary), yes=True, expected_plan=plan["planId"])
    assert code and not probes and json.loads(config.read_text()) == {"added": True}
    plan, _ = operate(binary=str(binary))
    binary.write_bytes(b"changed executable")
    assert operate("apply", binary=str(binary), yes=True, expected_plan=plan["planId"])[1]
    assert not probes


def test_changed_while_acquiring_lock(home, binary, probes):
    config = write_config(home, {})
    plan, _ = operate(binary=str(binary))

    @contextmanager
    def racing_lock():
        config.write_text('{"concurrent": true}')
        yield

    report, code = p.operate(args("apply", binary=str(binary), yes=True, expected_plan=plan["planId"]), racing_lock)
    assert code and not probes and json.loads(config.read_text()) == {"concurrent": True}


def test_changed_during_startup_no_clobber(home, binary, probes, monkeypatch):
    config = write_config(home, {})
    original = p.startup

    def racing_startup(binary, report):
        verified = original(binary, report)
        config.write_text('{"concurrent": true}')
        return verified

    monkeypatch.setattr(p, "startup", racing_startup)
    report, code = approve(binary=str(binary))
    assert code and json.loads(config.read_text()) == {"concurrent": True}
    assert not list(config.parent.glob(".peekaboo-*"))


def test_changed_at_final_write_no_clobber(home, binary, probes, monkeypatch):
    config = write_config(home, {})
    original = p.write_config

    def racing_write(config, document, selection):
        config.write_text('{"concurrent": true}')
        original(config, document, selection)

    monkeypatch.setattr(p, "write_config", racing_write)
    report, code = approve(binary=str(binary))
    assert code and json.loads(config.read_text()) == {"concurrent": True}
    assert not list(config.parent.glob(".peekaboo-*"))


@pytest.mark.parametrize("failure", ["signature", "crash", "4.6", "timeout", "cancel"])
@pytest.mark.parametrize("mode", ["direct", "bridge"])
def test_failed_startup_never_wires(home, binary, monkeypatch, failure, mode, bridge_socket):
    def run(command, timeout=15):
        if command[0] == "/usr/bin/codesign":
            return (1 if failure == "signature" else 0), b"", b"secret"
        if failure == "timeout":
            raise p.SetupError("Read-only probe timed out; no readiness is implied.")
        if failure == "cancel":
            raise KeyboardInterrupt
        return (1 if failure == "crash" else 0), b"Peekaboo 4.6.0", b"secret crash dump"

    monkeypatch.setattr(p, "run_bounded", run)
    options = {"mode": mode}
    if mode == "bridge":
        options["bridge_socket"] = str(bridge_socket)
    report, code = approve(binary=str(binary), **options)
    assert code and not report["ok"]
    assert "secret" not in json.dumps(report)
    assert not (home / ".config/mcp/mcp.json").exists()
    if failure in {"crash", "4.6", "timeout"}:
        assert report["evidence"]["runnable"] == "no"
    assert code == (130 if failure == "cancel" else 1)


def test_check_success_is_not_desktop_or_mcp_readiness(home, binary, probes):
    write_config(home, {"mcpServers": {"peekaboo": p.entry(binary)}})
    report, code = operate("check")
    assert code == 0, report
    assert set(report["evidence"]["permissions"].values()) == {"granted"}
    assert report["evidence"]["permissionSource"] == "local"
    assert report["evidence"]["mcp"] == report["evidence"]["desktop"] == "not-tested"
    assert report["evidence"]["toolCount"] is None
    assert "secret" not in json.dumps(report)
    assert probes[-1][1:] == ["permissions", "status", "--no-remote", "--json"]


@pytest.mark.parametrize("mode", ["denied", "bridge", "bad", "incomplete", "count-only"])
def test_permission_denial_or_bad_evidence_not_ready(home, binary, probes, monkeypatch, mode):
    write_config(home, {"mcpServers": {"peekaboo": p.entry(binary)}})
    original = p.run_bounded

    def run(command, timeout=15):
        if command[-1] != "--json":
            return original(command, timeout)
        outputs = {"denied": permission_output(False), "bridge": permission_output(source="bridge"),
                   "bad": b'{"error":"secret"}', "incomplete": b'{"success":true,"data":{"source":"local","permissions":[]}}',
                   "count-only": b'{"toolCount":26}'}
        return 0, outputs[mode], b"secret"

    monkeypatch.setattr(p, "run_bounded", run)
    report, code = operate("check")
    assert code and report["evidence"]["runnable"] == "yes"
    if mode == "denied":
        assert set(report["evidence"]["permissions"].values()) == {"denied"}
    assert report["evidence"]["desktop"] == "not-tested"
    assert report["evidence"]["mcp"] == "not-tested" and report["evidence"]["toolCount"] is None
    assert "secret" not in json.dumps(report)


def test_explicit_install_plan_pure_and_existing_refused(home, binary):
    plan, code = operate(install=True)
    assert not code and plan["evidence"]["binaryPath"] == str(p.managed() / "peekaboo")
    assert not p.managed().exists()
    assert "input-safety" in " ".join(plan["warnings"])
    assert operate(binary=str(binary), install=True)[1]


def test_install_intel_refused(home, monkeypatch):
    monkeypatch.setattr(p.platform, "machine", lambda: "x86_64")
    assert operate(install=True)[1]


def archive_bytes(members=None):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        sidecar = tarfile.TarInfo("._peekaboo-macos-arm64")
        sidecar.size = 163
        archive.addfile(sidecar, io.BytesIO(bytes(163)))
        root = tarfile.TarInfo("peekaboo-macos-arm64/")
        root.type = tarfile.DIRTYPE
        archive.addfile(root)
        for name, kind, data in members or [("peekaboo", "file", b"cli"),
                                            ("libswiftCompatibilitySpan.dylib", "file", b"lib")]:
            item = tarfile.TarInfo("peekaboo-macos-arm64/" + name)
            item.size = len(data)
            if kind == "symlink":
                item.type = tarfile.SYMTYPE
                item.linkname = "/outside"
            elif kind == "hardlink":
                item.type = tarfile.LNKTYPE
                item.linkname = "peekaboo"
            archive.addfile(item, io.BytesIO(data))
    return output.getvalue()


@pytest.mark.parametrize("member", [("../escape", "file", b"bad"), ("/escape", "file", b"bad"),
    ("peekaboo", "symlink", b""), ("peekaboo", "hardlink", b""), ("unexpected", "file", b"bad"),
    ("._peekaboo", "symlink", b""), ("._peekaboo", "file", bytes(4097))])
def test_safe_archive_extraction(home, member):
    archive = home / "archive.tgz"
    archive.write_bytes(archive_bytes([member]))
    target = home / "target"
    target.mkdir()
    with pytest.raises(p.SetupError):
        p.extract(archive, target)
    assert not list(target.iterdir())


def test_download_hash_size_and_rate(home, monkeypatch):
    raw = archive_bytes()

    class Response(io.BytesIO):
        status = 200

    class Opener:
        def open(self, url, timeout):
            assert url == p.URL and timeout == 15
            return Response(raw)

    monkeypatch.setattr(p.urllib.request, "build_opener", lambda *a: Opener())
    monkeypatch.setattr(p.time, "monotonic", lambda: 0)
    delays = []
    monkeypatch.setattr(p.time, "sleep", delays.append)
    with pytest.raises(p.SetupError, match="SHA256"):
        p.download(home / "bad.tgz")
    monkeypatch.setattr(p, "SHA256", hashlib.sha256(raw).hexdigest())
    p.download(home / "good.tgz")
    assert (home / "good.tgz").read_bytes() == raw
    assert sum(delays) >= 2 * len(raw) / 25_000_000
    monkeypatch.setattr(p, "MAX_ARCHIVE", 1)
    with pytest.raises(p.SetupError, match="size"):
        p.download(home / "large.tgz")


def test_download_has_hard_deadline_and_no_raw_child_output(home, monkeypatch):
    def run(command, timeout, label):
        assert timeout == 135 and label == "Release download"
        assert command[:4] == [sys.executable, "-I", "-B", "-c"]
        assert command[-1] == str(home / "archive.tgz")
        return 1, b"secret", b"private-url"

    monkeypatch.setattr(p, "run_bounded", run)
    with pytest.raises(p.SetupError, match="download/integrity") as error:
        p.download_bounded(home / "archive.tgz")
    assert "secret" not in str(error.value) and "private-url" not in str(error.value)


@pytest.mark.parametrize("url", ["http://github.com/file", "https://evil.invalid/file",
    "https://github.com.evil.invalid/file", "https://user:pass@github.com/file", "https://github.com:8443/file"])
def test_redirects_restricted(url):
    with pytest.raises(p.SetupError):
        p.OfficialRedirect().redirect_request(urllib.request.Request(p.URL), None, 302, "", {}, url)


def test_official_asset_redirect_allowed():
    url = "https://release-assets.githubusercontent.com/path?sig=not-printed"
    request = p.OfficialRedirect().redirect_request(urllib.request.Request(p.URL), None, 302, "", {}, url)
    assert request.full_url == url


def test_install_is_private_and_does_not_write_module_receipts(home, probes, monkeypatch):
    monkeypatch.setattr(p, "download_bounded", lambda path: path.write_bytes(archive_bytes()))
    report, code = approve(install=True)
    assert code == 0, report
    assert (p.managed() / "peekaboo").read_bytes() == b"cli"
    config = home / ".config/mcp/mcp.json"
    assert json.loads(config.read_text())["mcpServers"]["peekaboo"] == p.entry(p.managed() / "peekaboo")
    assert not (home / ".config/pi-shared/setup.json").exists()
    assert not list(p.managed().parent.glob(".peekaboo-*"))
    assert not (home / ".local/bin").exists()


@pytest.mark.parametrize("failure", ["hash", "signature", "crash", "cancel"])
def test_install_failure_never_wires_or_leaves_staging(home, probes, monkeypatch, failure):
    def download(path):
        if failure == "hash":
            raise p.SetupError("Release archive SHA256 mismatch; nothing was installed.")
        path.write_bytes(archive_bytes())

    def startup(binary, report):
        if failure == "cancel":
            raise KeyboardInterrupt
        raise p.SetupError("Signature or startup failed.")

    monkeypatch.setattr(p, "download_bounded", download)
    monkeypatch.setattr(p, "startup", startup)
    report, code = approve(install=True)
    assert code and not (home / ".config/mcp/mcp.json").exists()
    assert not p.managed().exists()
    assert not list(p.managed().parent.glob(".peekaboo-*"))


def test_install_no_clobber_when_destination_appears(home, probes, monkeypatch):
    def download(path):
        path.write_bytes(archive_bytes())
        p.managed().mkdir()
        (p.managed() / "keep").write_text("concurrent")

    monkeypatch.setattr(p, "download_bounded", download)
    report, code = approve(install=True)
    assert code and (p.managed() / "keep").read_text() == "concurrent"
    assert not (home / ".config/mcp/mcp.json").exists()


def test_partial_install_reports_present_binary_without_wiring(home, probes, monkeypatch):
    monkeypatch.setattr(p, "download_bounded", lambda path: path.write_bytes(archive_bytes()))
    original = p.os.rename
    count = 0

    def partial(*a, **kw):
        nonlocal count
        count += 1
        if count == 2:
            raise OSError("synthetic failure")
        original(*a, **kw)

    monkeypatch.setattr(p.os, "rename", partial)
    report, code = approve(install=True)
    assert code and p.managed().exists()
    assert report["evidence"]["binaryPresent"] == (p.managed() / "peekaboo").exists()
    assert any("partial install" in warning for warning in report["warnings"])
    assert not (home / ".config/mcp/mcp.json").exists()
    assert not list(p.managed().parent.glob(".peekaboo-*"))


def test_lock_uses_existing_cli_shared_lock(home, binary, monkeypatch):
    import runpy
    cli = runpy.run_path(str(ROOT / "bin/pi-shared"))
    monkeypatch.delenv("PI_SHARED_UPDATE_LOCK_FD", raising=False)
    with cli["update_lock"]():
        plan, code = operate(binary=str(binary))
        assert not code
        report, code = p.operate(args("apply", binary=str(binary), yes=True,
                                      expected_plan=plan["planId"]), cli["update_lock"])
    assert code and not report["ok"]
    assert not (home / ".config/mcp/mcp.json").exists()


def test_subprocess_timeout_and_output_bound(home, monkeypatch):
    with pytest.raises(p.SetupError, match="timed out"):
        p.run_bounded([sys.executable, "-c", "import time; time.sleep(10)"], timeout=0.05)
    monkeypatch.setattr(p, "MAX_CONFIG", 64)
    with pytest.raises(p.SetupError, match="output limit"):
        p.run_bounded([sys.executable, "-c", "print('x'*10000)"])


@pytest.mark.parametrize("native_only", [False, True])
def test_probe_environment_does_not_inherit_secrets(home, monkeypatch, native_only):
    monkeypatch.setenv("PRIVATE_KEY", "secret")
    monkeypatch.setenv("DYLD_LIBRARY_PATH", "secret")
    monkeypatch.setenv("PEEKABOO_DISABLE_TOOLS", "all")
    original = p.subprocess.Popen
    captured = []

    def spawn(*args, **kwargs):
        captured.append(kwargs["env"])
        return original(*args, **kwargs)

    monkeypatch.setattr(p.subprocess, "Popen", spawn)
    code, out, _ = p.run_bounded([sys.executable, "-c", "import os,json; print(json.dumps(dict(os.environ)))"], native_only=native_only)
    assert code == 0 and b"secret" not in out and b"DYLD" not in out
    expected = {"HOME": str(home), "PATH": "/usr/bin:/bin", "LANG": "en_US.UTF-8"}
    if native_only:
        expected["PEEKABOO_DISABLE_TOOLS"] = "browser"
    assert captured == [expected]


@pytest.mark.parametrize("argv", [["plan", "--unexpected", "secret", "--json"],
    ["apply", "--json"], ["check", "--install", "--json"], ["plan", "--yes", "--json"],
    ["plan", "--mode", "secret", "--json"], ["plan", "--mode", "bridge", "--json"],
    ["plan", "--bridge-socket", "--json"],
    ["plan", "--mode", "direct", "--bridge-socket", "/missing.sock", "--json"]])
def test_structured_nonzero_and_no_argument_leak(home, capsys, argv):
    assert p.main(argv, lock) != 0
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert not data["ok"] and data["errors"]
    assert data["schemaVersion"] == 2
    assert {"mode", "bridgeSocketPath", "bridgeSocketState", "permissionSource"} <= data["evidence"].keys()
    assert "secret" not in captured.out and captured.err == ""


@pytest.mark.parametrize("action", ["plan", "status"])
@pytest.mark.parametrize("mode", ["direct", "bridge"])
def test_offline_plan_audit_forbids_execution_network_and_writes(home, binary, action, mode, bridge_socket):
    write_config(home, {"mcpServers": {"peekaboo": p.entry(binary, mode, bridge_socket)}})
    launcher = '''
import os, runpy, sys
home = os.environ["HOME"] + os.sep
def guard(event, args):
    if event.startswith("socket.") or event in ("subprocess.Popen", "os.system", "os.exec", "os.posix_spawn",
        "os.mkdir", "os.rename", "os.remove", "os.chmod", "os.link", "os.symlink"):
        raise AssertionError("offline side effect: " + event)
    if event == "open" and isinstance(args[0], (str, bytes)) and os.path.abspath(os.fsdecode(args[0])).startswith(home):
        if args[2] & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC):
            raise AssertionError("offline write")
sys.addaudithook(guard)
sys.argv = sys.argv[1:]
runpy.run_path(sys.argv[0], run_name="__main__")
'''
    run = subprocess.run([sys.executable, "-B", "-c", launcher, str(ROOT / "bin/pi-shared"),
                          "peekaboo", action, "--json"],
                         env={"HOME": str(home), "PATH": os.environ["PATH"]},
                         capture_output=True, text=True, timeout=15)
    assert run.returncode == 0, run.stderr
    assert json.loads(run.stdout)["ok"]


@pytest.mark.parametrize("argv", [["peekaboo", "-h"], ["peekaboo", "plan", "--help"],
    ["peekaboo", "status", "--json"], ["peekaboo", "apply", "--json"]])
def test_cli_help_and_json_in_empty_home(home, argv):
    run = subprocess.run([sys.executable, "-B", str(ROOT / "bin/pi-shared"), *argv],
                         env={"HOME": str(home), "PATH": os.environ["PATH"]},
                         capture_output=True, text=True, timeout=15)
    if "--json" in argv:
        data = json.loads(run.stdout)
        assert data["ok"] == (run.returncode == 0)
        assert data["schemaVersion"] == 2
    else:
        assert run.returncode == 0 and "--expected-plan" in run.stdout
        assert "--mode" in run.stdout and "--bridge-socket" in run.stdout
    assert run.stderr == "" and list(home.iterdir()) == []


@pytest.mark.parametrize("mode", ["direct", "bridge"])
def test_exact_route_preserved_and_idempotent(home, binary, probes, mode, bridge_socket):
    server = p.entry(binary, mode, bridge_socket)
    config = write_config(home, {"settings": {"secret": "kept"}, "mcpServers": {"peekaboo": server}})
    before = config.read_bytes(), config.stat()
    for options in ({}, {"mode": mode}):
        report, code = approve(**options)
        assert code == 0, report
        evidence = report["evidence"]
        assert evidence["mode"] == mode
        assert evidence["bridgeSocketPath"] == (str(bridge_socket) if mode == "bridge" else None)
        assert evidence["bridgeSocketState"] == ("present" if mode == "bridge" else "not-applicable")
        assert evidence["permissionSource"] is None
        assert config.read_bytes() == before[0]
        assert config.stat().st_ino == before[1].st_ino
        assert config.stat().st_mtime_ns == before[1].st_mtime_ns
        assert "secret" not in json.dumps(report)
    assert all("permissions" not in call and "mcp" not in call for call in probes)


def test_new_bridge_entry_is_native_only(home, binary, probes, bridge_socket):
    original = {"other": {"url": "https://example.invalid", "env": {"TOKEN": "secret"}}}
    config = write_config(home, {"mcpServers": original, "setting": True})
    report, code = approve(binary=str(binary), mode="bridge", bridge_socket=str(bridge_socket))
    assert code == 0, report
    expected = {"command": str(binary), "args": ["mcp", "--bridge-socket", str(bridge_socket), "--allow-foreground"],
                "env": {"PEEKABOO_DISABLE_TOOLS": "browser"}, "lifecycle": "lazy-keep-alive",
                "requestTimeoutMs": 30000, "directTools": False}
    assert json.loads(config.read_text()) == {"mcpServers": original | {"peekaboo": expected}, "setting": True}
    assert "secret" not in json.dumps(report)
    assert report["evidence"]["toolCount"] is None
    assert any("browser tools disabled" in action for action in report["actions"])


@pytest.mark.parametrize("mode", [None, "direct"])
def test_absent_entry_defaults_direct(home, binary, mode):
    report, code = operate(binary=str(binary), mode=mode)
    assert code == 0 and report["evidence"]["mode"] == "direct"
    assert report["evidence"]["bridgeSocketPath"] is None
    assert report["evidence"]["bridgeSocketState"] == "not-applicable"


@pytest.mark.parametrize("options", [
    {"mode": "bridge"}, {"mode": "direct", "bridge_socket": "/missing.sock"},
    {"bridge_socket": "/missing.sock"}, {"mode": "bridge", "bridge_socket": "relative"},
    {"mode": "bridge", "bridge_socket": "~/bridge.sock"},
    {"mode": "bridge", "bridge_socket": "/a/../bridge.sock"},
    {"mode": "bridge", "bridge_socket": "/a//bridge.sock"},
    {"mode": "bridge", "bridge_socket": "/a/\nbridge.sock"},
    {"mode": "bridge", "bridge_socket": "/a/bridge.sock/"}])
def test_route_requires_explicit_safe_selection(home, binary, probes, options):
    report, code = operate(binary=str(binary), **options)
    assert code and not report["ok"] and not probes
    assert not (home / ".config").exists()
    evidence = report["evidence"]
    assert evidence["bridgeSocketPath"] is None
    assert evidence["bridgeSocketState"] == ("invalid" if evidence["mode"] == "bridge" else "not-applicable")


@pytest.mark.parametrize("existing_mode,selected_mode", [("direct", "bridge"), ("bridge", "direct")])
def test_no_automatic_route_migration(home, binary, probes, bridge_socket, existing_mode, selected_mode):
    config = write_config(home, {"mcpServers": {"peekaboo": p.entry(binary, existing_mode, bridge_socket)}})
    before = config.read_bytes()
    options = {"mode": selected_mode}
    if selected_mode == "bridge":
        options["bridge_socket"] = str(bridge_socket)
    report, code = operate(**options)
    assert code and report["evidence"]["configuration"] == "conflict"
    assert config.read_bytes() == before and not probes


@pytest.mark.parametrize("change", [
    {"env": {}}, {"env": {"PEEKABOO_DISABLE_TOOLS": "browser,agent"}},
    {"env": {"PEEKABOO_DISABLE_TOOLS": "browser", "TOKEN": "secret"}},
    {"includeTools": []}, {"excludeTools": ["browser"]}, {"approveTools": []},
    {"requestTimeoutMs": 30000.0}, {"directTools": 0}, {"lifecycle": "eager"},
    {"args": ["mcp", "--bridge-socket", "relative", "--allow-foreground"]}])
def test_bridge_conflicting_shape_not_adopted(home, binary, bridge_socket, change):
    config = write_config(home, {"mcpServers": {"peekaboo": p.entry(binary, "bridge", bridge_socket) | change}})
    before = config.read_bytes()
    report, code = operate()
    assert code and report["evidence"]["configuration"] == "conflict"
    assert config.read_bytes() == before and "secret" not in json.dumps(report)


def test_bridge_socket_override_conflict(home, binary, bridge_socket):
    config = write_config(home, {"mcpServers": {"peekaboo": p.entry(binary, "bridge", bridge_socket)}})
    before = config.read_bytes()
    report, code = operate(bridge_socket=str(home / "another.sock"))
    assert code and report["evidence"]["configuration"] == "conflict"
    assert report["evidence"]["mode"] == "bridge"
    assert report["evidence"]["bridgeSocketState"] == "invalid"
    assert config.read_bytes() == before
    report, code = operate(bridge_socket=str(bridge_socket))
    assert code == 0 and report["evidence"]["mode"] == "bridge"


@pytest.mark.parametrize("parent_missing", [False, True])
def test_missing_socket_can_be_planned_and_wired_but_not_checked(home, binary, probes, parent_missing):
    path = home / "missing" / "bridge.sock" if parent_missing else home / "bridge.sock"
    options = {"mode": "bridge", "bridge_socket": str(path), "binary": str(binary)}
    report, code = approve(**options)
    assert code == 0, report
    assert report["evidence"]["bridgeSocketState"] == "missing"
    assert report["evidence"]["permissionSource"] is None
    assert any("Start the separately installed" in step for step in report["nextSteps"])
    assert not path.exists()
    before = list(probes)
    report, code = operate("check")
    assert code and probes == before  # Not even a startup probe when Bridge is missing.
    assert report["evidence"]["runnable"] == "not-tested"
    assert any("start the desktop app" in error for error in report["errors"])


@pytest.mark.parametrize("kind", ["file", "directory", "fifo", "symlink", "dangling", "hardlink", "writable", "parent-link", "parent-writable"])
def test_unsafe_socket_rejected_offline(home, binary, bridge_socket, probes, kind):
    target = home / "unsafe.sock"
    if kind == "file":
        target.write_text("not a socket")
    elif kind == "directory":
        target.mkdir()
    elif kind == "fifo":
        os.mkfifo(target)
    elif kind in {"symlink", "dangling"}:
        target.symlink_to(bridge_socket if kind == "symlink" else home / "absent")
    elif kind == "hardlink":
        os.link(bridge_socket, target)
    elif kind == "writable":
        target = bridge_socket
        target.chmod(0o666)
    elif kind == "parent-link":
        target.symlink_to(bridge_socket.parent, target_is_directory=True)
        target = target / bridge_socket.name
    else:
        target.mkdir(mode=0o777)
        target.chmod(0o777)
        target = target / "absent.sock"
    report, code = operate(binary=str(binary), mode="bridge", bridge_socket=str(target))
    assert code and not probes
    assert report["evidence"]["bridgeSocketState"] == "invalid"
    assert report["evidence"]["permissionSource"] is None


def test_foreign_owned_socket_rejected(home, bridge_socket, monkeypatch):
    original = p.os.stat

    def foreign(*args, **kwargs):
        info = original(*args, **kwargs)
        if stat.S_ISSOCK(info.st_mode):
            return SimpleNamespace(st_mode=info.st_mode, st_uid=os.getuid() + 1, st_nlink=1)
        return info

    monkeypatch.setattr(p.os, "stat", foreign)
    report, code = operate(mode="bridge", bridge_socket=str(bridge_socket))
    assert code and report["evidence"]["bridgeSocketState"] == "invalid"


def test_route_and_socket_participate_in_approval(home, binary, probes):
    direct, _ = operate(binary=str(binary))
    options = {"binary": str(binary), "mode": "bridge", "bridge_socket": str(home / "one.sock")}
    bridge, _ = operate(**options)
    other, _ = operate(**(options | {"bridge_socket": str(home / "two.sock")}))
    assert len({direct["planId"], bridge["planId"], other["planId"]}) == 3
    for approved in (direct, other):
        report, code = operate("apply", yes=True, expected_plan=approved["planId"], **options)
        assert code and not probes
    assert not (home / ".config").exists()


@pytest.mark.parametrize("timing", ["before-apply", "lock", "startup"])
def test_socket_races_invalidate_approval(home, binary, probes, bridge_socket, monkeypatch, timing):
    options = {"binary": str(binary), "mode": "bridge", "bridge_socket": str(bridge_socket)}
    plan, code = operate(**options)
    assert code == 0
    original_startup = p.startup

    def startup(binary, report):
        verified = original_startup(binary, report)
        bridge_socket.unlink()
        return verified

    @contextmanager
    def racing_lock():
        bridge_socket.unlink()
        yield

    if timing == "before-apply":
        bridge_socket.unlink()
    if timing == "startup":
        monkeypatch.setattr(p, "startup", startup)
    report, code = p.operate(args("apply", yes=True, expected_plan=plan["planId"], **options),
                             racing_lock if timing == "lock" else lock)
    assert code and not (home / ".config/mcp/mcp.json").exists()
    assert report["evidence"]["bridgeSocketState"] == "missing"
    assert any("Start the separately installed" in step for step in report["nextSteps"])
    if timing != "startup":
        assert not probes


@pytest.mark.parametrize("timing", ["before-apply", "lock", "startup", "final-write"])
def test_bridge_config_races_preserve_concurrent_editor(home, binary, probes, bridge_socket, monkeypatch, timing):
    config = write_config(home, {"setting": True})
    options = {"binary": str(binary), "mode": "bridge", "bridge_socket": str(bridge_socket)}
    plan, _ = operate(**options)
    changed = b'{"concurrent":true}'
    original_startup, original_write = p.startup, p.write_config

    @contextmanager
    def racing_lock():
        config.write_bytes(changed)
        yield

    def startup(binary, report):
        verified = original_startup(binary, report)
        config.write_bytes(changed)
        return verified

    def write(config, document, selection):
        config.write_bytes(changed)
        original_write(config, document, selection)

    if timing == "before-apply":
        config.write_bytes(changed)
    elif timing == "startup":
        monkeypatch.setattr(p, "startup", startup)
    elif timing == "final-write":
        monkeypatch.setattr(p, "write_config", write)
    report, code = p.operate(args("apply", yes=True, expected_plan=plan["planId"], **options),
                             racing_lock if timing == "lock" else lock)
    assert code and config.read_bytes() == changed
    assert not list(config.parent.glob(".peekaboo-*"))


@pytest.mark.parametrize("action", ["plan", "status"])
def test_cli_bridge_selection_and_missing_socket_guidance(home, binary, action):
    path = home / "absent.sock"
    run = subprocess.run([sys.executable, "-B", str(ROOT / "bin/pi-shared"), "peekaboo", action,
                          "--binary", str(binary), "--mode", "bridge", "--bridge-socket", str(path), "--json"],
                         env={"HOME": str(home), "PATH": os.environ["PATH"]},
                         capture_output=True, text=True, timeout=15)
    assert run.returncode == 0 and run.stderr == ""
    report = json.loads(run.stdout)
    assert report["schemaVersion"] == 2
    assert report["evidence"]["mode"] == "bridge"
    assert report["evidence"]["bridgeSocketPath"] == str(path)
    assert report["evidence"]["bridgeSocketState"] == "missing"
    assert report["evidence"]["permissionSource"] is None
    assert report["nextSteps"] and list(home.iterdir()) == [binary]


def test_bridge_socket_changed_during_check_blocks_permission_probe(home, binary, probes, bridge_socket, monkeypatch):
    write_config(home, {"mcpServers": {"peekaboo": p.entry(binary, "bridge", bridge_socket)}})
    original = p.startup

    def startup(binary, report):
        original(binary, report)
        bridge_socket.unlink()

    monkeypatch.setattr(p, "startup", startup)
    report, code = operate("check")
    assert code and report["evidence"]["bridgeSocketState"] == "missing"
    assert report["evidence"]["permissionSource"] is None
    assert all("permissions" not in call for call in probes)


def test_bridge_check_routes_exactly_without_readiness_claims(home, binary, probes, bridge_socket):
    write_config(home, {"mcpServers": {"peekaboo": p.entry(binary, "bridge", bridge_socket)}})
    report, code = operate("check")
    assert code == 0, report
    evidence = report["evidence"]
    assert evidence["mode"] == evidence["permissionSource"] == "bridge"
    assert evidence["bridgeSocketState"] == "present"
    assert set(evidence["permissions"].values()) == {"granted"}
    assert evidence["toolCount"] is None
    assert evidence["mcp"] == evidence["desktop"] == "not-tested"
    assert probes[-1] == [str(binary), "permissions", "status", "--bridge-socket", str(bridge_socket), "--json"]
    assert all("mcp" not in call and "request" not in call for call in probes)
    assert any("App availability is unverified" in warning for warning in report["warnings"])


@pytest.mark.parametrize("failure", ["local", "missing-source", "denied", "incomplete", "duplicate", "bad-boolean", "nonzero", "crash", "timeout", "cancel"])
def test_bridge_permission_failures_do_not_claim_ready(home, binary, probes, bridge_socket, monkeypatch, failure):
    write_config(home, {"mcpServers": {"peekaboo": p.entry(binary, "bridge", bridge_socket)}})
    original = p.run_bounded

    def run(command, timeout=15, *, native_only=False):
        if command[-1] != "--json":
            return original(command, timeout)
        assert native_only is True
        if failure == "timeout":
            raise p.SetupError("Read-only probe timed out; no readiness is implied.")
        if failure == "cancel":
            raise KeyboardInterrupt
        if failure == "crash":
            return -6, b"secret crash", b"secret"
        output = json.loads(permission_output(granted=failure != "denied", source="bridge"))
        if failure == "local":
            output["data"]["source"] = "local"
        elif failure == "missing-source":
            del output["data"]["source"]
        elif failure == "incomplete":
            output["data"]["permissions"].pop()
        elif failure == "duplicate":
            output["data"]["permissions"].append(output["data"]["permissions"][0])
        elif failure == "bad-boolean":
            output["data"]["permissions"][0]["isGranted"] = 1
        return int(failure == "nonzero"), json.dumps(output).encode(), b"secret"

    monkeypatch.setattr(p, "run_bounded", run)
    report, code = operate("check")
    assert code == (130 if failure == "cancel" else 1)
    evidence = report["evidence"]
    assert evidence["permissionSource"] == ("bridge" if failure == "denied" else None)
    assert set(evidence["permissions"].values()) == ({"denied"} if failure == "denied" else {"unknown"})
    assert evidence["mcp"] == evidence["desktop"] == "not-tested" and evidence["toolCount"] is None
    assert "secret" not in json.dumps(report)
    if failure == "denied":
        assert any("Bridge permission owner" in step for step in report["nextSteps"])


def test_bridge_install_retains_official_cli_pin(home, probes, monkeypatch):
    monkeypatch.setattr(p, "download_bounded", lambda path: path.write_bytes(archive_bytes()))
    path = home / "absent.sock"
    report, code = approve(install=True, mode="bridge", bridge_socket=str(path))
    assert code == 0, report
    configured = json.loads((home / ".config/mcp/mcp.json").read_text())["mcpServers"]["peekaboo"]
    assert configured == p.entry(p.managed() / "peekaboo", "bridge", path)
    assert p.VERSION == "4.5.0" and p.TEAM == "FWJYW4S8P8"
    assert not list(p.managed().parent.glob(".peekaboo-*"))
    assert not (home / ".config/pi-shared/setup.json").exists()


def test_sigterm_unwinds_probe_process_group(home, binary):
    import signal
    import time
    pid_file = home / "probe.pid"
    sleeper = "import os,time; from pathlib import Path; Path(" + repr(str(pid_file)) + ").write_text(str(os.getpid())); time.sleep(60)"
    script = f"""
import importlib.util,sys
sys.path.insert(0, {str(ROOT / 'lib')!r})
from contextlib import nullcontext
spec=importlib.util.spec_from_file_location('p', {str(ROOT / 'lib/peekaboo_setup.py')!r})
p=importlib.util.module_from_spec(spec);spec.loader.exec_module(p)
p.platform.system=lambda:'Darwin'
p.startup=lambda *_:p.run_bounded([sys.executable,'-c',{sleeper!r}])
sys.exit(p.main(['check','--json','--binary',{str(binary)!r}],nullcontext))
"""
    process = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 5
        while not pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert pid_file.exists()
        child_pid = int(pid_file.read_text())
        process.send_signal(signal.SIGTERM)
        out, _ = process.communicate(timeout=5)
        assert process.returncode == 130
        assert json.loads(out)["ok"] is False
        with pytest.raises(ProcessLookupError):
            os.kill(child_pid, 0)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        if pid_file.exists():
            try:
                os.kill(int(pid_file.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass
