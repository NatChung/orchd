CREATE TABLE IF NOT EXISTS channel_cutovers (
  source_id TEXT PRIMARY KEY REFERENCES channel_sources(id),
  mode TEXT NOT NULL CHECK(mode IN ('adapter','legacy','shadow','frozen')),
  high_water INTEGER NOT NULL CHECK(high_water >= 0),
  backfill_digest TEXT,
  updated_at TEXT NOT NULL
);
