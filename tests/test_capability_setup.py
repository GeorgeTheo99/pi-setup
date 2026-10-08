"""Disposable capability contract tests; no installers, real config or live probes."""
from contextlib import contextmanager
import importlib.util
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
spec = importlib.util.spec_from_file_location("capability_setup", ROOT / "lib/capability_setup.py")
c = importlib.util.module_from_spec(spec)
spec.loader.exec_module(c)


@contextmanager
def lock():
    yield


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    home, project, shared, agent = [root / s for s in ("home", "project", "shared ' checkout", "profile")]
    for path in (home, project, shared, agent):
        path.mkdir(mode=0o700)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("PI_SOFTWARE_KB_ROOT", raising=False)
    node = root / "node"
    node.write_text("not executed")
    node.chmod(0o700)
    (project / "src").mkdir()
    (project / "src/a.ts").write_text("export const a = 1;")
    kb = shared / "knowledge/software-engineering"
    (kb / "corpus").mkdir(parents=True)
    for relative in c.KB_FILES:
        (kb / relative).write_text("{\"public\": true}\n" if relative.endswith(".json") else "Public source cards\n")
    (kb / "private").mkdir()
    (kb / "private/index.json").write_text("SECRET PRIVATE CONTENT")
    (kb / "corpus/private.pdf").write_text("SECRET PDF")
    base = shared / "extensions/code-intel"
    packages = {"": {"dependencies": {"typescript": "5.9.3", "typescript-language-server": "5.0.0"}}}
    for name, version, cli in (("typescript", "5.9.3", "tsserver.js"), ("typescript-language-server", "5.0.0", "cli.mjs")):
        package = base / "node_modules" / name
        (package / "lib").mkdir(parents=True)
        (package / "package.json").write_text(json.dumps({"version": version}))
        (package / "lib" / cli).write_text("not executed")
        packages["node_modules/" + name] = {"version": version}
    (base / "package-lock.json").write_text(json.dumps({"packages": packages}))
    return SimpleNamespace(home=home, project=project, shared=shared, agent=agent, node=node)


def args(fixture, component="development", action="plan", options=None, **kw):
    return SimpleNamespace(**{"component": component, "action": action, "project": str(fixture.project),
        "shared_root": str(fixture.shared), "agent_dir": str(fixture.agent), "node_executable": str(fixture.node),
        "options": json.dumps(options or {}), "yes": False, "expected_plan": None} | kw)


def operate(fixture, component="development", action="plan", options=None, **kw):
    return c.operate(args(fixture, component, action, options, **kw), lock)


def approved(fixture, component="development", options=None):
    report, code = operate(fixture, component, options=options)
    assert code == 0 and "planId" in report, report
    return operate(fixture, component, "apply", options, yes=True, expected_plan=report["planId"])


def snapshot(root):
    return sorted((str(p.relative_to(root)), p.stat().st_mode,
                   p.read_bytes() if p.is_file() else None) for p in root.rglob("*"))


def contract(report, code):
    assert report["schemaVersion"] == 1 and report["component"] in c.COMPONENTS
    assert report["action"] in ("plan", "apply", "check")
    assert report["ok"] == (code == 0)
    assert report["status"] in ("unknown", "not-installed", "needs-configuration", "configured-untested", "verified")
    for key in ("actions", "warnings", "errors", "nextSteps", "evidence", "handoffs"):
        assert isinstance(report[key], list) and len(report[key]) <= 40
    assert all(set(e) == {"label", "value"} for e in report["evidence"])
    assert all(set(h) == {"label", "command", "kind"} and h["kind"] in ("terminal", "pi") for h in report["handoffs"])
    encoded, ok = c.encoded_report(report)
    assert len(encoded.encode()) <= 65536 and ok == report["ok"]
    if "planId" in report:
        assert len(report["planId"]) == 64 and int(report["planId"], 16) >= 0


