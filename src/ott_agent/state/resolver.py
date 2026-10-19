"""Fact resolver: per-fact precedence and freshness rules, with conflicts kept visible.

Author: Rajveer Sharma
Date: 18 Oct 2026
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Iterable

from ..clock import ist_date, ist_day_key
from ..config import Freshness
from ..pack.catalogue import Catalogue
from ..pack.events import Event, EventIndex
from ..pack.loader import ProfileRow
from .facts import DEFAULT, DERIVED, EVENT, PROFILE, TRIGGER, Fact, ViewerFacts

SUB_EVENTS = ("purchase_completed", "subscription_renewed", "subscription_lapsed")
MESSAGE_CHANNELS = ("push", "whatsapp")
DEEPLINK = re.compile(r"^ott://c/(?P<campaign>[^/]+)/(?P<dialect>[^/]+)/title/(?P<title_id>[^/?#]+)")
MAX_REFS = 5  # aggregate facts cite their most recent evidence, not all of it


def deeplink_dialect(event: Event) -> str | None:
    m = DEEPLINK.match(str(event.properties.get("deeplink") or ""))
    return m.group("dialect") if m else None


def _refs(events: Iterable[Event]) -> tuple[str, ...]:
    return tuple(e.ref for e in list(events)[-MAX_REFS:])


def _status_from_event(e: Event) -> str:
    if e.event == "subscription_lapsed":
        return "lapsed"
    if e.event == "purchase_completed" and e.properties.get("is_trial") is True:
        return "trial_active"
    return "active"


class FactResolver:
    def __init__(
        self,
        index: EventIndex,
        profiles: Iterable[ProfileRow],
        catalogue: Catalogue,
        freshness: Freshness,
        history_start: datetime | None = None,
    ) -> None:
        self.index = index
        self.catalogue = catalogue
        self.freshness = freshness
        self.history_start = history_start
        self.profiles: dict[str, list[ProfileRow]] = defaultdict(list)
        for row in profiles:
            self.profiles[row.user_id].append(row)

    def resolve(self, user_id: str, at: datetime, trigger: Event | None = None) -> ViewerFacts:
        vf = ViewerFacts(user_id, at)
        # Every event that had arrived by `at` counts, including the trigger itself (it may be a purchase).
        # Only "previous app open" (G12) excludes the current trigger and its session; see _watch.
        events = self.index.for_user(user_id, until=at)
        row, stale = self._profile(vf, user_id)
        self._signup(vf, row, events)
        self._subscription(vf, row, stale, events)
        self._dialect(vf, row, events, trigger)
        self._language(vf, row, events, trigger)
        self._consent(vf, row, stale, events)
        self._watch(vf, events, trigger)
        self._messages(vf, events)
        return vf

    def _profile(self, vf: ViewerFacts, user_id: str) -> tuple[ProfileRow | None, bool]:
        rows = self.profiles.get(user_id, [])
        if not rows:
            vf.add(Fact("profile_row", "absent", DEFAULT, note="no row in the snapshot (new or unexported viewer)"))
            vf.add(Fact("is_new_viewer", True, DERIVED, note="no profile row"))
            return None, False
        row = rows[0]
        if len(rows) > 1:
            ordered = sorted(rows, key=lambda r: (r.updated_at is not None, r.updated_at or datetime.min))
            latest = ordered[-1]
            ties = [r for r in rows if r.updated_at == latest.updated_at]
            row = latest if len(ties) == 1 else _conservative_merge(ties)
            differing = sorted({c for c in ("subscription_status", "preferred_dialect", "notifications_opt_in",
                                             "whatsapp_opt_in", "signup_at") if len({getattr(r, c) for r in rows}) > 1})
            vf.conflict(
                "profile_row", chosen=row.ref, rejected=[r.ref for r in rows if r is not row],
                rule=("latest updated_at wins" if len(ties) == 1 else "tie on updated_at: conservative merge")
                + (f"; rows differ in {', '.join(differing)}" if differing else ""),
            )
        snapshot = row.snapshot_at or row.updated_at
        stale = snapshot is not None and (vf.at - snapshot) > timedelta(hours=self.freshness.snapshot_max_age_h)
        vf.add(Fact("profile_row", "stale" if stale else "present", PROFILE, (row.ref,), snapshot,
                    note=f"snapshot older than {self.freshness.snapshot_max_age_h:g} h" if stale else ""))
        vf.add(Fact("is_new_viewer", False, DERIVED))
        if row.platform:
            vf.add(Fact("platform", row.platform, PROFILE, (row.ref,), snapshot))
        return row, stale

    def _signup(self, vf: ViewerFacts, row: ProfileRow | None, events: list[Event]) -> None:
        signups = [e for e in events if e.event == "signup_completed"]
        if signups:
            first = signups[0]
            vf.add(Fact("signup_at", first.received_at, EVENT, (first.ref,), first.received_at))
            if row and row.signup_at and abs((row.signup_at - first.received_at).total_seconds()) > 86400:
                vf.conflict("signup_at", f"event:{first.received_at.date()}", [f"profile:{row.signup_at.date()}"],
                            "signup_completed event (server clock) beats the warehouse export")
        elif row and row.signup_at:
            vf.add(Fact("signup_at", row.signup_at, PROFILE, (row.ref,), row.snapshot_at))
        else:
            vf.add(Fact("signup_at", None, DEFAULT, note="no signup event or profile date"))

    def _subscription(self, vf: ViewerFacts, row: ProfileRow | None, stale: bool, events: list[Event]) -> None:
        subs = [e for e in events if e.event in SUB_EVENTS]
        cutoff = (row.snapshot_at or row.updated_at) if row else None
        after_snapshot = [e for e in subs if cutoff is None or e.received_at > cutoff]

        base = row.subscription_status if (row and not stale) else None
        if after_snapshot:
            last = after_snapshot[-1]
            value = _status_from_event(last)
            vf.add(Fact("subscription_status", value, EVENT, _refs(after_snapshot), last.received_at,
                        note="event received after the snapshot" if row else "no profile row; from events"))
            if base and base != value:
                vf.conflict("subscription_status", f"event:{value}", [f"profile:{base}"],
                            "an event received after the snapshot beats the day-old snapshot")
        elif base:
            vf.add(Fact("subscription_status", base, PROFILE, (row.ref,), cutoff))
        elif row is None and self._signed_up_inside_history(vf):
            vf.add(Fact("subscription_status", "never", DERIVED, (vf.facts["signup_at"].refs),
                        vf.get("signup_at"), note="signed up inside the event history and no subscription event since"))
        else:
            vf.add(Fact("subscription_status", "unknown", DEFAULT,
                        note="profile stale" if stale else "no profile row and no evidence of the full history"))

        paid = [e for e in subs if e.event == "subscription_renewed"
                or (e.event == "purchase_completed" and e.properties.get("is_trial") is not True)]
        status = vf.get("subscription_status")
        if paid:
            vf.add(Fact("ever_paid", True, EVENT, _refs(paid), paid[-1].received_at))
        elif status in ("active", "lapsed"):
            vf.add(Fact("ever_paid", True, vf.facts["subscription_status"].source, vf.facts["subscription_status"].refs))
        elif status in ("never", "trial_active"):
            vf.add(Fact("ever_paid", False, vf.facts["subscription_status"].source, vf.facts["subscription_status"].refs))
        else:
            vf.add(Fact("ever_paid", None, DEFAULT, note="unknown"))
        if status == "never" and paid:
            vf.conflict("ever_paid", "event:true", [f"{vf.facts['subscription_status'].source}:never"],
                        "a paid purchase/renewal event exists; the viewer is treated as having paid")

        lapses = [e for e in subs if e.event == "subscription_lapsed"]
        if lapses:
            last_lapse = lapses[-1]
            rebought = [e for e in paid if e.received_at > last_lapse.received_at]
            vf.add(Fact("last_lapsed_at", last_lapse.received_at, EVENT, (last_lapse.ref,), last_lapse.received_at))
            vf.add(Fact("repurchased_since_lapse", bool(rebought), EVENT, _refs(rebought) or (last_lapse.ref,)))
        else:
            vf.add(Fact("last_lapsed_at", None, DERIVED, note="no subscription_lapsed event in history"))

    def _signed_up_inside_history(self, vf: ViewerFacts) -> bool:
        signup = vf.fact("signup_at")
        return (signup is not None and signup.source == EVENT and self.history_start is not None
                and signup.value >= self.history_start)

    def _dialect(self, vf: ViewerFacts, row: ProfileRow | None, events: list[Event], trigger: Event | None) -> None:
        if trigger is not None and trigger.dialect:
            app, src, ref, as_of = trigger.dialect, TRIGGER, trigger.ref, trigger.received_at
        else:
            latest = next((e for e in reversed(events) if e.dialect), None)
            app, src, ref, as_of = (latest.dialect, EVENT, latest.ref, latest.received_at) if latest else (None, DEFAULT, None, None)
        profile_d = row.preferred_dialect if row else None
        linked = [e for e in ([trigger] if trigger else []) + list(reversed(events)) if deeplink_dialect(e)]
        campaign = deeplink_dialect(linked[0]) if linked else None

        vf.add(Fact("dialect_seen", app, src, (ref,) if ref else (), as_of,
                    note="" if app else "app dialect unknown (missing or NA)"))
        source_refs = (ref, row.ref if row else None, linked[0].ref if linked else None)
        vf.add(Fact("dialect_sources", {"app_setting": app, "profile": profile_d, "campaign": campaign}, DERIVED,
                    tuple(dict.fromkeys(r for r in source_refs if r))))
        others = [f"{name}:{d}" for name, d in (("profile", profile_d), ("campaign", campaign)) if d and d != app]
        vf.conflict("dialect_seen", f"app_setting:{app}", others,
                    "content follows the dialect the app is set to (it requests that dialect's home page); "
                    "profile and campaign dialects never pick content")

    def _language(self, vf: ViewerFacts, row: ProfileRow | None, events: list[Event], trigger: Event | None) -> None:
        if trigger is not None and trigger.app_language:
            vf.add(Fact("app_language", trigger.app_language, TRIGGER, (trigger.ref,), trigger.received_at))
            return
        latest = next((e for e in reversed(events) if e.app_language), None)
        if latest:
            vf.add(Fact("app_language", latest.app_language, EVENT, (latest.ref,), latest.received_at))
        elif row and row.preferred_language:
            vf.add(Fact("app_language", row.preferred_language, PROFILE, (row.ref,), row.snapshot_at))
        else:
            vf.add(Fact("app_language", None, DEFAULT))

    def _consent(self, vf: ViewerFacts, row: ProfileRow | None, stale: bool, events: list[Event]) -> None:
        opt_outs = [e for e in events if e.event == "notification_opt_out"]
        for channel, column in (("push", "notifications_opt_in"), ("whatsapp", "whatsapp_opt_in")):
            name = f"consent_{channel}"
            outs = [e for e in opt_outs if e.properties.get("channel") in (channel, None, "")]
            profile_value = getattr(row, column) if (row and not stale) else None
            if outs:
                vf.add(Fact(name, False, EVENT, _refs(outs), outs[-1].received_at, note="opt-out event"))
                if profile_value is True:
                    vf.conflict(name, "event:opt_out", [f"profile:{column}=true"],
                                "any opt-out event for the channel overrides the snapshot (conservative)")
            elif profile_value is not None:
                vf.add(Fact(name, profile_value, PROFILE, (row.ref,), row.snapshot_at))
            else:
                vf.add(Fact(name, False, DEFAULT, note="no consent on record (missing, stale or unparseable profile)"))

    def _watch(self, vf: ViewerFacts, events: list[Event], trigger: Event | None) -> None:
        plays = [e for e in events if e.event == "play_started"]
        completes = [e for e in events if e.event == "episode_completed"]
        vf.add(Fact("has_played", bool(plays), EVENT if plays else DERIVED, _refs(plays),
                    plays[-1].received_at if plays else None))
        vf.add(Fact("has_completed_episode", bool(completes), EVENT if completes else DERIVED, _refs(completes),
                    completes[-1].received_at if completes else None))
        vf.add(Fact("completed_episode_ids", sorted({str(e.properties.get("episode_id")) for e in completes
                                                     if e.properties.get("episode_id")}), EVENT, _refs(completes)))

        series: dict[str, dict] = {}
        genres: Counter = Counter()
        for e in plays:
            title = self.catalogue.titles.get(str(e.properties.get("title_id")))
            if title is None:
                continue
            genres.update(title.genres)
            if title.is_series:
                s = series.setdefault(title.content_slug, {"title_id": title.title_id, "plays": 0, "refs": []})
                s.update(title_id=title.title_id, last_play_at=e.received_at)
                s["plays"] += 1
                s["refs"] = (s["refs"] + [e.ref])[-MAX_REFS:]
        completed = set(vf.get("completed_episode_ids"))
        for slug, s in series.items():
            episodes = self.catalogue.episodes(slug)
            s["episodes_total"] = len(episodes)
            s["episodes_completed"] = sum(1 for ep in episodes if ep.episode_id in completed)
            nxt = next((ep for ep in episodes if ep.episode_id not in completed), None)
            s["next_episode_id"] = nxt.episode_id if nxt else None
        vf.add(Fact("series_progress", series, EVENT if series else DERIVED,
                    tuple(r for s in series.values() for r in s["refs"])[-MAX_REFS:]))
        vf.add(Fact("top_genres", [g for g, _ in genres.most_common(3)], DERIVED, _refs(plays)))
        played = {self.catalogue.titles[str(e.properties.get("title_id"))].content_slug for e in plays
                  if str(e.properties.get("title_id")) in self.catalogue.titles}
        vf.add(Fact("played_slugs", sorted(played), EVENT if played else DERIVED, _refs(plays)))

        window_start = vf.at - timedelta(days=14)
        recent = [e for e in plays if e.received_at >= window_start]
        vf.add(Fact("play_days_14d", len({ist_date(e.received_at) for e in recent}), EVENT if recent else DERIVED,
                    _refs(recent), note="distinct IST days with play_started, last 14 days"))

        session = trigger.session_id if trigger is not None else None
        current = trigger.event_id if trigger is not None else None
        opens = [e for e in events if e.event == "app_open" and e.event_id != current
                 and (session is None or e.session_id != session)]
        last_open = opens[-1] if opens else None
        vf.add(Fact("last_app_open_at", last_open.received_at if last_open else None,
                    EVENT if last_open else DERIVED, (last_open.ref,) if last_open else (),
                    last_open.received_at if last_open else None,
                    note="excludes the current trigger and its session (G12-a1)"))

    def _messages(self, vf: ViewerFacts, events: list[Event]) -> None:
        today = ist_day_key(vf.at)
        delivered = [e for e in events if e.event == "message_delivered"
                     and e.properties.get("channel") in MESSAGE_CHANNELS and ist_day_key(e.received_at) == today]
        vf.add(Fact("messages_delivered_today", len(delivered), EVENT if delivered else DERIVED, _refs(delivered),
                    note=f"push + WhatsApp delivered on IST day {today} (platform events; the agent ledger adds its own)"))


def _conservative_merge(rows: list[ProfileRow]) -> ProfileRow:
    """Rows tied on updated_at: keep agreed values, otherwise the safest one (no consent, unknown status)."""

    def agreed(attr, fallback):
        values = {getattr(r, attr) for r in rows}
        return values.pop() if len(values) == 1 else fallback

    base = rows[0]
    return replace(
        base,
        subscription_status=agreed("subscription_status", None),
        preferred_dialect=agreed("preferred_dialect", None),
        notifications_opt_in=agreed("notifications_opt_in", False),
        whatsapp_opt_in=agreed("whatsapp_opt_in", False),
        signup_at=agreed("signup_at", None),
    )


def build_resolver(pack, freshness: Freshness, include_replay: bool = True) -> FactResolver:
    """Resolver over the pack's history, plus live events and triggers (visible only once delivered)."""
    index = EventIndex(pack.history)
    if include_replay:
        for e in list(pack.live_events) + list(pack.triggers):
            index.add(e)
    start = pack.history[0].received_at if pack.history else None
    return FactResolver(index, pack.profiles, pack.catalogue, freshness, history_start=start)
