"""Bounded line comparison for reviewing a proposed file replacement.

The review dialog shows this comparison instead of asking the user to compare
two complete texts. Counts and notes always describe the whole change; only the
displayed rows are bounded, and every omission is stated in the result.
"""
from __future__ import annotations

from collections import Counter
import difflib
import os
import re

CONTEXT_LINES = 3
MAX_ROWS = 2000            # displayed rows across all hunks
MAX_TEXT = 200_000         # displayed characters across all rows
MAX_LINE = 2000            # displayed characters of one row
LEAD = 200                 # characters shown before the first difference in a long line
# Line alignment is roughly quadratic in the worst case. Larger changed regions
# (after removing the common start and end) are shown as one replaced block.
MAX_ALIGN = 4_000_000

_LINE = re.compile(r'[^\r\n]*(?:\r\n|\r|\n)|[^\r\n]+\Z')
_ENDINGS = {'\r\n': 'CRLF', '\n': 'LF', '\r': 'CR'}


def _lines(text):
    return [(line.rstrip('\r\n'), line[len(line.rstrip('\r\n')):]) for line in _LINE.findall(text)]


def _endings(lines):
    counts = Counter(_ENDINGS[end] for _, end in lines if end)
    if not counts:
        return counts, 'none'
    if len(counts) == 1:
        return counts, next(iter(counts))
    return counts, 'mixed (' + ', '.join(f'{n} {name}' for name, n in sorted(counts.items())) + ')'


def _plural(count, word):
    return f"{count} {word}{'' if count == 1 else 's'}"


def _ops(a, b):
    """Opcodes over complete sequences, aligning only the changed middle."""
    start = 0
    while start < min(len(a), len(b)) and a[start] == b[start]:
        start += 1
    end = 0
    while end < min(len(a), len(b)) - start and a[-1 - end] == b[-1 - end]:
        end += 1
    a_end, b_end = len(a) - end, len(b) - end
    codes = [('equal', 0, start, 0, start)] if start else []
    aligned = True
    if start == a_end and start == b_end:
        pass
    elif start == a_end or start == b_end or (a_end - start) * (b_end - start) > MAX_ALIGN:
        tag = 'insert' if start == a_end else 'delete' if start == b_end else 'replace'
        aligned = tag != 'replace'
        codes.append((tag, start, a_end, start, b_end))
    else:
        matcher = difflib.SequenceMatcher(None, a[start:a_end], b[start:b_end])
        codes += [(tag, i1 + start, i2 + start, j1 + start, j2 + start)
                  for tag, i1, i2, j1, j2 in matcher.get_opcodes()]
    if end:
        codes.append(('equal', a_end, len(a), b_end, len(b)))
    return codes, aligned


def _groups(codes, n=CONTEXT_LINES):
    """Group changes with context, as difflib.SequenceMatcher.get_grouped_opcodes."""
    if not any(tag != 'equal' for tag, *_ in codes):
        return []
    codes = list(codes)
    if codes[0][0] == 'equal':
        tag, i1, i2, j1, j2 = codes[0]
        codes[0] = tag, max(i1, i2 - n), i2, max(j1, j2 - n), j2
    if codes[-1][0] == 'equal':
        tag, i1, i2, j1, j2 = codes[-1]
        codes[-1] = tag, i1, min(i2, i1 + n), j1, min(j2, j1 + n)
    groups, group = [], []
    for tag, i1, i2, j1, j2 in codes:
        if tag == 'equal' and i2 - i1 > 2 * n:
            group.append((tag, i1, min(i2, i1 + n), j1, min(j2, j1 + n)))
            groups.append(group)
            group = []
            i1, j1 = max(i1, i2 - n), max(j1, j2 - n)
        group.append((tag, i1, i2, j1, j2))
    if group and not (len(group) == 1 and group[0][0] == 'equal'):
        groups.append(group)
    return groups


def _row(kind, line, numbers, start=0):
    body, end = line
    start = min(start, len(body))
    text = body[start:start + MAX_LINE]
    row = {'kind': kind, **numbers, 'text': text}
    if start:
        row['skipped_before'] = start
    if len(body) - start - len(text):
        row['skipped_after'] = len(body) - start - len(text)
    if not end:
        row['mark'] = 'no newline at end of file'
    return row


def _rows(group, a, b):
    """Yield one hunk's rows lazily, so display bounds also bound the work."""
    for tag, i1, i2, j1, j2 in group:
        if tag == 'equal':
            for k in range(i2 - i1):
                yield _row('context', a[i1 + k], {'before': i1 + k + 1, 'after': j1 + k + 1})
            continue
        for side, lines, s1, s2, other, o1 in (('removed', a, i1, i2, b, j1), ('added', b, j1, j2, a, i1)):
            for k in range(s2 - s1):
                line = lines[s1 + k]
                # Lines replaced in order are paired, so a long line is excerpted
                # around its first difference and an ending-only change is labelled.
                pair = other[o1 + k] if tag == 'replace' and k < min(i2 - i1, j2 - j1) else None
                start = 0
                if pair and pair[0] != line[0] and max(len(line[0]), len(pair[0])) > MAX_LINE:
                    start = max(0, len(os.path.commonprefix([line[0], pair[0]])) - LEAD)
                row = _row(side, line, {'before' if side == 'removed' else 'after': s1 + k + 1}, start)
                if pair and pair[0] == line[0] and line[1]:
                    row['mark'] = _ENDINGS[line[1]] + ' line ending'
                yield row


