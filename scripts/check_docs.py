"""Check that the repository's Markdown still points at things that exist.

Run by ``check_health.py``. For every tracked Markdown file outside ``skills/``
(skill text is model-facing and its examples name hypothetical paths), it checks:

- local links and their ``#anchors`` resolve, using GitHub's heading slugs;
- backticked repository paths (``src/…``, ``tests/…``, ``scripts/…`` and the other
  top-level source directories) exist, unless Git ignores them as generated;
- ``file::name`` references name a ``def`` or ``class`` in that file;
- no reference cites a line number (``file.py:123``). Line numbers drift with
  every edit above them; cite the function, class or test instead. Documents that
  predate this rule may keep at most their count in ``check_docs_baseline.json``.
  The baseline may only shrink: a count below it fails until the baseline is
  lowered, so a fixed reference cannot quietly come back.

Fenced code blocks are skipped. Exit status is nonzero when anything is wrong.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASELINE = Path(__file__).with_name('check_docs_baseline.json')
SOURCE_DIRS = ('src', 'scripts', 'docs', 'tests', 'skills', 'experiments',
               'ui-runtime', 'animation-runtime', 'diagram-runtime')
SKIPPED_PREFIXES = ('skills/',)

_FENCE = re.compile(r'^\s*(```|~~~)')
_LINK = re.compile(r'!?\[[^\]]*\]\(\s*<?([^)\s>]+)>?(?:\s+"[^"]*")?\s*\)')
_CODE = re.compile(r'`([^`\n]+)`')
_REPO_PATH = re.compile(r'^(?:{})/[^\s*<>{{}}|]+$'.format('|'.join(map(re.escape, SOURCE_DIRS))))
_TEST_REF = re.compile(r'^([\w./-]+\.(?:py|mjs|cjs|js|jsx))((?:::[\w.…]+)+)$')
_LINE_REF = re.compile(r'\b[\w./-]+\.(?:py|pyi|js|jsx|mjs|cjs|css|html|md|json|toml|sql|sh):\d+')
_URL = re.compile(r'\b[a-z][a-z0-9+.-]*://[^\s)>]+', re.IGNORECASE)
_HEADING = re.compile(r'^(#{1,6})\s+(.*?)\s*#*\s*$')


def prose_lines(text):
    """Yield (line number, line) outside fenced code blocks."""
    fenced = None
    for number, line in enumerate(text.splitlines(), 1):
        match = _FENCE.match(line)
        if match:
            if fenced is None:
                fenced = match.group(1)
            elif match.group(1) == fenced:
                fenced = None
            continue
        if fenced is None:
            yield number, line


def slug(heading):
    """GitHub's anchor for a heading (before duplicate suffixes)."""
    text = re.sub(r'<[^>]+>', '', heading)
    text = re.sub(r'\[([^\]]*)\]\([^)]*\)', r'\1', text)
    text = text.replace('`', '').strip().lower()
    text = re.sub(r'[^\w\- ]', '', text)
    return text.replace(' ', '-')


def anchors(path):
    seen = Counter()
    found = set()
    for _number, line in prose_lines(path.read_text(encoding='utf-8')):
        match = _HEADING.match(line)
        if not match:
            continue
        base = slug(match.group(2))
        found.add(f'{base}-{seen[base]}' if seen[base] else base)
        seen[base] += 1
    return found


def defines(path, name):
    """Whether the file defines every dotted part of ``name`` (``…`` elides a prefix).

    ``ChatController.stop`` needs both a ``ChatController`` and a ``stop``
    definition, so renaming either one is reported. Attributes count when
    assigned (``name =``); JavaScript methods when declared (``name(``).
    """
    text = path.read_text(encoding='utf-8', errors='replace')
    for part in name.replace('…', '').split('.'):
        if not part:
            continue
        escaped = re.escape(part)
        pattern = re.compile(rf'^\s*(?:async\s+def|def|class|function|async\s+function)\s+{escaped}\b'
                             rf'|\b{escaped}\s*[(=:]', re.MULTILINE)
        if not pattern.search(text):
            return False
    return True


