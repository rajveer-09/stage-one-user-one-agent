# AI Collaboration Log

**Author:** Rajveer Sharma  
**Date:** 18–19 October 2026  
**AI Tool:** Claude Code (Claude Opus 5.5), used through VS Code for planning, implementation, test generation, debugging, and documentation drafts.

## Engineering principle

**AI accelerated the implementation; evidence determined what I accepted.** I did not treat generated code, passing tests, or plausible explanations as proof of correctness. I validated behavior against the real data pack, independently reproduced counts, inspected actual records, and designed adversarial tests to expose failures before accepting changes.

## 1. How I directed the work

### Plan before implementation

I started with the assignment brief and structured the work into eleven implementation phases, P0–P10, with explicit deliverables, tests, and exit gates. Each phase had to satisfy its verification criteria before the next phase could proceed. This kept the implementation focused on the required **decide → act → measure** loop rather than expanding into an unnecessary UI.

### Profile the data before writing core logic

I delayed core implementation until I had profiled the data pack and checked the assumptions in the implementation plan against actual records. The main findings, with counts, are summarised in `sample_run/final_report.md` ("Data issues found and how they were handled"). This exposed data-quality issues and edge cases that would otherwise have become incorrect application logic.

### Keep engineering judgment explicit

I used AI to explore implementation options and propose sensible defaults, but retained responsibility for reviewing those decisions. I kept a decisions log of 38 judgment calls during the build, with the rationale, alternatives, and where behavior can be changed. The ones that shape behavior are reflected in `config/agent.toml`, `config/goal_interpretations.toml` and `KNOWN_LIMITATIONS.md`.

Examples include interpreting “recently lapsed,” abstaining when no suitable dialect-specific content exists, selecting follow-up timing, and reporting simulator results that vary across random seeds. Recording these choices made assumptions reviewable rather than embedding them silently in code.

### Prioritize reliability and safety over model dependence

Following the brief, I prioritized deterministic decision-making, independent verification, failure recovery, replay, and impact measurement. The deterministic mock provider remains the default, and the real-model provider is optional and disabled for the submitted run. This keeps evaluation reproducible and avoids requiring an API key.

### Retain ownership of the repository

I reviewed and corrected AI-generated changes, maintained control of Git, commits, and the final repository state, and kept code comments focused on explaining non-obvious behavior and design decisions.

### Verify the complete submission

I checked the implementation against the assignment's deliverables and added documentation, generated reports, and a fresh-clone verification step. The final check was intended to establish that the project could be installed and run from a clean checkout, rather than relying on my existing development environment.

## 2. How I verified AI-generated output

I used multiple forms of verification because no single test can establish correctness across the entire system.

| Verification method | Application and evidence |
|---|---|
| **Real-data execution** | Ran the loader, resolver, planner, and replay against the supplied pack and reproduced the independently calculated data-profiling counts. |
| **Record-level inspection** | Examined real records for three-way dialect conflicts, unsupported model claims, brand-new viewers, and purchases arriving two minutes after a decision. |
| **Full-trigger distribution review** | Reviewed all 2,679 triggers for goal selection, abstention reasons, skipped messages, and timing patterns. |
| **Adversarial verifier tests** | Constructed invalid decisions by breaking one rule at a time. Covered 15 rule types and asserted which check and component should reject each case. |
| **Mutation testing** | Deliberately broke rules to confirm that the tests detected the regressions rather than passing without exercising the relevant safeguards. |
| **Determinism testing** | Compared two full-pack runs: all 2,774 decisions matched, excluding timing fields. |
| **Statistical reference checks** | Validated Newcombe's confidence interval against an example from Newcombe (1998) and checked Wilson interval calculations against a known reference value. |
| **Second-seed evaluation** | Re-ran the simulator with another seed and reported seed-sensitive results side by side rather than selecting only the more favorable result. |
| **Fresh-clone verification** | Tested the non-ignored repository files in a clean environment, including installation, the documented run command, and tests. |
| **Report reconciliation** | Re-derived reported numbers from generated outputs and corrected discrepancies between earlier report drafts and the final run. |

These checks address different failure modes: data assumptions, business rules, invalid model output, nondeterminism, statistical interpretation, and reproducibility.

## 3. What the verification process caught and changed

