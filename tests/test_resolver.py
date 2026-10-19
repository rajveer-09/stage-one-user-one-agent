"""Tests for fact resolution: precedence, freshness, conflicts and time-awareness.

Author: Rajveer Sharma
Date: 18 Oct 2026
"""

from conftest import INTERPRETATIONS, event
from ott_agent.clock import parse_ts
from ott_agent.config import Freshness
from ott_agent.pack.catalogue import load_catalogue
from ott_agent.pack.events import EventIndex, parse_event
from ott_agent.pack.loader import ProfileRow, load_pack
from ott_agent.state.resolver import FactResolver, build_resolver

SNAPSHOT = parse_ts("2026-09-30T00:00:00Z")
HISTORY_START = parse_ts("2026-08-31T18:30:00Z")


def profile(user="U-1", status="never", dialect="d1", push=True, wa=False, updated="2026-09-29T10:00:00Z",
            signup="2026-01-01T00:00:00Z", snapshot=SNAPSHOT, row=2):
    return ProfileRow(user, parse_ts(signup), status, None, None, dialect, "hin", push, wa, "android",
                      parse_ts(updated), snapshot, row)


def ev(*args, source="history", deliver=None, **kw):
    raw = event(*args, **kw)
    if deliver:
        raw["deliver_at"] = deliver
    return parse_event(raw, source)


def resolver(mini_pack, events=(), profiles=(), max_age_h=48):
    cat = load_catalogue(mini_pack / "data/catalogue.csv", mini_pack / "data/episodes.csv")
    return FactResolver(EventIndex(events), profiles, cat, Freshness(snapshot_max_age_h=max_age_h), HISTORY_START)


# ---------------------------------------------------------------- dialect


def test_dialect_conflict_uses_app_setting_and_keeps_conflicts_visible(mini_pack):
    trigger = ev("R1", "U-1", "app_open", "2026-10-01T05:00:00Z", dialect="d3", source="trigger",
                 deliver="2026-10-01T05:00:00Z", deeplink="ott://c/cmp1/d5/title/T1")
    vf = resolver(mini_pack, profiles=[profile(dialect="d1")]).resolve("U-1", trigger.arrival, trigger)
    assert vf.get("dialect_seen") == "d3"
    assert vf.fact("dialect_seen").source == "trigger"
    assert vf.get("dialect_sources") == {"app_setting": "d3", "profile": "d1", "campaign": "d5"}
    [c] = vf.conflicts_for("dialect_seen")
    assert c.chosen == "app_setting:d3" and set(c.rejected) == {"profile:d1", "campaign:d5"}


def test_missing_trigger_dialect_falls_back_to_latest_event_then_unknown(mini_pack):
    older = ev("E1", "U-1", "app_open", "2026-09-20T10:00:00Z", dialect="d2")
    trigger = ev("R1", "U-1", "app_open", "2026-10-01T05:00:00Z", dialect="NA", source="trigger", deliver="2026-10-01T05:00:00Z")
    vf = resolver(mini_pack, [older]).resolve("U-1", trigger.arrival, trigger)
    assert (vf.get("dialect_seen"), vf.fact("dialect_seen").source) == ("d2", "event")
    vf = resolver(mini_pack).resolve("U-1", trigger.arrival, trigger)
    assert vf.get("dialect_seen") is None


# ---------------------------------------------------------------- subscription


def test_purchase_event_beats_snapshot_but_only_once_it_has_arrived(mini_pack):
    purchase = ev("L1", "U-1", "purchase_completed", "2026-10-01T05:02:00Z", source="live",
                  deliver="2026-10-01T05:02:00Z", plan_id="P_MONTH", price_inr=149, is_trial=False)
    r = resolver(mini_pack, [purchase], [profile(status="never")])

    before = r.resolve("U-1", parse_ts("2026-10-01T05:01:59Z"))
    assert before.get("subscription_status") == "never" and before.get("ever_paid") is False

    after = r.resolve("U-1", parse_ts("2026-10-01T05:02:00Z"))
    assert after.get("subscription_status") == "active"
    assert after.fact("subscription_status").refs == ("event:L1",)
    assert after.get("ever_paid") is True
    [c] = after.conflicts_for("subscription_status")
    assert (c.chosen, c.rejected) == ("event:active", ("profile:never",))


def test_purchase_before_snapshot_does_not_override_but_marks_ever_paid(mini_pack):
    old = ev("E1", "U-1", "purchase_completed", "2026-09-10T10:00:00Z", plan_id="P_MONTH", price_inr=149, is_trial=False)
    vf = resolver(mini_pack, [old], [profile(status="never")]).resolve("U-1", parse_ts("2026-10-01T05:00:00Z"))
    assert vf.get("subscription_status") == "never"  # the snapshot is newer than the event
    assert vf.get("ever_paid") is True               # but a paid purchase exists: never "never paid"
    assert vf.conflicts_for("ever_paid")


def test_lapse_and_repurchase_facts(mini_pack):
    lapse = ev("E1", "U-1", "subscription_lapsed", "2026-09-15T10:00:00Z")
    vf = resolver(mini_pack, [lapse], [profile(status="lapsed")]).resolve("U-1", parse_ts("2026-10-01T05:00:00Z"))
    assert vf.get("last_lapsed_at") == lapse.received_at
    assert vf.get("repurchased_since_lapse") is False


# ---------------------------------------------------------------- new viewers and stale profiles


