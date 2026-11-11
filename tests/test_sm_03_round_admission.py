"""SM-03: round admission modes, capability locks and finish-reason latching.

Each test pins one claim from
``docs/state-machines/sm-03-round-admission.md``.  Four machines are covered:

* **Admission** (``TurnRunner._admit_round``): exactly one of ``NORMAL``,
  ``UI_SEGMENT``, ``UI_FALLBACK`` or ``EXHAUSTED`` per streamed round.
* **Round meter** (``TurnRunner._answer`` + ``ToolExecutor.activation_rounds``):
  one free activation-only round, then paid rounds bounded by
  ``max_tool_rounds``.
* **Capability locks** (``Prompt``): ``ui_used`` is a one-way latch; the other
  capabilities are monotone too, and offerings are derived from the flags.
* **Stream latch** (``TurnRunner._stream_round``): one ``finish_reason`` per
  round and no accepted output after it.

The end-to-end tests drive the real ``TurnRunner`` through the real controller,
repository and tool executor with a scripted provider stream; they assert on the
requests the model actually received, never on a mock having been called.
"""
import json
import unittest
from copy import deepcopy
from dataclasses import dataclass
from unittest.mock import AsyncMock, Mock, patch

from blackrose.errors import InputRejected
from blackrose.illustrations import SKILL_ID as XIAOHEI_SKILL_ID
from blackrose.motion_canvas import MODEL_INSTRUCTIONS
from blackrose.prompts import ANIMATION_SKILL_ID, Prompt, PromptBuilder
from blackrose.storage.records import MessageState
from blackrose.turns import RoundOutput, TurnRunner, require_normal_finish
import test_gui_images as fixtures


UI_CONTRACT = 'UI contract: {profile}'
BUDGET_NOTICE = 'Tool budget spent'


@dataclass(frozen=True)
class Raw:
    """A scripted provider stream: raw choice dicts, yielded in order."""
    choices: tuple


def call(name, arguments=None):
    return (name, arguments or {})


def turn(*pairs):
    """One scripted round with several tool calls."""
    return list(pairs)


class Backend(fixtures.Backend):
    """A provider double for one reply; each step is one request."""

    def __init__(self, steps=('Done.',), context=128000):
        self.steps = iter(steps)
        self.context = context
        self.requests = []
        self.offered = []
        self.load = AsyncMock()

    async def models(self, provider=None):
        return [{'id': name, 'architecture': {'input_modalities': ['text', 'image']},
                 'meta': {'n_ctx': self.context}}
                for name in ('test-vision', 'opencode-go/test-vision')]

    async def stream(self, model, messages, tools):
        self.requests.append(deepcopy(messages))
        self.offered.append(deepcopy(tools))
        step = next(self.steps)
        if isinstance(step, str):
            yield {'delta': {'content': step}, 'finish_reason': 'stop'}
            return
        if isinstance(step, Raw):
            for choice in step.choices:
                yield choice
            return
        if isinstance(step, tuple):
            step = [step]
        yield {'delta': {'tool_calls': [
            {'index': index, 'id': f'call-{len(self.requests)}-{index}',
             'function': {'name': name, 'arguments': json.dumps(arguments)}}
            for index, (name, arguments) in enumerate(step)]},
            'finish_reason': 'tool_calls'}


def activation_capabilities(offered):
    """The capabilities advertised by this round's ``activate_tools``, if any."""
    for definition in offered:
        if definition['function']['name'] == 'activate_tools':
            return definition['function']['parameters']['properties']['capability']['enum']
    return None


def tool_names(offered):
    return {definition['function']['name'] for definition in offered}


def tool_result(request, name):
    """The last tool message in a request for ``name``."""
    return next((message for message in reversed(request)
                 if message.get('role') == 'tool' and message.get('name') == name), None)


class ImageProvider:
    """A configured image provider that must never be asked for a job."""

    def __init__(self):
        self.generate = Mock()

    async def close(self):
        pass


