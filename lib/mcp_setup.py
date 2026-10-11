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
import math
import re
import uuid

from peekaboo_setup import SetupError, directory, parse_json, path_value, read_at, read_file

LIMIT = 1024 * 1024
ADAPTER = re.compile(r"npm:pi-mcp-adapter(?:@[^\s/]+)?\Z")
ADAPTER_RESOURCES = ("extensions", "skills", "prompts", "themes")
DISABLED_POLICIES = {"approveTools", "sampling", "elicitation", "autoAuth", "scriptMode"}
# Adapter presentation/bookkeeping settings with no access effect (pi-mcp-adapter 2.x McpSettings).
ADAPTER_DISPLAY_SETTINGS = {"idleTimeout", "showStatusIcon", "mcpFooterStatus", "notifyOnStartupConnect", "warnOnLargeDirectTools",
                            "strictDirectToolArguments", "directToolResultDetails", "toolResultRendering", "collapsedResultLines",
                            "disableProxyTool", "freezeDirectTools", "outputGuard", "trace", "authRequiredMessage", "oauthDir"}
NATIVE_EXPOSURES = {"codemode", "codemode-deferred", "deferred", "direct", "hidden"}
NATIVE_KEYS = {"mcpServers", "autoEnableCodemode"}
NATIVE_SERVER_KEYS = {"type", "command", "args", "env", "cwd", "url", "headers", "oauth",
                      "timeout", "enabled", "exposure", "toolExposure"}
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


def adapter_defaults(document):
    """Validate adapter top-level policy; anything that could add servers is refused."""
    if set(document) - {"mcpServers", "settings"}:
        raise SetupError("Adapter imports or unknown top-level MCP settings require manual migration; source remains untouched.")
    defaults = document.get("settings", {})
    if not isinstance(defaults, dict) or set(defaults) - (ADAPTER_DISPLAY_SETTINGS | {"requestTimeoutMs", "directTools", "toolPrefix", "hostConfigDiscovery"} | DISABLED_POLICIES):
        raise SetupError("Unsupported global adapter policy (including approvals/plugins/resources) requires manual migration.")
    if any(defaults[key] is not False for key in DISABLED_POLICIES & defaults.keys()):
        raise SetupError("Enabled adapter approvals/sampling/elicitation/autoAuth/script policies require manual migration; only explicit false values are omitted.")
    if defaults.get("hostConfigDiscovery", "off") != "off":
        raise SetupError("Host config discovery must be off before migration; imported servers cannot be silently lost.")
    if not isinstance(defaults.get("toolPrefix", "server"), str) or defaults.get("toolPrefix", "server") not in {"server", "short", "none", "mcp"}:
        raise SetupError("Unsupported adapter tool prefix.")
    if type(defaults.get("directTools", False)) is not bool:
        raise SetupError("Global directTools must be boolean; arrays need manual exposure review.")
    return defaults


def native_document(document):
    """True only when Pi's native loader reads the file as the adapter did."""
    servers = document.get("mcpServers", {})
    if set(document) - NATIVE_KEYS or not isinstance(servers, dict):
        return False
    for server in servers.values():
        if not isinstance(server, dict) or set(server) - NATIVE_SERVER_KEYS:
            return False
        stdio, kind = isinstance(server.get("command"), str), server.get("type")
        if stdio == isinstance(server.get("url"), str) or ("type" in server and not isinstance(kind, str)) or kind not in (
                {None, "stdio"} if stdio else {None, "http", "streamable-http"}):
            return False
        if not native_values(server):
            return False
    return True


