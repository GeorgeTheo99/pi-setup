# Official Pi MCP and adapter migration

Pi is pinned to **0.99.1**, which includes native MCP. New setups do **not** need
`pi-mcp-adapter`. Native personal servers live in `<agent-dir>/mcp.json`; trusted
project servers live in `.pi/mcp.json`. Native `/mcp` manages them, and tools are
named `mcp__SERVER__TOOL`. An extension registering `/mcp` replaces the native
implementation in sessions; `pi mcp` shell commands always use native MCP.

## Inventory and migration

The existing bounded capability backend provides native inventory by default:

```sh
pi-shared capability mcp plan --json \
  --project /absolute/canonical/project \
  --agent-dir /absolute/canonical/profile \
  --shared-root /absolute/canonical/pi-shared \
  --node-executable /absolute/canonical/node
```

To migrate `~/.config/mcp/mcp.json`, repeat these same four context arguments with:

```sh
# Offline preview: no servers, auth commands, network, or writes.
pi-shared capability mcp plan --json ... --options '{"mode":"migrate"}'
# Read all warnings/actions; stop affected Pi sessions before this explicit apply.
pi-shared capability mcp apply --json ... --options '{"mode":"migrate"}' \
  --yes --expected-plan PLAN_ID_FROM_PREVIEW
# Static inventory only, after applying; do not retain mode:migrate here.
pi-shared capability mcp check --json ...
```

`...` above means the four identical context arguments, not a literal CLI option.
An alternative absolute source may be supplied as
`{"mode":"migrate","sourceConfig":"/absolute/source/mcp.json"}`. Repeat identical
options for plan and apply. Source contents, original settings, target absence,
relevant override files, proposed bytes and path ancestry bind the approval digest.
Changes invalidate approval, including changes detected under the shared update
lock. Apply requires both `--yes` and the exact `planId`; there is no default consent.
No release, install, global npm uninstall, or other profile mutation occurs.

Migration creates **only a missing** `<agent-dir>/mcp.json`. Existing native files,
including adapter profile overrides, are never replaced or merged automatically.
A conflicting target, additional shared/project MCP config layer, project adapter
selection or project `-builtin:mcp` produces a safe refusal. Review those scopes
and restrictions manually rather than deleting them merely to bypass the guard.
Unknown adapter policy, imports, plugin/socket/auth fields and enabled approvals also refuse
migration without changes. Unknown profile settings and unrelated packages and
extensions are preserved. No credentials appear in reports.

The profile's `settings.json` is narrowly updated:

- Recognized `npm:pi-mcp-adapter[@VERSION]` strings become package objects with
  `extensions`, `skills`, `prompts`, and `themes` all set to `[]`. This also disables
  adapter-only scripting guidance. Existing objects retain unrelated keys and
  pins. Recognized local adapter packages receive the same resource filters.
- Explicit adapter extension directories/entry paths become exact `-PATH`
  exclusions, including `+PATH` forms. Already excluded paths remain unchanged.
- Profile `-builtin:mcp` is replaced with `+builtin:mcp`.

No package files are deleted. Other profiles still using the shared adapter source
are unaffected. Auto-discovered, renamed, custom, CLI-loaded, and package-provided
extensions are **not audited**; project/CLI overrides can still replace native MCP.
Review these separately. Native inventory is not proof of current-session loading.

## Conversion and conservative restrictions

| Adapter | Native behavior |
| --- | --- |
| `command`, `args`, `env`, `cwd`, `url`, `headers` | Copied without executing or resolving credential references; only stdio/streamable HTTP supported |
| `requestTimeoutMs` (server or global) | `timeout` in seconds, including fractions; omitted uses 60 seconds; non-positive values refused |
| `disabled` | Inverted into `enabled`, including disabled servers |
| Boolean `directTools` (server or global) | `direct` or `codemode` exposure; arrays require manual review |
| `includeTools` | Hidden server plus original-name `toolExposure` allowlist; `*` patterns supported when there are no exclusions; alias matches may narrow |
| `excludeTools` | With a finite original-name allowlist, remove matching names and all matching adapter aliases; never override an exclusion with a native exact-name rule |
| Deny-only or overlapping wildcard filters | Refused: native exact-name/pattern precedence and adapter aliases cannot be safely equated without a finite allowlist |
| `exposeResources: false` or any filters | Hide all resources via hidden server exposure; expose only allowed tools, never silently broaden resource access |
| Peekaboo Bridge environment | Preserve `PEEKABOO_DISABLE_TOOLS=browser` exactly |
| Explicitly false `approveTools`, `sampling`, `elicitation`, `autoAuth`, `scriptMode` | Omit disabled adapter-specific policy with the listed compatibility warnings; non-false values require manual review. Native codemode replaces the disabled adapter scripting implementation, not its API |

