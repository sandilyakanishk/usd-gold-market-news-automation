"""Classification storage on every backend (SQLite always; PostgreSQL when TEST_DATABASE_URL is set)."""

import json
from dataclasses import replace

import pytest

from src.classification.models import Classification
from src.classification.service import classify_events, load_classified
from src.config import PROJECT_ROOT
from src.database.base import DatabaseError

from .test_repository_contract import loaded, repo, usd_events  # noqa: F401  (fixtures)

T1, T2, T3 = "2026-10-08T01:00:00Z", "2026-10-08T02:00:00Z", "2026-10-08T03:00:00Z"


def edited_rules(tmp_path, change):
    config = json.loads((PROJECT_ROOT / "config" / "gold_priority_rules.json").read_text(encoding="utf-8"))
    change(config)
    path = tmp_path / "rules.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    return path


def by_name(repo, name):
    (row,) = [r for r in load_classified(repo) if r.event.event_name == name]
    return row


def test_events_are_unclassified_until_classify_runs(loaded):
    assert loaded.count_classifications() == 0
    assert all(r.classification is None for r in load_classified(loaded))


def test_classify_stores_one_row_per_event(loaded, settings):
    result = classify_events(settings, loaded, now=T1)
    assert (result.events, result.inserted, result.updated, result.unchanged) == (23, 23, 0, 0)
    assert (result.version, result.unmatched) == ("1.0.0", [])
    assert loaded.count_classifications() == 23 == loaded.count()


def test_stored_classification_round_trips_exactly(loaded, settings):
    classify_events(settings, loaded, now=T1)
    stored = by_name(loaded, "FOMC Meeting Minutes").classification
    assert stored == Classification(
        event_id=stored.event_id,
        gold_relevance=True,
        gold_relevance_level="STRONG",
        gold_relevance_reason="Fed Chair or full-committee communication that can shift interest-rate expectations.",
        category="FED_COMMUNICATION",
        priority="HIGH",
        priority_score=85,
        highlight_required=True,
        classification_reason=(
            "Priority HIGH: score 85/100 = Forex Factory impact High (30) + Gold relevance STRONG (40) "
            "+ high-priority event (15). Highlighted by an explicit rule for this event."),
        classification_version="1.0.0",
        classified_at=T1,
        updated_at=T1,
    )
    assert isinstance(stored.gold_relevance, bool) and isinstance(stored.highlight_required, bool)
    assert isinstance(stored.priority_score, int)
    none = by_name(loaded, "Crude Oil Inventories").classification
    assert (none.gold_relevance, none.highlight_required) == (False, False)


def test_classification_is_idempotent(loaded, settings):
    classify_events(settings, loaded, now=T1)
    before = {r.event.event_id: r.classification for r in load_classified(loaded)}
    for moment in (T2, T3):
        result = classify_events(settings, loaded, now=moment)
        assert (result.inserted, result.updated, result.unchanged) == (0, 0, 23)
    assert loaded.count_classifications() == 23
    for row in load_classified(loaded):
        old = before[row.event.event_id]
        assert row.classification == replace(old, classified_at=T3)  # content and updated_at untouched
        assert row.classification.updated_at == T1


def test_source_events_are_not_modified_by_classification(loaded, settings):
    before = loaded.query_events()
    classify_events(settings, loaded, now=T1)
    assert loaded.query_events() == before


def test_reclassification_after_a_rule_change_updates_in_place(loaded, settings, tmp_path):
    classify_events(settings, loaded, now=T1)

    def promote_claims(config):
        config["moderate_gold_events"][0]["events"].remove("Unemployment Claims")
        config["strong_gold_events"][1]["events"].append("Unemployment Claims")

    changed = replace(settings, priority_rules_path=edited_rules(tmp_path, promote_claims))
    result = classify_events(changed, loaded, now=T2)
    assert (result.inserted, result.updated, result.unchanged) == (0, 1, 22)
    assert loaded.count_classifications() == 23
    claims = by_name(loaded, "Unemployment Claims").classification
    assert (claims.gold_relevance_level, claims.priority, claims.priority_score) == ("STRONG", "HIGH", 75)
    assert (claims.updated_at, claims.classified_at) == (T2, T2)
    other = by_name(loaded, "ISM Services PMI").classification
    assert (other.updated_at, other.classified_at) == (T1, T2)


