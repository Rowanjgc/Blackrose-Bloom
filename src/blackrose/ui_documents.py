"""Bounded semantic UI documents; Python alone authorizes fields and actions.

The ordinary protocol is NDJSON with complete-block boundaries. Raw OpenUI is
never accepted from a model. Trusted browser code compiles accepted content to
the pinned OpenUI library. Replay works independently of generation flags.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import secrets
import time
from copy import deepcopy
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from .storage.records import UIBlock

from jsonschema import Draft202012Validator

MAX_SOURCE = 65536
MAX_STATE = 16384
MAX_BLOCKS = 12
LIBRARY = 'blackrose-semantic/1'
ARTIFACT = Path(__file__).parent / 'gui/static/ui-contract.json'


@dataclass(frozen=True, slots=True)
class UIEnvelope:
    conversation_id: int
    assistant_message_id: int
    block_id: str
    source_revision: int
    view_id: str

    @classmethod
    def parse(cls, payload: object) -> UIEnvelope:
        bounded(payload, 20000)
        if not isinstance(payload, dict):
            raise ValueError('Invalid UI event envelope')
        if any(type(payload.get(key)) is not int for key in (
                'conversation_id', 'assistant_message_id', 'source_revision')):
            raise ValueError('Invalid UI conversation, message or revision identity')
        if any(not isinstance(payload.get(key), str) or not payload[key] for key in ('block_id', 'view_id')):
            raise ValueError('Invalid UI block or view identity')
        return cls(*(payload[key] for key in (
            'conversation_id', 'assistant_message_id', 'block_id', 'source_revision', 'view_id')))


def _fingerprint(path):
    stat = path.stat()
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


@lru_cache(maxsize=4)
def _validated_contract(path, fingerprint):
    artifact = json.loads(path.read_text())
    if _fingerprint(path) != fingerprint:
        raise ValueError('Interactive UI artifact changed during validation')
    if not isinstance(artifact, dict):
        raise ValueError('Interactive UI artifact must be an object; rebuild ui-runtime')
    expected = artifact.pop('artifact_hash')
    packed = json.dumps(artifact, ensure_ascii=False, separators=(',', ':'))
    if hashlib.sha256(packed.encode()).hexdigest() != expected:
        raise ValueError('Interactive UI prompt/schema artifact hash mismatch')
    artifact['artifact_hash'] = expected
    encoded = json.dumps(artifact['schema'], ensure_ascii=False, separators=(',', ':'))
    if hashlib.sha256(encoded.encode()).hexdigest() != artifact['schema_hash']:
        raise ValueError('Interactive UI schema artifact is inconsistent; rebuild ui-runtime')
    return artifact


def _contract():
    path = ARTIFACT
    return _validated_contract(path, _fingerprint(path))


def contract():
    # Callers may mutate nested schema, examples and prompts.
    return deepcopy(_contract())


def instructions(profile='mixed'):
    return _contract()['prompts'][profile]


@lru_cache(maxsize=4)
def _validated_bundle(path, fingerprint, expected):
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if _fingerprint(path) != fingerprint:
        raise ValueError('Interactive UI bundle changed during validation')
    return digest == expected


def available():
    try:
        artifact = _contract()
        bundle = ARTIFACT.parent / 'ui-runtime.js'
        return _validated_bundle(bundle, _fingerprint(bundle), artifact['bundle_hash'])
    except (OSError, ValueError, KeyError, TypeError, RecursionError):
        return False


def bounded(value, limit=MAX_STATE):
    # Check nesting before JSON encoding/validation. Do not permit non-finite
    # values, prototype-shaped keys, or huge flat arrays from bridge callers.
    def walk(item, depth=0):
        if depth > 12:
            raise ValueError('UI payload nesting exceeds 12 levels')
        if isinstance(item, dict):
            if len(item) > 200 or any(not isinstance(k, str) or k in (
                    '__proto__', 'prototype', 'constructor') for k in item):
                raise ValueError('Invalid UI object keys')
            for child in item.values():
                walk(child, depth + 1)
        elif isinstance(item, list):
            if len(item) > 200:
                raise ValueError('UI array exceeds 200 items')
            for child in item:
                walk(child, depth + 1)
        elif type(item) in (float, int):
            if abs(item) > 1e15 or not math.isfinite(item):
                raise ValueError('UI numbers must be finite and bounded')
        elif item is not None and type(item) not in (str, bool):
            raise ValueError('Invalid UI value type')
    walk(value)
    if len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode()) > limit:
        raise ValueError('UI payload exceeds its byte budget')
    return value


def validate_document(source, profile='mixed'):
    if not isinstance(source, str) or len(source.encode()) > MAX_SOURCE:
        raise ValueError('UI source exceeds 64 KiB')
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f'Duplicate key: {key}')
            result[key] = value
        return result
    try:
        document = bounded(json.loads(source, object_pairs_hook=unique), MAX_SOURCE)
    except (RecursionError, json.JSONDecodeError) as error:
        raise ValueError('Incomplete or invalid UI JSON') from error
    artifact = contract()
    errors = list(Draft202012Validator(artifact['schema']).iter_errors(document))
    if errors:
        # Report the relevant variant instead of the union's enormous error.
        variant = next((s for s in artifact['schema']['oneOf']
                        if s['properties']['type']['const'] == document.get('type')), None) if isinstance(document, dict) else None
        issue = next(Draft202012Validator(variant).iter_errors(document), errors[0]) if variant else errors[0]
        path = '.'.join(str(p) for p in issue.absolute_path) or '$'
        raise ValueError(f'{path}: {issue.message[:300]}')
    kind = document['type']
    if profile != 'mixed' and kind not in ('text', profile):
        raise ValueError(f'type: expected {profile} or text')
    if kind == 'comparison':
        columns = document['columns']
        if len(set(columns)) != len(columns) or any(set(row) != set(columns) for row in document['rows']):
            raise ValueError('rows: each row must contain exactly the unique column names')
    if kind == 'form':
        fields = document['fields']
        if len({f['id'] for f in fields}) != len(fields):
            raise ValueError('fields: IDs must be unique')
        for f in fields:
            if f['id'] in ('constructor', 'prototype', '__proto__'):
                raise ValueError('fields: reserved field identifier')
            if f['kind'] == 'select' and not f.get('options'):
                raise ValueError(f"fields.{f['id']}: select requires options")
            if f.get('minimum', -1e9) > f.get('maximum', 1e9):
                raise ValueError(f"fields.{f['id']}: minimum exceeds maximum")
            if f['kind'] != 'number' and ('minimum' in f or 'maximum' in f):
                raise ValueError('Only number fields may declare numeric bounds')
            if f['kind'] != 'select' and 'options' in f:
                raise ValueError('Only select fields may declare options')
            if 'default' in f:
                _field_value(f, f['default'])
    return document


def _field_value(field, value, *, submission=False):
    kind = field['kind']
    if kind in ('text', 'select'):
        if not isinstance(value, str) or len(value) > 4000:
            raise ValueError(f"{field['id']}: expected bounded text")
        if kind == 'select' and value not in field['options']:
            raise ValueError(f"{field['id']}: unknown choice")
        if submission and field.get('required') and not value.strip():
            raise ValueError(f"{field['id']}: required")
    elif kind == 'checkbox':
        if type(value) is not bool:
            raise ValueError(f"{field['id']}: expected boolean")
        if submission and field.get('required') and not value:
            raise ValueError(f"{field['id']}: required")
    elif type(value) not in (int, float) or not field.get('minimum', -1e9) <= value <= field.get('maximum', 1e9):
        raise ValueError(f"{field['id']}: number outside allowed range")


def validate_state(document: dict[str, Any], state: object, *, submission: bool = False) -> dict[str, Any]:
    bounded(state)
    if not isinstance(state, dict):
        raise ValueError('State must be an object')
    kind = document['type']
    if kind == 'form':
        fields = {f['id']: f for f in document['fields']}
        if set(state) - set(fields):
            raise ValueError('Unknown form fields')
        for key, value in state.items():
            _field_value(fields[key], value, submission=submission)
        if submission and any(f.get('required') and f['id'] not in state for f in fields.values()):
            raise ValueError('Required form field is missing')
    elif kind == 'comparison':
        if set(state) - {'query', 'sort', 'direction', 'selected'}:
            raise ValueError('Unknown comparison state')
        if 'query' in state and (not isinstance(state['query'], str) or len(state['query']) > 4000):
            raise ValueError('Invalid filter')
        if 'sort' in state and state['sort'] not in document['columns']:
            raise ValueError('Invalid sort column')
        if 'direction' in state and state['direction'] not in ('ascending', 'descending'):
            raise ValueError('Invalid sort direction')
        if 'selected' in state and (type(state['selected']) is not int or not 0 <= state['selected'] < len(document['rows'])):
            raise ValueError('Invalid selected row')
        if submission and 'selected' not in state:
            raise ValueError('Select a row first')
    elif kind == 'steps':
        if set(state) - {'step'} or ('step' in state and (type(state['step']) is not int or not 0 <= state['step'] < len(document['items']))):
            raise ValueError('Invalid step')
    elif kind == 'calculation':
        if set(state) - {'scale', 'quantities'}:
            raise ValueError('Unknown calculation state')
        if 'scale' in state and state['scale'] is not None and (type(state['scale']) not in (int, float) or not 0 <= state['scale'] <= 1000):
            raise ValueError('Scale must be between 0 and 1000')
        if 'quantities' in state:
            quantities = state['quantities']
            if (not isinstance(quantities, list) or len(quantities) != len(document['items'])
                    or any(value is not None and (type(value) not in (int, float) or not 0 <= value <= 1e6)
                           for value in quantities)):
                raise ValueError('Quantities must match the items and be between 0 and 1000000 or blank')
    elif state:
        raise ValueError('This block has no editable state')
    return state


def fallback(document):
    lines = [document['title']]
    kind = document['type']
    if kind == 'text':
        lines.append(document['text'])
    elif kind == 'form':
        lines += [f"{f['label']}: " + (' / '.join(f['options']) if 'options' in f else f['kind']) for f in document['fields']]
    elif kind == 'comparison':
        lines += [' | '.join(document['columns'])]
        lines += [' | '.join(str(row[c]) for c in document['columns']) for row in document['rows']]
    elif kind == 'steps':
        lines += [f"{i+1}. {item['title']}: {item['text']}" for i, item in enumerate(document['items'])]
    elif kind == 'calculation':
        lines += [f"{item['label']}: {item['quantity']} × {item['unit_price']} {document['unit']}" for item in document['items']]
    else:
        lines += [f"{item['label']}: {item['value']} {document['unit']}" for item in document['data']]
    return '\n'.join(lines)


def repair_messages(source, error, profile='mixed'):
    """Smallest prompt that can recover one mechanically invalid block."""
    artifact = contract()
    match = re.search(r'"type"\s*:\s*"([a-z]+)"', source or '')
    kind = match.group(1) if match else None
    variant = next((v for v in artifact['schema']['oneOf']
                    if v['properties']['type']['const'] == kind), None) if kind else None
    lines = ['Repair one invalid interactive answer block. Return ONLY the corrected JSON object on a single line.',
             'No prose, no code fences, no additional blocks. Match this schema exactly:',
             json.dumps(variant or artifact['schema'], ensure_ascii=False, separators=(',', ':'))]
    if kind in artifact['examples']:
        lines += ['Valid example:', json.dumps(artifact['examples'][kind], ensure_ascii=False, separators=(',', ':'))]
    return [{'role': 'system', 'content': '\n'.join(lines)},
            {'role': 'user', 'content': f'Validation error: {error}\nInvalid block:\n{source}\nReturn the corrected block only.'}]


def repaired_source(text, profile='mixed'):
    """Accept the first complete line that passes the same validator."""
    cleaned = (text or '').strip()
    fenced = re.search(r'```(?:json)?\s*(.*?)```', cleaned, re.S)
    if fenced:
        cleaned = fenced.group(1).strip()
    for line in cleaned.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            validate_document(line, profile)
            return line
        except ValueError:
            # Model repair output may include prose; keep scanning candidate lines.
            continue
    return None


def apply_repair(block, source, profile='mixed'):
    """Validate and stage an in-place repair; the caller persists the new revision."""
    # Defence in depth: only an invalid block may be repaired, matching the
    # repository's own guard, so a warped caller fails here rather than later.
    if block.get('status') != 'invalid':
        raise ValueError('Only an invalid UI block can be repaired')
    document = validate_document(source, profile)
    block.update(source=source, source_hash=hashlib.sha256(source.encode()).hexdigest(),
                 document=document, fallback_text=fallback(document), status='ready', error=None,
                 source_revision=block['source_revision'] + 1, source_sequence=block['source_sequence'] + 1)
    return block


def continuation(block: UIBlock, payload: dict[str, Any]) -> str:
    document = block['document']
    if document is None:
        raise ValueError('UI document is incomplete')
    if payload.get('action_id') != 'submit' or not document.get('submit_label'):
        raise ValueError('Undeclared UI action')
    values = validate_state(document, payload.get('values'), submission=True)
    if document['type'] == 'comparison':
        labeled = list(document['rows'][values['selected']].items())
    elif document['type'] == 'form':
        labeled = [(f['label'], values[f['id']]) for f in document['fields'] if f['id'] in values]
    elif document['type'] != 'form':
        raise ValueError('This block cannot continue a conversation')
    lines = [document['submit_label'], document['title']]
    lines += [f'{label}: ' + ('Yes' if value is True else 'No' if value is False else str(value))
              for label, value in labeled]
    return '\n'.join(lines)


class UIStream:
    """One turn's ordered Markdown and semantic blocks, independent of repaints."""
    def __init__(self, assistant, callback=None):
        self.assistant = assistant
        self.callback = callback
        self.blocks: list[UIBlock] = []
        self.active: UIBlock | None = None
        self.profile = None
        self.pending = ''
        self.used = False
        self.prose = False
        self.source_bytes = 0
        self.last_publish = 0.0

    def begin(self, profile=None):
        self.active = None
        self.profile = profile
        self.pending = ''
        self.prose = False
        self.used = self.used or profile is not None

    def _new(self, kind: str, source: str = '') -> UIBlock:
        block: UIBlock = {'block_id': secrets.token_hex(16), 'message_id': self.assistant.id,
                 'conversation_id': self.assistant.conversation_id,
                 'position': len(self.blocks), 'kind': kind, 'protocol_version': 1,
                 'library_version': LIBRARY, 'source_revision': 1, 'source_sequence': 0,
                 'source': source, 'status': 'streaming', 'fallback_text': '',
                 'source_hash': '', 'document': None, 'schema_hash': contract()['schema_hash']}
        self.blocks.append(block)
        return block

    def append(self, part):
        if self.profile is None:
            if self.active is None:
                self.active = self._new('markdown')
            self.active['source'] += part
            self.active['source_sequence'] += 1
            return
        self.source_bytes += len(part.encode())
        if self.source_bytes > MAX_SOURCE:
            raise ValueError('UI source exceeds the 64 KiB turn budget')
        self.pending += part
        while '\n' in self.pending:
            line, self.pending = self.pending.split('\n', 1)
            self._line(line)
        self.publish()

    def _line(self, line):
        if not line.strip():
            if self.prose:
                self._markdown('\n')
            return
        if self.prose or not line.lstrip().startswith('{'):
            # A UI profile is active, but this segment is ordinary prose. Keep
            # the answer readable as Markdown instead of one invalid block per
            # line (each of which renders as a fallback disclosure).
            self.prose = True
            self._markdown(line + '\n')
            return
        if sum(b['kind'] == 'interactive_ui' for b in self.blocks) >= MAX_BLOCKS:
            raise ValueError('UI reply exceeds 12 blocks')
        block = self._new('interactive_ui', line)
        block['source_hash'] = hashlib.sha256(line.encode()).hexdigest()
        try:
            block['document'] = validate_document(line, self.profile)
            block['fallback_text'] = fallback(block['document'])
        except ValueError as error:
            block.update({'status': 'invalid', 'error': str(error), 'fallback_text': (
                'This interactive answer could not be completed. Retry the reply to request a new version.')})

    def _markdown(self, part):
        if self.active is None or self.active['kind'] != 'markdown':
            self.active = self._new('markdown')
        self.active['source'] += part
        self.active['source_sequence'] += 1

    def end(self, status='ready'):
        if self.profile is not None and self.pending.strip():
            if status == 'ready':
                self._line(self.pending)
            else:
                try:
                    self._line(self.pending)
                except ValueError:
                    # Abnormal termination: drop a final block that exceeds the limit.
                    pass
        self.pending = ''
        for block in self.blocks:
            if block['status'] == 'streaming':
                block['status'] = status
        self.profile = None
        self.active = None
        self.publish(force=True)

    def publish(self, *, force=False):
        if self.used and self.callback:
            now = time.monotonic()
            if not force and now - self.last_publish < 1 / 30:
                return
            self.last_publish = now
            self.callback(self.blocks)


def event_id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{16,80}', value):
        raise ValueError('Invalid UI event identity')
    return value
