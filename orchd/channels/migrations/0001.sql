PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS channel_sources (
  id TEXT PRIMARY KEY,
  host_id TEXT NOT NULL, provider TEXT NOT NULL,
  tenant_ref TEXT NOT NULL DEFAULT '', account_ref TEXT NOT NULL,
  adapter_id TEXT NOT NULL, credential_refs_json TEXT NOT NULL DEFAULT '{}',
  enabled INTEGER NOT NULL DEFAULT 0 CHECK(enabled IN (0,1)),
  authority_epoch INTEGER NOT NULL DEFAULT 1 CHECK(authority_epoch > 0),
  manifest_hash TEXT NOT NULL, created_at TEXT NOT NULL,
  UNIQUE(provider, tenant_ref, account_ref)
);
CREATE TABLE IF NOT EXISTS channel_conversations (
  id TEXT PRIMARY KEY, source_id TEXT NOT NULL REFERENCES channel_sources(id),
  external_ref TEXT NOT NULL, kind TEXT NOT NULL
    CHECK(kind IN ('channel','group','dm','email','peer')),
  thread_key TEXT NOT NULL DEFAULT '',
  parent_id TEXT REFERENCES channel_conversations(id),
  UNIQUE(source_id, external_ref, thread_key)
);
CREATE TABLE IF NOT EXISTS channel_origins (
  origin_key TEXT PRIMARY KEY,
  provider TEXT NOT NULL, tenant_ref TEXT NOT NULL DEFAULT '',
  conversation_ref TEXT NOT NULL, provider_message_id TEXT NOT NULL,
  UNIQUE(provider, tenant_ref, conversation_ref, provider_message_id)
);
CREATE TABLE IF NOT EXISTS channel_messages (
  id TEXT PRIMARY KEY,
  conversation_id TEXT NOT NULL REFERENCES channel_conversations(id),
  origin_key TEXT NOT NULL REFERENCES channel_origins(origin_key),
  provider_message_id TEXT NOT NULL, revision_key TEXT NOT NULL,
  direction TEXT NOT NULL CHECK(direction IN ('in','out')),
  trust TEXT NOT NULL CHECK(trust IN
    ('operator_command','third_party_data','orch_peer_data','internal_notice')),
  sender_ref TEXT NOT NULL, source_time TEXT NOT NULL, received_at TEXT NOT NULL,
  content_hash TEXT NOT NULL, provenance_json TEXT NOT NULL DEFAULT '{}',
  intake_receipt_id TEXT UNIQUE REFERENCES intake_content(receipt_id),
  draft_id TEXT, draft_version INTEGER,
  entry_ref INTEGER REFERENCES entry_messages(id),
  deleted_at TEXT,
  FOREIGN KEY(draft_id, draft_version) REFERENCES intake_drafts(draft_id, version),
  CHECK((draft_id IS NULL) = (draft_version IS NULL)),
  UNIQUE(origin_key, revision_key)
);
CREATE TABLE IF NOT EXISTS channel_observations (
  source_id TEXT NOT NULL REFERENCES channel_sources(id),
  transport_event_key TEXT NOT NULL,
  origin_key TEXT NOT NULL REFERENCES channel_origins(origin_key),
  message_id TEXT NOT NULL REFERENCES channel_messages(id),
  content_hash TEXT NOT NULL, observed_at TEXT NOT NULL,
  PRIMARY KEY(source_id, transport_event_key)
);
CREATE TABLE IF NOT EXISTS channel_routes (
  id TEXT NOT NULL, version INTEGER NOT NULL CHECK(version > 0),
  scope_key TEXT NOT NULL, source_id TEXT NOT NULL REFERENCES channel_sources(id),
  conversation_ref TEXT NOT NULL, sender_scope_json TEXT NOT NULL DEFAULT '[]',
  project_ref TEXT, repo_ref TEXT, owner_orch TEXT NOT NULL REFERENCES orchs(id),
  responder_source_id TEXT REFERENCES channel_sources(id),
  mode TEXT NOT NULL CHECK(mode IN ('receive_only','respond','observer','peer')),
  authority_epoch INTEGER NOT NULL CHECK(authority_epoch > 0),
  active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
  effective_at TEXT NOT NULL, manifest_hash TEXT NOT NULL,
  PRIMARY KEY(id, version)
);
CREATE UNIQUE INDEX IF NOT EXISTS channel_route_active_scope
  ON channel_routes(scope_key) WHERE active = 1;
