# Subagent System Audit

Status: **audit and code hardening complete — production frontend deployed;
backend process restart pending**  
Started: 2026-07-23 UTC  
Completed: 2026-07-23 22:44 UTC  
Primary incident chat: `30bbf4ea-0766-469a-9a34-e487b6b36f16`
Latest durable-state recheck: 2026-07-23 22:45 UTC (read-only transaction)

## Purpose

This document is a living, evidence-based audit of the complete subagent
subsystem. It covers:

- the current database presentation of the incident parent chat and its hidden
  child chats;
- backend launch, continuation, execution, cancellation, rerun, adoption, and
  reconciliation paths;
- persistence in `chat`, `chat_message`, message `meta`, and any legacy JSON
  history;
- transient backend task/process state and socket event delivery;
- frontend request construction, live stores, reload hydration, rendering, and
  branch/rewind behavior;
- scenario traces, invariants, races, edge cases, and cleanup recommendations.

The audit distinguishes confirmed observations from hypotheses. It is updated
as each path is read and verified.

## Current incident summary

The original parent turn launched three subagents in one tool-call round. Their
original runs errored. The user opened each hidden full chat, rewound the latest
request, selected a model capable of the full context, and produced a completed
answer on each hidden chat's currently selected branch.

A new "Use latest answer from full chat" path was then added. Its intended
semantics are:

1. read the hidden chat's selected, completed assistant leaf;
2. replace the matching parent tool result and parent `subagent_runs` entry;
3. if the parent has already consumed the original result, create a sibling
   rewind branch instead of mutating history in place;
4. adopt the selected repaired results on that sibling;
5. resume the parent exactly once from the repaired tool results;
6. keep the old parent continuation navigable on its original branch.

The user reports that the parent did continue, but after a later reload the
three cards again display as errors. The database snapshot and reload
reconstruction path are being audited to determine whether:

- the adoption persisted only to transient/frontend state;
- the adopted sibling was created but is no longer the selected branch;
- the parent tool results were updated but `subagent_runs` was not, or vice
  versa;
- a later write overwrote the adopted state;
- reload hydration selected stale aliases or a stale parent message;
- the UI is rendering the original branch rather than the adopted branch.

### Database verdict for this incident

The selected database branch is repaired and internally coherent. The database
does **not** currently say that the three repaired subagents failed.

- Parent chat: `30bbf4ea-0766-469a-9a34-e487b6b36f16`
- Persisted selected leaf:
  `cf7707b6-4825-425c-95ea-a16d8ac31dc5`
- Selected leaf model: `google/gemini-3.6-flash`
- Selected leaf state: `done = true`, no non-null error, final parent content
  present
- All three selected-branch `subagent_runs` entries are `done`
- All three contain non-empty adopted `final_text`
- All three point to the repaired hidden-chat assistant leaf through
  `adopted_assistant_msg_id`
- All three matching tool results contain the same full repaired answer and are
  not error sentinels

Therefore a card that shows the original three errors after loading the
selected branch is displaying incorrectly reconstructed frontend state, stale
cached state, or a different sibling branch. It is not faithfully presenting
the currently selected persisted message.

The latest read-only recheck at 22:45 UTC reconfirmed:

- `history.currentId = cf7707b6-4825-425c-95ea-a16d8ac31dc5`;
- the selected row is sequence 8, `done=true`, `error=null`, with exactly three
  runs;
- each run is `status=done`, `adopted_from_child=true`, and has the expected
  repaired hidden assistant ID;
- answer sizes remain 17,099 / 19,316 / 14,816 characters, with stable
  content hashes;
- no database mutation was made (`BEGIN TRANSACTION READ ONLY` + `ROLLBACK`).

### Parent branch graph

The parent user message
`9e0168f3-d39a-4282-a092-a6d9bceba665` has five assistant children:

| Seq | Message | Persisted outcome | Subagent result state |
| ---: | --- | --- | --- |
| 4 | `4f1561b8-44f6-4efd-9ea1-a35208eb5500` | Original stopped answer | Three original errors plus one cancelled launch |
| 5 | `b6e754cb-11bf-443a-b211-57db96198190` | Abandoned partial rewind | Mixed/inconsistent: one fully adopted, one run says adopted while its tool result is still the old error, one still errored |
| 6 | `500fc7bf-e178-4f88-b203-a6a131de0cdf` | Repaired results, parent continuation failed | All three adopted; parent exceeded context window |
| 7 | `6a61b2ac-8c65-42d5-9bf1-655cba080519` | Successful repaired continuation | All three adopted; completed parent answer |
| 8 | `cf7707b6-4825-425c-95ea-a16d8ac31dc5` | Current successful continuation | All three adopted; completed parent answer |

The graph itself is valid: all five are durable siblings of the same user
message, and the chat's `history.currentId` selects sequence 8.

### Hidden-child graph snapshot

All four hidden chats are durable `chat` rows with
`subagent_of = 30bbf4ea-0766-469a-9a34-e487b6b36f16`. The three repaired
children select completed assistant siblings while preserving their original
failed siblings:

| Hidden chat | Original failed leaf | Selected repaired leaf | Selected content |
| --- | --- | --- | ---: |
| `723a32e4-9c17-4bae-85b3-d889ce406512` | `8342002b-a339-4597-969f-5a35e83685f8` | `362ab29e-c208-4ab4-904a-ef5c7d46a15d` | 17,099 chars |
| `a67bc7c1-02ac-4aee-8266-64258baeb2e4` | `61602b44-a363-4ceb-987e-207858fee2d8` | `05c5b43e-5cf7-42a7-bf3f-02b1286bf6d4` | 19,316 chars |
| `d9b1c530-27f8-447d-87b7-b53bf4f7f45d` | `f854a8ac-932e-400e-b3ad-3f25cd0d9b89` | `d1701760-fd6a-4bd5-8499-f9e17322bca8` | 14,816 chars |

The fourth hidden chat (`99e1983c-1cc4-44a8-9979-22d0b693148b`) belongs to a
later cancelled fourth launch and is not one of the three repaired inputs.

## Confirmed architecture so far

### Database

- Every visible parent chat and every hidden subagent chat is a row in `chat`.
- Hidden child rows are linked by the indexed `chat.subagent_of` column.
- `chat.messages_migrated = 1` means message bodies no longer live
  authoritatively inside `chat.chat.history.messages`.
- Migrated messages are stored one per row in `chat_message`.
- `chat_message` has dedicated columns for graph/identity fields
  (`message_id`, `parent_id`, `role`, `content`, `model`, `timestamp`,
  `sequence`) and a JSONB `meta` column for the rest of the message state.
- Parent subagent summaries/statuses are stored under message-level
  `subagent_runs` data. On migrated chats this is expected to be in
  `chat_message.meta`.
- The complete hidden transcript remains in the hidden subagent chat rather
  than being copied wholesale into the parent.
- Legacy, unmigrated chats still use `chat.chat.history.messages`, so many
  helpers support both persistence layouts.

### Backend runtime

- Independent reruns are registered as background tasks under item keys shaped
  like `subagent-rerun:{parent_chat_id}:{entry_key}`.