@pytest.mark.parametrize("component", c.COMPONENTS)
def test_all_plans_offline_and_readonly(fixture, monkeypatch, component):
    before = snapshot(fixture.home.parent)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("plan spawned process"))
    monkeypatch.setattr(c, "exclusive_file", lambda *a, **k: pytest.fail("plan wrote files"))
    report, code = operate(fixture, component)
    contract(report, code)
    assert code == 0, report
    assert snapshot(fixture.home.parent) == before
    assert ("planId" in report) == (component in ("development", "knowledge"))


@pytest.mark.parametrize("component,options", [
    ("mcp", {"mode": "default"}), ("documents", {"x": 1}), ("apple", {"command": "touch"}),
    ("diagnostics", {"probeImports": True}), ("models", {"mode": "arbitrary"}),
    ("browser", {"mode": []}), ("search", {"mode": "local"}),
    ("search", {"mode": "guided", "keyFile": "/tmp/key"}),
    ("search", {"mode": "existing", "url": "https://secret:token@example.org"}),
    ("search", {"mode": "existing", "url": "https://example.org?token=TOPSECRET"}),
    ("search", {"mode": "existing", "url": "http://example.org", "keyFile": "/tmp/key"}),
    ("search", {"mode": "local", "keyFile": "TOPSECRET"}),
    ("search", {"mode": "local", "keyFile": "/k", "decodoKeyFile": "TOPSECRET"}),
    ("search", {"mode": "guided", "decodoKeyFile": "/tmp/TOPSECRET"}),
    ("development", {"mode": "code-intel", "command": "oops"}),
    ("development", {"mode": "verification", "command": "node"}),
    ("development", {"mode": "verification", "command": "node", "args": [], "inputs": ["../escape"]}),
    ("development", {"mode": "verification", "command": "node", "args": [], "inputs": ["*.ts"]}),
    ("knowledge", {"root": "relative"}),
])
def test_options_rejected_without_echo(fixture, component, options):
    report, code = operate(fixture, component, options=options)
    contract(report, code)
    assert code != 0 and "TOPSECRET" not in json.dumps(report) and "planId" not in report


@pytest.mark.parametrize("raw", ["[]", "null", "{\"mode\":\"native\",\"mode\":\"native\"}", "{\"x\":NaN}", "{bad", "{}" * 20000])
def test_invalid_json(fixture, raw):
    request = args(fixture, "models")
    request.options = raw
    report, code = c.operate(request, lock)
    assert code != 0 and "planId" not in report


def test_codegen_exact_lock_paths_and_no_trust(fixture):
    report, code = approved(fixture)
    contract(report, code)
    assert code == 0, report
    path = fixture.project / ".pi/code-intel.json"
    data = json.loads(path.read_text())
    assert data == {"version": 1, "adapter": "typescript-language-server", "workspace": ".",
        "executable": str(fixture.node), "args": [str(fixture.shared / "extensions/code-intel/node_modules/typescript-language-server/lib/cli.mjs"), "--stdio"],
        "tsserverPath": str(fixture.shared / "extensions/code-intel/node_modules/typescript/lib/tsserver.js"), "timeoutMs": 15000}
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert not (fixture.agent / "trust.json").exists() and "planId" not in report
    before = path.stat().st_ino, path.read_bytes()
    report, code = operate(fixture)
    assert code == 0 and "planId" not in report
    report, code = operate(fixture, action="apply", yes=True, expected_plan="a" * 64)
    assert code != 0 and (path.stat().st_ino, path.read_bytes()) == before


def test_verification_explicit_and_no_test_claims(fixture, monkeypatch):
    monkeypatch.setattr(c, "run_bounded", lambda *a, **k: pytest.fail("executed project command"))
    options = {"mode": "verification", "command": "npm", "args": ["run", "test"], "inputs": ["src"]}
    report, code = approved(fixture, options=options)
    assert code == 0, report
    check = json.loads((fixture.project / ".pi/verification.json").read_text())["checks"][0]
    assert check == {"id": "project-check", "command": "npm", "args": ["run", "test"], "cwd": ".",
        "timeoutSeconds": 120, "inputs": {"paths": ["src"], "exclude": [], "untracked": "include"}, "report": {"format": "exit"}}
    report, code = operate(fixture, options=options, action="check")
    assert code == 0 and report["status"] == "configured-untested"


