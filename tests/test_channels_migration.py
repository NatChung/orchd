import sqlite3
import tempfile
import unittest
from pathlib import Path
from orchd import store
from orchd.channels import repository


class MigrationTest(unittest.TestCase):
    def test_old_database_copy_and_dedicated_foreign_keys(self):
        with tempfile.TemporaryDirectory() as d:
            old = Path(d) / 'old.db'
            shared = store.connect(old)
            self.assertEqual(shared.execute('PRAGMA foreign_keys').fetchone()[0], 0)
            shared.execute("INSERT INTO intake_content(receipt_id,status,text,attachments,created_at) VALUES('legacy-receipt','complete','synthetic body','[]',0)")
            shared.execute("INSERT INTO intake_events(receipt_id,version,provider,account,company,event_id,source_ref,source_time,received_at,producer,summary,content_sha256,state,acked_at,acked_by) VALUES('legacy-receipt',2,'slack','a','t','event','room',1,2,'producer','summary','hash','acked',3,'orch')")
            shared.execute("INSERT INTO intake_drafts(draft_id,version,receipt_id,provider,sender,target,source_ref,level,summary,body,body_sha256,expires_at,state,created_by,created_at,updated_at) VALUES('draft',1,'legacy-receipt','slack','a','target','room','L2','summary','synthetic reply','hash',100,'approved','orch',1,2)")
            shared.execute("INSERT INTO intake_approvals VALUES('approval','draft',1,'hash','a','target','room',100,'key','operator',3)")
            before = tuple(shared.execute("SELECT state,acked_at,acked_by FROM intake_events WHERE receipt_id='legacy-receipt'").fetchone())
            approval_before = tuple(shared.execute("SELECT * FROM intake_approvals WHERE approval_id='approval'").fetchone())
            shared.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            shared.close()
            original = old.read_bytes()
            copy = Path(d) / 'copy.db'
            copy.write_bytes(original)
            channel = repository.connect(copy)
            self.assertEqual(channel.execute('PRAGMA foreign_keys').fetchone()[0], 1)
            self.assertEqual(channel.execute("SELECT text FROM intake_content WHERE receipt_id='legacy-receipt'").fetchone()[0], 'synthetic body')
            self.assertEqual(tuple(channel.execute("SELECT state,acked_at,acked_by FROM intake_events WHERE receipt_id='legacy-receipt'").fetchone()), before)
            self.assertEqual(tuple(channel.execute("SELECT * FROM intake_approvals WHERE approval_id='approval'").fetchone()), approval_before)
            with self.assertRaises(sqlite3.IntegrityError):
                channel.execute("INSERT INTO channel_source_leases VALUES('missing','holder',1,'2026')")
            channel.close()
            repository.connect(copy).close()
            self.assertEqual(old.read_bytes(), original)

    def test_existing_violation_does_not_change_legacy_connection(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'old.db'
            shared = store.connect(path)
            shared.execute("INSERT INTO goal_history(goal_id,actor,created_at,changes) VALUES('missing','synthetic',0,'{}')")
            self.assertEqual(shared.execute('PRAGMA foreign_keys').fetchone()[0], 0)
            channel = repository.connect(path)
            try:
                self.assertEqual(shared.execute('PRAGMA foreign_keys').fetchone()[0], 0)
                with self.assertRaises(sqlite3.IntegrityError):
                    with store.immediate(channel):
                        channel.execute("UPDATE goal_history SET goal_id='still-missing' WHERE goal_id='missing'")
                self.assertEqual(shared.execute('SELECT goal_id FROM goal_history').fetchone()[0], 'missing')
                shared.execute("UPDATE goal_history SET goal_id='legacy-still-works' WHERE goal_id='missing'")
            finally:
                channel.close()
                shared.close()

    def test_every_declared_foreign_key_rejects_a_missing_parent(self):
        with tempfile.TemporaryDirectory() as d:
            con=repository.connect(Path(d)/'db')
            try:
                statements=[
                    "INSERT INTO orchs(id,kind,created_at) VALUES('o','codex',0)",
                    "INSERT INTO entries(id,orch_id,bound_at) VALUES('entry','o',0)",
                    "INSERT INTO entry_messages(id,entry_id,orch_id,direction,kind,body,body_bytes,body_sha256,delivery,created_at) VALUES(1,'entry','o','in','text','',0,'hash','pending',0)",
                    "INSERT INTO intake_content(receipt_id,status,attachments,created_at) VALUES('ir','unavailable','[]',0)",
                    "INSERT INTO intake_drafts(draft_id,version,provider,sender,target,source_ref,level,summary,body,body_sha256,expires_at,state,created_by,created_at,updated_at) VALUES('dr',1,'slack','s','t','c','L2','summary','body','hash',1,'pending_approval','orch',0,0)",
                    "INSERT INTO channel_sources(id,host_id,provider,tenant_ref,account_ref,adapter_id,manifest_hash,created_at) VALUES('s','h','slack','t','a','adapter','hash','now')",
                    "INSERT INTO channel_conversations(id,source_id,external_ref,kind,parent_id) VALUES('c','s','ref','channel',NULL)",
                    "UPDATE channel_conversations SET parent_id='c' WHERE id='c'",
                    "INSERT INTO channel_origins VALUES('origin','slack','t','ref','m')",
                    "INSERT INTO channel_messages(id,conversation_id,origin_key,provider_message_id,revision_key,direction,trust,sender_ref,source_time,received_at,content_hash,intake_receipt_id,draft_id,draft_version,entry_ref) VALUES('m','c','origin','m','r','in','third_party_data','sender','now','now','hash','ir','dr',1,1)",
                    "INSERT INTO channel_observations VALUES('s','event','origin','m','hash','now')",
                    "INSERT INTO channel_routes(id,version,scope_key,source_id,conversation_ref,owner_orch,responder_source_id,mode,authority_epoch,effective_at,manifest_hash) VALUES('r',1,'scope','s','ref','o','s','respond',1,'now','hash')",
                    "INSERT INTO channel_deliveries(id,kind,source_id,message_id,draft_id,draft_version,route_id,route_version,authority_epoch,target_snapshot,payload_hash,state,idempotency_key,created_at,updated_at) VALUES('d','outbound','s','m','dr',1,'r',1,1,'{}','hash','queued','key','now','now')",
                    "INSERT INTO channel_delivery_attempts(delivery_id,attempt_no,state,started_at) VALUES('d',1,'started','now')",
                    "INSERT INTO channel_legacy_refs VALUES('legacy','old','message','m')",
                    "INSERT INTO channel_checkpoints VALUES('s','scope','cursor',NULL,'hash','now')",
                    "INSERT INTO channel_sync_jobs(id,source_id,request_key,request_hash,requested_by,scope_json,state,created_at) VALUES('job','s','key','hash','actor','{}','pending','now')",
                    "INSERT INTO channel_attachments(id,message_id,provider_attachment_ref,status,updated_at) VALUES('att','m','ref','referenced','now')",
                    "INSERT INTO channel_source_leases VALUES('s','holder',1,'later')",
                    "INSERT INTO channel_processing_claims VALUES('origin','r','lane','o',1,'claimed','now')",
                    "INSERT INTO channel_loop_state(scope_key,correlation_id,window_start,reset_at,last_origin_key,updated_at) VALUES('scope','correlation','now','later','origin','now')",
                ]
                for sql in statements:
                    con.execute(sql)
                tables=['channel_conversations','channel_messages','channel_observations','channel_routes','channel_deliveries','channel_delivery_attempts','channel_checkpoints','channel_sync_jobs','channel_attachments','channel_source_leases','channel_processing_claims','channel_loop_state']
                exercised=0
                for table in tables:
                    groups={}
                    for fk in con.execute(f'PRAGMA foreign_key_list({table})'):
                        groups.setdefault(fk['id'], []).append(fk)
                    for group in groups.values():
                        field=group[0]['from']
                        missing=-999999 if field in ('entry_ref','draft_version','route_version') else 'missing-parent'
                        with self.subTest(table=table,field=field):
                            con.execute('SAVEPOINT test_fk')
                            try:
                                with self.assertRaisesRegex(sqlite3.IntegrityError,'FOREIGN KEY'):
                                    con.execute(f'UPDATE {table} SET {field}=?',(missing,))
                            finally:
                                con.execute('ROLLBACK TO test_fk')
                                con.execute('RELEASE test_fk')
                        exercised += 1
                self.assertGreaterEqual(exercised, 20)
                invalid_checks = [
                    ('channel_sources', 'enabled=2'), ('channel_sources', 'authority_epoch=0'),
                    ('channel_conversations', "kind='invalid'"),
                    ('channel_messages', "direction='invalid'"), ('channel_messages', "trust='invalid'"),
                    ('channel_messages', 'draft_id=NULL'),
                    ('channel_routes', 'version=0'), ('channel_routes', "mode='invalid'"),
                    ('channel_routes', 'authority_epoch=0'), ('channel_routes', 'active=2'),
                    ('channel_deliveries', "kind='invalid'"), ('channel_deliveries', "reply_kind='invalid'"),
                    ('channel_deliveries', 'authority_epoch=0'), ('channel_deliveries', "state='invalid'"),
                    ('channel_deliveries', 'draft_id=NULL'), ('channel_deliveries', 'route_id=NULL'),
                    ('channel_deliveries', 'draft_id=NULL,draft_version=NULL'),
                    ('channel_deliveries', 'route_id=NULL,route_version=NULL'),
                    ('channel_deliveries', "reply_kind='no_reply'"),
                    ('channel_delivery_attempts', 'attempt_no=0'), ('channel_delivery_attempts', "state='invalid'"),
                    ('channel_sync_jobs', "state='invalid'"), ('channel_attachments', 'byte_size=-1'),
                    ('channel_attachments', "status='invalid'"), ('channel_attachments', "status='available'"),
                    ('channel_attachments', "status='available',sha256='hash'"),
                    ('channel_attachments', "status='available',storage_key='key'"),
                    ('channel_source_leases', 'generation=0'), ('channel_processing_claims', 'authority_epoch=0'),
                    ('channel_processing_claims', "state='invalid'"), ('channel_loop_state', 'turns=-1'),
                    ('channel_loop_state', 'paused=2'),
                ]
                for table, assignment in invalid_checks:
                    with self.subTest(table=table, assignment=assignment):
                        con.execute('SAVEPOINT test_check')
                        try:
                            with self.assertRaisesRegex(sqlite3.IntegrityError, 'CHECK'):
                                con.execute(f'UPDATE {table} SET {assignment}')
                        finally:
                            con.execute('ROLLBACK TO test_check')
                            con.execute('RELEASE test_check')
                unique_cases = [
                    "INSERT INTO channel_sources SELECT 's2',host_id,provider,tenant_ref,account_ref,adapter_id,credential_refs_json,enabled,authority_epoch,manifest_hash,created_at FROM channel_sources",
                    "INSERT INTO channel_conversations SELECT 'c2',source_id,external_ref,kind,thread_key,parent_id FROM channel_conversations",
                    "INSERT INTO channel_origins SELECT 'other-origin',provider,tenant_ref,conversation_ref,provider_message_id FROM channel_origins",
                    "INSERT INTO channel_messages SELECT 'm2',conversation_id,origin_key,provider_message_id,revision_key,direction,trust,sender_ref,source_time,received_at,content_hash,provenance_json,NULL,draft_id,draft_version,entry_ref,deleted_at FROM channel_messages",
                    "INSERT INTO channel_messages SELECT 'm3',conversation_id,origin_key,provider_message_id,'other-revision',direction,trust,sender_ref,source_time,received_at,content_hash,provenance_json,intake_receipt_id,draft_id,draft_version,entry_ref,deleted_at FROM channel_messages",
                    "INSERT INTO channel_routes SELECT 'r2',version,scope_key,source_id,conversation_ref,sender_scope_json,project_ref,repo_ref,owner_orch,responder_source_id,mode,authority_epoch,active,effective_at,manifest_hash FROM channel_routes",
                    "INSERT INTO channel_deliveries SELECT 'd2',kind,source_id,message_id,draft_id,draft_version,reply_kind,route_id,route_version,authority_epoch,target_snapshot,payload_hash,state,idempotency_key,created_at,updated_at FROM channel_deliveries",
                    "INSERT INTO channel_sync_jobs SELECT 'job2',source_id,request_key,request_hash,requested_by,scope_json,state,created_at,completed_at,error_code FROM channel_sync_jobs",
                    "INSERT INTO channel_attachments SELECT 'att2',message_id,provider_attachment_ref,original_name,sha256,byte_size,status,storage_key,updated_at FROM channel_attachments",
                ]
                for sql in unique_cases:
                    with self.subTest(sql=sql):
                        with self.assertRaisesRegex(sqlite3.IntegrityError, 'UNIQUE'):
                            con.execute(sql)
                tables_with_primary_keys = [
                    'channel_sources', 'channel_conversations', 'channel_origins', 'channel_messages',
                    'channel_observations', 'channel_routes', 'channel_deliveries', 'channel_delivery_attempts',
                    'channel_legacy_refs', 'channel_checkpoints', 'channel_sync_jobs', 'channel_attachments',
                    'channel_source_leases', 'channel_processing_claims', 'channel_loop_state',
                ]
                for table in tables_with_primary_keys:
                    with self.subTest(table=table, constraint='primary key'):
                        with self.assertRaisesRegex(sqlite3.IntegrityError, 'UNIQUE'):
                            con.execute(f'INSERT INTO {table} SELECT * FROM {table}')
            finally:
                con.close()


    def test_incompatible_existing_table_is_rejected_without_version_record(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'db'
            con=store.connect(path)
            con.execute('CREATE TABLE channel_sources(id TEXT PRIMARY KEY)')
            con.close()
            with self.assertRaises((ValueError, sqlite3.Error)):
                repository.connect(path)
            check=sqlite3.connect(path)
            try:
                self.assertEqual([r[1] for r in check.execute('PRAGMA table_info(channel_sources)')], ['id'])
                self.assertIsNone(check.execute("SELECT name FROM sqlite_master WHERE name='orchd_channel_migrations'").fetchone())
            finally:
                check.close()
