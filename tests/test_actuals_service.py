"""Enrichment end to end with a fixture provider, on every backend
(SQLite always; PostgreSQL when TEST_DATABASE_URL is set)."""

from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from src.actuals.providers import ActualDataProvider, ProviderError
from src.actuals.service import (
    REASON_NO_NUMERIC_RESULT, REASON_NOT_MAPPED, REASON_TOO_OLD, enrich_actuals, is_due, load_enriched,
)
from src.classification.service import classify_events
from src.collector.models import Event
from src.collector.parser import make_event_id

from .test_repository_contract import repo  # noqa: F401  (fixture)

D = Decimal


class FixtureProvider(ActualDataProvider):
    """Stands in for a real source: returns canned observations and records what it was asked."""

    def __init__(self, name, observations=None, error=None, unavailable=None):
        self.name, self.observations, self.error, self.unavailable = name, observations or {}, error, unavailable
        self.requests = []

    def unavailable_reason(self):
        return self.unavailable

    def fetch(self, requests):
        self.requests.append(requests)
        if self.error:
            raise ProviderError(self.error)
        return {r.series_id: dict(self.observations.get(r.series_id, {})) for r in requests}


def bls_data():
    return {
        "CUSR0000SA0": {date(2026, 8, 1): D("332.813"), date(2026, 9, 1): D("334.131")},      # +0.4%
        "CUSR0000SA0L1E": {date(2026, 8, 1): D("336.789"), date(2026, 9, 1): D("337.765")},   # +0.3%
        "CES0000000001": {date(2026, 8, 1): D("159015"), date(2026, 9, 1): D("159044")},      # +29K
        "LNS14000000": {date(2026, 9, 1): D("4.2")},
    }


def fred_data():
    return {"ICSA": {date(2026, 10, 3): D("218000")}}


def make(name, when_utc, forecast=None, previous=None, currency="USD", impact="High"):
    """A stored Forex Factory event. `when_utc` is the exact release instant, e.g. '2026-10-14T12:30:00Z'."""
    instant = datetime.fromisoformat(when_utc.replace("Z", "+00:00"))
    local = instant.astimezone(timezone.utc)  # display fields are irrelevant to enrichment
    return Event(
        event_id=make_event_id(when_utc, currency, name), date=local.strftime("%Y-%m-%d"), time=local.strftime("%H:%M"),
        timezone="UTC", datetime_utc=when_utc, currency=currency, event_name=name, impact=impact, original_impact=impact,
        gold_relevance=False, forecast=forecast, previous=previous, actual=None,
        source="Forex Factory", source_url="synthetic-test-fixture", retrieved_at="2026-10-01T00:00:00Z",
    )


CPI = make("CPI m/m", "2026-10-14T12:30:00Z", forecast="0.3%", previous="0.4%")
CORE_CPI = make("Core CPI m/m", "2026-10-14T12:30:00Z", forecast="0.3%", previous="0.3%")
NFP = make("Non-Farm Employment Change", "2026-10-02T12:30:00Z", forecast="50K", previous="22K")
UNEMPLOYMENT = make("Unemployment Rate", "2026-10-02T12:30:00Z", forecast=None, previous="4.1%")
CLAIMS = make("Unemployment Claims", "2026-10-08T12:30:00Z", forecast="200K", previous="197K", impact="Medium")
SPEECH = make("FOMC Member Waller Speaks", "2026-10-08T08:30:00Z", impact="Medium")
ISM = make("ISM Services PMI", "2026-10-05T14:00:00Z", forecast="55.1", previous="55.4", impact="Medium")
ALL = [CPI, CORE_CPI, NFP, UNEMPLOYMENT, CLAIMS, SPEECH, ISM]

AFTER_ALL = datetime(2026, 10, 14, 13, 0, tzinfo=timezone.utc)
T1 = "2026-10-14T13:00:00Z"


@pytest.fixture
def db(repo):  # noqa: F811
    repo.upsert_events(ALL, now="2026-10-01T00:00:00Z")
    return repo