- Task state is surfaced through the chat task API and is used during reload to
  decide whether a persisted `running` entry is genuinely live or stranded.
- A reconciliation path marks stranded persisted runs terminal when no live
  parent/rerun task owns them.
- Live subagent state is also delivered through socket events; this is
  transient and must be reconstructible from persistence after reload.

### Frontend

- The parent message renders a serialized `subagent_launch` details token.
- The token is parsed by Markdown and lazily mounts the rich subagent card.
- `subagentLiveStates` is an in-memory frontend store keyed by several aliases
  (`entry_key`, `tool_call_id`, `subagent_id`, and `chat_id`).
- Chat loading reconstructs that store from persisted parent messages.
- The card can fetch the hidden child chat to display a richer transcript or
  its selected final answer when the parent stores only a slim summary.
- Chat open is local-first. A stitched tail snapshot is cached in IndexedDB and
  may paint before its network revalidation finishes.
- A fresh stitched tail contains full bodies only for the selected branch.
  Every off-branch sibling is initially represented by a lightweight stub.
- Switching to a sibling lazily hydrates that branch's bodies into the same
  in-memory `history.messages` object.

## Confirmed deployment/reload defect fixed during this audit

Before the deeper behavioral audit, the following concrete defect was found:

- dirty-tree frontend builds used only `git rev-parse HEAD` as the SvelteKit
  application/service-worker version;
- rebuilding modified code without committing kept the same application
  version;
- the shell service worker could therefore continue executing the previous
  subagent-card JavaScript even though new hashed assets existed on disk.

Corrections already made:

- dirty-tree builds now publish a unique application version;
- `start_modified.sh` now checks nested frontend source files and build config
  timestamps instead of comparing only the top-level `src` directory mtime;
- the rich card receives containing `chatId` and `messageId` explicitly;
- action context also has independent URL, DOM, persisted-run, and serialized
  tool-call fallbacks.

The public deployment was verified to serve the new application version and a
bundle in which the previous exact "missing parent chat context" error string
is absent.

## Verified working invariants

The traced implementation and regression suite now enforce:

1. **One authoritative run entry per parent tool call.** Aliases may point to
   it in memory, but persistence must not fork into contradictory entries.
2. **Parent summary and tool result agree.** A run marked `done` with repaired
   `final_text` must have a matching non-error tool result in the same parent
   message.
3. **Hidden selected leaf is explicit.** Adoption reads only the hidden chat's
   selected current branch and rejects running, errored, stopped, empty, or
   non-assistant leaves.
4. **Consumed history is immutable.** Once a parent has continued beyond a
   subagent result, repair happens on a sibling rewind branch.
5. **Branch selection is durable.** After rewind/adoption/resume, the new
   parent leaf and every relevant `childrenIds` relationship survive reload.
6. **Reload is a pure reconstruction.** Reload must never downgrade a persisted
   repaired result because of missing transient task/socket state.
7. **Terminal evidence wins over stale live state.** A completed persisted
   result cannot be shown as running or error solely because a socket event was
   missed.
8. **Cancellation is scoped.** Stopping a parent or one rerun cannot cancel
   unrelated subagents or another user's work.
9. **Resume happens once.** Multi-adoption or multi-rerun waits for all selected
   workers and triggers one parent continuation.
10. **Every failure is recoverable and legible.** The original branch remains
    navigable, and UI status derives from a single coherent persisted outcome.

## Audit work queue

- [x] Snapshot the current incident parent graph from `chat` and
  `chat_message`.
- [x] Snapshot each hidden child's graph and selected leaf.
- [x] Identify the exact adopted/rewound siblings and whether one is current.
- [x] Compare persisted `subagent_runs` with matching parent tool-call results.
- [x] Inventory the active and duplicate backend/frontend entry points.
- [x] Trace original launch and hidden-chat turn creation.
- [x] Trace hidden-chat continuation and rerun transcript replacement.
- [x] Trace parent cancellation teardown and stranded-run reconciliation.
- [x] Trace full-chat result validation and atomic multi-adoption.
- [x] Trace rewind/adopt/resume with multiple parallel siblings.
- [x] Trace normal success, provider error, timeout, and task/socket ownership.
- [x] Trace reload, local cache, live-store, and sibling hydration scenarios.
- [x] Trace rerun-in-place/restart-from-launch concurrency and multi-tab cases.
- [x] Move rewind-and-redo branch creation onto the guarded atomic DB primitive.
- [x] Close hidden-chat create/register unknown-commit cancellation windows.
- [x] Complete race and alias/branch consistency scenario matrix.
- [x] Classify findings by severity and confidence.
- [x] Propose a simplification/migration plan rather than further layering.
- [x] Run the production frontend build and final broad backend selection.

## Findings log

### F-001: frontend subagent aliases are not branch-scoped

- Severity: **high**
- Confidence: **confirmed code defect; incident causality still being
  reproduced**
- Affected invariants: 1, 6, 7, 10

`subagentLiveStates` is one flat `Record<string, SubagentRun>`. Chat hydration
iterates every full message body currently present in `history.messages` and
writes each run under four unscoped aliases:

- `tool_call_id`
- `subagent_id`
- `chat_id`
- `entry_key`

Rewind siblings intentionally copy the same tool calls and run identifiers.
They can also intentionally hold different outcomes: in this incident the same
tool-call IDs exist as original errors on sequence 4 and repaired results on
sequences 6–8. The hydration loop uses last-write-wins assignment, so whichever
hydrated sibling is enumerated last owns every shared alias. The rich card then
does a direct lookup by `tool_call_id`, without checking that the returned
run's `parent_message_id` matches the containing message.

This means a card on branch A can present branch B's status. The outcome can
change after lazy sibling hydration, navigation, cache restoration, or any
operation that changes message insertion order.

Structural recommendation: key state first by containing parent message ID,
then by per-run aliases. Every live event already carries
`parent_message_id`, and every rendered card now receives the containing
message ID, so lookups and writes can be branch-exact. Flat backward aliases
must not remain authoritative when more than one branch owns an identifier.

Patch made: the flat Svelte record remains for inexpensive updates, but its
authoritative keys are now composite `(parent_message_id, alias)` keys. Card
lookups, live start/update batching, reload hydration, adoption, and rewind-redo
optimistic state all resolve through the containing parent message. Raw aliases
remain a compatibility fallback and cannot be overwritten by another parent
message. A focused Vitest fixture uses the incident shape—identical aliases,
old sibling `error`, repaired sibling `done`—and verifies that both resolve to
their own outcome regardless of hydration order.

### F-002: the rewind/adopt/resume workflow is non-atomic and leaves branch debris

- Severity: **high**
- Confidence: **confirmed by code and incident data**
- Affected invariants: 2, 5, 9, 10

The frontend creates a rewind sibling before adoption succeeds, then sends
multiple adoption requests. A failed or interrupted attempt leaves that sibling
in the durable graph. Retrying creates another sibling rather than completing
or replacing the first attempt.

This incident contains five assistant siblings for one user turn:

- the original;
- an abandoned partial repair;
- an all-adopted branch whose parent continuation exceeded context;
- two completed repaired continuations.