def test_new_rule_version_is_recorded_on_every_row(loaded, settings, tmp_path):
    classify_events(settings, loaded, now=T1)
    bumped = replace(settings, priority_rules_path=edited_rules(
        tmp_path, lambda config: config.update(classification_version="1.1.0")))
    result = classify_events(bumped, loaded, now=T2)
    assert (result.version, result.inserted, result.updated, result.unchanged) == ("1.1.0", 0, 23, 0)
    assert {r.classification.classification_version for r in load_classified(loaded)} == {"1.1.0"}
    assert loaded.count_classifications() == 23


def test_classification_is_removed_with_its_event(loaded, settings):
    classify_events(settings, loaded, now=T1)
    removed = loaded.cleanup_old_events("2026-10-08")
    assert removed == 13
    assert loaded.count() == 10 == loaded.count_classifications()
    assert loaded.delete_missing([], "2026-10-01T00:00:00Z", "2026-10-31T00:00:00Z") == 10
    assert loaded.count_classifications() == 0


def test_classification_for_an_unknown_event_is_rejected(loaded, settings):
    classify_events(settings, loaded, now=T1)
    orphan = replace(by_name(loaded, "Trade Balance").classification, event_id="ff-does-not-exist")
    with pytest.raises(DatabaseError):
        loaded.upsert_classifications([orphan])
    assert loaded.count_classifications() == 23
    assert loaded.get_classification("ff-does-not-exist") is None


def test_fixture_week_distribution(loaded, settings):
    classify_events(settings, loaded, now=T1)
    done = [r.classification for r in load_classified(loaded)]
    count = lambda field, value: sum(getattr(c, field) == value for c in done)
    assert [count("gold_relevance_level", v) for v in ("STRONG", "MODERATE", "WEAK", "NONE")] == [1, 9, 4, 9]
    assert [count("priority", v) for v in ("CRITICAL", "HIGH", "MEDIUM", "LOW")] == [0, 1, 5, 17]
    assert sum(c.gold_relevance for c in done) == 14
    assert [r.event.event_name for r in load_classified(loaded, highlight_only=True)] == ["FOMC Meeting Minutes"]


def test_load_classified_filters(loaded, settings):
    classify_events(settings, loaded, now=T1)
    names = lambda **kw: [r.event.event_name for r in load_classified(loaded, **kw)]
    assert names(priorities=["HIGH"]) == ["FOMC Meeting Minutes"]
    assert names(priorities=["CRITICAL"]) == []
    assert names(priorities=["MEDIUM"]) == [
        "ISM Services PMI", "FOMC Member Waller Speaks", "Unemployment Claims",
        "Prelim UoM Consumer Sentiment", "Prelim UoM Inflation Expectations"]
    assert len(names(priorities=["HIGH", "MEDIUM"])) == 6
    assert len(names(gold_only=True)) == 14
    assert names(gold_only=True, impacts=["High"]) == ["FOMC Meeting Minutes"]
    assert names(gold_only=True, date_from="2026-10-08", date_to="2026-10-08") == [
        "FOMC Member Waller Speaks", "Unemployment Claims", "FOMC Member Musalem Speaks"]


def test_unmatched_event_names_are_reported(repo, settings, usd_events):
    repo.upsert_events([replace(usd_events[0], event_id="ff-new", event_name="Brand New Indicator")])
    result = classify_events(settings, repo, now=T1)
    assert result.unmatched == ["Brand New Indicator"]
    c = repo.get_classification("ff-new")
    assert (c.gold_relevance_level, c.category, c.priority) == ("NONE", "OTHER", "LOW")
