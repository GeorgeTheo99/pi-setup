"""Native migration fixtures only: no live profile, server, auth or npm execution."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import capability_setup as c
import mcp_setup as m
import peekaboo_setup as p


@contextmanager
def lock():
    yield


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    home, project, shared, agent = [root / name for name in ("home", "project", "shared", "agent")]
    for path in (home, project, shared, agent):
        path.mkdir(mode=0o700)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("PI_CODING_AGENT_DIR", raising=False)
    monkeypatch.setattr(p, "discover", lambda: None)
    monkeypatch.setattr(p.subprocess, "Popen", lambda *a, **kw: pytest.fail("unexpected subprocess"))
    source = home / ".config/mcp/mcp.json"
    source.parent.mkdir(parents=True)
    source.write_text(json.dumps({"mcpServers": {"peekaboo": p.entry("/safe/peekaboo", "bridge", "/safe/bridge.sock"),
        "docs": {"url": "https://example.invalid/mcp", "headers": {"Authorization": "Bearer SECRET"},
                 "requestTimeoutMs": 1250, "includeTools": ["get", "delete", "admin_delete"], "excludeTools": ["*delete*"]},
        "disabled": {"command": "node", "args": ["server.js"], "disabled": True}}}))
    settings = {"model": "preserved", "secretUnknown": "SECRET", "extensions": ["-builtin:mcp", "+/safe/pi-mcp-adapter/index.ts", "-other.ts", "-other.ts"],
                "packages": ["npm:other", "npm:pi-mcp-adapter@1.2.3", {"source": "npm:pi-mcp-adapter@2", "skills": [], "unknown": "preserved"}]}
    (agent / "settings.json").write_text(json.dumps(settings))
    return SimpleNamespace(home=home, project=project, shared=shared, agent=agent, source=source, settings=settings)


def run(f, action="plan", opts=None, **kw):
    args = SimpleNamespace(component="mcp", action=action, project=str(f.project), agent_dir=str(f.agent),
                           shared_root=str(f.shared), node_executable="/unused/node", options=json.dumps(opts if opts is not None else {"mode": "migrate"}),
                           yes=kw.pop("yes", False), expected_plan=kw.pop("expected_plan", None))
    return c.operate(args, kw.pop("lock", lock))


def modify(f, change):
    data = json.loads(f.source.read_bytes())
    change(data)
    f.source.write_text(json.dumps(data))


def approve(f):
    report, code = run(f)
    assert code == 0, report
    return run(f, "apply", yes=True, expected_plan=report["planId"])


def test_offline_plan_digest_and_no_secret_output(fixture):
    f = fixture
    before = {str(p): p.read_bytes() for p in f.home.parent.rglob("*") if p.is_file()}
    a, code = run(f)
    b, _ = run(f)
    assert code == 0 and a["planId"] == b["planId"]
    assert "SECRET" not in json.dumps(a)
    assert "startup" in " ".join(a["warnings"]) and "resources are hidden" in " ".join(a["warnings"])
    assert before == {str(p): p.read_bytes() for p in f.home.parent.rglob("*") if p.is_file()}
    assert c.encoded_report(a)[1]


def test_apply_preserves_source_unknown_settings_credentials_and_rollback(fixture):
    f = fixture
    original = f.source.read_bytes()
    old_settings = (f.agent / "settings.json").read_bytes()
    report, code = approve(f)
    assert code == 0, report
    assert "planId" not in report and "SECRET" not in json.dumps(report)
    assert f.source.read_bytes() == original
    native = json.loads((f.agent / "mcp.json").read_bytes())["mcpServers"]
    assert native["peekaboo"] == p.entry("/safe/peekaboo", "bridge", "/safe/bridge.sock", "native")
    assert native["docs"]["timeout"] == 1.25
    assert native["docs"]["headers"]["Authorization"] == "Bearer SECRET"
    assert native["docs"]["exposure"] == "hidden" and native["docs"]["toolExposure"] == {"get": "codemode"}
    assert native["disabled"]["enabled"] is False
    settings = json.loads((f.agent / "settings.json").read_bytes())
    assert settings["secretUnknown"] == "SECRET" and settings["model"] == "preserved"
    assert settings["extensions"] == ["-/safe/pi-mcp-adapter/index.ts", "-other.ts", "-other.ts", "+builtin:mcp"]
    assert settings["packages"] == ["npm:other", {"source": "npm:pi-mcp-adapter@1.2.3", **{key: [] for key in m.ADAPTER_RESOURCES}},
        {"source": "npm:pi-mcp-adapter@2", "unknown": "preserved", **{key: [] for key in m.ADAPTER_RESOURCES}}]
    manifest_path = next((f.agent / ".mcp-migration").glob("*/manifest.json"))
    manifest = json.loads(manifest_path.read_bytes())
    assert manifest_path.parent.stat().st_mode & 0o777 == 0o700
    for record in manifest["files"]:
        path = Path(record["path"])
        assert hashlib.sha256(path.read_bytes()).hexdigest() == record["afterSha256"]
        if record["backup"]:
            backup = manifest_path.parent / record["backup"]
            assert backup.stat().st_mode & 0o777 == 0o600
            assert hashlib.sha256(backup.read_bytes()).hexdigest() == record["beforeSha256"]
        if record["changed"]:
            assert path.stat().st_mode & 0o777 == 0o600
            # Exercise the documented conditional rollback in isolated fixtures.
            if record["backup"]:
                path.write_bytes((manifest_path.parent / record["backup"]).read_bytes())
            else:
                path.unlink()
    assert (f.agent / "settings.json").read_bytes() == old_settings
    assert not (f.agent / "mcp.json").exists()
    assert f.source.read_bytes() == original


@pytest.mark.parametrize("change", [
    lambda d: d["mcpServers"]["docs"].update(excludeTools=["delete*"], includeTools=[]),
    lambda d: d["mcpServers"]["docs"].update(includeTools=["get*"], excludeTools=["delete*"]),
    lambda d: d["mcpServers"]["docs"].update(includeTools=["get?"]),
    lambda d: d["mcpServers"]["docs"].update(type="sse"),
    lambda d: d["mcpServers"]["docs"].update(protocolVersion="auto"),
    lambda d: d["mcpServers"]["docs"].update(approveTools=["*"]),
    lambda d: d["mcpServers"]["docs"].update(requestTimeoutMs=0),
    lambda d: d["mcpServers"]["docs"].update(directTools=["get"]),
    lambda d: d["mcpServers"]["docs"].update(oauth={"clientId": "SECRET"}),
    lambda d: d["mcpServers"]["docs"].update(headers={"Authorization": "!!SECRET"}),
    lambda d: d.update(settings={"hostConfigDiscovery": "on"}),
    lambda d: d.update(imports=["claude-code"]),
    lambda d: d.update(unknown={"secret": "SECRET"}),
])
def test_ambiguous_policy_refused_without_writes(fixture, change):
    modify(fixture, change)
    report, code = run(fixture)
    assert code == 1 and "planId" not in report and "SECRET" not in json.dumps(report)
    assert not (fixture.agent / "mcp.json").exists()
    assert not (fixture.agent / ".mcp-migration").exists()


def test_explicit_disabled_adapter_policies_migrate_without_enabling_them(fixture):
    modify(fixture, lambda d: d.update(settings={**{key: False for key in m.DISABLED_POLICIES}, "hostConfigDiscovery": "off", "mcpFooterStatus": "compact"}))
    modify(fixture, lambda d: d["mcpServers"]["docs"].update(approveTools=False))
    report, code = approve(fixture)
    assert code == 0, report
    native = json.loads((fixture.agent / "mcp.json").read_bytes())
    assert native["mcpServers"]["docs"]["toolExposure"] == {"get": "codemode"}
    assert "settings" not in native


@pytest.mark.parametrize("policy", sorted(m.DISABLED_POLICIES))
@pytest.mark.parametrize("value", [True, [], 0, None])
def test_nonfalse_adapter_policies_fail_closed(policy, value):
    with pytest.raises(p.SetupError, match="require manual"):
        m.translate({"settings": {policy: value}, "mcpServers": {"s": {"command": "node"}}})


@pytest.mark.parametrize("field", ["env", "headers"])
@pytest.mark.parametrize("value", ["$TOKEN", "Bearer $TOKEN", "$$cash", "$!literal", "${123}", "$env:TOKEN", "{env:TOKEN}", "!!literal", "${TOKEN:-fallback}", "${TOKEN"])
def test_incompatible_secret_interpolation_is_refused(field, value):
    with pytest.raises(p.SetupError, match="interpolation"):
        m.translate({"mcpServers": {"s": {"command": "node", field: {"FIXTURE": value}}}})


@pytest.mark.parametrize("field", ["command", "args", "cwd", "url"])
@pytest.mark.parametrize("value", ["${TOKEN}", "$env:TOKEN", "{env:TOKEN}"])
def test_adapter_only_argument_interpolation_is_refused(field, value):
    definition = {"url": value} if field == "url" else {"command": "node", field: [value] if field == "args" else value}
    with pytest.raises(p.SetupError, match="interpolation"):
        m.translate({"mcpServers": {"s": definition}})


@pytest.mark.parametrize("field", ["command", "args", "cwd"])
@pytest.mark.parametrize("value", ["~", "~/private", "~\\private"])
def test_native_home_expansion_is_refused(field, value):
    definition = {"command": "node", field: [value] if field == "args" else value}
    with pytest.raises(p.SetupError, match="interpolation"):
        m.translate({"mcpServers": {"s": definition}})


@pytest.mark.parametrize("value", ["literal", "${TOKEN}", "Bearer ${TOKEN}", "!printf '%s' \"$TOKEN\""])
def test_compatible_secret_references_are_preserved_not_resolved(value, monkeypatch):
    monkeypatch.setenv("TOKEN", "MUST_NOT_BE_READ")
    document = {"mcpServers": {"s": {"command": "node", "env": {"X": value}, "headers": {"Y": value}}}}
    native, _ = m.translate(document)
    assert native["mcpServers"]["s"]["env"] == {"X": value}
    assert native["mcpServers"]["s"]["headers"] == {"Y": value}
    assert "MUST_NOT_BE_READ" not in json.dumps(native)


def test_allowlist_globs_narrow_aliases_and_disable_resources():
    native, warnings = m.translate({"mcpServers": {"s": {"command": "node", "includeTools": ["get*", "s_read"], "directTools": True}}})
    assert native["mcpServers"]["s"]["toolExposure"] == {"get*": "direct", "s_read": "direct"}
    assert native["mcpServers"]["s"]["exposure"] == "hidden"
    assert warnings


@pytest.mark.parametrize("name,deny", [("read-file", "read_file"), ("read.file", "read_file"), ("read", "mcp__foo_2d_mcp_read"), ("read", "foo_read")])
def test_excluded_legacy_alias_never_exposed(name, deny):
    native, _ = m.translate({"mcpServers": {"foo-mcp": {"command": "node", "includeTools": [name], "excludeTools": [deny]}}})
    assert native["mcpServers"]["foo-mcp"]["toolExposure"] == {}


@pytest.mark.parametrize("path", ["source", "settings", "target", "project"])
def test_stale_digest_rejects_all_input_changes(fixture, path):
    f = fixture
    report, _ = run(f)
    if path == "source":
        modify(f, lambda d: d["mcpServers"]["disabled"].update(disabled=False))
    elif path == "settings":
        (f.agent / "settings.json").write_text('{"packages": []}')
    elif path == "target":
        (f.agent / "mcp.json").write_text("{}")
    else:
        (f.project / ".mcp.json").write_text("{}")
    failed, code = run(f, "apply", yes=True, expected_plan=report["planId"])
    assert code == 1 and not failed["ok"]
    assert not (f.agent / ".mcp-migration").exists()


def test_change_under_lock_refused(fixture):
    report, _ = run(fixture)
    @contextmanager
    def changed():
        modify(fixture, lambda d: d["mcpServers"]["disabled"].update(disabled=False))
        yield
    failed, code = run(fixture, "apply", yes=True, expected_plan=report["planId"], lock=changed)
    assert code == 1 and not (fixture.agent / "mcp.json").exists()


@pytest.mark.parametrize("yes,digest", [(False, None), (True, None), (True, "a" * 64)])
def test_approval_required(fixture, yes, digest):
    _, code = run(fixture, "apply", yes=yes, expected_plan=digest)
    assert code == 1 and not (fixture.agent / "mcp.json").exists()


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "writable", "duplicate"])
def test_unsafe_source_refused(fixture, kind):
    f = fixture
    if kind == "symlink":
        saved = f.source.with_name("saved.json")
        f.source.rename(saved)
        f.source.symlink_to(saved)
    elif kind == "hardlink":
        os.link(f.source, f.source.with_name("alias.json"))
    elif kind == "writable":
        f.source.chmod(0o666)
    else:
        f.source.write_text('{"mcpServers":{},"mcpServers":{}}')
    report, code = run(f)
    assert code == 1 and not report["ok"]
    assert not (f.agent / ".mcp-migration").exists()


def test_partial_failure_keeps_backups_and_concurrent_settings(fixture, monkeypatch):
    def changed(path, data, expected):
        path.write_text('{"concurrent":true}')
        raise p.SetupError("Concurrent settings edit; inspect backups.")
    monkeypatch.setattr(m, "replace_file", changed)
    report, code = approve(fixture)
    assert code == 1 and (fixture.agent / "mcp.json").exists()
    assert json.loads((fixture.agent / "settings.json").read_bytes()) == {"concurrent": True}
    assert next((fixture.agent / ".mcp-migration").glob("*/manifest.json")).exists()
    assert any(row["label"] == "Rollback backup directory" for row in report["evidence"])


def test_existing_native_target_and_extra_layers_refused(fixture):
    f = fixture
    (f.agent / "mcp.json").write_text('{"unknown":"SECRET"}')
    _, code = run(f)
    assert code == 1 and (f.agent / "mcp.json").read_text() == '{"unknown":"SECRET"}'
    (f.agent / "mcp.json").unlink()
    (f.project / ".pi").mkdir()
    (f.project / ".pi/settings.json").write_text('{"extensions":["-builtin:mcp"]}')
    _, code = run(f)
    assert code == 1


def test_exclusions_are_idempotent_and_scope_exact():
    original = {"extensions": ["-/safe/pi-mcp-adapter/index.ts", "!**/pi-mcp-adapter/**", "/safe/not-pi-mcp-adapter/index.ts", "+builtin:mcp"],
                "packages": [{"source": "npm:pi-mcp-adapter", **{key: [] for key in m.ADAPTER_RESOURCES}, "unknown": True}]}
    updated, count = m.native_settings(original)
    assert updated == original and count == 0


def pb_args(f, action="plan", **kw):
    return SimpleNamespace(**({"action": action, "config": None, "agent_dir": str(f.agent), "backend": None,
        "binary": None, "mode": None, "bridge_socket": None, "install": False, "yes": False, "expected_plan": None} | kw))


def test_peekaboo_new_profile_defaults_native(fixture, monkeypatch):
    f = fixture
    f.source.unlink()
    (f.agent / "settings.json").write_text("{}")
    binary = f.home / "peekaboo"
    binary.write_text("fixture")
    binary.chmod(0o700)
    report, code = p.operate(pb_args(f, binary=str(binary)), lock)
    assert code == 0, report
    assert "Official Pi 0.99.1" in " ".join(report["warnings"])
    monkeypatch.setattr(p, "startup", lambda *a: p.read_file(binary, p.MAX_EXPANDED, executable=True)[1])
    applied, code = p.operate(pb_args(f, "apply", binary=str(binary), yes=True, expected_plan=report["planId"]), lock)
    assert code == 0, applied
    assert json.loads((f.agent / "mcp.json").read_bytes())["mcpServers"]["peekaboo"] == p.entry(binary, backend="native")
    assert not f.source.exists()


def test_peekaboo_after_migration_uses_native_even_legacy_source_retained(fixture):
    report, code = approve(fixture)
    assert code == 0, report
    report, code = p.operate(pb_args(fixture), lock)
    assert code == 0, report
    assert report["evidence"]["configuration"] == "matching"
    assert "Official Pi 0.99.1" in " ".join(report["warnings"])


def test_peekaboo_legacy_not_silently_switched(fixture):
    report, code = p.operate(pb_args(fixture), lock)
    assert code == 0, report
    assert "Legacy adapter" in " ".join(report["warnings"])
    report, code = p.operate(pb_args(fixture, backend="native"), lock)
    assert code == 1 and "migration" in " ".join(report["errors"])
