"""Bounded /setup capabilities: inventory, missing-only scaffolds, native MCP migration.

No project commands, package managers, services or inference are executed here.
The shared root is an explicitly supplied, trusted distribution (not discovered
from project config). Filesystem safeguards are shared with standalone Peekaboo.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shlex
import signal
import stat
import sys

from peekaboo_setup import (SetupError, directory, parse_json, path_value,
                            read_file, run_bounded, stamp)
from search_setup import _url

COMPONENTS = ("search", "browser", "mcp", "development", "documents", "apple",
              "knowledge", "models", "diagnostics")
MAX_FILE = 1024 * 1024
KB_FILES = ("sources.json", "documents.json", "corpus/source-cards.md")
MODES = {"search": ("guided", "local", "existing"), "browser": ("public", "app"),
         "mcp": ("native", "migrate"), "development": ("code-intel", "verification"),
         "knowledge": ("initialize", "ingest"), "models": ("native", "guided", "gateway", "omlx")}


def result(component, action):
    return {"schemaVersion": 1, "component": component, "action": action, "ok": False,
            "summary": "Capability operation not completed.", "status": "unknown",
            "evidence": [], "actions": [], "warnings": [], "errors": [],
            "nextSteps": [], "handoffs": []}


def text(value, empty=False):
    if (not isinstance(value, str) or (not value and not empty) or len(value) > 4096 or
            any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise SetupError("Expected bounded text without control characters.")
    return value


def absolute(value):
    return path_value(text(value))


def evidence(report, label, value):
    report["evidence"].append({"label": label, "value": value})


def handoff(report, label, argv, kind="terminal"):
    report["handoffs"].append({"label": label, "command": shlex.join(map(str, argv)) if
                               isinstance(argv, list) else argv, "kind": kind})


def options(component, raw):
    if len(raw.encode()) > 16384:
        raise SetupError("Options exceed 16 KiB.")
    data = parse_json(raw)
    if not isinstance(data, dict):
        raise SetupError("Options must be a JSON object.")
    modes = MODES.get(component)
    mode = data.get("mode", modes[0] if modes else None)
    if modes and (not isinstance(mode, str) or mode not in modes):
        raise SetupError("Unsupported component mode.")
    allowed = {"mode"} if modes else set()
    if component == "search":
        allowed |= {"keyFile"} if mode == "local" else {"url", "keyFile"} if mode == "existing" else set()
    if component == "development" and mode == "verification":
        allowed |= {"command", "args", "inputs"}
    if component == "knowledge":
        allowed.add("root")
    if component == "mcp" and mode == "migrate":
        allowed.add("sourceConfig")
    if set(data) - allowed:
        raise SetupError("Unknown option or option not supported by the selected mode.")
    if modes:
        data["mode"] = mode
    if component == "search":
        if mode == "local" and "keyFile" not in data:
            raise SetupError("Local search needs a private Brave key file path; never supply a literal key.")
        if "keyFile" in data:
            absolute(data["keyFile"])
        if mode == "existing":
            try:
                _url(text(data.get("url")), "keyFile" in data)
            except RuntimeError:
                raise SetupError("Use an HTTP(S) search URL without credentials, query, fragment or controls; bearer authentication requires HTTPS or loopback HTTP.") from None
    if component == "knowledge" and "root" in data:
        absolute(data["root"])
    if component == "mcp" and "sourceConfig" in data:
        absolute(data["sourceConfig"])
    if component == "development" and mode == "verification":
        # An empty selection is an inventory/guidance request, never a guessed test.
        fields = {"command", "args", "inputs"} & set(data)
        if fields and fields != {"command", "args", "inputs"}:
            raise SetupError("Verification requires command, args and explicit existing input paths together.")
        if fields:
            text(data["command"])
            for key in ("args", "inputs"):
                if not isinstance(data[key], list) or len(data[key]) > 128:
                    raise SetupError("Verification arrays must have at most 128 entries.")
                for item in data[key]:
                    text(item, empty=key == "args")
            if not data["inputs"] or len(json.dumps(data["args"]).encode()) > 8192:
                raise SetupError("Verification needs nonempty inputs and argv no larger than 8 KiB.")
    return data


def directory_identity(path, missing=False):
    try:
        with directory(path) as fd:
            info = os.fstat(fd)
            return [info.st_dev, info.st_ino, info.st_mode, info.st_uid]
    except FileNotFoundError:
        if missing:
            return None
        raise SetupError("Required context directory is missing.") from None


def ancestors(path):
    """Stable ancestry identity; directory mtimes change when our lock is created."""
    found = []
    for parent in reversed((path, *path.parents)):
        identity = directory_identity(parent, missing=True)
        found.append([str(parent), identity])
        if identity is None:
            break
    return found


def file_state(path, limit=MAX_FILE):
    raw, identity = read_file(path, limit)
    return raw, {"path": str(path), "ancestors": ancestors(path.parent), "file": identity}


def existing_input(project, value):
    if (value.startswith("/") or "\\" in value or
            any(part in {"", ".", ".."} for part in value.split("/")) or
            any(c in value for c in "*?[]{}")):
        raise SetupError("Verification inputs must be literal normalized project-relative paths without traversal or globs.")
    path = project / value
    with directory(path.parent) as fd:
        info = os.stat(path.name, dir_fd=fd, follow_symlinks=False)
        if (not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)) or
                info.st_uid not in {0, os.getuid()} or info.st_mode & 0o022):
            raise SetupError("Verification input is unsafe or not a regular file/directory.")
        return {"path": value, "identity": stamp(info)}


def context(args):
    paths = {name: absolute(getattr(args, name)) for name in
             ("project", "agent_dir", "shared_root", "node_executable")}
    directory_identity(paths["project"])
    directory_identity(paths["shared_root"])
    ancestors(paths["agent_dir"])
    paths["managed_roots"] = managed_roots(getattr(args, "read_state", None))
    return paths


def project_scope(paths):
    project = paths["project"]
    home = Path.home()
    if (project == Path("/") or project == home or project in home.parents or
            project == paths["agent_dir"] or project.is_relative_to(paths["agent_dir"]) or
            any(project.is_relative_to(Path(p)) for p in ("/System", "/usr", "/bin", "/sbin", "/etc", "/opt/homebrew/Cellar")) or
            any(project.is_relative_to(root) for root in paths["managed_roots"]) or project.is_relative_to(home / ".pi")):
        raise SetupError("Select an ordinary project directory, not a home, profile, system or installed-runtime directory.")
    with directory(project) as fd:
        if os.fstat(fd).st_uid != os.getuid():
            raise SetupError("Project must be owned by the current user.")
    ancestors(project / ".pi")


def setup_handoff(report, argv):
    handoff(report, "Review the owning setup plan in a terminal", ["pi-shared", "setup", *argv])
    report["warnings"].append("This is a handoff, not an apply action. The owning setup preserves saved selections and may change shared services and profile wiring beyond this capability; review its full plan and consent there. No --yes is supplied.")


def bundled(paths, relative, limit=MAX_FILE):
    return file_state(paths["shared_root"] / relative, limit)


def managed_roots(read_state=None):
    roots = [Path.home() / ".local/share/pi-shared", Path("/opt/homebrew/Cellar"), Path("/usr/local/Cellar")]
    receipt, _ = read_file(Path.home() / ".config/pi-shared/setup.json", 65536)
    if receipt is not None:
        if read_state is None:
            raise SetupError("Setup receipt validation is required before inspecting managed ownership.")
        try:
            state = read_state(allow_incomplete=True)
            root = absolute(state["code_root"])
            ancestors(root)
        except (RuntimeError, KeyError, TypeError, ValueError, OSError):
            raise SetupError("Cannot validate saved installation ownership; inspect the setup receipt before continuing.") from None
        roots.append(root)
    return tuple(roots)


def managed_shared(paths):
    return any(paths["shared_root"].is_relative_to(root) for root in paths["managed_roots"])


def dependency_update_handoff(report):
    handoff(report, "Preview the complete owning update scope first", ["pi-shared", "update", "--plan"])
    handoff(report, "Apply the full managed update only after reviewing its plan", ["pi-shared", "update"])
    report["warnings"].append("pi-shared update applies immediately: it can upgrade the CLI/runtime and all saved modules, rewire profiles and restart selected shared services. It is not dependency-only repair and does not ask for confirmation. Review update --plan before choosing to run it.")


def homebrew_node_ancestor(node, ancestor, info):
    match = re.fullmatch(r"(/opt/homebrew|/usr/local)/Cellar/node(?:@\d+)?/[^/]+/bin/node", str(node))
    allowed = {Path(match[1]), Path(match[1]) / "Cellar"} if match else set()
    return (platform.system() == "Darwin" and ancestor in allowed and info.st_gid == 80 and
            80 in os.getgroups() and not info.st_mode & 0o002)


def node_identity(node):
    """Pin a canonical executable, permitting only Homebrew's admin ancestors."""
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    path = Path("/")
    lineage = []
    try:
        for part in node.parent.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
            path /= part
            info = os.fstat(fd)
            admin = homebrew_node_ancestor(node, path, info)
            if (info.st_uid not in {0, os.getuid()} or info.st_mode & 0o022 and
                    not (admin or info.st_uid == 0 and info.st_mode & stat.S_ISVTX)):
                raise SetupError("Unsafe Node executable ancestor ownership or permissions.")
            lineage.append([info.st_dev, info.st_ino, info.st_mode, info.st_uid])
        info = os.stat(node.name, dir_fd=fd, follow_symlinks=False)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or
                not info.st_mode & 0o111 or info.st_mode & 0o6022 or info.st_uid not in {0, os.getuid()}):
            raise SetupError("Node must be a safe regular absolute executable without symlinks.")
        return {"stat": stamp(info), "ancestors": lineage}
    finally:
        os.close(fd)


