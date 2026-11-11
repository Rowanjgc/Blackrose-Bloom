"""Interactive UI authority, streaming, durable replay and turn integration."""
import asyncio
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from blackrose.gui.bridge import coalesce
from blackrose.prompts import wire_message
from blackrose.ui_documents import (UIStream, bounded, contract, continuation,
                                    validate_document, validate_state)
import test_gui_images as fixtures
from test_turns import Backend
from blackrose import ui_documents


def example(kind='form'):
    return deepcopy(contract()['examples'][kind])


def source(kind='form'):
    return json.dumps(example(kind))


class ValidationTests(unittest.TestCase):
    def test_artifact_cache_invalidation_and_isolation(self):
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory) / 'ui-contract.json'
            bundle = artifact.parent / 'ui-runtime.js'
            original_bundle = ui_documents.ARTIFACT.parent / 'ui-runtime.js'
            artifact.write_bytes(ui_documents.ARTIFACT.read_bytes())
            bundle.write_bytes(original_bundle.read_bytes())
            with patch.object(ui_documents, 'ARTIFACT', artifact):
                original = ui_documents.contract()
                original['prompts']['mixed'] = 'corrupted by caller'
                self.assertNotEqual(ui_documents.instructions(), 'corrupted by caller')
                self.assertTrue(ui_documents.available())
                saved = bundle.stat()
                data = bytearray(bundle.read_bytes())
                data[0] ^= 1
                bundle.write_bytes(data)
                os.utime(bundle, ns=(saved.st_atime_ns, saved.st_mtime_ns))
                self.assertFalse(ui_documents.available())
                bundle.unlink()
                self.assertFalse(ui_documents.available())
                bundle.symlink_to(original_bundle)
                self.assertTrue(ui_documents.available())
                saved = artifact.stat()
                data = bytearray(artifact.read_bytes())
                data[data.index(b'blackrose')] ^= 1
                artifact.write_bytes(data)
                os.utime(artifact, ns=(saved.st_atime_ns, saved.st_mtime_ns))
                self.assertFalse(ui_documents.available())
                replacement = artifact.with_suffix('.new')
                replacement.write_text('{}')
                replacement.replace(artifact)
                self.assertFalse(ui_documents.available())
                with self.assertRaises(KeyError):
                    ui_documents.contract()

    def test_fixtures_and_numeric_validation(self):
        for kind in contract()['examples']:
            with self.subTest(kind=kind):
                self.assertEqual(validate_document(source(kind))['type'], kind)
        for value in (float('nan'), float('inf'), 1e20):
            with self.assertRaises(ValueError):
                bounded({'value': value})

    def test_unknown_fields_wrong_types_missing_choices_and_duplicate_ids(self):
        docs = []
        d=example(); d['onclick']='alert(1)'; docs.append(d)
        d=example(); d['fields'][0]['kind']='password'; docs.append(d)
        d=example(); d['fields'][0]['default']=True; docs.append(d)
        d=example(); d['fields'][0]={'id':'choice','label':'Choice','kind':'select'}; docs.append(d)
        d=example(); d['fields']*=2; docs.append(d)
        d=example('comparison'); d['rows'][0]['extra']='unknown'; docs.append(d)
        for d in docs:
            with self.subTest(document=d), self.assertRaises(ValueError):
                validate_document(json.dumps(d))
        for bad in ('{"type":"form","type":"text"}', '{', '['*40, 'x'*65537):
            with self.assertRaises(ValueError):
                validate_document(bad)

    def test_profile_and_state_bounds(self):
        with self.assertRaises(ValueError):
            validate_document(source('steps'), 'form')
        for state in ({'unknown':4},{'days':True},{'days':8},{'days':'3'}):
            with self.assertRaises(ValueError):
                validate_state(example(), state, submission=True)
        self.assertEqual(validate_state(example(), {'days':3}, submission=True), {'days':3})
        with self.assertRaises(ValueError):
            validate_state(example(), {}, submission=True)
        for state in ({'selected':-1},{'selected':True},{'sort':'not a column'}):
            with self.assertRaises(ValueError):
                validate_state(example('comparison'),state)
        with self.assertRaises(ValueError):
            bounded({'__proto__':{}})

    def test_blank_preferences_require_answers_and_supported_defaults_still_validate(self):
        document = example()
        self.assertTrue(all('default' not in f for f in document['fields']))
        self.assertEqual(validate_state(document, {}), {})
        with self.assertRaisesRegex(ValueError, 'Required form field'):
            validate_state(document, {}, submission=True)
        document['fields'][0]['default'] = 3
        document['fields'][1]['default'] = 'Ebook'
        self.assertEqual(validate_document(json.dumps(document)), document)

    def test_calculation_quantities_and_legacy_scale_state(self):
        document = example('calculation') | {'items': [
            {'label': 'Desk', 'quantity': 1, 'unit_price': 320},
            {'label': 'Chair', 'quantity': 2, 'unit_price': 210},
            {'label': 'Monitor', 'quantity': 0, 'unit_price': 180},
        ]}
        for state in ({}, {'scale': 2}, {'quantities': [1, 1, 1]},
                      {'scale': 0, 'quantities': [0, 0.5, 1000000]},
                      {'scale': None, 'quantities': [None, 0, 0.00001]}):
            with self.subTest(state=state):
                self.assertEqual(validate_state(document, state), state)
        for state in ({'quantities': []}, {'quantities': [1, 1]},
                      {'quantities': [1, 1, 1, 1]}, {'quantities': {'0': 1}},
                      {'quantities': [1, True, 0]}, {'quantities': [1, '2', 0]},
                      {'quantities': [1, -1, 0]}, {'quantities': [1, 1000001, 0]},
                      {'quantities': [1, float('nan'), 0]}, {'scale': True},
                      {'scale': -1}, {'scale': 1001}, {'unknown': 1}):
            with self.subTest(state=state), self.assertRaises(ValueError):
                validate_state(document, state)

    def test_comparison_source_order_direction_and_legacy_state(self):
        document = example('comparison')
        for state in ({}, {'sort': 'Approach'},
                      {'query': 'semantic', 'sort': 'Approach', 'direction': 'descending', 'selected': 1},
                      {'direction': 'ascending'}):
            with self.subTest(state=state):
                self.assertEqual(validate_state(document, state), state)
        for direction in ('sideways', '', None, True, 1, []):
            with self.subTest(direction=direction), self.assertRaisesRegex(ValueError, 'sort direction'):
                validate_state(document, {'direction': direction})

    def test_cleared_optional_select_is_omitted_not_an_empty_choice(self):
        document = example()
        document['fields'][1]['default'] = 'Either'
        self.assertEqual(validate_state(document, {'days': 3}, submission=True), {'days': 3})
        with self.assertRaisesRegex(ValueError, 'unknown choice'):
            validate_state(document, {'days': 3, 'format': ''})
        text = continuation({'document': document}, {'action_id': 'submit', 'values': {'days': 3}})
        self.assertIn('Days per week: 3', text)
        self.assertNotIn('Preferred format', text)
        document['fields'][1]['required'] = True
        with self.assertRaisesRegex(ValueError, 'Required form field'):
            validate_state(document, {'days': 3}, submission=True)

    def test_stream_preserves_order_and_actions_require_completion(self):
        from types import SimpleNamespace
        stream=UIStream(SimpleNamespace(id=2,conversation_id=1))
        stream.begin();stream.append('Evidence first.');stream.end()
        stream.begin('mixed');stream.append(source()+'\n'+source('calculation')[:20])
        self.assertEqual([b['kind'] for b in stream.blocks],['markdown','interactive_ui'])
        self.assertEqual(stream.blocks[-1]['status'],'streaming')
        stream.append(source('calculation')[20:]);stream.end()
        self.assertEqual(len(stream.blocks),3)
        self.assertTrue(all(b['status']=='ready' for b in stream.blocks))
        with self.assertRaises(ValueError):
            continuation(stream.blocks[-1], {'action_id':'submit','values':{}})

    def test_prose_after_a_valid_block_falls_back_to_markdown(self):
        from types import SimpleNamespace
        stream=UIStream(SimpleNamespace(id=2,conversation_id=1))
        stream.begin('mixed')
        stream.append(source()+'\nThe animation uses the palette.\n\n- one\n- two\n')
        stream.end()
        self.assertEqual([b['kind'] for b in stream.blocks],['interactive_ui','markdown'])
        self.assertEqual(stream.blocks[1]['source'],
                         'The animation uses the palette.\n\n- one\n- two\n')
        self.assertTrue(all(b['status']=='ready' for b in stream.blocks))

    def test_prose_only_segment_never_creates_invalid_blocks(self):
        from types import SimpleNamespace
        stream=UIStream(SimpleNamespace(id=2,conversation_id=1))
        stream.begin('mixed')
        stream.append('I will build it.\n\n1. Setup\n2. Stitch\n')
        stream.end()
        self.assertEqual([b['kind'] for b in stream.blocks],['markdown'])
        self.assertEqual(stream.blocks[0]['source'],'I will build it.\n\n1. Setup\n2. Stitch\n')

    def test_cancel_invalid_and_coalescing_keep_canonical_snapshots(self):
        from types import SimpleNamespace
        stream=UIStream(SimpleNamespace(id=2,conversation_id=1))
        stream.begin('mixed');stream.append(source()+'\n{');stream.end('cancelled')
        self.assertEqual(stream.blocks[0]['status'],'cancelled')
        self.assertEqual(stream.blocks[1]['status'],'invalid')
        first={'key':1,'blocks':[{'source':'a'}]};last={'key':1,'blocks':[{'source':'abc'}]}
        events=coalesce([('ui_update',first),('reply_update',{'key':1,'html':'x'}),('ui_update',last)])
        self.assertEqual(events[0],('ui_update',last))
        self.assertEqual(len(events),2)


