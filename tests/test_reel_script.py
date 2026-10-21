"""The owner's daily reel script and its private, encrypted page."""

import json
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from src import cards, content_cards, pulse, reel_script as rs
from src.database.database import SQLiteRepository

IST = ZoneInfo("Asia/Kolkata")
DAY = date(2026, 10, 21)
PASSWORD = "correct horse battery"
SETTINGS = SimpleNamespace(display_timezone="Asia/Kolkata", gemini_api_key=None, gemini_model="m", request_timeout_seconds=5,
                           user_agent="t")
ENV = {"VERCEL_TOKEN": "vercel-TEST-token", "REEL_PAGE_PASSWORD": PASSWORD, "REEL_PAGE_PROJECT": "test-reels"}


def ist(day, hour, minute=0):
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=IST)


@pytest.fixture
def db():
    with SQLiteRepository(":memory:") as repo:
        for i, value in enumerate([4100.0, 4120.0, 4090.0, 4130.0, 4110.0, 4125.0, 4118.0]):
            moment = ist(DAY, 10) + timedelta(hours=i)
            cards.record_price(repo, pulse.PriceQuote(value, moment.astimezone(timezone.utc), "s"), moment)
        yield repo


def fake_vercel(log):
    def call(method, path, token, payload=None):
        log.append((method, path, payload))
        if method == "POST":
            return {"id": "dpl_new", "projectId": "prj_1"}
        if method == "GET":
            return {"deployments": [{"uid": "dpl_new"}, {"uid": "dpl_old1"}, {"uid": "dpl_old2"}]}
        return {}
    return call


GOOD_AI = {"title": "CPI ne gold ko hila diya", "hook": "Kal US CPI aaya aur gold mein bada move dikha.",
           "lines": ["CPI aaya 0.4 percent, forecast tha 0.3.", "Dollar mazboot hua.", "Gold 4,090 tak gaya.",
                     "Phir 4,118 par close hua.", "Kal Retail Sales aayenge, dhyaan rakhna."]
           + ["Ye line script ko ek poore minute tak le jaane ke liye thodi lambi rakhi gayi hai taaki words poore hon."] * 7,
           "on_screen": ["US CPI 0.4%", "Gold close 4,118"], "caption": "Kal ka USD news aur gold recap. Sirf information. #gold"}


# -- timing --------------------------------------------------------------------------------

def test_the_day_written_about_is_today_after_the_recap_and_yesterday_before_it():
    assert rs.target_day(ist(DAY, 23, 35), IST) == DAY
    assert rs.target_day(ist(DAY, 23, 34), IST) == DAY - timedelta(days=1)
    assert rs.target_day(ist(DAY, 9, 0), IST) == DAY - timedelta(days=1)       # the morning still shows yesterday's


# -- facts and script ----------------------------------------------------------------------

def test_facts_come_from_stored_prices(db):
    facts = rs.day_facts(db, DAY, IST, now=ist(DAY, 23, 40))
    assert facts["gold"] == {"open": 4100.0, "high": 4130.0, "low": 4090.0, "close": 4118.0, "change": 18.0, "change_percent": 0.44}
    assert facts["releases"] == [] and facts["day"] == "2026-10-21" and facts["weekday"] == "Wednesday"


def test_the_template_version_states_only_facts_in_hinglish(db):
    script = rs.template_script(rs.day_facts(db, DAY, IST, now=ist(DAY, 23, 40)))
    assert script.source == "template" and "21 October" in script.title
    text = " ".join([script.hook, *script.lines])
    assert "4,100.00" in text and "4,118.00" in text and "upar" in text and "kal ka market" in text
    assert any(w in text.lower() for w in ("follow", "comment", "save", "share"))