def native_values(server):
    """Mirror Pi 0.99 validateMcpServerConfig plus adapter-compatible string values."""
    for key in {"command", "args", "env", "cwd", "url", "headers"} & server.keys():
        value = server[key]
        if key == "args" and not isinstance(value, list) or key in {"env", "headers"} and not isinstance(value, dict):
            return False
        values = value if key == "args" else list(value.values()) if key in {"env", "headers"} else [value]
        if any(not isinstance(v, str) or not compatible_value(key, v) for v in values):
            return False
    if "url" in server and not http_url(server["url"]):
        return False
    timeout, tools, exposure = server.get("timeout", 1), server.get("toolExposure", {}), server.get("exposure", "codemode")
    return (type(server.get("enabled", True)) is bool and type(timeout) in {int, float} and math.isfinite(timeout) and timeout > 0
            and isinstance(exposure, str) and exposure in NATIVE_EXPOSURES and isinstance(tools, dict)
            and all(isinstance(v, str) and v in NATIVE_EXPOSURES for v in tools.values())
            and native_oauth(server.get("oauth", {})))


def http_url(url, pattern=r"https?://(?:[A-Za-z0-9._~-]+|\[[0-9A-Fa-f:.]+\])(?::(\d{1,5}))?(?:[/?#]\S*)?"):
    """Conservative subset of URLs Pi's URL parser accepts."""
    match = re.fullmatch(pattern, url)
    return bool(match) and (match[1] is None or int(match[1]) <= 65535)


def native_oauth(oauth):
    """Pi 0.99 validateOAuth: known fields are checked, unknown ones ignored."""
    if not isinstance(oauth, dict) or any(not isinstance(oauth.get(k, ""), str) for k in ("clientId", "clientSecret", "scope")):
        return False
    port = oauth.get("callbackPort")
    if "callbackPort" in oauth and (type(port) is not int or not 1 <= port <= 65535):
        return False
    if "callbackUrl" not in oauth:
        return True
    url = oauth["callbackUrl"]
    match = isinstance(url, str) and re.fullmatch(r"http://(?:localhost|127\.0\.0\.1|\[::1\])(?::(\d{1,5}))?(?:/[^?#]*)?", url)
    return (bool(match) and (match[1] is None or int(match[1]) <= 65535)
            and (match[1] is None or port is None or int(match[1]) == port))


MIGRATION_WARNINGS = [
    "Native MCP connects enabled servers at session startup, not lazily; idle lifecycle, cache, proxy/script APIs and adapter UI settings are not retained. Approval includes this lifecycle change.",
    "Native names are mcp__SERVER__TOOL; prompts, MCP Apps, sampling/elicitation and adapter permission brokers are not migrated. Review dependent extensions and instructions before restarting.",
    "Native protocol negotiation and progress-reset request timeouts differ; HTTP uses streamable HTTP only, never SSE fallback. No server is started or protocol compatibility verified here.",
    "Resources use native generic resource tools, not adapter generated tools. OAuth/keychain tokens are not read, copied or deleted; sign in separately with pi mcp login if needed."]


def translate(document):
    defaults = adapter_defaults(document)
    servers = document.get("mcpServers")
    if not isinstance(servers, dict) or not servers or len(servers) > 128:
        raise SetupError("Migration needs a nonempty bounded mcpServers object.")
    warnings = list(MIGRATION_WARNINGS)
    result = {name: translate_server(name, source, defaults, warnings) for name, source in servers.items()}
    return {"mcpServers": result}, list(dict.fromkeys(warnings))


def translate_server(name, source, defaults, warnings):
    common = {"command", "args", "env", "cwd", "url", "headers"}
    supported = common | {"type", "lifecycle", "idleTimeout", "requestTimeoutMs", "directTools", "disabled", "includeTools", "excludeTools", "exposeResources", "protocolVersion", "toolPrefix", "approveTools"}
    if not re.fullmatch(r"[A-Za-z0-9_-]+", name) or not isinstance(source, dict):
        raise SetupError("Invalid native MCP server name or definition.")
    if set(source) - supported:
        raise SetupError("Unsupported server fields (auth/approval/socket/import/unknown policy) require manual migration; no fields were silently dropped.")
    if "approveTools" in source and source["approveTools"] is not False:
        raise SetupError("Server approval policies require an equivalent native policy; only explicit false migrates automatically.")
    if any(not isinstance(source.get(key, ""), str) for key in ("protocolVersion", "lifecycle", "toolPrefix", "type")):
        raise SetupError("Malformed adapter transport/lifecycle fields.")
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
    if type(timeout) not in {int, float} or not math.isfinite(timeout) or timeout <= 0 or timeout / 1000 <= 0:
        raise SetupError("Non-positive/invalid adapter timeouts require manual review; native timeout must be positive.")
    if type(source.get("disabled", False)) is not bool:
        raise SetupError("Adapter disabled flag must be boolean.")
    native = {k: copy.deepcopy(v) for k, v in source.items() if k in common}
    native.update(timeout=timeout / 1000, enabled=not source.get("disabled", False))
    native.update(exposure(name, source, source.get("directTools", defaults.get("directTools", False)), warnings))
    return native