class RoundRecorder(TurnRunner):
    """A ``TurnRunner`` that records what each round was admitted with."""

    rounds = []
    tools = []
    streams = []
    final = None

    @classmethod
    def reset(cls):
        cls.rounds, cls.tools, cls.streams, cls.final = [], [], [], None

    def _admit_round(self, prompt, reply, exhausted):
        record = {
            'ui_profile': prompt.ui_profile,
            'ui_used': prompt.ui_used,
            'animations': prompt.animations_enabled,
            'library_write': prompt.library_write_enabled,
            'exhausted': exhausted,
            'activation_rounds': reply.executor.activation_rounds,
            'system': prompt.messages[0]['content'],
            'message_count': len(prompt.messages),
        }
        offered, metrics = super()._admit_round(prompt, reply, exhausted)
        record |= {
            'offered': [definition['function']['name'] for definition in offered],
            'capabilities': activation_capabilities(offered),
            'ui_profile_after': prompt.ui_profile,
            'ui_used_after': prompt.ui_used,
            'system_after': prompt.messages[0]['content'],
            'last': deepcopy(prompt.messages[-1]) if prompt.messages else None,
            'messages': deepcopy(prompt.messages),
        }
        self.rounds.append(record)
        return offered, metrics

    async def _run_tools(self, prompt, reply, output, request_number):
        before = reply.executor.activation_rounds
        free = await super()._run_tools(prompt, reply, output, request_number)
        self.tools.append({
            'free': free,
            'activation_rounds_before': before,
            'activation_rounds_after': reply.executor.activation_rounds,
            'request_number': request_number,
        })
        return free

    async def _stream_round(self, prompt, offered, metrics, reply):
        self.streams.append({'ui_profile': prompt.ui_profile, 'ui_used': prompt.ui_used})
        return await super()._stream_round(prompt, offered, metrics, reply)

    async def _require_final_answer(self, prompt, reply, output, exhausted, round_number):
        type(self).final = {'exhausted': exhausted, 'round_number': round_number,
                            'activation_rounds': reply.executor.activation_rounds}
        await super()._require_final_answer(prompt, reply, output, exhausted, round_number)


class TerminalPairTests(unittest.TestCase):
    """``require_normal_finish`` is protocol-independent; these are its pairs."""

    @staticmethod
    def output(reason, calls=None, content=''):
        return RoundOutput(content=content, finish_reason=reason,
                           calls=calls if calls is not None else {})

    def test_every_normal_terminal_pair_is_accepted(self):
        call_round = {0: {'id': 'call-a', 'function': {'name': 'file_read', 'arguments': '{}'}}}
        for reason, calls in (('stop', {}), ('end_turn', {}), ('tool_calls', call_round)):
            with self.subTest(reason=reason, calls=bool(calls)):
                require_normal_finish(self.output(reason, calls), ui_segment=False)

    def test_finish_reason_and_calls_must_agree(self):
        one_call = {0: {'id': 'call-a', 'function': {'name': 'file_read', 'arguments': '{}'}}}
        for reason, calls in (('tool_calls', {}), ('stop', one_call), ('end_turn', one_call)):
            with self.subTest(reason=reason, calls=bool(calls)):
                with self.assertRaisesRegex(RuntimeError, 'inconsistent with its finish reason'):
                    require_normal_finish(self.output(reason, calls), ui_segment=False)

    def test_abnormal_and_missing_reasons_reject_the_round(self):
        reasons = {'length': 'generation limit', 'content_filter': 'filtered',
                   'refusal': 'refused', 'context_length': 'context limit',
                   'pause_turn': 'paused', None: 'did not finish normally',
                   'future_reason': 'did not finish normally'}
        for reason, message in reasons.items():
            with self.subTest(reason=reason):
                with self.assertRaisesRegex(RuntimeError, message):
                    require_normal_finish(self.output(reason), ui_segment=False)

    def test_a_ui_segment_must_be_tool_free_and_normal(self):
        require_normal_finish(self.output('stop'), ui_segment=True)
        require_normal_finish(self.output('end_turn'), ui_segment=True)
        one_call = {0: {'id': 'call-a', 'function': {'name': 'file_read', 'arguments': '{}'}}}
        cases = (
            ('tool_calls', one_call, 'Interactive UI segment did not finish normally'),
            ('stop', one_call, 'inconsistent with its finish reason'),
            ('tool_calls', {}, 'inconsistent with its finish reason'),
            ('length', {}, 'generation limit'),
        )
        for reason, calls, message in cases:
            with self.subTest(reason=reason, calls=bool(calls)):
                with self.assertRaisesRegex(RuntimeError, message):
                    require_normal_finish(self.output(reason, calls), ui_segment=True)


