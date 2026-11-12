CREATE TABLE IF NOT EXISTS channel_outbound_grants (
  grant_id TEXT PRIMARY KEY,
  source_id TEXT NOT NULL REFERENCES channel_sources(id),
  route_id TEXT,
  target_json TEXT,
  created_by TEXT NOT NULL,
  created_at REAL NOT NULL,
  expires_at REAL,
  state TEXT NOT NULL CHECK(state IN ('active','revoked')),
  reason TEXT NOT NULL,
  provenance TEXT NOT NULL,
  revoked_at REAL,
  revoked_by TEXT,
  revoke_reason TEXT,
  max_messages INTEGER CHECK(max_messages IS NULL OR max_messages > 0),
  max_per_hour INTEGER CHECK(max_per_hour IS NULL OR max_per_hour > 0)
);
CREATE TABLE IF NOT EXISTS channel_draft_authors (
  draft_id TEXT NOT NULL,
  version INTEGER NOT NULL,
  role TEXT NOT NULL,
  identity TEXT NOT NULL,
  payload_hash TEXT NOT NULL,
  PRIMARY KEY(draft_id,version),
  FOREIGN KEY(draft_id,version) REFERENCES intake_drafts(draft_id,version)
);
CREATE TABLE IF NOT EXISTS channel_grant_approvals (
  approval_id TEXT PRIMARY KEY,
  grant_id TEXT NOT NULL REFERENCES channel_outbound_grants(grant_id),
  draft_id TEXT NOT NULL,
  version INTEGER NOT NULL,
  payload_hash TEXT NOT NULL,
  sender TEXT NOT NULL,
  target TEXT NOT NULL,
  created_at REAL NOT NULL,
  delivery_id TEXT NOT NULL REFERENCES channel_deliveries(id),
  FOREIGN KEY(draft_id,version) REFERENCES intake_drafts(draft_id,version)
);
