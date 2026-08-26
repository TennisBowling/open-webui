# Conversation compaction — design

> Status: IMPLEMENTED (2026-07-30). This document remains the authority for
> *why*; [`compaction.py`](./compaction.py) is the implementation and carries the
> local detail. Tests: `test/util/test_compaction.py`,
> `test/util/test_read_tool_result_tool.py`.
>
> | Section | Where it lives |
> |---|---|
> | §1 injection point, §2 anchor + cut, §3 envelope, §4 mechanical generators | `utils/compaction.py`, applied at the top of `blocks_to_api_messages` |
> | §5 trigger | `utils/middleware.py` `_maybe_compact_between_rounds` (mid-turn) and `compaction.maybe_compact_at_turn_start` called from `main.py` (inter-turn) |
> | §6 summarizer | `compaction.generate_compaction_narrative` / `extract_summary` |
> | §7 read-back | `utils/read_tool_result_tool.py`, bound as `builtin:read_tool_result` |
> | §8 UI | `src/lib/components/chat/Messages/CompactionBlock.svelte` + `GET /chats/{id}/messages/{mid}/compaction/{block_index}` |
>
> Knobs: `ENABLE_CONVERSATION_COMPACTION` (default on), `COMPACTION_THRESHOLD`
> (default 0.80) in `env.py`.
>
> The §5 token-accounting audit is **done and clean**: this fork has no
> `merge_usage()`. `response_usage` in `utils/middleware.py` is plain-assigned per
> usage chunk. Measured against production data — on turns of 180-280 rounds the
> value stored on the message equals the LAST round's `prompt_tokens`
> (14.9k / 13.8k / 326.9k / 292.1k), not the sum across rounds
> (292.8k / 291.2k / 43.6M / 31.1M). Upstream's shape would have compacted after
> about three rounds of a long turn.
>
> Companion to [`REASONING_DETAILS.md`](./REASONING_DETAILS.md). Read that first if
> you're touching the outbound message path — `blocks_to_api_messages` is the single
> gate every request funnels through, and compaction sits directly on top of it.

---

## TL;DR — the two hard rules

1. **Nothing mutates history until the threshold. Then one clean break.** No
   incremental tool-body eviction, no rolling truncation. Every mutation of history
   invalidates the prompt-cache prefix from that point forward, and the agentic loop
   re-sends the whole conversation every round — so continuous erosion means paying
   full input price on every round after every edit. One deliberate cut, one cache
   invalidation.
2. **Anything that must not be lost is generated mechanically, never by the
   summarizer.** The LLM writes prose. It does not write the list of user
   instructions and it does not write the tool index. See §4 for why this is the
   single most-repeated lesson in the prior art.

---

## 1. Where the summary goes

The compacted context is injected as **the first user message of the outbound
request**. The system prompt is untouched.

```
[system]                    ← byte-identical to an uncompacted request
[user]  <compacted_context> ← the summary
[...]                       ← every message after the cut, verbatim
```

**Why not append it to the system prompt** (which is what upstream open-webui does):
the system message is the very front of the prefix, so appending there invalidates
system *and* tool definitions on every compaction. Injecting at the cut keeps the
system+tools prefix cached and invalidates only from the cut forward. Upstream shows
no cache awareness anywhere in its compaction path; don't inherit that.

---

## 2. Anchoring — a content block, and the tree does the rest

The compaction record is **a `content_blocks` entry**, inserted at the cut index:

```json
{"type": "compaction", "compacted_at": "...", "narrative": "...", "covers": 47}
```

Not a new column on `chat_message` (which is what upstream did). The block form wins
on four counts at once:

- **The anchor *is* the block position**, so an intra-block cut needs no extra
  machinery; an inter-turn cut is just index 0 of a message.
- **Persistence, multi-client sync, and the `_rev` manifest** all ride machinery that
  already exists — no migration, no new column.
- **The UI marker is one more block renderer**, alongside `text` / `reasoning` /
  `tool_calls` / `user_steer` / `tool_selection_change`.
- **Tree scoping is inherited**, because the block lives in a message and
  `_walk_messages_from_leaf` (`utils/chat.py`) walks `parentId` from the leaf.

> A compaction applies to a path iff its message is an ancestor of the leaf. Use the
> **last** such block in the chain; skip everything before it when assembling.

Since rewind/edit create *siblings* sharing the entire ancestor prefix (see
`open_webui_rewind_feature`), branch behaviour falls out with no invalidation logic:

