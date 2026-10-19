# Architecture

**Author:** Rajveer Sharma
**Date:** 18 Oct 2026

## Shape of the system

```
                pack/ (events, profiles, catalogue, goals, interventions, pricing, replay day)
                                   │  load + validate (pack/loader.py)
                                   ▼
 triggers ─┐            ┌──────────────────────── ViewerAgent (one per viewer) ────────────────────────┐
 live ─────┼─► Replay ──►  resolve facts ─► choose goal ─► (model advisor) ─► build page ─► plan message  │
 control ──┘  harness   │  state/resolver  agent/goal_     agent/advisor     agent/homepage  agent/messaging│
              (queue,    │                  planner                                                      │
              clock)     └──────────────────────────────────┬─────────────────────────────────────────────┘
                                                             ▼ decision (JSON)
                                                Verifier (verify/) ── fails → drop the failing part, re-verify
                                                             ▼
                                   publish: store (journal, active goal, ledgers) + actions.jsonl
                                                             ▼
                          simulator ─► judge (measure/judge) ─► learnings into the store
                                                             ▼
                                           impact vs holdout, latency, pilot re-analysis
```

Everything is deterministic Python except an optional model call. The full replay runs without one.

## Ownership

- **One owner per viewer.** `ViewerAgent` is the only component that decides for a viewer. The harness hands each viewer's triggers to it one at a time, in order (`busy_until` per viewer), so two decisions for one viewer never overlap.
- **One active goal per viewer.** This is enforced by the database: `active_goal.user_id` is the primary key, so a second open goal raises `SingleGoalViolation`. The verifier checks the same invariant independently.
- **The plan belongs to the goal.** A home page and at most one planned message hang off the decision that opened the goal. When the goal closes (success, invalidation, replacement), its pending messages are cancelled through the `decision → messages` index.

## Planning

Goals are chosen and revised **when the viewer's situation changes**, never on a timer.

- **On a trigger:** if an open goal is still within its deadline, the agent continues it. Otherwise it evaluates every enabled goal from resolved facts, in a configured lifecycle order: activation → retention → monetisation (`config/agent.toml [planning]`). Every candidate is recorded with its verdict and reason.
- **On a live event:**
  - A purchase that is the open goal's success signal closes the goal, and the agent re-plans in-session.
  - A purchase that happened *before* the goal started (an offline-buffered event) marks the goal invalidated, because it was decided on stale facts.
  - An opt-out cancels pending messages on that channel.
- **On a control event:** a rule correction re-points only the open goals for that goal_id. A withdrawal rebuilds only the live pages, and cancels only the messages, that contain the title.
- **Goals that can't be measured are disabled.** This happens when the success event is null or never appears in the event stream (G10, G11). Vague rules get written interpretations with IDs (`config/goal_interpretations.toml`) that decisions cite as evidence.
- **"Do nothing" is a decision.** Holdout, stale trigger, store unreadable, no eligible goal, page still live, no content in the dialect, and low confidence are each a recorded NO_ACTION with a reason.

## Memory

`state/store.py` (SQLite) holds:

| Table | Purpose |
|---|---|
| `journal` | Append-only per-viewer record: the facts snapshot behind every decision, decisions, actions, goal events (opened, rule corrected, closed), outcomes and learnings |
| `active_goal` | The current goal and plan; indexed by goal_id so a correction finds affected goals without a scan |
| `idempotency` | One key per handled trigger and per judged outcome |
| `message_ledger` | Scheduled and sent push/WhatsApp per IST day, for the cap |

Reading the journal up to time *t* reconstructs exactly what the agent knew and did then (`python -m ott_agent explain --viewer … --at …`). Learnings are written after judging and read on the next decision: a goal that just failed is cooled down, and a channel whose messages are delivered but never opened is skipped.

## Facts and sources

`state/resolver.py` sees only events that had **arrived** by the decision time. Each fact carries its value, source, evidence refs and age. Each fact also has its own precedence rule, so no source wins by default:

| Fact | Precedence |
|---|---|
| subscription | events received after the snapshot > profile snapshot > derived (signed up inside the history window) > unknown |
| dialect for content | the app setting on the trigger > the latest event's app setting. Profile and campaign dialects are recorded as conflicts and never pick content. |
| consent | any opt-out event > profile opt-in > no consent |
| duplicate profile rows | latest `updated_at`; on a tie, merged conservatively |
| event time | `received_at` − offline buffering (`sent_at − occurred_at`), so phone clock skew cancels out |

Conflicts stay in the decision record with the rule that resolved them.

## Verification

`verify/` re-checks the decision's **serialized JSON**, which is exactly what would be published. It is independent of the planner:

- it looks up the arm itself and re-resolves the facts;
- it has its **own** eligibility implementation;
- a test fails the build if anything in `verify/` imports `ott_agent.agent`.

What it checks:
- **Decision:** holdout, duplicate, single goal, measurability, eligibility recheck, that every evidence ref resolves to something real that existed in time, and that model claims are supported.
- **Page:** dialect, availability through the 120-min TTL, quarantine, slug uniqueness, row size and type, no Continue Watching.
- **Message:** consent, quiet hours, the shared daily cap, copy length, forbidden phrases, prices, approved templates with variables sourced from the catalogue or pricing, and no instruction-like text.

A failed check names a component, and the publisher removes only that part and re-verifies: row → hero → page/message → NO_ACTION. Planned messages are re-verified again at send time, or at the end of the replay.

## Latency

- **Virtual clock, measured time.** The replay follows `deliver_at`. Each decision's service time is its measured processing plus simulated dependency time (model latency, slow store reads). `published_at = start + service`, so latency includes any queueing.
- **8 workers.** Under backlog, fresh triggers go before stale ones. If a trigger waits longer than 20 s the decision runs rules-only (load shedding).
- **The home-page path is rules-only and takes milliseconds.** The model is consulted only when two or more goals are eligible, with a 10 s per-call timeout.
- **Budgets per decision:** 8 model calls and 20 tool calls (`config/agent.toml`). Exceeding one becomes a recorded fallback or NO_ACTION.
- **Result on the replay day:** p95 1.2 s and max 3.3 s trigger-to-publish; 100% within 60 s.

## Failure recovery

| Failure | Response |
|---|---|
| Model timeout | No retry (latency already spent); rules decide |
| Malformed / schema-invalid model output | One retry within budget, then rules |
| Model provider down | Fail fast; the circuit breaker opens after 3 failures for 120 s |
| Unsupported model claim | Rejected and logged; rules decide |
| Store read timeout (degraded) | The agent's write-through cache; home page only, no messages |
| Store down | NO_ACTION (dependency_unavailable); writes buffered to a file and replayed on recovery |
| Duplicate trigger or live event | Idempotency key; recorded, no new action |
| Stale (offline-buffered) trigger | Ingested as history; NO_ACTION |
| Unknown control event | Logged and ignored |
| Pack schema drift | Missing files or columns, or more than 1% malformed records, stop the load with a clear error |

## Trust boundaries

- **Data is not instruction.** Catalogue text is scanned on load. Titles whose text addresses an AI agent (T0002, T0522) are quarantined and never used, and catalogue, event or search text is never sent to the model.
- **A model's output is a claim, not evidence.** It may only choose among goals the rules already found eligible, citing refs it was given. The verifier re-checks any claim that is used.
- **The planner is not its own verifier.** The verifier is a separate implementation that reads the published form.
- **Holdout has defence in depth.** The planner abstains, the verifier rejects, and the harness refuses to write output if any action targets a non-agent viewer.
- **Privacy.** The pack never leaves the machine in the default run. The optional real provider receives only pseudonymised derived features.

## At 10 million viewers and 40 dialects

The replay already uses the same boundaries this would need at scale:

- **Viewer agents:** a stream partitioned by `viewer_id`, so the one-owner-per-viewer rule holds across workers.
- **State:** journal plus active-goal rows in a key-value store, with a goal_id index for corrections.
- **Catalogue:** indexes and precomputed popularity per dialect.
- **Change propagation:** reverse indexes (title → live pages and messages, goal → open goals), so one change touches only what it affects.
- **Measurement:** the holdout stratified by dialect.

The rules-only fast path keeps the per-decision cost at milliseconds. Model calls stay limited to genuinely ambiguous goal choices.