# pi-mcp-adapter 2.x host imports, in its fixed precedence order. Project-relative
# candidates (vscode, ./opencode.json) depend on the session cwd and are not migrated.
IMPORT_PATHS = {
    "cursor": (".cursor/mcp.json",),
    "claude-code": (".claude/mcp.json", ".claude.json", ".claude/claude_desktop_config.json"),
    "claude-desktop": ("Library/Application Support/Claude/claude_desktop_config.json",),
    "codex": (".codex/config.toml", ".codex/config.json"),
    "opencode": (".config/opencode/opencode.json",),
    "windsurf": (".windsurf/mcp.json",),
}
IMPORT_LIMIT = 64 * 1024 * 1024
# Adapter merge: a changed transport or URL never inherits the old entry's credentials.
STDIO_DROPS = ("url", "headers", "requestHeadersCommand", "auth", "bearerToken", "bearerTokenEnv", "oauth", "httpTransport", "socket")
HTTP_DROPS = ("command", "args", "env", "cwd", "pluginDataDir", "literalEnv", "socket")
SOCKET_DROPS = HTTP_DROPS[:-1] + STDIO_DROPS[:-1]
URL_BOUND = ("headers", "bearerToken", "bearerTokenEnv", "bearerTokenStore", "requestHeadersCommand")
# Adapter-only features native MCP lacks; dropping them removes capability, never a restriction.
ADAPTER_FEATURES = ("scriptMode", "autoAuth", "sampling", "samplingAutoApprove", "elicitation")


def _scan_jsonc(text, drop_comma):
    """Copy text outside strings, skipping comments and (when asked) trailing commas."""
    out, i, n = [], 0, len(text)
    while i < n:
        c = text[i]
        if c == '"':
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            out.append(text[i:j + 1])
            i = j + 1
        elif text.startswith("//", i):
            end = text.find("\n", i)
            i = n if end < 0 else end
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 2
        elif drop_comma and c == "," and text[i + 1:i + 4097].lstrip(" \t\r\n")[:1] in ("}", "]"):
            i += 1
        else:
            out.append(c)
            i += 1
    return "".join(out)


def strip_jsonc(text):
    """Comments first, then trailing commas, so a comma before a comment is also removed."""
    return _scan_jsonc(_scan_jsonc(text, False), True)


def _reject(_value):
    raise ValueError("NaN/Infinity literals are not JSON")


def read_config(path, toml=False):
    """Parse a file the adapter read; None when absent, unreadable or invalid (the adapter skipped those)."""
    try:
        if path.stat().st_size > IMPORT_LIMIT:
            return None
        text = path.read_bytes().decode("utf-8", "replace")  # As Node's readFileSync(path, "utf-8").
        if toml:
            import tomllib
            return tomllib.loads(text)
        try:
            return json.loads(text, parse_constant=_reject)
        except ValueError:
            return json.loads(strip_jsonc(text), parse_constant=_reject)
    except (OSError, ValueError):
        return None
    except RecursionError:
        raise SetupError(f"{path} is nested too deeply to migrate safely; simplify it, then rerun pi-shared update.") from None