@pytest.fixture
def sources():
    return {"BLS": FixtureProvider("BLS", bls_data()), "FRED": FixtureProvider("FRED", fred_data())}


def run(settings, db, sources, now=AFTER_ALL, retry_days=60, **kwargs):
    """Enrich with the fixture sources. The retry window is wide open unless a test narrows it."""
    return enrich_actuals(replace(settings, actuals_retry_days=retry_days), db, providers=sources, now=now, **kwargs)


def row(result, event):
    return next(r for r in result.rows if r.event.event_id == event.event_id)


# -- release status ------------------------------------------------------------------

def test_upcoming_events_are_not_released_and_no_source_is_asked(settings, db, sources):
    before_everything = datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc)
    result = run(settings, db, sources, now=before_everything)
    assert {r.record.release_status for r in result.rows} == {"UPCOMING"}
    assert all(r.record.status_reason == "" and r.event.actual is None for r in result.rows)
    assert sources["BLS"].requests == [] and sources["FRED"].requests == []
    assert result.provider_calls == {} and result.actuals_written == 0


def test_an_event_becomes_due_exactly_at_its_release_instant(settings, db, sources):
    one_second_before = datetime(2026, 10, 14, 12, 29, 59, tzinfo=timezone.utc)
    assert row(run(settings, db, sources, now=one_second_before), CPI).record.release_status == "UPCOMING"
    at_release = datetime(2026, 10, 14, 12, 30, 0, tzinfo=timezone.utc)
    assert row(run(settings, db, sources, now=at_release), CPI).record.release_status == "RELEASED"


def test_due_time_uses_the_utc_instant_not_the_display_timezone():
    from datetime import timedelta
    # 18:00 in India on the 14th is 12:30 UTC. At 15:00 India time (09:30 UTC) it has not happened yet,
    # even though the stored local date "2026-10-14" has already begun.
    india = replace(CPI, date="2026-10-14", time="18:00", timezone="Asia/Kolkata")
    ist = timezone(timedelta(hours=5, minutes=30))
    assert not is_due(india, datetime(2026, 10, 14, 15, 0, tzinfo=ist))
    assert is_due(india, datetime(2026, 10, 14, 18, 0, tzinfo=ist))
    # An event with no known instant is only due once its date is clearly over.
    undated = replace(CPI, datetime_utc=None, date="2026-10-14")
    assert not is_due(undated, datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc))
    assert is_due(undated, datetime(2026, 10, 16, 0, 1, tzinfo=timezone.utc))


def test_released_event_gets_its_actual_source_and_surprise(settings, db, sources):
    result = run(settings, db, sources)
    cpi = db.get_event(CPI.event_id)
    record = db.get_actual_records([CPI.event_id])[CPI.event_id]
    assert cpi.actual == "0.4%"
    assert (record.release_status, record.status_reason) == ("RELEASED", "")
    assert record.actual_source == "BLS"
    assert record.actual_source_event == "CUSR0000SA0: CPI-U, all items, seasonally adjusted, 1-month percent change"
    assert (record.actual_period, record.actual_revision) == ("2026-09", 1)
    assert (record.surprise_status, record.surprise_value) == ("ABOVE_FORECAST", pytest.approx(0.1))
    assert (record.actual_retrieved_at, record.actual_updated_at, record.updated_at) == (T1, T1, T1)
    assert row(result, CPI).event.actual == "0.4%"
    assert result.actuals_written == 5 and result.provider_calls == {"BLS": 1, "FRED": 1}


