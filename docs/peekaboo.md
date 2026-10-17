# Standalone Peekaboo setup

`pi-shared peekaboo` is an opt-in macOS CLI integration, **not** a setup module,
service, or part of `setup`, `update`, `doctor` or their receipts. It installs the
desktop app only on explicit `--install-app` approval. It changes neither Pi runtime nor OS permissions. The shared Pi frontend
may invoke this backend; the backend works independently.

## Preview, approve, check

```sh
pi-shared peekaboo status --json
pi-shared peekaboo plan --binary /absolute/path/to/peekaboo --json
# Review the returned actions/warnings, then copy the exact planId:
pi-shared peekaboo apply --binary /absolute/path/to/peekaboo \
  --yes --expected-plan <planId> --json
pi-shared peekaboo check --json
```

To select the desktop app's Bridge as the permission owner:

```sh
pi-shared peekaboo plan --mode bridge --bridge-socket /absolute/path/to/bridge.sock \
  --binary /absolute/path/to/peekaboo --json
pi-shared peekaboo apply --mode bridge --bridge-socket /absolute/path/to/bridge.sock \
  --binary /absolute/path/to/peekaboo --yes --expected-plan <planId> --json
pi-shared peekaboo check --json
```

The complete download-and-install route (what `/setup peekaboo`'s recommended
choice runs) installs both the CLI and app, launches the app and configures MCP:

```sh
SOCK="$HOME/Library/Application Support/Peekaboo/bridge.sock"
pi-shared peekaboo plan --mode bridge --bridge-socket "$SOCK" --install --install-app --json
pi-shared peekaboo apply --mode bridge --bridge-socket "$SOCK" --install --install-app \
  --yes --expected-plan <planId> --json
```

Then allow Peekaboo under **Accessibility** and **Screen & System Audio Recording**
in System Settings › Privacy & Security, keep the app running, restart Pi and run
`check`. Bridge MCP fails at startup until Screen Recording is granted.

New setups default to official **Pi 1.0.3 native MCP**, writing
`<agent-dir>/mcp.json`. `--agent-dir ABS` selects the profile; otherwise
`PI_CODING_AGENT_DIR` or `~/.pi/agent` is used. Detected legacy adapter declarations
or an existing legacy config without a native target retain the adapter route with
an explicit warning, not a silent migration. A completed [MCP migration](mcp.md)
is preferred even while its untouched legacy source remains on disk.

All actions accept `--config /absolute/path/to/mcp.json` and
`--backend native|adapter`. With an explicit path, an existing adapter Peekaboo
entry is recognized; otherwise the new-entry default is native. Use
`--backend adapter` explicitly for a new entry in a custom adapter file. Native
selection refuses profiles still declaring the adapter or disabling `builtin:mcp`;
review capability MCP migration first. Peekaboo does not rewrite profile settings.
Repeat the **same agent-dir/backend/binary/config/install/mode/socket choices**
from `plan` in `apply`. A changed config, executable, adjacent runtime library or
Bridge socket metadata invalidates approval. Socket selection and route are also
part of the approval digest, along with profile settings and backend selection.
There are no interactive prompts. `--yes` alone is not enough.
Help never inspects configuration or starts anything.

`--mode direct|bridge` selects the route. When omitted, an **exactly recognized**
existing direct or Bridge entry is preserved; without an existing entry, the default
is direct. A new Bridge selection requires an explicit normalized absolute
`--bridge-socket`; an existing matching Bridge entry supplies its saved socket.
A socket alone does not select Bridge for a new entry. Direct mode plus a socket
is rejected. Existing conflicting entries are never overwritten, normalized or
migrated automatically—even when a different mode is explicitly requested.