def merge_servers(base, upper):
    merged = dict(base)
    for name, definition in upper.items():
        existing = merged.get(name)
        entry = dict(existing or {})
        if existing:
            drops = (STDIO_DROPS if isinstance(definition.get("command"), str) else
                     HTTP_DROPS if isinstance(definition.get("url"), str) else
                     SOCKET_DROPS if isinstance(definition.get("socket"), str) else ())
            for field in drops:
                entry.pop(field, None)
            if isinstance(definition.get("url"), str) and definition["url"] != existing.get("url"):
                for field in URL_BOUND:
                    entry.pop(field, None)
                if entry.get("oauth") is not False:
                    entry.pop("oauth", None)
        merged[name] = {**entry, **definition}
    return merged


def _opencode(entry):
    command = entry.get("command")
    if entry.get("type") == "local" and isinstance(command, list) and command and all(isinstance(v, str) for v in command):
        return {"command": command[0], "args": command[1:],
                **({"env": entry["environment"]} if isinstance(entry.get("environment"), dict) else {}),
                **({"cwd": entry["cwd"]} if isinstance(entry.get("cwd"), str) else {})}
    if entry.get("type") == "remote" and isinstance(entry.get("url"), str):
        mapped = {"url": entry["url"], **({"headers": entry["headers"]} if isinstance(entry.get("headers"), dict) else {})}
        if entry.get("oauth") is False:
            mapped["oauth"] = False
        elif isinstance(entry.get("oauth"), dict):
            mapped["auth"] = "oauth"  # Adapter OAuth: never passed through as native config.
            mapped["oauth"] = entry["oauth"]
        return mapped
    return None


def _codex(entry):
    mapped = {k: v for k, v in entry.items() if k not in {"bearer_token_env_var", "http_headers", "env_http_headers"}}
    if isinstance(entry.get("bearer_token_env_var"), str):
        mapped["bearerTokenEnv"] = entry["bearer_token_env_var"]
        mapped.setdefault("auth", "bearer")
    headers = dict(mapped["headers"]) if isinstance(mapped.get("headers"), dict) else {}
    if isinstance(entry.get("http_headers"), dict):
        headers.update(entry["http_headers"])
    if isinstance(entry.get("env_http_headers"), dict):
        for header, variable in entry["env_http_headers"].items():
            if isinstance(variable, str):
                headers.setdefault(header, f"$env:{variable}")
    if headers:
        mapped["headers"] = headers
    return mapped


def host_servers(kind, home, notes):
    for relative in IMPORT_PATHS[kind]:
        path = home / relative
        try:
            if path.stat().st_size > IMPORT_LIMIT:
                notes.append(f"{path} exceeds 64 MiB; its {kind} servers are not migrated.")
                return {}
        except OSError:
            continue
        if relative.endswith(".toml"):
            try:
                import tomllib  # noqa: F401  (Python 3.11+)
            except ImportError:
                notes.append(f"{path} needs Python 3.11+ to read; its {kind} servers are not migrated.")
                return {}
        value = read_config(path, relative.endswith(".toml"))
        if value is not None:
            break
    else:
        return {}
    if not isinstance(value, dict):
        return {}
    servers = (value.get("mcp") if kind == "opencode" else
               value.get("mcp_servers", value.get("mcpServers")) if kind == "codex" else
               value.get("mcpServers") if kind in {"claude-code", "claude-desktop"} else
               value.get("mcpServers", value.get("mcp-servers")))
    if not isinstance(servers, dict):
        return {}
    result = {}
    for name, entry in servers.items():
        if not isinstance(entry, dict) or (kind == "opencode" and entry.get("enabled") is False):
            continue
        mapped = _opencode(entry) if kind == "opencode" else _codex(entry) if kind == "codex" else entry
        if mapped is not None:
            result[name] = mapped
    return result


