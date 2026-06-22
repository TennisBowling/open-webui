<script lang="ts">
	import { getContext, onDestroy, onMount, tick } from 'svelte';
	import { slide } from 'svelte/transition';
	import { quintOut } from 'svelte/easing';
	import { toast } from 'svelte-sonner';

	import { chatId, socket, subagentLiveStates } from '$lib/stores';
	import { rerunSubagent, type SubagentRerunScope } from '$lib/apis/subagents';
	import { getChatById } from '$lib/apis/chats';
	import { blocksToDisplayMarkdown, formatDuration } from '$lib/utils';
	import Markdown from '../Markdown.svelte';
	import Spinner from '$lib/components/common/Spinner.svelte';

	const i18n: any = getContext('i18n');

	// `attributes` is what `Collapsible.svelte` parses out of the
	// `<details type="subagent_launch" ...>` tag that
	// `serialize_content_blocks` (middleware.py) emits for every
	// subagent_launch / subagent_continue tool call in the parent message.
	// Keys we care about:
	//   tool_call_id — primary key into subagentLiveStates (one entry per
	//                  parent-model tool call; preferred over `id`)
	//   id           — backstop: the subagent's chat_id (set by middleware as
	//                  soon as the side-channel has it, may be empty before)
	//   name         — function name (subagent_launch or subagent_continue)
	//   arguments    — JSON-encoded args (used for the "Args" disclosure)
	//   done         — "true" once the parent tool result lands
	export let attributes: Record<string, any> = {};

	const decodeHtml = (value: unknown) => {
		if (typeof value !== 'string') return value ?? '';
		if (typeof document === 'undefined') return value;
		const textarea = document.createElement('textarea');
		textarea.innerHTML = value;
		return textarea.value;
	};

	const parseArgs = (raw: unknown): Record<string, any> => {
		try {
			let value: any = decodeHtml(raw);
			if (typeof value === 'string') value = JSON.parse(value);
			// Backend/client projections double-encode OpenAI function.arguments.
			if (typeof value === 'string') value = JSON.parse(value);
			return value && typeof value === 'object' ? value : {};
		} catch {
			return {};
		}
	};

	$: attrToolCallId = (attributes?.tool_call_id || '') as string;
	$: attrSubagentId = (attributes?.id || '') as string;
	$: attrArgs = parseArgs(attributes?.arguments);
	// The persisted placeholder stamps done="true" once the tool call returned.
	// Used as a terminal-state fallback when no live/seeded run exists for this
	// block (e.g. a malformed subagent call that errored before creating a run).
	$: attrDone = String(attributes?.done) === 'true';
	$: directRunEntry =
		attrToolCallId && $subagentLiveStates[attrToolCallId]
			? ([attrToolCallId, $subagentLiveStates[attrToolCallId]] as [string, any])
			: attrSubagentId && $subagentLiveStates[attrSubagentId]
				? ([attrSubagentId, $subagentLiveStates[attrSubagentId]] as [string, any])
				: null;
	// Hot path is collapsed. Avoid scanning every subagent store entry unless the
	// user expands a block whose direct keys somehow missed after reload.
	$: scannedRunEntry =
		!directRunEntry && open
			? (Object.entries($subagentLiveStates).find(([, r]: any) => {
					if (!r) return false;
					return (
						(attrToolCallId && r.tool_call_id === attrToolCallId) ||
						(attrSubagentId && (r.subagent_id === attrSubagentId || r.chat_id === attrSubagentId))
					);
				}) as [string, any] | undefined)
			: undefined;
	$: runEntry = directRunEntry ?? scannedRunEntry ?? null;
	$: stateKey = runEntry?.[0] || attrToolCallId || attrSubagentId || '';
	$: run = runEntry?.[1];

	// A stale entry was orphaned by a "Redo subagent" (from_launch) restart: its
	// own recorded status is frozen at whatever it was before the wipe, so it
	// can't be trusted. The relaunch's outcome lives on the SAME subagent's
	// non-continuation (launch) entry, which the load path re-seeds with the
	// real result. Find that sibling so this card can mirror it instead of
	// inventing a status. If no launch sibling exists, fall back to the entry's
	// own state.
	$: staleLaunchSibling = (
		stale && run?.subagent_id
			? ((Object.values($subagentLiveStates) as any[])
					.filter(
						(r: any) =>
							r &&
							!r.continuation &&
							(r.subagent_id === run.subagent_id || r.chat_id === run.subagent_id)
					)
					// Multiple relaunches of the same subagent can leave several
					// non-continuation entries; mirror the MOST RECENT one (highest
					// started_at) so the stale card reflects its true replacement
					// rather than whichever happened to be enumerated first.
					.sort((a: any, b: any) => (b?.started_at ?? 0) - (a?.started_at ?? 0))[0] as any)
			: null
	);
	// The run we actually present for a stale card: the launch sibling when we
	// found one, else the (frozen) stale entry itself.
	$: effectiveRun = stale ? (staleLaunchSibling ?? run) : run;

	// Header status the user sees. Prefer the store entry, but let terminal
	// evidence from the persisted placeholder/final_text win over a stale
	// `running` row left behind by a reload while the old socket session owned
	// the stream.
	//
	// EXCEPTION: when the run is `live` (this session is actively driving it —
	// e.g. just after a redo), the store's status is authoritative even when it
	// is `running`. Otherwise the parent message's persisted
	// `<details done="true">` placeholder (never rewritten on redo) would clamp
	// a freshly-restarted run straight back to `done`.
	$: status = (
		stale
			? // Mirror the relaunch's authoritative outcome (its launch sibling),
				// NOT the frozen status on this orphaned entry. Only show a green
				// "done" when that run truly finished with a final answer; if the
				// relaunch was cancelled / errored / is still running, surface that
				// honestly instead of faking success.
				effectiveRun?.status === 'done' || effectiveRun?.final_text
				? 'done'
				: effectiveRun?.status === 'cancelled'
					? 'cancelled'
					: effectiveRun?.status === 'running'
						? 'running'
						: 'error'
			: run?.live
				? (run?.status ?? 'running')
				: run?.status && run.status !== 'running'
					? run.status
					: run?.final_text
						? 'done'
						: // No usable run state. If the persisted placeholder says the
							// call already returned (done="true") and we have neither a
							// run nor any final text, this is a terminal call with no
							// subagent output (e.g. a malformed launch that errored) —
							// surface 'error' instead of spinning on 'running' forever.
							!run && attrDone
							? 'error'
							: (run?.status ?? 'running')
	) as 'running' | 'done' | 'error' | 'cancelled';

	$: displayName = run?.name || attrArgs?.name || '';
	$: displayNum = run?.num ?? null;
	$: subagentChatId = run?.chat_id || run?.subagent_id || attrSubagentId || '';
	$: continuation = run?.continuation === true || attributes?.name === 'subagent_continue';
	$: stale = run?.stale === true;

	// --- Elapsed-time timer ---------------------------------------------------
	// Timing lives on the run object as `started_at` / `ended_at` (UNIX seconds,
	// stamped by the backend at every lifecycle point and mirrored client-side;
	// both survive reload via the persisted `subagent_runs` map). A stale card
	// (orphaned by a `from_launch` redo) mirrors its launch sibling's timing,
	// exactly like the status / final-text logic above.
	$: timingRun = stale ? (effectiveRun ?? run) : run;
	$: startedAt = typeof timingRun?.started_at === 'number' ? timingRun.started_at : null;
	$: endedAt = typeof timingRun?.ended_at === 'number' ? timingRun.ended_at : null;

	// A single self-managing 1s ticker, held ONLY while this block is actively
	// running. `nowTs` is deliberately isolated so a tick recomputes just the
	// timer string — never the heavy contentBlocks / hydration reactives.
	let nowTs = Math.floor(Date.now() / 1000);
	let timerInterval: ReturnType<typeof setInterval> | null = null;
	$: if (typeof window !== 'undefined') {
		const shouldTick = status === 'running' && startedAt != null;
		if (shouldTick && !timerInterval) {
			nowTs = Math.floor(Date.now() / 1000);
			timerInterval = setInterval(() => {
				nowTs = Math.floor(Date.now() / 1000);
			}, 1000);
		} else if (!shouldTick && timerInterval) {
			clearInterval(timerInterval);
			timerInterval = null;
		}
	}

	// Running -> ticks off `nowTs`; terminal -> frozen at `ended_at`. A terminal
	// run with no `ended_at` (only possible via a hard crash mid-flight) yields
	// no duration rather than a runaway clock. Clamp ≥ 0 for sub-second skew
	// between the server's time.time() and the client's Date.now().
	$: elapsedSeconds =
		startedAt == null
			? null
			: status === 'running'
				? Math.max(0, nowTs - startedAt)
				: endedAt != null
					? Math.max(0, endedAt - startedAt)
					: null;
	$: timerText = elapsedSeconds == null ? '' : formatDuration(elapsedSeconds);

	// Header line. While running we keep the "Researching…" badge and show the
	// ticking timer beside it (template). When terminal, the duration phrase
	// replaces the badge text; the colored icon still conveys state. When timing
	// is unavailable (legacy runs persisted before timing was stamped) we still
	// say "Researched" — NEVER a bare "Done", which reads as a different state
	// from the "Researched for 7m" cards and is exactly the inconsistency the
	// user reported. New runs always carry both timestamps (the backend stamps
	// them atomically), so they always get the duration.
	$: headerStatusText =
		status === 'running'
			? $i18n.t('Researching…')
			: status === 'done'
				? timerText
					? $i18n.t('Researched for {{duration}}', { duration: timerText })
					: $i18n.t('Researched')
				: status === 'cancelled'
					? timerText
						? $i18n.t('Stopped after {{duration}}', { duration: timerText })
						: $i18n.t('Stopped')
					: timerText
						? $i18n.t('Failed after {{duration}}', { duration: timerText })
						: $i18n.t('Error');

	// Subagent body renders the structured `content_blocks` directly via a
	// keyed `{#each}` (see template below). We deliberately do NOT compute a
	// flat markdown projection here: it would re-walk + re-stringify the
	// whole array on every store update, and the parent `<Markdown>` would
	// re-tokenise + {@html}-wipe the DOM on every chunk. That's the O(N²)
	// pattern that made the UI feel like 1-2 TPS while OpenRouter was happily
	// streaming at 20+. Structural rendering with keyed each lets Svelte
	// reuse component instances for unchanged blocks and apply O(diff) text
	// node updates for the one growing block, so per-event cost is O(1) and
	// the UI tracks upstream as fast as the model can produce.
	//
	// During streaming we render text blocks as plain pre-wrapped text — no
	// `marked.parse` per chunk. Once the run is complete we swap each text
	// block to a full `<Markdown>` render so the final answer gets headings,
	// lists, code blocks, etc. (one parse, total).
	// For a stale (orphaned-by-restart) entry, its own persisted content_blocks
	// are the OLD turn that the relaunch replaced — ignore them and render the
	// hidden chat's current content (fetched into fallbackBlocks) instead, so
	// the card shows the new run's thinking / tool calls / answer.
	// Structured blocks to render (work + answer). Priority: the live/seeded
	// run's own content_blocks, else the on-demand fetched subagent chat blocks
	// (fallbackBlocks). IMPORTANT: the parent's persisted `subagent_runs` entry
	// stores ONLY `final_text` (not content_blocks — that would bloat every parent
	// message with every subagent's full transcript), so after reload `run` has
	// final_text but NO content_blocks. We must still pull in `fallbackBlocks` for
	// the WORK transcript — previously a present `final_text` short-circuited to
	// `[]`, which is why opened cards showed the answer but none of the searches /
	// fetches / bash / thinking.
	$: contentBlocks = (
		open
			? stale
				? (effectiveRun?.content_blocks ?? fallbackBlocks ?? [])
				: ((run?.content_blocks && run.content_blocks.length
						? run.content_blocks
						: fallbackBlocks) ?? [])
			: []
	) as any[];
	$: hasContent = contentBlocks.length > 0;
	$: doneRendering = status !== 'running';
	// Final answer text to show when there are no structured blocks to render.
	// Stale cards mirror their launch sibling's answer.
	$: displayFinalText = (stale ? effectiveRun?.final_text : run?.final_text) || '';

	// Index of the subagent's FINAL answer text block (the last non-empty text
	// block, only once the run is terminal). The final answer is the single most
	// important thing a user opens the card to see, but it is the LAST block in a
	// possibly-huge transcript — the block window below renders from the START, so
	// without special handling the answer is hidden behind slow progressive
	// hydration (or entirely, if the main thread never goes idle). We therefore
	// pull it OUT of the windowed transcript and render it as a dedicated,
	// always-visible section. While running, the trailing text is still streaming
	// and stays inline in the transcript (idx = -1).
	$: finalTextIdx = doneRendering
		? (() => {
				for (let i = contentBlocks.length - 1; i >= 0; i--) {
					const b = contentBlocks[i];
					if (b?.type === 'text' && `${b.content ?? ''}`.trim()) return i;
					// Stop scanning once we hit the work (tool calls): a final answer
					// only ever trails the last tool_calls block. Reasoning is skipped.
					if (b?.type === 'tool_calls') return -1;
				}
				return -1;
			})()
		: -1;
	// The authoritative final answer. Prefer the actual trailing text block; if the
	// transcript somehow lacks it (a live-delivery gap), fall back to run.final_text
	// (what the parent model received). Only fall back to `fallbackContent` when
	// there is NO transcript at all — `fallbackContent` is the WHOLE subagent chat
	// serialized to `<details type="tool_calls">…` markdown (blocksToDisplayMarkdown),
	// which <Markdown> re-expands into the rich web_search/fetch cards. Using it as
	// the "final answer" alongside a present transcript renders every tool call
	// twice: once barebones in the transcript, once richly here. Guard on
	// !hasTranscript so the rich serialization is only ever the no-transcript
	// fallback path (which also has its own dedicated branch in the template).
	$: finalAnswerText = doneRendering
		? (finalTextIdx >= 0
				? `${contentBlocks[finalTextIdx].content ?? ''}`
				: displayFinalText || (hasTranscript ? '' : fallbackContent) || '')
		: '';
	// Transcript = the work (reasoning + tool calls + any interstitial text),
	// excluding the trailing final-answer block which is rendered separately.
	$: transcriptBlocks =
		doneRendering && finalTextIdx >= 0
			? contentBlocks.filter((_, i) => i !== finalTextIdx)
			: contentBlocks;
	$: hasTranscript = transcriptBlocks.length > 0;

	// Default collapsed in every state (running / done / error / cancelled).
	// The user clicks the header (or the redo button) to expand and watch
	// the subagent's current work. The status badge + spinner in the header
	// still convey progress at a glance without forcing the body open.
	let open = false;

	// Expansion should feel instant even for finished subagents with a long
	// transcript. We mount a cheap shell first, then reveal the body on the next
	// paint for large transcripts so the slide transition isn't janky.
	//
	// The transcript blocks themselves are cheap to render (plain pre-wrapped
	// text + barebones tool-call lines); the ONLY heavy render is the final
	// answer, which is its own dedicated Markdown section below. So we render the
	// WHOLE transcript at once — no progressive block window, no idle-callback
	// growth that could stall under main-thread load and leave a permanent
	// "Loading earlier steps…" line (the exact jank the user reported).
	const EAGER_BODY_BLOCK_LIMIT = 40;
	const EAGER_BODY_MAX_CHARS = 200_000;

	let bodyReady = false;
	let bodyFrame: number | null = null;
	let bodyTimeout: ReturnType<typeof setTimeout> | null = null;

	const runAfterPaint = (fn: () => void) => {
		if (typeof window === 'undefined') {
			fn();
			return;
		}
		if (bodyFrame !== null) window.cancelAnimationFrame(bodyFrame);
		if (bodyTimeout !== null) clearTimeout(bodyTimeout);
		bodyFrame = window.requestAnimationFrame(() => {
			bodyFrame = null;
			bodyTimeout = setTimeout(() => {
				bodyTimeout = null;
				fn();
			}, 0);
		});
	};

	const resetHydration = () => {
		if (typeof window !== 'undefined' && bodyFrame !== null) window.cancelAnimationFrame(bodyFrame);
		if (bodyTimeout !== null) clearTimeout(bodyTimeout);
		bodyFrame = null;
		bodyTimeout = null;
		bodyReady = false;
	};

	const estimateBlockSize = (block: any) => {
		if (!block || typeof block !== 'object') return 0;
		if (['text', 'reasoning'].includes(block.type)) {
			return `${block.content ?? ''}`.length;
		}
		try {
			return JSON.stringify(block).length;
		} catch {
			return 0;
		}
	};

	const estimateInitialBodySize = () => {
		const blocks = Array.isArray(run?.content_blocks) ? run.content_blocks : [];
		if (blocks.length > 0) {
			return blocks.reduce((sum: number, block: any) => sum + estimateBlockSize(block), 0);
		}
		return `${run?.final_text ?? fallbackContent ?? ''}`.length;
	};

	const shouldShowBodyImmediately = () => {
		// Running subagents render text blocks as plain pre-wrapped text, so there
		// is no expensive parse to protect. Showing the body immediately lets the
		// slide transition measure the real in-progress content.
		if (status === 'running') return true;
		const blocks = Array.isArray(run?.content_blocks) ? run.content_blocks : [];
		if (blocks.length > EAGER_BODY_BLOCK_LIMIT) return false;
		return estimateInitialBodySize() <= EAGER_BODY_MAX_CHARS;
	};

	const scheduleBodyReady = () => {
		resetHydration();
		if (shouldShowBodyImmediately()) {
			bodyReady = true;
			return;
		}
		runAfterPaint(() => {
			if (!open) return;
			bodyReady = true;
		});
	};

	// Fallback: when the store has no entry for this subagent after reload,
	// fetch the subagent chat's content directly from the API.
	let fallbackFetching = false;
	let fallbackContent = '';
	let fallbackError = '';
	// Structured content_blocks of the hidden chat's final assistant message,
	// used to render thinking / tool calls / answer exactly like a live run.
	let fallbackBlocks: any[] | null = null;

	// Render the whole transcript at once — it is cheap (see above), so there is
	// Render the whole transcript at once — it is cheap (see above), so there is
	// no window to grow and nothing to "load later".
	$: visibleContentBlocks = transcriptBlocks;

	async function fetchFallbackContent() {
		if (fallbackFetching || fallbackContent) return;
		const chatId = subagentChatId;
		if (!chatId) return;
		fallbackFetching = true;
		try {
			const chat = await getChatById(localStorage.token, chatId);
			if (chat?.chat?.history) {
				const msgs = chat.chat.history.messages ?? {};
				// Walk to the leaf assistant message via currentId when available
				// so we read the actual final turn, not whatever Object.values
				// order happens to surface.
				const currentId = chat.chat.history.currentId;
				let leaf: any =
					currentId && msgs[currentId] ? msgs[currentId] : null;
				let lastContent = '';
				let lastBlocks: any[] | null = null;
				const capture = (msg: any) => {
					if (!msg || msg.role !== 'assistant') return;
					if (Array.isArray(msg.content_blocks) && msg.content_blocks.length) {
						lastBlocks = msg.content_blocks;
					}
					lastContent =
						(typeof msg.content === 'string' && msg.content.trim()) ||
						(Array.isArray(msg.content_blocks)
							? blocksToDisplayMarkdown(msg.content_blocks)
							: '') ||
						lastContent;
				};
				if (leaf) {
					capture(leaf);
				} else {
					for (const msg of Object.values(msgs)) {
						capture(msg);
					}
				}
				fallbackBlocks = lastBlocks;
				fallbackContent = lastContent || $i18n.t('(empty response)');
			} else {
				fallbackError = $i18n.t('Could not load subagent results.');
			}
		} catch (e) {
			console.error('Failed to fetch subagent fallback:', e);
			fallbackError = $i18n.t('Could not load subagent results.');
		} finally {
			fallbackFetching = false;
		}
	}

	// Trigger fallback fetch when user expands and the WORK transcript is missing.
	// The parent's persisted `subagent_runs` carries only `final_text`, so after
	// reload we have the answer (`finalAnswerText`) but no content_blocks — we must
	// still fetch the subagent chat to show the searches/fetches/bash/thinking.
	// Gate on the absence of structured work blocks, NOT on the answer.
	//
	// Never fetch while the run is actively in flight (`running`): the hidden
	// chat is mid-rewrite (a redo wipes it), so a fetch would surface the old
	// "(empty response)" the rerun is replacing. Live updates arrive via the
	// socket instead.
	$: hasRunContentBlocks = !!(
		(stale ? effectiveRun?.content_blocks : run?.content_blocks)?.length
	);
	$: if (
		open &&
		status !== 'running' &&
		subagentChatId &&
		!hasRunContentBlocks &&
		!fallbackBlocks &&
		!fallbackFetching &&
		!fallbackError
	) {
		fetchFallbackContent();
	}

	// When a run (re)enters the live running state, drop any cached fallback
	// text/error from a prior terminal view of this same block so the body
	// doesn't keep showing the old result while the rerun streams.
	$: if (
		run?.live &&
		status === 'running' &&
		(fallbackContent || fallbackError || fallbackBlocks)
	) {
		fallbackContent = '';
		fallbackError = '';
		fallbackBlocks = null;
		fallbackFetching = false;
	}

	// Root element + the scrollable messages container, used to keep this card
	// anchored under the cursor when toggling. Expanding a long subagent grows
	// the page; the chat's global auto-scroll (and natural reflow) would otherwise
	// yank the viewport to the bottom. We pin the card's on-screen position across
	// the expand so the user stays exactly where they were reading.
	let rootEl: HTMLDivElement | null = null;

	const getScrollContainer = (): HTMLElement | null => {
		if (typeof document === 'undefined') return null;
		return document.getElementById('messages-container');
	};

	const toggle = () => {
		const container = getScrollContainer();
		// Capture where the card's top sits in the viewport BEFORE the toggle.
		const beforeTop = rootEl?.getBoundingClientRect().top ?? null;

		open = !open;
		if (open) {
			scheduleBodyReady();
			// Tell the chat the user is reading this card, so its auto-scroll /
			// ResizeObserver stops yanking the viewport to the bottom while the
			// expanded body (and any ongoing parent stream) grows the page.
			rootEl?.dispatchEvent(
				new CustomEvent('subagent:expand', { bubbles: true })
			);
		} else {
			resetHydration();
			// Tell the chat the user finished reading — it re-enables stream
			// following if they're back near the bottom.
			rootEl?.dispatchEvent(
				new CustomEvent('subagent:collapse', { bubbles: true })
			);
		}

		// After the DOM settles (body mount + slide start), restore the card to
		// the same viewport offset by adjusting the container's scrollTop by the
		// delta. Two rAFs so we run after Svelte's flush and the browser's layout.
		if (container && beforeTop != null && typeof window !== 'undefined') {
			const restore = () => {
				if (!rootEl) return;
				const afterTop = rootEl.getBoundingClientRect().top;
				const delta = afterTop - beforeTop;
				if (Math.abs(delta) > 0.5) {
					container.scrollTop += delta;
				}
			};
			tick().then(() => {
				requestAnimationFrame(() => {
					restore();
					// A second correction on the next frame catches the slide
					// transition's first measured height (the body grows over a
					// frame or two), keeping the anchor stable without chasing the
					// whole animation.
					requestAnimationFrame(restore);
				});
			});
		}
	};

	// Used by the tool_calls per-block render to look up the matching result
	// entry. Lives outside the template so the typed callback doesn't trip
	// Svelte's template parser (which doesn't accept TS annotations inside
	// `{@const}` expressions).
	const findToolResult = (results: any[] | undefined, callId: string | undefined) =>
		(results ?? []).find((r: any) => r?.tool_call_id === callId);

	const previewText = (value: unknown, max = 6000) => {
		const text = `${value ?? ''}`;
		return text.length > max ? `${text.slice(0, max).trimEnd()}\n\n…` : text;
	};

	// Redo menu state — anchored to the redo button in the header. The
	// outside-click handler covers both "click elsewhere" and "click on the
	// header (which would toggle the collapsible)".
	let redoMenuOpen = false;
	let redoMenuContainer: HTMLDivElement | null = null;
	let redoBusy = false;

	const closeRedoMenu = () => {
		redoMenuOpen = false;
	};

	const onDocClick = (e: MouseEvent) => {
		if (!redoMenuOpen) return;
		const target = e.target as Node | null;
		if (target && redoMenuContainer && !redoMenuContainer.contains(target)) {
			redoMenuOpen = false;
		}
	};

	const onDocKey = (e: KeyboardEvent) => {
		if (e.key === 'Escape') redoMenuOpen = false;
	};

	if (typeof document !== 'undefined') {
		document.addEventListener('click', onDocClick);
		document.addEventListener('keydown', onDocKey);
	}
	onDestroy(() => {
		resetHydration();
		if (timerInterval) {
			clearInterval(timerInterval);
			timerInterval = null;
		}
		if (typeof document !== 'undefined') {
			document.removeEventListener('click', onDocClick);
			document.removeEventListener('keydown', onDocKey);
		}
	});

	const toggleRedoMenu = (e: Event) => {
		e.preventDefault();
		e.stopPropagation();
		redoMenuOpen = !redoMenuOpen;
	};

	const triggerRerun = async (scope: SubagentRerunScope) => {
		closeRedoMenu();
		if (redoBusy) return;
		if (status === 'running') {
			toast.error($i18n.t('Subagent is already running.'));
			return;
		}
		// "Restart from beginning" wipes the WHOLE hidden subagent chat, which
		// discards every follow-up (continuation) turn for this subagent. Warn
		// before destroying that work so it never happens silently.
		if (scope === 'from_launch' && run?.subagent_id) {
			const continuations = Object.values($subagentLiveStates).filter(
				(r: any) =>
					r &&
					r.continuation === true &&
					(r.subagent_id === run.subagent_id || r.chat_id === run.subagent_id)
			).length;
			if (continuations > 0) {
				const ok =
					typeof window === 'undefined' ||
					window.confirm(
						$i18n.t(
							'Restarting from the beginning discards this subagent’s {{n}} follow-up turn(s). Continue?',
							{ n: continuations }
						)
					);
				if (!ok) return;
			}
		}
		// We need a few things to call /rerun: which parent chat (the chat
		// we're inside), which parent message (stored on the run), which
		// entry to refresh (the stateKey), and a session_id so events route
		// back to us. If any are missing the rerun would no-op silently —
		// surface that instead.
		const parentChatId = $chatId;
		const parentMessageId = run?.parent_message_id;
		const sessionId = $socket?.id;
		const entryKey = run?.entry_key || stateKey;
		if (!parentChatId || !parentMessageId || !sessionId || !entryKey) {
			toast.error($i18n.t('Cannot redo subagent: missing chat / message / session context.'));
			return;
		}

		redoBusy = true;
		try {
			await rerunSubagent(localStorage.token, {
				parent_chat_id: parentChatId,
				parent_message_id: parentMessageId,
				session_id: sessionId,
				entry_key: entryKey,
				scope
			});
			// Optimistically flip status so the spinner shows while the
			// backend's `chat:subagent:start` event is in flight. The store
			// entry will fully refresh once that event arrives.
			subagentLiveStates.update((s) => {
				const cur = s[stateKey] || s[entryKey];
				if (!cur) return s;
				const next: any = {
					...cur,
					status: 'running',
					live: true,
					content_blocks: [],
					content: '',
					final_text: undefined,
					error: undefined,
					stale: false,
					started_at: Math.floor(Date.now() / 1000),
					ended_at: undefined
				};
				const out = { ...s };
				// Write under EVERY key this run is addressable by (the store
				// seeds each run under up to 4 keys). Updating only two left the
				// others pointing at the stale done/stale object, so a card that
				// resolved via a different key kept showing the old state.
				const keys = [
					stateKey,
					entryKey,
					cur.tool_call_id,
					cur.subagent_id,
					cur.chat_id,
					cur.entry_key
				].filter(Boolean);
				for (const k of keys) out[k] = next;
				return out;
			});
			// Open the subagent body after redo so the live rerun is visible
			// even after the redo dropdown closes.
			open = true;
			resetHydration();
			bodyReady = true;
		} catch (err: any) {
			console.error(err);
			toast.error(`${err?.detail ?? err?.message ?? err}`);
		} finally {
			redoBusy = false;
		}
	};