def is_ignored(root, relative):
    """Generated outputs are named by docs but ignored by Git."""
    result = subprocess.run(['git', 'check-ignore', '-q', '--no-index', relative], cwd=root,
                            capture_output=True)
    return result.returncode == 0


def check_file(root, relative, *, ignored=is_ignored):
    """Problems in one Markdown file, and its count of line-number references."""
    path = root / relative
    problems = []
    line_refs = 0
    text = path.read_text(encoding='utf-8')
    anchor_cache = {}
    for number, line in prose_lines(text):
        where = f'{relative}:{number}'
        for target in _LINK.findall(line):
            if re.match(r'^[a-z][a-z0-9+.-]*:', target, re.IGNORECASE):
                continue
            file_part, _, anchor = target.partition('#')
            destination = (path.parent / file_part).resolve() if file_part else path
            if file_part and not destination.exists():
                problems.append(f'{where}: link to missing file {target}')
                continue
            if anchor and destination.suffix == '.md':
                known = anchor_cache.setdefault(destination, anchors(destination))
                if anchor.lower() not in known:
                    problems.append(f'{where}: link to missing anchor {target}')
        code_spans = _CODE.findall(line)
        line_refs += len(_LINE_REF.findall(_URL.sub('', line)))
        for span in code_spans:
            span = span.strip()
            reference = _TEST_REF.match(span)
            if reference:
                file_name, names = reference.groups()
                name = names.split('::')[-1]
                # Docs often cite a test or module relative to its home directory.
                candidates = [root / base / file_name
                              for base in ('.', 'tests', 'scripts', 'src/blackrose', 'src',
                                           'src/blackrose/gui/static')]
                existing = next((c for c in candidates if c.is_file()), None)
                if existing is None:
                    problems.append(f'{where}: reference to missing file `{span}`')
                elif not defines(existing, name):
                    problems.append(f'{where}: `{span}` names nothing defined in {file_name}')
                continue
            if _REPO_PATH.match(span) and not _LINE_REF.search(span):
                candidate = span.rstrip('/').rstrip('.,;:')
                if not (root / candidate).exists() and not ignored(root, candidate):
                    problems.append(f'{where}: missing path `{span}`')
    return problems, line_refs


def check(root, files, baseline, *, ignored=is_ignored):
    """All problems across ``files`` (paths relative to ``root``)."""
    problems = []
    counts = {}
    for relative in files:
        found, line_refs = check_file(root, relative, ignored=ignored)
        problems += found
        if line_refs:
            counts[relative] = line_refs
    for relative in sorted(set(counts) | set(baseline)):
        count, allowed = counts.get(relative, 0), baseline.get(relative, 0)
        if count > allowed:
            problems.append(f'{relative}: {count} line-number references (allowed {allowed}); '
                            'cite the function, class or test instead of a line')
        elif count < allowed:
            problems.append(f'{relative}: {count} line-number references, below the baseline '
                            f'of {allowed}; lower it in {BASELINE.name}')
    return problems


def tracked_markdown(root):
    output = subprocess.run(['git', 'ls-files', '-z', '*.md'], cwd=root, check=True,
                            capture_output=True).stdout.decode()
    return [name for name in output.split('\0')
            if name and not name.startswith(SKIPPED_PREFIXES)]


def main():
    baseline = json.loads(BASELINE.read_text()) if BASELINE.exists() else {}
    problems = check(ROOT, tracked_markdown(ROOT), baseline)
    for problem in problems:
        print(problem)
    if problems:
        print(f'{len(problems)} documentation problem(s).', file=sys.stderr)
        raise SystemExit(1)
    print('Documentation references resolve.')


if __name__ == '__main__':
    main()
