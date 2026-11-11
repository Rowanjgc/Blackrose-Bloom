"""Every saved conversation stays reachable from the sidebar, page by page."""
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from blackrose.gui.bridge import Api, LoopThread
from blackrose.gui.session import Session
from blackrose.storage.db import Database
from blackrose.storage.repository import Repository
from test_gui_images import make_runtime


def insert(database, count, *, stamp=None):
    """Bulk rows: distinct ascending timestamps, or all sharing ``stamp``."""
    def rows(conn):
        conn.executemany(
            'INSERT INTO conversations(title, created_at, updated_at) VALUES (?, ?, ?)',
            [(f'Chat {i}', (s := stamp or f'2026-01-01T00:00:00.{i:06d}+00:00'), s)
             for i in range(count)])
        conn.commit()
        return [r[0] for r in conn.execute(
            'SELECT id FROM conversations ORDER BY updated_at DESC, id DESC')]
    return database.run(rows)


async def walk(repo, limit=200):
    pages, after = [], None
    while True:
        page = await repo.conversation_page(limit, after=after)
        pages.append(page)
        if not page.more:
            return pages
        after = page.cursor


class RepositoryPagingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temporary.name) / 'chats.db')
        await self.db.connect()
        self.repo = Repository(self.db)

    async def asyncTearDown(self):
        await self.db.close()
        self.temporary.cleanup()

    async def test_pages_reach_every_row_at_201_and_1000(self):
        for count, sizes in ((201, [200, 1]), (1000, [200] * 5)):
            await self.db.run(lambda conn: (conn.execute('DELETE FROM conversations'), conn.commit()))
            expected = await insert(self.db, count)
            pages = await walk(self.repo)
            self.assertEqual([len(p.items) for p in pages], sizes)
            self.assertEqual([c.id for p in pages for c in p.items], expected)
            self.assertEqual({p.total for p in pages}, {count})
            self.assertEqual([p.more for p in pages], [True] * (len(sizes) - 1) + [False])
            # The legacy listing keeps its first-page contract.
            self.assertEqual([c.id for c in await self.repo.list_conversations()], expected[:200])

    async def test_equal_timestamps_page_by_id_without_overlap(self):
        expected = await insert(self.db, 450, stamp='2026-01-01T00:00:00+00:00')
        self.assertEqual(expected, sorted(expected, reverse=True))
        pages = await walk(self.repo, limit=100)
        ids = [c.id for p in pages for c in p.items]
        self.assertEqual(ids, expected)
        self.assertEqual(len(set(ids)), 450)
        # A second walk is identical: the tiebreak is deterministic.
        self.assertEqual([c.id for p in await walk(self.repo, limit=100) for c in p.items], ids)

    async def test_window_keeps_rows_reachable_while_activity_reorders_them(self):
        expected = await insert(self.db, 600)
        first = await self.repo.conversation_page()
        older = expected[350]
        await self.repo.touch_conversation(older)          # moves to the top
        await self.repo.rename_conversation(expected[10], 'Renamed')
        window = await self.repo.conversation_page(through=first.cursor)
        self.assertEqual([c.id for c in window.items], [older] + expected[:200])
        self.assertEqual(window.items[11].title, 'Renamed')
        rest = []
        after = window.cursor
        while after:
            page = await self.repo.conversation_page(after=after)
            rest += [c.id for c in page.items]
            after = page.cursor if page.more else None
        shown = [c.id for c in window.items] + rest
        self.assertEqual(sorted(shown), sorted(expected))
        self.assertEqual(len(shown), len(set(shown)))

    async def test_including_extends_the_window_contiguously(self):
        expected = await insert(self.db, 500)
        page = await self.repo.conversation_page(including=expected[420])
        self.assertEqual([c.id for c in page.items], expected[:421])
        self.assertTrue(page.more)
        # Already visible: the first page is unchanged.
        page = await self.repo.conversation_page(including=expected[5])
        self.assertEqual([c.id for c in page.items], expected[:200])
        page = await self.repo.conversation_page(including=expected[-1])
        self.assertEqual(len(page.items), 500)
        self.assertFalse(page.more)

    async def test_adjacent_conversation_matches_sidebar_order(self):
        expected = await insert(self.db, 3, stamp='2026-01-01T00:00:00+00:00')
        self.assertEqual((await self.repo.adjacent_conversation(expected[0])).id, expected[1])
        self.assertEqual((await self.repo.adjacent_conversation(expected[2])).id, expected[1])
        self.assertIsNone(await self.repo.adjacent_conversation(999))
        await self.repo.delete_conversation(expected[0])
        await self.repo.delete_conversation(expected[2])
        self.assertIsNone(await self.repo.adjacent_conversation(expected[1]))

    async def test_listing_1000_is_fast(self):
        await insert(self.db, 1000)
        started = time.perf_counter()
        pages = await walk(self.repo)
        elapsed = time.perf_counter() - started
        first = await self.repo.conversation_page()
        started = time.perf_counter()
        window = await self.repo.conversation_page(through=pages[-1].cursor)
        window_elapsed = time.perf_counter() - started
        self.assertEqual(len(window.items), 1000)
        self.assertEqual(len(first.items), 200)
        # Generous bound; this guards against per-row transcript loading.
        self.assertLess(elapsed + window_elapsed, 2.0)


class SessionPagingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        _, self.runtime = make_runtime(Path(self.temp.name))
        await self.runtime.open()
        self.expected = await insert(self.runtime.database, 1000)
        self.events = []
        self.session = Session(self.runtime.controller, lambda kind, payload: self.events.append((kind, payload)))
        await self.runtime.controller.ensure_conversation()
        await self.session.refresh_conversations()

    async def asyncTearDown(self):
        await self.session.close()
        await self.runtime.close()
        self.temp.cleanup()

    def sidebar(self):
        return next(p for k, p in reversed(self.events) if k == 'conversations')

    async def test_load_older_reaches_all_1000_and_discloses_the_remainder(self):
        shown = self.sidebar()
        self.assertEqual((len(shown['items']), shown['total'], shown['more']), (200, 1000, True))
        self.assertEqual(shown['current'], self.expected[0])
        for loaded in (400, 600, 800, 1000):
            result = await self.session.load_older_conversations()
            shown = self.sidebar()
            self.assertEqual(len(shown['items']), loaded)
            self.assertEqual(result['first'], self.expected[loaded - 200])
            self.assertEqual(result['more'], loaded < 1000)
        self.assertEqual([c['id'] for c in shown['items']], self.expected)
        self.assertFalse(shown['more'])
        self.assertEqual((await self.session.load_older_conversations())['first'], None)

    async def test_old_chats_open_delete_and_keep_their_place_after_activity(self):
        await self.session.load_older_conversations()
        target, neighbour = self.expected[350], self.expected[351]
        self.assertTrue(await self.session.open_conversation(target))
        shown = self.sidebar()
        self.assertEqual(shown['current'], target)
        # Activity moves another loaded chat to the top; nothing is dropped and
        # the selection is unchanged.
        await self.runtime.controller._repo.touch_conversation(self.expected[300])
        await self.session.refresh_conversations()
        shown = self.sidebar()
        ids = [c['id'] for c in shown['items']]
        self.assertEqual(ids[0], self.expected[300])
        self.assertEqual(sorted(ids), sorted(self.expected[:400]))
        self.assertEqual(shown['current'], target)
        # A new chat does not push the oldest loaded one out of reach.
        self.assertTrue(await self.session.new_conversation())
        self.assertEqual(len(self.sidebar()['items']), 401)
        self.assertTrue(await self.session.open_conversation(target))
        self.assertTrue(await self.session.delete_conversation(target))
        shown = self.sidebar()
        self.assertEqual(shown['current'], neighbour)
        self.assertNotIn(target, [c['id'] for c in shown['items']])
        self.assertEqual(shown['total'], 1000)

    async def test_selection_beyond_loaded_pages_extends_the_sidebar(self):
        oldest = self.expected[-1]
        self.assertTrue(await self.session.open_conversation(oldest))
        shown = self.sidebar()
        self.assertEqual([c['id'] for c in shown['items']], self.expected)
        self.assertFalse(shown['more'])
        # Deleting the oldest selects the nearest newer chat, which is visible.
        self.assertTrue(await self.session.delete_conversation(oldest))
        shown = self.sidebar()
        self.assertEqual(shown['current'], self.expected[-2])
        self.assertEqual(shown['items'][-1]['id'], self.expected[-2])

    async def test_page_reload_keeps_loaded_history(self):
        await self.session.load_older_conversations()
        await self.session.redraw()
        self.assertEqual(len(self.sidebar()['items']), 400)


class BridgePagingTests(unittest.TestCase):
    def test_page_can_request_older_conversations(self):
        loop = LoopThread()
        loop.start()
        calls = []

        async def load_older():
            calls.append('load')
            return {'first': 7, 'more': False}

        api = Api.__new__(Api)
        api._loop = loop
        api._session = SimpleNamespace(load_older_conversations=load_older)
        try:
            self.assertEqual(api.load_older_conversations(), {'first': 7, 'more': False})
        finally:
            loop.stop()
            loop.loop.close()
        self.assertEqual(calls, ['load'])


if __name__ == '__main__':
    unittest.main()