class PromptLatchTests(unittest.TestCase):
    """The UI latch lives entirely in ``Prompt`` and survives a fallback."""

    @staticmethod
    def prompt(policy='Policy.'):
        prompt = Prompt([], policy)
        prompt.messages.append({'role': 'system', 'content': prompt.system_text})
        return prompt

    @patch('blackrose.ui_documents.instructions',
           lambda profile='mixed': UI_CONTRACT.format(profile=profile))
    def test_the_ui_contract_can_be_armed_once_and_never_again(self):
        prompt = self.prompt()
        baseline = prompt.messages[0]['content']
        self.assertTrue(prompt.enable_ui('comparison'))
        self.assertEqual(prompt.ui_profile, 'comparison')
        self.assertTrue(prompt.ui_used)
        armed = prompt.messages[0]['content']
        self.assertIn('UI contract: comparison', armed)
        with self.assertRaises(ValueError):
            prompt.enable_ui('mixed')
        self.assertEqual(armed.count('UI contract:'), 1)
        # Unloading restores the exact earlier bytes; the reply stays spent.
        prompt.disable_ui()
        self.assertEqual(prompt.messages[0]['content'], baseline)
        self.assertIsNone(prompt.ui_profile)
        self.assertTrue(prompt.ui_used)
        with self.assertRaises(ValueError):
            prompt.enable_ui('steps')
        prompt.disable_ui()
        self.assertEqual(prompt.messages[0]['content'], baseline)

    @patch('blackrose.ui_documents.instructions',
           lambda profile='mixed': UI_CONTRACT.format(profile=profile))
    def test_the_fallback_state_is_profile_none_and_used_true(self):
        prompt = self.prompt()
        prompt.enable_ui('mixed')
        prompt.disable_ui()
        # Exactly the state ``_admit_round`` leaves after a refused UI contract.
        self.assertEqual((prompt.ui_profile, prompt.ui_used), (None, True))

    def test_animations_and_library_writes_are_loaded_once(self):
        prompt = self.prompt()
        prompt.animations_available = True
        prompt.motion_plan_first = True
        self.assertTrue(prompt.enable_animations())
        self.assertFalse(prompt.enable_animations())
        self.assertEqual(prompt.messages[0]['content'].count(MODEL_INSTRUCTIONS), 1)
        self.assertTrue(prompt.enable_library_write())
        self.assertFalse(prompt.enable_library_write())

    def test_animations_cannot_be_loaded_when_unavailable(self):
        prompt = self.prompt()
        prompt.animations_available = False
        self.assertFalse(prompt.enable_animations())
        self.assertFalse(prompt.animations_enabled)
        self.assertNotIn(MODEL_INSTRUCTIONS, prompt.messages[0]['content'])


class RoundAdmissionTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.ImageTests.asyncSetUp
    asyncTearDown = fixtures.ImageTests.asyncTearDown

    def setUp(self):
        RoundRecorder.reset()
        self.enterContext(patch('blackrose.chat.TurnRunner', RoundRecorder))
        self.enterContext(patch('blackrose.ui_documents.instructions',
                                lambda profile='mixed': UI_CONTRACT.format(profile=profile)))

    def controller(self, steps, *, context=128000, cloud=False):
        c = self.runtime.controller
        c.backend = Backend(steps, context)
        if cloud:
            c.model_name = 'opencode-go/test-vision'
        c.schedule_title = Mock()
        return c

    def install_skill(self, name, body='Read the instructions.'):
        package = self.root / f'source-{name}'
        package.mkdir()
        (package / 'SKILL.md').write_text(
            f'---\nname: {name}\ndescription: Fixture skill.\n---\n' + body)
        self.runtime.controller.skills.install(str(package), approved=True)

    def reject_ui_admission(self):
        """Inject the context refusal the UI fallback exists for."""
        original = PromptBuilder.admit

        def admit(builder, prompt, tools):
            if prompt.ui_profile is not None:
                raise InputRejected('Injected: the interactive UI contract does not fit')
            return original(builder, prompt, tools)

        return patch.object(PromptBuilder, 'admit', admit)

    # --- NORMAL and EXHAUSTED ------------------------------------------------

    async def test_a_normal_round_offers_tools_and_no_budget_notice(self):
        c = self.controller(['Done.'])
        await c.send('Hello')
        self.assertEqual(len(RoundRecorder.rounds), 1)
        round = RoundRecorder.rounds[0]
        self.assertFalse(round['exhausted'])
        self.assertIsNone(round['ui_profile'])
        self.assertIn('file_read', round['offered'])
        self.assertNotIn(BUDGET_NOTICE, json.dumps(round['messages']))
        self.assertEqual(RoundRecorder.final['round_number'], 0)
        self.assertFalse(RoundRecorder.final['exhausted'])

    async def test_the_exhausted_round_offers_nothing_and_appends_the_notice_once(self):
        c = self.controller([call('file_read', {'path': 'missing.txt'}), 'Answer.'], context=128000)
        c.config.max_tool_rounds = 1
        await c.send('Research this.')
        self.assertEqual(len(c.backend.requests), 2)
        exhausted = RoundRecorder.rounds[1]
        self.assertTrue(exhausted['exhausted'])
        self.assertEqual(exhausted['offered'], [])
        self.assertIsNone(exhausted['ui_profile'])
        self.assertEqual(exhausted['last']['role'], 'user')
        self.assertIn(BUDGET_NOTICE, exhausted['last']['content'])
        self.assertEqual(json.dumps(c.backend.requests[1]).count(BUDGET_NOTICE), 1)
        self.assertEqual(RoundRecorder.final, {'exhausted': True, 'round_number': 1,
                                               'activation_rounds': 0})

    # --- the free discovery round -------------------------------------------

    async def test_one_activation_only_round_does_not_spend_the_tool_budget(self):
        c = self.controller([call('activate_tools', {'capability': 'animations'}),
                             call('file_read', {'path': 'missing.txt'}), 'Ready.'], cloud=True)
        c.config.max_tool_rounds = 1
        await c.send('Show this process in a playable visual.')
        self.assertEqual(len(c.backend.requests), 3)
        self.assertEqual([record['free'] for record in RoundRecorder.tools], [True, False])
        self.assertEqual([record['activation_rounds_before'] for record in RoundRecorder.tools],
                         [0, 1])
        # The paid tool round and the final round both still happened.
        self.assertIn('file_read', tool_names(c.backend.offered[1]))
        self.assertEqual(c.backend.offered[2], [])
        self.assertEqual(RoundRecorder.final,
                         {'exhausted': True, 'round_number': 1, 'activation_rounds': 1})
        self.assertEqual((await c.current_messages())[-1].state, MessageState.COMPLETE)

    async def test_the_free_discovery_round_is_granted_only_once(self):
        # Two *successful* activations.  The second is charged as an ordinary
        # round, so the third request is already exhausted and its tool call is
        # refused before any effect.
        c = self.controller([call('activate_tools', {'capability': 'library_write'}),
                             call('activate_tools', {'capability': 'animations'}),
                             call('file_read', {'path': 'missing.txt'}), 'Unreached.'], cloud=True)
        c.config.knowledge_enabled = True
        c.config.max_tool_rounds = 1
        with self.assertRaisesRegex(RuntimeError, 'exceeded the tool-call limit'):
            await c.send('Research these options.')
        self.assertEqual(len(c.backend.requests), 3)
        self.assertEqual([record['free'] for record in RoundRecorder.tools], [True, False])
        self.assertEqual(RoundRecorder.rounds[1]['capabilities'], ['animations'])
        self.assertEqual(RoundRecorder.rounds[1]['activation_rounds'], 1)
        self.assertTrue(RoundRecorder.rounds[2]['exhausted'])
        self.assertEqual(c.backend.offered[2], [])
        self.assertEqual((await c.current_messages())[-1].state, MessageState.ERROR)

    # --- UI segment and UI fallback -----------------------------------------

    async def test_a_ui_segment_offers_no_tools_and_ends_the_turn(self):
        c = self.controller([call('activate_tools', {'capability': 'interactive_ui'}),
                             'A plain Markdown answer.'])
        c.config.interactive_ui_enabled = True
        await c.send('Compare these options.')
        self.assertEqual(len(c.backend.requests), 2)
        segment = RoundRecorder.rounds[1]
        self.assertEqual(segment['ui_profile'], 'mixed')
        self.assertEqual(segment['offered'], [])
        self.assertFalse(segment['exhausted'])
        self.assertNotIn(BUDGET_NOTICE, json.dumps(segment['messages']))
        self.assertIn('UI contract: mixed', segment['system'])
        self.assertIn('UI contract: mixed', segment['system_after'])
        self.assertTrue(segment['ui_used_after'])
        self.assertEqual(segment['ui_profile_after'], 'mixed')
        self.assertIsNone(RoundRecorder.streams[1]['ui_profile'])
        self.assertEqual(RoundRecorder.final,
                         {'exhausted': False, 'round_number': 0, 'activation_rounds': 1})
        saved = (await c.current_messages())[-1]
        self.assertEqual(saved.state, MessageState.COMPLETE)
        self.assertEqual(saved.content, 'A plain Markdown answer.')

    async def test_the_ui_fallback_restores_tools_and_keeps_the_ui_latch(self):
        c = self.controller([call('activate_tools', {'capability': 'interactive_ui'}),
                             'Markdown answer.'])
        c.config.interactive_ui_enabled = True
        c.config.knowledge_enabled = True
        with self.reject_ui_admission():
            await c.send('Compare these options.')
        self.assertEqual(len(c.backend.requests), 2)
        fallback = RoundRecorder.rounds[1]
        self.assertEqual(fallback['ui_profile'], 'mixed')
        self.assertIsNone(fallback['ui_profile_after'])
        self.assertTrue(fallback['ui_used_after'])
        self.assertIn('file_read', fallback['offered'])
        self.assertEqual(fallback['capabilities'], ['library_write'])
        self.assertIn('UI contract:', fallback['system'])
        self.assertNotIn('UI contract:', fallback['system_after'])
        self.assertEqual(fallback['last']['role'], 'tool')
        self.assertIn('Interactive UI contract could not fit', fallback['last']['content'])
        self.assertEqual(json.dumps(c.backend.requests[1]).count('Interactive UI contract could not fit'), 1)
        saved = (await c.current_messages())[-1]
        self.assertEqual(saved.state, MessageState.COMPLETE)
        self.assertEqual(saved.content, 'Markdown answer.')

    async def test_ui_cannot_be_reactivated_after_the_fallback(self):
        c = self.controller([call('activate_tools', {'capability': 'interactive_ui'}),
                             call('activate_tools', {'capability': 'interactive_ui'}),
                             'Final answer.'])
        c.config.interactive_ui_enabled = True
        c.config.knowledge_enabled = True
        with self.reject_ui_admission():
            await c.send('Compare these options.')
        self.assertEqual(len(c.backend.requests), 3)
        result = tool_result(c.backend.requests[2], 'activate_tools')
        self.assertIn('error', json.loads(result['content']))
        self.assertEqual(RoundRecorder.rounds[2]['capabilities'], ['library_write'])
        self.assertTrue(RoundRecorder.rounds[2]['ui_used'])
        self.assertNotIn('UI contract:', RoundRecorder.rounds[2]['system'])
        saved = (await c.current_messages())[-1]
        self.assertEqual(saved.state, MessageState.COMPLETE)
        self.assertEqual(saved.content, 'Final answer.')

    async def test_a_ui_segment_that_returns_tool_calls_runs_nothing(self):
        c = self.controller([call('activate_tools', {'capability': 'interactive_ui'}),
                             call('file_read', {'path': 'missing.txt'})])
        c.config.interactive_ui_enabled = True
        with self.assertRaisesRegex(RuntimeError, 'Interactive UI segment did not finish normally'):
            await c.send('Compare these options.')
        self.assertEqual(len(c.backend.requests), 2)
        # No tool continuation was appended: the segment's calls never ran.
        self.assertIsNone(tool_result(c.backend.requests[1], 'file_read'))
        self.assertEqual((await c.current_messages())[-1].state, MessageState.ERROR)

    async def test_the_fallback_corrects_the_activation_result_not_the_last_call(self):
        # H1 (fixed): `_admit_round` corrects the activate_tools result itself,
        # so a later call's evidence survives the UI-fit fallback.
        (self.root / 'note.txt').write_text('Fixture evidence.')
        c = self.controller([[call('activate_tools', {'capability': 'interactive_ui'}),
                              call('file_read', {'path': 'note.txt'})], 'Markdown answer.'])
        c.config.interactive_ui_enabled = True
        await c.set_workspace(str(self.root))
        with self.reject_ui_admission():
            await c.send('Compare the options in note.txt.')
        self.assertEqual(len(c.backend.requests), 2)
        request = c.backend.requests[1]
        self.assertIn('Fixture evidence.', json.dumps(request))
        read = tool_result(request, 'file_read')
        activation = tool_result(request, 'activate_tools')
        self.assertNotIn('Interactive UI contract could not fit', read['content'])
        self.assertIn('Interactive UI contract could not fit', activation['content'])
        saved = (await c.current_messages())[-1]
        self.assertEqual(saved.state, MessageState.COMPLETE)

    async def test_a_paid_activation_round_admits_the_ui_segment_at_the_budget_edge(self):
        # UI admission outranks exhaustion: a two-call activation round is paid,
        # so the next round is already exhausted, yet the armed UI segment is
        # still admitted (tool-free and without the budget notice).
        c = self.controller([turn(call('activate_tools', {'capability': 'interactive_ui'}),
                                  call('file_read', {'path': 'missing.txt'})),
                             'Markdown answer.'])
        c.config.interactive_ui_enabled = True
        c.config.max_tool_rounds = 1
        await c.send('Compare these options.')
        self.assertEqual(len(c.backend.requests), 2)
        segment = RoundRecorder.rounds[1]
        self.assertTrue(segment['exhausted'])
        self.assertEqual(segment['ui_profile'], 'mixed')
        self.assertEqual(segment['offered'], [])
        self.assertNotIn(BUDGET_NOTICE, json.dumps(segment['messages']))
        self.assertEqual(RoundRecorder.final['round_number'], 1)
        self.assertEqual((await c.current_messages())[-1].state, MessageState.COMPLETE)

    async def test_the_fallback_at_the_budget_edge_keeps_tools_withdrawn(self):
        c = self.controller([turn(call('activate_tools', {'capability': 'interactive_ui'}),
                                  call('file_read', {'path': 'missing.txt'})),
                             'Markdown answer.'])
        c.config.interactive_ui_enabled = True
        c.config.knowledge_enabled = True
        c.config.max_tool_rounds = 1
        with self.reject_ui_admission():
            await c.send('Compare these options.')
        self.assertEqual(len(c.backend.requests), 2)
        fallback = RoundRecorder.rounds[1]
        self.assertTrue(fallback['exhausted'])
        self.assertIsNone(fallback['ui_profile_after'])
        self.assertEqual(fallback['offered'], [])
        self.assertEqual(fallback['last']['role'], 'tool')
        self.assertIn('Interactive UI contract could not fit', json.dumps(fallback['messages']))
        self.assertEqual((await c.current_messages())[-1].state, MessageState.COMPLETE)

    # --- offering is derived from the flags ---------------------------------

    async def test_activation_capabilities_disappear_once_their_flag_is_set(self):
        c = self.runtime.controller
        c.model_name = 'opencode-go/test-vision'
        c.config.interactive_ui_enabled = True
        c.config.citation_rigor = 'off'
        c.config.knowledge_enabled = True
        fresh = activation_capabilities(c.tool_definitions())
        self.assertEqual(fresh, ['interactive_ui', 'animations', 'library_write'])
        self.assertEqual(activation_capabilities(c.tool_definitions(ui_used=True)),
                         ['animations', 'library_write'])
        self.assertEqual(activation_capabilities(c.tool_definitions(animations_enabled=True)),
                         ['interactive_ui', 'library_write'])
        self.assertEqual(activation_capabilities(c.tool_definitions(library_write_enabled=True)),
                         ['interactive_ui', 'animations'])
        self.assertIsNone(activation_capabilities(c.tool_definitions(
            ui_used=True, animations_enabled=True, library_write_enabled=True)))
        self.assertNotIn('file_read', activation_capabilities(c.tool_definitions()) or [])

    # --- mid-round capability changes ---------------------------------------

    async def test_a_skill_read_in_the_last_paid_round_discloses_an_unreachable_contract(self):
        # H2: the animation contract enters the system message for a request
        # whose toolset has already lost the animation tools to the budget.
        self.install_skill(ANIMATION_SKILL_ID)
        c = self.controller([call('read_skill', {'skill_id': ANIMATION_SKILL_ID}),
                             'No animation then.'], cloud=True)
        c.config.max_tool_rounds = 1
        await c.send('Explain this process using the relevant skill.')
        self.assertEqual(len(c.backend.requests), 2)
        self.assertNotIn('animation_create', tool_names(c.backend.offered[0]))
        self.assertIn(MODEL_INSTRUCTIONS, c.backend.requests[1][0]['content'])
        self.assertEqual(c.backend.offered[1], [])
        saved = (await c.current_messages())[-1]
        self.assertEqual(saved.state, MessageState.COMPLETE)
        self.assertEqual(saved.attachments, [])

    async def test_a_skill_read_switches_the_image_contract_inside_the_same_round(self):
        # H3: `read_skill` sets `prompt.xiaohei` between calls of one round, so an
        # ``image_gen`` authored under the advertised schema is rejected against
        # the newly current one before any image job starts.  The attempt latch
        # also turns that rejected call into an unmet illustration obligation.
        self.install_skill(XIAOHEI_SKILL_ID)
        c = self.controller([turn(call('read_skill', {'skill_id': XIAOHEI_SKILL_ID}),
                                  call('image_gen', {'prompt': 'A lighthouse at dusk'})), 'Done.'])
        c.config.image_provider = 'openrouter'
        c.image_generator = ImageProvider()
        with self.assertRaisesRegex(RuntimeError, 'Illustration generation did not complete'):
            await c.send('Read the illustration skill and make an image.')
        self.assertEqual(len(c.backend.requests), 2)
        result = json.loads(tool_result(c.backend.requests[1], 'image_gen')['content'])
        self.assertIn('error', result)
        self.assertIn('draft_id', json.dumps(result))
        c.image_generator.generate.assert_not_called()
        self.assertEqual((await c.current_messages())[-1].state, MessageState.ERROR)