The offline plan explicitly warns about native eager startup connections versus
adapter lazy/idle lifecycles; generic resources versus generated resource tools;
new tool names and discovery; unsupported prompts, MCP Apps, sampling/elicitation,
permission brokers and proxy/script APIs; and protocol/timeout differences.
Explicit modern/auto adapter protocol negotiation is refused. SSE is never
silently upgraded. Only known harmless adapter display/idle settings are omitted;
unknown policy requires manual review. Adapter-escaped `!!` commands, bare `$NAME`, native-only `$$`/`$!` escapes,
`$env:NAME`/`{env:NAME}`, noncanonical dollar syntax in env/headers, and adapter
interpolation in arguments/URLs/paths are refused rather than reinterpreted.
Home-prefixed (`~`, `~/…`, `~\\…`) commands, arguments and working directories
also require manual conversion to explicit paths: native expands argument homes
where the adapter passed literal strings, and path behavior varies by platform.
Literal credentials without those ambiguous forms and canonical `${NAME}` or
single-leading-`!command` references in environment/headers are preserved, never
resolved. Missing-variable and command-failure behavior still differs between
clients; native resolution fails closed.

Adapter OAuth/keychain tokens remain untouched and are not copied to native
`mcp-auth.json`. HTTP servers may need a separate native sign-in. Resource
narrowing can disable workflows; the plan is approval of that explicit difference,
not a promise of full adapter feature parity.

## Backups and rollback

Before changing either profile file, apply creates a new 0700 directory:

```text
<agent-dir>/.mcp-migration/<planId>/
  manifest.json
  0.original
  1.original   # when settings.json existed
```

Backups are exact original source/settings bytes, stored 0600, and may contain
credentials. The manifest records original and proposed SHA256 values, original
absence, target paths, and rollback instructions. Reports include the backup path,
never its contents. Source MCP JSON remains byte-for-byte unchanged. New native
config and replaced settings are 0600. Files are descriptor-checked, no-follow,
single-link and not shared-writable; config reads/writes are bounded to 1 MiB.

This is **not an atomic multi-file transaction**. The native file is created
exclusively first, followed by a compare-before-replace settings write. A failure
can leave a partial migration with backups. The advisory lock coordinates
pi-shared writers, not arbitrary same-user editors; do not concurrently edit files.
There is no automatic destructive recovery or rollback command.

For rollback: stop affected sessions, inspect the private manifest, and verify
current changed-file hashes against `afterSha256`. Restore the exact original
backup for changed files that match, with mode 0600; remove only a matching newly
created file whose `beforeSha256` is null. If a hash differs, **do not overwrite it**:
review the later edit/partial failure manually. Leave the unchanged source alone.
Restart the prior profile and retain backups until recovery is verified. Restoring
the adapter settings restores its old routing; no npm reinstall is needed.

## Runtime verification (separate, explicit execution)

After static check, restart Pi **0.99.1** in the selected profile. A runtime upgrade
requires a restart, not just `/reload`. For configuration-only changes `/reload`
can reconnect, but migration is best verified in a fresh session.

```sh
env PI_CODING_AGENT_DIR=/absolute/canonical/profile pi mcp list
# Only if a server needs OAuth, with separate browser/user consent:
env PI_CODING_AGENT_DIR=/absolute/canonical/profile pi mcp login SERVER
```

`pi mcp list` starts/connects **every enabled server**. It is deliberately a handoff,
not executed by capability check. Run it from a reviewed project (trusted project
MCP entries can override the profile). In Pi use `/mcp` and discover the actual
catalog through codemode or `tool_search`. CLI connection success alone
does not prove that extensions have not replaced `/mcp` in a session.

[Peekaboo setup](peekaboo.md) now uses native MCP for new profiles, preserves detected
legacy installs with a warning, and recognizes its exact migrated native entry.
Its check remains only CLI/signature/permission evidence, not an MCP or desktop test.
