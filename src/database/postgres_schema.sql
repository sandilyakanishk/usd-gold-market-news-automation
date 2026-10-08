-- Calendar events and their classifications (PostgreSQL / Supabase).
-- Applied by `python -m src.main --init-db`; safe to run repeatedly.
-- It can also be pasted into the Supabase SQL editor.

CREATE TABLE IF NOT EXISTS events (
    event_id        TEXT        PRIMARY KEY,
    date            DATE,                    -- in the zone named by "timezone"
    time            TIME,                    -- in the zone named by "timezone"
    timezone        TEXT,                    -- NULL when the source gave no offset
    datetime_utc    TIMESTAMPTZ,             -- exact instant; NULL when timezone is unknown
    currency        TEXT        NOT NULL,
    event_name      TEXT        NOT NULL,
    impact          TEXT        NOT NULL CHECK (impact IN ('High', 'Medium', 'Low', 'Holiday', 'None')),
    original_impact TEXT,
    gold_relevance  BOOLEAN     NOT NULL DEFAULT FALSE,
    forecast        TEXT,
    previous        TEXT,
    actual          TEXT,
    source          TEXT        NOT NULL,
    source_url      TEXT        NOT NULL,
    retrieved_at    TIMESTAMPTZ NOT NULL,
    updated_at      TIMESTAMPTZ NOT NULL
);

-- Date-window lookups (--today, --week, retention cleanup).
CREATE INDEX IF NOT EXISTS idx_events_date_currency ON events (date, currency);
-- Stale-event removal after each sync.
CREATE INDEX IF NOT EXISTS idx_events_datetime_utc ON events (datetime_utc);

-- Supabase exposes tables in "public" through its auto-generated web API.
-- Row level security with no policies keeps this table private to direct
-- database connections (the table owner, which this application uses, is
-- not restricted by it).
ALTER TABLE events ENABLE ROW LEVEL SECURITY;

-- Editorial classification of each event (Step 2). Derived data: it never
-- alters the source row, and it is deleted together with its event.
CREATE TABLE IF NOT EXISTS event_classifications (
    event_id               TEXT        PRIMARY KEY REFERENCES events (event_id) ON DELETE CASCADE,
    gold_relevance         BOOLEAN     NOT NULL,
    gold_relevance_level   TEXT        NOT NULL CHECK (gold_relevance_level IN ('STRONG', 'MODERATE', 'WEAK', 'NONE')),
    gold_relevance_reason  TEXT        NOT NULL,
    category               TEXT        NOT NULL,   -- allowed values are defined in config/gold_priority_rules.json
    priority               TEXT        NOT NULL CHECK (priority IN ('CRITICAL', 'HIGH', 'MEDIUM', 'LOW')),
    priority_score         INTEGER     NOT NULL CHECK (priority_score BETWEEN 0 AND 100),
    highlight_required     BOOLEAN     NOT NULL DEFAULT FALSE,
    classification_reason  TEXT        NOT NULL,
    classification_version TEXT        NOT NULL,
    classified_at          TIMESTAMPTZ NOT NULL,   -- last time the rules were applied
    updated_at             TIMESTAMPTZ NOT NULL,   -- last time the result changed
    CONSTRAINT event_classifications_gold_flag_matches_level
        CHECK (gold_relevance = (gold_relevance_level <> 'NONE'))
);

ALTER TABLE event_classifications ENABLE ROW LEVEL SECURITY;

