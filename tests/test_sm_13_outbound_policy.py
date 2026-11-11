"""SM-13: the conversation privacy latch, site grants and per-reply approval state.

See ``docs/state-machines/sm-13-outbound-policy.md``. These pin the state
transitions of ``OutboundPolicy`` directly (no controller, network or GUI), plus
the durable ``conversations.local_data_exposed`` write in a temporary database.
Every test is deterministic and offline.
"""
from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

import httpx

from blackrose.outbound import (MAX_LINK_HOPS, MAX_PROMPTS, PAGE_LINK, SITE, SITE_GRANT, TRUSTED_SITE,
                                OutboundBatch, OutboundDenied, OutboundPolicy, approval_text, refusal_key,
                                request_key)
from blackrose.storage.db import Database
from blackrose.storage.repository import Repository
from blackrose.web.fetcher import PageFetcher

TYPED = 'https://typed.example/'


def answering(*answers):
    """An approver that returns the given answers in order and records what it was shown."""
    asked = []

    async def approve(request):
        asked.append(request)
        return answers[len(asked) - 1]

    return asked, approve


class PrivacyLatchTests(unittest.IsolatedAsyncioTestCase):
    """`mark_private` is write-only, and every grant map is filtered at read time."""

    async def test_a_site_grant_recorded_before_local_data_stops_applying_when_it_is_used(self):
        asked, approve = answering(False)
        sites = {'docs.example': False}
        policy = OutboundPolicy(approve=approve, private=False, sites=sites)
        self.assertEqual(await policy.authorize('fetch_content', {'url': 'https://docs.example/a'}), SITE_GRANT)
        self.assertEqual(asked, [])
        policy.mark_private()
        with self.assertRaises(OutboundDenied) as raised:
            await policy.authorize('fetch_content', {'url': 'https://docs.example/b'})
        self.assertEqual(raised.exception.code, 'approval_declined')
        self.assertEqual([r.target for r in asked], ['https://docs.example/b'])
        self.assertIs(sites['docs.example'], False)
        # The refusal did not upgrade the grant.
        self.assertFalse(policy.site_allows('https://docs.example/c'))

    async def test_a_link_granted_before_local_data_is_rechecked_after_it_is_used(self):
        asked, approve = answering(False)
        policy = OutboundPolicy([TYPED], approve=approve)
        link = 'https://next.example/x'
        self.assertEqual(await policy.authorize('fetch_content', {'url': TYPED}), 'your message')
        policy.note_links(TYPED, {'links': [{'url': link}]})
        self.assertEqual(await policy.authorize('fetch_content', {'url': link}), PAGE_LINK)
        policy.mark_private()
        # The same entry is refused now, so the private check is applied after the map read.
        with self.assertRaises(OutboundDenied):
            await policy.authorize('fetch_content', {'url': link})
        self.assertEqual([r.target for r in asked], [link])
        # A second mark_private is a no-op, not a toggle.
        policy.mark_private()
        self.assertTrue(policy.private)

    async def test_only_exact_requests_survive_the_latch(self):
        asked, approve = answering(True)
        policy = OutboundPolicy([TYPED], approve=approve)
        sent = 'https://sent.example/a?x=1'
        self.assertEqual(await policy.authorize('fetch_content', {'url': sent}), 'approved by you')
        self.assertEqual(len(asked), 1)
        policy.mark_private()
        self.assertEqual(await policy.authorize('fetch_content', {'url': TYPED + '#part'}), 'your message')
        self.assertEqual(await policy.authorize('fetch_content', {'url': sent}), 'approved by you')
        self.assertEqual(len(asked), 1)

    async def test_a_trusted_site_loses_query_addresses_but_not_plain_ones(self):
        asked, approve = answering(False)
        policy = OutboundPolicy(approve=approve, private=False, trusted=('docs.example',))
        queried = 'https://docs.example/guide?q=1'
        self.assertEqual(await policy.authorize('fetch_content', {'url': queried}), TRUSTED_SITE)
        policy.mark_private()
        with self.assertRaises(OutboundDenied):
            await policy.authorize('fetch_content', {'url': queried})
        self.assertEqual([r.target for r in asked], [queried])
        self.assertEqual(await policy.authorize('fetch_content', {'url': 'https://docs.example/guide'}),
                         TRUSTED_SITE)

    async def test_a_site_answer_before_local_data_is_recorded_as_pre_local(self):
        # Only a post-local grant may cover later private reads; the record says which one it is.
        asked, approve = answering(SITE, SITE)
        sites = {}
        first = OutboundPolicy(approve=approve, private=False, sites=sites)
        self.assertEqual(await first.authorize('fetch_content', {'url': 'https://docs.example/a'}),
                         'approved by you')
        self.assertIs(sites['docs.example'], False)
        second = OutboundPolicy(approve=approve, private=True, sites=sites)
        self.assertEqual(await second.authorize('fetch_content', {'url': 'https://docs.example/b'}),
                         'approved by you')
        self.assertEqual([r.target for r in asked], ['https://docs.example/a', 'https://docs.example/b'])
        self.assertIs(sites['docs.example'], True)

    async def test_a_site_answer_where_no_site_was_offered_is_a_decline(self):
        asked, approve = answering(SITE)
        policy = OutboundPolicy(approve=approve, private=False)
        with self.assertRaises(OutboundDenied):
            await policy.authorize('fetch_content', {'url': 'https://unpkg.com/x'})
        self.assertEqual(len(asked), 1)
        self.assertTrue(policy._declined)
        self.assertEqual(policy._sites, {})