def test_missing_dependency_handoff_no_scaffold(fixture):
    (fixture.shared / "extensions/code-intel/node_modules/typescript/lib/tsserver.js").unlink()
    report, code = operate(fixture)
    assert code == 0 and "planId" not in report
    assert "npm ci --ignore-scripts" in report["handoffs"][0]["command"]
    assert not (fixture.project / ".pi").exists()


def test_mismatched_lock_rejected(fixture):
    package = fixture.shared / "extensions/code-intel/node_modules/typescript/package.json"
    package.write_text('{"version":"0.0.0"}')
    assert operate(fixture)[1] != 0


@pytest.mark.parametrize("change", ["target", "parent", "node", "dependency", "lock"])
def test_stale_approval(fixture, change):
    report, code = operate(fixture)
    assert code == 0
    if change == "target":
        (fixture.project / ".pi").mkdir()
        (fixture.project / ".pi/code-intel.json").write_text("DO NOT OVERWRITE")
    elif change == "parent":
        (fixture.project / ".pi").mkdir()
    elif change == "node":
        fixture.node.write_text("changed")
    elif change == "dependency":
        (fixture.shared / "extensions/code-intel/node_modules/typescript/lib/tsserver.js").write_text("changed")
    else:
        path = fixture.shared / "extensions/code-intel/package-lock.json"
        path.write_text(path.read_text() + "\n")
    output, code = operate(fixture, action="apply", yes=True, expected_plan=report["planId"])
    assert code != 0 and not output["ok"]
    if change == "target":
        assert (fixture.project / ".pi/code-intel.json").read_text() == "DO NOT OVERWRITE"
    else:
        assert not (fixture.project / ".pi/code-intel.json").exists()


def test_revalidate_under_lock(fixture):
    plan, _ = operate(fixture)
    @contextmanager
    def racing_lock():
        (fixture.project / ".pi").mkdir()
        yield
    report, code = c.operate(args(fixture, action="apply", yes=True, expected_plan=plan["planId"]), racing_lock)
    assert code != 0 and "changed" in report["errors"][0]
    assert not (fixture.project / ".pi/code-intel.json").exists()


@pytest.mark.parametrize("target", ["project", "pi", "file", "node", "input"])
def test_symlink_refusal(fixture, target):
    opts = None
    if target == "project":
        alias = fixture.project.parent / "alias"
        alias.symlink_to(fixture.project)
        fixture.project = alias
    elif target == "pi":
        (fixture.project / ".pi").symlink_to(fixture.agent)
    elif target == "file":
        (fixture.project / ".pi").mkdir()
        (fixture.project / ".pi/code-intel.json").symlink_to(fixture.agent / "missing.json")
    elif target == "node":
        fixture.node.unlink()
        fixture.node.symlink_to(sys.executable)
    else:
        (fixture.project / "link").symlink_to(fixture.agent)
        opts = {"mode": "verification", "command": "node", "args": [], "inputs": ["link"]}
    assert operate(fixture, options=opts)[1] != 0


def test_unsafe_project_scope_and_existing_config_preserved(fixture):
    actual = fixture.project
    fixture.project = fixture.home
    assert operate(fixture)[1] != 0
    fixture.project = actual
    (actual / ".pi").mkdir()
    path = actual / ".pi/code-intel.json"
    path.write_text("untrusted malformed config SECRET")
    before = snapshot(actual)
    report, code = operate(fixture)
    assert code == 0 and "SECRET" not in json.dumps(report) and "planId" not in report
    assert snapshot(actual) == before


