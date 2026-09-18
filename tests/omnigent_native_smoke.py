#!/usr/bin/env python3
"""Explicit macOS native-launch smoke. Creates disposable processes/files; never prompts a model.

Not part of doctor or installation. Only run against reviewed local packages.
The fixture uses a new HOME, no credentials, an inert model endpoint, and --offline.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request


PROBE = r'''
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
export default function(pi) {
  let timer;
  pi.on("session_start", async (_event, ctx) => {
    const tools = pi.getAllTools();
    const commands = pi.getCommands();
    const sources = [...tools, ...commands].map(x => x.sourceInfo?.path).filter(Boolean);
    const roots = EXPECTED_PACKAGES;
    const loaded = roots.map(root => sources.some(source => {
      try { return fs.realpathSync(source).startsWith(fs.realpathSync(root) + path.sep); }
      catch { return false; }
    }));
    const names = tools.map(x => x.name);
    const report = {
      mode: ctx.mode,
      omnigent: process.env.OMNIGENT === "1",
      profile: process.env.PI_CODING_AGENT_DIR || path.join(os.homedir(), ".pi", "agent"),
      executable: fs.realpathSync(process.argv[1]),
      provider: ctx.model?.provider,
      model: ctx.model?.id,
      packages_loaded: loaded,
      basic_tools: ["read", "bash", "edit", "write"].every(name => names.includes(name)),
      bridge_command: commands.some(x => x.name === "omnigent"),
      inference_tested: false,
    };
    fs.writeFileSync(REPORT_PATH + ".tmp", JSON.stringify(report), {mode: 0o600});
    fs.renameSync(REPORT_PATH + ".tmp", REPORT_PATH);
    if (timer) clearTimeout(timer);
    timer = setTimeout(() => ctx.shutdown(), 1000);
  });
  pi.on("session_shutdown", () => { if (timer) clearTimeout(timer); });
}
'''


def fixture_model(source: Path, provider: str, model: str) -> dict:
    """Read declarations, never auth.json; discard every credential/header/command."""
    catalog = json.loads((source / "models.json").read_text())
    entry = catalog.get("providers", {}).get(provider, {})
    found = next((m for m in entry.get("models", []) if m.get("id") == model), None)
    if found is None:
        raise ValueError("Requested provider/model is not declared in the source profile")
    # Exercise the requested identity, not its real transport or credentials.
    return {"providers": {provider: {
        "api": "openai-completions", "baseUrl": "http://127.0.0.1:9/v1",
        "apiKey": "synthetic-non-inference-fixture",
        "models": [{"id": model, "name": model, "input": ["text"],
                    "contextWindow": 128000, "maxTokens": 1024}],
    }}}


def stock_executable(pi: str) -> Path:
    """Resolve the stock Pi runtime the bootstrap execs into.

    The Homebrew package installs `bin/pi` as a bootstrap wrapper and exposes the
    stock CLI (cli.js) through a sibling `pi-upstream` symlink; the running
    process's argv[1] therefore resolves to that stock executable, not to the
    wrapper. A pre-bootstrap layout has no sibling and `pi` is itself stock.
    """
    sibling = Path(pi).resolve(strict=True).parent / "pi-upstream"
    return (sibling if sibling.exists() else Path(pi)).resolve(strict=True)


def restricted_path(home: Path, executables: dict[str, str], stock: Path) -> str:
    """Omnigent probes other installed CLIs at startup: never expose the caller's PATH."""
    directory = home / "bin"
    directory.mkdir()
    for name, executable in executables.items():
        (directory / name).symlink_to(Path(executable).resolve(strict=True))
    # Expose the stock runtime so the bootstrap `pi` can exec it without the
    # packaging environment; the wrapper resolves it via PI_UPSTREAM_BIN.
    if not (directory / "pi-upstream").exists():
        (directory / "pi-upstream").symlink_to(stock)
    return f"{directory}:/usr/bin:/bin:/usr/sbin:/sbin"


def isolated_env(home: Path, path: str, stock: Path) -> dict[str, str]:
    """Do not inherit credential families, shell startup, or live Pi/Omnigent selectors."""
    temp = home / "tmp"
    temp.mkdir()
    return {
        "HOME": str(home), "PATH": path, "TMPDIR": str(temp),
        # Supply the stock runtime the bootstrap `pi` execs; packaging normally
        # sets this. Without a setup receipt the wrapper routes straight to it.
        "PI_UPSTREAM_BIN": str(stock),
        "USER": os.environ.get("USER", "compatibility-test"),
        "LOGNAME": os.environ.get("LOGNAME", "compatibility-test"),
        "SHELL": "/bin/sh", "TERM": "xterm-256color", "LANG": "en_US.UTF-8",
        "ZDOTDIR": str(home), "XDG_CONFIG_HOME": str(home / ".config"),
        "OMNIGENT_DATA_DIR": str(home / ".omnigent"),
        "OMNIGENT_CONFIG_HOME": str(home / ".omnigent"),
        "OMNIGENT_AUTH_ENABLED": "0", "OMNIGENT_AUTH_PROVIDER": "header",
        "OMNIGENT_LOCAL_SINGLE_USER": "1",  # isolated loopback fixture only
        "OMNIGENT_NO_UPDATE_CHECK": "1", "OMNIGENT_TELEMETRY_ENABLED": "0",
        "DATABRICKS_CONFIG_FILE": str(home / "no-databrickscfg"),
        "PI_OFFLINE": "1", "PI_TELEMETRY": "0", "PI_SKIP_VERSION_CHECK": "1",
        "PYTHONDONTWRITEBYTECODE": "1", "UV_OFFLINE": "true", "UV_PYTHON_DOWNLOADS": "never",
    }