The partial sibling is not merely incomplete. Its
`egress_alternatives` run says `done`, carries 14,816 characters of repaired
`final_text`, and points to the repaired assistant leaf, but the matching tool
result remains the original 312-character error sentinel. That violates the
run/tool-result agreement invariant in durable storage.

Structural recommendation: replace the client-orchestrated sequence with one
server-side operation that preflights all selected hidden leaves, creates one
rewind copy, adopts all selected results atomically, advances the branch
pointer, and returns one committed message. Parent generation should begin only
after that commit. Failed preflight should create no branch.

Patch made: the full-chat repair flow now uses one
`POST /api/v1/subagents/adopt/rewind` operation. It:

1. resolves entries only inside the explicitly selected source parent message;
2. validates that every hidden chat is owned by the user and belongs to this
   parent;
3. validates every selected hidden leaf before any write;
4. builds one complete sibling in memory, replacing each selected
   `subagent_runs` entry and its exact matching tool result together;
5. records the source message revision plus every child chat/leaf PostgreSQL
   `xmin`;
6. locks and revalidates those rows at commit time;
7. inserts the sibling, updates the shared user's `childrenIds`, and advances
   `history.currentId` in one database transaction with one commit;
8. returns the committed sibling for the frontend to install before it starts
   parent generation.

The operation is idempotent by operation/branch ID. A failed preflight creates
no branch. A conflict or SQL failure rolls back the whole transaction. The
committed pre-generation checkpoint is `done=true`; the owning tab flips its
local copy to running only when it actually launches the parent request, so a
browser crash after commit cannot leave a fake indefinitely-running parent.

Regression coverage verifies:

- two repaired runs and both tool results are prepared coherently without
  mutating the old sibling;
- one bad hidden leaf prevents the commit primitive from being called at all;
- a successful multi-adoption invokes the commit primitive exactly once with
  source/child row-version guards;
- the DB primitive contains one commit boundary after both the migrated and
  legacy graph/pointer writes.

### F-003: local-first revalidation can ignore changed message bodies

- Severity: **high**
- Confidence: **confirmed code defect; incident causality still being
  reproduced**
- Affected invariants: 5, 6, 7

The backend ETag includes the `chat` row's `xmin`, `updated_at`, and current
leaf. Row-only subagent updates call `touch_updated_at`, which rotates `xmin`.
That correctly forces a 200 response even when several writes occur during the
same integer second.

However, after painting an IndexedDB snapshot, the frontend decides whether to
apply the network response using only:

- `freshChat.updated_at === cachedChat.updated_at`; and
- equal `history.currentId`.

It does not compare the response ETag, chat-row `xmin`, or per-message `_rev`.
Consequently, a same-second `touch_updated_at` can rotate the ETag and deliver
changed message rows, but the frontend can discard that authoritative response
because the integer `updated_at` and branch pointer stayed equal.

Structural recommendation: treat a true 304 as the only proof that the cached
body is unchanged. For any 200 response, apply the stitched network result (or
compare the opaque ETag/per-row manifest revisions), even when `updated_at` and
`currentId` happen to match.

Patch made: `getChatByIdTail` now marks the substituted cached body only when
the server actually returned HTTP 304. The local-first continuation short
circuits only on that marker; every 200 body is applied. Regression coverage
verifies that a 200 with the same `updated_at` and `currentId` but a changed
message `_rev`/body remains distinguishable and wins over the cached copy.

### F-004: ordinary single-response branch navigation is not durably persisted

- Severity: **medium**
- Confidence: **confirmed code defect**
- Affected invariants: 5, 10

The ordinary `Messages.svelte` previous/next/index branch handlers set
`history.currentId` locally but do not call `updateChat`/`patchChat`.
`MultiResponseMessages.svelte` does persist after its equivalent navigation.
Thus ordinary branch selection can revert after reload or differ between tabs.

This does not explain the current incident snapshot—the database currently
selects the successful repaired branch—but it directly explains the general
class of “I selected/restored a branch, then reload showed another branch.”

Patch made: all three ordinary branch navigation handlers now issue the
targeted `set_history_current_id` patch only when the selection actually
changes. The remaining multi-response group-click path, whose persistence call
had been commented out, now persists as well.

### F-005: nullable `error` fields are easy to misclassify as failures

- Severity: **low**
- Confidence: **confirmed data-shape hazard**
- Affected invariants: 7, 10

Successful assistant rows and runs can retain an `error` key whose value is
`null`. Code and diagnostics must test the value, not key presence. In the
incident, every successful repaired hidden leaf and current parent leaf has an
`error` key in JSON metadata, but its value is null.

This is not the current rich-card status calculation's primary error, but it is
a recurring source of false “has error” interpretations and should be
normalized on terminal success.

### F-006: a dead, obsolete second subagent router remains in the package

- Severity: **medium**
- Confidence: **confirmed by repository-wide import search**
- Affected invariants: maintainability and future correctness

There are two modules that present themselves as the subagent HTTP router:

- active: `backend/open_webui/routers/subagents.py`;
- obsolete: `backend/open_webui/subagents.py`.

`main.py` imports the router package module and mounts only the first one.
Repository-wide search found no import of `open_webui.subagents`. The obsolete
module is an earlier implementation: it launches reruns with a bare
`asyncio.create_task`, lacks registered task ownership and stop support, lacks
the session-recipient validation and per-user feature checks, and has no
full-chat adoption endpoint or external-tool configuration.

This is not the runtime cause of the incident, but it is concrete evidence of
the layered implementation problem: a future maintainer can easily patch or
import the wrong router and silently reintroduce already-fixed safety and
lifecycle bugs.

Patch made: the unmounted `backend/open_webui/subagents.py` router was removed
after repository-wide import tracing and focused coverage of the active router.

### F-007: parent cancellation claimed to sweep stragglers but did not

- Severity: **high**
- Confidence: **confirmed and patched**
- Affected invariants: 6, 7, 8, 10

The clean-completion and terminal-error finalizers both call
`_sweep_subagent_runs`, which atomically converts every remaining `running`
entry to a terminal state and broadcasts the durable result to all of the
user's sessions. The cancellation teardown had an explicit comment promising
the same sweep/reconcile/broadcast sequence, and an existing scope regression
test even required the sweep helper to be defined early enough for cancellation.
But the teardown never invoked it: it called only reconciliation.

Normally each inline child observes the parent's `CancelledError` and writes
its own terminal state. That is not a sufficient root guarantee: a second
cancellation, DB error, cancellation/write race, malformed run, or child that
never reached its guarded block can leave a durable `running` entry. A reload
then truthfully renders a permanently spinning card.

Patch made: the shielded cancellation teardown now awaits the terminal sweep
with `fallback_status="cancelled"` before reconciliation, matching the clean
and error finalizers. The regression test now verifies both that the call
exists and that sweep precedes reconciliation.

### F-008: a second, obsolete frontend subagent card also remains

- Severity: **medium**
- Confidence: **confirmed by import tracing**
- Affected invariants: maintainability and consistent UI behavior