def test_all_statuses_in_one_run(settings, db, sources):
    result = run(settings, db, sources)
    seen = {r.event.event_name: (r.record.release_status, r.event.actual, r.record.surprise_status) for r in result.rows}
    assert seen == {
        "CPI m/m": ("RELEASED", "0.4%", "ABOVE_FORECAST"),
        "Core CPI m/m": ("RELEASED", "0.3%", "IN_LINE_WITH_FORECAST"),
        "Non-Farm Employment Change": ("RELEASED", "29K", "BELOW_FORECAST"),
        "Unemployment Rate": ("RELEASED", "4.2%", "NOT_AVAILABLE"),   # no forecast to compare with
        "Unemployment Claims": ("RELEASED", "218K", "ABOVE_FORECAST"),
        "FOMC Member Waller Speaks": ("NO_DATA", None, "NOT_AVAILABLE"),
        "ISM Services PMI": ("NO_DATA", None, "NOT_AVAILABLE"),
    }
    assert row(result, SPEECH).record.status_reason == REASON_NO_NUMERIC_RESULT
    assert row(result, ISM).record.status_reason == REASON_NOT_MAPPED
    assert row(result, NFP).record.surprise_value == -21.0
    assert row(result, CLAIMS).record.actual_source == "FRED"
    assert row(result, CLAIMS).record.actual_period == "2026-10-03"


def test_no_data_when_the_source_has_not_published_the_period(settings, db, sources):
    sources["BLS"].observations["CUSR0000SA0"].pop(date(2026, 9, 1))
    result = run(settings, db, sources)
    cpi = row(result, CPI)
    assert (cpi.record.release_status, cpi.event.actual) == ("NO_DATA", None)
    assert cpi.record.status_reason == "BLS has not published CUSR0000SA0 for 2026-09 yet."
    assert db.get_event(CPI.event_id).actual is None
    assert row(result, CORE_CPI).record.release_status == "RELEASED"  # other series are unaffected
    # Once it is published, the next run picks it up.
    sources["BLS"].observations["CUSR0000SA0"][date(2026, 9, 1)] = D("334.131")
    assert row(run(settings, db, sources), CPI).record.release_status == "RELEASED"
    assert db.get_event(CPI.event_id).actual == "0.4%"


def test_an_older_release_is_never_used_for_a_newer_event(settings, db, sources):
    """The source only has August's figure; September's event must not receive it."""
    sources["BLS"].observations["CUSR0000SA0"] = {date(2026, 7, 1): D("331.0"), date(2026, 8, 1): D("332.813")}
    assert row(run(settings, db, sources), CPI).record.release_status == "NO_DATA"
    assert db.get_event(CPI.event_id).actual is None


def test_provider_failure_is_failed_not_no_data(settings, db, sources):
    sources["BLS"].error = "Could not reach BLS: timed out"
    result = run(settings, db, sources)
    cpi = row(result, CPI)
    assert (cpi.record.release_status, cpi.record.status_reason, cpi.event.actual) == (
        "FAILED", "Could not reach BLS: timed out", None)
    assert row(result, CLAIMS).record.release_status == "RELEASED"  # the other provider still works
    assert row(result, SPEECH).record.release_status == "NO_DATA"   # no provider involved
    assert db.get_event(CPI.event_id).actual is None
    # The failure clears on the next successful run.
    sources["BLS"].error = None
    assert row(run(settings, db, sources), CPI).record.release_status == "RELEASED"


def test_missing_api_key_is_no_data_with_a_clear_reason(settings, db, sources):
    sources["FRED"].unavailable = "FRED_API_KEY is not set; this event's source needs that free key."
    result = run(settings, db, sources)
    claims = row(result, CLAIMS)
    assert (claims.record.release_status, claims.event.actual) == ("NO_DATA", None)
    assert "FRED_API_KEY is not set" in claims.record.status_reason
    assert sources["FRED"].requests == []
    assert "FRED" not in result.provider_calls


def test_old_unpublished_events_stop_being_retried(settings, db, sources):
    sources["BLS"].observations["CES0000000001"] = {}
    late = datetime(2026, 10, 20, 0, 0, tzinfo=timezone.utc)  # 18 days after the payrolls release
    result = run(settings, db, sources, now=late, retry_days=7)
    assert (row(result, NFP).record.release_status, row(result, NFP).record.status_reason) == ("NO_DATA", REASON_TOO_OLD)
    asked = {r.series_id for batch in sources["BLS"].requests for r in batch}
    assert "CES0000000001" not in asked and "CUSR0000SA0" in asked