def launch_args(omnigent: str, server: str, provider: str, model: str, probe: Path) -> list[str]:
    # Explicit provider/model bypass Omnigent-managed discovery (including cap probes).
    # --offline must be argv: the runner does not forward arbitrary PI_* environment.
    return ["/usr/bin/script", "-q", "/dev/null", omnigent, "pi", "--server", server, "--",
            "--provider", provider, "--model", model, "--offline", "--extension", str(probe)]


def stop_owned_process(process: subprocess.Popen | None) -> None:
    if process is None or process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=5)
    finally:
        if process.stdin is not None:
            process.stdin.close()


def process_rows() -> dict[int, tuple[int, str]]:
    output = subprocess.check_output(["/bin/ps", "-axo", "pid=,ppid=,stat="], text=True)
    return {int(pid): (int(parent), state) for pid, parent, state in (line.split() for line in output.splitlines())}


def fixture_pids(home: Path, server: str | None, tmux: str, processes: list) -> set[int]:
    roots = {p.pid for p in processes if p is not None and p.poll() is None}
    for record in (home / ".omnigent/daemons").glob("*.json"):
        data = json.loads(record.read_text())
        if data.get("server_url") == server and isinstance(data.get("pid"), int):
            roots.add(data["pid"])
    for sock in (home / "tmp").rglob("tmux.sock"):
        if not sock.is_socket():
            continue
        result = subprocess.run([tmux, "-S", str(sock), "display-message", "-p", "#{pid}"],
                                capture_output=True, text=True, timeout=3)
        if result.returncode == 0 and result.stdout.strip().isdigit():
            roots.add(int(result.stdout.strip()))
    rows = process_rows()
    while True:
        children = {pid for pid, (parent, _state) in rows.items() if parent in roots}
        if children <= roots:
            return roots
        roots.update(children)


def wait_stopped(pids: set[int]) -> bool:
    deadline = time.monotonic() + 15
    while True:
        rows = process_rows()
        live = [pid for pid in pids if pid in rows and not rows[pid][1].startswith("Z")]
        if not live:
            return True
        if time.monotonic() >= deadline:
            return False  # Never signal unowned/reused PIDs to make a check pass.
        time.sleep(0.25)


def validate_report(report: dict, home: Path, pi: Path, provider: str, model: str, count: int) -> None:
    expected = {
        "mode": "tui", "omnigent": True, "profile": str(home / ".pi/agent"),
        "executable": str(pi.resolve()), "provider": provider, "model": model,
        "packages_loaded": [True] * count, "basic_tools": True,
        "bridge_command": True, "inference_tested": False,
    }
    if report != expected:
        raise ValueError("Native Pi report did not match the requested executable/profile/model/packages")