def test_new_viewer_facts_come_from_events(mini_pack):
    signup = ev("L1", "U-9", "signup_completed", "2026-10-01T01:32:28Z", source="live",
                deliver="2026-10-01T01:32:28Z", method="phone_otp")
    vf = resolver(mini_pack, [signup]).resolve("U-9", parse_ts("2026-10-01T01:32:46Z"))
    assert vf.get("is_new_viewer") is True
    assert vf.get("signup_at") == signup.received_at
    assert (vf.get("subscription_status"), vf.fact("subscription_status").source) == ("never", "derived")
    assert vf.get("consent_push") is False and vf.get("consent_whatsapp") is False


def test_viewer_without_profile_or_full_history_is_unknown(mini_pack):
    vf = resolver(mini_pack, [ev("E1", "U-9", "app_open", "2026-09-20T10:00:00Z")]).resolve("U-9", parse_ts("2026-10-01T05:00:00Z"))
    assert vf.get("subscription_status") == "unknown"
    assert vf.get("ever_paid") is None


def test_stale_snapshot_means_unknown_status_and_no_consent(mini_pack):
    r = resolver(mini_pack, profiles=[profile(status="active", push=True)], max_age_h=12)
    vf = r.resolve("U-1", parse_ts("2026-10-01T05:00:00Z"))  # 29 h after the snapshot
    assert vf.get("profile_row") == "stale"
    assert vf.get("subscription_status") == "unknown"
    assert vf.get("consent_push") is False


# ---------------------------------------------------------------- profile duplicates


def test_profile_duplicate_latest_updated_at_wins_regardless_of_file_order(mini_pack):
    newer = profile(status="never", dialect="d1", updated="2026-09-29T11:00:00Z", row=2)
    older = profile(status="lapsed", dialect="d2", updated="2026-09-22T02:00:00Z", row=3)
    vf = resolver(mini_pack, profiles=[newer, older]).resolve("U-1", parse_ts("2026-10-01T05:00:00Z"))
    assert vf.get("subscription_status") == "never"
    [c] = vf.conflicts_for("profile_row")
    assert c.chosen == newer.ref and c.rejected == (older.ref,)
    assert "subscription_status" in c.rule and "preferred_dialect" in c.rule


def test_profile_duplicate_tie_is_merged_conservatively(mini_pack):
    a = profile(status="active", push=True, wa=True, row=2)
    b = profile(status="never", push=False, wa=True, row=3)  # same updated_at
    vf = resolver(mini_pack, profiles=[a, b]).resolve("U-1", parse_ts("2026-10-01T05:00:00Z"))
    assert vf.get("subscription_status") == "unknown"
    assert vf.get("consent_push") is False      # disagreed -> no consent
    assert vf.get("consent_whatsapp") is True   # agreed
    assert "conservative merge" in vf.conflicts_for("profile_row")[0].rule


# ---------------------------------------------------------------- consent


def test_opt_out_event_overrides_profile_for_its_channel_only(mini_pack):
    out = ev("E1", "U-1", "notification_opt_out", "2026-09-30T12:00:00Z", channel="push")
    vf = resolver(mini_pack, [out], [profile(push=True, wa=True)]).resolve("U-1", parse_ts("2026-10-01T05:00:00Z"))
    assert vf.get("consent_push") is False
    assert vf.fact("consent_push").refs == ("event:E1",)
    assert vf.get("consent_whatsapp") is True
    assert vf.conflicts_for("consent_push")


# ---------------------------------------------------------------- watching behaviour


def test_series_progress_next_episode_and_g12_session_exclusion(mini_pack):
    events = [
        ev("E1", "U-1", "app_open", "2026-09-10T10:00:00Z"),
        ev("E2", "U-1", "play_started", "2026-09-10T10:01:00Z", title_id="T1", episode_id="show-a-d1-s1e1"),
        ev("E3", "U-1", "episode_completed", "2026-09-10T10:25:00Z", title_id="T1", episode_id="show-a-d1-s1e1"),
        ev("E4", "U-1", "play_started", "2026-09-25T10:00:00Z", title_id="T3", episode_id="film-b-d1-s1e1"),
    ]
    trigger = ev("R1", "U-1", "app_open", "2026-10-01T05:00:00Z", source="trigger", deliver="2026-10-01T05:00:00Z")
    vf = resolver(mini_pack, events, [profile()]).resolve("U-1", trigger.arrival, trigger)
    series = vf.get("series_progress")
    assert list(series) == ["show-a-d1"]  # the film is not a series
    assert series["show-a-d1"]["episodes_completed"] == 1
    assert series["show-a-d1"]["next_episode_id"] == "show-a-d1-s1e2"
    assert vf.get("play_days_14d") == 1                       # only the 25 Sep play is inside 14 days
    assert vf.get("last_app_open_at") == events[0].received_at  # the trigger itself is excluded
    assert set(vf.get("top_genres")) == {"Drama", "Comedy"}


def test_build_resolver_on_mini_pack_sees_live_events_only_after_delivery(mini_pack):
    pack = load_pack(mini_pack, INTERPRETATIONS)
    r = build_resolver(pack, Freshness())
    assert r.resolve("U-1", parse_ts("2026-10-01T05:01:00Z")).get("subscription_status") == "never"
    assert r.resolve("U-1", parse_ts("2026-10-01T05:02:00Z")).get("subscription_status") == "active"