def test_private_kb_only_allowlisted_public_files(fixture):
    root = fixture.home / "private kb"
    opts = {"root": str(root)}
    report, code = approved(fixture, "knowledge", opts)
    assert code == 0, report
    files = {str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()}
    assert files == set(c.KB_FILES)
    assert (root / "corpus/pdf-downloads").is_dir() and (root / "private").is_dir()
    for path in [root, *root.rglob("*")]:
        assert path.stat().st_mode & 0o777 == (0o700 if path.is_dir() else 0o600)
        if path.is_file():
            assert b"SECRET" not in path.read_bytes()
    before = snapshot(root)
    assert "planId" not in operate(fixture, "knowledge", options=opts)[0]
    assert operate(fixture, "knowledge", "apply", opts, yes=True, expected_plan="a" * 64)[1] != 0
    assert snapshot(root) == before


def test_kb_external_root_and_stale_public_source(fixture):
    assert operate(fixture, "knowledge", options={"root": str(fixture.project / "kb")})[1] != 0
    plan, code = operate(fixture, "knowledge")
    assert code == 0
    (fixture.shared / "knowledge/software-engineering/corpus/source-cards.md").write_text("changed")
    assert operate(fixture, "knowledge", "apply", yes=True, expected_plan=plan["planId"])[1] != 0
    assert not (fixture.home / ".pi/knowledge").exists()


def test_kb_env_fallback_and_symlink(fixture, monkeypatch):
    monkeypatch.setenv("PI_SOFTWARE_KB_ROOT", "relative")
    plan, code = operate(fixture, "knowledge")
    assert code == 0 and any("ignored" in x for x in plan["warnings"])
    root = fixture.home / "alias"
    root.symlink_to(fixture.agent)
    assert operate(fixture, "knowledge", options={"root": str(root)})[1] != 0


def test_handoffs_quote_and_use_actual_flags(fixture):
    key = str(fixture.home / "a ' key; $(touch bad)")
    report, code = operate(fixture, "search", options={"mode": "local", "keyFile": key})
    assert code == 0
    assert shlex.split(report["handoffs"][0]["command"]) == ["pi-shared", "setup", "--search", "local", "--brave-key-file", key]
    report, code = operate(fixture, "search", options={"mode": "existing", "url": "https://example.org/mcp", "keyFile": key})
    assert "--search-key-file" in shlex.split(report["handoffs"][0]["command"])
    report, code = operate(fixture, "search", options={"mode": "local", "keyFile": key, "decodoKeyFile": key + "d"})
    assert code == 0
    assert shlex.split(report["handoffs"][0]["command"])[-2:] == ["--decodo-key-file", key + "d"]
    assert operate(fixture, "search", options={"mode": "existing", "url": "https://example.org/mcp",
                                                "decodoKeyFile": key})[1] != 0
    report, _ = operate(fixture, "browser", options={"mode": "app"})
    command = report["handoffs"][0]["command"]
    assert "npx" not in command and "npm ci --ignore-scripts" in command
    assert shlex.split(command)[1] == str(fixture.shared / "extensions/pi-browser-capture")
    assert "node_modules/playwright/cli.js" in command
    for component, modes in c.MODES.items():
        for mode in modes:
            if component in ("search", "development", "knowledge"):
                continue
            report, _ = operate(fixture, component, options={"mode": mode})
            assert all("--yes" not in h["command"] for h in report["handoffs"])


def test_mcp_declaration_no_credentials_or_claim_loaded(fixture):
    path = fixture.agent / "settings.json"
    path.write_text(json.dumps({"packages": [{"source": "npm:pi-mcp-adapter@1.2.3", "extensions": []}], "apiKey": "NEVERPRINT"}))
    before = path.read_bytes()
    report, code = operate(fixture, "mcp")
    assert code == 0 and report["status"] == "needs-configuration"
    assert "NEVERPRINT" not in json.dumps(report)
    assert "current-session readiness" in " ".join(report["warnings"])
    assert shlex.split(report["handoffs"][0]["command"]) == ["env", "PI_CODING_AGENT_DIR=" + str(fixture.agent), "pi", "mcp", "list"]
    assert path.read_bytes() == before


@pytest.mark.parametrize("component", ["search", "browser", "mcp", "documents", "apple", "models", "diagnostics"])
def test_unsupported_apply_is_error(fixture, component):
    report, code = operate(fixture, component, "apply", yes=True, expected_plan="a" * 64)
    assert code != 0 and "No supported apply" in report["errors"][0]


