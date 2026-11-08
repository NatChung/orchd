"""Synthetic regressions for fixed origins, legacy queues and stopped-run notices."""
from unittest.mock import patch
import unittest

from orchd import core, entry, interface, store, workflow_runs as runs
from tests import test_workflow_runs as fixtures


QUESTION = "【給 Operator 的摘要】\nChoose the approved fixture scope.\n---詳細---\nMay this synthetic task continue?"


class WorkflowRunRouteTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.WorkflowRunTests('test_memory_database_never_starts_a_process')
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        entry.bind(self.f.con, self.f.rt, 'example-orch')

    def author(self):
        run_id = self.f.start()
        task_id = self.f.tick(run_id)['attempts'][0]['task_id']
        return run_id, task_id

    def desktop_rows(self):
        return self.f.con.execute("SELECT * FROM entry_messages WHERE direction='out'").fetchall()

    def origin(self):
        return self.f.con.execute('SELECT * FROM interface_requests WHERE id=?', (self.f.request_id,)).fetchone()

    def legacy_pending(self):
        run_id, task_id = self.author()
        core.ask(self.f.con, self.f.rt, task_id, QUESTION)
        # Model an already persisted row created by the pre-guard entry API.
        with patch.object(runs, 'guard_desktop_route', return_value=None), \
             patch.object(entry, '_desktop_route_error', return_value=None, create=True):
            entry.ask_nat(self.f.con, self.f.rt, 'example-orch', task_id=task_id, quote_worker_question=True)
        row = self.f.con.execute('SELECT * FROM entry_messages WHERE task_id=?', (task_id,)).fetchone()
        self.assertEqual(row['delivery'], 'pending')
        self.assertIsNone(row['recipient'])
        return run_id, task_id, row['id']

    def test_run_question_and_unscoped_send_never_create_desktop_rows(self):
        run_id, task_id = self.author()
        core.ask(self.f.con, self.f.rt, task_id, QUESTION)
        with self.assertRaisesRegex(ValueError, 'origin|Desktop'):
            entry.ask_nat(self.f.con, self.f.rt, 'example-orch', task_id=task_id, quote_worker_question=True)
        with self.assertRaisesRegex(ValueError, 'origin|Desktop'):
            entry.send_to_nat(self.f.con, self.f.rt, 'example-orch', QUESTION)
        self.assertEqual(self.desktop_rows(), [])
        state = self.f.tick(run_id)
        row = self.origin()
        self.assertEqual(state['state'], 'awaiting_input')
        self.assertEqual(row['conversation'], 'example-chat')
        self.assertIn(QUESTION, row['interim'])
        self.assertIsNone(row['reply'])
        self.assertEqual(store.get_task(self.f.con, task_id)['status'], 'question')

    def test_used_origin_interim_records_delivery_pending_without_falling_back_to_desktop(self):
        run_id, task_id = self.author()
        interface.reply(self.f.con, 'example-orch', request_id=self.f.request_id,
                        text='Earlier synthetic progress', kind='interim')
        core.ask(self.f.con, self.f.rt, task_id, QUESTION)
        state = self.f.tick(run_id)
        self.assertEqual(state['state'], 'awaiting_input')
        pending = [e for e in runs.events(self.f.con, run_id) if e['kind'] == 'origin_delivery_pending']
        self.assertEqual(len(pending), 1)
        self.assertEqual(self.origin()['interim'], 'Earlier synthetic progress')
        self.assertIsNone(self.origin()['reply'])
        self.assertEqual(self.desktop_rows(), [])
        self.f.tick(run_id, 2)
        self.assertEqual(len([e for e in runs.events(self.f.con, run_id) if e['kind'] == 'origin_delivery_pending']), 1)

    def test_explicit_unrelated_task_keeps_the_legacy_desktop_question_route(self):
        _, run_task = self.author()  # Another run does not capture an unrelated task.
        task = core.dispatch(self.f.con, self.f.rt, orch_thread='example-orch', repo='delivery-service',
                             title='Unrelated fixture', instructions='Read this unrelated fixture only',
                             done_when='Report its result', model='sonnet', model_reason='Synthetic fixture', task_type='ops')
        core.ask(self.f.con, self.f.rt, task['id'], QUESTION + '\nUnrelated text mentions task ' + run_task)
        entry.ask_nat(self.f.con, self.f.rt, 'example-orch', task_id=task['id'], quote_worker_question=True)
        rows = self.desktop_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['task_id'], task['id'])
        self.assertEqual(rows[0]['entry_id'], 'desktop')
        self.assertIsNone(self.origin()['reply'])

    def test_without_an_external_run_plain_desktop_messages_still_work(self):
        entry.send_to_nat(self.f.con, self.f.rt, 'example-orch', 'Unrelated local Operator message')
        rows = self.desktop_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['body'], 'Unrelated local Operator message')

    def test_needs_recovery_records_one_honest_final_on_the_original_request(self):
        self.f.rt.start_error = True
        run_id = self.f.start()
        state = self.f.tick(run_id)
        first = self.origin()
        self.assertEqual(state['state'], 'needs_recovery')
        self.assertEqual(first['conversation'], 'example-chat')
        self.assertIn(run_id, first['reply'])
        self.assertIn('needs_recovery', first['reply'])
        self.assertIn('not completed', first['reply'])
        self.assertNotIn('finished its declared steps', first['reply'])
        self.assertIsNone(first['ack_at'])  # Recorded final is not an adapter delivery/read receipt.
        self.f.tick(run_id, 3)
        self.assertEqual(self.origin()['reply'], first['reply'])
        notices = [e for e in runs.events(self.f.con, run_id) if e['kind'] == 'origin_notification:needs_recovery']
        self.assertEqual(len(notices), 1)
        self.assertEqual(self.desktop_rows(), [])
        self.assertEqual(len(self.f.rt.created), 1)

    def test_resend_cannot_reactivate_a_legacy_run_question_on_desktop(self):
        _, _, message_id = self.legacy_pending()
        self.f.con.execute("UPDATE entry_messages SET delivery='failed' WHERE id=?", (message_id,))
        with self.assertRaisesRegex(ValueError, 'origin|Desktop'):
            entry.resend_to_nat(self.f.con, self.f.rt, 'example-orch', message_id)
        row = self.f.con.execute('SELECT delivery,recipient FROM entry_messages WHERE id=?', (message_id,)).fetchone()
        self.assertEqual(row['delivery'], 'failed')
        self.assertIsNone(row['recipient'])

    def test_automatic_delivery_cannot_drain_a_legacy_run_question_to_a_different_chat(self):
        _, _, message_id = self.legacy_pending()
        entry.touch(self.f.con, 'desktop', 'different-desktop-chat')
        try:
            entry.deliver_outbound(self.f.con, self.f.rt, 'desktop')
        except ValueError:
            pass  # Refusing the queued wrong-origin row also prevents the leak.
        row = self.f.con.execute('SELECT delivery,recipient,body FROM entry_messages WHERE id=?', (message_id,)).fetchone()
        self.assertNotEqual(row['delivery'], 'delivered')
        self.assertIsNone(row['recipient'])
        self.assertIn(QUESTION, row['body'])  # Historical content stays available for recovery.

    def test_status_cannot_return_a_legacy_run_question_as_desktop_content(self):
        _, _, message_id = self.legacy_pending()
        try:
            result = entry.status(self.f.con, self.f.rt, 'desktop', 'different-desktop-chat')
        except ValueError:
            result = None
        if result is not None:
            self.assertNotIn(message_id, [row['message_id'] for row in result['from_orch']])
            self.assertNotEqual((result.get('current_question') or {}).get('message_id'), message_id)
        row = self.f.con.execute('SELECT delivery,recipient FROM entry_messages WHERE id=?', (message_id,)).fetchone()
        self.assertNotEqual(row['delivery'], 'delivered')
        self.assertIsNone(row['recipient'])

    def test_quarantine_preserves_history_and_does_not_block_a_legal_queue_item(self):
        _, _, message_id = self.legacy_pending()
        before = self.f.con.execute('SELECT body,body_sha256,created_at FROM entry_messages WHERE id=?', (message_id,)).fetchone()
        task = core.dispatch(self.f.con, self.f.rt, orch_thread='example-orch', repo='delivery-service',
                             title='Other fixture', instructions='Only the unrelated fixture', done_when='Report',
                             model='sonnet', model_reason='Synthetic case', task_type='ops')
        core.ask(self.f.con, self.f.rt, task['id'], QUESTION)
        entry.ask_nat(self.f.con, self.f.rt, 'example-orch', task_id=task['id'], quote_worker_question=True)
        entry.touch(self.f.con, 'desktop', 'legitimate-desktop-chat')
        entry.deliver_outbound(self.f.con, self.f.rt, 'desktop')
        wrong = self.f.con.execute('SELECT * FROM entry_messages WHERE id=?', (message_id,)).fetchone()
        self.assertEqual(wrong['question_state'], 'cancelled')
        self.assertEqual(wrong['delivery'], 'cancelled')
        self.assertEqual((wrong['body'], wrong['body_sha256'], wrong['created_at']), tuple(before))
        history = self.f.con.execute('SELECT to_state,reason FROM entry_question_history WHERE question_id=? ORDER BY id DESC LIMIT 1', (message_id,)).fetchone()
        self.assertEqual(history['to_state'], 'cancelled')
        self.assertIn('request', history['reason'])
        legal = self.f.con.execute('SELECT delivery,recipient FROM entry_messages WHERE task_id=?', (task['id'],)).fetchone()
        self.assertEqual(legal['delivery'], 'delivered')
        self.assertEqual(legal['recipient'], 'legitimate-desktop-chat')

    def test_stored_worker_message_link_is_used_when_legacy_task_id_is_absent(self):
        _, _, message_id = self.legacy_pending()
        self.f.con.execute('UPDATE entry_messages SET task_id=NULL WHERE id=?', (message_id,))
        entry.deliver_outbound(self.f.con, self.f.rt, 'desktop')
        row = self.f.con.execute('SELECT question_state,delivery FROM entry_messages WHERE id=?', (message_id,)).fetchone()
        self.assertEqual((row['question_state'], row['delivery']), ('cancelled', 'cancelled'))

    def test_delivered_legacy_question_cannot_take_a_new_desktop_answer(self):
        _, _, message_id = self.legacy_pending()
        self.f.con.execute("UPDATE entry_messages SET delivery='delivered',recipient='old-desktop-chat' WHERE id=?", (message_id,))
        row = self.f.con.execute('SELECT version FROM entry_messages WHERE id=?', (message_id,)).fetchone()
        with self.assertRaisesRegex(ValueError, 'request|origin'):
            entry.submit_answer(self.f.con, self.f.rt, 'desktop', message_id, row['version'], 'current',
                                'Synthetic approval', 'legacy-answer', orch_id='example-orch')
        self.assertEqual(self.f.con.execute("SELECT COUNT(*) FROM entry_messages WHERE kind='reply' AND reply_to=?", (message_id,)).fetchone()[0], 0)
        # Quarantine preserves the old delivery receipt instead of inventing non-delivery.
        entry.deliver_outbound(self.f.con, self.f.rt, 'desktop')
        old = self.f.con.execute('SELECT question_state,delivery,recipient FROM entry_messages WHERE id=?', (message_id,)).fetchone()
        self.assertEqual((old['question_state'], old['delivery'], old['recipient']), ('cancelled', 'delivered', 'old-desktop-chat'))

    def test_previously_saved_wrong_origin_answer_cannot_be_forwarded_to_a_worker(self):
        _, task_id, message_id = self.legacy_pending()
        self.f.con.execute("UPDATE entry_messages SET delivery='delivered',recipient='old-desktop-chat' WHERE id=?", (message_id,))
        version = self.f.con.execute('SELECT version FROM entry_messages WHERE id=?', (message_id,)).fetchone()[0]
        with patch.object(entry, '_desktop_route_error', return_value=None):
            saved = entry.submit_answer(self.f.con, self.f.rt, 'desktop', message_id, version, 'current',
                                        'Synthetic old approval', 'pre-guard-answer', orch_id='example-orch')
        reply = self.f.con.execute("SELECT id FROM entry_messages WHERE kind='reply' AND reply_to=?", (message_id,)).fetchone()[0]
        with self.assertRaisesRegex(ValueError, 'request|origin'):
            entry.reply_text(self.f.con, 'example-orch', reply, task_id)


if __name__ == '__main__':
    unittest.main()
