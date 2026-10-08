# USD / Gold calendar collector

Collects the Forex Factory economic calendar, keeps the USD events, tags the
ones that matter for Gold/XAUUSD, stores them in a database, and classifies
each one by Gold relevance, category and editorial priority.

- **Step 1** collects and stores the calendar.
- **Step 2** classifies the stored events (see
  [Event classification](#event-classification-step-2)).
- **Step 3** adds the released Actual value from official US sources (see
  [Actual results](#actual-results-step-3)).
- **Step 4** turns the stored data into ready-to-publish message text (see
  [Message content](#message-content-step-4)). It only generates text.
- **Step 5** delivers those messages, unchanged, to a WhatsApp Community's
  Announcements group through Whapi.Cloud (see
  [WhatsApp delivery](#whatsapp-delivery-step-5)).

- **Step 6** runs the project on GitHub Actions (see
  [Cloud runner](#cloud-runner-step-6)): a manual send-nothing test workflow,
  and a scheduled production workflow that sends real messages.

Telegram and AI analysis are not in this repository.

The classification is an editorial event-priority system for deciding which
events deserve attention in a news feed. It is **not a trading signal system
and not financial advice**: it never says which way Gold or the USD will move.

## Architecture

```
Forex Factory weekly export
        |
   collector  (download, parse, normalize)
        |
   USD / Gold filter
        |
   EventRepository  ----  SQLite      local development and tests
        |           \---  PostgreSQL  production (Supabase)
        |
   classification  (rules file -> relevance level, category, priority, highlight)
        |
   EventRepository  (event_classifications table, same two backends)
        |
   actual enrichment  (mapping file -> official source -> Actual, release status, surprise)
        |
   EventRepository  (events.actual + event_actuals table)
        |
   content engine  (read-only: headline rules + templates -> message text)
        |
   ready-to-publish message
        |
   WhatsApp adapter  (delivery only; the text is passed on unchanged)
        |
   Whapi.Cloud  ->  WhatsApp Community Announcements group
        |
   EventRepository  (message_deliveries table: what was sent, never twice)
```

- **SQLite** is the default. It is a file on your computer, needs no account,
  and is what the tests use.
- **Supabase PostgreSQL** is the production database. It lives in the cloud, so
  the data persists between runs of a cloud job and does not depend on any
  local disk or on your computer being on.

Which one is used is decided only by `DATABASE_BACKEND`. The collector, the
filters and the command line are identical for both. If `postgres` is selected
and cannot be reached, the program stops with an error; it never falls back to
SQLite.

Supabase is used purely as managed PostgreSQL through the standard `psycopg`
driver. No Supabase SDK or API key is involved, so any PostgreSQL server works.

## Local setup (SQLite)

```bash
pip install -r requirements.txt
```

```bash
python -m src.main --today
```

Python 3.10 or newer. Nothing else is required: with no `.env` file the
application uses SQLite at `data/events.db` and creates it on first run.

## Running the collector

Every normal run downloads the calendar (respecting the cache), updates the
database, removes events past the retention period, and prints the result.

| Option | Shows |
| --- | --- |
| `--today`, `--tomorrow`, `--week` (default), `--date YYYY-MM-DD`, `--all` | which days |
| `--from YYYY-MM-DD --to YYYY-MM-DD` | a date range, inclusive |
| `--usd` (default) | all USD events |
| `--high-impact`, `--medium-impact`, `--low-impact` | by Forex Factory impact |
| `--gold` | Gold-relevant events only |
| `--gold-high-impact`, `--gold-medium-impact` | Gold-relevant and that impact |
| `--json` | the normalized records as JSON |
| `--no-fetch` | database only: no download, no cleanup |
| `--force-fetch` | skip the local cache |

Options combine: `python -m src.main --week --gold --high-impact --json`.

Priority view (Step 2). Any of these classifies the stored events and prints
the priority view instead of the plain calendar:

| Option | Shows |
| --- | --- |
| `--classify` | every selected event with its classification |
| `--critical`, `--high-priority`, `--medium-priority`, `--low-priority` | only that priority (several can be combined) |
| `--highlight` | only events with `highlight_required` |

The date options and the impact options work in this view too. `--gold` here
means "Gold relevance is not NONE" according to the classification.

Actual results view (Step 3):

| Option | Does |
| --- | --- |
| `--enrich-actuals` | asks the mapped official sources for released values, stores them, shows the view |
| `--actuals` | shows the stored results without contacting any source |
| `--released` | only events with a verified Actual |
| `--missing-actual` | only past events without one (`NO_DATA` or `FAILED`) |
| `--dry-run` | with `--enrich-actuals`: shows what would change, writes nothing |
| `--recheck-released` | with `--enrich-actuals`: also checks released events for revisions |

The date, impact and priority options work here too, and narrow which events
are looked up.

Message previews (Step 4). These only read the database and print text; they
never download, classify, look up a source, write or send:

| Option | Shows |
| --- | --- |
| `--preview-morning` | the daily update for today (or `--tomorrow` / `--date`) |
| `--preview-alert` | alerts for upcoming events marked `highlight_required` |
| `--preview-actuals` | result messages for events with a verified Actual |
| `--preview-upcoming` | reminders for upcoming high-priority events |
| `--fixture` | with a preview: use the bundled sample events instead of the database |

WhatsApp delivery (Step 5):

| Option | Does |
| --- | --- |
| `--whatsapp-check` | checks the Whapi connection and the destination; sends nothing |
| `--whatsapp-test` | sends one fixed test message |
| `--whatsapp-send-morning` | sends the daily update for today (or `--tomorrow` / `--date`) |
| `--whatsapp-send-alert` | sends high-impact alerts |
| `--whatsapp-send-actuals` | sends result messages for released events |
| `--whatsapp-send-upcoming` | sends reminders for upcoming high-priority events |
| `--dry-run` | with a send option: shows the message, key and destination; sends and records nothing |

Database maintenance commands (each runs on its own and exits):

| Command | Does |
| --- | --- |
| `--init-db` | creates the tables in the configured database |
| `--cleanup` | deletes events older than `CALENDAR_RETENTION_DAYS` |
| `--migrate-to-postgres` | copies the local SQLite events into PostgreSQL |

Dates and times are stored and shown in `DISPLAY_TIMEZONE` (default
`Asia/Kolkata`); the exact instant is also stored as `datetime_utc`.

## Production setup (Supabase)

1. **Create a project** at [supabase.com](https://supabase.com) and choose a
   database password. Keep the password; it is needed in step 2.

2. **Get the connection string.** In the project, click **Connect** at the top
   of the page. Supabase offers three connection strings:

   | Method | Port | Network |
   | --- | --- | --- |
   | Direct connection | 5432 | IPv6 (IPv4 only with the paid IPv4 add-on) |
   | Session pooler | 5432 | works on IPv4 networks |
   | Transaction pooler | 6543 | works on IPv4 networks |

   All three work with this application. If the machine that will run it has
   no IPv6 (many home connections and cloud job runners do not), use one of
   the pooler strings. Replace `[YOUR-PASSWORD]` with the database password.
   See Supabase's
   [connection guide](https://supabase.com/docs/guides/database/connecting-to-postgres)
   for the details.

3. **Store it in the environment.** Copy `.env.example` to `.env` and set:

   ```
   DATABASE_BACKEND=postgres
   DATABASE_URL=postgresql://...your connection string...
   ```

   `.env` is ignored by Git. On a cloud runner, set both as secret environment
   variables instead of using a file.

4. **Create the table.**

   ```bash
   python -m src.main --init-db
   ```

   Alternatively paste [src/database/postgres_schema.sql](src/database/postgres_schema.sql)
   into the Supabase SQL editor. Both are safe to repeat.

5. **Optionally copy your local data across.**

   ```bash
   python -m src.main --migrate-to-postgres
   ```

6. **Run the collector against PostgreSQL.**

   ```bash
   python -m src.main --week
   ```

7. **Verify.** In Supabase open **Table Editor** and select `events`; this
   week's USD events should be listed. Or in the SQL editor:

   ```sql
   select date, time, event_name, impact, gold_relevance from events order by date, time;
   ```

To go back to SQLite, set `DATABASE_BACKEND=sqlite` (or remove the line).

### Access to the table

Supabase publishes tables in the `public` schema through an auto-generated web
API. The schema turns on row level security for `events` and defines no
policies, which blocks that API for the anon and authenticated keys. This
application is unaffected because it connects directly as the table owner.

## Environment variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `DATABASE_BACKEND` | `sqlite` | `sqlite` or `postgres` |
| `SQLITE_PATH` | `data/events.db` | SQLite file (`DATABASE_PATH` is accepted as an older name) |
| `DATABASE_URL` | *(empty)* | PostgreSQL connection string; required for `postgres`. Secret. |
| `DB_CONNECT_TIMEOUT_SECONDS` | `10` | PostgreSQL connection timeout |
| `CALENDAR_RETENTION_DAYS` | `14` | days of calendar history to keep (minimum 1) |
| `DISPLAY_TIMEZONE` | `Asia/Kolkata` | zone for stored dates, "today" and the retention cutoff |
| `FF_CALENDAR_URL` | Forex Factory weekly JSON | calendar source |
| `MIN_FETCH_INTERVAL_SECONDS` | `1800` | minimum time between downloads |
| `REQUEST_TIMEOUT_SECONDS` | `20` | download timeout |
| `RAW_CACHE_PATH` | `data/raw/ff_calendar_thisweek.json` | last raw feed, cache and debugging copy |
| `GOLD_RELEVANCE_CONFIG` | `config/gold_relevance.json` | Step 1 Gold keyword list |
| `GOLD_PRIORITY_RULES` | `config/gold_priority_rules.json` | Step 2 classification rules |
| `ACTUAL_EVENT_MAPPING` | `config/actual_event_mapping.json` | Step 3 event-to-source mapping |
| `BLS_API_KEY` | *(empty)* | optional BLS registration key (raises the daily limit). Secret. |
| `FRED_API_KEY` | *(empty)* | FRED key, needed for the FRED-sourced events. Secret. |
| `ACTUALS_RETRY_DAYS` | `7` | stop asking a source about an unpublished release after this many days |
| `WHAPI_TOKEN` | *(empty)* | Whapi.Cloud channel token. Secret. |
| `WHATSAPP_COMMUNITY_ID` | *(empty)* | the Community's id; used by `--whatsapp-check` to confirm the group |
| `WHATSAPP_ANNOUNCEMENT_CHAT_ID` | *(empty)* | the Announcements group messages are sent to |
| `WHAPI_BASE_URL` | `https://gate.whapi.cloud` | Whapi gateway |
| `MESSAGE_TEMPLATES` | `config/message_templates.json` | Step 4 templates, labels and selection rules |
| `HEADLINE_RULES` | `config/headline_rules.json` | Step 4 headline wording |
| `PREVIEW_FIXTURE` | `fixtures/message_preview_events.json` | sample events for `--fixture` |
| `LOG_PATH`, `LOG_LEVEL` | `logs/collector.log`, `INFO` | logging |
| `TEST_DATABASE_URL` | *(unset)* | tests only: also run the storage tests on a real PostgreSQL |

Setting `DATABASE_URL` alone does not switch the backend; `DATABASE_BACKEND`
must say `postgres`.

## Event classification (Step 2)

Step 2 reads the USD events already stored by Step 1 and attaches structured
metadata to each one. It is deterministic: the same event and the same rule
file always give the same result. No AI model or external service is called.

Five things are kept separate:

| Concept | Field | Where it comes from |
| --- | --- | --- |
| Forex Factory impact | `events.impact` | the source, never changed |
| Gold relevance | `gold_relevance_level`, `gold_relevance` | the rule file |
| Event category | `category` | the rule file |
| Internal priority | `priority`, `priority_score` | scoring, below |
| Future alert level | `highlight_required` | priority plus explicit rules |

### Running it

```bash
python -m src.main --classify --week
```

```bash
python -m src.main --highlight
```

Each run classifies every stored event and then prints the selected ones.
It is safe to repeat: there is one classification row per event, and a row is
only rewritten when its result actually changes.

### Gold relevance levels

| Level | Meaning | `gold_relevance` |
| --- | --- | --- |
| `STRONG` | a main driver: inflation, employment, Fed decisions, Fed Chair, headline growth and consumer demand | true |
| `MODERATE` | a recognised secondary release or Fed communication | true |
| `WEAK` | regional, minor or only occasionally relevant | true |
| `NONE` | explicitly excluded, or no rule matches | false |

Relevance is decided only by rules that name an event. There is no keyword
matching: a name containing "Fed", "Federal", "Bank" or "Treasury" gets no
relevance from that word. `Philly Fed Manufacturing Index` is `WEAK` because
it is listed as a regional survey, and an unlisted "Fed ..." event is `NONE`.

### Categories

Each event gets exactly one, from the fixed list in the rule file:
`MONETARY_POLICY`, `FED_COMMUNICATION`, `INFLATION`, `EMPLOYMENT`, `GROWTH`,
`CONSUMER_ACTIVITY`, `BUSINESS_ACTIVITY`, `MANUFACTURING`, `SERVICES`,
`HOUSING`, `TRADE`, `ENERGY`, `GOVERNMENT_FISCAL`, `SENTIMENT`, `OTHER`.

### Priority and score

`priority_score` (0 to 100) is the sum of three parts:

| Part | Points |
| --- | --- |
| Forex Factory impact | High 30, Medium 20, Low 10, Holiday/None 0 |
| Gold relevance | STRONG 40, MODERATE 25, WEAK 10, NONE 0 |
| Event importance | listed in `critical_events` 30, listed in `high_priority_events` 15, otherwise 0 |

| Priority | Score |
| --- | --- |
| `CRITICAL` | 90 or more |
| `HIGH` | 65 to 89 |
| `MEDIUM` | 40 to 64 |
| `LOW` | below 40 |

Relevance also caps the result: `NONE` can never exceed `LOW`, `WEAK` never
exceeds `MEDIUM`, `MODERATE` never exceeds `HIGH`. So a High-impact event that
is not Gold-relevant stays `LOW`, and Forex Factory impact alone never makes
an event `CRITICAL`.

Examples: `CPI m/m` at High impact scores 30 + 40 + 30 = 100, `CRITICAL`.
`Unemployment Claims` at Medium scores 20 + 25 + 15 = 60, `MEDIUM`.
`Crude Oil Inventories` at High scores 30 + 0 + 0 = 30, `LOW`.

The score ranks editorial importance. It is not a forecast of price movement.

### highlight_required

A true/false flag telling a later publishing stage that the event should use
the highlighted presentation. It is true when the priority is `CRITICAL`, and
for `HIGH`-priority events named in `highlight.high_priority_events`
(currently only `FOMC Meeting Minutes`). It is never true for an event with no
Gold relevance, whatever its Forex Factory impact. It is only a flag; no
template or message exists yet.

### Editing the rules

Everything is in [config/gold_priority_rules.json](config/gold_priority_rules.json);
no Python changes are needed.

| Section | Purpose |
| --- | --- |
| `classification_version` | label stored with every result |
| `categories` | the allowed categories |
| `exclusions` | events that are never Gold-relevant; checked first |
| `strong_gold_events`, `moderate_gold_events`, `weak_gold_events` | events at each level, grouped by category with a reason |
| `default` | category and reason for names no rule matches |
| `critical_events`, `high_priority_events` | events that earn importance points |
| `scoring` | the points, thresholds and caps |
| `highlight` | which priorities and which named events are highlighted |

Event names are Forex Factory's own. A name must match in full, ignoring
case. `*` stands for any text and `?` for one character, so `CPI ?/?` matches
`CPI m/m` and `CPI y/y` but not `Core CPI m/m`, and `FOMC Member * Speaks`
matches any member.

When Forex Factory lists an event that no rule covers, it is classified by
`default` (`OTHER`, `NONE`, `LOW`) and the priority view names it under "No
rule yet for", so you know to add it.

### Versions and reclassifying

1. Edit the rule file.
2. Raise `classification_version` (for example `1.0.0` to `1.1.0`).
3. Run `python -m src.main --classify`.

Existing rows are updated in place; none are added. Every row records the
version that produced it, so you can tell which rules a stored classification
came from. `updated_at` moves only when a row's result changed; `classified_at`
is the last time the rules were applied.

A broken rule file (invalid JSON, unknown category, an event listed in two
sections, thresholds out of order) stops classification with a
`CONFIGURATION ERROR` and changes nothing.

### Relation to the Step 1 Gold flag

`events.gold_relevance` is still the Step 1 keyword flag and still drives the
plain `--gold` view, so Step 1 behaves exactly as before. The classification's
own `gold_relevance` is the one later stages should use.

## Actual results (Step 3)

Forex Factory's weekly JSON export carries the event, its time, impact,
forecast and previous value, but no Actual column. Step 3 fills that gap.

**Forex Factory remains the source of the calendar and of every event.** A
second source is used for one thing only: the released Actual of an event that
has an explicit mapping. Nothing here is a trading signal or financial advice;
the comparison with the forecast is a statement of fact.

### Sources

| Source | Used for | Cost | Key | Limits |
| --- | --- | --- | --- | --- |
| **BLS** Public Data API (U.S. Bureau of Labor Statistics) | CPI m/m, CPI y/y, Core CPI m/m, PPI m/m, Core PPI m/m, Non-Farm Employment Change, Unemployment Rate, Average Hourly Earnings m/m | free | not required | 25 requests a day without a key, 500 with a free registration key |
| **FRED** API (Federal Reserve Bank of St. Louis; republishes BEA, Census Bureau, Department of Labor and Federal Reserve Board data) | Core PCE Price Index m/m, PCE Price Index m/m, Advance/Prelim/Final GDP q/q, Retail Sales m/m, Core Retail Sales m/m, Unemployment Claims, Federal Funds Rate | free | required (free account) | no fixed figure published; one request per series |

Both are official US sources. All BLS series for a run go into a single
request, and a source is only contacted when an event that it covers is past
its release time and has no verified value yet.

Without `FRED_API_KEY` the FRED-sourced events stay at `NO_DATA` with a note
saying the key is missing; the BLS events work regardless. To get a key,
register at [fred.stlouisfed.org](https://fred.stlouisfed.org/docs/api/api_key.html)
and put it in `.env`:

```
FRED_API_KEY=your-key
```

This product uses the FRED® API but is not endorsed or certified by the
Federal Reserve Bank of St. Louis. That notice is required by FRED's terms of
use wherever FRED-sourced values are shown, which will matter once results are
published.

### Commands

```bash
python -m src.main --enrich-actuals --week
```

```bash
python -m src.main --enrich-actuals --dry-run
```

```bash
python -m src.main --released
```

### How an event is matched

1. The event must be a USD event.
2. Its **whole** Forex Factory name must be listed in
   [config/actual_event_mapping.json](config/actual_event_mapping.json).
   There is no keyword or fuzzy matching: `CPI m/m` is mapped, `Median CPI m/m`
   and `CPI q/q` are not, and a EUR `CPI m/m` is never considered.
3. The event must be past its release time, judged by the exact UTC instant
   stored in Step 1 (not by the display timezone).
4. The value is taken for the specific period that release reports on, worked
   out from the release date: the previous month for monthly indicators, the
   previous quarter for GDP, the week ending the previous Saturday for jobless
   claims, the following day for the Fed funds target. If the source has not
   published that period, nothing is stored. An older figure is never reused.

Each mapping names the provider, the official series, the reporting period and
how the published figure becomes the number Forex Factory quotes (for example
the one-month percentage change of the CPI index, rounded to one decimal). To
support another event, add a mapping; no Python change is needed.

Speeches, statements and minutes are listed under `events_without_actual`;
no source is asked about them.

### Release status

| Status | Meaning |
| --- | --- |
| `UPCOMING` | the release time has not been reached; no source is contacted |
| `RELEASED` | a verified Actual was found and stored |
| `NO_DATA` | the release time has passed but there is no verified value: no mapping, no numeric result, a missing key, or not published yet |
| `FAILED` | the source was asked and the request failed (network error, HTTP error, daily limit) |

`status_reason` says which case applies. A `FAILED` or `NO_DATA` event is
tried again on the next run, for up to `ACTUALS_RETRY_DAYS` days. A value that
was already verified is never removed because of a later outage.

### Surprise

Once an Actual exists it is compared with the Forex Factory forecast:
`ABOVE_FORECAST`, `BELOW_FORECAST`, `IN_LINE_WITH_FORECAST`, or
`NOT_AVAILABLE` when there is no forecast or the two cannot be compared.
`surprise_value` is Actual minus Forecast in the unit both are quoted in
(`0.2` for 3.2% against 3.0%, `18` for 218K against 200K). It is left empty
when the units differ. This describes the release; it says nothing about what
any market will do.

### Revisions

`actual_revision` is 1 for the first verified value. Run with
`--recheck-released` to ask the sources again about released events; if a
figure has been revised, the same event is updated, `actual_revision` goes up
by one and `actual_updated_at` moves, while `actual_retrieved_at` keeps the
time of the first release. No new event is created. Without that flag,
released events are left alone, so routine runs never rewrite them.

### What is never changed

The event's `event_id`, `forecast`, `previous`, impact and timing stay exactly
as Forex Factory supplied them. Only `actual` is filled in, and it is never
estimated, calculated from the forecast or previous value, or copied from
them.

### Limitations

- Events without a free official source stay at `NO_DATA`. That includes the
  ISM and S&P PMIs, the University of Michigan and Conference Board surveys,
  and ADP employment (its series is third-party copyrighted on FRED).
- The Fed funds target is published as a daily series, so the rate decision's
  value appears the day after the announcement, not at the moment of it.
- Values are available when the agency's API updates, normally within minutes
  of the release but not guaranteed.
- A release delayed past its usual month (for example by a government
  shutdown) will not match its expected period and stays at `NO_DATA`.
- The FRED provider is tested with hand-written responses in FRED's documented
  format; it has not been run against the live service, because that needs
  your key.
- Without a BLS key, 25 requests a day is enough for occasional runs but not
  for frequent polling.

## Message content (Step 4)

Step 4 converts an event, its classification and its actual result into text
that can later be posted to a Telegram channel or a WhatsApp community. It is
deterministic: the same data and the same configuration always produce the
same text. No AI model is called. Nothing is sent anywhere, and the content
engine cannot write to the database.

### event_name and headline are different things

**`event_name` is the canonical Forex Factory event name and must never be
replaced by the generated headline.**

| | `event_name` | `headline` |
| --- | --- | --- |
| What it is | source data from Forex Factory | presentation text written by Step 4 |
| Example | `CPI m/m` | `🇺🇸 US CPI inflation data due today` |
| Changes over time | never | yes: before release, after release, by result |
| Stored | `events.event_name` | not stored; generated on demand |

`event_name` is set once, by the parser, from the feed's title. No later step
renames it: the classification and actual-result tables have no name column,
and the name of the official data series used for an Actual is kept apart in
`event_actuals.actual_source_event`. Every event-based message prints both
the headline and the exact event name, and a template that leaves either one
out is rejected when it is loaded.

### Message types

| Type | Covers | Generated when | `message_key` |
| --- | --- | --- | --- |
| `MORNING_UPDATE` | one overview of a day's events | on request, for one date | `DAILY_UPDATE_<date>` |
| `HIGH_ALERT` | one event | `highlight_required` is true and the event has not been released | `HIGH_ALERT_<event_id>` |
| `ACTUAL_RESULT` | one event | `release_status` is `RELEASED` | `ACTUAL_<event_id>_<actual_revision>` |
| `UPCOMING_REMINDER` | one event | not released yet and priority is high enough | `UPCOMING_<event_id>` |

`message_key` is a stable identity for the later delivery step, so the same
message is not posted twice. A revised figure gets a new key because the
revision number is part of it. Delivery tracking itself is not built.

Each message is a structure with `message_key`, `message_type`, `event_id`,
`event_name`, `headline`, `priority`, `highlight_required`, `text`,
`generated_at`, `events` (every event covered, each with its own name and
headline), `attribution` and `markdown_safe`. For the daily update, which
covers several events, `event_id`, `event_name` and `headline` are empty and
the per-event values are in `events`.

### Which events are selected

Selection uses the Step 2 classification (`priority`, `gold_relevance`,
`highlight_required`), never the Step 1 keyword flag. The rules are in the
`selection` part of [config/message_templates.json](config/message_templates.json):

| Setting | Default | Meaning |
| --- | --- | --- |
| `daily_minimum_priority` | `MEDIUM` | lowest priority shown in the daily update (so `LOW` is left out) |
| `daily_require_gold_relevance` | `true` | daily update only lists Gold-relevant events |
| `daily_order` | `priority` | order of events in the daily update: highest priority first, then strongest Gold relevance, then time. `time` gives plain chronological order. Each event still shows its own time; stored data is not reordered. |
| `alert_requires_highlight` | `true` | alerts only for `highlight_required` events |
| `alert_only_before_release` | `true` | no alert once the release time has passed |
| `upcoming_minimum_priority` | `HIGH` | lowest priority that gets a reminder |
| `upcoming_require_gold_relevance` | `true` | reminders only for Gold-relevant events |

A result message is produced only for `RELEASED` events that have a stored
Actual. `UPCOMING`, `NO_DATA` and `FAILED` events never get one.

### Impact, Gold relevance and priority

Every event block shows three separate things, each on its own line:

| Line | Whose assessment | Example |
| --- | --- | --- |
| 📊 Impact | Forex Factory's, exactly as stored | 🟡 Medium |
| 🥇 Gold Relevance | this bot's Step 2 level, plus a display number | 🟡 MODERATE · 65/100 |
| 🎯 Priority | this bot's Step 2 priority and its score | 🟠 HIGH · 85/100 |

**The Gold relevance number.** It is a fixed, display-only translation of the
Step 2 level:

| Level | Shown as |
| --- | --- |
| `STRONG` | 100/100 |
| `MODERATE` | 65/100 |
| `WEAK` | 30/100 |
| `NONE` | 0/100 |

It represents the relevance of the event to the bot's Gold/XAUUSD monitoring
framework. It is **not** a probability, not a likelihood that Gold rises or
falls, not an expected price move and not a prediction of direction. It is set
in `labels.gold_relevance_score`, is used only when text is generated, is not
stored, and has no effect on the Step 2 priority score, the priority, or
which events are selected.

**Why it matters.** One sentence per Step 2 category, from the
`why_it_matters` part of the template file (for example, for `INFLATION`:
"Inflation data can shift rate expectations and USD and yield pricing, making
it relevant to Gold."). It explains why an event is watched and never states
a direction. An event whose category has no sentence shows the Step 2
relevance reason instead.

**Alert title.** The alert is headed "HIGH-IMPACT USD ALERT" only when Forex
Factory rates the event High; a highlighted event with a lower rating is
headed "PRIORITY USD ALERT" (`labels.alert_title`).

### Headlines

Wording is in [config/headline_rules.json](config/headline_rules.json). A
headline depends on the event's state:

| State | When | Example for `CPI m/m` |
| --- | --- | --- |
| `UPCOMING` | not released yet | US CPI inflation data due today |
| `PASSED` | time has passed, no figure | US CPI inflation data |
| `ABOVE_FORECAST` | released, above forecast | US CPI comes in above expectations |
| `BELOW_FORECAST` | released, below forecast | US CPI comes in below expectations |
| `IN_LINE_WITH_FORECAST` | released, equal to forecast | US CPI in line with expectations |
| `RELEASED` | released, nothing to compare with | US CPI released |

A rule names its events exactly (`*` stands for any text and can be reused as
`{1}`, so `FOMC Member * Speaks` gives "Fed official Waller due to speak
today"). It supplies either a `subject` and `short` form for the shared
patterns, or its own text per state. `Federal Funds Rate` compares with the
previous rate rather than the forecast, giving "Fed leaves interest rates
unchanged at 4.00%", "Fed cuts interest rates to 3.75%" or "Fed raises
interest rates to 4.25%". Events without a rule use a wording for their
category (for example "US housing data due today"), so a headline never
repeats the technical name and never reads "CPI m/m m/m".

Headlines state facts only. They never give a trade direction or say what a
market will do.

### Templates and variables

Templates are lists of lines in `config/message_templates.json`, one set per
message type. To change a message, edit its lines; no Python change is needed.

Variables for one event:

| Variable | Example |
| --- | --- |
| `{headline}`, `{headline_text}` | with and without the leading emoji |
| `{event_name}` | `CPI m/m` (exact Forex Factory name) |
| `{currency}`, `{currency_flag}` | `USD`, 🇺🇸 |
| `{number}` | 1️⃣, 2️⃣ ... the event's position in the daily update |
| `{date}`, `{weekday}`, `{time}`, `{display_time}`, `{clock}` | `12 November 2026`, `Thursday`, `19:00`, `7:00 PM IST`, 🕖 |
| `{impact}`, `{impact_label}` | `High`, 🔴 High |
| `{gold_relevance}`, `{gold_relevance_level}`, `{gold_label}`, `{gold_relevance_score}` | `YES`, `STRONG`, 🟢 STRONG, `100` |
| `{why_it_matters}` | the category's explanation |
| `{alert_title}` | `HIGH-IMPACT USD ALERT` |
| `{result_sentence}` | "The figure came in above the forecast." |
| `{category}`, `{category_label}` | `FED_COMMUNICATION`, `FED COMMUNICATION` |
| `{priority}`, `{priority_label}`, `{priority_score}`, `{highlight_required}` | `CRITICAL`, 🔴 CRITICAL, `100`, `YES` |
| `{forecast}`, `{previous}`, `{actual}` | as stored, for example `0.3%`, `197K`, `4.00%` |
| `{release_status}`, `{actual_source}`, `{actual_period}`, `{actual_revision}` | `RELEASED`, `BLS`, `2026-10`, `1` |
| `{surprise_status}`, `{surprise_label}`, `{surprise_value}` | `ABOVE FORECAST`, 📈 ABOVE FORECAST, `+35K` |
| `{revision_note}` | "Revised figure (revision 2)", only for a revised Actual |
| `{classification_reason}`, `{gold_relevance_reason}` | the Step 2 explanations |
| `{source}`, `{attribution}` | `Forex Factory`; a source's required notice |

The header, footer and empty-day text of the daily update can use `{date}`,
`{weekday}`, `{event_count}` and `{source}`. An unknown variable is rejected
when the file is loaded.

**Missing values.** A value that does not exist is printed as `—` (the
`missing_value` setting), never as `None` or `null`, and is never made up.
Lines that use a variable listed in `omit_line_if_missing` are dropped
instead. That setting has a `default` list and can have a list per message
type. By default the result lines are dropped when there is no forecast to
compare with, as are the revision note and the attribution line when they do
not apply; in the daily update the `Previous`, `Forecast` and `Actual` lines
are dropped when empty, to keep it short, while the alert, result and
reminder messages show `—`.

**Numbers.** Values are shown exactly as stored. The content engine does not
convert, round or re-unit anything. The one conversion in the system happens
earlier, in Step 3, where a mapping states it explicitly (for example jobless
claims published as 218000 are stored as `218K`).

**Time.** Times are shown in `DISPLAY_TIMEZONE` (India time by default) as
`7:00 PM IST`. The path is: the feed's timestamp with its own UTC offset, to
the stored UTC instant, to an explicit conversion into `Asia/Kolkata` when
the text is built. There is exactly one conversion, from the UTC instant; the
machine's own timezone is never consulted, so a cloud runner in UTC prints
the same times as a computer in India. US daylight saving is handled by the
feed's offset: an 08:30 New York release shows as 6:00 PM IST in US summer
time and 7:00 PM IST in US winter time. The date printed is India's calendar
date, so an event at 18:30 UTC appears under the next day. The daily update's
own date is today in India, not the UTC date. The format, the zone label and
`strip_leading_zeros` are in the `time` part of the template file.

**Formatting.** Only `*bold*` and `_italic_` are used, which Telegram and
WhatsApp read the same way, and the text stays understandable with the
markers removed. `markdown_safe` is false when a value would unbalance those
markers; such a message should be sent as plain text. The event name is still
shown exactly, never altered to fit.

### Source lines and attribution

Calendar information is credited to Forex Factory. A result message names the
source of the Actual (`BLS` or `FRED`). When the figure came from FRED, the
message also carries the notice FRED's terms require: "This product uses the
FRED® API but is not endorsed or certified by the Federal Reserve Bank of St.
Louis." It is a single template line and a separate `attribution` field, so
it can be moved (for example to a pinned post) without restructuring
anything. Removing it entirely is your decision to make against FRED's terms.

### Previewing

```bash
python -m src.main --preview-morning
```

```bash
python -m src.main --preview-alert --fixture
```

```bash
python -m src.main --preview-actuals --fixture
```

```bash
python -m src.main --preview-upcoming --fixture
```

Without `--fixture` the previews read the configured database. With it they
use sample events from `fixtures/message_preview_events.json`, which are
classified by the real Step 2 rules and never written anywhere; this is how
to see an alert or a result message in a week that has none. Add `--json` for
the full message structure.

## WhatsApp delivery (Step 5)

Step 5 sends the messages produced by Step 4 to the Announcements group of a
WhatsApp Community, using [Whapi.Cloud](https://whapi.cloud).

The adapter is delivery-only. It takes a message from the content engine and
posts its text exactly as generated: headline, event name, values, priority
and source lines (including the FRED notice) are not touched. It contains no
template or headline logic.

Nothing here is scheduled. A message goes out only when you run a command.

### Setup

Add to `.env`:

```
WHAPI_TOKEN=your-channel-token
WHATSAPP_COMMUNITY_ID=the-community-id@g.us
WHATSAPP_ANNOUNCEMENT_CHAT_ID=the-announcements-group-id@g.us
```

On PostgreSQL, run `python -m src.main --init-db` once to create the
`message_deliveries` table. Then:

```bash
python -m src.main --whatsapp-check
```

This confirms the WhatsApp session is authenticated and that the configured
group really is the Announcements group of the configured Community. It sends
nothing.

```bash
python -m src.main --whatsapp-test
```

This sends one fixed test message and prints the Whapi message id.

### Sending

```bash
python -m src.main --whatsapp-send-morning --dry-run
```

```bash
python -m src.main --whatsapp-send-morning
```

The send commands use the data already stored; they do not download the
calendar, classify or look up actual values. Run the earlier steps first (for
example `--enrich-actuals --week`) so the content is current. Each uses the
same selection rules as its preview, so `--preview-morning` shows exactly
what `--whatsapp-send-morning` will post. A daily update for a day with no
qualifying events is not posted.

A dry run generates the real message and shows it with its `message_key` and
a masked destination. It sends nothing, records nothing, and needs no token.

### No duplicates

Every send is recorded in `message_deliveries`, keyed by `message_key`,
provider and destination. Before sending, that record is checked: if the
message was already sent successfully, the command prints "Already sent —
skipped." and sends nothing. The combination is the table's primary key, and
a `SENT` row can never be overwritten, so the rule holds at database level.

A failed attempt is recorded as `FAILED` with the error, and the next run
tries again. A revised Actual has a different `message_key`, so it is treated
as a new message.

### Errors

| Situation | What happens |
| --- | --- |
| Missing or rejected token | `WHATSAPP ERROR` naming `WHAPI_TOKEN`; nothing sent |
| Missing or invalid destination | error naming `WHATSAPP_ANNOUNCEMENT_CHAT_ID` |
| WhatsApp session not connected | error asking you to re-link the number in the Whapi dashboard |
| Timeout or network failure | recorded as `FAILED`; retried on the next run |
| Empty or over-long text | rejected before any request is made |
| Already sent | skipped |

The token is never printed or logged, and chat ids are shown masked
(`...8282@g.us`).

Whapi's gateway rejects requests that identify themselves as a generic Python
client, so the adapter always sends this project's own `User-Agent`.

### Limitations

- Duplicate prevention relies on one process sending at a time. Two runs
  started at the same instant could both pass the check before either records
  its send.
- If a send succeeds at Whapi but the database write that follows fails, the
  next run will send again.
- "Sent" means Whapi accepted the message. Delivery and read status are not
  tracked.
- Whapi's free sandbox has message and time limits; check your plan before
  relying on it daily.

## Telegram delivery

The same four message types can also be posted to a Telegram channel through
a bot. The text is identical to what WhatsApp receives; `*bold*` and
`_italic_` render the same way. Telegram keeps its own delivery record
(provider `telegram`), so the two channels are independent: a message sent to
one is still sent to the other, and a failure on one never blocks the other.

### Setup

1. In Telegram, message **@BotFather**, send `/newbot` and follow the prompts.
   It gives you a bot token.
2. Create a channel and add the bot as an administrator with permission to
   post messages.
3. Add to `.env`:

   ```
   TELEGRAM_BOT_TOKEN=the-token-from-botfather
   TELEGRAM_CHAT_ID=@yourchannel
   ```

4. Check it, which sends nothing:

   ```bash
   python -m src.main --telegram-check
   ```

### Commands

| Option | Does |
| --- | --- |
| `--telegram-check` | checks the token and that the bot may post to the channel; sends nothing |
| `--telegram-test` | sends one fixed test message |
| `--telegram-send-morning`, `--telegram-send-alert`, `--telegram-send-actuals`, `--telegram-send-upcoming` | send that message type |
| `--dry-run` | with a send option: shows what would be sent; sends and records nothing |

A WhatsApp and a Telegram option can be given together, for example
`--whatsapp-send-morning --telegram-send-morning`.

### In the cloud

Add `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` as repository secrets. The
production workflow posts to Telegram only when the repository **variable**
`TELEGRAM_ENABLED` is `true`:

```bash
gh variable set TELEGRAM_ENABLED --body true
```

Set it to `false` (or delete it) to stop Telegram posts without touching
WhatsApp. When enabled, each run checks the bot and channel, then sends the
same messages on the same timetable as WhatsApp. The Bot API is free and has
no monthly request cap.

If a value in a message would break Telegram's formatting, the same text is
sent as plain text instead.

## Cloud runner (Step 6)

The project runs on GitHub Actions, so it does not need your computer. There
are two workflows: **Safe test**, started by hand, which cannot send a
message, and **Production**, which runs on a schedule and sends real WhatsApp
messages.

### What is in the repository

| File | Purpose |
| --- | --- |
| `.github/workflows/safe-test.yml` | manual "Safe test" workflow; sends nothing |
| `.github/workflows/production.yml` | scheduled "Production" workflow; sends real messages |
| `src/preflight.py` | reports Python version, backend and which settings are present |
| `.gitignore`, `.gitattributes` | keep secrets and runtime files out; keep line endings identical on Windows and Linux |
| `tests/test_cloud_runner.py` | fails the build if the safe test could ever send or run on a schedule |
| `tests/test_production_workflow.py` | pins down what production may send, when, and under which conditions |

### Repository secrets

In the GitHub repository open **Settings -> Secrets and variables -> Actions
-> New repository secret** and add:

| Secret | Required | Value |
| --- | --- | --- |
| `DATABASE_URL` | yes | the Supabase session-pooler connection string, with the password |
| `WHAPI_TOKEN` | yes | the Whapi.Cloud channel token |
| `WHATSAPP_COMMUNITY_ID` | yes | the Community id, `<digits>@g.us` |
| `WHATSAPP_ANNOUNCEMENT_CHAT_ID` | yes | the Announcements group id, `<digits>@g.us` |
| `FRED_API_KEY` | yes | needed for PCE, GDP, retail sales, jobless claims and the Fed funds rate |
| `BLS_API_KEY` | no | the code works without it; add one to lift the 25-requests-a-day limit, which on shared cloud addresses is easier to hit |

`DATABASE_BACKEND` is not a secret; the workflow sets it to `postgres` itself.
Secret values live only in GitHub's encrypted store. They are never written
to the repository, and GitHub masks them in logs.

On the runner there is no `.env` file. The application reads the same
variable names straight from the environment, so nothing else changes.

### Preflight

```bash
python -m src.preflight
```

Prints the Python version, the database backend and `PRESENT` or `MISSING`
for each setting. It never prints a value, opens no connection and sends
nothing. With `--production` it exits with an error unless the backend is
`postgres` and every required setting is present; the workflow uses that form
so a missing secret stops the run before anything else happens.

### The safe test workflow

Start it from the repository's **Actions** tab: choose **Safe test (sends
nothing)**, then **Run workflow**. It:

1. checks out the code and installs `requirements.txt` on Python 3.11
2. runs the test suite, with no secret available to it
3. runs the preflight in production mode
4. runs `--whatsapp-check` (connection and destination; sends nothing)
5. runs `--enrich-actuals --week --dry-run` (asks BLS/FRED, writes nothing)
6. runs the four `--whatsapp-send-...` commands with `--dry-run`

It cannot send a message: every send command carries `--dry-run`, the
dry-run steps are not even given `WHAPI_TOKEN`, `--whatsapp-test` is not
used, and the only trigger is `workflow_dispatch`.

### The production workflow

`production.yml` runs on a schedule and can also be started by hand. GitHub
cron is in UTC; India time is UTC+5:30 all year.

| Cron (UTC) | India time | Runs a day |
| --- | --- | --- |
| `15,45 2-19 * * *` | 07:45, 08:15, 08:45 ... 00:45, 01:15 | 36 |

GitHub does not guarantee that a scheduled run starts on time, or at all, and
dropped runs do happen. So no message depends on one particular trigger.
Every run works out what is due from the India clock (`src/runplan.py`):

| India time | What the run does |
| --- | --- |
| any time | collects, enriches, sends newly released results |
| 08:15 to 16:00 | also sends the daily brief and today's high-impact alerts |
| 21:15 to 24:00 | also sends tomorrow's reminders |

Because the application never sends the same message twice, a run that
arrives late simply catches up, and the runs after it do nothing. If the
08:15 run is dropped, the 08:45 run sends the brief instead.

Each run uses the existing commands, in this order:

1. `python -m src.runplan` reads the clock and decides the duties above.
2. `python -m src.preflight --production` stops the run if a setting is missing.
3. `--whatsapp-check` confirms the WhatsApp session and that the destination
   is still the Community's Announcements group. To stay within Whapi's
   request allowance it runs once at the start of each window (08:15 and
   21:15) and on manual runs. If it fails, nothing is sent.
4. `--enrich-actuals --week` downloads the Forex Factory calendar, stores and
   classifies it, and asks BLS and FRED for released figures.
5. The sends that are due:
   - morning window: `--whatsapp-send-morning`, then `--whatsapp-send-alert --today`
   - every run: `--whatsapp-send-actuals --from <yesterday>`
   - evening window: `--whatsapp-send-upcoming --tomorrow`

Alerts are limited to today and reminders to tomorrow, so neither is ever
posted for the whole week at once. Results reach back to yesterday (India
time) so a figure released shortly before midnight is still picked up by the
next run.

Started by hand, the workflow follows the clock in the same way (`auto`), or
you can force `morning`, `evening` or `polling`. It never forces a message:
the same selection rules and duplicate check apply.

**Failures.** A database error, a missing setting, a failed WhatsApp send or a
failed destination check ends the run with an error. The application itself
carries on from stored data when the Forex Factory download is refused, and
marks an event `FAILED` when BLS or FRED cannot be reached; the last step of
the workflow turns either case into a failed run, so it is not missed. GitHub
emails the repository owner when a scheduled run fails. Nothing is retried
within a run; the next scheduled run simply tries again.

**Stopping it.** In the Actions tab open **Production**, then the "..." menu,
then **Disable workflow**; or run:

```bash
gh workflow disable production.yml
```

`gh workflow enable production.yml` turns it back on.

### One run at a time

Both workflows belong to the concurrency group `market-news-automation` with
`cancel-in-progress: false`. A second run waits until the first has finished,
and a run in progress is never cancelled, so two runs can never send at once;
that closes the simultaneous-run gap noted under WhatsApp delivery. GitHub
keeps at most one run waiting per group: if several pile up, only the newest
waiting one is kept.

### First push

1. Create an **empty private** repository on GitHub (no README or licence).
2. In this folder:

   ```bash
   git remote add origin https://github.com/YOUR-USER/YOUR-REPO.git
   ```

   ```bash
   git push -u origin main
   ```

3. Add the repository secrets listed above.
4. Run the **Safe test** workflow and read its log.

### Limits worth knowing

- Results are posted by the first run after a figure is published, so they
  can arrive up to about half an hour after the release, or later if GitHub
  starts the run late.
- Whapi's free Sandbox plan allows 1,000 API requests a month. A day of this
  schedule uses four for the destination checks plus one per message sent.
- 36 runs a day is roughly 1,100 to 1,500 runner minutes a month, inside the
  2,000 a private repository gets on GitHub's free plan. Do not shorten the
  interval without checking that budget.
- Without a BLS key the BLS limit is 25 requests a day, counted against
  addresses shared with other GitHub users.
- Forex Factory allows 2 downloads per 5 minutes, also counted per address.
  Runs are scheduled 30 minutes apart.

### Not built

- Telegram

## Database schema

Four tables with the same columns in both backends: `events` (Step 1),
`event_classifications` (Step 2), `event_actuals` (Step 3) and
`message_deliveries` (Step 5).

### events

| Column | PostgreSQL | SQLite | Notes |
| --- | --- | --- | --- |
| `event_id` | `TEXT PRIMARY KEY` | `TEXT PRIMARY KEY` | hash of source timestamp + currency + title |
| `date` | `DATE` | `TEXT` | in the zone named by `timezone` |
| `time` | `TIME` | `TEXT` | in the zone named by `timezone` |
| `timezone` | `TEXT` | `TEXT` | null when the source gave no offset |
| `datetime_utc` | `TIMESTAMPTZ` | `TEXT` | the exact instant |
| `currency` | `TEXT NOT NULL` | `TEXT NOT NULL` | |
| `event_name` | `TEXT NOT NULL` | `TEXT NOT NULL` | |
| `impact` | `TEXT NOT NULL` + check | `TEXT NOT NULL` | High, Medium, Low, Holiday or None |
| `original_impact` | `TEXT` | `TEXT` | as Forex Factory sent it |
| `gold_relevance` | `BOOLEAN NOT NULL` | `INTEGER NOT NULL` | |
| `forecast`, `previous`, `actual` | `TEXT` | `TEXT` | source strings such as `0.3%`, `200K`; null when absent |
| `source`, `source_url` | `TEXT NOT NULL` | `TEXT NOT NULL` | |
| `retrieved_at` | `TIMESTAMPTZ NOT NULL` | `TEXT NOT NULL` | last time the source was read |
| `updated_at` | `TIMESTAMPTZ NOT NULL` | `TEXT NOT NULL` | last time the content changed |

PostgreSQL indexes: the primary key, `(date, currency)` for date-window
queries and cleanup, and `(datetime_utc)` for stale-event removal. The table
holds a few hundred rows at most, so no others are needed.

**Duplicates.** `event_id` is the primary key and every write is an
`INSERT ... ON CONFLICT (event_id) DO UPDATE`, so processing the same feed any
number of times leaves one row per event. `updated_at` only moves when
something actually changed. A stored `actual` is never replaced by a missing
one.

**Rescheduled events.** The feed has no event ID, so a rescheduled event gets
a new ID. After each sync, rows inside the feed's week that the feed no longer
lists are removed, so the old row does not linger.

### event_classifications

One row per event, linked by `event_id`. It holds derived data only and never
alters the source row.

| Column | PostgreSQL | Notes |
| --- | --- | --- |
| `event_id` | `TEXT PRIMARY KEY`, references `events` | deleted with its event |
| `gold_relevance` | `BOOLEAN NOT NULL` | true unless the level is NONE (checked) |
| `gold_relevance_level` | `TEXT NOT NULL` + check | STRONG, MODERATE, WEAK, NONE |
| `gold_relevance_reason` | `TEXT NOT NULL` | from the matching rule |
| `category` | `TEXT NOT NULL` | from the list in the rule file |
| `priority` | `TEXT NOT NULL` + check | CRITICAL, HIGH, MEDIUM, LOW |
| `priority_score` | `INTEGER NOT NULL` + check | 0 to 100 |
| `highlight_required` | `BOOLEAN NOT NULL` | |
| `classification_reason` | `TEXT NOT NULL` | how the score and priority were reached |
| `classification_version` | `TEXT NOT NULL` | rule version used |
| `classified_at` | `TIMESTAMPTZ NOT NULL` | last time the rules were applied |
| `updated_at` | `TIMESTAMPTZ NOT NULL` | last time the result changed |

SQLite uses `TEXT` and `INTEGER` for the same columns. Because of the foreign
key, retention cleanup and stale-event removal take the classification away
with the event. Row level security is enabled here as on `events`.

After upgrading from Step 1, run `python -m src.main --init-db` once on
PostgreSQL to create this table (SQLite creates it automatically). The
SQLite-to-PostgreSQL migration copies events only; run `--classify` afterwards
to fill this table.

### event_actuals

One row per event, linked by `event_id` and deleted with its event. The Actual
value itself is stored in `events.actual`; this table describes it.

| Column | PostgreSQL | Notes |
| --- | --- | --- |
| `event_id` | `TEXT PRIMARY KEY`, references `events` | |
| `release_status` | `TEXT NOT NULL` + check | UPCOMING, RELEASED, NO_DATA, FAILED |
| `status_reason` | `TEXT NOT NULL` | why there is no value; empty otherwise |
| `actual_source` | `TEXT` | provider, for example `BLS` |
| `actual_source_event` | `TEXT` | the provider's series |
| `actual_period` | `TEXT` | period the value refers to, for example `2026-09` |
| `actual_revision` | `INTEGER NOT NULL` | 0 until released, then 1, 2, ... (checked against the status) |
| `surprise_status` | `TEXT NOT NULL` + check | see above |
| `surprise_value` | `DOUBLE PRECISION` | Actual minus Forecast |
| `actual_retrieved_at` | `TIMESTAMPTZ` | first time a value was obtained |
| `actual_updated_at` | `TIMESTAMPTZ` | last time the value changed |
| `updated_at` | `TIMESTAMPTZ NOT NULL` | last time this row changed |

A separate table was chosen over extra columns on `events` because every
calendar sync rewrites the `events` row from the feed; keeping enrichment data
apart means a sync can never disturb it. After upgrading, run
`python -m src.main --init-db` once on PostgreSQL to create it.

### message_deliveries

One row per message, provider and destination.

| Column | PostgreSQL | Notes |
| --- | --- | --- |
| `message_key` | `TEXT`, part of the primary key | the Step 4 message identity |
| `provider` | `TEXT`, part of the primary key | `whapi` |
| `destination_id` | `TEXT`, part of the primary key | the WhatsApp chat id |
| `destination` | `TEXT NOT NULL` | `whatsapp_community_announcement` |
| `message_type` | `TEXT NOT NULL` | for example `MORNING_UPDATE` |
| `status` | `TEXT NOT NULL` + check | `SENT` or `FAILED` |
| `provider_message_id` | `TEXT` | Whapi's message id |
| `sent_at` | `TIMESTAMPTZ` | set when sent |
| `error` | `TEXT` | last failure, if any |
| `attempts` | `INTEGER NOT NULL` | how many times sending was tried |
| `created_at`, `updated_at` | `TIMESTAMPTZ NOT NULL` | |

It is not linked to `events`, because a daily update covers several events
and the record of what was sent should outlive calendar retention. The
message text is not stored.

**Later tables.** Tables such as `published_messages`, `message_deliveries` or
`event_alerts` can reference `events.event_id`. Because retention deletes old
events, decide then whether such a reference should cascade, be set to null,
or keep its own copy of the event details.

## Retention

Events dated more than `CALENDAR_RETENTION_DAYS` days before today are
deleted, where "today" is the current date in `DISPLAY_TIMEZONE`. With the
default of 14, a run on 8 October keeps everything from 24 September onward.

Cleanup runs automatically at the end of every successful sync, and on demand:

```bash
python -m src.main --cleanup
```

## Migration from SQLite

```bash
python -m src.main --migrate-to-postgres
```

Copies every event from `SQLITE_PATH` into the database at `DATABASE_URL`,
creating the table first if needed. It works whatever `DATABASE_BACKEND` is
set to. Rows already in PostgreSQL are left untouched, so it is safe to run
more than once and it never overwrites newer production data. It is optional:
a normal sync fills an empty database with the current week anyway.

## Data source

Forex Factory's own weekly export,
`https://nfs.faireconomy.media/ff_calendar_thisweek.json`. It is free and
needs no key.

- **Rate limit: 2 downloads per 5 minutes**, and the file only changes about
  once an hour. The collector downloads at most once per
  `MIN_FETCH_INTERVAL_SECONDS` and otherwise reuses the cached copy.
- **Current week only** (Sunday to Saturday).
- **No `actual` values in the feed.** Step 3 fills `actual` from official
  sources for the events that have a mapping; for the rest it stays null.

## Impact vs. Gold relevance

- `impact` is Forex Factory's rating, normalized to `High`, `Medium`, `Low`,
  `Holiday` or `None`. The untouched source value is in `original_impact`.
- `gold_relevance` is this project's own tag, decided by the keyword list in
  [config/gold_relevance.json](config/gold_relevance.json). Edit that file to
  add or remove event names; the next run reclassifies everything.

## Using it from code

```python
from src.config import Settings
from src.database.factory import open_database
from src.filters import gold_usd_filters as filters
from src.pipeline import cleanup_old_events, sync

settings = Settings.from_env()
with open_database(settings) as db:
    sync(settings, db)
    cleanup_old_events(settings, db)
    for event in filters.get_high_impact_gold_events(db, "2026-10-04", "2026-10-10"):
        print(event.to_dict())
```

## Layout

```
src/collector/forex_factory.py     download the export
src/collector/parser.py            raw JSON -> normalized events
src/collector/models.py            the Event record
src/filters/gold_usd_filters.py    Gold relevance rules and query helpers
src/classification/rules.py        rule engine: reads the rule file and applies it
src/classification/service.py      classify stored events, load them with results
src/classification/models.py       the Classification record
src/actuals/mapping.py             event-to-series mapping, reporting period, value formatting
src/actuals/providers.py           ActualDataProvider, BLS and FRED
src/actuals/surprise.py            Actual-against-Forecast comparison
src/actuals/service.py             enrichment: release status, storing the Actual
src/actuals/models.py              the ActualRecord
src/content/headlines.py           headline generation from headline_rules.json
src/content/templates.py           template loading, validation and rendering
src/content/builder.py             selects events and builds the four message types
src/content/fixtures.py            sample events for --fixture previews
src/content/models.py              the Message structure
src/delivery/whapi.py              Whapi.Cloud client (the only code that talks to WhatsApp)
src/delivery/service.py            sends a message once and records the outcome
src/delivery/models.py             the DeliveryRecord
src/database/base.py               EventRepository: all storage behaviour
src/database/database.py           SQLite backend
src/database/postgres.py           PostgreSQL backend
src/database/postgres_schema.sql   PostgreSQL table definition
src/database/factory.py            picks the backend from configuration
src/database/migrate.py            SQLite -> PostgreSQL copy
src/pipeline.py                    sync() and cleanup_old_events()
src/main.py                        command line
config/gold_relevance.json         Step 1 Gold keyword list
config/gold_priority_rules.json    Step 2 classification rules
config/actual_event_mapping.json   Step 3 event-to-source mapping
config/message_templates.json      Step 4 templates, labels, selection rules
config/headline_rules.json         Step 4 headline wording
fixtures/                          saved real export used by the tests
data/, logs/                       local files, not committed
```

## Tests

```bash
python -m pytest
```

The suite runs offline and needs no Supabase account. Two groups are opt-in:

- `TEST_DATABASE_URL=postgresql://...` also runs the storage tests against a
  real PostgreSQL server. Use a direct or session-mode connection. The tests
  work in a temporary schema and do not touch the real `events` table.
- `RUN_LIVE_TESTS=1` makes one real request to Forex Factory and one to the
  BLS API.

Provider tests otherwise use saved responses: a real BLS response in
`fixtures/bls_timeseries_sample.json`, and hand-written FRED payloads.