def test_approval_required_and_check_no_project_execution(fixture, monkeypatch):
    plan, _ = operate(fixture)
    assert operate(fixture, action="apply", expected_plan=plan["planId"])[1] != 0
    assert operate(fixture, action="apply", yes=True)[1] != 0
    monkeypatch.setattr(c, "run_bounded", lambda *a, **k: pytest.fail("unapproved probe"))
    for component in ("search", "browser", "mcp", "development", "knowledge", "models"):
        report, code = operate(fixture, component, "check")
        assert code == 0 and "planId" not in report and report["status"] != "verified"


def test_apple_probes_bounded_and_output_not_reflected(fixture, monkeypatch):
    monkeypatch.setattr(c.platform, "system", lambda: "Darwin")
    developer = fixture.home / "Xcode.app/Contents/Developer"
    developer.mkdir(parents=True)
    commands = []
    def probe(command, **kw):
        commands.append(command)
        if command[0].endswith("xcode-select"):
            return 0, str(developer).encode(), b"SECRET"
        if command[0].endswith("xcodebuild"):
            return 0, b"Xcode 26.0\nBuild version secret\n", b"SECRET"
        return 0, json.dumps({"devices": {"secret-runtime": [{"isAvailable": True, "name": "SECRET"}]}}).encode(), b"SECRET"
    monkeypatch.setattr(c, "run_bounded", probe)
    report, code = operate(fixture, "apple", "check")
    assert code == 0 and report["status"] == "verified" and "SECRET" not in json.dumps(report)
    assert commands == [["/usr/bin/xcode-select", "-p"], ["/usr/bin/xcodebuild", "-version"], ["/usr/bin/xcrun", "simctl", "list", "devices", "available", "-j"]]


def test_diagnostics_only_static_no_raw_output(fixture, monkeypatch):
    for relative in ("bin/pi-doctor", "bin/pi-profile-check", "bin/pi-browser-check", "bin/pi-shared-check-deps", "extensions/dev-doctor/doctor_common.py"):
        path = fixture.shared / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not executed")
    calls = []
    def probe(command, **kw):
        calls.append(([str(x) for x in command], kw))
        return 0, json.dumps({"schema_version": 1, "capabilities": [
            {"probe_type": "static", "installed": "yes", "configured": "no", "guidance": "SECRET", "capability": "SECRET"},
            {"probe_type": "static", "capability": "browser_worker", "outcome": "missing", "guidance": "SECRET"},
            {"probe_type": "static", "capability": "models", "outcome": "SECRET"},
        ]}).encode(), b"SECRET"
    monkeypatch.setattr(c, "run_bounded", probe)
    report, code = operate(fixture, "diagnostics", "check")
    assert code == 0 and report["status"] == "verified" and "SECRET" not in json.dumps(report)
    argv, kw = calls[0]
    assert argv[-4:] == ["--agent-dir", str(fixture.agent), "--timeout", "5"] and kw["timeout"] == 20
    assert not any(x.startswith("--probe") for x in argv)
    assert {"label": "Doctor: browser_worker", "value": "missing"} in report["evidence"]
    assert any("/setup browser" in step for step in report["nextSteps"])
    assert {"label": "Doctor: models", "value": "unknown"} in report["evidence"]


def cli_args(fixture, component="development", action="plan"):
    return [component, action, "--json", "--project", str(fixture.project), "--agent-dir", str(fixture.agent),
            "--shared-root", str(fixture.shared), "--node-executable", str(fixture.node)]


def test_real_cli_dispatch_and_invalid_args(fixture):
    result = subprocess.run([sys.executable, ROOT / "bin/pi-shared", "capability", *cli_args(fixture)], capture_output=True, text=True)
    assert result.returncode == 0 and not result.stderr
    contract(json.loads(result.stdout), result.returncode)
    result = subprocess.run([sys.executable, ROOT / "bin/pi-shared", "capability", "models", "plan", "--json", "--SECRET"], capture_output=True, text=True)
    assert result.returncode == 2 and not result.stderr and "SECRET" not in result.stdout
    contract(json.loads(result.stdout), result.returncode)


