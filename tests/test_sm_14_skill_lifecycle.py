"""SM-14: the skill package, review-digest and enablement state machine.

Pins the derived states of `SkillManager` (installed package, review record,
switch, effective availability, registry health) and of the bridge's staged
import session, plus the refusal paths that keep third-party text unavailable
until the exact reviewed bytes are approved.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import blackrose.skills as skills_module
from blackrose.gui.bridge import Api
from blackrose.skills import SkillManager

DIGEST = 'a' * 64


def staging_directories(manager: SkillManager) -> list[Path]:
    return sorted(p for p in manager.root.iterdir() if p.name.startswith('.import-'))


def failing_writes(*names):
    """Delegate to the real atomic writer, except for the named registry file."""
    real = skills_module.write_json

    def write(path, value):
        if path.name in names:
            raise OSError(f'injected {path.name} failure')
        return real(path, value)
    return write


def review_record(digest):
    return {'digest': digest, 'source': '/somewhere', 'origin': 'imported'}


class SkillFixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.manager = SkillManager(self.root / 'managed')
        self.source = self.root / 'source'
        self.source.mkdir()
        (self.source / 'SKILL.md').write_text(
            '---\nname: demo\ndescription: Notes.\n---\nBODY')

    def install(self, *, approved=False):
        return self.manager.install(str(self.source), approved=approved)

    def listed(self, key='demo'):
        return next(skill for skill in self.manager.list() if skill['id'] == key)

    def write_state(self, value):
        self.manager.state_path.write_text(json.dumps(value))

    def write_reviews(self, value):
        self.manager.reviews_path.write_text(json.dumps(value))


class SkillAvailabilityGridTests(SkillFixture):
    """`enabled` is a total function of the switch and the review record."""

    def test_every_switch_and_review_combination_has_one_named_state(self):
        self.install()
        entry = self.manager.root / 'demo/SKILL.md'
        digest = skills_module._digest(entry.read_bytes())
        self.assertNotEqual(digest, DIGEST)
        states = {'absent': None, 'on': {'demo': True}, 'off': {'demo': False}}
        reviews = {'absent': None,
                   'unreviewed': {'demo': review_record(None)},
                   'approved': {'demo': review_record(digest)},
                   'changed': {'demo': review_record(DIGEST)}}
        expected = {('absent', 'absent'): ('required', False),
                    ('absent', 'unreviewed'): ('required', False),
                    ('absent', 'approved'): ('approved', True),
                    ('absent', 'changed'): ('changed', False),
                    ('on', 'absent'): ('required', False),
                    ('on', 'unreviewed'): ('required', False),
                    ('on', 'approved'): ('approved', True),
                    ('on', 'changed'): ('changed', False),
                    ('off', 'absent'): ('required', False),
                    ('off', 'unreviewed'): ('required', False),
                    ('off', 'approved'): ('approved', False),
                    ('off', 'changed'): ('changed', False)}
        for state_name, state in states.items():
            for review_name, review in reviews.items():
                with self.subTest(state=state_name, review=review_name):
                    for path, value in ((self.manager.state_path, state),
                                        (self.manager.reviews_path, review)):
                        if value is None:
                            path.unlink(missing_ok=True)
                        else:
                            path.write_text(json.dumps(value))
                    skill = self.listed()
                    self.assertEqual((skill['review'], skill['enabled']),
                                     expected[(state_name, review_name)])
                    available = skill['enabled']
                    self.assertEqual(bool(self.manager.catalog()), available)
                    if available:
                        self.assertIn('BODY', self.manager.read('demo')['text'])
                    else:
                        with self.assertRaises(ValueError):
                            self.manager.read('demo')

    def test_review_status_never_returns_anything_else(self):
        for record in (None, {}, {'digest': None}, {'digest': DIGEST},
                       review_record(DIGEST), review_record(None)):
            for digest in (DIGEST, 'b' * 64):
                with self.subTest(record=record):
                    self.assertIn(SkillManager._review_status(record, digest),
                                  ('required', 'approved', 'changed'))

    def test_a_switch_written_without_a_review_does_not_enable_text(self):
        self.install()
        self.write_state({'demo': True})
        self.assertEqual((self.listed()['review'], self.listed()['enabled']),
                         ('required', False))
        self.assertEqual(self.manager.catalog(), '')
        with self.assertRaisesRegex(ValueError, 'until the user reviews it'):
            self.manager.read('demo')

    def test_approval_binds_the_exact_bytes_and_a_change_revokes_availability(self):
        self.install(approved=True)
        self.assertEqual((self.listed()['review'], self.listed()['enabled']), ('approved', True))
        entry = self.manager.root / 'demo/SKILL.md'
        original = entry.read_text()
        entry.write_text(original.replace('BODY', 'UNREVIEWED'))
        self.assertEqual((self.listed()['review'], self.listed()['enabled']), ('changed', False))
        self.assertEqual(self.manager.catalog(), '')
        with self.assertRaisesRegex(ValueError, 'changed after it was approved'):
            self.manager.read('demo')
        with self.assertRaisesRegex(ValueError, 'Review this skill'):
            self.manager.set_enabled('demo', True)
        # The switch itself survives; restoring the approved bytes re-enables it.
        self.assertIs(json.loads(self.manager.state_path.read_text())['demo'], True)
        entry.write_text(original)
        self.assertEqual((self.listed()['review'], self.listed()['enabled']), ('approved', True))


class SkillPublicationRaceTests(SkillFixture):
    """A change between digest verification and activation cannot enable new bytes."""

    def test_bytes_changed_between_digest_check_and_rename_stay_disabled(self):
        staged = self.manager.stage_install(str(self.source))
        approved_text = (staged.package / 'SKILL.md').read_text()
        original_rename = Path.rename
        swapped = []

        def rename_after_swapping(path, target):
            if Path(path) == staged.package and not swapped:
                swapped.append(True)
                (Path(path) / 'SKILL.md').write_text(approved_text.replace('BODY', 'SWAPPED'))
            return original_rename(path, target)

        with patch.object(Path, 'rename', rename_after_swapping):
            self.manager.publish_install(staged, approved=True)
        self.assertTrue(swapped)
        # The persisted digest is the staged digest, so the installed copy is
        # already unreviewed on the very next discovery pass.
        self.assertEqual((self.listed()['review'], self.listed()['enabled']), ('changed', False))
        self.assertEqual(self.manager.catalog(), '')
        with self.assertRaisesRegex(ValueError, 'changed after it was approved'):
            self.manager.read('demo')
        self.assertEqual(staging_directories(self.manager), [])

    def test_failed_state_write_removes_the_package_and_restores_the_review(self):
        staged = self.manager.stage_install(str(self.source))
        with patch('blackrose.skills.write_json', side_effect=failing_writes('state.json')):
            with self.assertRaisesRegex(OSError, 'state.json'):
                self.manager.publish_install(staged, approved=True)
        self.assertFalse((self.manager.root / 'demo').exists())
        self.assertEqual(json.loads(self.manager.reviews_path.read_text()), {})
        self.assertFalse(self.manager.state_path.exists())
        self.assertEqual(staging_directories(self.manager), [])
        self.assertEqual(self.manager.list(), [])

    def test_failed_review_write_removes_the_package_and_leaves_no_record(self):
        staged = self.manager.stage_install(str(self.source))
        with patch('blackrose.skills.write_json', side_effect=failing_writes('reviews.json')):
            with self.assertRaisesRegex(OSError, 'reviews.json'):
                self.manager.publish_install(staged, approved=True)
        self.assertFalse((self.manager.root / 'demo').exists())
        self.assertFalse(self.manager.reviews_path.exists())
        self.assertFalse(self.manager.state_path.exists())
        self.assertEqual(staging_directories(self.manager), [])


class SkillRegistryHealthTests(SkillFixture):
    """Malformed registries fail closed for reads and block every mutation."""

    STATE_CASES = ('{broken', '[]', 'null', '{"demo": "true"}', '{"demo": 1}',
                   '{"other": "yes"}')

    def assert_state_degraded(self):
        self.assertFalse(self.listed()['enabled'])
        self.assertIn(str(self.manager.state_path), self.listed()['description'])
        self.assertEqual(self.manager.catalog(), '')
        with self.assertRaisesRegex(ValueError, 'Back up this file'):
            self.manager.set_enabled('demo', True)
        with self.assertRaisesRegex(ValueError, 'Back up this file'):
            self.manager.set_enabled('demo', False)
        with self.assertRaisesRegex(ValueError, 'Back up this file'):
            self.manager.approve('demo', DIGEST)
        with self.assertRaisesRegex(ValueError, 'left untouched'):
            self.manager.remove('demo')
        with self.assertRaisesRegex(ValueError, 'left untouched'):
            self.manager.install(str(self.source))
        self.assertTrue((self.manager.root / 'demo/SKILL.md').is_file())

    def test_malformed_state_blocks_mutation_and_recovers_when_repaired(self):
        self.install(approved=True)
        for invalid in self.STATE_CASES:
            with self.subTest(invalid=invalid):
                original = self.manager.state_path.read_text()
                self.manager.state_path.write_text(invalid)
                self.manager._logged_state_warning = None
                with self.assertLogs('blackrose.skills', level='WARNING'):
                    self.assert_state_degraded()
                # The failed mutations never rewrote the damaged registry.
                self.assertEqual(self.manager.state_path.read_text(), invalid)
                self.manager.state_path.write_text(original)
                self.assertTrue(self.listed()['enabled'])
                self.assertEqual(self.manager._last_state_warning, None)
                self.assertNotIn(str(self.manager.state_path), self.listed()['description'])

    def test_a_definite_grant_survives_partial_state_corruption(self):
        second = self.manager.root / 'second'
        second.mkdir()
        (second / 'SKILL.md').write_text('---\nname: second\n---\nSecond')
        self.install(approved=True)
        self.manager.approve('second', self.manager.review('second')['digest'])
        self.manager.state_path.write_text('{"demo": true, "second": "yes"}')
        with self.assertLogs('blackrose.skills', level='WARNING'):
            skills = {skill['id']: skill for skill in self.manager.list()}
        self.assertTrue(skills['demo']['enabled'])
        self.assertFalse(skills['second']['enabled'])
        with self.assertRaisesRegex(ValueError, 'Back up this file'):
            self.manager.set_enabled('second', True)

    def test_malformed_reviews_require_review_and_recover_after_repair(self):
        self.install(approved=True)
        invalid_records = ('{broken', '[]', 'null',
                           '{"demo": {"digest": "yes", "origin": "imported"}}',
                           json.dumps({'demo': {'digest': DIGEST}}))
        for invalid in invalid_records:
            with self.subTest(invalid=invalid):
                self.manager.reviews_path.write_text(invalid)
                self.manager._logged_review_warning = None
                with self.assertLogs('blackrose.skills', level='WARNING'):
                    self.assertFalse(self.listed()['enabled'])
                    self.assertEqual(self.listed()['review'], 'required')
                    self.assertIn(str(self.manager.reviews_path), self.listed()['description'])
                    self.assertEqual(self.manager.catalog(), '')
                    with self.assertRaisesRegex(ValueError, 'until the user reviews it'):
                        self.manager.read('demo')
                    with self.assertRaisesRegex(ValueError, 'Back up this file'):
                        self.manager.approve('demo', self.manager.review('demo')['digest'])
                    with self.assertRaisesRegex(ValueError, 'left untouched'):
                        self.manager.remove('demo')
                self.assertEqual(self.manager.reviews_path.read_text(), invalid)
                self.manager.reviews_path.unlink()
                self.assertEqual(self.listed()['review'], 'required')
                self.assertEqual(self.manager._last_review_warning, None)
                self.manager.approve('demo', self.manager.review('demo')['digest'])
                self.assertTrue(self.listed()['enabled'])

    def test_disabling_still_works_when_only_the_review_registry_is_damaged(self):
        self.install(approved=True)
        self.manager.reviews_path.write_text('{broken')
        self.manager.set_enabled('demo', False)
        with self.assertLogs('blackrose.skills', level='WARNING'):
            self.assertFalse(self.listed()['enabled'])
        self.assertIs(json.loads(self.manager.state_path.read_text())['demo'], False)


class SkillRecoveryHazardTests(SkillFixture):
    """Characterizes two fail-open recoveries recorded as SM-14 hazards."""

    def test_deleting_state_json_restores_the_implicit_on_switch(self):
        self.install(approved=True)
        self.manager.set_enabled('demo', False)
        self.assertFalse(self.listed()['enabled'])
        self.manager.state_path.unlink()
        # H5: the recovery instruction does not say that deleting the file
        # re-enables every skill whose bytes are already approved.
        self.assertTrue(self.listed()['enabled'])
        self.assertIn('demo', self.manager.catalog())

    def test_an_approval_whose_switch_write_fails_is_already_effective(self):
        package = self.manager.root / 'demo'
        package.mkdir()
        (package / 'SKILL.md').write_text('---\nname: demo\n---\nBODY')
        digest = self.manager.review('demo')['digest']
        with patch('blackrose.skills.write_json', side_effect=failing_writes('state.json')):
            with self.assertRaisesRegex(OSError, 'state.json'):
                self.manager.approve('demo', digest)
        # H2: the review record is committed before the switch, and an absent
        # switch defaults on, so the reported failure is not a refusal.
        self.assertFalse(self.manager.state_path.exists())
        self.assertEqual(json.loads(self.manager.reviews_path.read_text())['demo']['digest'], digest)
        self.assertTrue(self.listed()['enabled'])


class SkillRemovalRecoveryTests(SkillFixture):
    """A partial removal must not strand a grant that blocks the same skill ID."""

    def test_remove_forgets_records_when_the_package_is_already_gone(self):
        self.install(approved=True)
        shutil.rmtree(self.manager.root / 'demo')
        self.manager.remove('demo')
        self.assertEqual(json.loads(self.manager.state_path.read_text()), {})
        self.assertEqual(json.loads(self.manager.reviews_path.read_text()), {})
        self.install()
        self.assertEqual((self.listed()['review'], self.listed()['enabled']), ('required', False))

    def test_remove_after_a_failed_state_write_is_retryable(self):
        self.install(approved=True)
        with patch('blackrose.skills.write_json', side_effect=failing_writes('state.json')):
            with self.assertRaisesRegex(OSError, 'state.json'):
                self.manager.remove('demo')
        self.assertFalse((self.manager.root / 'demo').exists())
        self.assertIn('demo', json.loads(self.manager.state_path.read_text()))
        with self.assertRaisesRegex(ValueError, 'already installed'):
            self.install()
        # Regression: before the fix this retry raised "Skill not found" and the
        # stranded grant blocked the skill ID forever.
        self.manager.remove('demo')
        self.assertEqual(json.loads(self.manager.state_path.read_text()), {})
        self.assertEqual(json.loads(self.manager.reviews_path.read_text()), {})
        self.install()

    def test_remove_refuses_unknown_keys_and_damaged_registries(self):
        self.install(approved=True)
        before = self.manager.state_path.read_text()
        with self.assertRaisesRegex(ValueError, 'not found'):
            self.manager.remove('ghost')
        self.assertEqual(self.manager.state_path.read_text(), before)
        self.manager.state_path.write_text('{broken')
        with self.assertLogs('blackrose.skills', level='WARNING'):
            with self.assertRaisesRegex(ValueError, 'left untouched'):
                self.manager.remove('demo')
        self.assertTrue((self.manager.root / 'demo/SKILL.md').is_file())


class SkillStagingTests(SkillFixture):
    """Staged copies are private, invisible and refuse every escape."""

    def test_a_staging_tree_is_private_and_invisible_to_discovery(self):
        staged = self.manager.stage_install(str(self.source))
        staging = staged.package.parent
        self.assertEqual(staging.parent, self.manager.root)
        self.assertTrue(staging.name.startswith('.import-'))
        self.assertEqual(stat.S_IMODE(staging.stat().st_mode), 0o700)
        self.assertEqual(self.manager.list(), [])
        self.assertEqual(self.manager.catalog(), '')
        staged.cleanup()
        self.assertFalse(staging.exists())

    def test_nested_symlinks_are_refused_and_leave_no_staging(self):
        package = self.root / 'linked'
        (package / 'nested').mkdir(parents=True)
        (package / 'SKILL.md').write_text('---\nname: linked\n---\nBODY')
        outside = self.root / 'outside.txt'
        outside.write_text('SECRET')
        try:
            (package / 'nested/alias').symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest('symlinks are unavailable')
        with self.assertRaisesRegex(ValueError, 'symlinks'):
            self.manager.install(str(package))
        self.assertFalse((self.manager.root / 'linked').exists())
        self.assertEqual(staging_directories(self.manager), [])
        self.assertEqual(outside.read_text(), 'SECRET')

    def test_special_files_are_refused_and_leave_no_staging(self):
        if not hasattr(os, 'mkfifo'):
            self.skipTest('mkfifo is unavailable')
        package = self.root / 'special'
        package.mkdir()
        (package / 'SKILL.md').write_text('---\nname: special\n---\nBODY')
        os.mkfifo(package / 'pipe')
        with self.assertRaisesRegex(ValueError, 'regular files and directories'):
            self.manager.install(str(package))
        self.assertFalse((self.manager.root / 'special').exists())
        self.assertEqual(staging_directories(self.manager), [])

    def test_duplicate_install_is_refused_before_publishing_anything(self):
        self.install(approved=True)
        with self.assertRaisesRegex(ValueError, 'already installed'):
            self.install()
        self.assertEqual(staging_directories(self.manager), [])
        self.assertTrue((self.manager.root / 'demo/SKILL.md').is_file())

    def test_skill_ids_that_are_paths_are_refused_by_every_entry_point(self):
        self.install(approved=True)
        for key in ('../escape', 'a/b', '.hidden', 'Upper', '', 'a' * 81):
            with self.subTest(key=key):
                with self.assertRaises(ValueError):
                    self.manager.set_enabled(key, False)
                with self.assertRaises(ValueError):
                    self.manager.review(key)
                with self.assertRaises(ValueError):
                    self.manager.remove(key)
        self.assertFalse((self.root / 'escape').exists())
        self.assertTrue((self.manager.root / 'demo/SKILL.md').is_file())


class _Loop:
    def run(self, coroutine):
        return asyncio.run(coroutine)

    def call(self, fn, *args):
        return fn(*args)


class _Worker:
    """Run one callable on another thread and keep its result or exception."""

    def __init__(self, function):
        self.function = function
        self.result = None
        self.error = None
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.ident = None

    def _run(self):
        self.ident = threading.get_ident()
        try:
            self.result = self.function()
        except BaseException as error:  # noqa: BLE001 - the test inspects the type
            self.error = error

    def start(self):
        self.thread.start()
        return self

    def join(self):
        self.thread.join(2)
        if self.thread.is_alive():
            raise AssertionError('test worker did not finish')
        return self


class SkillRegistryConcurrencyTests(SkillFixture):
    """A degraded read keeps its own health even if another thread repairs the file."""

    def test_list_does_not_reinterpret_a_degraded_state_read(self):
        self.install(approved=True)
        self.manager.state_path.write_text('{broken')
        real_read_entry = skills_module._read_entry
        entered, release = threading.Event(), threading.Event()

        def blocked_read_entry(path):
            if threading.get_ident() == running.ident and not entered.is_set():
                entered.set()
                if not release.wait(2):
                    raise TimeoutError('test barrier timed out')
            return real_read_entry(path)

        running = _Worker(self.manager.list)
        with patch('blackrose.skills._read_entry', blocked_read_entry):
            running.start()
            self.assertTrue(entered.wait(2))
            # Another thread repairs the registry and reads it, clearing the
            # shared warning the first read's default depended on.
            self.manager.state_path.write_text(json.dumps({'demo': False}))
            self.assertFalse(self.manager.list()[0]['enabled'])
            release.set()
            running.join()
        self.assertIsNone(running.error)
        self.assertFalse(running.result[0]['enabled'])

    def test_a_mutation_cannot_write_a_partial_state_after_a_concurrent_repair(self):
        self.install(approved=True)
        partial = json.dumps({'demo': False, 'other': 'yes'})
        repaired = json.dumps({'demo': False, 'other': False})
        self.manager.state_path.write_text(partial)
        real_state = self.manager._state
        entered, release = threading.Event(), threading.Event()

        def blocked_state():
            result = real_state()
            if threading.get_ident() == running.ident and not entered.is_set():
                entered.set()
                if not release.wait(2):
                    raise TimeoutError('test barrier timed out')
            return result

        running = _Worker(lambda: self.manager.set_enabled('demo', True))
        with patch.object(self.manager, '_state', blocked_state):
            running.start()
            self.assertTrue(entered.wait(2))
            self.manager.state_path.write_text(repaired)
            self.manager.list()
            release.set()
            running.join()
        # The degraded read must refuse; it must not rewrite the registry with
        # only the definite grants it happened to see.
        self.assertIsInstance(running.error, ValueError)
        self.assertEqual(self.manager.state_path.read_text(), repaired)

    def test_approval_cannot_drop_other_records_after_a_concurrent_repair(self):
        self.install(approved=True)
        second = self.manager.root / 'second'
        second.mkdir()
        (second / 'SKILL.md').write_text('---\nname: second\n---\nSecond')
        self.manager.approve('second', self.manager.review('second')['digest'])
        complete = json.loads(self.manager.reviews_path.read_text())
        digest = self.manager.review('demo')['digest']
        broken = {'demo': complete['demo'],
                  'second': {'digest': 'not-a-digest', 'origin': 'imported'}}
        self.manager.reviews_path.write_text(json.dumps(broken))
        repaired = json.dumps(complete)
        real_reviews = self.manager._reviews
        entered, release = threading.Event(), threading.Event()

        def blocked_reviews():
            result = real_reviews()
            if threading.get_ident() == running.ident and not entered.is_set():
                entered.set()
                if not release.wait(2):
                    raise TimeoutError('test barrier timed out')
            return result

        running = _Worker(lambda: self.manager.approve('demo', digest))
        with patch.object(self.manager, '_reviews', blocked_reviews):
            running.start()
            self.assertTrue(entered.wait(2))
            self.manager.reviews_path.write_text(repaired)
            self.manager.list()
            release.set()
            running.join()
        self.assertIsInstance(running.error, ValueError)
        self.assertEqual(json.loads(self.manager.reviews_path.read_text()), complete)


class SkillImportSessionTests(SkillFixture):
    """The bridge's `(token, staged)` session is single-use and cancel-safe."""

    def setUp(self):
        super().setUp()
        self.api = Api.__new__(Api)
        self.api._loop = _Loop()
        self.api._window = SimpleNamespace(create_file_dialog=lambda *_: [str(self.source)])
        self.controller = SimpleNamespace(is_generating=False, skills=self.manager)
        self.api._runtime = SimpleNamespace(controller=self.controller)
        self.api._skill_import = None

    def import_review(self):
        webview = SimpleNamespace(FileDialog=SimpleNamespace(FOLDER='folder'))
        with patch.dict(sys.modules, {'webview': webview}):
            return self.api.import_skill()

    def finish(self, token, approve=False):
        return self.api.finish_skill_import(token, approve)

    def test_a_published_token_is_single_use(self):
        review = self.import_review()
        self.assertTrue(self.finish(review['token'], True)['enabled'])
        self.assertEqual(len(self.manager.list()), 1)
        with self.assertRaisesRegex(ValueError, 'expired'):
            self.finish(review['token'], True)
        self.assertEqual(len(self.manager.list()), 1)
        self.assertEqual(staging_directories(self.manager), [])

    def test_only_the_literal_true_approves(self):
        review = self.import_review()
        self.assertEqual(self.finish(review['token'], 1), {'cancelled': True})
        self.assertEqual(self.manager.list(), [])
        self.assertEqual(staging_directories(self.manager), [])
        with self.assertRaisesRegex(ValueError, 'expired'):
            self.finish(review['token'], True)

    def test_a_new_import_replaces_the_previous_session_and_its_staging(self):
        first = self.import_review()
        second = self.import_review()
        self.assertEqual(len(staging_directories(self.manager)), 1)
        with self.assertRaisesRegex(ValueError, 'expired'):
            self.finish(first['token'], True)
        self.assertTrue(self.finish(second['token'], True)['enabled'])
        self.assertEqual(staging_directories(self.manager), [])

    def test_a_failed_publish_consumes_the_token_and_cleans_up_staging(self):
        review = self.import_review()
        self.manager.state_path.write_text('{broken')
        with self.assertLogs('blackrose.skills', level='WARNING'):
            with self.assertRaisesRegex(ValueError, 'Back up this file'):
                self.finish(review['token'], True)
        self.assertEqual(self.manager.list(), [])
        self.assertEqual(staging_directories(self.manager), [])
        with self.assertRaisesRegex(ValueError, 'expired'):
            self.finish(review['token'], True)


if __name__ == '__main__':
    unittest.main()