class BatchRefusalTests(unittest.IsolatedAsyncioTestCase):
    """An unchecked batch row is refused for the reply, whatever spelling reaches the policy later."""

    async def refuse_one_of_two(self):
        linked = 'https://linked.example/x'
        asked, approve = answering((False, True))
        policy = OutboundPolicy([TYPED], approve=approve)
        await policy.preauthorize([('fetch_content', {'url': TYPED}),
                                   ('fetch_content', {'url': linked}),
                                   ('fetch_content', {'url': 'https://other.example/'})])
        self.assertEqual([r.target for r in asked[0].requests], [linked, 'https://other.example/'])
        self.assertEqual(await policy.authorize('fetch_content', {'url': TYPED}), 'your message')
        policy.note_links(TYPED, {'links': [{'url': linked}]})
        return policy, linked

    async def test_an_unchecked_request_stays_refused_when_the_round_grants_it(self):
        policy, linked = await self.refuse_one_of_two()
        with self.assertRaises(OutboundDenied) as raised:
            await policy.authorize('fetch_content', {'url': linked})
        self.assertEqual(raised.exception.code, 'approval_declined')
        # The checked row still runs: the decline stops asking, not the approved request.
        self.assertEqual(await policy.authorize('fetch_content', {'url': 'https://other.example/'}),
                         'approved by you')

    async def test_the_refusal_covers_every_spelling_of_the_same_request(self):
        # Regression: the refusal used the raw text while grants use `request_key`, so a fragment,
        # a host's case or an explicit default port made the same GET look like a new request.
        policy, linked = await self.refuse_one_of_two()
        for variant in (f'{linked}#part', 'https://LINKED.example/x', 'https://linked.example:443/x'):
            with self.subTest(variant=variant), self.assertRaises(OutboundDenied) as raised:
                await policy.authorize('fetch_content', {'url': variant})
            self.assertEqual(raised.exception.code, 'approval_declined')

    async def test_a_decline_blocks_later_questions_without_asking_again(self):
        asked, approve = answering(False)
        policy = OutboundPolicy([TYPED], approve=approve)
        with self.assertRaises(OutboundDenied):
            await policy.authorize('fetch_content', {'url': 'https://one.example/'})
        with self.assertRaises(OutboundDenied) as raised:
            await policy.authorize('fetch_content', {'url': 'https://two.example/'})
        self.assertEqual(raised.exception.code, 'approval_declined')
        self.assertIn('declined earlier in this reply', str(raised.exception))
        self.assertEqual(len(asked), 1)

    async def test_a_batch_answer_of_site_grants_nothing_and_declines_every_row(self):
        # A batch never offers a site, so a site answer is not a wider approval.
        asked, approve = answering(SITE)
        policy = OutboundPolicy(approve=approve)
        calls = [('fetch_content', {'url': f'https://docs.example/{n}'}) for n in 'ab']
        await policy.preauthorize(calls)
        self.assertEqual([r.site for r in asked[0].requests], ['', ''])
        for _, arguments in calls:
            with self.assertRaises(OutboundDenied):
                await policy.authorize('fetch_content', arguments)
        self.assertEqual(policy._sites, {})
        self.assertTrue(policy._declined)


