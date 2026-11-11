"""Start, adopt and stop the local llama-router used by this desktop session."""
from __future__ import annotations

import asyncio
import contextlib
import fcntl
import hashlib
import ipaddress
import logging
import os
import secrets
import select
import signal
import stat
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from ..config import state_dir
from ..diagnostics import BoundedLogHandler, RouterOutput

log = logging.getLogger(__name__)
# A launched router names one origin that no web page can present (.invalid never
# resolves), so browsers cannot read its responses. The key is the access control.
BROWSER_ORIGIN = 'https://blackrose.invalid'


def _exited(handle: int) -> bool:
    return bool(select.select([handle], [], [], 0)[0])


def _endpoint_id(endpoint: str) -> str:
    return hashlib.sha256(endpoint.encode()).hexdigest()[:16]


def is_loopback(endpoint: str) -> bool:
    """Whether ``endpoint`` names this machine, so a proxy from the environment can only be a detour."""
    try:
        host = urlsplit(endpoint).hostname
    except ValueError:
        return False
    if host is None:
        return False
    if host == 'localhost' or host.endswith('.localhost'):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def local_client(endpoint: str, **options) -> httpx.AsyncClient:
    """An HTTP client for ``endpoint`` that ignores proxy and TLS settings in the environment when it is loopback.

    httpx applies HTTP_PROXY and ALL_PROXY to loopback addresses unless NO_PROXY names them,
    which would send every prompt and the router key to the proxy. A remote router keeps the
    environment, since reaching it may legitimately need one.
    """
    return httpx.AsyncClient(**options, trust_env=not is_loopback(endpoint))


def key_rejected(endpoint: str) -> RuntimeError:
    return RuntimeError(
        f'The llama-router at {endpoint} requires an API key that Blackrose does not have. '
        'Set router_api_key_file to a file containing its key, or stop that router so '
        'Blackrose can start its own.')