</script>

<div
	class="my-2 rounded-2xl border border-gray-200 dark:border-gray-800 bg-white/40 dark:bg-gray-900/40 subagent-block"
	data-tool-call-id={attributes?.tool_call_id ?? ''}
	data-subagent-id={attributes?.id ?? ''}
	bind:this={rootEl}
>
	<div class="flex items-center gap-2 px-3 py-2">
		<button
			type="button"
			class="flex-1 flex items-center gap-2 text-left hover:opacity-90 transition-opacity"
			on:click|preventDefault={toggle}
			aria-expanded={open}
		>
			<!-- Status icon -->
			<span class="shrink-0 inline-flex items-center justify-center size-5">
				{#if status === 'running'}
					<Spinner className="size-4" />
				{:else if status === 'done'}
					<svg
						xmlns="http://www.w3.org/2000/svg"
						viewBox="0 0 24 24"
						fill="none"
						stroke="currentColor"
						stroke-width="2"
						stroke-linecap="round"
						stroke-linejoin="round"
						class="size-4 text-green-600 dark:text-green-500"
					>
						<path d="M20 6L9 17l-5-5" />
					</svg>
				{:else if status === 'error'}
					<svg
						xmlns="http://www.w3.org/2000/svg"
						viewBox="0 0 24 24"
						fill="none"
						stroke="currentColor"
						stroke-width="2"
						stroke-linecap="round"
						stroke-linejoin="round"
						class="size-4 text-red-600 dark:text-red-500"
					>
						<line x1="18" y1="6" x2="6" y2="18" />
						<line x1="6" y1="6" x2="18" y2="18" />
					</svg>
				{:else if status === 'cancelled'}
					<svg
						xmlns="http://www.w3.org/2000/svg"
						viewBox="0 0 24 24"
						fill="none"
						stroke="currentColor"
						stroke-width="2"
						stroke-linecap="round"
						stroke-linejoin="round"
						class="size-4 text-gray-500"
					>
						<circle cx="12" cy="12" r="10" />
						<line x1="4.93" y1="4.93" x2="19.07" y2="19.07" />
					</svg>
				{/if}
			</span>

			<!-- Header text -->
			<div class="flex-1 min-w-0 flex items-baseline gap-2">
				<span class="text-sm font-medium text-gray-900 dark:text-gray-100">
					{$i18n.t('Subagent')}{displayNum != null ? ` #${displayNum}` : ''}
				</span>
				{#if displayName}
					<span class="text-sm text-gray-500 dark:text-gray-400 truncate">
						— {displayName}{continuation ? ` (${$i18n.t('continued')})` : ''}
					</span>
				{/if}
			</div>

			<!-- Status badge + elapsed timer -->
			<span
				class="shrink-0 text-xs font-medium {status === 'done'
					? 'text-green-600 dark:text-green-500'
					: status === 'error'
						? 'text-red-600 dark:text-red-500'
						: status === 'cancelled'
							? 'text-gray-500'
							: 'text-blue-600 dark:text-blue-400'}"
			>
				{headerStatusText}
			</span>
			{#if status === 'running' && timerText}
				<span class="shrink-0 text-xs tabular-nums text-gray-400 dark:text-gray-500">
					{timerText}
				</span>
			{/if}

			<!-- Caret -->
			<svg
				xmlns="http://www.w3.org/2000/svg"
				viewBox="0 0 24 24"
				fill="none"
				stroke="currentColor"
				stroke-width="2"
				stroke-linecap="round"
				stroke-linejoin="round"
				class="size-4 shrink-0 transition-transform text-gray-500 {open ? 'rotate-180' : ''}"
			>
				<polyline points="6 9 12 15 18 9" />
			</svg>
		</button>

		<!-- Redo dropdown (sibling of the header toggle, not inside it, so the
			click doesn't bubble up to the collapsible toggle). -->
		<div class="relative shrink-0" bind:this={redoMenuContainer}>
			<button
				type="button"
				class="p-1.5 rounded-md text-gray-500 hover:text-gray-900 dark:hover:text-gray-100 hover:bg-gray-100 dark:hover:bg-gray-800 transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
				on:click={toggleRedoMenu}
				disabled={status === 'running' || redoBusy}
				aria-label={$i18n.t('Redo subagent')}
				title={$i18n.t('Redo subagent')}
			>
				<svg
					xmlns="http://www.w3.org/2000/svg"
					viewBox="0 0 24 24"
					fill="none"
					stroke="currentColor"
					stroke-width="1.75"
					stroke-linecap="round"
					stroke-linejoin="round"
					class="size-4"
				>
					<polyline points="23 4 23 10 17 10" />
					<path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10" />
				</svg>
			</button>

			{#if redoMenuOpen}
				<div
					class="absolute right-0 top-full mt-1 z-10 min-w-[200px] rounded-lg border border-gray-200 dark:border-gray-800 bg-white dark:bg-gray-900 shadow-lg overflow-hidden"
					transition:slide={{ duration: 120, axis: 'y' }}
				>
					<button
						type="button"
						class="w-full text-left px-3 py-2 text-sm text-gray-900 dark:text-gray-100 hover:bg-gray-100 dark:hover:bg-gray-800 flex flex-col gap-0.5"
						on:click|preventDefault={() => triggerRerun('this_turn')}
					>
						<span class="font-medium">{$i18n.t('Redo last subagent turn')}</span>
						<span class="text-xs text-gray-500 dark:text-gray-400">
							{$i18n.t('Re-send the same request; keep prior turns intact.')}
						</span>
					</button>
					<div class="h-px bg-gray-100 dark:bg-gray-800" />
					<button
						type="button"
						class="w-full text-left px-3 py-2 text-sm text-gray-900 dark:text-gray-100 hover:bg-gray-100 dark:hover:bg-gray-800 flex flex-col gap-0.5"
						on:click|preventDefault={() => triggerRerun('from_launch')}
					>
						<span class="font-medium">{$i18n.t('Redo subagent')}</span>
						<span class="text-xs text-gray-500 dark:text-gray-400">
							{$i18n.t("Discard the subagent's work; restart from the original prompt.")}
						</span>
					</button>
				</div>
			{/if}
		</div>
	</div>

	{#if open}
		<div transition:slide={{ duration: 250, easing: quintOut }} class="px-3 pb-3">
			{#if !bodyReady}
				<div class="space-y-2 py-1">
					<div class="h-4 w-1/3 rounded bg-gray-100 dark:bg-gray-800"></div>
					<div class="h-20 rounded-xl bg-gray-100 dark:bg-gray-800"></div>
				</div>
			{:else}
				{#if run?.prompt}
					<div
						class="mb-2 text-xs text-gray-500 dark:text-gray-500 border-l-2 border-gray-200 dark:border-gray-700 pl-2"
					>
						<span class="font-semibold text-gray-700 dark:text-gray-400">
							{$i18n.t('Prompt')}:
						</span>
						{run.prompt}
						{#if run?.background}
							<div class="mt-1">
								<span class="font-semibold text-gray-700 dark:text-gray-400">
									{$i18n.t('Background')}:
								</span>
								{run.background}
							</div>
						{/if}
					</div>
				{/if}

				{#if status === 'error' && (effectiveRun?.error || run?.error)}
					{@const errSrc = effectiveRun?.error ?? run?.error}
					<div
						class="rounded-lg bg-red-50 dark:bg-red-950/30 border border-red-200 dark:border-red-900 px-3 py-2 text-sm text-red-700 dark:text-red-300"
					>
						{typeof errSrc === 'string'
							? errSrc
							: (errSrc?.message ?? $i18n.t('Subagent failed without details.'))}
					</div>
				{/if}

				{#if hasTranscript}
					<div class="subagent-inner space-y-2">
						{#each visibleContentBlocks as block, i (i)}
							{#if block?.type === 'text'}
								<!-- Interstitial "thinking out loud" text between tool calls.
								     Rendered as plain pre-wrapped text (cheap, instant) — the
								     final answer is the only rich-Markdown render, in its own
								     section below. -->
								<div
									class="whitespace-pre-wrap text-sm text-gray-800 dark:text-gray-200 leading-relaxed"
								>
									{doneRendering ? previewText(block.content) : (block.content ?? '')}
								</div>
							{:else if block?.type === 'reasoning'}
								<details class="rounded-lg bg-gray-50 dark:bg-gray-850 px-3 py-1.5">
									<summary
										class="text-xs font-medium text-gray-600 dark:text-gray-400 cursor-pointer select-none"
									>
										{#if block.duration != null}
											{$i18n.t('Thought for {{n}}s', { n: block.duration })}
										{:else}
											{$i18n.t('Thinking…')}
										{/if}
									</summary>
									<div
										class="mt-2 whitespace-pre-wrap text-xs text-gray-500 dark:text-gray-400 leading-relaxed"
									>
										{block.content ?? ''}
									</div>
								</details>
							{:else if block?.type === 'tool_calls'}
								<div class="space-y-1">
									{#each block.content ?? [] as call, j (call?.id ?? `tc-${i}-${j}`)}
										{@const result = findToolResult(block.results, call?.id)}
										{@const callErrored = result?.error === true}
										<div
											class="flex items-baseline gap-2 text-xs font-mono {callErrored
												? 'text-red-600 dark:text-red-400'
												: 'text-gray-600 dark:text-gray-400'}"
										>
											<span class="shrink-0 w-3 text-center">
												{result === undefined ? '·' : callErrored ? '✗' : '✓'}
											</span>
											<span
												class="shrink-0 whitespace-nowrap font-medium {callErrored
													? 'text-red-600 dark:text-red-400'
													: 'text-gray-800 dark:text-gray-300'}"
											>
												{call?.function?.name ?? 'tool'}
											</span>
											<span class="min-w-0 truncate {callErrored
													? 'text-red-500/80 dark:text-red-400/80'
													: 'text-gray-500 dark:text-gray-500'}">
												{callErrored && result?.error_reason
													? result.error_reason
													: (call?.function?.arguments ?? '')}
											</span>
										</div>
									{/each}
								</div>
							{/if}
						{/each}
					</div>
				{/if}

				<!-- The subagent's FINAL answer. Always rendered (when terminal) as a
				     dedicated, immediately-visible section so it is never hidden behind
				     the transcript window's progressive hydration. Falls back to
				     run.final_text / the reload fetch when the transcript lacks it. -->
				{#if finalAnswerText}
					<div class="markdown-prose" class:mt-3={hasTranscript} class:pt-3={hasTranscript} class:border-t={hasTranscript} class:border-gray-100={hasTranscript} class:dark:border-gray-800={hasTranscript}>
						<Markdown
							id={`subagent-${stateKey}-final`}
							content={finalAnswerText}
							done={true}
							editCodeBlock={false}
						/>
					</div>
				{:else if !hasTranscript && fallbackContent}
					<div class="markdown-prose">
						<Markdown
							id={`subagent-${stateKey}-fallback`}
							content={fallbackContent}
							done={true}
							editCodeBlock={false}
						/>
					</div>
				{:else if !hasTranscript && fallbackFetching}
					<div class="text-sm text-gray-500 dark:text-gray-400 italic">
						{$i18n.t('Loading subagent results…')}
					</div>
				{:else if !hasTranscript && fallbackError}
					<div class="text-sm text-red-500 italic">{fallbackError}</div>
				{:else if !hasTranscript && status === 'running'}
					<div class="text-sm text-gray-500 dark:text-gray-400 italic">
						{$i18n.t('Subagent is starting up…')}
					</div>
				{:else if !hasTranscript && status === 'error'}
					<div class="text-sm text-red-500 italic">
						{$i18n.t('Subagent did not produce any output.')}
					</div>
				{/if}

				{#if subagentChatId}
					<div class="mt-3 pt-2 border-t border-gray-100 dark:border-gray-800">
						<a
							href={`/c/${subagentChatId}`}
							target="_blank"
							rel="noopener noreferrer"
							class="text-xs text-gray-500 dark:text-gray-400 hover:text-gray-900 dark:hover:text-gray-100 inline-flex items-center gap-1"
						>
							{$i18n.t('Open full subagent chat')}
							<svg
								xmlns="http://www.w3.org/2000/svg"
								viewBox="0 0 24 24"
								fill="none"
								stroke="currentColor"
								stroke-width="1.5"
								class="size-3"
							>
								<path d="M7 17L17 7M17 7H9M17 7v8" stroke-linecap="round" stroke-linejoin="round" />
							</svg>
						</a>
					</div>
				{/if}
			{/if}
		</div>
	{/if}
</div>

<style>
	.subagent-block :global(details summary) {
		cursor: pointer;
	}
</style>