The following examples show where AI-generated output was rejected or revised based on evidence.

| # | Initial AI-generated issue | How it was detected | Correction |
|---:|---|---|---|
| 1 | Duplicate detection retained every event's full payload in memory. | Code review before the real-data run. | Stored a hash instead, reducing unnecessary memory retention. |
| 2 | The profiler classified all 2,176 duplicates as having different payloads. | Field-by-field comparison showed that only `received_at` differed. | Excluded delivery-specific fields from payload comparison. |
| 3 | Duplicate-profile resolution used `snapshot_at` as the tie-breaker. | Real data showed identical snapshot timestamps; file order would have selected the wrong row in 15 of 30 cases. | Used `updated_at` as the tie-breaker. |
| 4 | Episode records were joined using `title_id`. | Inspection showed that episodes were keyed by `content_slug`. | Corrected the join key. |
| 5 | Trigger age was calculated naively from delivery and occurrence timestamps. | This concealed the 04:40 UTC offline-event burst because of phone-clock skew. | Added skew-aware age handling; 221 of 237 burst triggers were classified as stale. |
| 6 | Goal G07, “resume series,” could select a series containing quarantined titles. | Full replay over the real triggers. | Required the series to be promotable; the corrected run reported zero decisions touching quarantined titles. |
| 7 | The verifier checked model claims without validating the goal's target series. | Reviewed the decision-to-verifier wiring. | Corrected the verification logic so that G07 claims were checked against the relevant target series. |
| 8 | The mock model accepted evidence references without validating their support. | Traced how evidence flowed through the decision pipeline. | Corrected the evidence handling so unsupported claims could be rejected by verification. |
| 9 | After a purchase closed a “first paid plan” goal, replanning selected the same goal again. | Inspected the replay control log and the facts available to the resolver. | Narrowed the exclusion to the previous app-open context and added a regression test. |
| 10 | The withdrawn-title check reported nine violations. | Inspected the actual published page contents. | Identified a false positive caused by title names appearing in explanatory reason fields; the rechecked violation count was zero. |
| 11 | A later correction making a goal unmeasurable could overturn an already-won goal. | A small failing test fixture exposed the flaw in the outcome judge. | Limited the short-circuit to goals that were never measurable and added tests for both cases. |
| 12 | Repeated report execution appended the same outcomes and learnings again. | The explanation output showed each outcome three times. | Added claim-key-based idempotency. |
| 13 | The impact report labelled “served versus everyone else” as “Agent arm versus Holdout arm.” | Inspected the generated report. | Corrected the labels to reflect the actual comparison and avoid implying randomized assignment where none existed. |
| 14 | A secondary metric with a confidence interval excluding zero could be interpreted as a positive finding. | Reviewed multiple comparisons and checked the result against a second simulator seed. | Reported it as a lead for further testing, not an established finding. |
| 15 | Identical inputs produced different decision text in 67 of 2,774 decisions. | Compared full-pack outputs. | Removed a millisecond timestamp from the generated reason, restoring stable decision text. |
| 16 | A deduplication fixture represented a redelivered event with inconsistent phone timestamps. | Two deduplication tests failed. | Corrected the fixture to represent the intended event semantics rather than weakening the assertions. |
| 17 | A `.gitignore` entry excluded the source package under `src/ott_agent/pack/`. | The fresh-clone check revealed that the package was missing. | Changed the ignore pattern from `pack/` to `/pack/`, preserving the source package in Git. |
| 18 | Final-report figures were carried over from an earlier run, including 475 model agreements and an incorrect purchase-related cancellation explanation. | Re-derived the figures and cancellation causes from generated outputs. | Corrected the agreement count to 478 and updated the cancellation explanations. |
| 19 | Smaller defects included a console encoding crash on `→`, an unnecessarily complex time-window expression, JSON serialization failures for tuple keys, duplicate evidence references, and an empty loop. | Executed and inspected the affected code paths. | Fixed the individual defects and re-ran the relevant checks. |

The important outcome was not simply that defects were found. Each correction was tied to a reproducible observation, a code change, or a test intended to prevent the same failure from returning.

## 4. Safety and evaluation decisions

Several implementation choices were deliberate safeguards rather than shortcuts.

