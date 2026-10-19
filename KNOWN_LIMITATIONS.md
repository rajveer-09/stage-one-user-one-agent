# Known Limitations

**Author:** Rajveer Sharma
**Date:** 19 Oct 2026

What was left incomplete on purpose, and what to keep in mind when reading the results. Written interpretations of vague goal rules are in `config/goal_interpretations.toml`; thresholds and budgets are in `config/agent.toml`.

## Measurement

- **The effect is small and not robust.** Goal success was +2.1 pt [−1.2, +5.4] with the default simulator seed and +5.2 pt [+2.0, +8.4] with a second seed. Play within 7 days did not exclude zero on either seed. One run does not settle it; several seeds or a larger replay would.
- **Secondary metrics are leads, not findings.** "Finished an episode within 3 days" excluded zero on one seed (+6.2 pt) but not on the other (+2.2 pt).
- **Outcomes come only from the supplied simulator.** No goal was tuned to its internals. Its session cut-off (10 min) differs from our stale threshold (15 min), and we kept ours.
- **Confidence is an evidence-quality score, not a probability.** The planned reliability table, checking confidence against outcomes, was not built.
- **Per-viewer "succeeded" is not causal credit.** For example, U-00649's purchase 2 minutes after the decision was a scripted organic event. Causal claims come only from the arm comparison.
- **A possible measurement asymmetry.** If the agent abstains *before* choosing a goal (store unreadable, budget exceeded), that viewer has no intended goal, while a holdout viewer in the same spot keeps a shadow goal. This did not happen at any first fresh trigger in this run.

## Decisions and content

- **Viewers whose app dialect has no catalogue content (`d7`, `NA`, unknown) get nothing.** No title can honestly be shown in a dialect that has none.
- **The G03 rule correction re-pointed 0 open goals**, because new viewers get G01 first. The path is covered by tests, not by this replay day.
- **"Has never finished an episode" (G02)** can only be checked within the 30-day history. For older viewers this is an assumption (`G02-a1`), with a confidence penalty.
- **The `wa_recommend_title_v1` WhatsApp template is never used.** Its `dialect_name` variable has no source in the catalogue or pricing.
- **About half of acting decisions plan a conditional follow-up push.** Each is cancelled if the goal succeeds first. Whether that volume is right is a product decision (D19).
- **Quarantined titles are blocked automatically, with no human review queue.** A title flagged by mistake would stay out until the pattern list changes.
- **A page rebuilt after a title withdrawal gets a fresh 120-min TTL.** The action format has no field to keep the original expiry.

## Engineering

- **The real-model provider (`--provider claude`) has not been run.** No API key was used. The submitted run uses the deterministic mock plus the pack's fault replay.
- **The store slowdown is simulated with an approximation:** 20% of reads time out at our 2 s read timeout and the rest take up to 2 s. The pack's "p95 8 s" is not modelled in full.
- **The replay is a single-process simulation of 8 workers.** Real concurrency, distributed state and partitioning are described in ARCHITECTURE.md but not built.
- **`sample_run/` is large (about 50 MB without `tmp/`).** Most of it is `viewer_state/`, which keeps every decision's full facts snapshot for auditing, and the simulator's raw output.
- **`explain` needs the local state database** (`sample_run/tmp/state.db`, git-ignored), so it works after running `run`.
- **No UI.** The brief puts the decide, act and measure loop first. Auditing is done through `explain` and the JSONL outputs.
