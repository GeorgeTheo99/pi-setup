"""Teardown tests use disposable HOME and stub launchctl, never live services."""
import argparse
import json
import os
from pathlib import Path
import plistlib
import subprocess

import pytest
from test_cli import cli, isolated
import teardown


def put(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))
    path.chmod(0o600)


@pytest.fixture
def installation(isolated):
    home, calls = isolated
    root = home / 'modules'
    profile = home / '.pi/agent'
    data = dict(version=2, status='module-checks-passed', modules=['pi-shared'],
                code_root=str(root), agent_dir=str(profile), settings={},
                cli_file=str(home / '.pi/launcher.json'), services={})
    put(profile / 'settings.json', {'packages': [str(root / 'pi-shared'), 'npm:other'], 'theme': 'custom'})
    put(home / '.pi/launcher.json', {'generation': {'args': ['--shared-dir', str(root / 'pi-shared')]}})
    put(profile / 'auth.json', {'secret': 'preserve-this'})
    cli.write_state(data)
    return home, root, data


def args(**kw):
    return argparse.Namespace(**dict(plan=False, yes=True, archive_config=False) | kw)


def test_preview_and_confirmation_never_mutate(installation):
    home, root, data = installation
    before = {p: p.read_bytes() for p in home.rglob('*') if p.is_file()}
    teardown.uninstall(args(plan=True), cli.read_state, cli.state_path, cli.update_lock)
    with pytest.raises(RuntimeError, match='--yes'):
        teardown.uninstall(args(yes=False), cli.read_state, cli.state_path, cli.update_lock)
    assert before == {p: p.read_bytes() for p in home.rglob('*') if p.is_file()}


def test_detaches_and_archives_preserving_unrelated_data(installation):
    home, root, data = installation
    teardown.uninstall(args(), cli.read_state, cli.state_path, cli.update_lock)
    assert not cli.state_path().exists()
    assert not (home / '.pi/launcher.json').exists()
    assert json.loads((home / '.pi/agent/settings.json').read_text()) == {'packages': ['npm:other'], 'theme': 'custom'}
    assert json.loads((home / '.pi/agent/auth.json').read_text()) == {'secret': 'preserve-this'}
    archives = list((home / '.local/state/pi-shared/uninstall').iterdir())
    assert len(archives) == 1
    assert archives[0].stat().st_mode & 0o077 == 0
    manifest = json.loads((archives[0] / 'manifest.json').read_text())
    assert any('pi-shared' in Path(item['backup']).read_text() for item in manifest)
    assert all(Path(item['backup']).stat().st_mode & 0o077 == 0 for item in manifest)
    assert teardown.uninstall(args(), cli.read_state, cli.state_path, cli.update_lock) == 0


def service(installation):
    home, root, data = installation
    path = home / 'Library/LaunchAgents/com.local.model-gateway.plist'
    path.parent.mkdir(parents=True)
    document = dict(Label='com.local.model-gateway', WorkingDirectory=str(root / 'model-gateway'),
                    ProgramArguments=['/usr/bin/true'], EnvironmentVariables={})
    path.write_bytes(plistlib.dumps(document)); path.chmod(0o600)
    data['modules'].append('model-gateway')
    data['services']['model-gateway'] = dict(path=str(path), identity=cli.update_support.identity(document))
    cli.write_state(data)
    return path


def test_changed_service_identity_fails_before_any_edit(installation):
    path = service(installation)
    doc = plistlib.loads(path.read_bytes()); doc['WorkingDirectory'] = '/other'
    path.write_bytes(plistlib.dumps(doc))
    before = cli.state_path().read_bytes()
    with pytest.raises(RuntimeError, match='ownership changed'):
        teardown.build(installation[2])
    assert cli.state_path().read_bytes() == before


def test_stop_failure_keeps_plist_receipt_and_backup(installation, monkeypatch):
    path = service(installation)
    def run(command, **kw):
        if command[1] == 'print':
            return subprocess.CompletedProcess(command, 0, f'path = {path}\n', '')
        raise subprocess.CalledProcessError(5, command)
    monkeypatch.setattr(teardown.subprocess, 'run', run)
    with pytest.raises(subprocess.CalledProcessError):
        teardown.uninstall(args(), cli.read_state, cli.state_path, cli.update_lock)
    assert path.exists() and cli.state_path().exists()
    assert list((installation[0] / '.local/state/pi-shared/uninstall').glob('*/manifest.json'))


