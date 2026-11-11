"""Launch the Gothic Bloom desktop client, or inspect its local services."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import subprocess
import sys
from pathlib import Path

from .config import ConfigLoadError, load_config, state_dir


async def doctor(config):
    from .backend.router import RouterClient
    from .backend.providers import ChatProviders, model_provider
    from .backend.service import router_key
    from .knowledge import KnowledgeService
    client = ChatProviders(RouterClient(config.endpoint, config.request_timeout,
                                        auth=router_key(config)))
    knowledge = KnowledgeService(config.knowledge_config)
    report = {'endpoint': config.endpoint, 'default_model': config.default_model,
              'web_enabled': config.web_enabled, 'searxng_url': config.searxng_url}
    ok = True
    try:
        rows = await client.models(model_provider(config.default_model))
        report['models'] = [{'id': r['id'], 'status': r.get('status', {}).get('value')}
                            for r in rows]
        if not any(r['id'] == config.default_model for r in rows):
            report['model_error'] = 'Default model is missing from its provider'
            ok = False
        elif model_provider(config.default_model) == 'opencode-go':
            from .credentials import OpenCodeGoKeychain
            await asyncio.to_thread(OpenCodeGoKeychain().get)
            report['opencode_go_key'] = 'available in desktop keychain'
    except Exception as error:
        report['backend_error'] = str(error)
        ok = False
    finally:
        await client.aclose()
    try:
        result = await knowledge.call('list', limit=1)
        report['knowledge'] = {'config': str(knowledge.config_path), 'readable': True,
                                'has_documents': bool(result.get('documents'))}
    except Exception as error:
        report['knowledge_error'] = str(error)
        ok = False
    finally:
        await knowledge.close()
    # Readiness checks do not run model inference or send public search queries.
    from .gui.app import gui_problem
    report['gui_problem'] = gui_problem()
    report['ok'] = ok
    print(json.dumps(report, indent=2))
    return 0 if ok else 1


def main(argv=None):
    os.umask(0o077)
    parser = argparse.ArgumentParser(prog='blackrose', description='Blackrose Bloom desktop assistant')
    parser.add_argument('--config', type=Path, help='Startup TOML configuration')
    parser.add_argument('-v', '--verbose', action='store_true')
    parser.add_argument('command', nargs='?', default='chat',
                        help='Workspace directory, chat, doctor, artifacts [repair], setup-animations, install-skill, openrouter-key, or opencode-go-key')
    parser.add_argument('key_action', nargs='?', help='Key action, bundled skill ID, or directory after chat')
    args = parser.parse_args(argv)
    if args.command == 'artifacts':
        if args.key_action not in (None, 'repair'):
            parser.error('artifacts accepts only the optional repair action')
        from .artifact_cli import run
        return asyncio.run(run(args.key_action))
    if args.command in ('setup-animations', 'install-skill'):
        from .resources import bundled_skills_dir, setup_animation_runtime
        from .config import data_dir
        try:
            if args.command == 'setup-animations':
                if args.key_action:
                    parser.error('setup-animations takes no additional arguments')
                print(f'Animation compiler ready: {setup_animation_runtime()}')
            else:
                from .skills import SkillManager
                available = bundled_skills_dir()
                names = sorted(path.name for path in available.iterdir() if (path / 'SKILL.md').is_file())
                if args.key_action not in names:
                    parser.error('Choose a bundled skill: ' + ', '.join(names))
                # Bundled skills are reviewed with the application source that ships them.
                result = SkillManager(data_dir() / 'skills').install(
                    str(available / args.key_action), approved=True, origin='bundled')
                print(f'Installed skill: {result["id"]}')
            return 0
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            print(str(error), file=sys.stderr)
            return 1
    if args.command in ('openrouter-key', 'opencode-go-key'):
        if args.key_action not in (None, 'set', 'status', 'delete'):
            parser.error('key action must be set, status, or delete')
        from .credentials import CredentialError, OpenCodeGoKeychain, OpenRouterKeychain
        keychain = OpenCodeGoKeychain() if args.command == 'opencode-go-key' else OpenRouterKeychain()
        provider = keychain.provider
        try:
            action = args.key_action or 'status'
            if action == 'set':
                import getpass
                import warnings
                if not sys.stdin.isatty():
                    raise CredentialError('Run this command in a terminal to enter the key with hidden input.')
                with warnings.catch_warnings():
                    warnings.simplefilter('error', getpass.GetPassWarning)
                    try:
                        key = getpass.getpass(f'{provider} API key (hidden): ')
                    except getpass.GetPassWarning:
                        raise CredentialError('Hidden input is unavailable. Run this command in an interactive terminal.') from None
                keychain.set(key)
                print(f'{provider} API key saved in the desktop keychain.')
            elif action == 'delete':
                keychain.delete()
                print(f'{provider} API key removed from the desktop keychain.')
            else:
                keychain.get()
                print(f'{provider} API key is available in the desktop keychain.')
            return 0
        except (CredentialError, EOFError, KeyboardInterrupt) as error:
            print(str(error) or 'Key entry cancelled.', file=sys.stderr)
            return 1
    if args.key_action and args.command != 'chat':
        parser.error('key_action is only valid with openrouter-key or opencode-go-key')
    launch_workspace = None
    launch_problem = None
    if args.command != 'doctor':
        from .workspace import authorize_directory, authorize_output_folder
        raw = args.key_action if args.command == 'chat' else args.command
        if raw is None:
            # The output folder is the only place new files need no review, and
            # the default chat reads nothing else. The desktop launcher passes no
            # path, so a broken default opens without a workspace instead of failing.
            try:
                launch_workspace = authorize_output_folder()['path']
            except (OSError, ValueError, RuntimeError) as error:
                launch_problem = error
        else:
            try:
                launch_workspace = authorize_directory(Path(raw).expanduser())['path']
            except (OSError, ValueError, RuntimeError) as error:
                parser.error(f'Cannot open workspace: {error}')
    state_dir().mkdir(parents=True, exist_ok=True)
    from .diagnostics import BoundedLogHandler
    handlers = [BoundedLogHandler(state_dir() / 'blackrose.log')]
    if args.verbose:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        handlers=handlers, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    if launch_problem:
        logging.getLogger(__name__).warning('Opening without a workspace: %s', launch_problem)
    try:
        config = load_config(args.config)
    except ConfigLoadError as error:
        print(str(error), file=sys.stderr)
        return 2
    if args.command == 'doctor':
        return asyncio.run(doctor(config))
    from .gui.app import gui_problem, run_gui
    problem = gui_problem()
    if problem:
        print(f'Cannot open Blackrose Bloom: {problem}', file=sys.stderr)
        return 1
    from .instance import AlreadyRunning
    try:
        return run_gui(config, workspace=launch_workspace)
    except AlreadyRunning as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