| Event | Result |
|---|---|
| Rewind/edit *after* the anchor | anchor still an ancestor → summary still applies |
| Rewind/edit *before* the anchor | new branch's chain lacks the anchor → full history |
| Branch switch | whichever anchors are on that path apply |
| Anchor message deleted | summary goes with it → full history |
| Repeated compactions | last anchor wins; drop everything before it |

**Nothing is deleted.** Blocks before the compaction stay in storage for read-back and
for the transcript; only the outbound payload skips them.

**Cut only at a completed `tool_calls` block boundary.** A `tool_calls` block holds
the whole parallel batch — calls *and* results — so cutting there can never dangle a
`tool_use`. `getRewindCutIndices` (`src/lib/utils/retryLastRequest.ts`) already
computes this exact boundary set for rewind. Upstream's compaction is turn-blind and
splits tool-call sequences in half (their issue #27035); reusing rewind's invariant
avoids the entire bug class.

**Mind the dense invariant.** `content_blocks` must stay hole-free (see
`open_webui_content_blocks_dense_invariant`). The insert has to go through the path
that already guarantees density — reducer plus write choke — not around it.

---

## 3. Message format

Deliberately flat. Tokens spent on structure are tokens not spent on content, and a
baroque envelope risks confusing the model receiving it.

```xml
<compacted_context compacted_at="2026-07-30T15:22:41Z">

<note>
Earlier messages in this conversation were summarized to free context — often
in the middle of ongoing work. Nothing is lost: retrieve any tool result in
full with read_tool_result(ref).
The summary below is a lossy digest written by a separate pass that could not
see your unfinished plans, so it may understate what remains. Judge what is
still unverified or unexplored against the verbatim user instructions and keep
working until the request is actually satisfied — a polished summary is not
evidence that the work is done. Do not redo work Findings already covers.
</note>

<user_instructions verbatim="true">
<message>Research how X works and write it up</message>
<message>actually focus on Y instead</message>
<message>also compare against W</message>
</user_instructions>

<tool_calls>
<call ref="m3/c001" name="web_search" args='{"query":"openai compaction api"}'>10 results, 18.8KB</call>
<call ref="m3/c002" name="web_fetch" args='{"url":"https://developers.openai.com/..."}'>1 page, 42KB</call>
</tool_calls>

<narrative>
## Findings
## Decisions
## Current State
## Next Steps
</narrative>

</compacted_context>
```

Notes on what is deliberately absent:

- **No per-message `seq` or timestamp.** Document order already encodes sequence. One
  `compacted_at` on the root gives the temporal anchor; per-message stamps are pure
  token cost.
- **No `<steer>` / `<queued>` distinction.** A steer block and a queued send are both
  just user messages by the time they reach the model. The distinction is internal
  bookkeeping and would only invite the model to treat them differently.
- **Narrative goes last** so `Current State` / `Next Steps` sit adjacent to the live
  conversation that follows.

### The re-execution hazard

`<user_instructions>` is a list of things the user *asked for*, most of which are
already done. Presented bare, a model may restart from instruction #1. Two defenses,
both required:

- the `<note>` explicitly says to continue from Next Steps, not restart;
- the narrative uses **temporal anchoring** — "Sent the proposal email on 2026-07-30",
  not "email John about the proposal" (Hermes's technique, for exactly this failure).

### Escaping — for correctness, not security

This instance is single-user and trusted, so user content is not treated as an
attack surface. The escape exists because of **accidental** collision: verbatim user
content containing `</user_instructions>` breaks the envelope, and in a project where
code, XML, and config get pasted constantly that is a when-not-if bug. (The design
conversation that produced this document quoted the tag repeatedly — compacting that
chat would have broken it.)

Fix surgically: neutralize only the exact closing delimiter and pass everything else
through byte-for-byte. Escaping all `<`/`>` would mangle every code snippet the model
later reads back, which defeats the point of keeping user messages verbatim.

Genuinely untrusted content is a separate matter: fetched pages and search results
are third-party, and a research turn reads a lot of them. Under this design they
never appear verbatim in the compacted context — only tool name, args, and a size
descriptor. Don't widen that later by inlining result snippets into the index.

---

## 4. Mechanical vs. LLM — and why

**Mechanical sections are regenerated deterministically from the tree walk on every
assembly.** They are *not* inherited from the previous summary. Because the original
messages never leave the database, these are **lossless across N compactions** — the
fifth compaction still carries every user instruction and every tool call from the
entire session. Only the narrative erodes, and it's the part that tolerates erosion.

This is also what makes repeated compaction strictly better than a sliding window.

Determinism is preserved: a pure function of persisted state produces identical bytes
every assembly, so replay matches live and the cache holds.

Derivable mechanically from the existing data model — none of it should ever be a
summarization task:

| Section | Source |
|---|---|
| user instructions | `role: user` messages + `user_steer` blocks |
| tool call index | `tool_calls` blocks + `tool_result_bodies` descriptors |
| subagent runs | `subagent_runs` |
| attachments | `message.files` |
| tool-set changes | `tool_selection_change` blocks |
| errors | persisted terminal-error payloads |

### Why this rule exists

Four independent codebases converged on it after being burned:

- **Factory.ai**, measuring three production systems over 36,611 real messages, found
  "artifact trail" (which files/tools were touched) the **worst-scoring dimension for
  all three** — 2.19–2.45 out of 5, including OpenAI's and Anthropic's. Their
  conclusion: needs a separate index, not better summarization.
- **Hermes** shipped a bug where the summarizer paraphrased a reload marker into
  "some skills were loaded", erasing the instruction. Fix: mechanical re-injection
  *after* the LLM call.
- **Cline** computes its Files section from parsed tool-call history "so file paths
  can't be hallucinated away".
- **LibreChat** mechanically extracts failed tool results because "LLMs often omit
  specific failure details from their summaries".

---

## 5. Trigger

```
last response's usage.total_tokens >= 0.80 * context_length
```

Checked **before every model request**, including between rounds inside one agentic
turn — not only at turn boundaries. This is OpenHands' shape: one gate evaluated on
every step, so there is no mid-turn vs. inter-turn distinction to keep in sync. It is
also the only design that helps a single long research turn, which message-boundary
compaction cannot touch.

`total_tokens` (prompt + completion of the last round) is the right quantity: the
previous round's output becomes part of the next round's input, so it is already the
floor of what the next request will cost. 0.80 matches goose's
`DEFAULT_COMPACTION_THRESHOLD`.

**`context_length` unknown ⇒ never auto-compact.** `resolve_context_length` returns
`None` rather than `0` precisely so this stays decidable (llama-swap declares no
window).

**Audit token accounting before wiring this up.** Upstream's `merge_usage()` *sums*
`input_tokens` across every call in the tool loop instead of overwriting, so their
threshold reads a wildly inflated number and compacts far too early (issue #27031).
Whatever we read `total_tokens` from must not have that shape.

---

## 6. The summarizer call

- **Model:** the chat's current model.
- **No `max_tokens`.** Err long — the instruction is to retain maximum detail. Crush's
  prompt is the right instinct: "No limit. Err on the side of too much detail rather
  than too little."
- **Validate before persisting.** Upstream's #27604 is the cautionary tale: they
  hardcoded `max_tokens=1000`, silently accepted `finish_reason: length`, and stored
  the truncated **`reasoning_content`** as the summary. Take message content only,
  never reasoning; refuse to persist an empty or truncated result.
- **Generate once, persist, never regenerate.** Required for replay determinism and
  cache stability — and independently supported on quality grounds: Factory found
  regenerate-from-scratch (Anthropic's SDK) drifts across compaction cycles, while
  anchored-iterative does not.
- **Cache-aware invocation:** LibreChat's `summarizeWithCacheHit()` appends the
  summarization instruction to the *raw* conversation so the system+tools prefix
  still hits cache. Only valid when the summarizer model equals the chat model —
  which is our configuration.
- **Tell it truncation is expected.** Slim/lazy tool results already carry
  `content: ""` plus a descriptor. Without a heads-up the summarizer treats its own
  inputs as suspicious. LibreChat's wording: "If a tool result appears truncated,
  that's just a display artifact from context management: the tool executed fully."

### Second and later compactions

Input is the **full previous narrative plus the new span**. The instruction is
merge-not-append: preserve still-true detail, drop what has been resolved or
superseded, integrate new facts. Mechanical sections are rebuilt from scratch, so
nothing in them degrades.

### Failure handling

Do **not** fall back to full history — if we are compacting because we are over the
limit, an uncompacted request just fails upstream anyway. Degrade, then fail visibly.
goose retries stripping progressively more tool responses from the middle
(0/10/20/50/100%) before hard-erroring; Codex trims oldest-first and then surfaces a
visible error. Nobody in the coding-agent set silently continues.

---

## 7. Read-back

The bodies are still in `tool_result_bodies`, and
`GET /{id}/messages/{message_id}/tool-results/{tool_call_id}` already resolves them
(live stream → persisted → inline). `utils/lazy_blocks.py` owns that contract. The
read-back tool is a model-facing wrapper over machinery that already works — the
`ref` in the tool index is the handle.

**Do not rely on the model choosing to use it.** This is why the tool index is
mechanical and complete: MemGPT's own paper reports the model "will often stop paging
through retriever results before exhausting the database", and only 2 of ~12 systems
surveyed have any read-back at all. The index is the load-bearing part; the tool is
the escape hatch.

---

## 8. UI

Anchored to a message, so it renders at that message's position — the same insertion
pattern `RewindBoundary.svelte` already uses.

- A divider: `Context compacted · 47 messages → 3.2k tokens · 15:22`
- Click to expand the full `<compacted_context>` in a collapsible panel.
- Pair with a context-usage meter now that `context_length` resolves, so compaction
  is never a surprise.

Because we author the summary ourselves it is fully human-readable — a real advantage
over OpenAI's opaque `cmp_*` items, which Factory specifically criticized: "you
cannot read the compressed output to verify what was preserved."

**The panel shows the bytes that were actually sent, not a re-render of them.** The
envelope is assembled per request and was originally not persisted at all (only the
narrative is; the mechanical sections are a pure function of the tree), so the endpoint
reconstructed it. That is not good enough for the one job this panel has: a
reconstruction can differ from the payload — the send path folds attached-file text
into user message content, and a second cut inherits its instruction list from the
private carrier rather than from a tree walk. "Verify what was preserved" means
verifying the real thing.

So the outbound path records it. `capture_compaction_envelope` runs at the HTTP
boundary in `routers/openai.py`, alongside `strip_compaction_carry` and for the same
reason — it is the last point where the final bytes and the carrier that says which
anchor produced them still exist together. `record_sent_envelope` writes them back onto
the block, detached from the request and idempotent (one cut spans every round of an
agentic turn, so a seen-set keyed on the bytes keeps it to a single read+write).

Two details that are load-bearing:

- **The mid-turn anchor is written by the stream, not the router.** It lives on the
  in-flight assistant, which is API-shaped between rounds and carries no message id.
  `capture_compaction_envelope` returns `message_id: None` for it and
  `record_sent_envelope` declines — because a whole-list `content_blocks` write from
  the router would truncate whatever blocks the round appended in the meantime.
  `_maybe_compact_between_rounds` instead mutates the live anchor in place so the
  bytes land through `checkpoint_stream_state`, the writer that already owns that row.
- **The bytes never ship with the chat.** They restate every verbatim user instruction
  and the whole tool index, and they sit behind a click, so
  `slim_content_blocks_for_read` projects `envelope` down to
  `envelope_size`/`envelope_lazy` — the same shape heavy reasoning text uses. The
  stored row keeps the body; the endpoint reads it there.

The response carries `source: "sent" | "rendered"` and the UI states which it is
showing. `rendered` is the fallback for anchors written before this existed, or a cut
whose turn never reached the wire.

---

## 8b. `/compact` — the manual command

Typed bare in the composer (exact match, case-insensitive — `is_compact_command`).
Anything longer that merely starts with `/compact` is an ordinary instruction and is
delivered as one; silently eating a real request because it shares a prefix is the
worse failure.

It runs the same cut, envelope and anchor as the automatic gate. What it overrides is
**policy only**: the feature flag, the threshold, and an unresolvable `context_length`
(display-only on the block, so not knowing the window is no reason to refuse). It does
NOT override `has_uncompacted_span` — that is arithmetic, not policy: with nothing
after the last anchor there is nothing to summarize and a second anchor would only
restate the first. The user is told "nothing to compact" instead of being billed for a
summarizer call that can't produce anything new.

Three entry points, because a cut is only safe at certain moments:

| State | Route | Consumed at |
|---|---|---|
| Idle | `POST /chats/{id}/compact` | immediately (branch is quiescent) |
| Working | steer (`mode: "steer"`) | the tool-round boundary, by the mid-turn gate |
| Queued | `after_final` | the drain, before it builds a generation |

The composer already dispatches `steer` while a turn is working, so the frontend only
intercepts the idle case; the other two are recognized backend-side. Both in-flight
routes exist for the same reason: `/compact` must never become a message. As a steer it
is consumed like any other (same dedupe, same deferred delete) but produces **no
`user_steer` block** — it arms `_maybe_compact_between_rounds(force=True)`, which runs
a few lines later at that same boundary. The steer-consume delete had to move out from
under `if steer_blocks:` for this: a command that yields no block would otherwise stay
queued, re-arm the gate every round, and finally drain as a turn whose prompt is the
literal `/compact`.

The drain interception is not optional even with the steer path — an unconsumed steer
falls through to the drain whenever the model finishes without another tool round.

The idle endpoint refuses while a generation is live (same liveness check the drain
uses). It writes the whole `content_blocks` list, so it would truncate whatever the
stream appended in between — the hazard `record_sent_envelope` sidesteps by declining
in-flight anchors. Only an odd composer state (files staged mid-turn) routes a
`/compact` there during a turn; the user is told to steer it instead.

Nothing is streaming on the idle and queued paths, so no `chat:completion` carries the
anchor. `compact_chat_now` emits `chat:message:compacted` to the user's tabs; the
acting tab also splices its own response so it doesn't wait on a round trip.

---

## 9. Deferred — known gaps, deliberately not solved yet

- **A single round can overshoot the threshold.** The check runs *before* a request
  using the last response's `total_tokens`, so tool results returned after that
  response aren't counted. At 79%, a parallel batch returning 25% worth of content
  overflows before the next check ever runs. Deliberately left alone for now —
  revisit only if it actually bites. When it does, the cheap fixes are a byte-based
  estimate from the lazy-body `size` descriptors (no tokenizer needed), and/or a
  reactive catch on the provider's context-length error, which is what goose and
  opencode do.
- **Tool-index growth.** ~20 tokens/entry × 300 calls ≈ 6k tokens — affordable, but
  unbounded across a long session. Needs a collapse policy eventually.
- **Threshold shape** — flat 80% vs. Crush's split (models over 200k get a fixed
  absolute buffer; smaller ones get a ratio). Relevant here because the same slug is
  272k on one connection and 1M on another. Measure before changing.
- **Compaction thrash.** Claude Code trips a circuit breaker after 3 consecutive
  refills within 3 turns of the previous compact.
- **Mid-turn injection position.** Codex places the summary *before* the last user
  message for mid-turn compaction specifically, "because the model is trained to see
  the compaction summary as the last item in history after mid-turn compaction." Our
  §1 placement is first-user-message; worth revisiting if mid-turn resumption
  misbehaves.
- **`read_tool_result` isn't bound until the turn AFTER the first compaction.**
  The tool list is resolved once per turn in `process_chat_payload`, so a
  conversation whose first compaction happens mid-turn has the index but not the
  escape hatch for the remainder of that turn. Deliberate: binding a tool
  mid-turn means rebuilding the tools schema, which invalidates the cached
  prefix — the exact cost compaction exists to avoid. The index is the
  load-bearing part (§7), so this is a degradation, not a break.
- **Size descriptors can differ by one entry between the two assembly shapes.**
  The mechanical collectors read from both the tree-walked internal messages and
  the already-converted API list. They agree everywhere except a tool result
  whose stored `content` is empty and which `_expand_assistant` substitutes for
  (subagent `final_text` recovery, or the "[No output…]" placeholder): the
  internal shape reports the persisted size, the API shape the substituted text's.
  Worth at most one cache invalidation at a turn boundary. Fixing it needs a
  second private carrier on every tool message.

---

## 10. Provenance

Design informed by reading source across ~12 implementations (2026-07-30): upstream
open-webui, LibreChat, Lobe Chat, AnythingLLM, Aider, continue.dev, goose, Codex CLI,
Cline, Roo Code, OpenHands, opencode, Crush, Hermes, Letta/MemGPT — plus Factory.ai's
head-to-head evaluation and the LOCOMO / LongMemEval literature. Details and the full
citation trail are in the `open_webui_compaction_prior_art` note.

Benchmark calibration: LOCOMO has a documented ~6.4% answer-key error rate and its
standard LLM judge accepted 62.81% of deliberately-wrong answers; the Zep/Mem0
comparison is a contested vendor dispute. Trust the qualitative convergence across
independent codebases, not the published percentages.