class RouterKey(httpx.Auth):
    """Bearer credential for llama-router, read from its key file on each request.

    An owned key is generated for each launch and removed after that router stops;
    a crash leaves it beside the surviving router so the next desktop can adopt it.
    A configured file belongs to the user. The key itself never appears in argv,
    logs, errors or the page bridge; messages name only the file.
    """

    def __init__(self, path: Path, *, owned: bool):
        self.path = path
        self.owned = owned

    def read(self) -> str | None:
        try:
            # Nonblocking, so a FIFO at the path cannot stall the event loop.
            descriptor = os.open(self.path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
        except FileNotFoundError:
            if self.owned:
                return None
            raise RuntimeError(f'router_api_key_file does not exist: {self.path}') from None
        except OSError as error:
            raise RuntimeError(f'Cannot read the llama-router key file {self.path} '
                               f'({type(error).__name__})') from None
        with os.fdopen(descriptor, 'rb') as stream:
            mode = os.fstat(descriptor).st_mode
            if not stat.S_ISREG(mode) or mode & 0o077:
                raise RuntimeError(f'The llama-router key file {self.path} must be a regular file '
                                   'readable only by you (chmod 600)')
            try:
                text = stream.read(4096).decode('ascii')
            except (OSError, UnicodeError) as error:
                raise RuntimeError(f'Cannot read the llama-router key file {self.path} '
                                   f'({type(error).__name__})') from None
        # llama-server takes each whole line except blanks and # comments.
        key = next((line for line in text.split('\n') if line and not line.startswith('#')), '')
        if not key or not key.isprintable() or key != key.strip():
            raise RuntimeError(f'The llama-router key file {self.path} must start with a key '
                               'on its own line, without surrounding spaces')
        return key

    def create(self) -> None:
        """Replace the owned key with a fresh random key in a 0600 file."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f'.{self.path.name}.{secrets.token_hex(8)}')
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
        try:
            with os.fdopen(descriptor, 'w', encoding='ascii') as stream:
                stream.write(secrets.token_urlsafe(32) + '\n')
            os.replace(temporary, self.path)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary)
            raise

    def remove(self) -> None:
        if self.owned:
            with contextlib.suppress(FileNotFoundError):
                self.path.unlink()

    def auth_flow(self, request):
        key = self.read()
        if key is not None:
            request.headers['Authorization'] = f'Bearer {key}'
        yield request


def router_key(config) -> RouterKey:
    """The key Blackrose presents to the router at ``config.endpoint``."""
    if config.router_api_key_file:
        return RouterKey(Path(config.router_api_key_file).expanduser(), owned=False)
    return RouterKey(state_dir() / f'llama-router-{_endpoint_id(config.endpoint)}.key', owned=True)


class RouterService:
    def __init__(self, config):
        self.endpoint = config.endpoint
        address = urlsplit(self.endpoint)
        self.host = address.hostname
        self.port = address.port or 80
        self.local = address.scheme == 'http' and self.host in ('127.0.0.1', 'localhost', '::1')
        self.binary = Path(config.llama_server_path).expanduser()
        self.preset = Path(config.router_preset).expanduser()
        self.start_timeout = config.router_start_timeout
        self.stop_timeout = config.router_stop_timeout
        self.log_path = state_dir() / 'llama-router.log'
        self.lock_path = state_dir() / f'llama-router-{_endpoint_id(self.endpoint)}.lock'
        self.key = router_key(config)
        self._operation = asyncio.Lock()
        self._http = local_client(self.endpoint, timeout=2.0)
        self._pid = None
        self._handle = None
        self._process = None
        self._output = None
        self._warnings: list[str] = []

    def take_warnings(self) -> list[str]:
        """Return adoption warnings not yet shown, once each."""
        warnings, self._warnings = self._warnings, []
        return warnings

    async def _ready(self):
        response = await self._http.get(self.endpoint + '/v1/models', auth=self.key)
        if response.status_code == 503:
            return False
        if response.status_code == 401:
            raise key_rejected(self.endpoint)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or not isinstance(payload.get('data'), list):
            raise RuntimeError('The configured endpoint did not return a model catalog')
        return True

    def _adopt(self):
        if not self.local:
            return
        if self._handle is not None:
            if not _exited(self._handle):
                return
            os.close(self._handle)
            self._handle = None
            self._pid = None
        expected = self.binary.resolve()
        allowed_hosts = {self.host, 'localhost', '0.0.0.0', '::'}
        if self.host == 'localhost':
            allowed_hosts.update(('127.0.0.1', '::1'))
        for entry in Path('/proc').iterdir():
            if not entry.name.isdigit():
                continue
            try:
                if entry.stat().st_uid != os.getuid() or (entry / 'exe').resolve() != expected:
                    continue
                arguments = (entry / 'cmdline').read_bytes().decode().strip('\0').split('\0')
                if '--models-preset' not in arguments:
                    continue
                port = arguments[arguments.index('--port') + 1]
                host = arguments[arguments.index('--host') + 1]
                if int(port) != self.port or host not in allowed_hosts:
                    continue
                handle = os.pidfd_open(int(entry.name))
                if _exited(handle):
                    os.close(handle)
                    continue
                self._pid, self._handle = int(entry.name), handle
                return host
            except (OSError, ValueError, IndexError, UnicodeError):
                # Another process's or exiting pid; keep scanning /proc.
                continue
        raise RuntimeError('Cannot identify the local llama-router process for shutdown; '
                           'check llama_server_path and the endpoint host/port')

    async def _check_adopted(self, host):
        """Warn when an adopted router lacks a key or listens beyond loopback."""
        if host is None:
            return
        try:
            # Deliberately sent without the key.
            response = await self._http.get(self.endpoint + '/v1/models')
            anonymous = response.is_success
        except httpx.HTTPError:
            anonymous = False
        problems = []
        if anonymous:
            problems.append('accepts requests without an API key')
        if host in ('0.0.0.0', '::'):
            problems.append(f'listens on all network interfaces (`{host}`)')
        if problems:
            warning = (f'The llama-router already running at {self.endpoint} '
                       f'{" and ".join(problems)}. Blackrose adopted it as found and stops it on '
                       'exit; the next launch starts a key-protected loopback router. If another '
                       'service manages it or other clients share it, set `router_autostart = false` '
                       'before the next Blackrose launch so Blackrose does not start or stop it. '
                       'For authentication, give the shared router `--api-key-file`, bind it to '
                       '`127.0.0.1`, and set `router_api_key_file` in Blackrose and credentials '
                       'in the other clients. A key file alone does not change router ownership.')
            log.warning('%s', warning)
            self._warnings.append(warning)

    def _command(self) -> list[str]:
        # argv is world-readable through /proc, so it names only the 0600 key file.
        # The router passes the other options, but no key, to its model children.
        return [str(self.binary), '--models-preset', str(self.preset),
                '--models-max', '1', '--no-models-autoload',
                '--host', self.host, '--port', str(self.port), '--no-webui',
                '--api-key-file', str(self.key.path), '--no-slots',
                '--cors-origins', BROWSER_ORIGIN, '--no-cors-credentials']

    def _launch(self):
        if self._handle is not None:
            if not _exited(self._handle):
                return
            os.close(self._handle)
            self._handle = None
            self._pid = None
        if not self.local:
            raise RuntimeError('Automatic router startup requires a local HTTP endpoint')
        if not self.binary.is_file() or not os.access(self.binary, os.X_OK):
            raise RuntimeError(f'llama-server is not executable: {self.binary}')
        if not self.preset.is_file():
            raise RuntimeError(f'llama-router preset does not exist: {self.preset}')
        if self._output is not None:
            if not self._output.join():
                raise RuntimeError('Previous router output is still open; stop its remaining child processes before retrying')
            self._output = None
        command = self._command()
        if self.key.owned:
            self.key.create()
        else:
            self.key.read()
        output = RouterOutput(BoundedLogHandler(self.log_path))
        try:
            self._process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                            stderr=subprocess.STDOUT, start_new_session=True)
        except BaseException:
            output.handler.close()
            self.key.remove()
            raise
        try:
            output.start(self._process.stdout)
            self._pid = self._process.pid
            self._handle = os.pidfd_open(self._pid)
        except BaseException:
            # A launched router without a pidfd could never be supervised or
            # stopped; it would outlive this desktop with this launch's key.
            self._process.kill()
            self._process.wait()
            self._process = self._pid = None
            self.key.remove()
            raise
        self._output = output

    async def ensure_running(self):
        async with self._operation:
            if self._process is not None:
                self._process.poll()
            try:
                if await self._ready():
                    await self._check_adopted(self._adopt())
                    return
            except httpx.ConnectError:
                # No compatible service is listening; start one below.
                pass
            self.lock_path.parent.mkdir(parents=True, exist_ok=True)
            with self.lock_path.open('a') as lock:
                try:
                    async with asyncio.timeout(self.start_timeout):
                        while True:
                            try:
                                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                                break
                            except BlockingIOError:
                                await asyncio.sleep(0.1)
                        # Another window may have started it while we waited.
                        try:
                            ready = await self._ready()
                        except httpx.ConnectError:
                            self._launch()
                            ready = False
                        while not ready:
                            if self._process is not None and self._process.poll() is not None:
                                raise RuntimeError(f'llama-router exited with code {self._process.returncode}; '
                                                   f'see {self.log_path}')
                            await asyncio.sleep(0.1)
                            try:
                                ready = await self._ready()
                            except httpx.ConnectError:
                                # Still starting; poll again.
                                pass
                        await self._check_adopted(self._adopt())
                except TimeoutError as error:
                    raise TimeoutError(f'Timed out starting llama-router; see {self.log_path}') from error
                finally:
                    fcntl.flock(lock, fcntl.LOCK_UN)

    def _descendant_handles(self):
        handles = []
        pending = [self._pid]
        while pending:
            parent = pending.pop()
            try:
                children = Path(f'/proc/{parent}/task/{parent}/children').read_text().split()
            except OSError:
                # The task exited between enumeration and this read.
                continue
            for child in children:
                try:
                    handle = os.pidfd_open(int(child))
                    handles.append(handle)
                    pending.append(int(child))
                except OSError:
                    # The child exited before its pidfd could be opened.
                    continue
        return handles

    async def close(self):
        try:
            async with self._operation:
                if self._handle is None:
                    return
                handles = [self._handle]
                try:
                    if not _exited(self._handle):
                        handles += self._descendant_handles()
                        with contextlib.suppress(ProcessLookupError):
                            signal.pidfd_send_signal(self._handle, signal.SIGTERM)
                        deadline = asyncio.get_running_loop().time() + self.stop_timeout
                        while any(not _exited(handle) for handle in handles):  # noqa: ASYNC110 - pidfd state changes outside this loop
                            if asyncio.get_running_loop().time() >= deadline:
                                for handle in handles:
                                    if not _exited(handle):
                                        with contextlib.suppress(ProcessLookupError):
                                            signal.pidfd_send_signal(handle, signal.SIGKILL)
                                break
                            await asyncio.sleep(0.1)
                        async with asyncio.timeout(5):
                            while any(not _exited(handle) for handle in handles):  # noqa: ASYNC110 - waiting for external process exit
                                await asyncio.sleep(0.05)
                    if self._process is not None:
                        self._process.poll()
                    # Only after the router has exited: a surviving router keeps its key.
                    self.key.remove()
                finally:
                    for handle in handles:
                        os.close(handle)
                    self._pid = self._handle = self._process = None
        finally:
            if self._output is not None:
                await asyncio.to_thread(self._output.join)
                self._output = None
            await self._http.aclose()