def development(paths, opts, report, selection):
    project_scope(paths)
    mode = opts["mode"]
    target = paths["project"] / ".pi" / ("code-intel.json" if mode == "code-intel" else "verification.json")
    raw, state = file_state(target)
    selection["target"] = state
    evidence(report, "Project configuration", "present; preserved without execution" if raw is not None else "missing")
    report["warnings"].append("No project or verification trust is granted. No language server, project command or tests are started.")
    if raw is not None:
        report["status"] = "configured-untested"
        report["nextSteps"].append("Review the existing project configuration manually; setup never replaces it or certifies its contents.")
        return
    report["status"] = "needs-configuration"
    if mode == "verification":
        if "command" not in opts:
            report["nextSteps"].append("Choose a command, argv and real project-relative input paths; no test command is guessed. Then request a new plan.")
            return
        selection["inputs"] = [existing_input(paths["project"], v) for v in opts["inputs"]]
        data = {"version": 1, "checks": [{"id": "project-check", "command": opts["command"],
                "args": opts["args"], "cwd": ".", "timeoutSeconds": 120,
                "inputs": {"paths": opts["inputs"], "exclude": [], "untracked": "include"},
                "report": {"format": "exit"}}]}
        if len(json.dumps(data["checks"][0]).encode()) > 16384:
            raise SetupError("Verification check exceeds 16 KiB.")
        report["nextSteps"].append("Review .pi/verification.json, then use /verification-trust in a separately trusted project. Exit-only evidence does not claim discovered or passing tests.")
    else:
        base = "extensions/code-intel/"
        tls = "node_modules/typescript-language-server/lib/cli.mjs"
        ts = "node_modules/typescript/lib/tsserver.js"
        dependencies = {}
        blobs = {}
        for relative in ("package-lock.json", tls, ts,
                         "node_modules/typescript-language-server/package.json", "node_modules/typescript/package.json"):
            blobs[relative], dependencies[relative] = bundled(paths, base + relative, 32 * MAX_FILE)
        selection["dependencies"] = dependencies
        if not all(value is not None for value in blobs.values()):
            if managed_shared(paths):
                dependency_update_handoff(report)
            else:
                handoff(report, "Install locked dependencies in this source checkout",
                        "cd " + shlex.quote(str(paths["shared_root"] / base)) + " && npm ci --ignore-scripts --no-audit --no-fund")
            report["nextSteps"].append("Install missing locked code-intel dependencies through their owner, then create a fresh plan.")
            return
        lock = parse_json(blobs["package-lock.json"])
        try:
            packages = lock["packages"]
            for name in ("typescript", "typescript-language-server"):
                installed = parse_json(blobs[f"node_modules/{name}/package.json"])
                version = packages[f"node_modules/{name}"]["version"]
                if installed["version"] != version or packages[""]["dependencies"][name] != version:
                    raise SetupError("Code-intel dependencies do not match their exact lockfile pins.")
        except (KeyError, TypeError):
            raise SetupError("Code-intel dependency lock or installed manifests are invalid.") from None
        node = paths["node_executable"]
        selection["node"] = node_identity(node)
        data = {"version": 1, "adapter": "typescript-language-server", "workspace": ".",
                "executable": str(node), "args": [str(paths["shared_root"] / base / tls), "--stdio"],
                "tsserverPath": str(paths["shared_root"] / base / ts), "timeoutMs": 15000}
        report["nextSteps"].append("Review .pi/code-intel.json in a separately trusted project. Reload Pi and request code_intel status; server startup remains a separate action.")
    payload = (json.dumps(data, indent=2, ensure_ascii=True) + "\n").encode()
    if len(payload) > (16384 if mode == "code-intel" else 65536):
        raise SetupError("Proposed configuration exceeds its consumer's size limit.")
    selection["files"] = {str(target): payload}
    evidence(report, "Destination", str(target))
    preview = payload.decode("utf-8")
    for offset in range(0, len(preview), 3500):
        evidence(report, "Proposed JSON" + (f" (part {offset // 3500 + 1})" if len(preview) > 3500 else ""), preview[offset:offset + 3500])
    report["actions"].append("Create only the missing .pi/" + target.name + " (0600); preserve all existing configuration.")