def adapter_config(agent, notes):
    """Reproduce the adapter's user-level effective config for this profile."""
    home = Path.home()
    exclusive = os.environ.get("PI_MCP_CONFIG_MODE", "").strip().lower() == "exclusive"
    if exclusive:
        notes.append("PI_MCP_CONFIG_MODE=exclusive in the update environment: only the profile mcp.json layer is migrated.")
        paths = (agent / "mcp.json",)
    else:
        paths = tuple(dict.fromkeys((home / ".config/mcp/mcp.json", home / ".agents/mcp.json",
                                     home / ".agents/mcp/mcp.json", agent / "mcp.json")))
    layers = []
    for path in paths:
        value = read_config(path)
        if value is None:
            if os.path.lexists(path):
                notes.append(f"{path} is unreadable or invalid; the adapter skipped it too, so its servers are not migrated.")
            continue
        value = value if isinstance(value, dict) else {}
        servers = value.get("mcpServers") if value.get("mcpServers") is not None else value.get("mcp-servers")
        imports = value.get("imports") if isinstance(value.get("imports"), list) else []
        layers.append({"mcpServers": {k: v for k, v in servers.items() if isinstance(v, dict)} if isinstance(servers, dict) else {},
                       "imports": [kind for kind in imports if isinstance(kind, str)],
                       "settings": value.get("settings") if isinstance(value.get("settings"), dict) else {}})
    settings = {}
    for layer in layers:
        settings.update(layer["settings"])
    merged = {}
    discovery = not exclusive and settings.get("hostConfigDiscovery") == "on"
    if discovery:
        for kind in IMPORT_PATHS:
            merged = merge_servers(merged, host_servers(kind, home, notes))
    relative = discovery
    for layer in layers:
        imported = {}
        for kind in dict.fromkeys(layer["imports"]):
            relative |= kind in {"vscode", "opencode"}
            for name, entry in (host_servers(kind, home, notes) if kind in IMPORT_PATHS else {}).items():
                imported.setdefault(name, entry)
        merged = merge_servers(merged, merge_servers(imported, layer["mcpServers"]))
    if relative:
        notes.append("Project-relative imports (.vscode/mcp.json, ./opencode.json) depend on the working directory and are not migrated.")
    if not exclusive and settings.get("agentPluginPaths"):
        notes.append("Adapter agent-plugin servers (settings.agentPluginPaths) are not migrated; add them with pi mcp add.")
    return merged, settings


def _label(name):
    return name if isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", name) else repr(name)[:66]


def convert_servers(servers, settings, notes):
    """Convert each server independently; omit (never loosen) anything needing review.

    Shared policy that restricts every server (approvals, unknown settings) raises
    instead: the adapter stays selected rather than migrating to an empty config.
    """
    dropped = [key for key in ADAPTER_FEATURES if settings.get(key) not in (None, False)]
    defaults = {k: v for k, v in settings.items() if k not in {"hostConfigDiscovery", "agentPluginPaths", *ADAPTER_FEATURES}}
    try:
        adapter_defaults({"settings": defaults})
    except (TypeError, ValueError):
        raise SetupError("Malformed shared adapter settings require manual migration.") from None
    if dropped:
        notes.append("Adapter-only features have no native equivalent and are not carried over: " + ", ".join(dropped) + ".")
    warnings, converted = [], {}
    malformed = (SetupError, TypeError, ValueError, AttributeError, OverflowError)
    for name, entry in servers.items():
        try:
            converted[name] = translate_server(name, entry, defaults, warnings)
            continue
        except malformed as exc:
            reason = str(exc) if isinstance(exc, SetupError) else "malformed definition"
        try:
            native = (isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9_-]+", name) and native_document({"mcpServers": {name: entry}})
                      and bool(json.dumps(entry, allow_nan=False)))
        except malformed:
            native = False
        if native:
            converted[name] = entry  # Native-only keys and values: native reads it as written.
        else:
            notes.append(f"Not migrated: {_label(name)} ({reason})")
    return converted, list(dict.fromkeys(warnings))


UPDATE_WARNINGS = [
    "Migrated servers now start with every Pi session (the adapter started them on demand); disable unwanted ones in /mcp.",
    *MIGRATION_WARNINGS[1:]]
