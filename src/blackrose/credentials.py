"""Provider credentials in the desktop's Secret Service keychain only."""
from __future__ import annotations

import shutil
import subprocess

SERVICE = 'blackrose-bloom'
ACCOUNT = 'openrouter-api-key'


class CredentialError(RuntimeError):
    pass


class APIKeychain:
    """Use libsecret's client; never fall back to a plaintext credential file."""

    provider: str
    account: str
    command: str

    def _run(self, action: str, secret: str | None = None):
        executable = shutil.which('secret-tool')
        if not executable:
            raise CredentialError('Install libsecret (secret-tool) and unlock your desktop keychain.')
        args = [executable, action]
        if action == 'store':
            args += [f'--label=Blackrose Bloom {self.provider} API key']
        args += ['service', SERVICE, 'account', self.account]
        try:
            result = subprocess.run(args, input=secret, text=True, capture_output=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired):
            raise CredentialError('Cannot access the desktop keychain. Unlock it and try again.') from None
        if result.returncode and not (action in ('lookup', 'clear') and not result.stderr.strip()):
            # Neither stdout (the secret) nor backend diagnostics belong in logs or chat.
            raise CredentialError('Cannot access the desktop keychain. Unlock it and try again.')
        return result.stdout.strip() if result.returncode == 0 else ''

    def has_key(self) -> bool:
        """Whether a key is stored. The key is not returned to the caller."""
        return bool(self._run('lookup'))

    def get(self) -> str:
        key = self._run('lookup')
        if not key:
            # A locked libsecret collection can also return a quiet no-match
            # when no prompt agent is available. Do not suggest replacing a
            # valid credential until the user has tried unlocking it.
            raise CredentialError(f'{self.provider} API key is missing or the desktop keychain is locked. '
                                  f'Unlock it and retry; if no key is stored, run: blackrose {self.command} set')
        return key

    def set(self, key: str) -> None:
        key = key.strip()
        if not key or any(c.isspace() for c in key):
            raise CredentialError('Enter a nonempty API key without whitespace.')
        self._run('store', key)

    def delete(self) -> None:
        self._run('clear')


class OpenRouterKeychain(APIKeychain):
    provider = 'OpenRouter'
    account = ACCOUNT
    command = 'openrouter-key'


class OpenCodeGoKeychain(APIKeychain):
    provider = 'OpenCode Go'
    account = 'opencode-go-api-key'
    command = 'opencode-go-key'