def knowledge(paths, opts, report, selection):
    default = str(Path.home() / ".pi/knowledge/software-engineering")
    configured = os.environ.get("PI_SOFTWARE_KB_ROOT")
    if "root" in opts:
        root = absolute(opts["root"])
    elif configured is not None:
        try:
            root = absolute(configured)
        except SetupError:
            root = absolute(default)
            report["warnings"].append("Invalid PI_SOFTWARE_KB_ROOT was ignored; using the private default root.")
    else:
        root = absolute(default)
    project = paths["project"]
    ordinary_project = project != Path.home() and project not in Path.home().parents
    if (root.is_relative_to(paths["shared_root"]) or ordinary_project and root.is_relative_to(project) or
            any(root.is_relative_to(managed) for managed in paths["managed_roots"]) or
            root.is_relative_to(paths["agent_dir"]) or root == Path.home() or root in Path.home().parents):
        raise SetupError("Knowledge root must be external to the project, shared distribution and agent profile.")
    selection["root"] = str(root)
    selection["rootAncestors"] = ancestors(root)
    present = directory_identity(root, missing=True) is not None
    report["status"] = "configured-untested" if present else "not-installed"
    evidence(report, "Private knowledge layout", "existing root; untouched" if present else "new external root")
    evidence(report, "Knowledge root", str(root))
    report["warnings"].append("Metadata is not an indexed corpus. No books, private text or indexes are copied, downloaded or uploaded.")
    handoff(report, "Activate this root in the shell that launches Pi",
            "export PI_SOFTWARE_KB_ROOT=" + shlex.quote(str(root)))
    report["nextSteps"].append("For a custom root, set PI_SOFTWARE_KB_ROOT before launching Pi, then restart/reload with that environment. Only ingest PDFs you are entitled to use locally.")
    if present:
        for relative in KB_FILES:
            raw, state = file_state(root / relative)
            selection[relative] = state
            evidence(report, relative, "present (contents not displayed)" if raw is not None else "missing")
        # Never open the private index or inspect PDF content.
        evidence(report, "Indexing", "not checked; metadata presence is not indexing evidence")
    if opts["mode"] == "ingest":
        argv = ["python3", str(paths["shared_root"] / "extensions/software-kb/ingest.py"), "--private", "--root", str(root)]
        handoff(report, "Explicit local private ingestion (requires Poppler)", argv)
        if platform.system() == "Darwin":
            handoff(report, "Optional local Vision OCR (requires full local prerequisites)", [*argv, "--ocr"])
        return
    if present:
        report["nextSteps"].append("Existing roots are never merged or repaired by apply. Review missing metadata manually or choose a new external root.")
        return
    files = {}
    for relative in KB_FILES:
        raw, state = bundled(paths, "knowledge/software-engineering/" + relative)
        if raw is None:
            raise SetupError("Bundled public knowledge metadata is missing; update the owning shared distribution.")
        selection[relative] = state
        files[str(root / relative)] = raw
    selection["files"] = files
    selection["directories"] = [str(root / p) for p in ("corpus", "corpus/pdf-downloads", "private")]
    report["actions"].append("Create a new owner-only knowledge root, corpus/pdf-downloads and private directories (0700), and only the three bundled public metadata files (0600).")