class UITests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=fixtures.ImageTests.asyncSetUp
    asyncTearDown=fixtures.ImageTests.asyncTearDown

    def controller(self, steps):
        c=self.runtime.controller
        c.config.interactive_ui_enabled=True
        c.config.interactive_ui_actions_enabled=True
        c.backend=Backend(steps)
        return c

    async def create(self):
        c=self.controller([[('activate_tools',{'capability':'interactive_ui','profile':'form'})],source()])
        await c.send('Collect my reading preferences.')
        await self.session.render_transcript()
        block=next(p for k,p in reversed(self.events) if k=='transcript')['messages'][-1]['blocks'][-1]
        return c, block

    def envelope(self, block):
        return {'conversation_id':block['conversation_id'],'assistant_message_id':block['message_id'],
                'block_id':block['block_id'],'source_revision':1,'view_id':block['view_id']}

    async def test_disclosure_local_model_no_tools_in_ui_segment_and_history(self):
        c,b=await self.create()
        self.assertEqual(c.backend.offered[1],[])
        self.assertNotIn('Interactive answer protocol',c.backend.requests[0][0]['content'])
        self.assertIn('Interactive answer protocol',c.backend.requests[1][0]['content'])
        message=(await c.current_messages())[-1]
        self.assertEqual(message.blocks[-1]['document'],example())
        history=wire_message(message,c._store,False)
        self.assertIn('Reading preferences',history['content'])
        self.assertNotIn('source_hash',history['content'])
        c.backend=Backend(['Hello.'])
        await c.send('Hello')
        self.assertNotIn('Interactive answer protocol',c.backend.requests[0][0]['content'])
        self.assertEqual((await c.current_messages())[-1].blocks,[])

    async def test_calculation_edits_persist_on_reopen_without_a_model_call(self):
        document = example('calculation') | {'items': [
            {'label': 'Desk', 'quantity': 1, 'unit_price': 320},
            {'label': 'Chair', 'quantity': 2, 'unit_price': 210},
            {'label': 'Monitor', 'quantity': 0, 'unit_price': 180},
        ]}
        c = self.controller([[('activate_tools', {'capability': 'interactive_ui', 'profile': 'calculation'})],
                             json.dumps(document)])
        await c.send('Let me change office quantities independently.')
        await self.session.render_transcript()
        block = next(p for k, p in reversed(self.events) if k == 'transcript')['messages'][-1]['blocks'][0]
        for sequence, values in enumerate(({'scale': 2}, {'scale': 1, 'quantities': [1, 1, 1]},
                                           {'scale': 0.5, 'quantities': [0, 0.5, None]}), 1):
            result = await self.session.ui_state(self.envelope(block) | {
                'state_sequence': sequence, 'values': values})
            self.assertTrue(result['ok'])
            await self.session.render_transcript()
            reopened = next(p for k, p in reversed(self.events) if k == 'transcript')['messages'][-1]['blocks'][0]
            self.assertEqual(reopened['saved_state'], values)
            self.assertEqual(reopened['state_sequence'], sequence)
            self.assertEqual(reopened['document'], document)
            block = reopened
        self.assertEqual(len(c.backend.requests), 2)
        self.assertEqual(len(await c.current_messages()), 2)

    async def test_generation_flag_and_code_examples_stay_markdown(self):
        c=self.controller(['```openui\nroot = Form([])\n```'])
        turn=await c.send('Explain the code')
        self.assertEqual(turn.assistant_message.blocks,[])
        c.config.interactive_ui_enabled=False
        c.backend=Backend(['Ordinary answer'])
        await c.send('Hello')
        activations=[t for t in c.backend.offered[0] if t['function']['name']=='activate_tools']
        self.assertEqual(activations,[])

    async def test_missing_runtime_keeps_ordinary_markdown_available(self):
        c=self.controller(['Still a useful answer.'])
        with patch('blackrose.ui_documents.available',return_value=False):
            turn=await c.send('Hello')
        self.assertEqual(turn.assistant_message.content,'Still a useful answer.')
        self.assertEqual(turn.assistant_message.blocks,[])

    async def test_stop_during_action_validation_has_no_missing_generation(self):
        self.session._ui_action_pending=True
        try:
            self.assertFalse(self.session.stop())
        finally:
            self.session._ui_action_pending=False

    async def test_cloud_provider_fixture_uses_the_same_text_protocol(self):
        c=self.controller([[('activate_tools',{'capability':'interactive_ui','profile':'form'})],source()])
        c.model_name='opencode-go/test-vision'
        turn=await c.send('Collect preferences')
        self.assertEqual(c.backend.offered[1],[])
        self.assertEqual(turn.assistant_message.blocks[0]['document']['type'],'form')

    async def test_invalid_block_is_repaired_in_place_without_a_visible_turn(self):
        bad=json.dumps({'type':'comparison','title':'Bad','columns':['A','B'],'rows':[{'A':'1','C':'2'}]})
        c=self.controller([[('activate_tools',{'capability':'interactive_ui','profile':'comparison'})],bad,source('comparison')])
        turn=await c.send('Compare the options')
        block=turn.assistant_message.blocks[0]
        self.assertEqual(block['status'],'ready')
        self.assertEqual(block['document'],example('comparison'))
        self.assertEqual(block['source_revision'],2)
        self.assertEqual(block['error'],None)
        self.assertEqual(c.backend.offered[2],[])
        self.assertNotIn('Compare the options',json.dumps(c.backend.requests[2]))
        self.assertEqual(len(await c.current_messages()),2)
        saved=await c._repo.ui_block(block['conversation_id'],block['message_id'],block['block_id'])
        self.assertEqual(saved['document'],example('comparison'))
        self.assertEqual(saved['source_revision'],2)

    async def test_failed_repair_keeps_the_readable_fallback(self):
        bad=json.dumps({'type':'comparison','title':'Bad','columns':['A','B'],'rows':[{'A':'1','C':'2'}]})
        c=self.controller([[('activate_tools',{'capability':'interactive_ui','profile':'comparison'})],bad,bad])
        turn=await c.send('Compare the options')
        block=turn.assistant_message.blocks[0]
        self.assertEqual(block['status'],'invalid')
        self.assertEqual(block['source_revision'],1)
        self.assertIn('could not be completed',block['fallback_text'])

    async def test_oversized_repair_is_not_sent_to_the_model(self):
        # Prose now falls back to Markdown, so use a JSON-looking block to
        # exercise the oversized invalid-block repair path.
        bad='{"type":"form","title":"'+'x'*60000+'"}'
        c=self.controller([])
        c.backend=Backend([[('activate_tools',{'capability':'interactive_ui','profile':'form'})],
                           bad,source()],context=32768)
        turn=await c.send('Collect preferences')
        block=turn.assistant_message.blocks[0]
        self.assertEqual(len(c.backend.requests),2)
        self.assertEqual(turn.assistant_message.state.value,'complete')
        self.assertEqual(block['status'],'invalid')
        self.assertEqual(block['source_revision'],1)
        self.assertIn('could not be completed',block['fallback_text'])

    async def test_repair_can_be_disabled(self):
        bad=json.dumps({'type':'comparison','title':'Bad','columns':['A','B'],'rows':[{'A':'1','C':'2'}]})
        c=self.controller([[('activate_tools',{'capability':'interactive_ui','profile':'comparison'})],bad,source('comparison')])
        c.config.interactive_ui_repairs=0
        turn=await c.send('Compare the options')
        self.assertEqual(turn.assistant_message.blocks[0]['status'],'invalid')
        self.assertEqual(len(c.backend.requests),2)

    async def test_source_export_is_owned_and_independent_of_action_flag(self):
        c,block=await self.create()
        c.config.interactive_ui_actions_enabled=False
        self.assertEqual(json.loads(await self.session.ui_source(self.envelope(block))),example())
        for changes in ({'conversation_id':999},{'source_revision':2},{'view_id':'obsolete'}):
            with self.assertRaises(ValueError):
                await self.session.ui_source(self.envelope(block)|changes)

    async def test_stream_budgets_fail_to_terminal_fallback_without_actions(self):
        for output in ('x'*65537, (source()+'\n')*13):
            with self.subTest(size=len(output)):
                c=self.controller([[('activate_tools',{'capability':'interactive_ui'})],output])
                await c.new_conversation()
                with self.assertRaises(ValueError):
                    await c.send('A form')
                message=(await c.current_messages())[-1]
                self.assertEqual(message.state.value,'error')
                self.assertTrue(all(b['status']!='ready' for b in message.blocks))

    async def test_state_sequences_reload_obsolete_views_and_isolation(self):
        c,b=await self.create()
        payload=self.envelope(b)|{'state_sequence':1,'values':{'days':4}}
        self.assertTrue((await self.session.ui_state(payload))['ok'])
        self.assertFalse((await self.session.ui_state(payload))['ok'])
        self.assertFalse((await self.session.ui_state(payload|{'state_sequence':2,'values':{'days':99}}))['ok'])
        await self.session.redraw()
        self.assertFalse((await self.session.ui_state(payload|{'state_sequence':2}))['ok'])
        saved=(await c.current_messages())[-1].blocks[-1]
        self.assertEqual(saved['saved_state'],{'days':4})
        self.assertEqual(saved['state_sequence'],1)
        await c.new_conversation()
        self.assertFalse((await self.session.ui_state(payload|{'state_sequence':2}))['ok'])

    async def test_valid_action_exactly_one_visible_user_turn_preserves_draft(self):
        c,b=await self.create()
        c.backend=Backend(['Your reading plan.'])
        self.session._draft_text='Keep my draft'
        payload=self.envelope(b)|{'event_id':'test_event_0123456789','action_id':'submit',
            'action_type':'continue_conversation','values':{'days':4}}
        self.assertTrue((await self.session.ui_action(payload))['ok'])
        self.assertFalse((await self.session.ui_action(payload))['ok'])
        await self.session._generation
        self.assertEqual(self.session._draft_text,'Keep my draft')
        messages=await c.current_messages()
        self.assertEqual(len([m for m in messages if m.role=='user']),2)
        self.assertIn('Days per week: 4',messages[-2].content)
        self.assertNotIn('document_source',messages[-2].content)
        self.assertIn('"days": 4',wire_message(messages[-2],c._store,False)['content'])
        self.assertIn('document_source',wire_message(messages[-2],c._store,False)['content'])
        self.assertIn('Plan my reading',messages[-2].content)
        self.assertFalse((await self.session.ui_action(payload))['ok'])
        row=await c._repo._db.run(lambda conn:conn.execute('SELECT * FROM ui_events').fetchone())
        self.assertEqual(row['user_message_id'],messages[-2].id)
        self.assertEqual(row['status'],'complete')

    async def test_optional_choice_clearing_survives_save_reload_and_continuation(self):
        c, block = await self.create()
        envelope = self.envelope(block)
        self.assertTrue((await self.session.ui_state(envelope | {
            'state_sequence': 1, 'values': {'days': 3, 'format': 'Ebook'}}))['ok'])
        self.assertTrue((await self.session.ui_state(envelope | {
            'state_sequence': 2, 'values': {'days': 3}}))['ok'])
        await self.session.redraw()
        saved = (await c.current_messages())[-1].blocks[-1]
        self.assertEqual(saved['saved_state'], {'days': 3})
        self.assertEqual(saved['state_sequence'], 2)
        published = next(p for k, p in reversed(self.events) if k == 'snapshot')['messages'][-1]['blocks'][-1]
        c.backend = Backend(['Your plan.'])
        result = await self.session.ui_action(self.envelope(published) | {
            'event_id': 'cleared_choice_0123456789', 'action_id': 'submit',
            'action_type': 'continue_conversation', 'values': saved['saved_state']})
        self.assertTrue(result['ok'], result)
        await self.session._generation
        user = (await c.current_messages())[-2]
        self.assertIn('Days per week: 3', user.content)
        self.assertNotIn('Preferred format', user.content)
        self.assertNotIn('Ebook', user.content)
        self.assertEqual(user.ui_context['submitted_values'], {'days': 3})

    async def test_cancellation_during_acceptance_settles_as_interrupted(self):
        c, block = await self.create()
        payload = self.envelope(block) | {'event_id': 'cancelled_event_01234', 'action_id': 'submit',
            'action_type': 'continue_conversation', 'values': {'days': 4}}
        committed, release = asyncio.Event(), asyncio.Event()
        original = c._repo.accept_ui_event

        async def delayed(*args):
            await original(*args)
            committed.set()
            await release.wait()

        with patch.object(c._repo, 'accept_ui_event', side_effect=delayed):
            task = asyncio.create_task(self.session.ui_action(payload))
            await asyncio.wait_for(committed.wait(), 5)
            task.cancel()
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        row = await c._repo._db.run(lambda conn: conn.execute('SELECT status FROM ui_events').fetchone())
        self.assertEqual(row['status'], 'interrupted')
        self.assertEqual(len(await c.current_messages()), 2)
        self.assertFalse(self.session._ui_action_pending)
        # A new identity can retry; the cancelled identity remains deduplicated.
        self.assertFalse((await self.session.ui_action(payload))['ok'])
        c.backend = Backend(['Retried.'])
        self.assertTrue((await self.session.ui_action(payload | {'event_id': 'retry_event_01234567'}))['ok'])
        await self.session._generation
        self.assertEqual(len(await c.current_messages()), 4)

    async def test_reload_during_acceptance_interrupts_before_a_turn(self):
        c, block = await self.create()
        payload = self.envelope(block) | {'event_id': 'reload_event_0123456', 'action_id': 'submit',
            'action_type': 'continue_conversation', 'values': {'days': 4}}
        original = c._repo.accept_ui_event

        async def reload(*args):
            await original(*args)
            await self.session.redraw()

        with patch.object(c._repo, 'accept_ui_event', side_effect=reload):
            self.assertFalse((await self.session.ui_action(payload))['ok'])
        row = await c._repo._db.run(lambda conn: conn.execute('SELECT status FROM ui_events').fetchone())
        self.assertEqual(row['status'], 'interrupted')
        self.assertEqual(len(await c.current_messages()), 2)

    async def test_startup_reaps_accepted_and_started_events(self):
        c, block = await self.create()
        payload = self.envelope(block) | {'event_id': 'restart_event_012345', 'action_id': 'submit',
            'action_type': 'continue_conversation', 'values': {'days': 4}}
        for started in (False, True):
            payload['event_id'] += 'x'
            owned = await self.session._owned_ui(payload, for_action=True)
            accepted = await c.interactive_answers.accept_action(owned, payload)
            if started:
                await c._repo.begin_turn(c.conversation.id, accepted.text, c.model_name, ui_event_id=accepted.event_id)
            await c._repo.reap_pending()
            row = await c._repo._db.run(lambda conn, accepted=accepted: conn.execute(
                'SELECT status FROM ui_events WHERE event_id=?', (accepted.event_id,)).fetchone())
            self.assertEqual(row['status'], 'interrupted')

    async def test_actions_reject_stale_tampered_cross_conversation_and_oversized(self):
        c,b=await self.create()
        payload=self.envelope(b)|{'event_id':'test_event_0123456789','action_id':'submit',
            'action_type':'continue_conversation','values':{'days':4}}
        mutations=[{'source_revision':2},{'conversation_id':999},{'assistant_message_id':999},
            {'action_id':'file_edit'},{'action_type':'delete'},{'values':{'days':True}},
            {'values':{'days':4,'extra':'x'}},{'values':{'days':'x'*20001}},{'view_id':'old'}, {'view_id':None}]
        for changes in mutations:
            with self.subTest(changes=changes):
                self.assertFalse((await self.session.ui_action(payload|changes))['ok'])
        c.config.interactive_ui_actions_enabled=False
        self.assertFalse((await self.session.ui_action(payload))['ok'])
        c.config.interactive_ui_enabled=False
        await self.session.render_transcript()
        self.assertTrue(next(p for k,p in reversed(self.events) if k=='transcript')['messages'][-1]['blocks'])

    async def test_cancel_inflight_source_restores_cancelled_fallback(self):
        c=self.controller([[('activate_tools',{'capability':'interactive_ui'})]])
        base=c.backend.stream
        seen=asyncio.Event()
        async def stream(model,messages,tools):
            if len(c.backend.requests)==0:
                async for choice in base(model,messages,tools):yield choice
            else:
                yield {'delta':{'content':source()+'\n'}}
                seen.set()
                await asyncio.Event().wait()
        c.backend.stream=stream
        task=asyncio.create_task(c.send('A form'))
        await asyncio.wait_for(seen.wait(),10)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        message=(await c.current_messages())[-1]
        self.assertEqual(message.state.value,'cancelled')
        self.assertEqual(message.blocks[-1]['status'],'cancelled')

    async def test_malformed_block_preserves_valid_content_and_immutable_source(self):
        c=self.controller([[('activate_tools',{'capability':'interactive_ui'})],source()+'\n{bad}'])
        turn=await c.send('A form')
        self.assertEqual([b['status'] for b in turn.assistant_message.blocks],['ready','invalid'])
        blocks=deepcopy(turn.assistant_message.blocks)
        for b in blocks:
            b.pop('saved_state',None);b.pop('state_sequence',None)
        blocks[0]['source']='tampered'
        with self.assertRaises(ValueError):await c._repo.save_blocks(turn.assistant_message.id,blocks)

    async def test_comparison_submission_uses_the_owned_row(self):
        document=example('comparison')|{'submit_label':'Discuss my choice'}
        c=self.controller([[('activate_tools',{'capability':'interactive_ui','profile':'comparison'})],json.dumps(document)])
        await c.send('Compare approaches and let me choose')
        await self.session.render_transcript()
        block=next(p for k,p in reversed(self.events) if k=='transcript')['messages'][-1]['blocks'][-1]
        c.backend=Backend(['Discussed.'])
        payload=self.envelope(block)|{'event_id':'comparison_0123456789','action_id':'submit',
            'action_type':'continue_conversation','values':{'selected':1}}
        self.assertTrue((await self.session.ui_action(payload))['ok'])
        await self.session._generation
        self.assertIn('Semantic',(await c.current_messages())[-2].content)

    async def test_crash_recovery_cancels_streaming_and_preserves_old_chats(self):
        c=self.controller(['A normal answer'])
        await c.send('Hello')
        request,assistant=await c._repo.begin_turn(c.conversation.id,'A form',c.model_name)
        stream=UIStream(assistant)
        stream.begin('form');stream.append(source()+'\n')
        await c._repo.save_blocks(assistant.id,stream.blocks)
        await c._repo.reap_pending()
        messages=await c.current_messages()
        self.assertEqual(messages[1].content,'A normal answer')
        self.assertEqual(messages[1].blocks,[])
        self.assertEqual(messages[-1].blocks[0]['status'],'cancelled')

    async def test_profile_expansion_readmission_falls_back_to_markdown(self):
        c=self.controller(['Done.'])
        await c.send('Collect preferences')
        from blackrose.prompts import estimated_tokens
        bound=estimated_tokens(c.backend.requests[0],c.backend.offered[0])
        await c.new_conversation()
        c.backend=Backend([[('activate_tools',{'capability':'interactive_ui'})], 'Please tell me your preferences.'],context=int(bound/.875)+700)
        turn=await c.send('Collect preferences')
        self.assertEqual(turn.assistant_message.blocks,[])
        self.assertIn('Please tell me',turn.assistant_message.content)
        self.assertNotIn('Interactive answer protocol',c.backend.requests[-1][0]['content'])

    async def test_refinement_context_is_explicit_and_keeps_saved_fields_private(self):
        c,b=await self.create()
        await self.session.ui_state(self.envelope(b)|{'state_sequence':1,'values':{'days':3}})
        c.backend=Backend(['Updated.'])
        await c.send('Revise the form title')
        wire=c.backend.requests[0][-1]['content']
        self.assertIn('Latest interactive answer context',wire)
        self.assertIn('"source"',wire)
        self.assertNotIn('saved_user_values',wire)
        c.backend=Backend(['Updated again.'])
        # Request immediately after the UI-bearing turn for this focused check.
        history=await c.current_messages()
        from blackrose.prompts import PromptBuilder, DraftMessage
        prompt=PromptBuilder(c.config,c.skills,c._store,{}).build(history[:2],DraftMessage('Adjust the form using my saved values'))
        self.assertIn('saved_user_values',prompt.messages[-1]['content'])


if __name__=='__main__':
    unittest.main()
