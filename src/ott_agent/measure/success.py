"""Does one event count as a goal's success signal? Shared by the replay harness and the outcome judge.

Author: Rajveer Sharma
Date: 19 Oct 2026
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

from ..clock import ist_date
from ..pack.catalogue import Catalogue
from ..pack.events import Event
from ..pack.goals import GoalRule

AGGREGATE = None  # sentinel: needs more than one event to decide


def signal_matches(rule: GoalRule, start: datetime, target: dict[str, Any] | None, event: Event,
                   catalogue: Catalogue) -> bool | None:
    if rule.success.event is None or event.event != rule.success.event:
        return False
    if event.received_at < start:
        return False
    cond = (rule.success.condition or "").lower()
    props = event.properties
    if "is_trial = false" in cond:
        return props.get("is_trial") is False
    if "paid plan" in cond:
        return props.get("is_trial") is not True
    if "later ist calendar day" in cond:
        return ist_date(event.received_at) > ist_date(start)
    if "same content_slug" in cond:
        title = catalogue.titles.get(str(props.get("title_id")))
        return title is not None and title.content_slug == (target or {}).get("content_slug")
    m = re.search(r"released in the (\d+) days before", cond)
    if m:
        title = catalogue.titles.get(str(props.get("title_id")))
        if title is None or title.release_date is None:
            return False
        lo = (start - timedelta(days=int(m.group(1)))).date()
        return lo <= title.release_date <= ist_date(start)
    if "distinct ist days" in cond:
        return AGGREGATE
    if cond.startswith("any") or cond == "":
        return True
    return AGGREGATE  # unknown condition wording: let the judge decide (inconclusive if it can't)


def distinct_days_needed(rule: GoalRule) -> int | None:
    m = re.search(r"on (\d+) distinct ist days", (rule.success.condition or "").lower())
    return int(m.group(1)) if m else None