CREATE TABLE IF NOT EXISTS channel_deliveries (
  id TEXT PRIMARY KEY, kind TEXT NOT NULL CHECK(kind IN ('wake','outbound','peer')),
  source_id TEXT NOT NULL REFERENCES channel_sources(id),
  message_id TEXT NOT NULL REFERENCES channel_messages(id),
  draft_id TEXT, draft_version INTEGER,
  reply_kind TEXT CHECK(reply_kind IN ('interim','final','no_reply')),
  route_id TEXT, route_version INTEGER,
  authority_epoch INTEGER NOT NULL CHECK(authority_epoch > 0),
  target_snapshot TEXT NOT NULL, payload_hash TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN
    ('queued','claimed','provider_accepted','delivered','failed','uncertain','cancelled')),
  idempotency_key TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  FOREIGN KEY(draft_id, draft_version) REFERENCES intake_drafts(draft_id, version),
  FOREIGN KEY(route_id, route_version) REFERENCES channel_routes(id, version),
  CHECK((draft_id IS NULL) = (draft_version IS NULL)),
  CHECK((route_id IS NULL) = (route_version IS NULL)),
  CHECK(kind <> 'outbound' OR (draft_id IS NOT NULL AND route_id IS NOT NULL)),
  CHECK(reply_kind IS NULL OR reply_kind <> 'no_reply' OR kind <> 'outbound')
);
CREATE INDEX IF NOT EXISTS channel_deliveries_pending
  ON channel_deliveries(source_id, state, created_at);
CREATE TABLE IF NOT EXISTS channel_delivery_attempts (
  delivery_id TEXT NOT NULL REFERENCES channel_deliveries(id),
  attempt_no INTEGER NOT NULL CHECK(attempt_no > 0),
  state TEXT NOT NULL CHECK(state IN ('started','provider_accepted','failed','uncertain','delivered')),
  started_at TEXT NOT NULL, finished_at TEXT,
  provider_receipt TEXT, error_code TEXT,
  PRIMARY KEY(delivery_id, attempt_no)
);
CREATE TABLE IF NOT EXISTS channel_legacy_refs (
  system TEXT NOT NULL, old_id TEXT NOT NULL,
  new_kind TEXT NOT NULL, new_id TEXT NOT NULL,
  PRIMARY KEY(system, old_id, new_kind)
);
CREATE TABLE IF NOT EXISTS channel_checkpoints (
  source_id TEXT NOT NULL REFERENCES channel_sources(id), scope TEXT NOT NULL,
  committed_cursor TEXT NOT NULL, window_end TEXT,
  receipt_set_hash TEXT NOT NULL, updated_at TEXT NOT NULL,
  PRIMARY KEY(source_id, scope)
);
CREATE TABLE IF NOT EXISTS channel_sync_jobs (
  id TEXT PRIMARY KEY, source_id TEXT NOT NULL REFERENCES channel_sources(id),
  request_key TEXT NOT NULL, request_hash TEXT NOT NULL,
  requested_by TEXT NOT NULL, scope_json TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('pending','running','succeeded','failed')),
  created_at TEXT NOT NULL, completed_at TEXT, error_code TEXT,
  UNIQUE(source_id, request_key)
);
CREATE TABLE IF NOT EXISTS channel_attachments (
  id TEXT PRIMARY KEY, message_id TEXT NOT NULL REFERENCES channel_messages(id),
  provider_attachment_ref TEXT NOT NULL, original_name TEXT,
  sha256 TEXT, byte_size INTEGER CHECK(byte_size >= 0),
  status TEXT NOT NULL CHECK(status IN ('referenced','queued','available','unavailable','deleted')),
  storage_key TEXT, updated_at TEXT NOT NULL,
  UNIQUE(message_id, provider_attachment_ref),
  CHECK(status <> 'available' OR (sha256 IS NOT NULL AND storage_key IS NOT NULL))
);
CREATE TABLE IF NOT EXISTS channel_source_leases (
  source_id TEXT PRIMARY KEY REFERENCES channel_sources(id),
  holder_id TEXT NOT NULL, generation INTEGER NOT NULL CHECK(generation > 0),
  lease_until TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS channel_processing_claims (
  origin_key TEXT NOT NULL REFERENCES channel_origins(origin_key),
  revision_key TEXT NOT NULL, lane TEXT NOT NULL,
  owner_orch TEXT NOT NULL REFERENCES orchs(id),
  authority_epoch INTEGER NOT NULL CHECK(authority_epoch > 0),
  state TEXT NOT NULL CHECK(state IN ('claimed','completed','uncertain')),
  created_at TEXT NOT NULL,
  PRIMARY KEY(origin_key, revision_key, lane)
);
CREATE TABLE IF NOT EXISTS channel_loop_state (
  scope_key TEXT NOT NULL, correlation_id TEXT NOT NULL,
  turns INTEGER NOT NULL DEFAULT 0 CHECK(turns >= 0),
  window_start TEXT NOT NULL, reset_at TEXT NOT NULL,
  paused INTEGER NOT NULL DEFAULT 0 CHECK(paused IN (0,1)),
  last_origin_key TEXT REFERENCES channel_origins(origin_key),
  updated_at TEXT NOT NULL,
  PRIMARY KEY(scope_key, correlation_id)
);
