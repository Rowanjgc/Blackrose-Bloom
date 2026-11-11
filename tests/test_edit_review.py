"""Bounded comparison shown before approving a file replacement."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from blackrose import edit_review
from blackrose.edit_review import compare
import test_workspace

FILE = ''.join(f'Line {i}: ordinary text for the review fixture.\n' for i in range(1, 1001))


def changes(result):
    return [row for hunk in result['hunks'] for row in hunk['rows'] if row['kind'] != 'context']


class CompareTests(unittest.TestCase):
    def test_one_line_change_in_middle_of_long_file(self):
        after = FILE.replace('Line 500: ordinary', 'Line 500: revised')
        result = compare(FILE, after)
        self.assertEqual(result['summary'], '1 line removed, 1 line added in 1 place.')
        self.assertEqual(result['notes'], [])
        [hunk] = result['hunks']
        self.assertEqual((hunk['before_start'], hunk['before_count'], hunk['after_start']), (497, 7, 497))
        self.assertEqual([r['kind'] for r in hunk['rows']], ['context'] * 3 + ['removed', 'added'] + ['context'] * 3)
        self.assertEqual(changes(result), [
            {'kind': 'removed', 'before': 500, 'text': 'Line 500: ordinary text for the review fixture.'},
            {'kind': 'added', 'after': 500, 'text': 'Line 500: revised text for the review fixture.'}])

    def test_one_line_change_at_end_and_insertion_shift_line_numbers(self):
        after = FILE.replace('Line 10: ', 'Inserted\nLine 10: ').replace('Line 1000: ordinary', 'Line 1000: final')
        result = compare(FILE, after)
        self.assertEqual(result['summary'], '1 line removed, 2 lines added in 2 places.')
        first, last = result['hunks']
        self.assertEqual([(r['kind'], r['text']) for r in first['rows'] if r['kind'] != 'context'],
                         [('added', 'Inserted')])
        self.assertEqual([(r['kind'], r.get('before'), r.get('after')) for r in last['rows']][-2:],
                         [('removed', 1000, None), ('added', None, 1001)])
        self.assertEqual(last['rows'][0], {'kind': 'context', 'before': 997, 'after': 998,
                                           'text': 'Line 997: ordinary text for the review fixture.'})

    def test_deletion_only(self):
        lines = FILE.splitlines(keepends=True)
        result = compare(FILE, ''.join(lines[:399] + lines[409:]))
        self.assertEqual(result['summary'], '10 lines removed, 0 lines added in 1 place.')
        self.assertEqual([r['before'] for r in changes(result)], list(range(400, 410)))
        self.assertEqual({r['kind'] for r in changes(result)}, {'removed'})
        self.assertEqual(compare(FILE, '')['notes'], ['The proposed replacement is empty; approving leaves an empty file.'])

    def test_uniform_line_ending_conversion_is_stated_without_hiding_content_changes(self):
        crlf = FILE.replace('\n', '\r\n')
        result = compare(crlf, FILE)
        self.assertEqual(result['summary'], 'No lines change; only the line endings differ.')
        self.assertEqual(result['notes'], ['Line endings change from CRLF to LF on every line.'])
        self.assertEqual((result['hunks'], result['line_endings']), ([], {'before': 'CRLF', 'after': 'LF'}))
        result = compare(crlf, FILE.replace('Line 700: ordinary', 'Line 700: edited'))
        self.assertEqual(result['summary'], '1 line removed, 1 line added in 1 place.')
        self.assertEqual([r['text'][:10] for r in changes(result)], ['Line 700: '] * 2)
        self.assertTrue(all('\r' not in r['text'] and '\n' not in r['text']
                            for h in result['hunks'] for r in h['rows']))

    def test_single_line_ending_change_is_labelled(self):
        result = compare(FILE, FILE.replace('Line 321: ordinary text for the review fixture.\n',
                                            'Line 321: ordinary text for the review fixture.\r\n'))
        self.assertIn('Line endings change: LF to mixed (1 CRLF, 999 LF).', result['notes'])
        self.assertIn('1 line changes only its line ending; the ending is labelled on those lines.', result['notes'])
        self.assertEqual([(r['kind'], r['mark']) for r in changes(result)],
                         [('removed', 'LF line ending'), ('added', 'CRLF line ending')])
        self.assertEqual(compare('a\rb\r', 'a\nb\n')['notes'], ['Line endings change from CR to LF on every line.'])

    def test_final_newline_change(self):
        result = compare(FILE, FILE[:-1])
        self.assertIn('The final newline is removed.', result['notes'])
        self.assertEqual([r.get('mark') for r in changes(result)], ['LF line ending', 'no newline at end of file'])
        self.assertIn('A final newline is added.', compare('a\r\nb', 'a\r\nb\r\n')['notes'])
        # Converting endings while dropping the final newline still shows that line.
        self.assertEqual(len(changes(compare('a\r\nb\r\n', 'a\nb'))), 2)

    def test_huge_line_is_excerpted_around_its_difference(self):
        before = 'x' * 200_000 + 'OLD' + 'y' * 50_000
        after = 'x' * 200_000 + 'NEW' + 'y' * 50_000
        removed, added = changes(compare(f'start\n{before}\nend\n', f'start\n{after}\nend\n'))
        for row, marker in ((removed, 'OLD'), (added, 'NEW')):
            self.assertIn(marker, row['text'])
            self.assertLessEqual(len(row['text']), edit_review.MAX_LINE)
            self.assertEqual(row['skipped_before'] + len(row['text']) + row['skipped_after'], 250_003)
        unpaired = changes(compare('', 'z' * 10_000))[0]
        self.assertEqual((len(unpaired['text']), unpaired['skipped_after']), (edit_review.MAX_LINE, 8000))

    def test_non_ascii_text_and_byte_order_mark(self):
        before = '标题\nCafé — naïve\n🌹 rose\n'
        result = compare(before, '\ufeff' + before.replace('🌹 rose', '🥀 wilted rose'))
        self.assertIn('A byte order mark (BOM) is added to the start of the file.', result['notes'])
        self.assertEqual([r['text'] for r in changes(result)], ['标题', '\ufeff标题', '🌹 rose', '🥀 wilted rose'])

    def test_display_bound_states_what_is_omitted(self):
        after = ''.join(line.replace('ordinary', 'changed') if i % 2 else line
                        for i, line in enumerate(FILE.splitlines(keepends=True)))
        with patch.object(edit_review, 'MAX_ROWS', 300):
            result = compare(FILE, after)
        rows = [r for h in result['hunks'] for r in h['rows']]
        self.assertEqual(len(rows), 300)
        self.assertTrue(result['truncated'] and result['hunks'][-1]['truncated'])
        self.assertEqual(result['summary'], '500 lines removed, 500 lines added in 1 place.')
        shown = sum(r['kind'] == 'removed' for r in rows), sum(r['kind'] == 'added' for r in rows)
        self.assertEqual(shown[0] + result['omitted']['removed'], 500)
        self.assertEqual(shown[1] + result['omitted']['added'], 500)
        self.assertGreater(result['omitted']['added'], 0)
        self.assertIn(f"{result['omitted']['removed']} more removed and {result['omitted']['added']} more added",
                      result['notes'][-1])
        with patch.object(edit_review, 'MAX_TEXT', 5000):
            result = compare(('b' * 1999 + '\n') * 3, ('d' * 1999 + '\n') * 3)
        self.assertTrue(result['truncated'])
        self.assertLessEqual(sum(len(r['text']) for h in result['hunks'] for r in h['rows']), 5000)

    def test_unaligned_region_falls_back_to_one_replaced_block(self):
        after = FILE.replace('Line 2: ordinary', 'Line 2: edited').replace('Line 999: ordinary', 'Line 999: edited')
        with patch.object(edit_review, 'MAX_ALIGN', 100):
            result = compare(FILE, after)
        self.assertEqual(result['summary'], '998 lines removed, 998 lines added in 1 place.')
        self.assertIn('too large to align line by line', result['notes'][-1])

    def test_identical_and_empty_files(self):
        self.assertEqual(compare(FILE, FILE)['summary'], 'No changes.')
        self.assertEqual(compare(FILE, FILE)['hunks'], [])
        result = compare('', 'new\n')
        self.assertEqual(result['notes'], ['The current file is empty.'])
        self.assertEqual(changes(result), [{'kind': 'added', 'after': 1, 'text': 'new'}])


class ReviewTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = test_workspace.WorkflowTests.asyncSetUp
    asyncTearDown = test_workspace.WorkflowTests.asyncTearDown
    workspace = test_workspace.WorkflowTests.workspace
    backend = test_workspace.WorkflowTests.backend

    async def test_review_includes_comparison_and_full_texts(self):
        work = await self.workspace()
        target = work / 'notes.md'
        target.write_bytes(FILE.replace('\n', '\r\n').encode())
        content = FILE.replace('\n', '\r\n').replace('Line 900: ordinary', 'Line 900: revised')
        c = self.backend([[('file_edit', {'path': 'notes.md', 'content': content})], 'Please approve.'])
        await c.send('Revise line 900')
        token = next(iter(c.pending_edits))
        review = await c.review_edit(token)
        self.assertEqual((review['before'], review['after']), (target.read_text(newline=''), content))
        self.assertEqual(review['comparison']['summary'], '1 line removed, 1 line added in 1 place.')
        self.assertEqual([r['before' if r['kind'] == 'removed' else 'after'] for r in changes(review['comparison'])],
                         [900, 900])
        self.assertEqual(review['comparison']['line_endings'], {'before': 'CRLF', 'after': 'CRLF'})


if __name__ == '__main__':
    unittest.main()