# -- idempotency and data safety -----------------------------------------------------

def test_repeated_enrichment_changes_nothing(settings, db, sources):
    first = run(settings, db, sources)
    assert (first.inserted, first.updated, first.unchanged) == (7, 0, 0)
    events_before = {e.event_id: e for e in db.query_events()}
    records_before = db.get_actual_records(list(events_before))
    calls_before = (len(sources["BLS"].requests), len(sources["FRED"].requests))

    later = datetime(2026, 10, 14, 18, 0, tzinfo=timezone.utc)
    for _ in range(3):
        again = run(settings, db, sources, now=later)
        assert (again.inserted, again.updated, again.unchanged, again.actuals_written) == (0, 0, 7, 0)
    assert db.count() == 7 == db.count_actual_records()
    assert {e.event_id: e for e in db.query_events()} == events_before      # not even a timestamp moved
    assert db.get_actual_records(list(events_before)) == records_before
    # Released events are not looked up again, and nothing else here has a source to ask.
    assert (len(sources["BLS"].requests), len(sources["FRED"].requests)) == calls_before


def test_event_identity_forecast_and_previous_are_untouched(settings, db, sources):
    before = {e.event_id: e for e in db.query_events()}
    run(settings, db, sources)
    after = {e.event_id: e for e in db.query_events()}
    assert set(after) == set(before)
    for event_id, old in before.items():
        new = after[event_id]
        ignore = {"actual", "updated_at"}
        assert {k: v for k, v in new.to_dict().items() if k not in ignore} == {
            k: v for k, v in old.to_dict().items() if k not in ignore}
        assert (new.forecast, new.previous) == (old.forecast, old.previous)
    # The stored Actual is the source's figure, not a copy of the forecast or the previous value.
    assert after[NFP.event_id].actual == "29K" and "29K" not in (NFP.forecast, NFP.previous)
    assert after[CORE_CPI.event_id].actual == "0.3%"  # equal to the forecast only because the source says so


def test_actual_survives_a_later_calendar_sync(settings, db, sources):
    run(settings, db, sources)
    db.upsert_events(ALL)  # the feed still carries no actual
    assert db.get_event(CPI.event_id).actual == "0.4%"
    assert db.get_actual_records([CPI.event_id])[CPI.event_id].release_status == "RELEASED"


def test_released_value_is_not_downgraded_by_a_later_outage(settings, db, sources):
    run(settings, db, sources)
    sources["BLS"].error = "BLS answered HTTP 503."
    result = run(settings, db, sources, recheck_released=True)
    assert row(result, CPI).record.release_status == "RELEASED"
    assert db.get_event(CPI.event_id).actual == "0.4%"
    assert result.updated == 0


# -- revisions --------------------------------------------------------------------------

def test_revision_updates_the_existing_event(settings, db, sources):
    run(settings, db, sources)
    sources["BLS"].observations["CES0000000001"][date(2026, 9, 1)] = D("159070")  # revised: +55K instead of +29K

    # Without a recheck, a released value is left alone.
    untouched = run(settings, db, sources, now=datetime(2026, 11, 6, 13, 0, tzinfo=timezone.utc))
    assert untouched.updated == 0 and db.get_event(NFP.event_id).actual == "29K"

    revised = run(settings, db, sources, now=datetime(2026, 11, 6, 13, 0, tzinfo=timezone.utc), recheck_released=True)
    assert (revised.inserted, revised.updated, revised.actuals_written) == (0, 1, 1)
    record = db.get_actual_records([NFP.event_id])[NFP.event_id]
    assert db.get_event(NFP.event_id).actual == "55K"
    assert db.get_event(NFP.event_id).event_id == NFP.event_id
    assert (record.release_status, record.actual_revision) == ("RELEASED", 2)
    assert (record.surprise_status, record.surprise_value) == ("ABOVE_FORECAST", 5.0)
    assert (record.actual_retrieved_at, record.actual_updated_at) == (T1, "2026-11-06T13:00:00Z")
    assert db.count() == 7 == db.count_actual_records()
    # Unrevised events keep revision 1 and their timestamps.
    cpi = db.get_actual_records([CPI.event_id])[CPI.event_id]
    assert (cpi.actual_revision, cpi.actual_updated_at, cpi.updated_at) == (1, T1, T1)