@pytest.mark.parametrize("kind", ["shared-write", "hardlink", "fifo", "lock-symlink"])
def test_unsafe_targets_and_lock_refused(fixture, kind):
    target = fixture.project / ".pi/code-intel.json"
    target.parent.mkdir()
    if kind == "shared-write":
        target.parent.chmod(0o777)
    elif kind == "hardlink":
        source = fixture.home / "original"
        source.write_text("DO NOT OVERWRITE")
        os.link(source, target)
    elif kind == "fifo":
        os.mkfifo(target)
    else:
        plan, code = operate(fixture)
        assert code == 0
        lockfile = fixture.home / ".config/pi-shared/update.lock"
        lockfile.parent.mkdir(parents=True)
        lockfile.symlink_to(fixture.home / "absent")
        report, code = operate(fixture, action="apply", yes=True, expected_plan=plan["planId"])
        assert code != 0 and not target.exists()
        return
    report, code = operate(fixture)
    assert code != 0 and "planId" not in report


def test_exclusive_creation_race_never_overwrites(fixture, monkeypatch):
    plan, _ = operate(fixture)
    original = c.exclusive_file
    def racing_create(path, payload):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("CONCURRENT EDIT")
        original(path, payload)
    monkeypatch.setattr(c, "exclusive_file", racing_create)
    report, code = operate(fixture, action="apply", yes=True, expected_plan=plan["planId"])
    assert code != 0 and (fixture.project / ".pi/code-intel.json").read_text() == "CONCURRENT EDIT"


def test_ingest_handoff_never_copies_private_sources(fixture, monkeypatch):
    monkeypatch.setattr(c, "run_bounded", lambda *a, **k: pytest.fail("ingestion executed"))
    opts = {"mode": "ingest", "root": str(fixture.home / "custom ' kb")}
    before = snapshot(fixture.home.parent)
    report, code = operate(fixture, "knowledge", options=opts)
    assert code == 0 and "planId" not in report
    command = next(h["command"] for h in report["handoffs"] if "--private" in h["command"])
    assert shlex.split(command) == ["python3", str(fixture.shared / "extensions/software-kb/ingest.py"), "--private", "--root", opts["root"]]
    assert snapshot(fixture.home.parent) == before
    assert operate(fixture, "knowledge", "apply", opts, yes=True, expected_plan="a" * 64)[1] != 0


def test_apple_clt_stops_before_build_or_simulator(fixture, monkeypatch):
    monkeypatch.setattr(c.platform, "system", lambda: "Darwin")
    calls = []
    def probe(command, **kw):
        calls.append(command)
        return 0, b"/Library/Developer/CommandLineTools\n", b"NEVERPRINT"
    monkeypatch.setattr(c, "run_bounded", probe)
    report, code = operate(fixture, "apple", "check")
    assert code != 0 and len(calls) == 1 and "NEVERPRINT" not in json.dumps(report)


def test_cli_cancellation_return_and_output_bounds(fixture, monkeypatch, capsys):
    def cancel(*a):
        raise KeyboardInterrupt
    monkeypatch.setattr(c, "inspect", cancel)
    assert c.main(cli_args(fixture), lock) == 130
    report = json.loads(capsys.readouterr().out)
    contract(report, 130)
    assert "planId" not in report
    oversized = c.result("models", "plan")
    oversized["handoffs"] = [{"label": "x", "command": "x" * 4097, "kind": "terminal"}]
    encoded, ok = c.encoded_report(oversized)
    assert not ok and "exceeds" in encoded and "x" * 4097 not in encoded


def test_plan_previews_exact_project_bytes_and_private_root(fixture):
    report, code = operate(fixture)
    assert code == 0
    preview = "".join(row["value"] for row in report["evidence"] if row["label"].startswith("Proposed JSON"))
    assert json.loads(preview)["executable"] == str(fixture.node)
    assert next(row["value"] for row in report["evidence"] if row["label"] == "Destination") == str(fixture.project / ".pi/code-intel.json")
    report, code = operate(fixture, "knowledge")
    assert code == 0
    assert next(row["value"] for row in report["evidence"] if row["label"] == "Knowledge root") == str(fixture.home / ".pi/knowledge/software-engineering")


