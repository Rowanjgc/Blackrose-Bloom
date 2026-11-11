"""Typed ``$skill-id`` tokens promote to the same reference a composer chip adds."""
from types import SimpleNamespace
import unittest

from blackrose.chat import ChatController, SKILL_TOKEN


class SkillTokenTests(unittest.IsolatedAsyncioTestCase):
    def controller(self):
        skills = SimpleNamespace(list=lambda: [
            {'id': 'explanatory-animations', 'enabled': True},
            {'id': 'disabled-skill', 'enabled': False}])
        stub = SimpleNamespace(skills=skills)

        async def resolve_references(refs):
            return list(refs)
        stub.resolve_references = resolve_references
        return stub

    async def resolve(self, text, refs=()):
        stub = self.controller()
        return await ChatController.resolve_skill_tokens(stub, text, list(refs))

    async def test_typed_enabled_token_is_promoted_once(self):
        self.assertEqual(await self.resolve('Make it using $explanatory-animations'),
                         [{'kind': 'skill', 'id': 'explanatory-animations'}])
        self.assertEqual(await self.resolve('$explanatory-animations and $explanatory-animations'),
                         [{'kind': 'skill', 'id': 'explanatory-animations'}])

    async def test_existing_chip_is_not_duplicated(self):
        chip = {'kind': 'skill', 'id': 'explanatory-animations'}
        self.assertEqual(await self.resolve('Use $explanatory-animations', [chip]), [chip])

    async def test_disabled_unknown_and_non_token_text_stay_inert(self):
        for text in ('Use $disabled-skill', 'Use $unknown-skill', 'Cost is $5', 'No token here',
                     'price$explanatory-animations'):
            with self.subTest(text=text):
                self.assertEqual(await self.resolve(text), [])

    def test_token_pattern_requires_a_boundary(self):
        self.assertEqual(SKILL_TOKEN.findall('a $one, ($two) x$three'), ['one', 'two'])

    def test_trailing_sentence_punctuation_is_not_part_of_the_id(self):
        for text in ('Use $thinking-partner.', 'Use $thinking-partner,', 'Use $thinking-partner:',
                     'Use $thinking-partner;', 'Use $thinking-partner?', 'Use $thinking-partner!',
                     'Use ($thinking-partner)'):
            with self.subTest(text=text):
                self.assertEqual(SKILL_TOKEN.findall(text), ['thinking-partner'])

    async def test_punctuated_typed_token_selects_enabled_skill(self):
        self.assertEqual(await self.resolve('Use $explanatory-animations.'),
                         [{'kind': 'skill', 'id': 'explanatory-animations'}])
        self.assertEqual(await self.resolve('Make it with $explanatory-animations!'),
                         [{'kind': 'skill', 'id': 'explanatory-animations'}])

    def test_ids_with_internal_punctuation_still_resolve(self):
        self.assertEqual(SKILL_TOKEN.findall('Use $tool.v2_beta-x now'), ['tool.v2_beta-x'])
