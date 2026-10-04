"""#71 uses only fake runtime operations and temporary SQLite databases."""
import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchd import cli, inventory, mcp_server, store
from tests.test_orchd import FakeRuntime

LIVE = {"pid": 4242, "status": "idle", "sessionId": "session"}


class LifecycleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.con = store.connect(Path(self.tmp.name) / 'orchd.db')
        self.addCleanup(self.con.close)
        self.rt = FakeRuntime()
        self.rt.jobs = {}

    def orch(self, id='orch', *, stopped=False, session='session', kind='claude'):
        store.register_orch(self.con, id, kind, socket=f'/tmp/{id}.sock',
                            session_id=session, job_id=id+'job', model='opus')
        if stopped:
            store.stop_orch(self.con, id)
        self.rt.dead_sockets = {f'/tmp/{id}.sock', *getattr(self.rt, 'dead_sockets', ())}
        return store.get_orch(self.con, id)

    def probe(self, now=10000):
        return inventory.list_orchs(self.con, self.rt, observe=True, now=now)

    def task(self, owner='orch', status='closed'):
        store.create_task(self.con, id='task', repo='demo', repo_path='/demo', title='T',
                          instructions='I', done_when='D', orch_thread=owner, codex_bin='codex', status=status)

    def test_states_default_reminder_and_all(self):
        self.orch('live')
        self.rt.jobs['livejob'] = LIVE
        self.rt.dead_sockets.remove('/tmp/live.sock')
        self.orch('idle')
        self.orch('dead', stopped=True)
        self.orch('empty', stopped=True)
        self.orch('codex', kind='codex')
        self.task('dead', 'done')
        report = self.probe()
        self.assertEqual({r['orch_id']: r['health'] for r in report['orchs']},
                         dict(live='alive', idle='idle', dead='dead', empty='dead', codex='unknown'))
        text = inventory.render(report)
        self.assertIn('live  claude  alive', text)
        self.assertIn('idle  claude  idle', text)
        self.assertIn('1 dead Orch(s)', text)
        self.assertNotIn('dead  claude', text)
        self.assertNotIn('empty  claude', text)
        self.assertNotIn('codex  codex', text)
        self.assertIn('codex  codex  unknown', inventory.render(report, all=True))

    def test_socket_and_partial_process_evidence_stay_honest(self):
        self.orch()
        for entry in ({"status": "idle"}, {"pid": 42}, {"pid": True, "status": "idle"},
                      {"pid": 0, "status": "idle"}, {"pid": 42, "status": "future"}):
            self.rt.jobs = {'orchjob': entry}
            self.assertEqual(self.probe()['orchs'][0]['health'], 'unknown')
        self.rt.jobs = {'orchjob': LIVE}
        self.assertEqual(self.probe()['orchs'][0]['health'], 'idle')  # live entry, retired socket
        store.stop_orch(self.con, 'orch')
        self.assertEqual(self.probe()['orchs'][0]['health'], 'unknown')  # conflicting process/socket
        self.rt.jobs = {}
        self.rt.dead_sockets.clear()
        self.assertEqual(self.probe()['orchs'][0]['health_reason'], 'socket_job_conflict')
        with patch.object(self.rt, 'socket_listening', return_value=None):
            self.assertEqual(self.probe()['orchs'][0]['health_reason'], 'socket_invalid')

    def test_archive_two_dead_at_least_one_hour_restore_and_revival(self):
        self.orch(stopped=True)
        self.task()
        store.add_message(self.con, 'task', 'usage', 'history')
        stopped = store.get_orch(self.con, 'orch')['stopped_at']
        self.assertFalse(self.probe(10000)['orchs'][0]['archived'])
        self.assertFalse(self.probe(13599)['orchs'][0]['archived'])
        self.assertTrue(self.probe(13600)['orchs'][0]['archived'])
        self.assertIn('archived', inventory.render(self.probe(13601), all=True))
        self.assertEqual(self.con.execute('SELECT COUNT(*) FROM messages').fetchone()[0], 1)
        self.assertEqual(self.con.execute('SELECT COUNT(*) FROM tasks').fetchone()[0], 1)
        self.assertEqual(store.get_orch(self.con, 'orch')['stopped_at'], stopped)
        inventory.restore(self.con, 'orch')
        row = store.get_orch(self.con, 'orch')
        self.assertIsNone(row['archived_at'])
        self.assertIsNone(row['first_seen_dead'])
        self.probe(20000)
        self.probe(23600)
        self.rt.jobs['orchjob'] = LIVE
        self.rt.dead_sockets.clear()
        row = self.probe(24000)['orchs'][0]
        self.assertEqual(row['health'], 'alive')
        self.assertFalse(row['archived'])
        with self.assertRaisesRegex(ValueError, 'unknown orch'):
            inventory.restore(self.con, 'missing')

    def test_unknown_query_invalid_and_socket_failure_never_archive(self):
        self.orch(stopped=True)
        self.probe(10000)
        for jobs in (None, [], {'bad': None}):
            self.rt.jobs = jobs
            self.assertFalse(self.probe(20000)['orchs'][0]['archived'])
        with patch.object(self.rt, 'live_jobs', side_effect=OSError('private')):
            self.assertEqual(self.probe()['orchs'][0]['health'], 'unknown')
        self.rt.jobs = {}
        self.assertFalse(self.probe(30000)['orchs'][0]['archived'])
        with patch.object(self.rt, 'socket_listening', side_effect=OSError('private')):
            self.assertEqual(self.probe(40000)['orchs'][0]['health'], 'unknown')
        self.assertIsNone(store.get_orch(self.con, 'orch')['first_seen_dead'])

    def test_codex_never_gets_death_observations(self):
        self.orch(kind='codex', stopped=True)
        self.probe(10000)
        row = self.probe(20000)['orchs'][0]
        self.assertEqual(row['health'], 'unknown')
        self.assertIsNone(row['first_seen_dead'])
        self.assertIsNone(row['archived_at'])

    def test_obligations_protect_closed_tasks_unread_failed_and_questions(self):
        for kind, read, error in (('report', False, None), ('progress', True, 'OSError: private'),
                                  ('question', True, None)):
            with self.subTest(kind=kind):
                self.con.execute('DELETE FROM messages')
                self.con.execute('DELETE FROM tasks')
                self.orch(stopped=True)
                self.task()
                mid = store.add_message(self.con, 'task', kind, 'private')
                self.con.execute('UPDATE messages SET read_at=?,wake_error=? WHERE id=?',
                                 (10 if read else None, error, mid))
                self.probe(10000)
                row = self.probe(20000)['orchs'][0]
                self.assertFalse(row['archived'])
                self.assertTrue(row['has_obligations'])
        store.add_message(self.con, 'task', 'answer', 'yes')
        self.assertTrue(self.probe(24000)['orchs'][0]['archived'])

    def test_failed_old_owner_notice_after_adopt_is_protected(self):
        self.orch('old', stopped=True)
        self.task('new')
        mid = store.add_message(self.con, 'task', 'adopt', 'private')
        self.con.execute('UPDATE messages SET notice_recipient=?,notice_error=? WHERE id=?',
                         ('old', 'OSError: private', mid))
        self.probe(10000)
        row = self.probe(20000)['orchs'][0]
        self.assertFalse(row['archived'])
        self.assertTrue(row['has_obligations'])

    def test_bindings_and_entry_messages_protect(self):
        self.orch(stopped=True)
        self.con.execute("INSERT INTO entries(id,orch_id,bound_at) VALUES('desktop','orch',1)")
        self.probe(10000)
        self.assertFalse(self.probe(20000)['orchs'][0]['archived'])
        self.con.execute("UPDATE entries SET orch_id='new'")
        for delivery, read, state in (('failed', 1, None), ('delivered', None, None),
                                      ('delivered', 1, 'current'), ('held', 1, 'queued')):
            with self.subTest(delivery=delivery, state=state):
                self.con.execute('DELETE FROM entry_messages')
                self.con.execute('INSERT INTO entry_messages(entry_id,orch_id,direction,kind,body,'
                                 'body_bytes,body_sha256,delivery,read_at,question_state,created_at) '
                                 "VALUES('desktop','orch','out','question','private',7,'hash',?,?,?,1)",
                                 (delivery, read, state))
                self.assertFalse(self.probe(24000)['orchs'][0]['archived'])

    def test_socket_and_runtime_probes_allow_another_writer(self):
        self.orch(stopped=True)
        peer = store.connect(Path(self.tmp.name) / 'orchd.db')
        self.addCleanup(peer.close)
        peer.execute('PRAGMA busy_timeout=100')

        def runtime_probe():
            self.assertFalse(self.con.in_transaction)
            store.register_orch(peer, 'peer', 'codex')
            return {}

        def slow_socket_probe(socket):
            # While this probe is still running, another connection can write.
            self.assertFalse(self.con.in_transaction)
            peer.execute("UPDATE orchs SET model='new' WHERE id='peer'")
            return False

        with patch.object(self.rt, 'live_jobs', side_effect=runtime_probe), patch.object(
                self.rt, 'socket_listening', side_effect=slow_socket_probe):
            report = self.probe()
        self.assertEqual(store.get_orch(peer, 'peer')['model'], 'new')
        self.assertEqual(store.get_orch(self.con, 'orch')['first_seen_dead'], 10000)
        self.assertEqual({r['orch_id']: r['health_reason'] for r in report['orchs']}['peer'],
                         'registry_changed')

    def test_changed_registry_does_not_apply_stale_death_probe(self):
        self.orch(stopped=True)
        self.probe(10000)
        peer = store.connect(Path(self.tmp.name) / 'orchd.db')
        self.addCleanup(peer.close)
        peer.execute('PRAGMA busy_timeout=100')
        for change in ('restore', 'identity'):
            with self.subTest(change=change):
                def socket_probe(socket):
                    if change == 'restore':
                        inventory.restore(peer, 'orch')
                    else:
                        peer.execute("UPDATE orchs SET job_id='replacement' WHERE id='orch'")
                    return False

                with patch.object(self.rt, 'socket_listening', side_effect=socket_probe):
                    row = self.probe(20000)['orchs'][0]
                self.assertEqual(row['health_reason'], 'registry_changed')
                self.assertFalse(row['archived'])
                self.assertIsNone(store.get_orch(self.con, 'orch')['first_seen_dead'])
                self.assertIsNone(store.get_orch(self.con, 'orch')['last_verified_dead'])

    def test_cli_json_matches_complete_mcp_text(self):
        self.orch('dead', stopped=True)
        self.orch('codex', kind='codex')
        self.con.execute("UPDATE orchs SET first_seen_dead=1,archived_at=1 WHERE id='dead'")
        self.rt.jobs['unidentified'] = LIVE
        with patch.object(inventory.time, 'time', return_value=10000):
            reply = mcp_server.handle(dict(id=1, method='tools/call', params=dict(
                name='list_orchs', arguments={})), self.con, self.rt)['result']
            with patch.object(cli.store, 'home', return_value=Path(self.tmp.name)), patch.object(
                    cli, 'Runtime', return_value=self.rt), contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(cli.main(['orchs', '--json']), 0)
        self.assertEqual(output.getvalue().rstrip('\n'), reply['content'][0]['text'])
        report = json.loads(output.getvalue())
        self.assertEqual(report, reply['structuredContent'])
        self.assertEqual(report['unidentified_claude_sessions'], [dict(job_id='unidentified', health='alive')])
        self.assertEqual(set(report['counts']), {'registered', 'alive', 'idle', 'dead', 'unknown'})
        rows = {row['orch_id']: row for row in report['orchs']}
        self.assertEqual(set(rows), {'dead', 'codex'})
        self.assertTrue(rows['dead']['archived'])
        self.assertEqual(set(rows['dead']), {'orch_id', 'kind', 'created_at', 'stopped_at', 'health',
                         'health_reason', 'open_task_count', 'archived', 'archived_at', 'first_seen_dead',
                         'last_verified_dead', 'has_obligations', 'notification_count',
                         'entry_pending_count', 'binding_count'})

    def test_mcp_text_keeps_complete_json_including_dead_unknown_and_archived(self):
        self.orch('dead', stopped=True)
        self.orch('codex', kind='codex')
        self.con.execute("UPDATE orchs SET archived_at=1 WHERE id='dead'")
        reply = mcp_server.handle(dict(id=1, method='tools/call', params=dict(
            name='list_orchs', arguments={})), self.con, self.rt)['result']
        report = json.loads(reply['content'][0]['text'])
        self.assertEqual(report, reply['structuredContent'])
        self.assertTrue({'orchs', 'counts', 'unidentified_claude_sessions'} <= report.keys())
        rows = {r['orch_id']: r for r in report['orchs']}
        self.assertEqual(set(rows), {'dead', 'codex'})
        self.assertEqual(rows['dead']['health'], 'dead')
        self.assertTrue(rows['dead']['archived'])
        self.assertEqual(rows['codex']['health'], 'unknown')
        for row in rows.values():
            self.assertTrue({'orch_id', 'kind', 'created_at', 'stopped_at', 'health',
                             'health_reason', 'open_task_count'} <= row.keys())
        self.assertEqual(store.list_orchs(self.con)[0]['id'], 'dead')

    def live(self):
        self.orch()
        self.rt.jobs['orchjob'] = LIVE
        self.rt.dead_sockets.clear()

    def test_attach_and_viewer_revalidate_no_new_session_or_binding(self):
        self.live()
        inventory.attach(self.con, self.rt, 'orch')
        self.assertEqual(self.rt.attached, 'orchjob')
        inventory.attach(self.con, self.rt, 'orch', viewer=True)
        self.assertEqual(self.rt.viewed, ['orchjob'])
        self.assertFalse(getattr(self.rt, 'resumed_orchs', []))
        self.assertEqual(self.rt.stopped, [])
        self.assertEqual(self.con.execute('SELECT COUNT(*) FROM entries').fetchone()[0], 0)

    def test_idle_attach_resumes_same_conversation(self):
        self.orch()
        self.rt.jobs['orchjob'] = {}  # retained historical job, no process
        with contextlib.redirect_stdout(io.StringIO()) as output:
            inventory.attach(self.con, self.rt, 'orch')
        self.assertIn('Resuming Orch orch', output.getvalue())
        self.assertEqual(self.rt.resumed_orchs, [('orch', 'opus', 'session')])
        self.assertEqual(self.rt.attached, 'resumed1')
        self.assertEqual(len(store.list_orchs(self.con)), 1)
        self.assertEqual(self.rt.stopped, [])

    def test_attach_dead_unknown_missing_codex_and_changed_identity(self):
        self.orch(stopped=True)
        with self.assertRaisesRegex(ValueError, 'dead'):
            inventory.attach(self.con, self.rt, 'orch')
        with self.assertRaisesRegex(ValueError, 'registered Claude'):
            inventory.attach(self.con, self.rt, 'missing')
        self.orch('codex', kind='codex')
        with self.assertRaisesRegex(ValueError, 'registered Claude'):
            inventory.attach(self.con, self.rt, 'codex')
        self.live()
        with patch.object(self.rt, 'live_jobs', return_value=None), self.assertRaisesRegex(ValueError, 'unknown'):
            inventory.attach(self.con, self.rt, 'orch')
        self.rt.jobs['orchjob'] = {**LIVE, 'sessionId': 'other'}
        with self.assertRaisesRegex(ValueError, 'session_mismatch'):
            inventory.attach(self.con, self.rt, 'orch')
        self.assertFalse(hasattr(self.rt, 'attached'))

    def test_attach_target_dies_or_identity_changes_between_probes(self):
        self.live()
        # Explicitly stopped => cannot resume when the selected process disappears.
        store.stop_orch(self.con, 'orch')
        with patch.object(self.rt, 'live_jobs', side_effect=[{'orchjob': LIVE}, {}]), \
                self.assertRaisesRegex(ValueError, 'cannot attach'):
            inventory.attach(self.con, self.rt, 'orch')
        def mutate():
            self.con.execute("UPDATE orchs SET job_id='different' WHERE id='orch'")
            return {'orchjob': LIVE}
        with patch.object(self.rt, 'live_jobs', side_effect=mutate), \
                self.assertRaisesRegex(ValueError, 'changed during selection'):
            inventory.attach(self.con, self.rt, 'orch')
        self.assertFalse(hasattr(self.rt, 'attached'))
        self.assertEqual(self.rt.stopped, [])

    def test_cli_all_restore_and_attach_with_fake_runtime(self):
        self.live()
        self.con.execute("UPDATE orchs SET archived_at=1 WHERE id='orch'")
        connection_factory = store.connect
        for args in (['orchs', '--all'], ['orchs', '--restore', 'orch'], ['attach', 'orch', '--viewer']):
            with patch.object(store, 'home', return_value=Path(self.tmp.name)), \
                    patch.object(store, 'connect', side_effect=lambda: connection_factory(Path(self.tmp.name) / 'orchd.db')), \
                    patch.object(cli, 'Runtime', return_value=self.rt), \
                    contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(cli.main(args), 0)
            if args[0] == 'attach':
                self.assertEqual(self.rt.viewed, ['orchjob'])
            elif '--restore' in args:
                self.assertIn('Restored orch', out.getvalue())
            else:
                self.assertIn('orch  claude  alive', out.getvalue())

    def test_legacy_database_migrates_preserving_rows(self):
        path = Path(self.tmp.name) / 'old.db'
        old = sqlite3.connect(path)
        old.execute('CREATE TABLE orchs(id TEXT PRIMARY KEY,kind TEXT,model TEXT,socket TEXT,'
                    'session_id TEXT,job_id TEXT,created_at REAL,stopped_at REAL)')
        old.execute("INSERT INTO orchs(id,kind,created_at) VALUES('old','codex',1)")
        old.commit()
        old.close()
        con = store.connect(path)
        try:
            row = store.get_orch(con, 'old')
            self.assertEqual(row['kind'], 'codex')
            self.assertIsNone(row['first_seen_dead'])
            self.assertIsNone(row['archived_at'])
        finally:
            con.close()