The active rich card is
`src/lib/components/chat/Messages/Markdown/SubagentBlock.svelte`. Both
`MarkdownTokens.svelte` and the generic `Collapsible.svelte` lazy-load that
component. A separate
`src/lib/components/chat/SubagentBlock.svelte` contains an older, much simpler
implementation, but repository-wide import search found no consumer.

The stale component lacks full-chat result adoption, moved-on rewind flows,
multi-subagent repair, robust parent-context resolution, v2.1 transcript
hydration, and several terminal-state corrections. Keeping two same-named
implementations makes it easy to apply fixes to the wrong file or accidentally
restore old behavior with a future import.

Patch made: `src/lib/components/chat/SubagentBlock.svelte` was removed after
tracing both lazy-load sites to the active Markdown component.

### F-009: adoption did not prove the selected answer belonged to the clicked turn

- Severity: **high**
- Confidence: **confirmed and patched**
- Affected invariants: 1, 3, 10

One hidden subagent chat can contain an original launch followed by multiple
`subagent_continue` user→assistant turns. The old adoption helper followed only
the hidden chat's global `history.currentId`. It checked that the selected leaf
was a clean completed assistant answer, but did not check which hidden user
message it answered.

Therefore, clicking “Use latest answer” on an earlier launch/continuation could
copy the answer to a different, later continuation into the clicked parent tool
result. The resulting run/result pair could look internally consistent while
being semantically attached to the wrong request.

Patch made: when run metadata is available, adoption now requires the selected
assistant leaf's `parentId` to equal the clicked run's launch-owned
`user_msg_id`. A manually repaired assistant sibling satisfies this invariant
because rewind preserves the user parent; another continuation does not. Older
rows missing `user_msg_id` fall back to the original
`assistant_msg_id`'s parent when available. Both the single in-place adoption
and the new atomic multi-adoption use this guard. Regression tests cover the
accepted sibling and rejected later-turn shapes.

### F-010: launch wrote hidden-chat identity into the wrong JSON layer

- Severity: **medium**
- Confidence: **confirmed and patched**
- Affected invariants: 1, 3, 8, 10

The hidden chat is initially imported with `Chat.meta.subagent_id = null`
because its row ID does not exist until insertion. The follow-up patch used
`update_chat_by_id`, which edits the conversation-body `chat` JSON, not the
row's authoritative `meta` column. It left `Chat.meta.subagent_id` null and
created a misleading `chat.chat["meta"]` shadow instead. Code using the real
row metadata for lineage/identity could therefore disagree with code reading
the accidental body shadow.

Patch made: launch now uses `update_chat_meta_by_id`, so `subagent_of`,
`subagent_id`, display name, and launch number live together in the
authoritative metadata column.

### F-011: hidden-chat continue/rerun rewrote the complete transcript

- Severity: **critical**
- Confidence: **confirmed and patched**
- Affected invariants: 1, 3, 5, 6, 8, 10

Hidden-chat continuation and rerun used a read/edit/`update_chat_by_id`
sequence. For migrated chats, `update_chat_by_id` implements a complete
`DELETE` and reinsert of every `chat_message` row when the caller includes a
messages map. Two overlapping operations could each prepare from a stale
snapshot and whichever committed last could silently delete the other's turn.
Reset-from-launch and retry made the blast radius the entire hidden transcript.

There was also a setup-window race. Rerun first changed the parent entry to
`running`, then appended the new blank hidden assistant leaf. A concurrent
request inspecting that interval saw no generating hidden leaf, misclassified
the fresh claim as stranded, terminalized it, and could defeat the intended
compare-and-set ownership.

Patch made:

1. `prepare_subagent_turn_atomic` locks the hidden `chat` row and verifies the
   exact expected `history.currentId`;
2. reset, one-turn revert, replacement user/assistant append, parent
   `childrenIds`, and pointer advance now share one transaction;
3. the migrated path touches only the involved `chat_message` rows; the legacy
   path mutates the live locked JSON blob;
4. launch retry, continuation retry, rerun-in-place, and restart-from-launch
   all use that primitive rather than separate reset/revert and append writes;
5. stale from-launch cards are marked only after the hidden transcript
   transaction commits;
6. a short, timestamp-based setup grace prevents reconciliation from
   terminalizing a newly claimed run before its blank hidden leaf exists;
7. graph/body writers now consistently lock the `chat` row before message
   rows, removing the inverse lock-order deadlock with atomic subagent writes.

Regression coverage proves the guarded single-commit primitive is used, the
old whole-chat helpers are absent, rerun no longer performs a separate
destructive setup, and a fresh setup claim cannot be reconciled as stranded.

### F-012: the UI counted one continuation several times

- Severity: **medium**
- Confidence: **confirmed and patched**
- Affected invariants: 1, 9, 10

The active card counted “other active continuations” with
`Object.values(subagentLiveStates)`. A single run is intentionally stored under
several aliases (`entry_key`, `tool_call_id`, `subagent_id`, and `chat_id`),
and rewind siblings can add more aliases for the same logical continuation.
The confirmation UI could therefore claim that several continuations would be
discarded when only one existed.

Patch made: the count is derived from unique continuation identity, scoped to
the containing parent message, rather than raw store values.

### F-013: reload hydration referenced two function-local variables and erased all cards

- Severity: **critical**
- Confidence: **confirmed; direct cause of the reported reload regression**
- Affected invariants: 6, 7, 10

The reload-time `subagentLiveStates` reconstruction lives inside `loadChat`.
It attempted to decide whether persisted `running` entries were truly live by
reading `_taskRes` and `activeStreamMessageIds`. Both identifiers were declared
with `let`/`const` inside the separate `applyStreamTaskState` function and were
therefore not in scope.

On any chat containing `subagent_runs`, hydration reached those expressions,
threw `ReferenceError`, entered its broad catch, logged a warning, and executed:

```text
subagentLiveStates.set({})
```

That precisely explains the incident shape: PostgreSQL selected the all-adopted
sequence-8 branch, but a page reload discarded the reconstructed rich-card
state and forced rendering to fall back to stale serialized/branch artifacts.
Fixing only `_taskRes` was insufficient because the later
`activeStreamMessageIds` reference independently caused the same failure.

Patch made:

- the consolidated chat-open response's non-enumerable `__active` bundle is
  now the only reload-time liveness input;
- pure helpers derive rerun entry keys and active stream message IDs from that
  bundle;
- no hydration code reaches into the post-paint reconciler's locals;
- a source-level Vitest regression isolates the hydration section and rejects
  `_taskRes?.`, `_activeStreamsRes?.`, and `new Set(activeStreamMessageIds)`;
- focused tests cover malformed/missing active bundles.

This is the strongest confirmed explanation for “the parent continued, but
after reload the three cards say errored.”

### F-014: chat-open and task-poll endpoints had divergent liveness contracts

- Severity: **high**
- Confidence: **confirmed and patched**
- Affected invariants: 6, 7, 8, 9

Detached reruns are deliberately not parent-generation tasks. Reload still
needs to observe them, but the consolidated chat-open response originally
reported only the ordinary chat task IDs while `/api/tasks/chat/{id}` derived a
different view. Depending on whether a load used the bundled response, route
loader, socket acknowledgment, or later poll, the same rerun could be treated
as live or stranded.

