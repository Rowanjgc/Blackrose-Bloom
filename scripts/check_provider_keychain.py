"""Exercise real libsecret in a disposable private bus/keyring, never the desktop's.

No network or real credentials. Requires dbus-run-session, gnome-keyring-daemon,
gdbus and secret-tool. Writes only sanitized outcomes to the requested report.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

from blackrose.credentials import CredentialError, OpenCodeGoKeychain, OpenRouterKeychain


def inside(root):
    if (os.environ.get('BRA_DISPOSABLE_KEYRING') != str(root)
            or os.environ.get('XDG_DATA_HOME') != str(root / 'data')
            or not os.environ.get('DBUS_SESSION_BUS_ADDRESS')):
        raise RuntimeError('Private keyring environment was not established')
    password = 'synthetic-disposable-keyring-password'
    secret = 'synthetic-disposable-provider-key'
    command = ['gnome-keyring-daemon', '--unlock', '--components=secrets',
               '--control-directory=' + str(root / 'control')]
    def start_daemon():
        process = subprocess.Popen([*command, '--foreground'], stdin=subprocess.PIPE,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True)
        process.stdin.write(password)
        process.stdin.close()
        return process

    daemon = start_daemon()
    results = {}
    stage = 'start_private_service'
    try:
        subprocess.run(['gdbus', 'wait', '--session', '--timeout', '10', 'org.freedesktop.secrets'],
                       check=True, capture_output=True, timeout=12)
        # All secret-tool calls use the actual APIKeychain implementation and
        # the private bus/data directory inherited from the outer process.
        for keychain in (OpenCodeGoKeychain(), OpenRouterKeychain()):
            stage = keychain.provider + ':missing'
            keychain.delete()
            try:
                keychain.get()
            except CredentialError as error:
                if 'missing' not in str(error):
                    raise AssertionError('Missing key did not produce the expected diagnostic') from None
            else:
                raise AssertionError('Missing key unexpectedly resolved')
            stage = keychain.provider + ':store_lookup'
            keychain.set(secret)
            if keychain.get() != secret:
                raise AssertionError('Disposable credential round trip failed')
            results[keychain.provider] = {'missing': 'passed', 'store_lookup': 'passed'}

        stage = 'lock_private_collection'
        subprocess.run(['gdbus', 'call', '--session', '--dest', 'org.freedesktop.secrets',
                        '--object-path', '/org/freedesktop/secrets',
                        '--method', 'org.freedesktop.Secret.Service.Lock',
                        "['/org/freedesktop/secrets/collection/login']"],
                       capture_output=True, check=True, timeout=10)
        locked = subprocess.run(['gdbus', 'call', '--session', '--dest', 'org.freedesktop.secrets',
                                 '--object-path', '/org/freedesktop/secrets/collection/login',
                                 '--method', 'org.freedesktop.DBus.Properties.Get',
                                 'org.freedesktop.Secret.Collection', 'Locked'],
                                capture_output=True, check=True, text=True, timeout=5)
        if 'true' not in locked.stdout:
            raise AssertionError('Private collection was not locked')
        for keychain in (OpenCodeGoKeychain(), OpenRouterKeychain()):
            stage = keychain.provider + ':locked'
            try:
                keychain.get()
            except CredentialError as error:
                if secret in str(error):
                    raise AssertionError('Locked keyring exposed a credential') from None
                if 'Unlock' not in str(error):
                    raise AssertionError('Locked keyring did not suggest unlocking') from None
                results[keychain.provider]['locked'] = 'passed'
            else:
                raise AssertionError('Locked keyring unexpectedly returned a credential')
        stage = 'unlock_private_collection'
        # Exercise login-style unlock of the persisted disposable keyring.
        # --unlock initializes a daemon; it is not an RPC to an existing one.
        daemon.terminate()
        daemon.wait(timeout=5)
        daemon = start_daemon()
        subprocess.run(['gdbus', 'wait', '--session', '--timeout', '10', 'org.freedesktop.secrets'],
                       check=True, capture_output=True, timeout=12)
        for _ in range(50):
            state = subprocess.run(['gdbus', 'call', '--session', '--dest', 'org.freedesktop.secrets',
                                    '--object-path', '/org/freedesktop/secrets/collection/login',
                                    '--method', 'org.freedesktop.DBus.Properties.Get',
                                    'org.freedesktop.Secret.Collection', 'Locked'],
                                   capture_output=True, text=True, check=True, timeout=5)
            if 'false' in state.stdout:
                break
            time.sleep(0.1)
        else:
            raise AssertionError('Private collection stayed locked after unlock')
        for keychain in (OpenCodeGoKeychain(), OpenRouterKeychain()):
            stage = keychain.provider + ':recovery'
            if keychain.get() != secret:
                raise AssertionError('Unlock did not restore credential access')
            results[keychain.provider]['unlock_recovery'] = 'passed'
            keychain.delete()
        return {'status': 'passed', 'isolation': 'private D-Bus and temporary XDG data/runtime directories',
                'providers': results, 'real_credentials': False, 'network_requests': 0,
                'unlock_method': 'login-style daemon restart with the disposable keyring password'}
    except Exception as error:
        return {'status': 'failed', 'stage': stage, 'error_type': type(error).__name__,
                'providers': results,
                'diagnostic': str(error) if isinstance(error, CredentialError) else 'Probe assertion/process failed'}
    finally:
        if daemon.poll() is None:
            daemon.terminate()
            try:
                daemon.wait(timeout=5)
            except subprocess.TimeoutExpired:
                daemon.kill()
                daemon.wait(timeout=5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('/tmp/bra-wb9-keychain.json'))
    parser.add_argument('--private-root', type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.private_root:
        try:
            result = inside(args.private_root)
        except Exception as error:
            # Never print libsecret stdout/stderr or exception payloads.
            result = {'status': 'failed', 'error_type': type(error).__name__}
        print(json.dumps(result))
        return
    for executable in ('dbus-run-session', 'gnome-keyring-daemon', 'gdbus', 'secret-tool'):
        if not shutil.which(executable):
            raise SystemExit(f'Required executable unavailable: {executable}')
    with tempfile.TemporaryDirectory(prefix='blackrose-keychain-') as directory:
        root = Path(directory)
        for child in ('data', 'runtime', 'control', 'config', 'cache'):
            (root / child).mkdir(mode=0o700)
        env = dict(os.environ)
        for name in ('DBUS_SESSION_BUS_ADDRESS', 'DBUS_STARTER_ADDRESS', 'DBUS_STARTER_BUS_TYPE',
                     'GNOME_KEYRING_CONTROL', 'DISPLAY', 'WAYLAND_DISPLAY'):
            env.pop(name, None)
        env.update(BRA_DISPOSABLE_KEYRING=str(root), XDG_DATA_HOME=str(root / 'data'),
                   XDG_RUNTIME_DIR=str(root / 'runtime'), XDG_CONFIG_HOME=str(root / 'config'),
                   XDG_CACHE_HOME=str(root / 'cache'))
        process = subprocess.run(['dbus-run-session', '--', sys.executable, str(Path(__file__).resolve()),
                                  '--private-root', directory],
                                 env=env, capture_output=True, text=True, timeout=170)
        try:
            result = json.loads(process.stdout)
        except ValueError:
            result = {'status': 'failed', 'error': 'Private keyring probe returned no valid report'}
        args.output.write_text(json.dumps(result, indent=2) + '\n')
        print(json.dumps(result, indent=2))
        if result['status'] != 'passed':
            raise SystemExit(1)


if __name__ == '__main__':
    main()
