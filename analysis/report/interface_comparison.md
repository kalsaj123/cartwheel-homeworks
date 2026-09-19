# Interface comparison

Part A of `homework/module-2/hw4.md`. The interface lives under
`analysis/review_app/` (`server.py` + `ui/index.html`).

## Friction observed in Langfuse's stock annotation view

- Annotation is built around structured "score configs" (numeric, categorical,
  or boolean), selected from a dropdown. There is no direct way to jot a quick
  free-text note without first defining a score config — it forces a
  structured score type before you can jot a quick note.
- Each tool call and its result is its own row in a nested span tree; reading
  a full turn means clicking into each tool call one at a time rather than
  seeing the conversation and tool activity together in one continuous view.
  This is slow and easy to lose your place in on a multi-tool-call turn.
- Multi-turn conversation grouping is weak on this (free/self-hosted) tier:
  the native "Sessions" view sits behind what looks like a paid-tier gate, and
  the Traces list's "Metadata" filter doesn't see `cartwheel.scenario_id` (it
  is nested under `metadata.attributes`, not flat trace metadata). Checked
  directly via the API (Langfuse `trace.sessionId`): it is simply `None` on
  every trace in this project, even though `server/app.py` sets
  `cartwheel.session_id` on the span. Net effect: there is no reliable way,
  free tier or API, to pull up all traces of one conversation by session id in
  this dataset — `cartwheel.scenario_id` is the only join key actually
  present, confirmed via the API and already what
  `analysis/helpers/normalization.py` groups by.

## Design retained from the reference interface