# -- matching safety ---------------------------------------------------------------------

def test_non_usd_and_lookalike_events_are_never_enriched(settings, repo, sources):  # noqa: F811
    lookalikes = [
        make("CPI m/m", "2026-10-14T12:30:00Z", forecast="0.2%", currency="EUR"),
        make("CPI m/m", "2026-10-14T12:30:00Z", forecast="0.2%", currency="CAD"),
        make("Median CPI m/m", "2026-10-14T12:30:00Z", forecast="0.2%"),
        make("CPI q/q", "2026-10-14T12:30:00Z", forecast="0.2%"),
        make("Unemployment Rate Outlook", "2026-10-02T12:30:00Z"),
        make("ADP Non-Farm Employment Change", "2026-09-30T12:15:00Z", forecast="40K"),
    ]
    repo.upsert_events(lookalikes + [CPI])
    result = run(settings, repo, sources)
    assert row(result, CPI).record.release_status == "RELEASED"
    usd_lookalikes = [e for e in lookalikes if e.currency == "USD"]
    for event in usd_lookalikes:
        assert row(result, event).record.release_status == "NO_DATA", event.event_name
        assert row(result, event).record.status_reason == REASON_NOT_MAPPED
    # Other currencies are not even considered, and none of them received a value.
    assert {r.event.currency for r in result.rows} == {"USD"}
    assert all(e.actual is None for e in repo.query_events() if e.event_id != CPI.event_id)
    asked = {r.series_id for batch in sources["BLS"].requests for r in batch}
    assert asked == {"CUSR0000SA0"}


# -- dry run, selection, cleanup ----------------------------------------------------------

def test_dry_run_reports_but_writes_nothing(settings, db, sources):
    result = run(settings, db, sources, dry_run=True)
    assert result.dry_run and result.actuals_written == 5 and result.inserted == 7
    assert row(result, CPI).event.actual == "0.4%"                 # shown...
    assert db.get_event(CPI.event_id).actual is None                # ...but not stored
    assert db.count_actual_records() == 0
    assert all(e.actual is None for e in db.query_events())


def test_enrichment_can_be_limited_to_chosen_events_and_dates(settings, db, sources):
    result = run(settings, db, sources, event_ids={CPI.event_id})
    assert [r.event.event_name for r in result.rows] == ["CPI m/m"]
    assert db.count_actual_records() == 1 and sources["FRED"].requests == []
    by_date = run(settings, db, sources, date_from="2026-10-02", date_to="2026-10-02")
    assert {r.event.event_name for r in by_date.rows} == {"Non-Farm Employment Change", "Unemployment Rate"}


def test_enrichment_record_is_removed_with_its_event(settings, db, sources):
    run(settings, db, sources)
    assert db.cleanup_old_events("2026-10-10") == 5
    assert db.count() == 2 == db.count_actual_records()


def test_load_enriched_reads_without_any_source(settings, db, sources):
    unchecked = load_enriched(db, now=AFTER_ALL)
    assert {r.record.release_status for r in unchecked} == {"NO_DATA"}
    assert all("Not checked yet" in r.record.status_reason for r in unchecked)
    classify_events(settings, db)
    run(settings, db, sources)
    rows = {r.event.event_name: r for r in load_enriched(db, now=AFTER_ALL)}
    assert rows["CPI m/m"].record.release_status == "RELEASED" and rows["CPI m/m"].event.actual == "0.4%"
    assert rows["CPI m/m"].classification.priority == "CRITICAL"
    assert len(sources["BLS"].requests) == 1