def smoke(source: Path, packages: list[Path], provider: str, model: str) -> dict:
    if sys.platform != "darwin":
        raise ValueError("This native-TUI smoke currently targets macOS only")
    paths = {name: shutil.which(name) for name in ("pi", "omnigent", "tmux", "node")}
    if not all(paths.values()):
        raise ValueError("pi, omnigent, tmux, and node must already be installed on PATH")
    stock = stock_executable(paths["pi"])
    catalog = fixture_model(source, provider, model)
    roots = [p.expanduser().resolve(strict=True) for p in packages]
    shared = next((p for p in roots if (p / "bin/pi-profile-check").is_file()), None)
    if shared is None:
        raise ValueError("Include the reviewed pi-shared package")
    if any(not (p / "package.json").is_file() for p in roots):
        raise ValueError("Every --package must be an existing local Pi package")
    # macOS's default /var/folders TMPDIR makes nested tmux socket paths exceed
    # sockaddr_un.sun_path. Keep the entire disposable HOME short and private.
    home = Path(tempfile.mkdtemp(prefix="pi-omni-", dir="/tmp"))
    env = isolated_env(home, restricted_path(home, paths, stock), stock)
    work = home / "workspace"
    work.mkdir()
    agent = home / ".pi/agent"
    agent.mkdir(parents=True)
    (agent / "settings.json").write_text(json.dumps({"packages": list(map(str, roots)),
        "defaultProvider": "unused-fixture-default", "defaultModel": "unused-fixture-default",
        "enableInstallTelemetry": False}))
    (agent / "models.json").write_text(json.dumps(catalog))
    (agent / "AGENTS.md").symlink_to(shared / "AGENTS.md")
    report_path = home / "native-report.json"
    probe = home / "probe.ts"
    probe.write_text(PROBE.replace("EXPECTED_PACKAGES", json.dumps(list(map(str, roots))))
                     .replace("REPORT_PATH", json.dumps(str(report_path))))
    server_process = launch_process = None
    server = None
    successful = False
    cleanup_ok = True
    try:
        with (home / "probe.log").open("w") as output:
            argv = [sys.executable, str(shared / "bin/pi-profile-check"), "--agent-dir", str(agent)]
            for root in roots:
                argv += ["--expect-package", str(root)]
            subprocess.run(argv, env=env, cwd=work, stdout=output, stderr=output, check=True, timeout=90)
        # Use an owned foreground server, not Omnigent's persistent default server.
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        server = f"http://127.0.0.1:{port}"
        with (home / "server.log").open("w") as output:
            server_process = subprocess.Popen([paths["omnigent"], "server", "--host", "127.0.0.1",
                "--port", str(port), "--no-open"], env=env, cwd=work, stdout=output, stderr=output,
                start_new_session=True)
        deadline = time.monotonic() + 60
        while True:
            if server_process.poll() is not None:
                raise RuntimeError("Isolated server exited before health check")
            try:
                with urllib.request.urlopen(server + "/health", timeout=1) as response:
                    healthy = json.load(response).get("status") == "ok"
                if healthy:
                    break
            except (OSError, ValueError, urllib.error.URLError):
                pass
            if time.monotonic() >= deadline:
                raise TimeoutError("Isolated server did not become healthy")
            time.sleep(0.25)
        with (home / "launch.log").open("w") as output:
            launch_process = subprocess.Popen(launch_args(paths["omnigent"], server, provider, model, probe),
                # Keep the PTY's input open but send nothing. DEVNULL injects Ctrl-D
                # through BSD script and can quit Pi before session_start runs.
                env=env, cwd=work, stdin=subprocess.PIPE, stdout=output, stderr=output,
                start_new_session=True)
        deadline = time.monotonic() + 120
        while not report_path.is_file():
            if launch_process.poll() is not None:
                raise RuntimeError("Native launch exited without its structured report")
            if time.monotonic() >= deadline:
                raise TimeoutError("Native launch did not produce its structured report")
            time.sleep(0.25)
        report = json.loads(report_path.read_text())
        validate_report(report, home, stock, provider, model, len(roots))
        successful = True
    finally:
        snapshot_ok = True
        try:
            owned = fixture_pids(home, server, paths["tmux"], [launch_process, server_process])
        except (OSError, ValueError, subprocess.SubprocessError):
            owned = set()
            snapshot_ok = False
        stop_owned_process(launch_process)
        if server is not None:
            # Never use `omnigent server stop`: that can sweep an untracked default-port server.
            with (home / "cleanup.log").open("w") as output:
                try:
                    cleanup = subprocess.run([paths["omnigent"], "host", "stop", "--server", server],
                        env=env, cwd=work, stdout=output, stderr=output, timeout=45)
                    cleanup_ok = cleanup_ok and cleanup.returncode == 0
                except subprocess.TimeoutExpired:
                    cleanup_ok = False
                if not cleanup_ok:
                    try:
                        cleanup = subprocess.run([paths["omnigent"], "host", "stop", "--server", server,
                            "--daemon-only", "--force"], env=env, cwd=work,
                            stdout=output, stderr=output, timeout=30)
                        cleanup_ok = cleanup.returncode == 0
                    except subprocess.TimeoutExpired:
                        cleanup_ok = False
        stop_owned_process(server_process)
        cleanup_ok = wait_stopped(owned) and cleanup_ok and snapshot_ok
        if successful and cleanup_ok:
            # All owned processes must be stopped before removing their HOME.
            shutil.rmtree(home)
        else:
            print(f"Private diagnostic artifacts retained: {home}", file=sys.stderr)
    if not cleanup_ok:
        raise RuntimeError("Isolated host cleanup failed; inspect the retained artifacts")
    return {"native_launch": "passed", "model_selection": f"{provider}/{model}",
            "packages": list(map(str, roots)), "profile_import": "passed",
            "inference": "not_tested", "cleanup": "passed"}


def main() -> int:
    def interrupted(_signum, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupted)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent-dir", type=Path, required=True, help="Read only models.json declarations here")
    parser.add_argument("--package", action="append", type=Path, required=True, help="Reviewed local package; preserve load order")
    parser.add_argument("--provider", required=True)
    parser.add_argument("--model", required=True)
    args = parser.parse_args()
    try:
        result = smoke(args.agent_dir.expanduser().resolve(), args.package, args.provider, args.model)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"FAIL: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
