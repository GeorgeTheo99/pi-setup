# Standalone Peekaboo setup

`pi-shared peekaboo` is an opt-in macOS CLI integration, **not** a setup module,
service, application installer, or part of `setup`, `update`, `doctor` or their
receipts. It changes neither Pi runtime nor OS permissions. The shared Pi frontend
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

All actions accept `--config /absolute/path/to/mcp.json`; the default is
`~/.config/mcp/mcp.json`. Repeat the **same binary/config/install choices** from
`plan` in `apply`. A changed config, executable or adjacent runtime library
invalidates approval. There are no interactive prompts. `--yes` alone is not enough.
Help never inspects configuration or starts anything.

`plan` and `status` only inspect local files, without subprocesses, network,
locks, or writes. They can succeed with a missing installation: `ok` means the
requested inventory operation succeeded, not that Peekaboo is ready. They read
existing MCP JSON to preserve it, but never disclose server settings, secrets,
permission instructions from child output, or raw UI content.

Discovery uses only an explicit `--binary`, the existing correctly shaped
`mcpServers.peekaboo.command`, the managed 4.5.0 path, or known Homebrew
`/opt/homebrew/bin/peekaboo` / `/usr/local/bin/peekaboo` links into their respective
`Cellar/peekaboo` directories. It never searches PATH or the working directory.
Explicit and configured binary paths must be normalized absolute regular
executables without symlinks, hardlinks, setuid/setgid bits, or shared-write access.
For a Homebrew symlink supplied explicitly, select its actual Cellar executable.
Signatures and actual versions cannot be verified by offline plans.

## Optional missing-binary install

```sh
pi-shared peekaboo plan --install --json
pi-shared peekaboo apply --install --yes --expected-plan <planId> --json
```

**Compatibility/security warning:** the pin is official CLI **4.5.0**, which
lacks newer input-safety fixes, including modifier cleanup. CLI 4.6.0 has a known
Swift startup defect; the 4.5.0 app-Bridge route has a separate startup defect.
This integration uses direct stdio only. Successful startup is not evidence of
safe complex keyboard/foreground workflows. Reassess a fixed official release
before expanding reliance on desktop automation.

The first implementation supports automatic install on **Apple Silicon macOS
only**. No Intel archive metadata is assumed. Existing signed binaries must also
report exactly 4.5.0; unsupported versions are rejected, never downgraded. Installation
is refused when a binary or managed version directory already exists. It never
replaces a custom configured command, adds a global symlink, or runs Homebrew.

Pinned archive:

- URL: `https://github.com/openclaw/Peekaboo/releases/download/v4.5.0/peekaboo-macos-arm64.tar.gz`
- SHA256: `a65323e5c79a0094c860199c86f99248c60955d9803ac2fa7a62b18494186beb`
- CLI signature identifier: `boo.peekaboo.peekaboo`
- Developer ID team: `FWJYW4S8P8` (Apple-anchored Developer ID Application requirement)
- Managed directory: `~/.local/share/peekaboo/4.5.0`

Downloads disable proxies and allow HTTPS redirects only to exact official GitHub
asset hosts. Transfer is capped at 25 MB/s (200 Mbps), 150 MiB compressed, a
120-second transfer budget (15-second socket timeout), and 300 MiB expanded.
A killable download child enforces a 135-second hard deadline including DNS and
redirects. Extraction accepts only expected flat regular files, including the
required signed `libswiftCompatibilitySpan.dylib`; no archive links, traversal,
devices, or overwrite extraction. The CLI and its adjacent compatibility library
are verified before execution; staged CLI startup must pass before promotion.
Promotion uses exclusive directory creation, not replacement. Staging is cleaned
on ordinary failures/cancellation; an interrupted promotion can leave a partial
version directory. Inspect it before retrying—no automatic destructive cleanup.

## Configuration and ownership

The exact full-catalog entry is:

```json
{
  "mcpServers": {
    "peekaboo": {
      "command": "/absolute/path/to/peekaboo",
      "args": ["mcp", "--no-remote", "--allow-foreground"],
      "lifecycle": "lazy-keep-alive",
      "requestTimeoutMs": 30000,
      "directTools": false
    }
  }
}
```

No include/exclude filters or approval lists are installed. Foreground support is
explicit; this is not a semantic authorization boundary or desktop lock. Optional
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
MCP entry and, only with `--install`, the private version directory. Manual uninstall:
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
2. The selected executable's `--version`, requiring exactly 4.5.0.
3. `permissions status --no-remote --json`, accepting only a structured local-host
   snapshot for Screen Recording, Accessibility and Event Synthesizing.

It never requests permissions, resets TCC, starts a service/app Bridge, captures a
screen, lists apps/windows, dispatches an action, or calls a model provider. A
denied/unknown permission produces nonzero exit. This strict check includes Event
Synthesizing even though some background accessibility actions do not require it.
Granted permissions describe **this setup process's responsible host**, not a later
Pi adapter process. MCP initialize/listTools and adapter-mediated permissions are
**not implemented in this version**. No tool count is invented or treated as readiness.

After applying, restart Pi or `/reload` and discover the server through its MCP
adapter. Verify permissions again from that actual host. These steps do not imply
that the current Pi session has reloaded config. Only separately authorized,
scoped observation/action tests could provide desktop evidence.

With `--json`, stdout contains exactly one result object, including failures and
invalid arguments (help remains ordinary help). No child stdout/stderr is printed.
Exit 0 means the requested operation succeeded; operational failures return 1,
argument failures 2, and cancellation 130. Every result has:

```json
{
  "schemaVersion": 1,
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

Other configuration values: `matching`, `conflict`, `invalid`. Runnable values:
`yes`, `no`, `not-tested`; permission values: `unknown`, `granted`, `denied`.
`mcp` remains `not-tested`, `toolCount` remains null, and `desktop` always remains
`not-tested` in v1. Unknown/invalid action arguments use `action: "unknown"`.

Implementation references for the pinned upstream JSON/version shape:
[PermissionHelpers.swift](https://github.com/openclaw/Peekaboo/blob/v4.5.0/Apps/CLI/Sources/PeekabooCLI/Helpers/PermissionHelpers.swift),
[PermissionsCommand.swift](https://github.com/openclaw/Peekaboo/blob/v4.5.0/Apps/CLI/Sources/PeekabooCLI/Commands/Core/PermissionsCommand.swift),
[Version.swift](https://github.com/openclaw/Peekaboo/blob/v4.5.0/Apps/CLI/Sources/PeekabooCLI/Version.swift).