PROJECT_WARNING = ("Trusted projects' .pi/mcp.json files are now read by native MCP: their servers start with each session and "
                   "adapter-only keys there (disabled, includeTools/excludeTools) no longer apply; project .mcp.json files are no "
                   "longer read. Review them.")


def inspect(paths, opts, report, selection):
    from capability_setup import evidence, file_state, handoff
    agent = paths["agent_dir"]
    settings_path, target = agent / "settings.json", agent / "mcp.json"
    settings, _, _ = object_file(settings_path)
    _, adapters = native_settings(settings)
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
    source = path_value(opts.get("sourceConfig", str(Path.home() / ".config/mcp/mcp.json")))
    if "sourceConfig" in opts and object_file(source)[1] is None:
        raise SetupError("Adapter source configuration is missing.")
    plan_migration(agent, source, paths["project"], selection, report)
    report["nextSteps"].append("Stop affected Pi sessions before apply. After apply restart Pi 0.99.1, use /mcp, then separately approve pi mcp list and any OAuth login. Rollback instructions and hashes are in the private backup manifest.")


def plan_migration(agent, source, project, selection, report):
    """Fill selection with the profile migration; raise SetupError on any conflict.

    An adapter source without servers needs only the settings change, so a
    missing or empty source migrates settings alone and never creates mcp.json.
    A None source skips adapter configuration entirely (no adapter selected).
    """
    from capability_setup import evidence, file_state
    settings_path, target = agent / "settings.json", agent / "mcp.json"
    settings, raw_settings, _ = object_file(settings_path)
    updated, _ = native_settings(settings)
    native, raw_native, _ = object_file(target)
    selection.setdefault("mcpStates", {}).update({str(p): file_state(p)[1] for p in (settings_path, target)})
    document, raw_source = {}, None
    if source is not None:
        if source in {target, settings_path} or source.is_relative_to(agent / ".mcp-migration"):
            raise SetupError("Migration source must be separate from profile targets/backups.")
        document, raw_source, _ = object_file(source)
        selection["mcpStates"][str(source)] = file_state(source)[1]
        # Additional automatic adapter layers could hide a disable/restriction. Do not
        # infer effective config from only one layer or change a project implicitly.
        layers = [Path.home() / ".config/mcp/mcp.json", Path.home() / ".agents/mcp.json", Path.home() / ".agents/mcp/mcp.json"]
        if project is not None:
            layers += [project / ".mcp.json", project / ".pi/mcp.json"]
        for other in layers:
            if other == source:
                continue
            raw, state = file_state(other)
            selection["mcpStates"][str(other)] = state
            if raw is not None:
                raise SetupError("Additional shared/project MCP layers exist; review their overrides manually before a single-source migration.")
    if project is not None:
        project_settings, _, _ = object_file(project / ".pi/settings.json")
        _, project_adapters = native_settings(project_settings)
        selection["mcpStates"][str(project / ".pi/settings.json")] = file_state(project / ".pi/settings.json")[1]
        if project_adapters or "-builtin:mcp" in project_settings.get("extensions", []):
            raise SetupError("Project settings still select the adapter or disable native MCP; resolve that scope separately.")
    servers = document.get("mcpServers")
    # The adapter also reads and writes <agent>/mcp.json as an override layer. Keep
    # an existing file only when nothing must be converted and it is provably native.
    if raw_native is not None and (servers or not native_document(native)):
        raise SetupError("Native target already exists (possibly adapter overrides); migration never overwrites it. Review/merge it manually, including disabled flags and restrictions, before choosing a fresh target profile.")
    if servers:
        converted, warnings = translate(document)
    else:
        adapter_defaults(document)
        converted, warnings = None, []
    selection["files"] = {str(settings_path): payload(updated)}
    # Hex keeps the existing plan serializer deterministic without printing secrets.
    selection["mcpOriginals"] = {str(settings_path): raw_settings.hex() if raw_settings is not None else None}
    if raw_source is not None:
        selection["mcpOriginals"][str(source)] = raw_source.hex()
    if converted is not None:
        selection["files"][str(target)] = payload(converted)
        selection["mcpOriginals"][str(target)] = None
        report["actions"].append("Create only the missing profile mcp.json; preserve the adapter source byte-for-byte, including credentials and Peekaboo's disabled-browser environment.")
    report["warnings"].extend(warnings)
    report["actions"].extend([
        "In this profile only: disable adapter package extensions/skills/prompts/themes, exclude explicit adapter entry paths, and enable +builtin:mcp; preserve unrelated settings and package pins. No npm uninstall.",
        "Before writing: save private exact source/settings backups and before/after SHA256 rollback evidence under AGENT/.mcp-migration/PLAN. Never print configuration values."])
    evidence(report, "Servers to migrate", str(len(converted["mcpServers"]) if converted else 0))