def test_loaded_other_plist_fails_before_edits(installation, monkeypatch):
    service(installation)
    monkeypatch.setattr(teardown.subprocess, 'run', lambda c, **kw: subprocess.CompletedProcess(c, 0, 'path = /different.plist\n', ''))
    before = (installation[0] / '.pi/agent/settings.json').read_bytes()
    with pytest.raises(RuntimeError, match='recorded plist'):
        teardown.uninstall(args(), cli.read_state, cli.state_path, cli.update_lock)
    assert (installation[0] / '.pi/agent/settings.json').read_bytes() == before


def test_absent_service_is_retryable(installation, monkeypatch):
    path = service(installation)
    monkeypatch.setattr(teardown.subprocess, 'run', lambda c, **kw: subprocess.CompletedProcess(c, 113, '', 'Could not find service'))
    teardown.uninstall(args(), cli.read_state, cli.state_path, cli.update_lock)
    assert not path.exists() and not cli.state_path().exists()


@pytest.mark.parametrize('unsafe', ['symlink', 'hardlink', 'parent', 'malformed'])
def test_unsafe_profile_fails_before_changes(installation, unsafe):
    home, root, data = installation
    path = home / '.pi/agent/settings.json'
    if unsafe == 'symlink':
        path.rename(path.with_name('original')); path.symlink_to('original')
    elif unsafe == 'hardlink':
        os.link(path, home / 'duplicate')
    elif unsafe == 'parent':
        path.parent.rename(home / 'real-profile'); path.parent.symlink_to(home / 'real-profile')
    else:
        path.write_text('not-json')
    with pytest.raises((RuntimeError, ValueError)):
        teardown.build(data)
    assert cli.state_path().exists()


def test_archive_models_preserves_other_provider(installation):
    home, root, data = installation
    path = home / '.pi/agent/models.json'
    put(path, {'providers': {'model-gateway': {'models': []}, 'personal': {'secret': 'keep'}}})
    put(home / '.pi/agent/settings.json', {'packages': [str(root / 'pi-shared'), 'npm:other'], 'defaultProvider': 'model-gateway', 'defaultModel': 'old'})
    teardown.uninstall(args(archive_config=True), cli.read_state, cli.state_path, cli.update_lock)
    assert json.loads(path.read_text()) == {'providers': {'personal': {'secret': 'keep'}}}
    assert json.loads((home / '.pi/agent/settings.json').read_text()) == {'packages': ['npm:other']}


def test_symlink_catalog_archive_preserves_target(installation):
    home, root, data = installation
    catalog = home / 'catalog.json'
    put(catalog, {'providers': {'model-gateway': {'models': []}}})
    path = home / '.pi/agent/models.json'; path.symlink_to(catalog)
    original = catalog.read_bytes()
    teardown.uninstall(args(archive_config=True), cli.read_state, cli.state_path, cli.update_lock)
    assert not path.is_symlink() and catalog.read_bytes() == original


def test_symlink_with_other_models_is_not_removed(installation):
    home, root, data = installation
    catalog = home / 'catalog.json'; put(catalog, {'providers': {'personal': {}}})
    (home / '.pi/agent/models.json').symlink_to(catalog)
    with pytest.raises(RuntimeError, match='unrelated providers'):
        teardown.build(data, archive_config=True)


def test_target_changed_after_plan_is_rejected(installation):
    home, root, data = installation
    plan = teardown.build(data)
    put(home / '.pi/agent/settings.json', {'packages': [], 'theme': 'new'})
    with pytest.raises(RuntimeError, match='Target changed'):
        plan.apply()
    assert cli.state_path().exists()


def test_missing_plist_with_loaded_service_is_not_orphaned(installation, monkeypatch):
    path = service(installation)
    path.unlink()
    monkeypatch.setattr(teardown.subprocess, 'run', lambda c, **kw: subprocess.CompletedProcess(c, 0, f'path = {path}\n', ''))
    with pytest.raises(RuntimeError, match='plist is missing'):
        teardown.uninstall(args(), cli.read_state, cli.state_path, cli.update_lock)
    assert cli.state_path().exists()


def test_loaded_service_stops_before_plist_removal(installation, monkeypatch):
    path = service(installation)
    running = True
    def run(command, **kw):
        nonlocal running
        if command[1] == 'bootout':
            assert path.exists()
            assert list((installation[0] / '.local/state/pi-shared/uninstall').glob('*/manifest.json'))
            running = False
            return subprocess.CompletedProcess(command, 0, '', '')
        return subprocess.CompletedProcess(command, 0 if running else 113,
                                           f'path = {path}\n' if running else '',
                                           '' if running else 'Could not find service')
    monkeypatch.setattr(teardown.subprocess, 'run', run)
    teardown.uninstall(args(), cli.read_state, cli.state_path, cli.update_lock)
    assert not path.exists() and not running