Patch made:

- `collect_chat_task_state` is the single backend task-registry projection;
- both endpoints now return the same `task_ids`, `rerun_task_ids`, and
  `subagent_rerun_entry_keys` fields;
- byte/string Redis response variants are normalized centrally;
- parent `generating` excludes detached reruns while the poller still tracks
  them;
- the unauthorized empty response has the same complete shape;
- chat ownership checks use a lightweight validator instead of hydrating the
  entire conversation.

### F-015: stranded-run repair could be hidden behind a valid stale ETag

- Severity: **high**
- Confidence: **confirmed and patched**
- Affected invariants: 6, 7, 10

Stranded reconciliation edits message rows. Before this audit, that repair did
not always rotate the root chat row. A subsequent conditional chat open could
therefore return 304 and substitute the cached pre-repair body, even though the
message row had been fixed.

Patch made:

- reconciliation enumerates only messages that actually carry subagent runs;
- successful healing calls `touch_updated_at`;
- consolidated chat open runs reconciliation before constructing the branch
  response and recomputes its validator after a repair;
- the current request therefore returns the repaired body, and later requests
  cannot 304 past it.

### F-016: detached rerun events and cleanup had no total generation order

- Severity: **critical**
- Confidence: **confirmed and patched**
- Affected invariants: 1, 7, 8, 9

Two reruns of one card can start within the same second. Timestamp comparison
cannot order them, and delayed socket start/terminal events or an old task's
cleanup could overwrite the newer attempt. Cleanup also risked interpreting
the previous `assistant_msg_id` as the newly completed answer.

Patch made:

- every attempt has an opaque `rerun_id` and persisted monotonic
  `rerun_attempt`;
- claim, per-attempt hidden IDs, socket events, optimistic UI state, terminal
  writes, finalizer, and batch freshness gate all carry that identity;
- compare-and-set terminalization requires the exact active `rerun_id`;
- recovery reads only `rerun_assistant_msg_id` owned by that attempt;
- a fast socket terminal result wins over a late HTTP optimistic flip;
- the parent terminal sweep excludes detached reruns because their own
  generation-guarded task owns them;
- the router re-awaits its shielded terminal-write + ETag cleanup through
  repeated Stops instead of leaving it detached.

### F-017: per-run updates were process-local rather than cross-worker atomic

- Severity: **critical**
- Confidence: **confirmed and patched**
- Affected invariants: 1, 2, 7, 8

The async per-message lock serialized writes only inside one Python worker.
Two workers could read the same `subagent_runs` map, update different children,
and overwrite one another. Full-map serialization also made a large fan-out
quadratic.

Patch made:

- every subagent run mutation locks `chat` then the exact `chat_message` row in
  PostgreSQL;
- migrated chats update only `meta.subagent_runs[entry_key]` with targeted
  JSONB writes and preserve sibling keys;
- launch slot/name assignment happens inside that locked mutation;
- the committed mutator result, not an in-memory holder populated before a
  failed commit, determines success;
- one retry covers uncertain transient writer outcomes, while rerun CAS is
  idempotent only for the same `rerun_id`;
- launch/continue/rerun generation does not start until its identity is
  durably registered.

### F-018: task registration and stop authorization had lifecycle gaps

- Severity: **high**
- Confidence: **confirmed and patched**
- Affected invariants: 7, 8, 9

A task could start and finish before its Redis registration completed, leaving
a ghost registry member. Conversely, stop/model-switch/service-tier endpoints
could not consistently prove that an opaque task ID belonged to the caller.

Patch made:

- task execution waits on a registration gate;
- the done callback owns registry cleanup after registration is visible;
- stop fails closed when no task→item ownership record exists;
- rerun item keys resolve back to the owning parent chat;
- socket model/service-tier changes use the same ownership proof;
- reruns have distinct item/task IDs, and the chat-wide Stop excludes them;
- the card's targeted Stop endpoint matches clicked entry aliases and the
  underlying subagent ID.

### F-019: swallowed search-index errors could poison the enclosing chat transaction

- Severity: **high**
- Confidence: **confirmed and patched**
- Affected invariants: 2, 5, 6

The chat/message search upserts were described as non-fatal and caught their
exceptions, but PostgreSQL marks the whole transaction aborted after a
statement error. Catching without a savepoint meant the later core chat commit
could still fail.

Patch made: chat-search and message-search work now runs under nested
savepoints. A search-index failure rolls back only the savepoint; the graph,
message, run, and pointer transaction remains committable.

### F-020: launch cancellation had create/register unknown-commit windows

- Severity: **critical**
- Confidence: **confirmed by async DB semantics and patched**
- Affected invariants: 1, 6, 7, 8, 10

Hidden chat creation and parent registration are two durable transitions. A
Stop can arrive immediately after either database commit but before the await
returns. The old cleanup could then:

- leak a just-committed hidden row that had no parent pointer; or
- delete the hidden row while the parent reservation had actually committed,
  leaving a durable run pointing to nothing.

Patch made:

- hidden creation, parent registration, terminalization, and exact-row deletion
  each run as one protected task;
- repeated cancellation re-awaits that same task until its result is known;
- cancellation before a parent reservation deletes the exact hidden row;
- cancellation after a committed reservation keeps the hidden row and marks
  the registered run terminal;
- a failed reservation deletes the row before returning an error;
- metadata/title publication happens only after the authoritative parent slot
  reservation, so parallel name disambiguation is reflected correctly.

### F-021: rewind-and-redo still used a non-atomic client branch even after adoption was fixed

- Severity: **high**
- Confidence: **confirmed and patched**
- Affected invariants: 2, 5, 9, 10

Atomic rewind/adoption fixed one repair path, but “rewind & redo subagent(s)”
still used `createRewindBranch` plus the generic multi-op chat PATCH. That API
performs message-row and chat-body work in separate async model calls. A
failure could leave an appended sibling without the selected pointer, while a
client-side failure could leave a branch visible only in RAM.

Patch made:

- `POST /api/v1/subagents/rerun/rewind` now validates the selected entries and
  pure-fanout cut on the backend;
- it uses the same guarded/idempotent `append_rewind_branch_atomic` primitive
  as adoption;
- sibling insert, shared-parent `childrenIds`, lazy body carry, root pointer,
  and validator bump share one transaction;
- adoption and rerun use a generic `rewind_operation` marker while recognizing
  the legacy adoption marker for idempotent retries;
- both frontend flows use one committed-branch installer for graph, cache,
  sidebar, offline-copy, and viewport behavior;
- reruns launch only after that durable checkpoint is installed, and parent
  resume remains fail-closed until every selected exact `rerun_id` is `done`.

### F-022: an idempotent rewind retry could reselect a superseded branch only in RAM

- Severity: **high**
- Confidence: **confirmed and patched**
- Affected invariants: 5, 6, 9, 10

`append_rewind_branch_atomic` correctly recognized a retry whose operation ID
had already committed. It returned that prior branch as a successful
idempotent result without rechecking the live `history.currentId`. If another
tab had since selected or committed a different branch, the retrying frontend
would install the old branch locally even though PostgreSQL still selected the
newer one. Reload then appeared to “undo” the repair.

