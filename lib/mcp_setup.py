"""Offline, profile-scoped migration from pi-mcp-adapter to Pi 0.99.1 MCP.

Never execute servers/credential commands, uninstall packages, or edit the source.
Ambiguous adapter policy fails closed rather than becoming an ignored native key.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import re
import uuid

from peekaboo_setup import SetupError, directory, parse_json, path_value, read_at, read_file

LIMIT = 1024 * 1024
ADAPTER = re.compile(r"npm:pi-mcp-adapter(?:@[^\s/]+)?\Z")
ADAPTER_RESOURCES = ("extensions", "skills", "prompts", "themes")
DISABLED_POLICIES = {"approveTools", "sampling", "elicitation", "autoAuth", "scriptMode"}
# Only the adapter's own entry points, not arbitrary similarly named extensions.
ADAPTER_PATH = re.compile(r"(?:^|/)pi-mcp-adapter(?:\.[cm]?[jt]s|/(?:index\.[cm]?[jt]s|dist/index\.js))?\Z")


def adapter_reference(value):
    return isinstance(value, str) and bool(ADAPTER.fullmatch(value) or ADAPTER_PATH.search(value))


def native_settings(document):
    """Narrow package resources; exclude explicit paths without uninstalling anything."""
    if (not isinstance(document, dict) or not isinstance(document.get("packages", []), list) or
            not isinstance(document.get("extensions", []), list) or
            any(not isinstance(v, str) for v in document.get("extensions", []))):
        raise SetupError("Profile settings must contain valid packages and extensions arrays.")
    updated = copy.deepcopy(document)
    count = 0
    packages = []
    for item in document.get("packages", []):
        source = item.get("source") if isinstance(item, dict) else item
        if adapter_reference(source):
            replacement = copy.deepcopy(item) if isinstance(item, dict) else {"source": item}
            if any(replacement.get(key) != [] for key in ADAPTER_RESOURCES):
                count += 1
            for key in ADAPTER_RESOURCES:
                replacement[key] = []
            packages.append(replacement)
        else:
            packages.append(item)
    if "packages" in document:
        updated["packages"] = packages
    extensions = []
    for value in document.get("extensions", []):
        if value in {"builtin:mcp", "+builtin:mcp", "-builtin:mcp"}:
            continue
        if not value.startswith(("-", "!")) and adapter_reference(value.removeprefix("+")):
            count += 1
            value = "-" + value.removeprefix("+")
        extensions.append(value)
    updated["extensions"] = [*extensions, "+builtin:mcp"]
    return updated, count


def payload(document):
    data = (json.dumps(document, indent=2, ensure_ascii=True, allow_nan=False) + "\n").encode()
    if len(data) > LIMIT:
        raise SetupError("Proposed MCP configuration exceeds 1 MiB.")
    return data


def object_file(path):
    raw, state = read_file(path, LIMIT)
    value = parse_json(raw) if raw is not None else {}
    if not isinstance(value, dict):
        raise SetupError("MCP/profile configuration must be a JSON object.")
    return value, raw, state


def string_list(value):
    if (not isinstance(value, list) or len(value) > 256 or
            any(not isinstance(v, str) or not v or len(v) > 256 for v in value)):
        raise SetupError("MCP tool selectors must be bounded string arrays.")
    return value


def candidates(name, server):
    # Superset of adapter current/legacy aliases: over-hiding is safe; exposing
    # an excluded alias is not. No catalog startup is necessary for finite lists.
    short = re.sub(r"-?mcp$", "", server, flags=re.I) or "mcp"
    def legacy(value):
        return re.sub(r"[^A-Za-z0-9]", lambda m: "_" + format(ord(m[0]), "x") + "_", value)
    prefixes = {server, short, "mcp__" + server, legacy(server), legacy(short), "mcp__" + legacy(server)}
    names = {name, name.replace(".", "_"), name.replace("-", "_"), name.replace(".", "_").replace("-", "_")}
    values = names | {p + "_" + n for p in prefixes for n in names}
    return values | {v.replace("-", "_") for v in values}


def matches(pattern, value):
    return re.fullmatch(re.escape(pattern).replace(r"\*", ".*").replace(r"\?", "."), value) is not None


def exposure(server, definition, direct, warnings):
    if type(direct) is not bool:
        raise SetupError("directTools arrays need manual native exposure review; only boolean defaults migrate automatically.")
    base = "direct" if direct else "codemode"
    includes = string_list(definition.get("includeTools", []))
    excludes = string_list(definition.get("excludeTools", []))
    resources = definition.get("exposeResources", True)
    if type(resources) is not bool:
        raise SetupError("exposeResources must be boolean.")
    if excludes and (not includes or any("*" in v or "?" in v for v in includes)):
        raise SetupError("Deny-only or overlapping wildcard MCP filters require manual review: supply a finite original-name includeTools allowlist before migration; no restrictions were broadened.")
    if any(not re.fullmatch(r"[A-Za-z0-9_.*-]+", v) for v in includes):
        raise SetupError("Unsupported includeTools selector; use original names or star patterns (not question-mark patterns).")
    if includes or excludes or not resources:
        rules = {}
        for name in includes or ["*"]:
            if not any(matches(pattern, alias) for pattern in excludes for alias in candidates(name, server)):
                rules[name] = base
        warnings.append("Filtered servers use hidden server exposure and original-name toolExposure allowlists: prefixed/legacy aliases may narrow access; all resources are hidden to prevent bypassing adapter resource filters.")
        return {"exposure": "hidden", "toolExposure": rules}
    return {"exposure": base}


def compatible_value(key, value):
    """Do not reinterpret adapter literals as native environment/command references.

    Native expands bare $NAME and $$/$! escapes; the adapter does not. Both
    understand canonical ${NAME} in env/headers, but not in argv/URL/path values.
    Command references are copied verbatim, never resolved by migration.
    """
    if value.startswith("!!") or "$env:" in value or "{env:" in value:
        return False
    # Native expands home in command AND every argv entry; the adapter does not.
    # Refuse cwd too: its backslash form is expanded on different platforms.
    if key in {"command", "args", "cwd"} and (value == "~" or value.startswith(("~/", "~\\"))):
        return False
    if key in {"env", "headers"}:
        if value.startswith("!"):
            return True
        remainder = re.sub(r"\$\{[A-Za-z_][A-Za-z0-9_]*\}", "", value)
        return "$" not in remainder
    return "${" not in value and not value.startswith("!")


def translate(document):
    if set(document) - {"mcpServers", "settings"}:
        raise SetupError("Adapter imports or unknown top-level MCP settings require manual migration; source remains untouched.")
    defaults = document.get("settings", {})
    if not isinstance(defaults, dict) or set(defaults) - ({"requestTimeoutMs", "directTools", "toolPrefix", "idleTimeout", "showStatusIcon", "mcpFooterStatus", "notifyOnStartupConnect", "warnOnLargeDirectTools", "hostConfigDiscovery"} | DISABLED_POLICIES):
        raise SetupError("Unsupported global adapter policy (including approvals/plugins/resources) requires manual migration.")
    if any(defaults[key] is not False for key in DISABLED_POLICIES & defaults.keys()):
        raise SetupError("Enabled adapter approvals/sampling/elicitation/autoAuth/script policies require manual migration; only explicit false values are omitted.")
    if defaults.get("hostConfigDiscovery", "off") != "off":
        raise SetupError("Host config discovery must be off before migration; imported servers cannot be silently lost.")
    if defaults.get("toolPrefix", "server") not in {"server", "short", "none", "mcp"}:
        raise SetupError("Unsupported adapter tool prefix.")
    servers = document.get("mcpServers")
    if not isinstance(servers, dict) or not servers or len(servers) > 128:
        raise SetupError("Migration needs a nonempty bounded mcpServers object.")
    warnings = [
        "Native MCP connects enabled servers at session startup, not lazily; idle lifecycle, cache, proxy/script APIs and adapter UI settings are not retained. Approval includes this lifecycle change.",
        "Native names are mcp__SERVER__TOOL; prompts, MCP Apps, sampling/elicitation and adapter permission brokers are not migrated. Review dependent extensions and instructions before restarting.",
        "Native protocol negotiation and progress-reset request timeouts differ; HTTP uses streamable HTTP only, never SSE fallback. No server is started or protocol compatibility verified here.",
        "Resources use native generic resource tools, not adapter generated tools. OAuth/keychain tokens are not read, copied or deleted; sign in separately with pi mcp login if needed."]
    result = {}
    common = {"command", "args", "env", "cwd", "url", "headers"}
    supported = common | {"type", "lifecycle", "idleTimeout", "requestTimeoutMs", "directTools", "disabled", "includeTools", "excludeTools", "exposeResources", "protocolVersion", "toolPrefix", "approveTools"}
    for name, source in servers.items():
        if not re.fullmatch(r"[A-Za-z0-9_-]+", name) or not isinstance(source, dict):
            raise SetupError("Invalid native MCP server name or definition.")
        if set(source) - supported:
            raise SetupError("Unsupported server fields (auth/approval/socket/import/unknown policy) require manual migration; no fields were silently dropped.")
        if "approveTools" in source and source["approveTools"] is not False:
            raise SetupError("Server approval policies require an equivalent native policy; only explicit false migrates automatically.")
        if source.get("protocolVersion", "legacy") != "legacy":
            raise SetupError("Explicit modern/auto adapter protocol negotiation cannot be preserved by native MCP; manual review required.")
        if source.get("lifecycle", "lazy") not in {"lazy", "lazy-keep-alive", "keep-alive", "eager"}:
            raise SetupError("Unsupported adapter lifecycle.")
        if source.get("toolPrefix", "server") not in {"server", "short", "none", "mcp"}:
            raise SetupError("Unsupported adapter tool prefix.")
        stdio = "command" in source
        if stdio == ("url" in source) or source.get("type", "stdio" if stdio else "http") not in ({"stdio"} if stdio else {"http", "streamable-http"}):
            raise SetupError("Use exactly one stdio command or streamable HTTP URL; SSE/socket transport is unsupported.")
        for key in common & source.keys():
            value = source[key]
            values = value if key == "args" else list(value.values()) if isinstance(value, dict) and key in {"env", "headers"} else [value]
            if (key == "args" and not isinstance(value, list) or key in {"env", "headers"} and not isinstance(value, dict) or
                    any(not isinstance(v, str) for v in values)):
                raise SetupError("Invalid MCP command, arguments, environment or headers.")
            # Native interpolation is intentionally narrower. Never reinterpret an
            # adapter escaped command as a command, or expand a different URL.
            if any(not compatible_value(key, v) for v in values):
                raise SetupError("Adapter interpolation/escaped commands/home-prefixed paths are not native-compatible; migrate those values manually.")
        if not source.get("command" if stdio else "url"):
            raise SetupError("MCP command/URL must be nonempty.")
        if not stdio and not re.match(r"https?://", source["url"]):
            raise SetupError("Native MCP requires an HTTP(S) URL.")
        timeout = source.get("requestTimeoutMs", defaults.get("requestTimeoutMs", 60000))
        if type(timeout) not in {int, float} or timeout <= 0 or timeout / 1000 <= 0:
            raise SetupError("Non-positive/invalid adapter timeouts require manual review; native timeout must be positive.")
        if type(source.get("disabled", False)) is not bool:
            raise SetupError("Adapter disabled flag must be boolean.")
        native = {k: copy.deepcopy(v) for k, v in source.items() if k in common}
        native.update(timeout=timeout / 1000, enabled=not source.get("disabled", False))
        native.update(exposure(name, source, source.get("directTools", defaults.get("directTools", False)), warnings))
        result[name] = native
    return {"mcpServers": result}, list(dict.fromkeys(warnings))


def inspect(paths, opts, report, selection):
    from capability_setup import evidence, file_state, handoff
    agent = paths["agent_dir"]
    settings_path, target = agent / "settings.json", agent / "mcp.json"
    settings, raw_settings, _ = object_file(settings_path)
    updated, adapters = native_settings(settings)
    native, raw_native, _ = object_file(target)
    if not isinstance(native.get("mcpServers", {}), dict):
        raise SetupError("Native mcpServers must be an object.")
    selection["mcpStates"] = {str(p): file_state(p)[1] for p in (settings_path, target)}
    evidence(report, "Native MCP target", str(target))
    evidence(report, "Profile adapter declarations needing exclusion", str(adapters))
    disabled = "-builtin:mcp" in settings.get("extensions", [])
    report["status"] = "needs-configuration" if adapters or disabled or raw_native is None else "configured-untested"
    report["warnings"].append("Profile-only inventory is not current-session readiness. Project settings, auto-discovered/custom extensions, CLI -e/--no-extensions and package-provided servers may override it; review them separately. No credentials or MCP processes are probed.")
    handoff(report, "Explicit native connection check (starts every enabled server)", ["env", "PI_CODING_AGENT_DIR=" + str(agent), "pi", "mcp", "list"])
    handoff(report, "Native server manager after restarting the selected profile", "/mcp", "pi")
    if opts["mode"] != "migrate":
        report["nextSteps"].append("Use mode migrate to preview adapter conversion, or add new servers with pi mcp add in this profile. No adapter installation is needed on Pi 0.99.1.")
        return
    if raw_native is not None:
        raise SetupError("Native target already exists (possibly adapter overrides); migration never overwrites it. Review/merge it manually, including disabled flags and restrictions, before choosing a fresh target profile.")
    source = path_value(opts.get("sourceConfig", str(Path.home() / ".config/mcp/mcp.json")))
    if source in {target, settings_path} or source.is_relative_to(agent / ".mcp-migration"):
        raise SetupError("Migration source must be separate from profile targets/backups.")
    document, raw_source, _ = object_file(source)
    if raw_source is None:
        raise SetupError("Adapter source configuration is missing.")
    selection["mcpStates"][str(source)] = file_state(source)[1]
    # Additional automatic adapter layers could hide a disable/restriction. Do not
    # infer effective config from only one layer or change a project implicitly.
    for other in (Path.home() / ".config/mcp/mcp.json", Path.home() / ".agents/mcp.json", Path.home() / ".agents/mcp/mcp.json",
                  paths["project"] / ".mcp.json", paths["project"] / ".pi/mcp.json"):
        if other == source:
            continue
        raw, state = file_state(other)
        selection["mcpStates"][str(other)] = state
        if raw is not None:
            raise SetupError("Additional shared/project MCP layers exist; review their overrides manually before a single-source migration.")
    project_settings, _, _ = object_file(paths["project"] / ".pi/settings.json")
    _, project_adapters = native_settings(project_settings)
    selection["mcpStates"][str(paths["project"] / ".pi/settings.json")] = file_state(paths["project"] / ".pi/settings.json")[1]
    if project_adapters or "-builtin:mcp" in project_settings.get("extensions", []):
        raise SetupError("Project settings still select the adapter or disable native MCP; resolve that scope separately.")
    converted, warnings = translate(document)
    selection["files"] = {str(target): payload(converted), str(settings_path): payload(updated)}
    # Hex keeps the existing plan serializer deterministic without printing secrets.
    selection["mcpOriginals"] = {str(source): raw_source.hex(), str(settings_path): raw_settings.hex() if raw_settings is not None else None, str(target): None}
    report["warnings"].extend(warnings)
    report["actions"].extend([
        "Create only the missing profile mcp.json; preserve the adapter source byte-for-byte, including credentials and Peekaboo's disabled-browser environment.",
        "In this profile only: disable adapter package extensions/skills/prompts/themes, exclude explicit adapter entry paths, and enable +builtin:mcp; preserve unrelated settings and package pins. No npm uninstall.",
        "Before writing: save private exact source/settings backups and before/after SHA256 rollback evidence under AGENT/.mcp-migration/PLAN. Never print configuration values."])
    evidence(report, "Servers to migrate", str(len(converted["mcpServers"])))
    report["nextSteps"].append("Stop affected Pi sessions before apply. After apply restart Pi 0.99.1, use /mcp, then separately approve pi mcp list and any OAuth login. Rollback instructions and hashes are in the private backup manifest.")


def replace_file(path, data, expected):
    """Atomic compare-before-replace, same-user advisory-lock guarantees only."""
    with directory(path.parent, create=True) as fd:
        temp = ".mcp-" + uuid.uuid4().hex
        out = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
        try:
            with os.fdopen(out, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            if read_at(fd, path.name, LIMIT)[1] != expected:
                raise SetupError("Profile settings changed before replacement; no concurrent edit was overwritten.")
            with directory(path.parent) as current:
                if (os.fstat(fd).st_dev, os.fstat(fd).st_ino) != (os.fstat(current).st_dev, os.fstat(current).st_ino):
                    raise SetupError("Profile directory changed during migration.")
            os.replace(temp, path.name, src_dir_fd=fd, dst_dir_fd=fd)
            os.fsync(fd)
        finally:
            try:
                os.unlink(temp, dir_fd=fd)
            except FileNotFoundError:
                pass


def apply(selection, plan_id, report):
    from capability_setup import evidence, exclusive_file, file_state
    agent = Path(selection["context"]["agent_dir"])
    backup = agent / ".mcp-migration" / plan_id
    evidence(report, "Rollback backup directory", str(backup))
    # Revalidate immediately before backups/writes, beyond the outer lock check.
    for name, state in selection["mcpStates"].items():
        if file_state(Path(name))[1] != state:
            raise SetupError("MCP migration inputs changed; request a fresh plan.")
    with directory(backup.parent, create=True) as fd:
        os.mkdir(backup.name, 0o700, dir_fd=fd)
    records = []
    for index, (name, original) in enumerate(selection["mcpOriginals"].items()):
        old = bytes.fromhex(original) if original is not None else None
        filename = f"{index}.original" if old is not None else None
        if old is not None:
            exclusive_file(backup / filename, old)
        new = selection["files"].get(name, old)
        records.append({"path": name, "backup": filename, "beforeSha256": hashlib.sha256(old).hexdigest() if old is not None else None,
                        "afterSha256": hashlib.sha256(new).hexdigest() if new is not None else None,
                        "changed": name in selection["files"]})
    exclusive_file(backup / "manifest.json", payload({"schemaVersion": 1, "planId": plan_id, "files": records,
        "rollback": "Stop affected sessions. Verify current changed-file hashes equal afterSha256; refuse rollback if edited. Restore exact original backups (0600), or remove only files whose beforeSha256 is null. The source is unchanged. Restart the old profile. Partial failure may require restoring only files actually changed. Never remove the backup directory until recovery is verified."}))
    report["warnings"].append("Private backups contain credentials. Multi-file migration is not atomic: on interruption, inspect manifest hashes and restore only matching changed files; never overwrite later edits. Stop sessions and do not edit files during apply.")
    # Native file first: the still-enabled adapter cannot accidentally lose its
    # profile settings if creation fails. Every original already has a backup.
    target = agent / "mcp.json"
    exclusive_file(target, selection["files"][str(target)])
    settings = agent / "settings.json"
    state = selection["mcpStates"][str(settings)]["file"]
    if state is None:
        exclusive_file(settings, selection["files"][str(settings)])
    else:
        replace_file(settings, selection["files"][str(settings)], state)
    report["summary"] = "Native MCP profile migration written with private rollback backups; restart required, connections untested."
    report["status"] = "configured-untested"