class PromptBudgetTests(unittest.IsolatedAsyncioTestCase):
    """`_prompts` is consumed when a question is shown, and a batch counts once."""

    async def test_a_full_budget_refuses_the_next_request_without_showing_it(self):
        asked, approve = answering(*[True] * MAX_PROMPTS)
        policy = OutboundPolicy([TYPED], approve=approve)
        for index in range(MAX_PROMPTS - 1):
            self.assertEqual(await policy.authorize('fetch_content', {'url': f'https://single{index}.example/'}),
                             'approved by you')
        await policy.preauthorize([('fetch_content', {'url': 'https://batch0.example/'}),
                                   ('fetch_content', {'url': 'https://batch1.example/'})])
        self.assertIsInstance(asked[-1], OutboundBatch)
        self.assertEqual(len(asked), MAX_PROMPTS)
        self.assertEqual(await policy.authorize('fetch_content', {'url': 'https://batch0.example/'}),
                         'approved by you')
        with self.assertRaises(OutboundDenied) as raised:
            await policy.authorize('fetch_content', {'url': 'https://late.example/'})
        self.assertEqual(raised.exception.code, 'approval_limit')
        self.assertEqual(len(asked), MAX_PROMPTS)
        self.assertEqual(policy._prompts, MAX_PROMPTS)


class LinkHopTests(unittest.IsolatedAsyncioTestCase):
    """Links widen a typed read by at most MAX_LINK_HOPS; an approval starts a new chain."""

    async def test_a_typed_address_reaches_two_link_hops_and_no_further(self):
        chain = [f'https://docs.example/{index}' for index in range(MAX_LINK_HOPS + 2)]
        policy = OutboundPolicy([chain[0]])
        self.assertEqual(await policy.authorize('fetch_content', {'url': chain[0]}), 'your message')
        bases = []
        for page, link in zip(chain, chain[1:], strict=False):
            policy.note_links(page, {'links': [{'url': link}]})
            try:
                bases.append(await policy.authorize('fetch_content', {'url': link}))
            except OutboundDenied as error:
                bases.append(error.code)
        self.assertEqual(bases, [PAGE_LINK] * MAX_LINK_HOPS + ['approval_unavailable'])

    async def test_an_approved_address_starts_a_new_chain(self):
        chain = [f'https://docs.example/{index}' for index in range(MAX_LINK_HOPS + 3)]
        asked, approve = answering(True)
        policy = OutboundPolicy([chain[0]], approve=approve)
        await policy.authorize('fetch_content', {'url': chain[0]})
        for page, link in zip(chain, chain[1:], strict=False):
            policy.note_links(page, {'links': [{'url': link}]})
        # The third link needs approval; approving it drops its hop count.
        self.assertEqual(await policy.authorize('fetch_content', {'url': chain[MAX_LINK_HOPS + 1]}),
                         'approved by you')
        self.assertEqual([r.target for r in asked], [chain[MAX_LINK_HOPS + 1]])
        policy.note_links(chain[MAX_LINK_HOPS + 1], {'links': [{'url': chain[MAX_LINK_HOPS + 2]}]})
        self.assertEqual(await policy.authorize('fetch_content', {'url': chain[MAX_LINK_HOPS + 2]}),
                         PAGE_LINK)


class RequestIdentityTests(unittest.TestCase):
    """A refusal identity is the request, not the text that named it."""

    def test_refusal_identity_is_the_request_not_its_text(self):
        self.assertEqual(refusal_key('fetch', 'https://Docs.Example:443/a#part'),
                         refusal_key('fetch', 'https://docs.example/a'))
        self.assertEqual(refusal_key('fetch', 'https://docs.example/a?'),
                         refusal_key('fetch', 'https://docs.example/a'))
        self.assertNotEqual(refusal_key('fetch', 'https://docs.example/a'),
                            refusal_key('fetch', 'https://docs.example/b'))
        # A query is sent verbatim, so its identity is the exact string.
        self.assertNotEqual(refusal_key('search', 'private terms'), refusal_key('search', 'private terms '))
        self.assertEqual(refusal_key('search', 'q'), ('search', 'q'))
        # An address with no request identity falls back to its text.
        self.assertEqual(refusal_key('fetch', 'not a url'), ('fetch', 'not a url'))

    def test_typed_addresses_and_site_grants_agree_on_the_default_port(self):
        self.assertEqual(request_key('https://Docs.Example:443/a#part'),
                         ('https', 'docs.example', 443, '/a', ''))


