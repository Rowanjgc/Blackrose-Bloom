"""Compact, revisioned illustration drafts and provider-independent compilation."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import re
import unicodedata

from jsonschema import Draft202012Validator

from .config import write_json

SKILL_ID = 'ian-xiaohei-illustrations'
MAX_DRAFTS = 4
MAX_REPAIRS = 2
MAX_DRAFT_BYTES = 16000


def text(limit=2400):
    return {'type': 'string', 'minLength': 1, 'maxLength': limit}


def array(item, maximum=12):
    return {'type': 'array', 'items': item, 'maxItems': maximum}


def record(properties, required=()):
    return {'type': 'object', 'properties': properties, 'required': list(required),
            'additionalProperties': False}


# Author only scene-specific decisions. Style and harmless layout defaults belong to the host.
DRAFT_FIELDS = {
    'core_idea': text(),
    'facts': {**array(text()), 'minItems': 1},
    'scene': {**text(6000), 'description': 'Physical scene, layout, metaphor, and its meaning.'},
    'action': {**text(), 'description': 'What Xiaohei physically does and touches.'},
    'labels': array(record({'text': text(120), 'target': text(80), 'position': text(400)},
                           ['text', 'target']), 8),
    'objects': array(record({'id': text(80), 'description': text(), 'position': text(400)},
                            ['id', 'description']), 8),
    'connections': array(record({'from': text(80), 'to': text(80), 'meaning': text(),
                                'appearance': text(800)}, ['from', 'to', 'meaning'])),
    'placement': text(400),
    'review_notes': {**text(), 'description': 'Uncertain facts, likely misreadings, and safeguards; not proof.'},
}
DRAFT_SCHEMA = record(DRAFT_FIELDS, ['core_idea', 'facts', 'scene', 'action', 'labels'])
DEFAULTS = {'placement': 'standalone', 'objects': [], 'connections': []}

CHARACTER = (
    'Xiaohei has ONE continuous, slightly irregular upright bean/capsule-shaped SOLID BLACK '
    'ink body, about 1.6 times as tall as it is wide. The upper body is also the head: '
    'no separate round head, neck, shoulders, or human torso. Exactly two equal small '
    'WHITE DOT eyes sit horizontally in the upper quarter, each about one tenth of the body '
    'width, spaced about two eye diameters apart. No mouth, nose, eyebrows, highlights, or '
    'facial outline. Two thin black stick arms and two thin black stick legs, each limb '
    'roughly half the body height, with tiny blunt ends and no detailed fingers. '
    'The outline wobbles slightly; the body stays filled black. No fur, cat ears, tail, '
    'clothing, accessories, human anatomy, or cute mascot styling. '
    'Preserve this identity across images; only the pose, facing, position, and scale vary.'
)

CONTRACT = (
    'For Xiaohei illustration draft authoring, first call illustration_draft with changes containing '
    'core_idea, facts, scene, action, and labels. Use objects with unique IDs when labels or '
    'connections refer to them; xiaohei is reserved. Labels contain text and target ID, optional '
    'position. The host supplies the fixed character/style and standalone layout defaults. '
    'Draft saving returns all blocking issues, review status, draft_id and revision; no image job '
    'starts. Repair using only changed fields plus that draft_id and revision (at most two repairs). '
    'Then call image_gen with draft_id and the exact validated revision; never resubmit a plan '
    'or prompt to image_gen. Facts and visual quality remain unverified until actually reviewed. '
    'For plan-only requests save a draft and describe it, without image_gen. A familiar topic '
    'is enough; no article is required. Use English labels. Do not draw the character name.'
)


def mentions_xiaohei(value):
    return bool(re.search(r'\bxiaohei\b', value, re.IGNORECASE))


def wants_plan_only(value):
    return bool(re.search(r"\b(?:plan(?:ning)? only|only plan|do not generate|don't generate|"
                          r"without generating|no images yet)\b", value, re.IGNORECASE))


def normalize_changes(changes):
    """Only unquote recognized field names; reject conflicting aliases."""
    if not isinstance(changes, dict):
        raise ValueError('changes must be an object')
    result = {}
    for key, value in changes.items():
        candidate = key[1:-1] if key.startswith('"') and key.endswith('"') else key
        name = candidate if candidate in DRAFT_FIELDS else key
        if name in result:
            raise ValueError(f'Conflicting aliases for {name}; supply it once.')
        result[name] = deepcopy(value)
    return result


def validate_draft(plan):
    """Return all structural issues. Never certify factual or visual correctness."""
    issues = []
    def issue(path, message):
        issues.append({'path': path, 'message': message})
    for error in Draft202012Validator(DRAFT_SCHEMA).iter_errors(plan):
        path = '.'.join(map(str, error.absolute_path))
        if error.validator == 'required':
            missing = [k for k in error.validator_value if k not in error.instance]
            for key in missing:
                entry = {'path': '.'.join(filter(None, [path, key])), 'message': 'Required field is missing.'}
                if entry not in issues:
                    issues.append(entry)
        else:
            issue(path or '$', error.message[:400])
    def blanks(value, path=''):
        if isinstance(value, str) and not value.strip():
            issue(path, 'Supply nonblank text.')
        elif isinstance(value, dict):
            for key, child in value.items():
                blanks(child, '.'.join(filter(None, [path, key])))
        elif isinstance(value, list):
            for i, child in enumerate(value):
                blanks(child, f'{path}.{i}')
    blanks(plan)
    # Validate independent references even when unrelated authoring fields are missing.
    def entries(key):
        value = plan.get(key, [])
        if not isinstance(value, list):
            return []
        validator = Draft202012Validator(DRAFT_FIELDS[key]['items'])
        return [(i, item) for i, item in enumerate(value) if validator.is_valid(item)]
    ids = {'xiaohei'}
    for i, obj in entries('objects'):
        if obj['id'] in ids:
            issue(f'objects.{i}.id', 'IDs must be unique; xiaohei is reserved.')
        ids.add(obj['id'])
    for i, label in entries('labels'):
        if label['target'] not in ids:
            issue(f'labels.{i}.target', 'Use an existing object ID or xiaohei.')
        if any(c.isalpha() and 'LATIN' not in unicodedata.name(c, '') for c in label['text']):
            issue(f'labels.{i}.text', 'Use Latin-script labels; English still requires review.')
    for i, edge in entries('connections'):
        for key in ('from', 'to'):
            if edge[key] not in ids:
                issue(f'connections.{i}.{key}', 'Use an existing object ID or xiaohei.')
    return issues


class IllustrationDrafts:
    """Turn-scoped on-disk drafts, with optimistic revision checks and bounded repairs."""
    def __init__(self, root: Path):
        self.root = root
        self.created = max((int(p.stem.split('-')[1]) for p in root.glob('draft-[1-4].json')), default=0)

    def _path(self, draft_id):
        if not isinstance(draft_id, str) or not re.fullmatch(r'draft-[1-4]', draft_id):
            raise ValueError('Unknown draft ID in this reply.')
        return self.root / (draft_id + '.json')

    def read(self, draft_id, revision):
        path = self._path(draft_id)
        if not path.is_file():
            raise ValueError('Unknown draft ID in this reply.')
        saved = json.loads(path.read_text())
        if type(revision) is not int or revision != saved['revision']:
            raise ValueError(f"Stale draft revision; current revision is {saved['revision']}.")
        return saved

    def save(self, changes, draft_id=None, revision=None):
        changes = normalize_changes(changes)
        if draft_id is None:
            if revision is not None:
                raise ValueError('revision requires draft_id.')
            if self.created >= MAX_DRAFTS:
                raise ValueError('Draft limit reached for this reply.')
            plan, next_revision = deepcopy(DEFAULTS), 1
            draft_id = f'draft-{self.created + 1}'
        else:
            saved = self.read(draft_id, revision)
            if saved['revision'] > MAX_REPAIRS:
                raise ValueError('Draft repair limit reached; report unresolved issues.')
            plan, next_revision = saved['plan'], saved['revision'] + 1
        plan.update(changes)
        if len(json.dumps(plan, ensure_ascii=False).encode()) > MAX_DRAFT_BYTES:
            raise ValueError('Draft exceeds 16000 bytes; shorten the scene or changed fields.')
        issues = validate_draft(plan)
        saved = {'draft_id': draft_id, 'revision': next_revision, 'plan': plan,
                 'valid': not issues, 'issues': issues}
        # Retain immutable revision snapshots so a generated image's recipe remains inspectable.
        write_json(self.root / f'{draft_id}-r{next_revision}.json', saved)
        write_json(self._path(draft_id), saved)
        if next_revision == 1:
            self.created += 1
        return self.summary(saved) | {'saved_path': str(self._path(draft_id).resolve())}

    @staticmethod
    def summary(saved):
        findings = ['Facts, metaphor, English language, and final pixels need review.']
        if isinstance(saved['plan'].get('objects'), list) and len(saved['plan']['objects']) > 3:
            findings.append('More than three main objects may crowd the illustration.')
        if isinstance(saved['plan'].get('labels'), list) and len(saved['plan']['labels']) > 5:
            findings.append('More than five labels may reduce readability.')
        return {key: deepcopy(saved[key]) for key in ('draft_id', 'revision', 'valid', 'issues')} | {
            'message': f"Draft revision {saved['revision']} saved · {len(saved['issues'])} blocking issues",
            'review': {'facts': 'unverified', 'metaphor': 'unverified',
                       'visual_quality': 'not_generated', 'label_language': 'unverified', 'findings': findings},
            'notice': ('Ready for image_gen with this draft_id and revision. Structural checks only.'
                       if saved['valid'] else 'No image job started. Repair all issues with changed fields only.')}

    def validated(self, draft_id, revision):
        saved = self.read(draft_id, revision)
        issues = validate_draft(saved['plan'])
        if issues:
            raise ValueError('Draft has blocking issues; repair with illustration_draft before generation.')
        return saved['plan']


def prepare_illustration(plan):
    """Only this compiler adds fixed style; authoring prose is never a QA certificate."""
    if validate_draft(plan):
        raise ValueError('Draft has blocking issues.')
    names = {'xiaohei': 'Xiaohei'} | {o['id']: o['description'] for o in plan['objects']}
    labels = [{'text': label['text'], 'target': names[label['target']],
               'position': label.get('position', 'beside its target, without overlap')}
              for label in plan['labels']]
    objects = [{'description': o['description'], 'position': o.get('position', 'as described in scene')}
               for o in plan['objects']]
    edges = [{'from': names[e['from']], 'to': names[e['to']], 'meaning': e['meaning'],
              'appearance': e.get('appearance', 'one thin restrained path')}
             for e in plan['connections']]
    text_rule = ('Render ONLY these exact English labels, each once: ' + json.dumps(labels, ensure_ascii=False)
                 if labels else 'No visible text at all.')
    return '\n\n'.join([
        'ONE 16:9 horizontal English article illustration. Pure white background. Thin slightly '
        'wobbly black hand-drawn pen lines, restrained orange movement and sparse red/blue accents. '
        'At least 35% blank white space. No title, PPT, formal flowchart, commercial vector styling, '
        'childish cuteness, paper texture, gradients, or shadows.',
        CHARACTER, 'Takeaway: ' + plan['core_idea'],
        'Author-supplied factual constraints (not independently verified): ' + '; '.join(plan['facts']),
        'Scene: ' + plan['scene'], 'Xiaohei physical action: ' + plan['action'],
        'Objects: ' + json.dumps(objects, ensure_ascii=False),
        'Directed relationships: ' + json.dumps(edges, ensure_ascii=False), text_rule,
        'Draw no internal IDs, art-direction prose, captions, or character name.',
        'Author review notes: ' + plan.get('review_notes', 'No review notes supplied.')])


def plan_markdown(plan):
    def clean(value):
        return str(value).replace('<', '&lt;').replace('>', '&gt;').replace('|', '\\|').replace('\n', ' ')
    rows = [(key.replace('_', ' ').title(), json.dumps(value, ensure_ascii=False)
             if isinstance(value, (dict, list)) else value) for key, value in plan.items()]
    return ('\n\n**Illustration draft**\n\n| Decision | Art direction |\n| --- | --- |\n' +
            '\n'.join(f'| {clean(k)} | {clean(v)} |' for k, v in rows) +
            '\n\nStructural validation passed. Facts, metaphor, and visual quality still require review.\n\n')


def delete_conversation_drafts(store_root, conversation_id):
    """Conversation deletion also removes its private draft specifications."""
    import shutil
    root = store_root / 'illustration-drafts' / str(int(conversation_id))
    if root.resolve() != store_root.resolve() / 'illustration-drafts' / str(int(conversation_id)):
        raise ValueError('Illustration draft path contains a symlink.')
    if root.exists():
        shutil.rmtree(root)