def update_profile(agent, report, dry_run=False):
    """Managed-update migration of one profile under the caller's update lock.

    While the adapter is selected, built-in MCP never read <agent>/mcp.json: the
    adapter did, as one of its layers. So the adapter's effective config becomes
    the native file (backed up), converting each server independently. Without
    an adapter only settings change. Project scopes are not inspected.
    Returns False when nothing is needed.
    """
    from capability_setup import evidence, file_state
    settings_path, target = agent / "settings.json", agent / "mcp.json"
    for path in (settings_path, target):
        if path.is_symlink():
            raise SetupError(f"{path} is a symlink; replace it with a regular file (copy its contents), then rerun pi-shared update.")
    settings, raw_settings, _ = object_file(settings_path)
    updated, adapters = native_settings(settings)
    if not adapters and "-builtin:mcp" not in settings.get("extensions", []):
        return False
    selection = {"schemaVersion": 1, "component": "mcp", "context": {"agent_dir": str(agent)},
                 "mcpStates": {str(p): file_state(p)[1] for p in (settings_path, target)},
                 "files": {str(settings_path): payload(updated)},
                 "mcpOriginals": {str(settings_path): raw_settings.hex() if raw_settings is not None else None}}
    converted = {}
    if adapters:
        notes = []
        servers, adapter_settings = adapter_config(agent, notes)
        converted, warnings = convert_servers(servers, adapter_settings, notes)
        warnings = (UPDATE_WARNINGS if converted else []) + warnings + [PROJECT_WARNING]
        raw_native = read_file(target, LIMIT)[0]
        if raw_native is not None or converted:
            document = {"mcpServers": converted}
            previous = read_config(target)
            if isinstance(previous, dict) and isinstance(previous.get("autoEnableCodemode"), bool):
                document["autoEnableCodemode"] = previous["autoEnableCodemode"]
            selection["files"][str(target)] = payload(document)
            selection["mcpOriginals"][str(target)] = raw_native.hex() if raw_native is not None else None
        report["warnings"].extend(warnings + notes)
    evidence(report, "Servers to migrate", str(len(converted)))
    if not dry_run:
        digestable = {**selection, "files": {k: v.hex() for k, v in selection["files"].items()}}
        # Unique per attempt: a failed attempt's backup directory never blocks a retry.
        plan_id = hashlib.sha256(json.dumps(digestable, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:48] + uuid.uuid4().hex[:16]
        apply(selection, plan_id, report)
    return True


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
                raise SetupError("A profile file changed before replacement; no concurrent edit was overwritten.")
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
    evidence(report, "Profile writes started", "yes")
    target = agent / "mcp.json"
    if str(target) in selection["files"]:
        current = selection["mcpStates"][str(target)]["file"]
        if current is None:
            exclusive_file(target, selection["files"][str(target)])
        else:
            replace_file(target, selection["files"][str(target)], current)
    settings = agent / "settings.json"
    state = selection["mcpStates"][str(settings)]["file"]
    if state is None:
        exclusive_file(settings, selection["files"][str(settings)])
    else:
        replace_file(settings, selection["files"][str(settings)], state)
    report["summary"] = "Native MCP profile migration written with private rollback backups; restart required, connections untested."
    report["status"] = "configured-untested"