Patch made: an existing-operation retry succeeds only while the durable
current pointer still selects its branch. Otherwise it raises the structured
`rewind_operation_superseded` conflict and the client cannot manufacture a
local/DB pointer split.

### F-023: adoption and rerun validation had a preflight-to-write TOCTOU

- Severity: **critical**
- Confidence: **confirmed and patched**
- Affected invariants: 2, 4, 5, 9

The consumed-result/current-branch/parent-done guards were evaluated before
the per-run database transaction. A second tab could move the branch or start
the parent after preflight but before the result write. The later subagent write
could then mark a run adopted/done while a parent checkpoint concurrently
re-saved the old tool-result blocks. This is a direct mechanism for the
sequence-5 incident artifact: run ledger says repaired, matching tool result
still says error.

Patch made:

- the targeted per-run writer accepts a precondition evaluated after locking
  the `chat` and exact `chat_message` rows;
- that precondition checks the live selected pointer, `done=true`, the exact
  run's continued existence, and the message-local unconsumed-result invariant;
- direct adoption, every rerun claim/identity/success transition, recovered
  success finalization, and completed-on-cancel replacement use the atomic
  guard appropriate to their mutation;
- error/cancel writes that merely terminalize a spinner remain possible after
  movement, but cannot install a new answer.

Regression tests force branch movement and parent generation between preflight
and row lock and verify the guarded write returns the corresponding structured
conflict without mutation.

### F-024: reload enrichment still had an all-or-nothing failure boundary

- Severity: **high**
- Confidence: **confirmed design defect and patched**
- Affected invariants: 6, 7, 10

F-013 removed the two immediate `ReferenceError`s, but all persisted run
seeding still lived inside one rich parser/task/stream `try` block whose catch
cleared the entire store. Any future malformed block or enrichment regression
would recreate the same “all cards vanished after reload” symptom.

Patch made:

- a pure baseline seeder reconstructs every durable `subagent_runs` entry
  before rich hydration begins;
- the containing message ID, not a copied run's potentially stale embedded
  parent ID, is authoritative for branch scope;
- malformed messages/runs are skipped individually;
- enrichment failures fall back to that durable baseline instead of `{}`;
- a delayed rerun event with a differing, unorderable generation ID now fails
  closed instead of resurrecting a terminal card as running.

The incident-shaped frontend fixture includes one failed sibling, one repaired
sibling with deliberately stale embedded attribution, and malformed entries;
each branch still resolves its own persisted outcome.

### F-025: the main parent could consume a detached rerun's temporary half-state

- Severity: **critical**
- Confidence: **confirmed and patched**
- Affected invariants: 2, 4, 7, 9

Detached reruns intentionally do not set the parent composer's `generating`
flag. In the initiating tab (before the next task poll), or from another tab, a
user could Continue/regenerate/send while the rerun had changed its run ledger
to `running` and removed the old canonical tool result but had not produced the
new one. The parent could therefore assemble a transcript containing a missing
subagent result. The rerun's later answer would be rejected as consumed, leaving
an avoidable failed redo and potentially a parent answer based on the
half-state.

Patch made with transactional replacement semantics:

1. frontend parent-generation entry points reject while the live store shows an
   active detached rerun;
2. the backend checks the distributed rerun task registry before assembly and
   returns `409/subagent_rerun_in_progress`;
3. conversation assembly independently rejects persisted active reruns before
   it writes a new user row;
4. rerun setup/attempt-identity writes no longer remove the prior parent tool
   result—the old coherent result remains until the guarded success write
   atomically replaces run and result together.

The fourth rule closes the irreducible cross-tab ordering race: if a parent
request checks just before a rerun claims, it consumes the coherent old
snapshot; setting the parent running then makes the new-answer commit fail
closed. It can never consume a missing half-result.

### F-026: Redis task members survived worker death forever

- Severity: **critical**
- Confidence: **confirmed and patched**
- Affected invariants: 6, 7, 8, 9, 10

The distributed registry stored task IDs in a global hash and per-item sets,
but neither structure had a per-task expiry. A SIGKILL, host restart, or worker
crash cannot execute the done callback, so its parent/rerun members remained
“live” indefinitely. Reload then protected a genuinely stranded run from
reconciliation and could keep cards/composer state wedged forever.

Patch made:

- every task now owns a separate expiring Redis lease;
- the worker heartbeat refreshes the lease and idempotently reasserts both
  registry indexes;
- every global/item/prefix read filters by leases and prunes expired hash/set
  debris;
- task ownership checks fail closed for expired leases;
- normal cleanup removes the lease and indexes;
- a task-state backend failure now returns 503 (“unknown”), not an empty list;
  the frontend preserves current state and retries rather than falsely
  terminalizing live work during a Redis outage.

Tests simulate an unclean worker death by deleting only the lease and verify
the next read repairs both stale indexes. A live worker test verifies its
heartbeat restores a missing lease and normal completion removes it.

### F-027: parent resume was process-local, so two workers could generate one message

- Severity: **critical**
- Confidence: **confirmed and patched**
- Affected invariants: 2, 5, 6, 9

The “resume once” guard originally lived only in one worker's
`ACTIVE_MESSAGE_GENERATIONS` dictionary. Two tabs routed to different workers
could submit the same assistant message ID and both pass it. The tasks then
shared one message row, stream run, and checkpoint target; whichever write
landed last won.

Patch made:

- the claim identity is the length-prefixed `(chat_id, message_id)` pair, so
  reused client message IDs in different chats cannot collide;
- Redis `SET NX EX` owns the registration window;
- `create_task` compare-and-sets that pending token to the real task ID before
  opening its worker start gate;
- a duplicate request returns the live owning task instead of resetting the
  stream or starting another generation;
- the heartbeat refreshes the claim, and cleanup deletes it only when the exact
  task still owns it.

This is the cross-worker barrier used when several adoption/rerun completions
all try to resume the same parent checkpoint.

### F-028: a Redis partition could outlive the task lease while its worker kept running

- Severity: **critical**
- Confidence: **confirmed by lease semantics and patched**
- Affected invariants: 6, 7, 8, 9

Adding expiring task/claim leases fixed dead-worker ghosts, but the first
heartbeat implementation logged Redis failures and let the provider request
continue forever. After 180 seconds the lease could expire. When Redis returned,
another worker could claim the same assistant message before the original
worker's next heartbeat noticed that ownership had changed.

Patch made: short Redis interruptions remain retryable, but a worker cancels its
own generation after 120 seconds without a successful registry refresh—60
seconds before the 180-second lease can expire. A replacement therefore cannot
acquire an expired claim while the old provider stream is still allowed to run.
A test forces repeated heartbeat failures and proves the owner task cancels and
cleans up.

### F-029: detached-rerun terminalization and cache invalidation were separate commits

- Severity: **high**
- Confidence: **confirmed and patched**
- Affected invariants: 2, 6, 7, 10

