"""Standalone contract/security tests; never execute Peekaboo, download it or touch TCC."""
from contextlib import contextmanager
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
from types import SimpleNamespace
import urllib.request

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("peekaboo_setup", ROOT / "lib/peekaboo_setup.py")
p = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p)


@pytest.fixture
def home(tmp_path, monkeypatch):
    tmp_path = tmp_path.resolve()
    monkeypatch.setenv("HOME", str(tmp_path))
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
                              "install": False, "yes": False, "expected_plan": None} | kwargs))


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

    def run(command, timeout=15):
        calls.append([str(x) for x in command])
        if command[0] == "/usr/bin/codesign":
            assert command[command.index("-R") + 1].startswith("=anchor apple generic"), "codesign needs '=' for an inline requirement, otherwise it opens a file"
            return 0, b"", b""
        if command[-1] == "--version":
            return 0, b"Peekaboo 4.5.0 (main/abc, built: fixture)\n", b""
        assert command[1:] == ["permissions", "status", "--no-remote", "--json"]
        return 0, permission_output(), b""

    monkeypatch.setattr(p, "run_bounded", run)
    return calls


def permission_output(granted=True, source="local", **extra):
    return json.dumps({"success": True, "data": {"source": source, "permissions": [
        {"name": name, "isGranted": granted, "grantInstructions": "secret must not be printed"}
        for name in ["Screen Recording", "Accessibility", "Event Synthesizing"]]}, **extra}).encode()


def test_contract_offline_missing(home, monkeypatch):
    def forbidden(*a, **kw):
        pytest.fail("offline operation attempted a side effect")

    monkeypatch.setattr(p.subprocess, "Popen", forbidden)
    monkeypatch.setattr(p.urllib.request, "build_opener", forbidden)
    monkeypatch.setattr(p, "write_config", forbidden)
    for action in ["plan", "status"]:
        report, code = p.operate(args(action), forbidden)
        assert code == 0 and report["ok"]
        assert report["schemaVersion"] == 1 and report["component"] == "peekaboo"
        assert report["action"] == action
        assert len(report["planId"]) == 64
        assert report["evidence"] == {
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
        p.write_config(config, {"unrelated": float("inf")}, {"binary": str(binary), "configStamp": None})
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
def test_failed_startup_never_wires(home, binary, monkeypatch, failure):
    def run(command, timeout=15):
        if command[0] == "/usr/bin/codesign":
            return (1 if failure == "signature" else 0), b"", b"secret"
        if failure == "timeout":
            raise p.SetupError("Read-only probe timed out; no readiness is implied.")
        if failure == "cancel":
            raise KeyboardInterrupt
        return (1 if failure == "crash" else 0), b"Peekaboo 4.6.0", b"secret crash dump"

    monkeypatch.setattr(p, "run_bounded", run)
    report, code = approve(binary=str(binary))
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


def test_probe_environment_does_not_inherit_secrets(home, monkeypatch):
    monkeypatch.setenv("PRIVATE_KEY", "secret")
    monkeypatch.setenv("DYLD_LIBRARY_PATH", "secret")
    code, out, _ = p.run_bounded([sys.executable, "-c", "import os,json; print(json.dumps(dict(os.environ)))"])
    assert code == 0 and b"secret" not in out and b"DYLD" not in out


@pytest.mark.parametrize("argv", [["plan", "--unexpected", "secret", "--json"],
    ["apply", "--json"], ["check", "--install", "--json"], ["plan", "--yes", "--json"]])
def test_structured_nonzero_and_no_argument_leak(home, capsys, argv):
    assert p.main(argv, lock) != 0
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert not data["ok"] and data["errors"]
    assert "secret" not in captured.out and captured.err == ""


@pytest.mark.parametrize("action", ["plan", "status"])
def test_offline_plan_audit_forbids_execution_network_and_writes(home, binary, action):
    write_config(home, {"mcpServers": {"peekaboo": p.entry(binary)}})
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
    else:
        assert run.returncode == 0 and "--expected-plan" in run.stdout
    assert run.stderr == "" and list(home.iterdir()) == []


def test_sigterm_unwinds_probe_process_group(home, binary):
    import signal
    import time
    pid_file = home / "probe.pid"
    sleeper = "import os,time; from pathlib import Path; Path(" + repr(str(pid_file)) + ").write_text(str(os.getpid())); time.sleep(60)"
    script = f"""
import importlib.util,sys
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