class TransmittedRequestTests(unittest.IsolatedAsyncioTestCase):
    """A grant covers the request the fetcher transmits, not only the text it was keyed on.

    ``request_key`` reads an address the way ``urlsplit`` does, which ignores outer
    whitespace (including Unicode spaces) and drops tabs and newlines. If the fetcher sent
    the model's raw text, httpx would percent-encode those characters into the request
    target, so one grant would carry an arbitrary encoded payload past the latch.
    """

    GRANTED = 'https://docs.example/guide'
    STUFFED = ('https://docs.example/guide\u00a0\u3000\u2009', '\u00a0https://docs.example/guide',
               'https://docs.example/guide ', 'https://docs.example/guide\x85\u2028', 'https://docs.example/gu\tide')

    @staticmethod
    async def transmitted(url):
        """The request target the shipped fetcher puts on the wire for ``url``."""
        sent = []

        def handler(request):
            sent.append((request.url.host, request.url.raw_path))
            return httpx.Response(200, headers={'content-type': 'text/plain'}, text='ok')

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await PageFetcher(client=client).fetch(url)
        return sent

    async def test_a_granted_address_with_ignored_characters_transmits_only_the_granted_request(self):
        asked, approve = answering()
        policy = OutboundPolicy([self.GRANTED], approve=approve)
        policy.note_results({'results': [{'url': 'https://found.example/a'}]})
        policy.mark_private()
        expected = await self.transmitted(self.GRANTED)
        self.assertEqual(expected, [('docs.example', b'/guide')])
        for url in self.STUFFED:
            with self.subTest(url=url):
                # The key matches, so no sheet; then the bytes sent must be the granted ones.
                self.assertEqual(await policy.authorize('fetch_content', {'url': url}), 'your message')
                self.assertEqual(await self.transmitted(url), expected)
        self.assertEqual(await policy.authorize('fetch_content', {'url': 'https://found.example/a\u00a0'}),
                         'search result')
        self.assertEqual(await self.transmitted('https://found.example/a\u00a0'), [('found.example', b'/a')])
        self.assertEqual(asked, [])

    async def test_an_encoded_payload_inside_the_path_is_a_new_request_after_the_latch(self):
        asked, approve = answering(False)
        policy = OutboundPolicy([self.GRANTED], approve=approve)
        policy.mark_private()
        stuffed = 'https://docs.example/guide\u00a0\u3000x'
        with self.assertRaises(OutboundDenied):
            await policy.authorize('fetch_content', {'url': stuffed})
        self.assertEqual([r.target for r in asked], [stuffed])
        # The sheet shows the spaces that would be sent rather than rendering them blank.
        text = approval_text(asked[0])
        self.assertIn('guide\\u00A0\\u3000x', text)
        self.assertNotIn('\u00a0', text)

    async def test_the_sheet_shows_the_address_that_would_be_sent(self):
        asked, approve = answering(False)
        policy = OutboundPolicy(approve=approve)
        with self.assertRaises(OutboundDenied):
            await policy.authorize('fetch_content', {'url': ' https://new.example/a\u00a0'})
        self.assertEqual([r.target for r in asked], ['https://new.example/a'])
        self.assertEqual(await self.transmitted(' https://new.example/a\u00a0'), [('new.example', b'/a')])


class RepositoryLatchTests(unittest.IsolatedAsyncioTestCase):
    """`mark_local_data_exposed` only ever writes 1, and it survives a reopen."""

    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / 'chats.db'
        self.db = Database(self.path)
        await self.db.connect()
        self.repo = Repository(self.db)

    async def asyncTearDown(self):
        await self.db.close()
        self.temporary.cleanup()

    async def test_the_mark_is_idempotent_and_durable(self):
        exposed, other = await self.repo.create_conversation(), await self.repo.create_conversation()
        self.assertFalse(await self.repo.local_data_exposed(exposed.id))
        await self.repo.mark_local_data_exposed(exposed.id)
        await self.repo.mark_local_data_exposed(exposed.id)
        self.assertTrue(await self.repo.local_data_exposed(exposed.id))
        self.assertFalse(await self.repo.local_data_exposed(other.id))
        await self.db.close()
        self.db = Database(self.path)
        await self.db.connect()
        self.repo = Repository(self.db)
        # Nothing in the schema can clear it: the only write is this UPDATE of 1.
        self.assertTrue(await self.repo.local_data_exposed(exposed.id))
        self.assertFalse(await self.repo.local_data_exposed(other.id))
        self.assertEqual(await self.db.run(lambda conn: conn.execute(
            'SELECT local_data_exposed FROM conversations WHERE id = ?', (exposed.id,)).fetchone()[0]), 1)


if __name__ == '__main__':
    unittest.main()