class RoundStreamLatchTests(unittest.IsolatedAsyncioTestCase):
    """``_stream_round`` latches the finish reason and refuses any later output."""

    asyncSetUp = fixtures.ImageTests.asyncSetUp
    asyncTearDown = fixtures.ImageTests.asyncTearDown

    def controller(self, choices):
        c = self.runtime.controller
        c.backend = Backend([Raw(tuple(choices))])
        c.schedule_title = Mock()
        return c

    async def test_output_after_the_finish_latch_is_rejected(self):
        tail = (
            ({'delta': {'content': 'Late text.'}}, 'content'),
            ({'delta': {'reasoning_content': 'Late thought.'}}, 'reasoning'),
            ({'delta': {'tool_calls': [{'index': 0, 'id': 'call-late',
                                        'function': {'name': 'file_read',
                                                     'arguments': '{}'}}]}}, 'tool_calls'),
        )
        for choice, label in tail:
            with self.subTest(stream=label):
                c = self.controller([{'delta': {'content': 'Partial.'}, 'finish_reason': 'stop'},
                                     choice])
                with self.assertRaisesRegex(RuntimeError, 'after its finish reason'):
                    await c.send('Answer')
                saved = (await c.current_messages())[-1]
                self.assertEqual(saved.state, MessageState.ERROR)
                self.assertEqual(saved.content, 'Partial.')
                self.assertEqual(len(c.backend.requests), 1)
                await c.new_conversation()

    async def test_conflicting_finish_reasons_are_rejected(self):
        c = self.controller([{'delta': {'content': 'Partial.'}, 'finish_reason': 'stop'},
                             {'delta': {}, 'finish_reason': 'end_turn'}])
        with self.assertRaisesRegex(RuntimeError, 'conflicting finish reasons'):
            await c.send('Answer')
        self.assertEqual((await c.current_messages())[-1].content, 'Partial.')

    async def test_an_identical_repeated_finish_reason_is_accepted(self):
        c = self.controller([{'delta': {'content': 'Answer.'}, 'finish_reason': 'stop'},
                             {'delta': {}, 'finish_reason': 'stop'}])
        turn = await c.send('Answer')
        self.assertEqual(turn.assistant_message.state, MessageState.COMPLETE)
        self.assertEqual(turn.assistant_message.content, 'Answer.')

    async def test_a_round_without_a_finish_reason_is_never_a_finished_answer(self):
        c = self.controller([{'delta': {'content': 'Answer.'}}])
        with self.assertRaisesRegex(RuntimeError, 'did not finish normally'):
            await c.send('Answer')
        saved = (await c.current_messages())[-1]
        self.assertEqual(saved.state, MessageState.ERROR)
        self.assertEqual(saved.content, 'Answer.')


if __name__ == '__main__':
    unittest.main()
