"""Standalone, opt-in Peekaboo CLI wiring. No module or service ownership."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import http.client
import json
import math
import os
from pathlib import Path, PurePosixPath
import platform
import re
import selectors
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.parse
import urllib.request
import uuid

VERSION = "4.5.0"
URL = "https://github.com/openclaw/Peekaboo/releases/download/v4.5.0/peekaboo-macos-arm64.tar.gz"
SHA256 = "a65323e5c79a0094c860199c86f99248c60955d9803ac2fa7a62b18494186beb"
TEAM = "FWJYW4S8P8"
IDENTIFIER = "boo.peekaboo.peekaboo"
MAX_CONFIG = 1024 * 1024
MAX_ARCHIVE = 150 * 1024 * 1024
MAX_EXPANDED = 300 * 1024 * 1024
RATE = 25_000_000  # 200 Mbps, one download at a time under the shared lock.
WARNING = ("Compatibility pin 4.5.0 lacks newer input-safety fixes (including modifier cleanup). "
           "4.6.0 has a known CLI startup failure; no automatic upgrade/downgrade is used.")
HOST_WARNING = ("Direct permission evidence belongs to this setup process, not the later Pi host; "
                "Bridge evidence must identify the Bridge source. App availability is unverified: "
                "socket presence does not prove an app is installed or listening. "
                "MCP and desktop readiness are not tested; no permissions are requested or granted.")


class SetupError(RuntimeError):
    """Only fixed, safe diagnostics are surfaced; never echo child output/config values."""


def result(action):
    return {"schemaVersion": 2, "component": "peekaboo", "action": action, "ok": False,
            "summary": "Peekaboo operation not completed.", "actions": [], "warnings": [WARNING, HOST_WARNING],
            "errors": [], "nextSteps": [], "evidence": {
                "mode": "direct", "bridgeSocketPath": None,
                "bridgeSocketState": "not-applicable", "permissionSource": None,
                "binaryPath": None, "binaryPresent": False, "configuration": "missing",
                "runnable": "not-tested", "permissions": {"screenRecording": "unknown",
                    "accessibility": "unknown", "eventSynthesizing": "unknown"},
                "mcp": "not-tested", "toolCount": None, "desktop": "not-tested"}}


def path_value(value):
    if (not isinstance(value, str) or not value.startswith("/") or
            any(ord(c) < 32 or ord(c) == 127 for c in value) or
            ".." in value.split("/") or str(Path(value)) != value):
        raise SetupError("Use a normalized absolute path without control characters or parent traversal.")
    return Path(value)


def _directory_info(info):
    # Root-owned system ancestors are fine. A sticky shared temp ancestor is safe
    # when subsequent directories are owned and not shared-writable.
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid not in {0, os.getuid()} or
            (info.st_mode & 0o022 and not (info.st_uid == 0 and info.st_mode & stat.S_ISVTX))):
        raise SetupError("Unsafe directory ownership or permissions.")


@contextmanager
def directory(path, create=False):
    """Walk from / using pinned directory descriptors, never following symlinks."""
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            if create:
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
            _directory_info(os.fstat(fd))
        yield fd
    finally:
        os.close(fd)


def stamp(info):
    return [info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_nlink,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns]


def read_at(fd, name, limit, executable=False):
    try:
        file_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    except FileNotFoundError:
        return None, None
    try:
        info = os.fstat(file_fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or
                info.st_uid not in ({0, os.getuid()} if executable else {os.getuid()}) or
                info.st_mode & 0o022 or info.st_size > limit or
                (executable and (not info.st_mode & 0o111 or info.st_mode & 0o6000))):
            raise SetupError("Unsafe file type, ownership, links, size, or permissions.")
        chunks, size = [], 0
        while True:
            block = os.read(file_fd, min(65536, limit + 1 - size))
            if not block:
                break
            chunks.append(block)
            size += len(block)
            if size > limit:
                raise SetupError("File exceeds the size limit.")
        if stamp(os.fstat(file_fd)) != stamp(info):
            raise SetupError("File changed while being inspected; create a fresh plan.")
        data = b"".join(chunks)
        return data, {"stat": stamp(info), "sha256": hashlib.sha256(data).hexdigest()}
    finally:
        os.close(file_fd)


def read_file(path, limit, executable=False):
    try:
        with directory(path.parent) as fd:
            return read_at(fd, path.name, limit, executable)
    except FileNotFoundError:
        return None, None


def _pairs(pairs):
    data = {}
    for key, value in pairs:
        if key in data:
            raise SetupError("Duplicate JSON keys are not supported.")
        data[key] = value
    return data


def parse_json(raw):
    def invalid(_):
        raise SetupError("Non-finite JSON values are not supported.")
    def finite_float(value):
        number = float(value)
        if not math.isfinite(number):
            raise SetupError("Non-finite JSON values are not supported.")
        return number
    try:
        return json.loads(raw, object_pairs_hook=_pairs, parse_constant=invalid, parse_float=finite_float)
    except (ValueError, UnicodeError, RecursionError):
        raise SetupError("Invalid JSON; no configuration was changed.") from None


def entry(binary, mode="direct", bridge_socket=None):
    route = ["--bridge-socket", str(bridge_socket)] if mode == "bridge" else ["--no-remote"]
    value = {"command": str(binary), "args": ["mcp", *route, "--allow-foreground"],
             "lifecycle": "lazy-keep-alive", "requestTimeoutMs": 30000, "directTools": False}
    if mode == "bridge":
        value["env"] = {"PEEKABOO_DISABLE_TOOLS": "browser"}
    return value


def socket_snapshot(path, report):
    """Inspect only filesystem metadata; never connect to or open the socket."""
    report["evidence"]["bridgeSocketState"] = "invalid"
    try:
        with directory(path.parent) as fd:
            info = os.stat(path.name, dir_fd=fd, follow_symlinks=False)
    except FileNotFoundError:
        report["evidence"]["bridgeSocketState"] = "missing"
        guidance = "Start the separately installed Peekaboo desktop app yourself with its Bridge enabled at the selected socket, then recheck. Setup never installs or launches the app."
        if guidance not in report["nextSteps"]:
            report["nextSteps"].append(guidance)
        return None
    if (not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or
            info.st_nlink != 1 or info.st_mode & 0o022):
        raise SetupError("Bridge socket has unsafe type, ownership, links, or permissions.")
    report["evidence"]["bridgeSocketState"] = "present"
    return stamp(info)


def select_route(args, existing, report):
    """Recognize only exact owned shapes; never migrate or normalize an entry."""
    mode, socket = args.mode, None
    if args.bridge_socket is not None:
        if mode == "direct":
            raise SetupError("--bridge-socket requires Bridge mode; direct mode cannot use a socket.")
        socket = path_value(args.bridge_socket)
    existing_mode, existing_socket = None, None
    if existing is not None:
        if (not isinstance(existing, dict) or not isinstance(existing.get("command"), str) or
                type(existing.get("directTools")) is not bool or
                type(existing.get("requestTimeoutMs")) is not int):
            raise SetupError("Existing Peekaboo entry conflicts with the exact direct/Bridge policy.")
        if existing == entry(existing["command"]):
            existing_mode = "direct"
        else:
            argv = existing.get("args")
            if isinstance(argv, list) and len(argv) == 4 and isinstance(argv[2], str):
                existing_socket = path_value(argv[2])
                if existing == entry(existing["command"], "bridge", existing_socket):
                    existing_mode = "bridge"
            if existing_mode is None:
                raise SetupError("Existing Peekaboo entry conflicts with the exact direct/Bridge policy.")
    mode = mode or existing_mode or "direct"
    report["evidence"]["mode"] = mode
    report["evidence"]["bridgeSocketState"] = "invalid" if mode == "bridge" else "not-applicable"
    if mode == "direct" and socket is not None:
        raise SetupError("--bridge-socket requires Bridge mode; direct mode cannot use a socket.")
    if existing_mode and mode != existing_mode:
        raise SetupError("Selected mode conflicts with the existing Peekaboo entry; no automatic migration is allowed.")
    if mode == "bridge":
        if socket is not None and existing_socket is not None and socket != existing_socket:
            raise SetupError("Selected Bridge socket conflicts with the existing Peekaboo entry.")
        socket = socket or existing_socket
        report["evidence"]["bridgeSocketState"] = "invalid"
        if socket is None:
            raise SetupError("Bridge mode requires --bridge-socket with a normalized absolute path.")
        report["evidence"]["bridgeSocketPath"] = str(socket)
        socket_stamp = socket_snapshot(socket, report)
    else:
        report["evidence"]["bridgeSocketState"] = "not-applicable"
        socket_stamp = None
    return mode, socket, socket_stamp


def managed():
    return Path.home() / ".local/share/peekaboo" / VERSION


def discover():
    local = managed() / "peekaboo"
    if local.exists() or local.is_symlink():
        return local
    # Only these Homebrew links are resolved. Explicit/configured symlinks are
    # rejected; never search PATH or the working directory.
    for prefix in (Path("/opt/homebrew"), Path("/usr/local")):
        candidate = prefix / "bin/peekaboo"
        if candidate.exists() or candidate.is_symlink():
            target = candidate.resolve(strict=True)
            if not target.is_relative_to(prefix / "Cellar/peekaboo"):
                raise SetupError("Homebrew Peekaboo link does not target its expected Cellar.")
            return target
    return None


def inspect(args, report):
    report["evidence"]["mode"] = args.mode or "direct"
    if args.mode == "bridge":
        report["evidence"]["bridgeSocketState"] = "invalid"
    config = path_value(args.config or str(Path.home() / ".config/mcp/mcp.json"))
    report["evidence"]["configuration"] = "invalid"
    raw, config_stamp = read_file(config, MAX_CONFIG)
    document = parse_json(raw) if raw is not None else {}
    if not isinstance(document, dict) or not isinstance(document.get("mcpServers", {}), dict):
        raise SetupError("MCP configuration must be an object with an object mcpServers field.")
    servers = document.get("mcpServers", {})
    existing = servers.get("peekaboo")
    report["evidence"]["configuration"] = "conflict" if "peekaboo" in servers else "missing"
    for name, server in servers.items():
        if name != "peekaboo" and (name.casefold() == "peekaboo" or
                isinstance(server, dict) and isinstance(server.get("command"), str) and
                Path(server["command"]).name == "peekaboo"):
            raise SetupError("Another Peekaboo entry already exists; resolve it manually.")
    if "peekaboo" in servers and existing is None:
        raise SetupError("Existing Peekaboo entry conflicts with the exact direct/Bridge policy.")
    mode, socket, socket_stamp = select_route(args, existing, report)
    configured = path_value(existing["command"]) if existing is not None else None
    binary = path_value(args.binary) if args.binary else configured or discover()
    if existing is not None and binary != configured:
        raise SetupError("Selected binary conflicts with the existing Peekaboo entry.")
    if existing is not None:
        report["evidence"]["configuration"] = "matching"
    binary_stamp = None
    runtime_stamp = None
    if binary:
        report["evidence"]["binaryPath"] = str(binary)
        _, binary_stamp = read_file(binary, MAX_EXPANDED, executable=True)
        _, runtime_stamp = read_file(binary.parent / "libswiftCompatibilitySpan.dylib", MAX_EXPANDED, executable=True)
        report["evidence"]["binaryPresent"] = binary_stamp is not None
        if args.binary and binary_stamp is None:
            raise SetupError("--binary must select an existing executable.")
    if args.install:
        if binary_stamp is not None:
            raise SetupError("--install is for a missing binary only; existing binaries are never overwritten or downgraded.")
        if args.binary or (configured is not None and configured != managed() / "peekaboo"):
            raise SetupError("Installation cannot replace a custom configured command.")
        if platform.system() != "Darwin" or platform.machine() != "arm64":
            raise SetupError("Pinned installation supports Apple Silicon macOS only.")
        if managed().exists() or managed().is_symlink():
            raise SetupError("Managed version directory already exists; inspect it manually, then create a fresh plan.")
        # Validate existing destination ancestors without creating anything.
        read_file(managed() / "peekaboo", MAX_EXPANDED, executable=True)
        binary = managed() / "peekaboo"
        report["evidence"]["binaryPath"] = str(binary)
        report["actions"].append("Download and verify official arm64 CLI 4.5.0 into the private version directory.")
    elif not binary_stamp:
        report["nextSteps"].append("Select an existing signed CLI with --binary, or review a new plan with --install.")
    if binary and report["evidence"]["configuration"] == "missing":
        report["actions"].append("Add the Peekaboo Bridge stdio entry with only browser tools disabled; preserve other MCP settings."
                                 if mode == "bridge" else
                                 "Add the full-catalog Peekaboo direct stdio entry; preserve other MCP settings.")
    if binary:
        report["actions"].append("On apply: verify Developer ID and exact CLI version before writing configuration.")
    selection = {"config": str(config), "configStamp": config_stamp, "binary": str(binary) if binary else None,
                 "binaryStamp": binary_stamp, "runtimeStamp": runtime_stamp,
                 "install": args.install, "version": VERSION, "sha256": SHA256,
                 "mode": mode, "bridgeSocketPath": str(socket) if socket else None,
                 "bridgeSocketStamp": socket_stamp,
                 "entry": entry(binary, mode, socket) if binary else None, "schemaVersion": 2}
    report["planId"] = hashlib.sha256(json.dumps(selection, sort_keys=True).encode()).hexdigest()
    return config, document, selection


def run_bounded(command, timeout=15, label="Read-only probe", *, native_only=False):
    """Bound combined child output and runtime; no shell, inherited secrets, or DYLD overrides."""
    env = {"HOME": str(Path.home()), "PATH": "/usr/bin:/bin", "LANG": "en_US.UTF-8"}
    if native_only:
        env["PEEKABOO_DISABLE_TOOLS"] = "browser"
    process = subprocess.Popen([str(x) for x in command], stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
                               cwd="/", start_new_session=True)
    output = {"stdout": bytearray(), "stderr": bytearray()}
    deadline, size = time.monotonic() + timeout, 0
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ, "stdout")
            selector.register(process.stderr, selectors.EVENT_READ, "stderr")
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise SetupError(f"{label} timed out; no readiness is implied.")
                for key, _ in selector.select(min(remaining, 0.2)):
                    block = os.read(key.fileobj.fileno(), 65536)
                    if not block:
                        selector.unregister(key.fileobj)
                        continue
                    size += len(block)
                    if size > MAX_CONFIG:
                        raise SetupError(f"{label} exceeded its output limit.")
                    output[key.data].extend(block)
            try:
                code = process.wait(timeout=max(0.01, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                raise SetupError(f"{label} timed out; no readiness is implied.") from None
        return code, bytes(output["stdout"]), bytes(output["stderr"])
    finally:
        # Also stop descendants retaining the probe's pipes after its leader exits.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
        process.stdout.close()
        process.stderr.close()


def signature(binary, identifier=IDENTIFIER):
    requirement = (f'=anchor apple generic and identifier "{identifier}" and '
                   f'certificate leaf[subject.OU] = "{TEAM}" and '
                   'certificate 1[field.1.2.840.113635.100.6.2.6] exists and '
                   'certificate leaf[field.1.2.840.113635.100.6.1.13] exists')
    code, _, _ = run_bounded(["/usr/bin/codesign", "--verify", "--strict", "-R", requirement, binary])
    if code:
        raise SetupError("Developer ID signature/identifier verification failed; executable was not started.")


def startup(binary, report):
    if platform.system() != "Darwin":
        raise SetupError("CLI validation requires macOS.")
    _, before = read_file(binary, MAX_EXPANDED, executable=True)
    if before is None:
        raise SetupError("Peekaboo executable is missing.")
    signature(binary)
    library = binary.parent / "libswiftCompatibilitySpan.dylib"
    _, library_before = read_file(library, MAX_EXPANDED, executable=True)
    if library_before:
        signature(library, "com.apple.dt.runtime.swiftCompatibilitySpan")
    if read_file(binary, MAX_EXPANDED, executable=True)[1] != before:
        raise SetupError("Executable changed during signature verification.")
    report["evidence"]["runnable"] = "no"
    code, out, _ = run_bounded([binary, "--version"])
    if code:
        raise SetupError("CLI startup failed (possibly incompatible Swift runtime); configuration was not written.")
    # Do not accept 4.6 or a misleading embedded version substring.
    if not re.fullmatch(rb"(?:Peekaboo\s+)?v?4\.5\.0(?:\s+\([^\r\n]*\))?\s*", out.strip()):
        report["evidence"]["runnable"] = "no"
        raise SetupError("CLI version is not the supported 4.5.0 compatibility pin; no downgrade was attempted.")
    if (read_file(binary, MAX_EXPANDED, executable=True)[1] != before or
            read_file(library, MAX_EXPANDED, executable=True)[1] != library_before):
        raise SetupError("Executable/runtime changed during startup validation.")
    report["evidence"]["runnable"] = "yes"
    return before


def permissions(binary, report):
    bridge = report["evidence"]["mode"] == "bridge"
    route = ["--bridge-socket", report["evidence"]["bridgeSocketPath"]] if bridge else ["--no-remote"]
    command = [binary, "permissions", "status", *route, "--json"]
    code, out, _ = run_bounded(command, native_only=True) if bridge else run_bounded(command)
    data = parse_json(out)
    if not isinstance(data, dict) or data.get("success") is not True or not isinstance(data.get("data"), dict):
        raise SetupError("Permission probe did not return a successful structured snapshot.")
    snapshot = data["data"]
    source = "bridge" if bridge else "local"
    if snapshot.get("source") != source or not isinstance(snapshot.get("permissions"), list):
        raise SetupError("Permission probe did not report the selected permission source.")
    names = {"Screen Recording": "screenRecording", "Accessibility": "accessibility",
             "Event Synthesizing": "eventSynthesizing"}
    found = {}
    for item in snapshot["permissions"]:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            raise SetupError("Invalid permission snapshot.")
        name = names.get(item["name"])
        if name:
            if name in found or type(item.get("isGranted")) is not bool:
                raise SetupError("Invalid permission snapshot.")
            found[name] = "granted" if item["isGranted"] else "denied"
    if code or len(found) != 3:
        raise SetupError("Permission probe failed or returned an incomplete snapshot.")
    report["evidence"]["permissions"].update(found)
    report["evidence"]["permissionSource"] = source
    if "denied" in found.values():
        report["nextSteps"].extend([
            "Open System Settings > Privacy & Security > Accessibility and Screen & System Audio Recording; approve the selected Peekaboo desktop app (Bridge permission owner)."
            if bridge else
            "Open System Settings > Privacy & Security > Accessibility and Screen & System Audio Recording; approve the actual CLI/responsible host shown by macOS, not an unrelated Peekaboo.app.",
            "If macOS requests a restart, restart the selected permission host yourself, then recheck through Pi's MCP adapter. Direct setup-process grants can differ; do not restart other sessions automatically."])
        raise SetupError("One or more permissions are denied for the selected host; no permission request was made.")


class OfficialRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urllib.parse.urlsplit(newurl)
        if (parsed.scheme != "https" or parsed.hostname not in {
                "github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com",
                "github-releases.githubusercontent.com"} or parsed.username or parsed.password or
                parsed.port not in {None, 443}):
            raise SetupError("Release download redirected outside official GitHub asset hosts.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download(destination):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), OfficialRedirect())
    digest, size, started = hashlib.sha256(), 0, time.monotonic()
    with opener.open(URL, timeout=15) as response, destination.open("xb") as target:
        if response.status != 200:
            raise SetupError("Release download failed.")
        while True:
            if time.monotonic() - started > 120:
                raise SetupError("Release download exceeded its time limit.")
            block = response.read1(65536)
            if not block:
                break
            size += len(block)
            if size > MAX_ARCHIVE:
                raise SetupError("Release archive exceeds its size limit.")
            target.write(block)
            digest.update(block)
            delay = size / RATE - (time.monotonic() - started)
            if delay > 0:
                time.sleep(delay)
    if digest.hexdigest() != SHA256:
        raise SetupError("Release archive SHA256 mismatch; nothing was installed.")


def download_bounded(destination):
    # Socket timeouts alone do not bound DNS or an entire redirect chain. Isolate
    # only the transfer in a killable child with a hard wall-clock deadline.
    script = ("import runpy,sys; from pathlib import Path; "
              "runpy.run_path(sys.argv[1])['download'](Path(sys.argv[2]))")
    code, _, _ = run_bounded([sys.executable, "-I", "-B", "-c", script,
                              str(Path(__file__).resolve()), str(destination)],
                             timeout=135, label="Release download")
    if code:
        raise SetupError("Release download/integrity validation failed; nothing was installed.")


def extract(archive, destination):
    allowed = {"peekaboo", "libswiftCompatibilitySpan.dylib", "README.md", "LICENSE", "VERSION"}
    seen, metadata_seen, total = set(), set(), 0
    prefix = "peekaboo-macos-arm64/"
    # The signed release tarball contains small AppleDouble sidecars. Ignore
    # only these exact regular-file names; never materialize their xattrs.
    metadata = {"._peekaboo-macos-arm64", *(prefix + "._" + name for name in allowed)}
    with tarfile.open(archive, "r:gz") as source:
        for member in source:
            archived_name = member.name.removeprefix("./")
            if archived_name in metadata:
                if not member.isfile() or member.size < 0 or member.size > 4096 or archived_name in metadata_seen:
                    raise SetupError("Release archive contains unsafe metadata.")
                metadata_seen.add(archived_name)
                continue
            if member.isdir() and archived_name.rstrip("/") == prefix.rstrip("/"):
                continue
            if not archived_name.startswith(prefix):
                raise SetupError("Release archive has an unexpected top-level directory.")
            name = archived_name[len(prefix):]
            if (name not in allowed or name in seen or not member.isfile() or
                    PurePosixPath(name).name != name or member.size < 0):
                raise SetupError("Release archive contains an unexpected or unsafe member.")
            total += member.size
            if total > MAX_EXPANDED:
                raise SetupError("Expanded release archive exceeds its size limit.")
            seen.add(name)
            target = destination / name
            with source.extractfile(member) as incoming, target.open("xb") as outgoing:
                while block := incoming.read(65536):
                    outgoing.write(block)
            target.chmod(0o755 if name in {"peekaboo", "libswiftCompatibilitySpan.dylib"} else 0o644)
    if not {"peekaboo", "libswiftCompatibilitySpan.dylib"}.issubset(seen):
        raise SetupError("Release archive is missing the CLI or its required runtime library.")


def install(report):
    destination = managed()
    with directory(destination.parent, create=True) as parent_fd:
        with tempfile.TemporaryDirectory(prefix=".peekaboo-", dir=destination.parent) as temp:
            stage = Path(temp)
            download_bounded(stage / "release.tar.gz")
            payload = stage / "payload"
            payload.mkdir(mode=0o700)
            extract(stage / "release.tar.gz", payload)
            startup(payload / "peekaboo", report)
            # mkdir is exclusive: never replace even an empty existing directory.
            os.mkdir(destination.name, 0o700, dir_fd=parent_fd)
            report["warnings"].append("Managed version directory created; a later failure may leave a partial install. Inspect before retrying.")
            with directory(destination) as target_fd, directory(payload) as source_fd:
                for child in payload.iterdir():
                    os.rename(child.name, child.name, src_dir_fd=source_fd, dst_dir_fd=target_fd)
    report["actions"].append("Installed verified CLI and runtime library in the managed version directory.")
    report["evidence"]["binaryPresent"] = True


def write_config(config, document, selection):
    document.setdefault("mcpServers", {})["peekaboo"] = selection["entry"]
    payload = (json.dumps(document, indent=2, ensure_ascii=True, allow_nan=False) + "\n").encode()
    if len(payload) > MAX_CONFIG:
        raise SetupError("Merged configuration exceeds the size limit; no configuration was changed.")
    with directory(config.parent, create=True) as fd:
        name = ".peekaboo-" + uuid.uuid4().hex
        temp_fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
        try:
            with os.fdopen(temp_fd, "wb") as output:
                output.write(payload)
                output.flush()
                os.fsync(output.fileno())
            if read_at(fd, config.name, MAX_CONFIG)[1] != selection["configStamp"]:
                raise SetupError("Configuration changed after planning; no configuration was overwritten.")
            # Confirm the path still names the pinned parent, not a renamed/replaced ancestor.
            with directory(config.parent) as current:
                if (os.fstat(current).st_dev, os.fstat(current).st_ino) != (os.fstat(fd).st_dev, os.fstat(fd).st_ino):
                    raise SetupError("Configuration directory changed after planning.")
            os.replace(name, config.name, src_dir_fd=fd, dst_dir_fd=fd)
            os.fsync(fd)
        finally:
            try:
                os.unlink(name, dir_fd=fd)
            except FileNotFoundError:
                pass


def refresh_after_failure(args, report):
    """Report actual static state after partial apply, without replacing probe evidence."""
    if args.action != "apply":
        return
    current = result(args.action)
    try:
        inspect(argparse.Namespace(**(vars(args) | {
            "install": False, "binary": report["evidence"]["binaryPath"] or args.binary})), current)
    except (RuntimeError, OSError, ValueError):
        pass
    for key in ("binaryPath", "binaryPresent", "configuration", "mode", "bridgeSocketPath", "bridgeSocketState"):
        report["evidence"][key] = current["evidence"][key]
    for step in current["nextSteps"]:
        if step not in report["nextSteps"]:
            report["nextSteps"].append(step)


def operate(args, lock):
    report = result(args.action)
    try:
        config, document, selection = inspect(args, report)
        if args.action in {"plan", "status"}:
            report["summary"] = "Offline Peekaboo inventory; executable, permissions, MCP and desktop not tested."
        elif args.action == "apply":
            if not args.yes:
                raise SetupError("Apply requires explicit --yes approval; no changes made.")
            if not args.expected_plan or not re.fullmatch(r"[a-f0-9]{64}", args.expected_plan) or args.expected_plan != report["planId"]:
                raise SetupError("Expected plan is missing or stale; run plan again with the same options.")
            if not selection["binary"]:
                raise SetupError("No Peekaboo binary selected; create a plan with --binary or --install.")
            # Validate lock ancestors before the existing shared lock creates anything.
            read_file(Path.home() / ".config/pi-shared/update.lock", MAX_CONFIG)
            with lock():
                current = result(args.action)
                config, document, selection = inspect(args, current)
                if current["planId"] != args.expected_plan:
                    raise SetupError("State changed while acquiring the lock; create a fresh plan.")
                if args.install:
                    install(report)
                binary = Path(selection["binary"])
                verified = startup(binary, report)
                if not args.install and (verified != selection["binaryStamp"] or
                        read_file(binary.parent / "libswiftCompatibilitySpan.dylib", MAX_EXPANDED, executable=True)[1] != selection["runtimeStamp"]):
                    raise SetupError("Binary/runtime changed after planning; no configuration was written.")
                if selection["mode"] == "bridge" and socket_snapshot(Path(selection["bridgeSocketPath"]), report) != selection["bridgeSocketStamp"]:
                    raise SetupError("Bridge socket changed after planning; create a fresh plan.")
                if read_file(config, MAX_CONFIG)[1] != selection["configStamp"]:
                    raise SetupError("Configuration changed after planning; no configuration was overwritten.")
                if report["evidence"]["configuration"] != "matching":
                    write_config(config, document, selection)
                    report["actions"].append("Wrote Peekaboo configuration atomically; unrelated settings preserved.")
                report["evidence"]["configuration"] = "matching"
            report["summary"] = "Peekaboo CLI validated and configured; MCP, permissions and desktop not tested."
            report["nextSteps"].extend(["Run pi-shared peekaboo check --json for explicit read-only CLI/permission probes.",
                "Restart Pi or /reload, then discover Peekaboo through the MCP adapter; recheck permissions from that host."])
        elif args.action == "check":
            if args.install:
                raise SetupError("--install is only supported by plan/apply; check never installs.")
            if not report["evidence"]["binaryPresent"]:
                raise SetupError("Peekaboo executable is missing; no probe was run.")
            if selection["mode"] == "bridge" and report["evidence"]["bridgeSocketState"] != "present":
                raise SetupError("Bridge socket is missing; start the desktop app yourself before checking.")
            startup(Path(selection["binary"]), report)
            if selection["mode"] == "bridge" and socket_snapshot(Path(selection["bridgeSocketPath"]), report) != selection["bridgeSocketStamp"]:
                raise SetupError("Bridge socket changed during validation; recheck before probing permissions.")
            permissions(Path(selection["binary"]), report)
            if report["evidence"]["configuration"] != "matching":
                raise SetupError("CLI probes passed but Peekaboo configuration is missing.")
            report["summary"] = "CLI and selected-host permissions checked; MCP and desktop remain not tested."
        report["ok"] = True
        return report, 0
    except KeyboardInterrupt:
        refresh_after_failure(args, report)
        report["errors"].append("Cancelled; completed actions were not rolled back. Inspect config and managed directory before retrying.")
        report["summary"] = "Peekaboo operation cancelled."
        return report, 130
    except (OSError, ValueError, tarfile.TarError, RuntimeError, EOFError, http.client.HTTPException) as exc:
        refresh_after_failure(args, report)
        report["errors"].append(str(exc) if isinstance(exc, SetupError) else
                                "Peekaboo operation failed safely; inspect local paths, network availability and lock ownership. No child output is disclosed.")
        report["summary"] = "Peekaboo operation failed; review diagnostics."
        return report, 1


def main(argv, lock):
    # A private parser keeps structured failures isolated from the main setup CLI.
    report = result(argv[0] if argv and argv[0] in {"plan", "status", "apply", "check"} else "unknown")

    class Parser(argparse.ArgumentParser):
        def error(self, message):
            raise SetupError("Invalid Peekaboo arguments; use pi-shared peekaboo --help.")

    parser = Parser(prog="pi-shared peekaboo", description="Standalone Peekaboo CLI configuration (not a setup module).",
                    epilog="Plans/status are offline. Apply needs --yes and a matching plan ID. No TCC requests, services, app installation/launch, provider setup or desktop actions.")
    parser.add_argument("action", choices=["plan", "status", "apply", "check"])
    parser.add_argument("--json", action="store_true", help="Emit only the schema-v2 JSON result on stdout")
    parser.add_argument("--config", metavar="ABS", help="MCP JSON path (default ~/.config/mcp/mcp.json)")
    parser.add_argument("--binary", metavar="ABS", help="Existing signed executable; never search PATH")
    parser.add_argument("--mode", choices=["direct", "bridge"], help="Preserve an exact existing route when omitted; otherwise default direct")
    parser.add_argument("--bridge-socket", metavar="ABS", help="Bridge socket path; required for a new Bridge entry, never auto-discovered")
    parser.add_argument("--install", action="store_true", help="Explicit missing-binary-only 4.5.0 Apple Silicon installation")
    parser.add_argument("--yes", action="store_true", help="Approve apply; no interactive confirmation")
    parser.add_argument("--expected-plan", metavar="SHA256", help="Plan ID from plan with identical binary/config/install/mode/socket choices")
    try:
        args = parser.parse_args(argv)
        if args.action != "apply" and (args.yes or args.expected_plan):
            raise SetupError("--yes and --expected-plan are only valid for apply.")
        if args.action not in {"plan", "apply"} and args.install:
            raise SetupError("--install is only valid for plan/apply.")
        # The frontend cancels our process group with SIGTERM. Child probes use
        # their own groups for bounded cleanup, so unwind their finally blocks
        # rather than letting TERM orphan a download or probe.
        def cancel(_signum, _frame):
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            raise KeyboardInterrupt
        previous = signal.signal(signal.SIGTERM, cancel)
        try:
            report, code = operate(args, lock)
        finally:
            signal.signal(signal.SIGTERM, previous)
    except SetupError as exc:
        report["errors"].append(str(exc))
        code = 2
    if "--json" in argv:
        print(json.dumps(report, sort_keys=True))
    else:
        print(report["summary"])
        if "planId" in report:
            print("Plan ID: " + report["planId"])
        for key in ("actions", "warnings", "errors", "nextSteps"):
            for text in report[key]:
                print(f"{key}: {text}")
    return code
