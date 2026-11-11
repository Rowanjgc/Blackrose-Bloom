"""Reviewed package snapshots and supervised, optional Linux script execution.

No user workspace or outbound capability enters this module. The controller owns
artifact publication; subprocess output is untrusted data.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import platform
import re
import signal
import stat
import tempfile
import unicodedata

from .outbound import visible
from .artifacts import settle

MAX_INPUT = 64_000
MAX_OUTPUT = 64_000
WALL_SECONDS = 10
PERMISSIONS = ('Python standard library only. No network, credentials, user files or child '
               'processes. Read-only package; 1 MiB private scratch. 256 MiB memory, '
               '5 CPU seconds, 10 seconds elapsed, 64 KB input/output. '
               'Only an explicit script tool call runs code. Any package change requires review again.')


def package_snapshot(root: Path) -> tuple[dict[str, bytes], str, dict]:
    """Read bounded regular files through no-follow descriptors; hash those exact bytes."""
    files: dict[str, bytes] = {}
    total = entries = 0
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW

    def walk(fd: int, prefix: str, depth: int):
        nonlocal total, entries
        if depth > 32:
            raise ValueError('Execution package exceeds 32 directory levels')
        with os.scandir(fd) as scanned:
            for item in scanned:
                entries += 1
                if entries > 1000:
                    raise ValueError('Execution package exceeds 1000 entries')
                if any(unicodedata.category(c) in ('Cc', 'Cf', 'Zl', 'Zp') for c in item.name):
                    raise ValueError('Execution package names cannot contain control characters')
                name = prefix + item.name
                mode = item.stat(follow_symlinks=False).st_mode
                if stat.S_ISDIR(mode):
                    child = os.open(item.name, directory_flags, dir_fd=fd)
                    try:
                        walk(child, name + '/', depth + 1)
                    finally:
                        os.close(child)
                elif stat.S_ISREG(mode):
                    child = os.open(item.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
                    with os.fdopen(child, 'rb') as stream:
                        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                            raise ValueError('Execution package contains a special file')
                        data = stream.read(20_000_001 - total)
                    total += len(data)
                    if total > 20_000_000:
                        raise ValueError('Execution package exceeds 20 MB')
                    if (name.endswith(('.so', '.pyd', '.dll', '.dylib'))
                            or data.startswith((b'\x7fELF', b'MZ', b'\xcf\xfa\xed\xfe', b'\xfe\xed\xfa\xcf'))):
                        raise ValueError('Packaged native libraries and executables are unsupported')
                    files[name] = data
                else:
                    raise ValueError('Execution packages cannot contain links or special files')

    fd = os.open(root, directory_flags)
    try:
        walk(fd, '', 0)
    finally:
        os.close(fd)
    try:
        if len(files.get('execution.json', b'')) > 64_000:
            raise ValueError('Execution manifest exceeds 64 KB')
        manifest = json.loads(files['execution.json'])
    except (KeyError, UnicodeError, ValueError) as error:
        raise ValueError('A valid execution.json manifest is required') from error
    if (not isinstance(manifest, dict) or set(manifest) != {'version', 'entry_points'}
            or type(manifest['version']) is not int or manifest['version'] != 1
            or not isinstance(manifest['entry_points'], dict)
            or not 1 <= len(manifest['entry_points']) <= 16):
        raise ValueError('Unsupported execution manifest; use version 1 and entry_points')
    for name, path in manifest['entry_points'].items():
        if (not re.fullmatch(r'[a-z][a-z0-9_-]{0,63}', name)
                or not isinstance(path, str) or path not in files
                or str(PurePosixPath(path)) != path or '..' in PurePosixPath(path).parts
                or PurePosixPath(path).is_absolute() or not path.endswith('.py')):
            raise ValueError('Entry points must name packaged relative Python files')
    if 'SKILL.md' not in files:
        raise ValueError('Execution package requires SKILL.md')
    source_bytes = sum(len(data) for path, data in files.items() if path.endswith('.py'))
    if source_bytes > 256_000:
        raise ValueError('Execution source exceeds the 256 KB review limit')
    inventory = [[path, len(data), hashlib.sha256(data).hexdigest()]
                 for path, data in sorted(files.items())]
    digest = hashlib.sha256(json.dumps(['skill-execution-v1', inventory],
                                      separators=(',', ':')).encode()).hexdigest()
    return files, digest, manifest


def execution_review(root: Path) -> dict:
    files, digest, manifest = package_snapshot(root)
    try:
        sources = {path: '\n'.join(visible(line) for line in data.decode('utf-8').split('\n'))
                   for path, data in sorted(files.items())
                   if path.endswith('.py') or path in ('execution.json', 'SKILL.md')}
    except UnicodeError as error:
        raise ValueError('Execution source must be UTF-8') from error
    return {'digest': digest, 'manifest': manifest, 'sources': sources,
            'permissions': PERMISSIONS, 'resources': [
                {'path': path, 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
                for path, data in sorted(files.items())]}


class ScriptUnavailable(ValueError):
    """The host cannot enforce the execution contract."""


def _finite_number(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError('Script JSON numbers must be finite')
    return number


def validate_artifact(result) -> dict | None:
    if not isinstance(result, dict) or 'artifact' not in result:
        return None
    artifact = result['artifact']
    if (not isinstance(artifact, dict) or set(artifact) != {'filename', 'content'}
            or not isinstance(artifact['filename'], str)
            or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,99}\.txt', artifact['filename'])
            or not isinstance(artifact['content'], str)
            or len(artifact['content'].encode('utf-8')) > 32_000):
        raise ValueError('Script artifact must be a plain-text .txt basename and at most 32 KB')
    return artifact


def runtime_command(stage: Path, entry: str) -> list[str]:
    if platform.system() != 'Linux' or platform.machine() not in ('x86_64', 'aarch64'):
        raise ScriptUnavailable('Skill scripts require supported Linux namespace confinement')
    runtime = next((Path(p) for p in ('/usr/bin/python3.13', '/usr/bin/python3.12')
                    if Path(p).is_file()), None)
    if runtime is None or not Path('/usr/bin/bwrap').is_file():
        raise ScriptUnavailable('Install system Python 3.12/3.13 and Bubblewrap to run skill scripts')
    command = ['/usr/bin/bwrap', '--unshare-all', '--unshare-user', '--disable-userns', '--die-with-parent',
               '--new-session', '--cap-drop', 'ALL', '--clearenv',
               '--setenv', 'LANG', 'C.UTF-8', '--dir', '/usr', '--dir', '/usr/bin',
               '--ro-bind', str(runtime), str(runtime)]
    # System runtime/library trees only; no application environment, config or data.
    for path in ('/usr/lib', '/usr/lib64', '/lib', '/lib64'):
        if Path(path).exists():
            command += ['--ro-bind', path, path]
    command += ['--dir', '/dev', '--ro-bind', '/dev/null', '/dev/null',
                '--ro-bind', '/dev/urandom', '/dev/urandom',
                '--ro-bind', str(stage), '/skill',
                '--ro-bind', str(Path(__file__).with_name('skill_script_launcher.py')), '/launcher.py',
                '--size', '1048576', '--tmpfs', '/tmp', '--chdir', '/tmp',
                '--remount-ro', '/', '--', str(runtime), '-I', '-S', '-B',
                '/launcher.py', entry]
    return command


class ScriptRunner:
    """Own jobs through cancellation/shutdown, including launch and pipe cleanup."""
    def __init__(self):
        self.jobs: set[asyncio.Task] = set()
        self.closed = False

    async def close(self):
        self.closed = True
        jobs = list(self.jobs)
        for task in jobs:
            task.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)

    async def run(self, files: dict[str, bytes], entry: str, payload) -> dict:
        if self.closed:
            raise ScriptUnavailable('Skill script runtime is shutting down')
        if self.jobs:
            raise ValueError('A skill script is already running')
        data = json.dumps(payload, allow_nan=False).encode('utf-8')
        if len(data) > MAX_INPUT:
            raise ValueError('Script input exceeds 64 KB')
        task = asyncio.create_task(self._run(files, entry, data))
        self.jobs.add(task)
        try:
            try:
                return await asyncio.wait_for(task, WALL_SECONDS)
            except TimeoutError as error:
                raise ValueError('Skill script exceeded its 10 second deadline') from error
        finally:
            self.jobs.discard(task)

    async def _run(self, files: dict[str, bytes], entry: str, data: bytes) -> dict:
        with tempfile.TemporaryDirectory(prefix='blackrose-script-') as temporary:
            stage = Path(temporary)
            def write_stage():
                for name, body in files.items():
                    path = stage / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(body)
            await settle(asyncio.to_thread(write_stage))
            launch = asyncio.create_task(asyncio.create_subprocess_exec(
                *runtime_command(stage, entry), stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                env={}, close_fds=True, start_new_session=True, limit=MAX_OUTPUT))
            process = None
            readers = []
            cancelled = False
            try:
                # Launch may create a child before its await completes. Always acquire it.
                while not launch.done():
                    try:
                        await asyncio.shield(launch)
                    except asyncio.CancelledError:
                        cancelled = True
                process = launch.result()
                if cancelled:
                    raise asyncio.CancelledError
                consumed = 0

                async def read(stream):
                    nonlocal consumed
                    chunks = []
                    while chunk := await stream.read(4096):
                        consumed += len(chunk)
                        if consumed > MAX_OUTPUT:
                            raise ValueError('Script output exceeds 64 KB')
                        chunks.append(chunk)
                    return b''.join(chunks)

                async def feed():
                    try:
                        process.stdin.write(data)
                        await process.stdin.drain()
                    except (BrokenPipeError, ConnectionResetError):
                        pass  # A denied launch may exit before reading input.
                    finally:
                        process.stdin.close()

                readers = [asyncio.create_task(read(process.stdout)),
                           asyncio.create_task(read(process.stderr)), asyncio.create_task(feed()),
                           asyncio.create_task(process.wait())]
                stdout, stderr, _, code = await asyncio.gather(*readers)
                ready = stdout.startswith(b'BLACKROSE_SCRIPT_V1\n')
                if not ready:
                    raise ScriptUnavailable('Skill script confinement/runtime is unavailable')
                stdout = stdout.removeprefix(b'BLACKROSE_SCRIPT_V1\n')
                if code != 0:
                    # Diagnostics are package output; never reflect host Bubblewrap paths.
                    raise ValueError('Skill script exited unsuccessfully: ' + stderr.decode('utf-8', errors='replace')[:2000])
                try:
                    result = json.loads(stdout.decode('utf-8'), parse_float=_finite_number,
                                        parse_constant=_finite_number)
                    diagnostics = stderr.decode('utf-8')
                except (ValueError, UnicodeError) as error:
                    raise ValueError('Script must return UTF-8 JSON') from error
                return {'result': result, 'diagnostics': diagnostics, 'untrusted': True}
            finally:
                async def cleanup():
                    if process is not None and process.returncode is None:
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                    for reader in readers:
                        reader.cancel()
                    await asyncio.gather(*readers, return_exceptions=True)
                    if process is not None:
                        process.stdin.close()
                        # A reader stopped at the output bound can leave a paused
                        # pipe. Drain after killing, without accumulating output,
                        # so EOF closes every transport before we reap the job.
                        async def discard(stream):
                            while await stream.read(4096):
                                pass
                        await asyncio.gather(discard(process.stdout), discard(process.stderr), process.wait())

                cleanup_task = asyncio.create_task(cleanup())
                while not cleanup_task.done():
                    try:
                        await asyncio.shield(cleanup_task)
                    except asyncio.CancelledError:
                        cancelled = True
                cleanup_task.result()
                if cancelled:
                    raise asyncio.CancelledError