def test_managed_dependencies_never_offer_npm_ci_inside_installation(fixture):
    managed = fixture.home / ".local/share/pi-shared/modules/pi-shared"
    managed.parent.mkdir(parents=True)
    fixture.shared.rename(managed)
    fixture.shared = managed
    (managed / "extensions/code-intel/node_modules/typescript/lib/tsserver.js").unlink()
    for component, options in [("development", {}), ("browser", {"mode": "app"})]:
        report, code = operate(fixture, component, options=options)
        assert code == 0
        commands = [h["command"] for h in report["handoffs"]]
        assert "pi-shared update" in commands
        assert not any("npm ci" in command for command in commands)


def test_home_launch_can_initialize_default_kb_but_managed_roots_are_excluded(fixture):
    fixture.project = fixture.home
    report, code = operate(fixture, "knowledge")
    assert code == 0 and "planId" in report
    for root in (fixture.home / ".local/share/pi-shared/private-kb", fixture.shared / "kb"):
        report, code = operate(fixture, "knowledge", options={"root": str(root)})
        assert code != 0 and "planId" not in report


def test_custom_receipt_ownership_controls_handoffs_and_scaffolds(fixture):
    custom = fixture.home / "custom-modules"
    custom.mkdir()
    fixture.shared.rename(custom / "pi-shared")
    fixture.shared = custom / "pi-shared"
    receipt = fixture.home / ".config/pi-shared/setup.json"
    receipt.parent.mkdir(parents=True)
    receipt.write_text('{"fixture":true}')
    receipt.chmod(0o600)
    calls = []
    def reader(**kwargs):
        calls.append(kwargs)
        return {"code_root": str(custom)}
    report, code = c.operate(args(fixture, "browser", options={"mode": "app"}), lock, reader)
    assert code == 0 and calls == [{"allow_incomplete": True}]
    commands = [row["command"] for row in report["handoffs"]]
    assert commands[:2] == ["pi-shared update --plan", "pi-shared update"]
    assert not any("npm ci" in command for command in commands)
    assert any("applies immediately" in warning and "restart" in warning for warning in report["warnings"])
    report, code = c.operate(args(fixture, "knowledge", options={"root": str(custom / "books")}), lock, reader)
    assert code != 0 and "planId" not in report
    fixture.project = fixture.shared
    report, code = c.operate(args(fixture, "development"), lock, reader)
    assert code != 0
    report, code = c.operate(args(fixture, "browser"), lock)
    assert code != 0, "saved ownership must not be guessed without its validator"


def test_homebrew_node_ancestor_exception_is_narrow(monkeypatch):
    monkeypatch.setattr(c.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(c.os, "getgroups", lambda: [20, 80])
    node = Path("/opt/homebrew/Cellar/node/26.10.0/bin/node")
    info = SimpleNamespace(st_gid=80, st_mode=0o775)
    assert c.homebrew_node_ancestor(node, Path("/opt/homebrew/Cellar"), info)
    assert c.homebrew_node_ancestor(node, Path("/opt/homebrew"), info)
    assert not c.homebrew_node_ancestor(node, node.parent, info)
    assert not c.homebrew_node_ancestor(Path("/tmp/node"), Path("/opt/homebrew/Cellar"), info)
    assert not c.homebrew_node_ancestor(node, Path("/opt/homebrew/Cellar"), SimpleNamespace(st_gid=80, st_mode=0o777))
    assert not c.homebrew_node_ancestor(node, Path("/opt/homebrew/Cellar"), SimpleNamespace(st_gid=20, st_mode=0o775))
    monkeypatch.setattr(c.os, "getgroups", lambda: [20])
    assert not c.homebrew_node_ancestor(node, Path("/opt/homebrew/Cellar"), info)
