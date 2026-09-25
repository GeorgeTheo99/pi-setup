"""Explicit user setup teardown. Homebrew remains the software package owner."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import stat
import subprocess
import tempfile
import time

import update_support


def checked(path, *, link=False):
    path = Path(path).absolute()
    home = Path.home().resolve()
    if not path.is_relative_to(home) or path == home or '..' in path.parts:
        raise RuntimeError(f"Teardown target must be inside your home: {path}")
    for parent in path.parents:
        if parent == home:
            break
        if parent.is_symlink():
            raise RuntimeError(f"Symlinked parent; reconcile explicitly: {parent}")
        if parent.exists():
            info = parent.stat()
            if info.st_uid != os.getuid() or info.st_mode & 0o022:
                raise RuntimeError(f"Unsafe directory: {parent}")
    if not path.exists() and not path.is_symlink():
        return None
    info = path.lstat()
    if link and stat.S_ISLNK(info.st_mode) and info.st_uid == os.getuid():
        return ('link', os.readlink(path))
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_nlink != 1 or info.st_mode & 0o022 or info.st_size > 8 * 1024 * 1024):
        raise RuntimeError(f"Unsafe or oversized teardown file: {path}")
    return ('file', hashlib.sha256(path.read_bytes()).hexdigest())


class Plan:
    def __init__(self):
        self.actions = []
        self.retained = []
        self.absent_services = []

    def add(self, path, description, *, content=None, service=None):
        path = Path(path)
        before = checked(path, link=True)
        if before is not None:
            action = dict(path=path, description=description, before=before, content=content, service=service)
            for index, existing in enumerate(self.actions):
                if existing['path'] == path:
                    self.actions[index] = action
                    return
            self.actions.append(action)

    def show(self):
        print("User setup teardown (Homebrew packages are not removed):")
        for action in self.actions:
            print(f"  {action['description']}: {action['path']}")
        if not self.actions:
            print("  Nothing remains to detach.")
        for path, reason in self.retained:
            print(f"  RETAIN {path}: {reason}")
        print("Every changed file/link is backed up privately before service stops or file edits.")

    def apply(self):
        # Validate the entire plan before any service or configuration mutation.
        for action in self.actions:
            if checked(action['path'], link=True) != action['before']:
                raise RuntimeError(f"Target changed; rerun the plan: {action['path']}")
            if action['service']:
                service_state(action['path'], action['service'])
        for path, label in self.absent_services:
            if service_state(path, label):
                raise RuntimeError(f'Service {label} is loaded but its plist is missing; reconcile before teardown')
        if not self.actions:
            return
        base = Path.home() / '.local/state/pi-shared/uninstall'
        checked(base / 'probe')
        base.mkdir(parents=True, mode=0o700, exist_ok=True)
        archive = Path(tempfile.mkdtemp(prefix=time.strftime('%Y%m%dT%H%M%S-'), dir=base))
        # Keep link targets as text, never copy the file they reference.
        manifest = []
        for i, action in enumerate(self.actions):
            path = action['path']
            backup = archive / str(i)
            payload = os.readlink(path).encode() if path.is_symlink() else path.read_bytes()
            fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'wb') as stream:
                stream.write(payload)
            manifest.append(dict(path=str(path), backup=str(backup), kind=action['before'][0],
                                 mode=stat.S_IMODE(path.lstat().st_mode), description=action['description']))
        report = archive / 'manifest.json'
        report.write_text(json.dumps(manifest, indent=2) + '\n')
        report.chmod(0o600)
        print(f"Recovery archive: {archive}", flush=True)
        for action in self.actions:
            path = action['path']
            if checked(path, link=True) != action['before']:
                raise RuntimeError(f"Target changed; recovery archive retained: {path}")
            if action['service']:
                stop_service(path, action['service'])
            if action['content'] is None:
                path.unlink()
            else:
                fd, temporary = tempfile.mkstemp(prefix='.teardown-', dir=path.parent)
                try:
                    with os.fdopen(fd, 'w') as stream:
                        stream.write(json.dumps(action['content'], indent=2) + '\n')
                    os.replace(temporary, path)
                finally:
                    if os.path.exists(temporary):
                        os.unlink(temporary)
        print("User setup detached. Retained data is listed above; restart existing Pi sessions/shells.")


def service_state(path, label):
    target = f'gui/{os.getuid()}/{label}'
    result = subprocess.run(['launchctl', 'print', target], capture_output=True, text=True)
    if result.returncode:
        if 'Could not find service' in result.stderr:
            return False
        raise RuntimeError(f"Cannot establish service state for {label}; no plist removed")
    # A matching plist on disk is not sufficient: another process may own this label.
    if not re.search(r'^\s*path = ' + re.escape(str(path)) + r'\s*$', result.stdout, re.M):
        raise RuntimeError(f"Loaded service does not use the recorded plist: {label}")
    return True


def stop_service(path, label):
    if not service_state(path, label):
        return
    target = f'gui/{os.getuid()}/{label}'
    subprocess.run(['launchctl', 'bootout', target], check=True, capture_output=True)
    for _ in range(20):
        result = subprocess.run(['launchctl', 'print', target], capture_output=True, text=True)
        if result.returncode and 'Could not find service' in result.stderr:
            return
        time.sleep(0.1)
    raise RuntimeError(f"Service is still registered or cannot be verified: {label}; plist retained")


def detach_packages(plan, profile, package):
    path = profile / 'settings.json'
    if checked(path) is None:
        return
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise RuntimeError(f"Invalid settings: {path}")
    changed = False
    for field in ('packages', 'extensions'):
        entries = data.get(field, [])
        if not isinstance(entries, list):
            raise RuntimeError(f"Invalid {field}: {path}")
        keep = []
        for entry in entries:
            source = (entry.get('source') or entry.get('path')) if isinstance(entry, dict) else entry
            target = None
            if isinstance(source, str) and not source.startswith(('npm:', 'git:', 'http:', 'https:', 'ssh:')):
                target = (profile / Path(source).expanduser()).resolve()
            if target in {package.resolve(), (package / 'extensions').resolve()}:
                changed = True
            else:
                keep.append(entry)
        if field in data:
            data[field] = keep
    if changed:
        plan.add(path, 'Detach shared package; preserve other settings', content=data)


def build(data, archive_config=False):
    home = Path.home()
    root = Path(data['code_root'])
    shared = root / 'pi-shared'
    settings = data.get('settings', {})
    plan = Plan()
    profiles = {Path(data['agent_dir']), Path(settings.get('PI_SHARED_OMLX_AGENT_DIR', home / '.pi-omlx/agent'))}
    for profile in sorted(profiles):
        detach_packages(plan, profile, shared)
        link = profile / 'AGENTS.md'
        if link.is_symlink() and link.resolve() == (shared / 'AGENTS.md').resolve():
            plan.add(link, 'Unlink shared instructions')
    bindir = Path(settings.get('PI_SHARED_BIN_DIR', home / '.local/bin'))
    for name in ('pi-catalog', 'pi-gateway', 'pi-omlx-repair', 'pi-vanilla'):
        path = bindir / name
        if path.is_symlink() and path.resolve() == (shared / 'bin' / name).resolve():
            plan.add(path, 'Unlink shared helper')
    for name in data['modules']:
        if name == 'pi-shared':
            continue
        saved = data.get('services', {}).get(name)
        if not saved:
            raise RuntimeError(f"No saved service ownership for {name}; reconcile explicitly before teardown")
        path = Path(saved['path'])
        if checked(path) is None:
            # A previous partial teardown may already have removed this plist.
            if not re.fullmatch(r'[A-Za-z0-9_.-]+', path.stem):
                raise RuntimeError('Invalid service label')
            plan.absent_services.append((path, path.stem))
            continue
        service = update_support.read_service(path, name)
        if update_support.identity(service) != saved['identity']:
            raise RuntimeError(f"{name} service ownership changed; refusing teardown")
        label = service.get('Label')
        if not isinstance(label, str) or not re.fullmatch(r'[A-Za-z0-9_.-]+', label):
            raise RuntimeError('Invalid service label')
        plan.add(path, f'Stop and unregister {name} (affects all its clients)', service=label)
        env = service.get('EnvironmentVariables', {})
        if name == 'model-gateway':
            for key, fallback in [('MODEL_GATEWAY_CONFIG', root / name / 'config/config.yaml'),
                                  ('MODEL_GATEWAY_MODEL_INFO', root / name / 'model-info.json')]:
                config = Path(env.get(key, str(fallback)))
                # Gateway config can be shared with other clients. Even explicit
                # archive mode never removes its actual data or follows its links.
                plan.retained.append((config, 'workspace/provider configuration; archive separately for fresh gateway onboarding'))
    if data.get('omlx') == 'install':
        raise RuntimeError('Stop/unregister the Homebrew oMLX service explicitly before teardown; automatic oMLX removal is not supported')
    if data.get('cli_file'):
        path = Path(data['cli_file'])
        if checked(path) is not None:
            config = json.loads(path.read_text())
            args = config.get('generation', {}).get('args', [])
            if not isinstance(args, list) or '--shared-dir' not in args or args[args.index('--shared-dir') + 1:][:1] != [str(shared.resolve())]:
                raise RuntimeError(f'Launcher ownership is unrecognized: {path}')
            plan.add(path, 'Archive generated launcher routing')
    for profile in sorted(profiles):
        path = profile / 'models.json'
        if not path.exists() and not path.is_symlink():
            continue
        if not archive_config:
            plan.retained.append((path, 'model configuration; use --archive-config to detach gateway provider'))
            continue
        # A symlink is itself the profile registration; leave its target intact.
        if path.is_symlink():
            target = path.resolve(strict=True)
            checked(target)
            config = json.loads(target.read_text())
            if not isinstance(config, dict) or set(config.get('providers', {})) != {'model-gateway'}:
                raise RuntimeError(f'Catalog link contains unrelated providers; reconcile explicitly: {path}')
            plan.add(path, 'Archive model catalog symlink; retain target')
            plan.retained.append((path.resolve(), 'catalog symlink target retained'))
        else:
            checked(path)
            config = json.loads(path.read_text())
            providers = config.get('providers', {})
            if not isinstance(providers, dict):
                raise RuntimeError(f'Invalid providers: {path}')
            if 'model-gateway' in providers:
                del providers['model-gateway']
                plan.add(path, 'Archive/remove model-gateway provider; preserve other providers', content=config)
        settings_path = profile / 'settings.json'
        if checked(settings_path) is not None:
            pending = next((a for a in plan.actions if a['path'] == settings_path), None)
            config = pending['content'] if pending else json.loads(settings_path.read_text())
            if config.get('defaultProvider') == 'model-gateway':
                config.pop('defaultProvider')
                config.pop('defaultModel', None)
                plan.add(settings_path, 'Detach shared package/default gateway selection; preserve other settings', content=config)
    plan.retained += [(root, 'module checkouts, dependencies and local edits'),
                      (home / '.pi', 'sessions, credentials, custom settings and research/browser configuration'),
                      (home / '.pi-omlx', 'sessions, credentials and custom settings'),
                      (home / '.claude/model-aliases.json', 'catalog may be used by other clients'),
                      (home / 'Library/Logs', 'service logs'),
                      (home / '.config/pi-shared/update.lock', 'inert operation lock'),
                      ('Homebrew packages and other applications', 'remove packages separately after teardown')]
    plan.add(home / '.config/pi-shared/setup.json', 'Archive setup receipt (last; retryable after failure)')
    return plan


def uninstall(args, read_state, state_path, lock):
    if not state_path().exists() and not state_path().is_symlink():
        print('No managed setup receipt; nothing changed. Source-only/unrecorded installations require explicit reconciliation.')
        return 0
    data = read_state(allow_incomplete=True)
    if data['version'] != 2:
        raise RuntimeError('Teardown requires a schema-2 ownership receipt')
    plan = build(data, args.archive_config)
    plan.show()
    if args.plan:
        return 0
    if not args.yes:
        raise RuntimeError('Review --plan, then pass --yes to detach this setup; shared services affect all clients')
    with lock():
        if read_state(allow_incomplete=True) != data:
            raise RuntimeError('Receipt changed; rerun teardown plan')
        plan.apply()
    return 0
