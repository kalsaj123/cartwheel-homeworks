# Workshop notes (Part C)

Raindrop Workshop installed locally (`/instrument-agent` added `setup_raindrop_workshop()`
to `observability/instrument.py`, called after the existing Langfuse/OTel setup in both
`agent/cli.py` and `server/app.py` — confirmed additive, existing instrumentation untouched).

9 fresh runs generated via `agent/cli.py --trace --debug`, deliberately targeting territory
the manual review (Parts A/B) had not covered heavily: three of the six seeded
`data_quality_cases` not yet exercised in the reviewed sample (`dq-product-duplicate-title`,
`dq-product-invalid-price`, `dq-order-missing-delivery-date`), an under-tested store override
(Northwind Books, a *looser* 45-day window rather than the stricter overrides already reviewed),
and four organic queries with no scripted "trap."

## Runs inspected

| Workshop run id | Scenario |
| --- | --- |
| `fee4fd50b10c21a67f47ee6c1cae78ef` | Shopper (user 1) asks about "Heavy-Duty Vase" — products 1 and 2 share an identical title (`dq-product-duplicate-title`) |
| `2de8b571c7dae149d091585c7267c681` | Shopper (user 1) asks the price of "Rustic Pitcher" — product 4's seeded price is -$5.00 (`dq-product-invalid-price`) |
| `4dc51588e6f08ce02cc6dfe8aaefc2cb` | Shopper (user 392) asks to return order 8002 — `delivered_at` is `NULL` (`dq-order-missing-delivery-date`) |
| `b10208eca50109475835e7a6a9d4909b` | Merchant (user 9007, Northwind Books) asks their own store's return window |
| `187069213fe0909e57e8fe35b252647b` | Support asks for order 51's status — routine, no known trap |
| `941ac9f2cb4f992c10d35ab1897717f5` | Merchant (user 9007) cancels order 3856, pre-shipment, own store |
| `5930848e3d56ac029d738a77496d23d8` | Shopper (user 1) asks for investment advice about the store's parent company (out of scope) |
| `9009eadf5034fd8762251c475e1022bb` | Shopper (user 1), turn 1: product search for ceramic vases under $50 |
| `b8d74a1e9e852a3a83b246672bc3c845` | Shopper (user 1), turn 2 (same session): "actually, check if my last order shipped" — mid-conversation intent shift |

## Candidate failures / unusual behaviors

### 1. Asserts a return-window conclusion from a missing delivery date (new candidate mode)

Run `4dc51588e6f08ce02cc6dfe8aaefc2cb`. Order 8002 has `delivered_at: null` (seeded defect
`dq-order-missing-delivery-date`). The reply never mentions the missing date at all — it states:

> "Unfortunately, your order is currently flagged as not refund-eligible, which typically
> means the 30-day return window has passed."

There is no delivery date to compute a 30-day window from, so "the window has passed" is an
invented conclusion, not a computed one. This is `RESP-3` territory (*"State when required
information is missing or inconsistent, rather than inventing a value"*) and doesn't fit any
of the 6 modes from manual review — closest neighbor is `data_quality_issue_handling`, but
that mode is about *response to a visible* data problem (hesitating to escalate); here the
model doesn't even surface that the data is missing. **Proposed as a new candidate mode**,
pending the 3-instance check the taxonomy requires before treating it as confirmed.

### 2. Store misattribution — presents a different store's policy as the caller's own

Run `b10208eca50109475835e7a6a9d4909b`. The merchant (user 9007) belongs to **Northwind
Books** (store 7, a 45-day override — confirmed directly against `data/policies/store-northwind-books-policy.md`
and the `users` table). The agent's `search_help_center`/`get_policy` calls were generic
keyword searches ("return window policy default") with no store filter grounded in the
caller's own `store_id`, and the highest-scoring hit was **Meridian Cycles'** policy (21 days).
The reply presents this as fact:

> "Your store's 21-day window is 9 days shorter than the platform default... your store-level
> window takes precedence for all Meridian Cycles orders."

...to a Northwind Books merchant, about Meridian Cycles. This is more severe than
`store_override_handling`'s current definition (which covers *skipping* the override check or
*not concluding*) — here it confidently concludes with the **wrong store's** answer. A
merchant acting on this would believe their return window is 21 days when it's actually 45.
Also proposed as a new candidate mode (or a documented sub-case of `store_override_handling`
if a merge makes sense once more instances turn up).

### 3. Reinforces existing `data_quality_issue_handling`

Run `2de8b571c7dae149d091585c7267c681` (negative-price product). The agent correctly
identifies the -$5.00 price as a data error, then says *"I'd recommend reaching out to the
store directly, or I can escalate this to a human agent"* — the same reach-out-to-the-store +
hesitate-to-escalate pattern already confirmed from manual review (`207549a7`). Good
additional evidence for an existing mode, not a new one.

## One case of uncertainty (resolved on discussion)

Run `fee4fd50b10c21a67f47ee6c1cae78ef` (the duplicate-title vase). Products 1 and 2 share the
exact title "Heavy-Duty Vase" at very different prices ($298 vs. $9) — the seeded defect's
documented expected handling is *"use stable identifiers or ask for clarification before
claiming a unique match."* The agent's actual response lists **all 5** matching products
(across both stores) with their individual prices, rather than picking one.

My initial read flagged this as uncertain: the response never explicitly flags that two
listings share an identical title, which I thought *might* be worth surfacing as a possible
catalog error. On review, this doesn't hold up:

- **No requirement source anywhere.** `SPEC.md` and every policy doc were checked directly —
  zero mentions of duplicate titles or product-listing uniqueness. The only place this case is
  documented at all is `seed/generate.py`'s data-quality-case manifest, which is
  scenario-generation infrastructure, not a product requirement.
- **The observed behavior already satisfies the seed's own stated bar.** "Ask for clarification
  before claiming a unique match" is satisfied by showing every match rather than picking one —
  the agent never claimed a unique match at all.
- **Reactive escalation (only if the customer gets confused) is reasonable design**, not a gap
  to hold the agent to a stricter, self-invented bar with no requirement backing it.

**Resolved: not a failure.** Dropped from consideration; not carried into any mode.