def static_file(report, paths, label, relative):
    raw, _ = bundled(paths, relative)
    evidence(report, label, "present; execution not tested" if raw is not None else "missing")
    return raw is not None


def document_inventory(report):
    # Filesystem presence only: do not execute PATH entries, office macros or PDFs.
    candidates = {"LibreOffice/soffice": ["/Applications/LibreOffice.app/Contents/MacOS/soffice",
                    "/usr/bin/soffice", "/usr/bin/libreoffice"],
                  "Poppler/pdfinfo": ["/opt/homebrew/bin/pdfinfo", "/usr/local/bin/pdfinfo", "/usr/bin/pdfinfo"],
                  "Poppler/pdftoppm": ["/opt/homebrew/bin/pdftoppm", "/usr/local/bin/pdftoppm", "/usr/bin/pdftoppm"]}
    found = []
    for label, locations in candidates.items():
        present = any(Path(p).is_file() and os.access(p, os.X_OK) for p in locations)
        found.append(present)
        evidence(report, label, "executable path present (not run)" if present else "not found in standard locations")
    report["status"] = "configured-untested" if all(found) else "not-installed"
    if platform.system() == "Darwin":
        handoff(report, "Install LibreOffice explicitly", ["brew", "install", "--cask", "libreoffice"])
        handoff(report, "Install Poppler explicitly", ["brew", "install", "poppler"])
    else:
        report["nextSteps"].append("Install LibreOffice and Poppler (pdfinfo and pdftoppm) using your operating system's package manager; setup does not install them.")
    return all(found)


