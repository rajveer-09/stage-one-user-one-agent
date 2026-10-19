# Final Report: One User, One Agent

**Author:** Rajveer Sharma
**Date:** 19 Oct 2026

Run: `python -m ott_agent run --pack ./pack --out ./sample_run`, mock model provider, simulator seed `oua-practice`. Detailed numbers: [impact_report.md](impact_report.md), [latency_report.json](latency_report.json).

## Summary

- **The agent acted carefully.** Across 2,769 trigger deliveries it made 2,774 decisions (5 of them in-session re-plans after a purchase). It published 918 times: 927 home pages, 382 pushes and 10 WhatsApp messages. The other decisions were deliberate NO_ACTIONs, each with a reason.
- **It was safe.** No holdout viewer received an action, the verifier and the simulator found 0 policy problems, and no withdrawn or quarantined title was used.
- **It was on time.** All decisions were published within 60 s: p95 1.2 s, max 3.3 s, including during the model outage, the store slowdown and the evening peak.
- **The effect on viewers is small and not robust.** On the randomized arms, goal success was +2.1 pt [−1.2, +5.4] with the default seed and +5.2 pt [+2.0, +8.4] with a second seed. Play within 7 days did not differ detectably on either seed. The honest reading is a possible small positive effect that this replay cannot confirm.
- **The pilot's +22.4 pt "uplift" was selection.** On its randomized arms the pilot shows −1.3 pt [−3.8, +1.2].

## What the agent did on the replay day

| | Count |
|---|---|
| Decisions | 2,774 (1,325 holdout, 918 published, 441 NO_ACTION, 90 duplicates) |
| NO_ACTION reasons (agent arm) | 189 stale offline opens · 188 page still live for the same goal · 60 no content in the app's dialect · 4 no eligible goal |
| Goals opened | G07 307 · G02 208 · G08 75 · G01 57 · G05 47 · G09 16 · G06 10 · G03 1 · G12 1 |
| Model consulted | 484 decisions (2+ eligible goals): agreed with the rules 478 times; 6 fell back to the rules (4 provider outage, 1 malformed output, 1 rejected claim); the circuit breaker skipped the model for 3 more |
| Messages | 392 planned follow-ups kept after a final re-check; 3 cancelled (1 when its G07 goal succeeded during the replay, 2 at the final re-check because their goal had closed) |
| Verifier objections | 0 on real decisions. Tests confirm it rejects each kind of violation. |

## Impact (agent arm vs holdout, intention-to-treat)

All 1,746 viewers with a replay trigger (863 agent, 883 holdout) are counted, served or not.

| Metric | Seed `oua-practice` | Seed `second-seed-2026` |
|---|---|---|
| Intended-goal success (primary) | 13.5% vs 11.4%: **+2.1 pt [−1.2, +5.4]** | 14.0% vs 8.8%: **+5.2 pt [+2.0, +8.4]** |
| Any play within 7 days (primary) | +2.5 pt [−1.9, +7.0] | +1.8 pt [−2.7, +6.3] |
| Finished an episode within 3 days | +6.2 pt [+1.8, +10.6] | +2.2 pt [−2.1, +6.5] |
| Paid purchase within 7 days | −1.5 pt [−4.2, +1.2] | −0.8 pt [−3.5, +2.0] |

**Why "served vs everyone else" misleads.** On this run, served viewers vs everyone else reads +14.7 pt, and served vs unserved within the agent arm reads +44.3 pt. The agent only serves viewers who are in a live session, in a dialect with content and with an eligible goal. Those viewers would watch more anyway. Only the randomized comparison isolates the agent, and it shows a few points at most.

**Was doing nothing right?** For viewers whose first trigger was stale, or whose dialect has no content, the agent arm did not do measurably better or worse than the holdout. Both intervals include 0 (see the impact report).

## Data issues found and how they were handled

| Issue | Count | Handling |
|---|---|---|
| Duplicate events (same `event_id`) | 2,176 copies, differing only in `received_at` | Keep the first received copy |
| Phone clock skew | 4,319 events off by more than 10 min (±29 h at the extremes) | Order by `received_at`; event time = `received_at` − (`sent_at` − `occurred_at`) |
| Offline-buffered opens | 417 stale replay triggers, 221 of them in the 04:40Z burst | NO_ACTION (stale_trigger), ingested as history |
| Duplicate replay triggers / live events | 90 / 7 | Idempotency key; recorded, no new action |
| Duplicate profile rows | 30 viewers | Latest `updated_at` wins, never file order |
| Viewers with no profile row | 103 in the replay | Facts from events; no consent assumed |
| Dialect disagreement (app / profile / campaign) | 1,337 replay decisions; 260 viewers have three different dialects | Content follows the app setting; others recorded as conflicts; no message if all three differ |
| Dialects with no content (`d7`, `NA`, missing) | 178 replay triggers | NO_ACTION (insufficient content) |
| Instructions in catalogue descriptions | T0002 (WhatsApp everyone, including opt-outs), T0522 (mark everyone eligible) | Quarantined; never used; logged |
| Unmeasurable goals | G10 (null signal), G11 (`tv_app_open` never occurs) | Disabled |
| Vague goal rules | G06 "recently lapsed", G07 "unwatched episodes" | Written interpretations `G06-a1`, `G07-a1`, cited in decisions |
| Stale subscription snapshot | 5 purchases after the snapshot; 36 purchases about 2 min after a trigger | Newer events beat the snapshot, once they have arrived |