def test_the_ai_writes_it_when_it_can_and_its_text_is_checked(db):
    facts = rs.day_facts(db, DAY, IST, now=ist(DAY, 23, 40))
    config = SimpleNamespace(**{**vars(SETTINGS), "gemini_api_key": "k"})
    asked = []
    script = rs.write_script(facts, config, ask=lambda prompt, key, **o: asked.append(prompt) or GOOD_AI)
    assert script.source == "ai" and script.title == GOOD_AI["title"]
    assert "Hinglish" in asked[0] and "Never tell viewers to buy or sell" in asked[0] and '"close": 4118.0' in asked[0]
    for bad in ({**GOOD_AI, "lines": ["Abhi gold buy karo, target 4200 hai."] * 5}, {**GOOD_AI, "lines": ["one"]}, {"title": "x"},
                content_cards.ContentError("the AI writer answered HTTP 503")):
        def ask(prompt, key, bad=bad, **o):
            if isinstance(bad, Exception):
                raise bad
            return bad
        assert rs.write_script(facts, config, ask=ask).source == "template"


@pytest.mark.parametrize("line", ["Gold buy karo abhi", "Gold will rise tomorrow", "Entry le lo 4100 par", "Pakka profit hoga",
                                  "Sell now before it falls", "Target 4200 aur stop loss 4050"])
def test_advice_and_predictions_are_refused(line):
    with pytest.raises(rs.ReelScriptError):
        rs.validate({**GOOD_AI, "lines": GOOD_AI["lines"][:4] + [line]}, "2026-10-21", "ai")


def test_every_forex_factory_release_of_the_day_is_in_the_facts_most_important_first(db):
    from src.collector.models import Event
    stamp = "2026-10-21T00:00:00Z"

    def event(name, hhmm, impact, forecast=None, previous=None, actual=None):
        hour, minute = (int(x) for x in hhmm.split(":"))
        utc = ist(DAY, hour, minute).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return Event(event_id="ff-" + name.replace(" ", "")[:12].lower(), date=DAY.isoformat(), time=hhmm, timezone="Asia/Kolkata",
                     datetime_utc=utc, currency="USD", event_name=name, impact=impact, original_impact=impact, gold_relevance=False,
                     forecast=forecast, previous=previous, actual=actual, source="forexfactory", source_url="https://example.test",
                     retrieved_at=stamp, updated_at=stamp)
    db.upsert_events([event("Building Permits", "18:00", "Low", "1.39M", "1.39M"), event("Housing Starts", "18:00", "Low", "1.30M", "1.28M"),
                      event("Crude Oil Inventories", "20:00", "Low", "-0.1M", "-8.6M"), event("Core CPI m/m", "18:00", "High", "0.3%", "0.2%", "0.4%")])
    facts = rs.day_facts(db, DAY, IST, now=ist(DAY, 23, 40))
    assert [r["name"] for r in facts["releases"]] == ["Core CPI m/m", "Building Permits", "Housing Starts", "Crude Oil Inventories"]
    cpi = facts["releases"][0]
    assert (cpi["time_ist"], cpi["actual"], cpi["forecast"], cpi["previous"]) == ("6:00 PM", "0.4%", "0.3%", "0.2%")
    script = rs.template_script(facts)
    text = " ".join(script.lines)
    assert "Core CPI m/m aaya 0.4%, forecast tha 0.3%" in text and "Building Permits" in text and "forecast 1.39M" in text
    assert text.index("Core CPI") < text.index("Gold ne")                    # the news comes first, then gold
    prompt = rs.build_prompt(facts)
    assert "Forex Factory" in prompt and "the result only if 'actual' is given" in prompt and "Building Permits" in prompt


def test_the_prompt_follows_the_pattern_and_changes_style_every_day(db):
    facts = rs.day_facts(db, DAY, IST, now=ist(DAY, 23, 40))
    assert facts["today"]["date"] == "2026-10-22" and facts["today"]["weekday"] == "Thursday"
    prompt = rs.build_prompt(facts, ["Kal gold ne sabko chauka diya"])
    for part in ("1. HOOK", "2. YESTERDAY'S MARKET", "3. WHAT IT CAN MEAN FOR TODAY", "4. CALL TO ACTION"):
        assert part in prompt
    assert "conditional terms only" in prompt and "Kal gold ne sabko chauka diya" in prompt
    styles = {rs.build_prompt({**facts, "day": (DAY + timedelta(days=d)).isoformat()}).split("in this style: ")[1].split(".")[0]
              for d in range(10)}
    assert len(styles) == 10                                              # ten days, ten different kinds of hook