def inspect(args, report):
    paths = context(args)
    opts = options(args.component, args.options)
    selection = {"schemaVersion": 1, "component": args.component, "options": opts,
                 "context": {k: str(v) for k, v in paths.items() if k != "managed_roots"},
                 "managedRoots": [str(root) for root in paths["managed_roots"]], "files": {}}
    c, mode = args.component, opts.get("mode")
    report["summary"] = "Offline capability inventory and explicit handoffs; no execution readiness is implied."
    if c == "development":
        development(paths, opts, report, selection)
    elif c == "knowledge":
        knowledge(paths, opts, report, selection)
    elif c == "search":
        argv = ["--guided"] if mode == "guided" else ["--search", mode]
        if mode == "existing":
            argv += ["--search-url", opts["url"]]
        if "keyFile" in opts:
            # Check path ancestors but never open credentials, even for check.
            ancestors(absolute(opts["keyFile"]).parent)
            argv += ["--brave-key-file" if mode == "local" else "--search-key-file", opts["keyFile"]]
        setup_handoff(report, argv)
        static_file(report, paths, "Search integration", "extensions/websearch/index.ts")
        report["warnings"].append("Search credentials, endpoint authentication and tool availability are not probed. web_search/web_fetch are independent of browser tools.")
    elif c == "browser":
        if mode == "public":
            setup_handoff(report, ["--with-browser"])
            static_file(report, paths, "Public browser integration", "extensions/pi-browser-capture/src/browser-worker.ts")
        else:
            base = paths["shared_root"] / "extensions/pi-browser-capture"
            if managed_shared(paths):
                dependency_update_handoff(report)
                handoff(report, "Install Chromium in the user browser cache after dependencies are present",
                        [str(paths["node_executable"]), str(base / "node_modules/playwright/cli.js"), "install", "chromium"])
            else:
                handoff(report, "Install locked app dependencies and local Playwright Chromium",
                        "cd " + shlex.quote(str(base)) + " && npm ci --ignore-scripts && " +
                        shlex.join([str(paths["node_executable"]), "node_modules/playwright/cli.js", "install", "chromium"]))
            static_file(report, paths, "Local Playwright CLI", "extensions/pi-browser-capture/node_modules/playwright/cli.js")
            report["warnings"].append("Chromium download is separate explicit consent; no browser is started here. Managed package artifacts are changed only through their owning update workflow.")
        report["warnings"].append("Public browser_fetch/browser_inspect and private app_* have separate runtimes and policies. Presence does not verify authenticated inventory or browser execution.")
    elif c == "mcp":
        import mcp_setup
        mcp_setup.inspect(paths, opts, report, selection)
    elif c == "documents":
        selection["prerequisites"] = document_inventory(report)
    elif c == "apple":
        evidence(report, "Platform", "macOS" if platform.system() == "Darwin" else "not macOS")
        evidence(report, "Standard full Xcode location", "present (selection not tested)" if Path("/Applications/Xcode.app/Contents/Developer").is_dir() else "not found; custom location possible")
        report["nextSteps"].append("Install full Xcode manually from Apple and select its developer directory yourself. Command Line Tools alone are insufficient. Check only lists available simulators; no boot, build, signing, license acceptance or upload.")
    elif c == "models":
        if mode == "native":
            handoff(report, "Authenticate through Pi's native provider flow", "/login", "pi")
            handoff(report, "Select a native model", "/model", "pi")
        else:
            setup_handoff(report, ["--guided"] if mode == "guided" else
                          ["--with", "model-gateway"] if mode == "gateway" else ["--with", "omlx", "--omlx", "guide"])
        report["warnings"].append("No credentials, model weights, provider endpoints or inference are inspected or invoked. oMLX guide is guidance, not installation.")
    elif c == "diagnostics":
        static_file(report, paths, "Static doctor", "bin/pi-doctor")
        report["actions"].append("On check only: run the shared pi-doctor --json --agent-dir with the explicit profile and no opt-in probes.")
        report["warnings"].append("Static doctor evidence is not current-session load, authentication, browser execution or inference readiness.")
    if selection["files"]:
        digestable = {**selection, "files": {k: v.hex() for k, v in selection["files"].items()}}
        report["planId"] = hashlib.sha256(json.dumps(digestable, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return paths, opts, selection


def exclusive_file(path, payload):
    with directory(path.parent, create=True) as fd:
        # Confirm the named directory still identifies our pinned descriptor.
        with directory(path.parent) as current:
            if (os.fstat(fd).st_dev, os.fstat(fd).st_ino) != (os.fstat(current).st_dev, os.fstat(current).st_ino):
                raise SetupError("Target directory changed; inspect before retrying.")
        out = os.open(path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
        with os.fdopen(out, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.fsync(fd)


def apply(args, report, selection, lock):
    if not selection["files"]:
        raise SetupError("No supported apply mutation exists for this selection; use the explicit handoff or review existing configuration.")
    if not args.yes:
        raise SetupError("Apply requires explicit --yes approval; no changes made.")
    if not args.expected_plan or not re.fullmatch(r"[a-f0-9]{64}", args.expected_plan) or args.expected_plan != report.get("planId"):
        raise SetupError("Expected plan is missing or stale; request a fresh plan with identical context and options.")
    read_file(Path.home() / ".config/pi-shared/update.lock", MAX_FILE)
    with lock():
        current = result(args.component, args.action)
        _, _, selected = inspect(args, current)
        if current.get("planId") != args.expected_plan:
            raise SetupError("State changed while acquiring the lock; request a fresh plan.")
        if args.component == "knowledge":
            root = Path(selected["root"])
            with directory(root.parent, create=True) as fd:
                os.mkdir(root.name, 0o700, dir_fd=fd)  # Exclusive, even for an empty existing root.
            for name in selected["directories"]:
                with directory(Path(name), create=True):
                    pass
        if args.component == "mcp":
            import mcp_setup
            mcp_setup.apply(selected, args.expected_plan, report)
            report.pop("planId", None)
            return
        for name, payload in selected["files"].items():
            exclusive_file(Path(name), payload)
    report.pop("planId", None)
    report["status"] = "configured-untested"
    for row in report["evidence"]:
        if row["label"] in {"Project configuration", "Private knowledge layout"}:
            row["value"] = "created; execution/indexing not tested"
    report["summary"] = "Missing-only scaffold created; no trust, indexing, command execution or readiness was granted."
    report["warnings"].append("New files belong to you; setup has no uninstall ownership over them. Inspect manually before removal or subsequent edits.")


def apple_check(report):
    if platform.system() != "Darwin":
        raise SetupError("Apple prerequisites require macOS; no probe was run.")
    code, out, _ = run_bounded(["/usr/bin/xcode-select", "-p"])
    try:
        developer = absolute(out.decode().strip())
    except (SetupError, UnicodeError):
        raise SetupError("Xcode selection did not return a valid developer directory.") from None
    if code or developer.name != "Developer" or developer.parent.name != "Contents" or developer.parent.parent.suffix != ".app":
        raise SetupError("Full Xcode is not selected; Command Line Tools alone are insufficient.")
    if not developer.is_dir():
        raise SetupError("Selected Xcode developer directory does not exist.")
    code, out, _ = run_bounded(["/usr/bin/xcodebuild", "-version"])
    if code or not re.search(rb"(?m)^Xcode \d+(?:\.\d+)*\s*$", out):
        raise SetupError("Xcode version probe failed; no license was accepted.")
    evidence(report, "Full Xcode selection/version", "checked successfully (version output withheld)")
    code, out, _ = run_bounded(["/usr/bin/xcrun", "simctl", "list", "devices", "available", "-j"])
    data = parse_json(out)
    if code or not isinstance(data, dict) or not isinstance(data.get("devices"), dict):
        raise SetupError("Available simulator inventory probe failed.")
    count = 0
    for devices in data["devices"].values():
        if not isinstance(devices, list) or any(not isinstance(d, dict) for d in devices):
            raise SetupError("Invalid simulator inventory snapshot.")
        count += sum(d.get("isAvailable") is True for d in devices)
    evidence(report, "Available simulator devices", str(count))
    if not count:
        raise SetupError("No available simulator devices were reported; install a simulator runtime/device manually.")
    report["status"] = "verified"
    report["summary"] = "Full Xcode version and available simulator inventory checked; no app was built or booted."


def doctor_check(paths, report):
    # No paths/commands are sourced from settings. Validate the fixed distribution
    # files before executing this explicitly trusted shared diagnostic owner.
    for relative in ("bin/pi-doctor", "bin/pi-profile-check", "bin/pi-browser-check",
                     "bin/pi-shared-check-deps", "extensions/dev-doctor/doctor_common.py"):
        raw, _ = bundled(paths, relative)
        if raw is None:
            raise SetupError("Static doctor distribution is incomplete; update its owner.")
    # The probe helper strips the inherited environment. Restore only standard
    # package-manager executable locations for doctor's static PATH inventory.
    code, out, _ = run_bounded(["/usr/bin/env", "PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
                                sys.executable, "-I", "-B", paths["shared_root"] / "bin/pi-doctor",
                                "--json", "--agent-dir", paths["agent_dir"], "--timeout", "5"],
                               timeout=20, label="Static doctor")
    data = parse_json(out)
    if (code not in (0, 1, 2) or not isinstance(data, dict) or data.get("schema_version") != 1 or
            not isinstance(data.get("capabilities"), list) or len(data["capabilities"]) > 200):
        raise SetupError("Static doctor returned an invalid bounded report.")
    rows = data["capabilities"]
    if not rows or any(not isinstance(row, dict) or row.get("probe_type") != "static" for row in rows):
        raise SetupError("Doctor evidence was not exclusively static.")
    # Only fixed row names/status tokens are exposed; never reflect arbitrary
    # guidance, paths, simulator names or child output into the setup UI.
    routes = {"profile": "Review pi-shared setup --plan for profile wiring.",
              "extensions": "Review shared installation wiring; /setup mcp covers native MCP and adapter migration.",
              "models": "Use /setup models for provider/connection guidance.",
              "dependencies": "Use /setup development or the owning pi-shared update for locked dependencies.",
              "browser_worker": "Use /setup browser for browser-worker setup.",
              "node": "Review the owning Homebrew runtime installation.",
              "npm": "Review the owning Node/npm installation.",
              "python3": "Install Python through your package manager."}
    outcomes = {"available", "missing", "inspected", "not_checked", "not_exercised", "invalid_config",
                "disabled", "configured", "timeout", "spawn_error", "output_limit", "invalid_report"}
    seen = set()
    for row in rows:
        name = row.get("capability")
        if not isinstance(name, str) or name not in routes or name in seen:
            continue
        seen.add(name)
        outcome = row.get("outcome")
        status = outcome if isinstance(outcome, str) and outcome in outcomes else "unknown"
        evidence(report, "Doctor: " + name, status)
        if status not in {"available", "inspected", "disabled"}:
            report["nextSteps"].append(routes[name])
    evidence(report, "Static doctor rows", str(len(rows)))
    for key in ("installed", "configured"):
        evidence(report, "Doctor " + key, str(sum(row.get(key) == "yes" for row in rows)) + " yes; other rows unknown/no")
    if code:
        report["status"] = "needs-configuration"
        raise SetupError("Static doctor found issues; use the owning diagnostics locally to review them. No child output is disclosed.")
    report["status"] = "verified"
    report["summary"] = "Static doctor completed for the explicit profile; not a runtime or authentication readiness verdict."


def check(args, paths, selection, report):
    report.pop("planId", None)
    if args.component == "apple":
        apple_check(report)
    elif args.component == "diagnostics":
        doctor_check(paths, report)
    elif args.component == "documents":
        if not selection["prerequisites"]:
            raise SetupError("Document prerequisites were not found in standard locations; no document tools were executed.")
        report["status"] = "verified"
        report["summary"] = "Standard LibreOffice and Poppler executable paths checked only; no conversion or rendering was run."
    else:
        report["summary"] = "Static prerequisite inventory completed only; no commands, services, credentials or current-session tools were tested."


def operate(args, lock, read_state=None):
    args.read_state = read_state
    report = result(args.component, args.action)
    try:
        paths, _, selection = inspect(args, report)
        if args.action == "apply":
            apply(args, report, selection, lock)
        elif args.action == "check":
            check(args, paths, selection, report)
        report["ok"] = True
        return report, 0
    except KeyboardInterrupt:
        report.pop("planId", None)
        report["summary"] = "Capability operation cancelled."
        report["errors"].append("Cancelled; partial files may remain. For MCP migration inspect the private rollback manifest before recovery; other scaffolds never replace existing files.")
        return report, 130
    except (OSError, ValueError, RuntimeError, TypeError) as exc:
        report.pop("planId", None)
        report["summary"] = "Capability operation failed safely."
        report["errors"].append(str(exc) if isinstance(exc, SetupError) else
                                "Cannot safely inspect or create the selected paths. Check ownership, symlinks, permissions and context; no raw configuration or child output is disclosed.")
        if args.action == "apply":
            report["warnings"].append("An interrupted operation may leave newly created files; inspect before retrying. MCP migration may also have changed profile settings: use its private backup manifest for hash-checked rollback. Other scaffolds never replace targets.")
        return report, 1


def encoded_report(report):
    def bounded(value):
        if isinstance(value, str):
            return len(value) <= 4096
        if isinstance(value, list):
            return len(value) <= 40 and all(bounded(x) for x in value)
        if isinstance(value, dict):
            return all(bounded(k) and bounded(v) for k, v in value.items())
        return True
    encoded = json.dumps(report, sort_keys=True, ensure_ascii=True)
    if not bounded(report) or len(encoded.encode()) > 65536:
        failed = result(report["component"], report["action"])
        failed["errors"] = ["Capability report exceeds its output bounds; no output was truncated or disclosed."]
        return json.dumps(failed, sort_keys=True), False
    return encoded, report["ok"]


def main(argv, lock, read_state=None):
    report = result(argv[0] if argv and argv[0] in COMPONENTS else "diagnostics",
                    argv[1] if len(argv) > 1 and argv[1] in {"plan", "apply", "check"} else "plan")

    class Parser(argparse.ArgumentParser):
        def error(self, _message):
            raise SetupError("Invalid capability arguments; use pi-shared capability --help.")

    parser = Parser(prog="pi-shared capability", description=__doc__)
    parser.add_argument("component", choices=COMPONENTS)
    parser.add_argument("action", choices=("plan", "apply", "check"))
    parser.add_argument("--json", action="store_true")
    for name in ("project", "agent-dir", "shared-root", "node-executable"):
        parser.add_argument("--" + name, required=True, metavar="ABS")
    parser.add_argument("--options", default="{}", metavar="JSON")
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--expected-plan", metavar="HEX")
    try:
        args = parser.parse_args(argv)
        if args.action != "apply" and (args.yes or args.expected_plan):
            raise SetupError("Approval flags are only valid for apply.")

        def cancel(_signum, _frame):
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            raise KeyboardInterrupt

        previous = signal.signal(signal.SIGTERM, cancel)
        try:
            report, code = operate(args, lock, read_state)
        finally:
            signal.signal(signal.SIGTERM, previous)
    except SetupError as exc:
        report["errors"].append(str(exc))
        code = 2
    encoded, ok = encoded_report(report)
    if not ok and code == 0:
        code = 1
    print(encoded if "--json" in argv else json.loads(encoded)["summary"])
    return code