## Surprises

The logs named below (`control_log.jsonl`, `security_log.jsonl`, `replay_summary.json`) are written by `python -m ott_agent run`.

| Surprise | What happened | Where to look |
|---|---|---|
| Burst of offline-buffered opens (04:40Z) | 221 of 237 triggers stale → NO_ACTION; no queueing | `decisions.jsonl` (`stale_trigger`) |
| Purchase 2 min after a decision | U-00649: G05 closed as succeeded, then re-planned to G08 in-session (walkthrough below) | `control_log.jsonl` |
| Three different dialects | e.g. U-04945: app d4, profile d2, campaign d6 → page in d4, conflicts recorded, no message | `decisions.jsonl` (`dialect.conflicts`) |
| Goal rule corrected (G03, 08:30Z) | New rule version; 0 open G03 goals to re-point; no other viewer reprocessed | `control_log.jsonl` |
| Model claims eligibility without evidence | U-01991: "on an active trial (confirmed in billing)" rejected; their profile says `never` | `security_log.jsonl` |
| Malformed model output | U-02563: retried once, then the rules decided | `decisions.jsonl` (`model_claims_rejected`) |
| Model timeout (45 s) | Scheduled for a holdout viewer, so correctly never reached; covered by tests | `replay_summary.json` |
| Catalogue text telling the agent to message everyone | Quarantined at load; never followed | `security_log.jsonl` |
| Title withdrawn (T0001, 07:00Z) | 9 live pages rebuilt without it; 0 uses afterwards | `control_log.jsonl` |
| Model provider down (10:35–10:42Z) | 4 failed calls, then the circuit breaker; 22 decisions, max 0.05 s | `latency_report.json` |
| Store degraded (13:10–13:17Z) | Slow reads charged as latency (max 3.3 s); timed-out reads use the cache, home page only | `latency_report.json` |
| Evening peak (15:00Z, 165 triggers in 10 min) | No queueing on 8 workers; max 1.5 s | `latency_report.json` |

## One viewer, from app open to judged outcome: U-00649

1. **07:35:14Z, app open (13:05 IST), age 0 s.**
   - The agent knows: profile status `never` (snapshot, 32 h old), a finished episode in history, push and WhatsApp consent off, app dialect `d4`, with a campaign link naming `d1` (recorded as a conflict).
2. **Goal.** G02 is ruled out (they have already finished an episode), as are G04 (not on a trial) and G07 (no stalled series). G05 "first paid plan within 7 days" is eligible and, being activated, no longer deferred. Only one goal is eligible, so the model is not consulted.
3. **Action.**
   - A `d4` home page: hero T0915 and 4 rows of 8 titles, each title in `d4`, available and with no repeated content.
   - No message: there is no consent, and the home page alone is proportionate for this goal.
   - Confidence 0.75: base 0.80, −0.05 for the disagreeing campaign dialect.
   - All 11 verifier checks passed; published in 2.4 ms.
4. **07:37:32Z, purchase arrives (2 min later).** It matches G05's success rule, so the goal closes as succeeded. The agent re-plans in the same session: the viewer is now `active`, so G08 "try a new release" is chosen, and a new page leads with new releases.
5. **Judged.** G05 succeeded (`purchase_completed`, 07:37:32Z). G08 succeeded (`play_started` on 5 Oct). Learnings are written to the viewer's state.
6. **What this does not show.** The purchase was a scripted organic event, so this viewer's "success" is not evidence that the agent caused it. Causal credit comes only from the arm comparison above.

To reproduce:

```bash
python -m ott_agent explain --viewer U-00649
python -m ott_agent explain --viewer U-00649 --at 2026-10-01T07:37:00Z
```

The second command shows the open G05 goal and status `never`, as the agent knew it before the purchase.

## Read next

- [ARCHITECTURE.md](../ARCHITECTURE.md): ownership, planning, memory, verification, latency, failure recovery, trust boundaries
- [KNOWN_LIMITATIONS.md](../KNOWN_LIMITATIONS.md)
- [AI_COLLABORATION.md](../AI_COLLABORATION.md)