Normal rerun terminal writes changed `chat_message`, then the router separately
touched the root `chat.updated_at`. Cancellation could land between them, or a
reload could validate against the old root ETag after the message commit.
Additionally, the detached finalizer could recover a completed hidden answer
with `sync_placeholder=False`: the run became `done` but its parent tool result
stayed old.

A second edge occurred when the hidden answer completed just as Stop arrived
and the parent moved. The guarded success correctly refused to replace consumed
history, but the finalizer retried only that same guarded success, leaving the
claim `running` forever.

Patch made:

- every rerun success/error/cancel terminal writes the run/result and rotates
  the root validator in the same transaction;
- recovered success synchronizes the matching parent tool result;
- if the parent changed, the exact claim falls back to a terminal error/cancel
  without replacing the coherent old result;
- the router's separate touch was removed.

### F-030: direct one-at-a-time adoption could resume before sibling repairs committed

- Severity: **critical**
- Confidence: **confirmed by request ordering and patched in the active UI**
- Affected invariants: 2, 4, 5, 9, 10

The old card called `/adopt` for one result and immediately resumed the parent.
If three failed fan-out siblings had been repaired, the first response could
assemble the parent while the second/third adoption was still in flight—or
before the user clicked them at all. Cross-worker generation deduplication
ensures one resume, but it cannot make that first assembled transcript include
future writes.

Patch made: the card no longer imports or calls the direct-adopt client. “Use
latest answer from full chat” opens the common branch chooser, and Chat owns one
`/adopt/rewind` request containing the complete selection. Hidden leaves are
validated first; one transaction creates/selects the sibling and replaces every
selected run/result pair; only that committed response can launch the parent.
The prior branch remains navigable. The legacy backend endpoint remains for
old deployed clients, but the maintained UI has one repair workflow.

### F-031: repair could not branch from an older turn on the selected transcript

- Severity: **high**
- Confidence: **confirmed and patched**
- Affected invariants: 4, 5, 10

The rewind primitive required `source_message_id == history.currentId`. A
subagent card remains visible when later user/assistant turns exist, so repairing
that older selected-branch turn incorrectly failed as “parent moved.” The
desired behavior is exactly a rewind: keep later work on the old branch and
create a sibling at the repaired turn.

Patch made:

- preflight proves the source is an ancestor on the currently selected branch;
- the transaction receives and guards the separately observed current leaf;
- it locks the chat, rechecks that leaf and the source revision, then appends
  the sibling at the source;
- an off-branch source still fails closed.

### F-032: different run keys could concurrently mutate one hidden transcript

- Severity: **critical**
- Confidence: **confirmed and patched**
- Affected invariants: 1, 3, 7, 8, 10

The per-entry CAS prevented two reruns of the same key, but a launch entry and a
continuation entry (or two continuation keys) for the same hidden chat could
pass read-only checks together. Each claimed a different parent key and then
reset/reverted the same hidden transcript concurrently. The 30-second “setup
grace” reduced the window but did not serialize the two claims.

Patch made: the targeted run writer now supports a selected-branch
cross-message precondition under the locked root chat transaction. Continuation
and rerun claims scan every selected-branch run and permit only one active turn
for the hidden `subagent_id`; inactive sibling branches do not interfere.
Continuation also uses the same-key CAS for the no-tool-call-ID fallback.

### F-033: stranded recovery could acknowledge a write that never committed

- Severity: **high**
- Confidence: **confirmed and patched**
- Affected invariants: 2, 6, 7, 10

`_terminalize_stranded_entry` set an in-memory `holder["changed"]` inside its
mutator and returned success even when the database writer returned `None`.
Its recovered `final_text` also updated only `subagent_runs`, leaving the old
parent tool result in place. The outer reconciler then touched the ETag and
broadcast a terminal that was not necessarily durable.

Patch made: recovery uses the targeted run writer with `touch_chat=True`,
updates the matching tool result when an answer exists, and reports healed only
when the writer returns a committed patch. The parent terminal sweep received
the same no-false-acknowledgement check.

### F-034: deleting a chat sent Stop but did not wait for cancellation

- Severity: **medium**
- Confidence: **confirmed and patched with a bounded acknowledgement**
- Affected invariants: 7, 8, 10

For Redis-backed tasks, `stop_task` publishes a command and returns immediately.
Chat deletion then removed the row while a remote parent/rerun worker could
still be executing shielded terminal cleanup.

Patch made: deletion snapshots the parent and rerun task IDs, publishes each
Stop once, and waits up to 30 seconds for their registry leases to disappear.
Registry cleanup is the cross-worker acknowledgement. A timeout remains
bounded and is logged; subsequent writes cannot recreate a deleted chat row.
The same pass fixed delete-by-missing-ID dereferences of `chat.meta`.

## End-to-end scenario matrix

“Covered” means the authoritative transition and its failure outcome were
traced in code and have focused regression coverage; it does not mean every
provider/network combination has a browser E2E test.

| Scenario | Authoritative path and expected outcome | Status |
| --- | --- | --- |
| Parallel fan-out, all succeed | Parent task owns inline children; per-run writes serialize under the chat/message lock; final sweep and fan-out terminal delivery converge persistence/UI. | Covered |
| One sibling provider error | That entry becomes `error` with a matching error tool result; siblings remain independent and the parent can consume the complete fan-out round. | Covered |
| Deterministic context overflow | Classified non-retryable; avoids a second doomed research run; produces a recoverable error card/full hidden chat. | Covered |
| Configured wall-clock timeout | Inner task is cancelled and enters the ordinary retry/terminal path; parent Stop remains distinguishable from an isolated inner timeout. | Covered |
| Parent Stop once or repeatedly | Listener cancels only a live, not-already-cancelling owner; shielded terminal writes/sweep survive repeated Stop events. | Covered |
| Parent finalizes while detached rerun exists | Parent sweep deliberately does not own detached reruns; assembly rejects their temporary active state; old tool result remains coherent. | Covered |
| Reload after missed socket terminal | Persisted baseline seeds first; task/stream enrichment is optional; terminal persistence wins over stale live aliases. | Covered |
| Reload during live rerun | Registered task forces a 200 body, identifies the exact run key, and keeps only that owning card live. | Covered |
| Redis temporarily unavailable | Conditional open fails toward 200; task API returns unknown/503 rather than fake idle; frontend preserves state and retries. | Covered |
| Redis unavailable near lease expiry | Worker self-cancels before expiry, preventing an old provider stream from overlapping a replacement owner. | Covered |
| Worker SIGKILL/server crash | Lease expires; next task/open read prunes both indexes and reconciles the persisted running claim. Bounded stale-display window is at most the lease TTL. | Covered, bounded delay |
| Same completion POST retried after lost HTTP response | `(chat,message)` claim returns the existing task without stream reset or second generation. | Covered |
| Same assistant ID used in different chats | Length-prefixed claim identity keeps ownership separate. | Covered |
| Two tabs redo the same run | Per-run CAS permits one claim; loser gets structured `subagent_already_running`. | Covered |
| Two keys target the same hidden chat | Selected-branch cross-message guard permits one active hidden-transcript mutator. | Covered |
| Several different hidden chats redo in parallel | Each hidden chat has its own exclusivity key; parent per-run JSON writes remain lossless; barrier resumes only if every selected rerun generation is fresh/done. | Covered |
| Parent Continue/send during detached rerun | Frontend blocks immediately; task preflight and persisted assembly both fail closed; no half-result can be consumed. | Covered |
| Manually repaired hidden leaf, single result | Card routes through atomic rewind/adopt; old branch remains; committed repaired branch resumes once. | Covered |
| Several manually repaired fan-out leaves | One chooser sends the complete selection; one transaction validates every hidden leaf and installs all run/result pairs or none. | Covered |
| Hidden selection changes during adoption | Hidden chat/current leaf and message revisions are rechecked under deterministic row locks; operation conflicts without a partial branch. | Covered |
| Repair an older visible turn | Source must be an ancestor of the guarded selected leaf; later work stays on the old branch and the repaired sibling becomes current. | Covered |
| Attempt repair from inactive sibling branch | Selected-branch ancestry check rejects it. | Covered |
| Network loss after rewind commit | Stable operation/branch IDs make the same HTTP retry idempotent while that branch remains selected; a later superseding selection returns conflict. | Covered |
| Branch switch then reload | Every selection handler persists `history.currentId`; local cache invalidates and lazy sibling hydration is branch-scoped. | Covered |
| Fresh body with same-second timestamp | A successful 200 always replaces the cache body; ETag/xmin and explicit validator rotation do not depend on timestamp alone. | Covered |
| Parent deletion during rerun | Delete stops parent plus rerun task IDs and waits for registry cleanup before row deletion, bounded to 30 seconds. | Covered with bounded timeout |
| Unauthorized task stop/rerun/adoption | Parent ownership, task item ownership, feature permission, child-parent relation, and socket-session ownership are independently checked. | Covered |
| Legacy unmigrated chat | Atomic helpers fall back under the locked root JSON row; maintained behavior remains, but this path retains higher complexity and less granular performance. | Covered compatibility path |