def test_a_hook_that_opens_like_a_recent_one_is_not_used(db):
    facts = rs.day_facts(db, DAY, IST, now=ist(DAY, 23, 40))
    config = SimpleNamespace(**{**vars(SETTINGS), "gemini_api_key": "k"})
    repeat = {**GOOD_AI, "lines": GOOD_AI["lines"] + ["Comment karo aaj kya lagta hai."]}
    script = rs.write_script(facts, config, ask=lambda *a, **k: repeat, recent_hooks=["Kal US CPI aaya aur sab hil gaya"])
    assert script.source == "template"                                   # same first four words as a recent hook
    fresh = rs.write_script(facts, config, ask=lambda *a, **k: repeat, recent_hooks=["Aaj ka gold kuch alag hai"])
    assert fresh.source == "ai"


def test_hooks_are_remembered_between_days_and_templates_vary_too(db):
    log = []
    rs.run(db, SETTINGS, now=ist(DAY, 23, 40), environ={"VERCEL_TOKEN": "t"}, call=fake_vercel(log))
    rs.run(db, SETTINGS, now=ist(DAY + timedelta(days=1), 23, 40), environ={"VERCEL_TOKEN": "t"}, call=fake_vercel(log))
    remembered = json.loads(db.get_content_state(rs.HOOK_MEMORY)["recent"])["recent"]
    assert len(remembered) == 2 and remembered[0] != remembered[1]
    hooks = {rs.template_script({"day": (DAY + timedelta(days=d)).isoformat(), "gold": None, "releases": [], "coming_up": []}).hook
             for d in range(5)}
    assert len(hooks) == 5


# -- the private page ----------------------------------------------------------------------

def test_the_script_is_encrypted_and_opens_only_with_the_password():
    sealed = rs.encrypt({"title": "Secret script"}, PASSWORD)
    assert "Secret script" not in json.dumps(sealed) and sealed["iterations"] >= 300_000
    assert rs.decrypt(sealed, PASSWORD) == {"title": "Secret script"}
    with pytest.raises(Exception):
        rs.decrypt(sealed, "wrong password")
    assert rs.encrypt({"a": 1}, PASSWORD)["salt"] != rs.encrypt({"a": 1}, PASSWORD)["salt"]
    with pytest.raises(rs.ReelScriptError):
        rs.encrypt({"a": 1}, "short")


def test_the_page_carries_no_script_and_matches_the_encryption():
    page = (rs.PAGE_DIR / "index.html").read_text(encoding="utf-8")
    assert "PBKDF2" in page and "AES-GCM" in page and "SHA-256" in page and "script.json" in page
    assert 'name="robots" content="noindex' in page
    files = rs.site_files(rs.template_script({"day": "2026-10-21", "gold": None, "releases": [], "coming_up": []}), PASSWORD,
                          ist(DAY, 23, 40))
    assert set(files) == {"index.html", "script.json", "robots.txt"}
    assert "Gold aur USD news" not in files["index.html"] + files["script.json"]
    assert rs.decrypt(json.loads(files["script.json"]), PASSWORD)["day"] == "2026-10-21"


def test_publishing_deploys_once_and_removes_every_older_version():
    log = []
    address, deleted = rs.publish({"index.html": "<html>", "script.json": "{}"}, "tok", "test-reels", call=fake_vercel(log))
    assert address == "https://test-reels.vercel.app" and deleted == 2
    method, path, payload = log[0]
    assert (method, path) == ("POST", "/v13/deployments") and payload["target"] == "production" and payload["name"] == "test-reels"
    assert [d for d in log if d[0] == "DELETE"] == [("DELETE", "/v13/deployments/dpl_old1", None),
                                                     ("DELETE", "/v13/deployments/dpl_old2", None)]