The text-select-to-annotate popover and margin-note layout
(`analysis/ui/index.html`'s highlight/margin-note mechanic) were kept
verbatim. It already solves the free-text-note friction above, and the
hover-linking between a highlight and its margin note works well for a dense,
multi-turn transcript.

## Design changed after inspecting real traces

The reference renders one Langfuse trace (one user turn) per screen. Pulling
real traces via the API for Part A showed two things the reference could not
have anticipated: (1) a multi-turn conversation is split across several
traces with no reliable session id to join them, and (2) grouping naively by
`scenario_id` is not safe — two traces can share a `scenario_id` because the
same scenario was *run twice* (a pilot pass and a final pass, ~15 minutes
apart, both starting with the identical opening message) rather than because
they are sequential turns of one conversation. `support-0243`, `support-0210`,
`support-0208`, and `support-0239` are all examples of this false-positive
grouping. The built interface (`analysis/review_app/server.py`,
`_split_into_runs`) only merges same-scenario traces into one conversation
when they land within 120 seconds of each other — a genuine followup turn in
this data lands ~10 seconds after the previous one (see `support-0002`),
while independent reruns land minutes apart. Traces that fail that check are
kept as separate conversations instead of being silently spliced together
into a fake multi-turn transcript.

## Additional changes found by hands-on testing of the reference interface

Running `analysis/server.py` directly (not just reading its code) surfaced
three more concrete gaps, all present in the reference and, until fixed, also
present in `review_app` since that code was carried over unchanged:

- **The annotation mechanic has zero on-page affordance.** Selecting text to
  annotate is documented only in a server-side `print()` statement and in
  SKILL.md — nothing in the rendered page tells a first-time reviewer that
  click-and-drag opens a note popover. Fixed with a one-line visible hint
  above the conversation.
- **The final reply is buried at the end of a long tool-call trail.** Both
  interfaces originally rendered strictly in execution order, so judging the
  outcome of a turn required reading past every tool call first. Fixed by
  adding a "final reply" callout immediately after each turn's user message,
  while keeping the full chronological detail (including that same reply, in
  its original position) unchanged below it — nothing is removed, and
  annotation indexing is unaffected.
- **The header carried no scenario context beyond role/store.** Pulling
  `expected` from `scenarios/final-results.jsonl` / `pilot-results.jsonl`
  (ground truth about what a scenario was designed to test, including known
  data-quality issues such as `dq-order-reversed-dates`) and cross-referencing
  it by `scenario_id` at conversation-build time makes that context available
  in the header. Also see the next section — the first version of this
  surfaced it as an always-open verdict, which was itself a mistake.

## Two bugs found only by using the built interface, not by reading its code

- **Tool calls silently disappeared.** Loading `support-0001` showed the user
  message and reply with no tool activity in between, looking like a
  rendering bug. It was an environment bug: `analysis/review_app/server.py`
  read `LANGFUSE_*` from `os.environ` with no fallback, so a server launched
  without first running `source .env` silently used the offline path
  (`scenarios/final-results.jsonl`, which only records final user/agent text,
  never tool calls) instead of erroring. Fixed by having the server load
  `.env` itself at startup (`_load_dotenv`, `setdefault` so a real `export`
  still wins) — no dependency added, no reliance on the caller's shell.
- **The scenario's designed outcome was shown as an open verdict banner.**
  The first version of the header enrichment (previous section) rendered
  `expected.outcome` and its reasoning inline and always visible — meaning a
  reviewer read the "right answer" before reading the trace at all. For a
  task whose entire point is independent discovery (open coding), that turns
  review into confirming a pre-computed conclusion. Fixed by moving it behind
  a closed `<details>` disclosure ("scenario design intent — open after
  you've formed your own judgment"), so the information is still there to
  cross-check afterward (and to cite as a requirement source in Part D), but
  can't leak into the first read.

## More metadata added after further hands-on use

Two more fields were requested after using the interface directly, matching
what Langfuse's own trace view shows: **user id** (was already present in
every trace's raw metadata as `cartwheel.user_id`, just never surfaced by
`normalize_trace()`'s curated `meta` subset — read from the raw `metadata`
dict instead, no core-file change needed) and **tool-call count per turn**
(already computed by `normalize_trace()` as `features.tool_call_count`;
`review_app` just wasn't threading it through). Both now show in the header
and per-turn, respectively. A third fix from this same round: the default
`/api/conversations` fetch limit (150) was smaller than the project's actual
trace count (~450), and since traces are fetched in `trace_id` order (unrelated
to `scenario_id`), a low limit silently hid an arbitrary subset of scenarios
rather than a clean "first N" slice — `support-0001` fell outside it entirely,
looking like a missing-data bug. Raised the default to comfortably cover the
whole project; the full fetch costs ~8s once per server run and is cached
after that, which is cheap enough not to default low for.

## Two more fixes from continued hands-on use

- **The final reply was shown twice.** An earlier fix (above) added a "final
  reply" preview right after the user message to avoid burying the outcome at
  the end of a long tool-call trail — but it left the *same* reply rendering
  again in its original chronological position too, which just reads as
  confusing duplication rather than a useful summary. Fixed by moving the
  reply, not copying it: it now renders exactly once, immediately after the
  user message, with the tool-call trail that produced it following after.
  Each message keeps the same stable `idx` it always had (its position in the
  flattened per-conversation item list), so reordering where something
  renders doesn't disturb annotation lookups, which key off that `idx`
  rather than DOM order.
- **Click-and-drag annotation frequently failed silently.** Root cause:
  wrapping the selection used `Range.surroundContents()`, which throws
  whenever a selection's start or end falls partway inside a formatted
  element — and replies are full of `**bold**` order numbers and policy
  names, so this was hit constantly, with no visible error. Replaced with
  `extractContents()` + a manual wrap-and-reinsert, which handles a selection
  spanning multiple/partial nodes correctly; verified against a selection
  deliberately crossing a `<strong>` boundary. Also changed the interaction
  itself: selecting text now shows a small "+" button (Google Docs/Medium
  style) rather than immediately focusing a note input, so an incidental
  drag while reading doesn't pop a note box open, and the affordance is a
  visible button rather than an undocumented keyboard-adjacent flow.

## Limitation remaining in the built interface

No clustering/map view was built (the reference's 2D scatter view). It is not
one of the interface requirements this handout section lists, and building a
real embedding+clustering pipeline belongs to Part B/D's sampling work, not
the interface itself — building it now would have been scope beyond what
Part A asks for. A second, smaller limitation: the labeling grid cannot
distinguish "explicitly reviewed and marked absent, but not yet saved because
the reviewer un-clicked it" from "never looked at" — both currently show as
`unset` until a Pass/Fail is saved.

A third, deliberately deferred limitation: **no latency is shown**, even
though Langfuse's own trace view has it. The raw trace object carries a
top-level `latency` field, but the shared `normalize_trace()` helper
(`analysis/helpers/normalization.py`, used by HW3/HW5 tooling too, not just
this interface) drops it — it keeps only *per-observation* latency, not the
whole-trace total. Adding it means either a small additive change to that
shared helper, or deriving it independently inside `review_app` from
observation start/end timestamps. Deferred rather than decided unilaterally,
since it touches a choice about whether to modify shared infrastructure.