Socket inspection is filesystem metadata only: no connect, app discovery or
listener probe. Socket paths reject symlinks, unsafe ancestors, non-socket types,
foreign ownership, hardlinks and shared-write access. A missing socket can be
planned/configured, but `check` fails with start-app guidance. A present socket
**does not prove the app is listening**. Without `--install-app`, install and start
the desktop app yourself; see [optional installs](#optional-installs).

`plan` and `status` only inspect local files, without subprocesses, network,
locks, or writes. They can succeed with a missing installation: `ok` means the
requested inventory operation succeeded, not that Peekaboo is ready. They read
existing MCP JSON to preserve it, but never disclose server settings, secrets,
permission instructions from child output, or raw UI content.

Discovery uses only an explicit `--binary`, the existing correctly shaped
`mcpServers.peekaboo.command`, the managed 4.9.5 path, or known Homebrew
`/opt/homebrew/bin/peekaboo` / `/usr/local/bin/peekaboo` links into their respective
`Cellar/peekaboo` directories. It never searches PATH or the working directory.
Explicit and configured binary paths must be normalized absolute regular
executables without symlinks, hardlinks, setuid/setgid bits, or shared-write access.
For a Homebrew symlink supplied explicitly, select its actual Cellar executable.
Signatures and actual versions cannot be verified by offline plans.

## Optional installs

```sh
pi-shared peekaboo plan --install --json
pi-shared peekaboo apply --install --yes --expected-plan <planId> --json
```

Both components are pinned to official release **4.9.5**. It was verified on
macOS 26.3: the CLI starts, and app-Bridge MCP advertises all 27 tools, `browser`
included, so 4.5.0's browser exclusion is gone.

**CLI (`--install`)** supports **Apple Silicon macOS only**. It installs when no
binary is found and reuses an already installed managed 4.9.5 CLI (for example
after an interrupted setup). An entry for an **older setup-managed** CLI
(`~/.local/share/peekaboo/<older version>/peekaboo`, any entry shape) makes
`plan`/`check` fail with upgrade guidance; `--install` with an explicit `--mode`
replaces it with the selected route and leaves the old version directory in place.
Newer managed versions and custom configured commands are never replaced. Existing signed binaries must report exactly 4.9.5;
other versions are rejected, never downgraded. Installation is refused when the
managed version directory already exists. It never adds a global symlink or runs Homebrew.

- URL: `https://github.com/openclaw/Peekaboo/releases/download/v4.9.5/peekaboo-macos-arm64.tar.gz`
- SHA256: `2f435c736459a3d30279835038fc7e67028875be837f467c87521ea14da3a9ee`
- CLI signature identifier: `boo.peekaboo.peekaboo` (runtime library `boo.peekaboo.peekaboo.libswiftCompatibilitySpan`)
- Developer ID team: `FWJYW4S8P8` (Apple-anchored Developer ID Application requirement)
- Managed directory: `~/.local/share/peekaboo/4.9.5`

**App (`--install-app`, Bridge mode only)** installs `/Applications/Peekaboo.app`
when absent, then launches it and waits up to 20 seconds for the selected Bridge
socket. Apps elsewhere (for example `~/Applications`) may not appear in the macOS
privacy lists, so only `/Applications` is used. An existing official app at 4.9.5
or newer (it self-updates) is deep-signature-verified, never replaced; an older or
foreign bundle at that path is refused with update/removal guidance.

- URL: `https://github.com/openclaw/Peekaboo/releases/download/v4.9.5/Peekaboo-4.9.5.app.zip`
- SHA256: `d176301b0ef4b695c11cbdb8c0bdbbc71f47f866f76c734ddad8343ab5f93e38`
- App identifier: `boo.peekaboo.mac`, same Developer ID team, `codesign --deep --strict`

The app archive is extracted with `/usr/bin/ditto` into private staging next to the
managed CLI directory, verified (single `Peekaboo.app`, signature, exact version),
then moved into `/Applications` with an exclusive rename (`RENAME_EXCL`), so even an
empty directory that appears there is never replaced. Apply rechecks the MCP config
and profile before touching `/Applications`. Launching can recreate the socket, so
`--install-app` plans do not bind its stamp.

Downloads disable proxies and allow HTTPS redirects only to exact official GitHub
asset hosts. Transfer is capped at 25 MB/s (200 Mbps), 150 MiB compressed, a
120-second transfer budget (15-second socket timeout), and 300 MiB expanded.
A killable download child enforces a 135-second hard deadline including DNS and
redirects. CLI extraction accepts only expected flat regular files, including the
required signed `libswiftCompatibilitySpan.dylib`; no archive links, traversal,
devices, or overwrite extraction. The CLI and its adjacent compatibility library
are verified before execution; staged CLI startup must pass before promotion.
Promotion uses exclusive directory creation, not replacement. Staging is cleaned
on ordinary failures/cancellation; an interrupted promotion can leave a partial
version directory. Inspect it before retrying—no automatic destructive cleanup.

## Configuration and ownership

For new native setups, the exact direct, full-catalog entry is:

```json
{
  "mcpServers": {
    "peekaboo": {
      "command": "/absolute/path/to/peekaboo",
      "args": ["mcp", "--no-remote", "--allow-foreground"],
      "timeout": 30,
      "enabled": true,
      "exposure": "codemode"
    }
  }
}
```

The exact Bridge entry differs only in `args`:

```json
{
  "mcpServers": {
    "peekaboo": {
      "command": "/absolute/path/to/peekaboo",
      "args": ["mcp", "--bridge-socket", "/absolute/path/to/bridge.sock", "--allow-foreground"],
      "timeout": 30,
      "enabled": true,
      "exposure": "codemode"
    }
  }
}
```

Native MCP connects at session startup, not lazily. `codemode` controls tool
discovery through scripts (also accessible to `tool_search`), not connection lifecycle. The MCP migration
produces this same entry, so Peekaboo plan/apply/check recognize it afterward.
Existing legacy adapter entries instead use `lifecycle: "lazy-keep-alive"`,
`requestTimeoutMs: 30000`, and `directTools: false` in place of native
`timeout`/`enabled`/`exposure`; they remain supported without normalization.

No include/exclude filters or approval lists are installed. Any `env` or other
entry shape conflicts, including the 4.5.0-era `PEEKABOO_DISABLE_TOOLS=browser`
Bridge entry (a setup-managed one is replaced by `--install`). Foreground support is explicit; this is not a
semantic authorization boundary or desktop lock. Optional
agent/analysis tools may need their own provider setup; no credentials are copied
or configured. Do not operate the same desktop concurrently from multiple sessions.

Apply uses the existing per-user `~/.config/pi-shared/update.lock`, revalidates the
plan under that lock, checks for config changes after startup and immediately
before atomic replacement, and preserves unrelated JSON/settings/servers.
Malformed/duplicate-key JSON, conflicting Peekaboo entries, unsafe ownership,
shared-writable paths, and symlink/hardlink targets are rejected. Config reads are
limited to 1 MiB. New config is mode 0600; an already correct entry is left untouched
(including inode, mode and formatting). Directory traversal uses pinned,
no-follow descriptors. The advisory lock coordinates pi-shared writers, not
arbitrary same-user editors: it is not a defense against a malicious process
racing the final replacement or executable launch. Avoid editing config during
apply. Failed startup never writes config.

There is **no setup/module receipt** for this standalone operation. Existing
manually created receipts are neither adopted nor changed. Ownership is the one
MCP entry and, only with `--install`, the private version directory. An app installed
by `--install-app` is an ordinary user application afterwards (it self-updates). Manual uninstall:
stop/disconnect the Peekaboo MCP process through its owning Pi session, remove only
the matching `mcpServers.peekaboo` entry while preserving all other settings, and
remove the private version directory only after confirming nothing else uses it.
Do not remove separately managed binaries, applications, TCC grants, or other MCP
entries. General `pi-shared uninstall` intentionally does not own this integration.

## Explicit verification and JSON contract

`check` performs bounded read-only subprocess probes (15 seconds and 1 MiB combined
output per probe), in a minimal environment without inherited secrets or DYLD
injection variables:

1. Developer ID signature/identifier verification using `/usr/bin/codesign`.
2. The selected executable's `--version`, requiring exactly 4.9.5.
3. Direct: `permissions status --no-remote --json`, requiring `data.source: "local"`.
   Bridge: `permissions status --bridge-socket ABS --json`, requiring
   `data.source: "bridge"`. Both require a successful structured snapshot for Screen Recording,
   Accessibility and Event Synthesizing. A wrong/missing source or incomplete
   snapshot is rejected rather than reported as granted evidence.

It never requests permissions, resets TCC, installs/starts the app Bridge, captures a
screen, lists apps/windows, dispatches an action, or calls a model provider. A
denied/unknown permission produces nonzero exit. This strict check includes Event
Synthesizing even though some background accessibility actions do not require it.
Granted direct permissions describe **this setup process's responsible host**, not
a later Pi MCP process. Bridge permissions describe the selected desktop app's
Bridge permission owner, not local CLI grants. Denial guidance names the selected
owner; permissions and required restarts remain manual. MCP initialize/listTools
and MCP-mediated permissions are **not implemented by this backend**. No tool
count (including a native-only count) is invented or treated as readiness.

After applying, restart Pi or `/reload` and discover the server through its selected
MCP backend (`/mcp`; codemode or `tool_search` for native tools). Runtime upgrades
require restarting Pi. For native connection checks, separately approve
`env PI_CODING_AGENT_DIR=AGENT pi mcp list`, which starts every enabled server. Verify permissions again from that actual host. These steps do not imply
that the current Pi session has reloaded config. Only separately authorized,
scoped observation/action tests could provide desktop evidence.

With `--json`, stdout contains exactly one result object, including failures and
invalid arguments (help remains ordinary help). No child stdout/stderr is printed.
Exit 0 means the requested operation succeeded; operational failures return 1,
argument failures 2, and cancellation 130. Every result has:

```json
{
  "schemaVersion": 2,
  "component": "peekaboo",
  "action": "plan",
  "ok": true,
  "summary": "...",
  "actions": [],
  "warnings": [],
  "errors": [],
  "nextSteps": [],
  "planId": "<64 lowercase hex characters; only when inspection succeeded>",
  "evidence": {
    "mode": "direct",
    "bridgeSocketPath": null,
    "bridgeSocketState": "not-applicable",
    "permissionSource": null,
    "binaryPath": null,
    "binaryPresent": false,
    "configuration": "missing",
    "runnable": "not-tested",
    "permissions": {
      "screenRecording": "unknown",
      "accessibility": "unknown",
      "eventSynthesizing": "unknown"
    },
    "mcp": "not-tested",
    "toolCount": null,
    "desktop": "not-tested"
  }
}
```

All result objects, including operational/argument failures and cancellation,
use schema 2 and retain all prior evidence fields. `mode` is `direct` or `bridge`;
`bridgeSocketPath` is the selected absolute path or null; `bridgeSocketState` is
`not-applicable`, `missing`, `present` or `invalid`. `permissionSource` is null
until a valid complete permissions probe, then `local` or `bridge` (including a
valid snapshot with denied permissions). Unparsed argument failures retain the
default unknown-evidence values shown above; invalid action names use
`action: "unknown"`.

Other configuration values: `matching`, `conflict`, `invalid`. Runnable values:
`yes`, `no`, `not-tested`; permission values: `unknown`, `granted`, `denied`.
`mcp` remains `not-tested`, `toolCount` remains null, and `desktop` always remains
`not-tested` in schema 2. Socket evidence is not MCP or desktop readiness.

Implementation references for the pinned upstream JSON/version shape:
[PermissionHelpers.swift](https://github.com/openclaw/Peekaboo/blob/v4.9.5/Apps/CLI/Sources/PeekabooCLI/Helpers/PermissionHelpers.swift),
[PermissionsCommand.swift](https://github.com/openclaw/Peekaboo/blob/v4.9.5/Apps/CLI/Sources/PeekabooCLI/Commands/Core/PermissionsCommand.swift),
[Version.swift](https://github.com/openclaw/Peekaboo/blob/v4.9.5/Apps/CLI/Sources/PeekabooCLI/Version.swift).