def compare(before, after):
    """Describe how replacing ``before`` with ``after`` changes the text.

    Returns a summary and notes covering the complete change, followed by
    unified hunks bounded by MAX_ROWS/MAX_TEXT. Rows carry only text, never
    markup, and long lines are excerpted around their first difference.
    """
    a, b = _lines(before), _lines(after)
    a_counts, a_style = _endings(a)
    b_counts, b_style = _endings(b)
    # A uniform conversion (for example CRLF to LF) would mark every line; it is
    # stated once in the notes and lines are aligned by text and final newline.
    converted = len(a_counts) == 1 and len(b_counts) == 1 and a_counts.keys() != b_counts.keys()
    if converted:
        a_keys = [body + ('\n' if end else '') for body, end in a]
        b_keys = [body + ('\n' if end else '') for body, end in b]
    else:
        a_keys = [body + end for body, end in a]
        b_keys = [body + end for body, end in b]
    codes, aligned = _ops(a_keys, b_keys)
    removed = sum(i2 - i1 for tag, i1, i2, _, _ in codes if tag != 'equal')
    added = sum(j2 - j1 for tag, _, _, j1, j2 in codes if tag != 'equal')
    ending_only = sum(1 for tag, i1, i2, j1, j2 in codes if tag == 'replace'
                      for k in range(min(i2 - i1, j2 - j1)) if a[i1 + k][0] == b[j1 + k][0])

    notes = []
    if before == after:
        notes.append('The proposed text is identical to the current file.')
    if not before:
        notes.append('The current file is empty.')
    if not after:
        notes.append('The proposed replacement is empty; approving leaves an empty file.')
    if before.startswith('\ufeff') != after.startswith('\ufeff'):
        notes.append('A byte order mark (BOM) is ' + ('removed from' if before.startswith('\ufeff') else 'added to')
                     + ' the start of the file.')
    if converted:
        notes.append(f'Line endings change from {a_style} to {b_style} on every line.')
    elif a_style != b_style and 'none' not in (a_style, b_style):
        notes.append(f'Line endings change: {a_style} to {b_style}.')
    if ending_only:
        notes.append(f"{_plural(ending_only, 'line')} {'changes only its' if ending_only == 1 else 'change only their'} line ending; "
                     'the ending is labelled on those lines.')
    a_final, b_final = before.endswith(('\n', '\r')), after.endswith(('\n', '\r'))
    if a and b and a_final != b_final:
        notes.append('The final newline is removed.' if a_final else 'A final newline is added.')
    if not aligned:
        notes.append('The changed region is too large to align line by line, so it is shown as '
                     'one removed block followed by one added block.')

    groups = _groups(codes)
    hunks, rows, chars, shown_removed, shown_added, full = [], 0, 0, 0, 0, False
    for group in groups:
        first, last = group[0], group[-1]
        hunk = {'before_start': first[1] + 1, 'before_count': last[2] - first[1],
                'after_start': first[3] + 1, 'after_count': last[4] - first[3], 'rows': []}
        hunks.append(hunk)
        for row in _rows(group, a, b):
            if rows >= MAX_ROWS or chars + len(row['text']) > MAX_TEXT:
                hunk['truncated'] = full = True
                break
            hunk['rows'].append(row)
            rows, chars = rows + 1, chars + len(row['text'])
            shown_removed += row['kind'] == 'removed'
            shown_added += row['kind'] == 'added'
        if full:
            break

    if before == after:
        summary = 'No changes.'
    elif not groups:
        summary = 'No lines change; only the line endings differ.'
    else:
        summary = (f"{_plural(removed, 'line')} removed, {_plural(added, 'line')} added"
                   f"{' in ' + _plural(len(groups), 'place') if groups else ''}.")
    omitted = {'removed': removed - shown_removed, 'added': added - shown_added,
               'hunks': len(groups) - len(hunks)}
    if full:
        notes.append(f"Only the first {_plural(rows, 'row')} of the comparison are shown. "
                     f"{omitted['removed']} more removed and {omitted['added']} more added lines are not shown here; "
                     'review them in the full texts below.')
    return {'summary': summary, 'notes': notes, 'hunks': hunks, 'truncated': full,
            'omitted': omitted, 'removed': removed, 'added': added,
            'line_endings': {'before': a_style, 'after': b_style},
            'final_newline': {'before': a_final, 'after': b_final}}