- **No simulator-specific policy fitting:** I did not copy the simulator's internal 10-minute session cutoff into the agent's decision policy. The policy should follow the assignment's requirements, not exploit an implementation detail of the evaluator.
- **Untrusted content remains data:** Catalogue descriptions, event text, and search text are not treated as instructions to the model. The model is not shown catalogue, event, or search text in the implemented flow, reducing the risk of injected instructions influencing decisions.
- **Independent verification before publication:** Model output is not treated as evidence of eligibility or policy compliance. Decisions must pass verification before publication.
- **Safe degradation:** The mock/replay setup exercises model faults, malformed output, and fallback behavior. Optional model functionality is not required for the standard evaluation run.
- **Honest impact reporting:** The report distinguishes observed results from robust conclusions. The effect was small and not consistent across seeds; the earlier pilot's reported +22.4 percentage-point uplift became −1.3 percentage points when evaluated using the randomized arms.

## 5. Decisions and responsibilities retained by me

AI generated implementation proposals, tests, and documentation drafts, but it did not replace my responsibility for the final engineering decisions.

- The 38-entry decisions log made assumptions, alternatives, and change locations explicit for review.
- I used real records, execution results, and failing tests to decide whether proposed behavior was acceptable.
- I corrected generated logic when its assumptions conflicted with the actual data or assignment requirements.
- I kept the real-model provider optional. Although `--provider claude` was implemented against the official SDK, **it was not executed**. No API key was used, and the data pack was not sent to an external model provider during this work.
- I did not present a favorable simulator result as proof of real-world impact. Results that varied across seeds were reported with that limitation.

## 6. Implementation phase log

| Phase | Date | Main deliverables | Exit-gate evidence recorded |
|---|---|---|---|
| Plan | 18 Oct | Implementation plan, organized into P0–P10. | Phased scope and verification criteria defined. |
| P0–P1: Data exploration | 18 Oct | Project scaffold, data-profiling script. | Data traps counted; data findings and `goal_interpretations.toml` produced. |
| P2: Data and policy | 18 Oct | Loader, events, catalogue, goal registry, policy. | Loader counts matched findings; scanner flagged T0002 and T0522. |
| P3: Viewer state | 18 Oct | Fact resolver and durable store. | Facts resolved for all 2,679 triggers; recorded p99 latency of 0.5 ms. |
| P4: Decision planning | 18 Oct | Goal planner, home page, messaging, confidence, decision records. | Draft decisions generated for all triggers; quarantined-series issue corrected. |
| P5: Verification and publication | 18 Oct | Independent verifier and publish flow. | Cross-process duplicate rejection and 15 adversarial test types. |
| P6: Model integration and faults | 19 Oct | Model provider, fault handling, budgets, advisor. | Unsupported injected claim rejected; malformed output retried before rule-based fallback; circuit breaker exercised during outage. |
| P7: Replay and scheduling | 19 Oct | Replay harness, scheduler, reverse indexes. | Replay surprises traced in the control log; repeated-goal issue corrected. |
| P8: Outcome and impact analysis | 19 Oct | Judge, impact analysis, latency reporting, explanation. | Intention-to-treat analysis with confidence intervals; zero simulator policy problems reported; outcome and report issues corrected. |
| P9: End-to-end validation | 19 Oct | End-to-end and invariant tests. | Determinism and second-seed checks completed; nondeterministic reason text corrected. |
| P10: Submission and documentation | 19 Oct | README, architecture, known limitations, final report. | Fresh-clone issue corrected; report figures reconciled against outputs. |

## 7. Conclusion

This assignment was an exercise in using AI as an engineering accelerator without delegating engineering judgment to it.

The most valuable part of the workflow was the feedback loop between generated code, real-data execution, independent checks, adversarial tests, and corrected implementation. It exposed issues that a successful happy-path run would not have revealed, including incorrect joins, stale-event handling, unsupported evidence, repeated decisions, misleading report labels, and packaging defects.

The final implementation and its reported results should be judged by the reproducible checks, generated artifacts, and documented limitations—not by the amount of code produced or the apparent confidence of the AI assistant.

**My takeaway:** AI can increase implementation speed, but correctness, safety, reproducibility, and honest measurement still require deliberate engineering and independent verification.