-- Actual-result enrichment (Step 3). The value itself is stored in
-- events.actual; this table records where it came from, the release status
-- and the factual comparison with the forecast.
CREATE TABLE IF NOT EXISTS event_actuals (
    event_id            TEXT        PRIMARY KEY REFERENCES events (event_id) ON DELETE CASCADE,
    release_status      TEXT        NOT NULL CHECK (release_status IN ('UPCOMING', 'RELEASED', 'NO_DATA', 'FAILED')),
    status_reason       TEXT        NOT NULL DEFAULT '',
    actual_source       TEXT,                    -- provider that supplied the value, e.g. BLS
    actual_source_event TEXT,                    -- the provider's series
    actual_period       TEXT,                    -- period the value refers to, e.g. 2026-09
    actual_revision     INTEGER     NOT NULL DEFAULT 0 CHECK (actual_revision >= 0),
    surprise_status     TEXT        NOT NULL
        CHECK (surprise_status IN ('ABOVE_FORECAST', 'BELOW_FORECAST', 'IN_LINE_WITH_FORECAST', 'NOT_AVAILABLE')),
    surprise_value      DOUBLE PRECISION,        -- Actual minus Forecast, in their shared unit
    actual_retrieved_at TIMESTAMPTZ,             -- when a value was first obtained
    actual_updated_at   TIMESTAMPTZ,             -- when the value last changed
    updated_at          TIMESTAMPTZ NOT NULL,    -- when this record last changed
    CONSTRAINT event_actuals_released_has_a_value
        CHECK ((release_status = 'RELEASED') = (actual_revision > 0))
);

ALTER TABLE event_actuals ENABLE ROW LEVEL SECURITY;

-- Message deliveries (Step 5). One row per message, provider and destination;
-- the primary key is what prevents the same message being sent twice.
CREATE TABLE IF NOT EXISTS message_deliveries (
    message_key         TEXT        NOT NULL,    -- the content engine's message identity
    provider            TEXT        NOT NULL,    -- e.g. whapi
    destination_id      TEXT        NOT NULL,    -- the provider's address, e.g. a WhatsApp chat id
    destination         TEXT        NOT NULL,    -- e.g. whatsapp_community_announcement
    message_type        TEXT        NOT NULL,
    status              TEXT        NOT NULL CHECK (status IN ('SENT', 'FAILED')),
    provider_message_id TEXT,
    sent_at             TIMESTAMPTZ,
    error               TEXT,
    attempts            INTEGER     NOT NULL DEFAULT 1 CHECK (attempts >= 1),
    created_at          TIMESTAMPTZ NOT NULL,
    updated_at          TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (message_key, provider, destination_id),
    CONSTRAINT message_deliveries_sent_is_complete
        CHECK ((status = 'SENT') = (sent_at IS NOT NULL AND provider_message_id IS NOT NULL))
);

ALTER TABLE message_deliveries ENABLE ROW LEVEL SECURITY;

-- The price shown in each market-pulse post, one row per half-hour slot.
-- "slot" is an ISO UTC instant kept as text (2026-10-08T10:30:00Z), so it
-- sorts and compares as written on every backend.
CREATE TABLE IF NOT EXISTS price_snapshots (
    symbol            TEXT             NOT NULL,    -- e.g. XAU
    slot              TEXT             NOT NULL,
    price             DOUBLE PRECISION NOT NULL CHECK (price > 0),
    source            TEXT             NOT NULL,
    source_updated_at TEXT,
    recorded_at       TEXT             NOT NULL,
    PRIMARY KEY (symbol, slot)
);

ALTER TABLE price_snapshots ENABLE ROW LEVEL SECURITY;

-- One line per trading day, made from that day's recorded prices before they
-- are deleted. Feeds the "key levels" card. "day" is an ISO date kept as text.
CREATE TABLE IF NOT EXISTS daily_prices (
    symbol  TEXT             NOT NULL,
    day     TEXT             NOT NULL,
    open    DOUBLE PRECISION NOT NULL,
    high    DOUBLE PRECISION NOT NULL,
    low     DOUBLE PRECISION NOT NULL,
    close   DOUBLE PRECISION NOT NULL,
    samples INTEGER          NOT NULL,
    PRIMARY KEY (symbol, day)
);

ALTER TABLE daily_prices ENABLE ROW LEVEL SECURITY;

-- Where each content list (quiz, rules, facts, myths, lessons) has got to,
-- and what was used lately. A handful of rows; never cleaned up.
CREATE TABLE IF NOT EXISTS content_state (
    kind       TEXT    PRIMARY KEY,
    position   INTEGER NOT NULL DEFAULT 0,
    recent     TEXT    NOT NULL DEFAULT '[]',
    updated_at TEXT    NOT NULL
);

ALTER TABLE content_state ENABLE ROW LEVEL SECURITY;