# -- a run ---------------------------------------------------------------------------------

def test_one_script_per_day_and_nothing_goes_to_a_channel(db):
    log = []
    first = rs.run(db, SETTINGS, now=ist(DAY, 23, 40), environ=ENV, call=fake_vercel(log))
    assert first[0] == "SENT" and "test-reels.vercel.app" in first[2]
    again = rs.run(db, SETTINGS, now=ist(DAY, 23, 50), environ=ENV, call=fake_vercel(log))
    assert again[0] == "ALREADY_SENT" and sum(1 for d in log if d[0] == "POST") == 1
    record = db.get_delivery("REEL_SCRIPT_2026-10-21", "vercel", "owner_reel_page")
    assert record.status == "SENT" and record.destination == "owner_reel_page"
    assert {r.provider for r in db.list_deliveries()} == {"vercel"}            # no telegram, no whapi


def test_without_a_vercel_token_nothing_is_written_or_asked(db):
    asked = []
    outcome, script, detail = rs.run(db, SimpleNamespace(**{**vars(SETTINGS), "gemini_api_key": "k"}), now=ist(DAY, 23, 40),
                                     environ={"REEL_PAGE_PASSWORD": PASSWORD}, ask=lambda *a, **k: asked.append(1) or GOOD_AI)
    assert outcome == "NOT_CONFIGURED" and script is None and asked == []


def test_without_a_password_the_script_is_published_as_it_is(db):
    log = []
    outcome, script, _ = rs.run(db, SETTINGS, now=ist(DAY, 23, 40), environ={"VERCEL_TOKEN": "tok"}, call=fake_vercel(log))
    assert outcome == "SENT"
    files = {f["file"]: f["data"] for f in log[0][2]["files"]}
    data = json.loads(files["script.json"])
    assert data["v"] == 0 and data["plain"]["title"] == script.title and data["plain"]["lines"] == script.lines
    assert files["robots.txt"] == "User-agent: *\nDisallow: /\n"


def test_the_page_shows_a_plain_script_straight_away_and_asks_for_a_password_only_when_encrypted():
    page = (rs.PAGE_DIR / "index.html").read_text(encoding="utf-8")
    assert 'id="lock" class="hidden"' in page and "if (data.plain) { show(data.plain)" in page


def test_a_failed_publish_is_recorded_and_retried(db):
    def down(method, path, token, payload=None):
        raise rs.ReelScriptError("Vercel answered HTTP 500.")
    assert rs.run(db, SETTINGS, now=ist(DAY, 23, 40), environ=ENV, call=down)[0] == "FAILED"
    assert rs.run(db, SETTINGS, now=ist(DAY, 23, 50), environ=ENV, call=fake_vercel([]))[0] == "SENT"


def test_dry_run_publishes_nothing(db):
    outcome, script, _ = rs.run(db, SETTINGS, now=ist(DAY, 23, 40), dry_run=True, environ={})
    assert outcome == "DRY_RUN" and script is not None and db.count_deliveries() == 0


def test_an_ai_script_must_fill_a_whole_minute():
    short = {**GOOD_AI, "lines": GOOD_AI["lines"][:5]}
    with pytest.raises(rs.ReelScriptError, match="one-minute reel"):
        rs.validate(short, "2026-10-21", "ai")
    script = rs.validate(GOOD_AI, "2026-10-21", "ai")
    assert rs.MIN_WORDS <= rs.spoken_words(script) <= rs.MAX_WORDS + 15


def test_the_template_version_is_close_to_a_minute_too(db):
    script = rs.template_script(rs.day_facts(db, DAY, IST, now=ist(DAY, 23, 40)))
    assert rs.spoken_words(script) >= 100
