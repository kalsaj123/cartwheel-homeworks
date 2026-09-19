# HW4 review summary

## Reviewed sample: size and composition

125 scenarios, 136 distinct traces (11 scenarios are multi-turn), assembled across seven batches rather than one uniform draw:

| Batch | Scenarios | Purpose |
|---|---|---|
| `batch1_uniform` | 15 | Part B, uniform random draw |
| `batch1_cluster` | 15 | Part B, clustered draw (`analysis.helpers.selection`) |
| `batch2_intent` | 30 | Part B, stratified evenly across 8 `intent` values |
| `batch3_depth` | 25 | Part D, semantic-neighbor depth search on early candidate modes |
| `batch4_final_check` | 15 | Part B/D, final stability check batch |
| `partD_close_negative_search` | 21 | Part D, targeted search for close negatives on thin modes |
| `partD_structural_search` | 4 | Part D, structural search for the 2 code-check modes |

Role breakdown: 73 shopper, 32 merchant, 20 support-staff traces. (The `support-` scenario-ID prefix is a naming artifact of which scenario file a case came from — it does not mean the caller is support staff; 57 of the 101 `support-`-prefixed scenarios are actually shopper-role.)

Because several batches were built by targeted search rather than random sampling, this composition is deliberately non-representative of the full trace store — the fractions below are **sample fractions**, not prevalence estimates. Homework 5 will estimate prevalence from the complete Module 1 trace store.

## Final taxonomy: 7 modes

`gratuitous_escalation_offer`, `store_override_handling`, `unnecessary_reconfirmation`, `overstates_refund_capability`, `dispute_misidentified_as_refund`, `data_quality_issue_handling`, `asserts_eligibility_from_missing_date`. Full definitions, boundaries, evaluator types, and requirement sources are in `analysis/state/patterns.json`.

## Sample fractions (Part E: every mode applied to every trace)

Every one of the 136 traces was assigned an explicit present/absent judgment for all 7 modes (952 judgments total), written to Langfuse as scores and mirrored under `analysis/state/labels/`.

| Mode | Present | Fraction |
|---|---|---|
| `gratuitous_escalation_offer` | 13 / 136 | 9.6% |
| `unnecessary_reconfirmation` | 13 / 136 | 9.6% |
| `store_override_handling` | 12 / 136 | 8.8% |
| `data_quality_issue_handling` | 9 / 136 | 6.6% |
| `dispute_misidentified_as_refund` | 4 / 136 | 2.9% |
| `asserts_eligibility_from_missing_date` | 4 / 136 | 2.9% |
| `overstates_refund_capability` | 1 / 136 | 0.7% |

The two code-check modes (`store_override_handling`, `asserts_eligibility_from_missing_date`) were labeled by a structural script (`analysis/apply_structural_checks.py`) with every verdict spot-checked against the actual seed data before trusting it; the five LLM-judge modes were labeled by reading each trace against its mode definition, using a keyword pre-filter only to separate "trigger condition plainly doesn't apply" (fast, confident absent) from genuine candidates needing a close read.

## Final-15 stability check

`batch4_final_check` (the last 15 traces reviewed) produced **0 new modes**. It did surface 3 new positives, but all were additional instances of already-established modes, not new failure types: two more `store_override_handling` instances (`8e042b8795...`, merchant-facing; `1e1482293e...`, shopper-facing) and one more `overstates_refund_capability` instance (`277bc33c7b...`). The taxonomy was treated as stable at that point and no further batch was needed.

## One taxonomy revision: merging `store_policy_misattribution` into `store_override_handling`

**The gap.** Early axial coding surfaced two superficially different symptoms: (1) the agent skips checking for a store-specific policy override and answers from the platform default alone, or explains the two-tier platform/store rule without ever concluding for the caller's specific case, and (2) the agent confidently cites a *different* store's policy as if it were the caller's own. The second one was tracked as its own candidate mode, `store_policy_misattribution`, based on one clear instance: a Northwind Books merchant asking about their own return window was told Meridian Cycles' terms instead.

**Why it stayed separate at first.** Misattributing a policy to the wrong store felt qualitatively different from just failing to check — it looked closer to a permissions problem (showing one party a different entity's information) than an accuracy problem.

**Why it was checked, not assumed.** Before deciding anything, `store_policy_misattribution`'s permissions angle was checked directly against `AUTH-1` and the policy corpus: `AUTH-1` restricts *orders* by store ("view store's orders: merchant, own store only") but places no such restriction on policy documents, and every policy file's front matter is `audience: all`. So citing another store's real, legitimately-accessible policy isn't an access-control violation — it's a grounding/accuracy failure: real information, attached to the wrong entity.

**The merge test.** Part D's guidance is to merge two symptoms when one product fix would correct both. All three symptoms trace back to the same root cause: the agent doesn't consistently ground store-policy retrieval in the caller's actual `store_id` (or an order's actual store), and falls back to ambiguous keyword search instead. A single fix — always resolve the caller's own store via `ctx.store_id` (or the order's store) before answering, never via generic search — would prevent the skip, the non-conclusion, and the misattribution alike. `store_policy_misattribution` had also stayed stuck at exactly one confirmed instance even after a dedicated structural search for more, while `store_override_handling` had grown to seven — a sign it wasn't sustaining life as an independent mode.

**What happened to the evidence.** The single `store_policy_misattribution` instance was absorbed into `store_override_handling`'s `example_trace_ids` rather than being deleted. The original mode definition, its one example, and the reasoning for the merge are preserved under `patterns.json`'s `merged_modes` array (with `merged_into`, `merged_on`, and `reason_for_merge` fields), so the path from observation to final category stays inspectable rather than disappearing.

**Aftermath, found during Part E.** Applying the merged definition across the full 136-trace sample surfaced a second, independent misattribution instance that the original single-symptom review had missed and had actually mislabeled as a *correct* handling: a merchant trace (`pilot-0015::run2`) where the agent told a Juniper Home Goods merchant (store 2) that their store was "Meridian Cycles" and gave that store's window instead. The original review trusted the agent's own claim about its store identity without checking it against the database — the review interface itself had no way to display the caller's actual store name at the time, which was a separate, un-taxonomy-related interface bug fixed during Part E (`analysis/helpers/normalization.py`, `analysis/review_app/server.py`). `store_override_handling` now has 9 confirmed positives.

## SPEC.md

No changes applied. `store_override_handling`'s proposed revision (documented in full in `patterns.json`'s `requirement_source` field) would add: resolve and cite the caller's own store's policy via `ctx.store_id` or the order's actual store before answering a return-window or restocking-fee question — never present the platform default without checking, never explain the two-tier rule without concluding for the specific caller, and never present a different store's policy as the caller's own.

## Deliverables status

- `analysis/review_app/` — review interface, extended through Part E (sticky Labeling-grid header, structural-suggestion confirm workflow, store name/ID surfaced in the conversation header).
- `analysis/state/sample_manifest.json`, `annotations.json`, `patterns.json`, `suggestions.json`, `suggestions_rejected.jsonl` — current.
- `analysis/state/labels/*.jsonl` — one file per final mode, 136/136 traces labeled in each.
- `analysis/report/workshop_notes.md`, `interface_comparison.md` — from Parts A and C.
- `SPEC.md` — unchanged; proposed revisions recorded, not applied.
- Video — left to the student, not attempted here.