### Residual operational limits

- A hard worker death is detected by lease expiry rather than instantly, so a
  `running` card can remain truthful-but-stale for up to 180 seconds before the
  next read reconciles it.
- Multi-worker exactly-once ownership requires Redis. With Redis disabled, the
  local registry is intentionally a single-worker contract.
- Delete has a bounded 30-second wait. A task that ignores cancellation can
  waste provider work after that timeout, but guarded writes cannot resurrect
  the deleted parent row.
- The legacy direct `/adopt` endpoint remains mounted for older built clients.
  The maintained frontend no longer uses it; removal should happen after the
  old asset/service-worker compatibility window is closed.
- Socket delivery remains advisory. Correctness now comes from PostgreSQL plus
  task leases; a missed event may delay presentation until poll/reload but
  cannot be the sole copy of a terminal result.

## Severity classification

The audit currently records **34 findings**:

- **12 critical**: possible data/result divergence, concurrent transcript
  mutation, indefinite liveness wedge, or duplicate parent generation;
- **15 high**: durable branch/cache inconsistency or a broadly reproducible
  reload/recovery failure;
- **6 medium**: cancellation/deletion lifecycle or maintainability paths that
  materially increase regression risk;
- **1 low**: nullable-error representation hazard.

Every critical finding has a concrete code change and focused regression
coverage. The low-severity nullable-error item remains a data-shape convention:
readers consistently test the value, while old rows are not rewritten merely
to remove JSON `null`.

## Simplification and migration plan

The fixes above make the current representation safe, but the user's
maintainability concern is valid: `subagent_runs` is still a workflow ledger
inside message JSON, while task ownership lives in local memory/Redis and the
UI maintains aliases. The clean endpoint is not another helper layered onto
that shape. It is a staged replacement:

1. **Typed run table.** Introduce `subagent_run` with a canonical run UUID,
   `(parent_chat_id, parent_message_id, tool_call_id)` uniqueness, hidden chat
   ID, status enum, attempt/generation, selected hidden leaf, timestamps,
   error, and version. Keep large final text/tool output in the existing
   message/result storage, referenced by the run.
2. **Typed operation table.** Represent fan-out, rerun, adoption, rewind, and
   parent-resume as `subagent_operation` rows with explicit states and one
   idempotency key. A transition service—not routers/components—becomes the only
   writer.
3. **Transactional outbox + fenced workers.** Commit the state transition and
   an outbox event together. Workers claim with a monotonically increasing
   fencing token; PostgreSQL rejects a terminal write from an older owner even
   if Redis is partitioned.
4. **One command API.** Replace direct `/adopt`, `/adopt/rewind`, and client
   orchestration with commands on the operation resource. The server decides
   in-place versus branch and schedules the one allowed resume.
5. **Projection-only frontend.** Render a canonical run ID plus operation
   revision. Remove four-way aliases and client-owned polling barriers; socket
   events only invalidate/refetch a durable projection.
6. **Backfill and dual-read window.** Backfill typed rows from message JSON,
   compare projections in telemetry/tests, then switch writes to tables and
   eventually remove legacy JSON run ledgers and the direct-adopt endpoint.

This keeps the current repaired chats readable throughout migration and avoids
a risky all-at-once rewrite. The first implementation slice should be the run
table plus dual-read verifier; it yields the largest simplification without
changing user-visible behavior.

## Verification log

- Final broad backend selection across cancellation, open/reload, queue,
  message, bounds, task-registry, reliability, rerun, and stranded-recovery
  paths: **262 passed**, with four unrelated deprecation warnings.
- Complete discovered Vitest suite: **19 files / 247 tests passed**. This
  includes the subagent alias/generation/cache tests; the expected relative-URL
  stderr from the mocked sidebar retry test did not fail the suite.
- Production SvelteKit build: passed in 53.04 seconds; the static adapter wrote
  `build/`, postbuild generated 580 Brotli files, and the service-worker
  precache manifest contains 133 entries.
- The live site serves version
  `f0362475e2774733113393246ba5af59f7aae918-dirty-mry3hqqr`. Its generated
  subagent bundle contains “Use latest answer from full chat” and does not
  contain “Cannot use subagent answer: missing parent chat context.”
- Prettier on the touched frontend/API/state files: passed.
- Targeted `git diff --check`: passed.
- Python compilation of the changed backend modules: passed.
- PostgreSQL integration check against connection-local copies of an affected
  hidden chat: append, replace-latest, stale-leaf rollback, and reset all
  passed; explicit before/after assertions confirmed the durable production
  row and message count were unchanged.
- The final read-only production snapshot reconfirmed the selected repaired
  parent leaf and exact equality between all three adopted answers and their
  matching parent tool results.

## Deployment activation note

The frontend build is active without a process reload because the running
server reads the static `build/` files. The backend is a standalone Uvicorn
process without auto-reload and predates these Python changes. Restarting that
live process is therefore still required before the server-side task leases,
atomic adoption/finalization, generation claims, and recovery guards take
effect. The audit did not restart it implicitly because that interrupts the
only observed application worker and can terminate active requests.

Future findings continue to use:

- severity;
- affected invariant;
- exact code/data evidence;
- reproduction scenario;
- recommended structural fix;
- whether a patch was made during the audit.
