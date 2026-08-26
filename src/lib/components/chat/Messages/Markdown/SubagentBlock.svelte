<script lang="ts">
	import { preventDefault } from '$lib/utils/eventModifiers';

	import { getContext, onDestroy, onMount, tick } from 'svelte';
	import { slide } from 'svelte/transition';
	import { quintOut } from 'svelte/easing';
	import { toast } from '$lib/utils/toast';

	import dayjs from '$lib/dayjs';
	import duration from 'dayjs/plugin/duration';
	import relativeTime from 'dayjs/plugin/relativeTime';

	import { chatId, socket, subagentLiveStates } from '$lib/stores';
	import { rerunSubagent, stopSubagentRerun, type SubagentRerunScope } from '$lib/apis/subagents';
	import { getChatById } from '$lib/apis/chats';
	import { blocksToDisplayMarkdown, formatDurationLong } from '$lib/utils';
	import {
		countUniqueSubagentContinuations,
		findSubagentRunEntry,
		setSubagentRunAliases,
		shouldApplyRerunOptimisticState
	} from '$lib/utils/subagentState';
	import Markdown from '../Markdown.svelte';
	import Spinner from '$lib/components/common/Spinner.svelte';

	dayjs.extend(duration);
	dayjs.extend(relativeTime);

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

	// These come from the containing parent Markdown render. Persisted
	// subagent_runs predate parent_message_id and may not carry it after a full

	// Generated files from the subagent roll up into the PARENT message's files,

	interface Props {
		//   done         — "true" once the parent tool result lands
		attributes?: Record<string, any>;
		// reload, so action buttons must not depend exclusively on the run object.
		parentChatId?: string;
		parentMessageId?: string;
		// so its final-answer markdown resolves sandbox: links against the same list.
		sandboxFiles?: any[];
	}

	let {
		attributes = {},
		parentChatId = '',
		parentMessageId = '',
		sandboxFiles = []
	}: Props = $props();

	let rootEl: HTMLDivElement | null = $state(null);
	let mountedParentChatId = $state('');
	let mountedParentMessageId = $state('');

	const parentChatIdFromLocation = () => {
		if (typeof window === 'undefined') return '';
		const match = window.location.pathname.match(/(?:^|\/)c\/([^/?#]+)/);
		if (!match?.[1]) return '';
		try {
			return decodeURIComponent(match[1]);
		} catch {
			return match[1];
		}
	};

	const parentMessageIdFromElement = () => {
		const id = rootEl?.closest<HTMLElement>('[id^="message-"]')?.id ?? '';
		return id.startsWith('message-') ? id.slice('message-'.length) : '';
	};

	onMount(() => {
		// The component is mounted inside `#message-{id}`. Capture both DOM and
		// route context as an independent recovery path for legacy persisted runs
		// and for callers that render cached Markdown tokens without action props.
		mountedParentChatId = parentChatIdFromLocation();
		mountedParentMessageId = parentMessageIdFromElement();
	});

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

	const resolveActionContext = () => {
		const chatCandidates = [
			parentChatId,
			mountedParentChatId,
			parentChatIdFromLocation(),
			$chatId
		].filter((value): value is string => typeof value === 'string' && value.length > 0);
		const savedChatId =
			chatCandidates.find((value) => !value.startsWith('local:')) ?? chatCandidates[0] ?? '';
		return {
			parentChatId: savedChatId,
			parentMessageId:
				parentMessageId ||
				mountedParentMessageId ||
				parentMessageIdFromElement() ||
				run?.parent_message_id ||
				attributes?.parent_message_id ||
				'',
			entryKey: run?.entry_key || stateKey || attrToolCallId || attrSubagentId || ''
		};
	};

	// A single self-managing 1s ticker, held ONLY while this block is actively
	// running. `nowTs` is deliberately isolated so a tick recomputes just the
	// timer string — never the heavy contentBlocks / hydration reactives.
	let nowTs = $state(Math.floor(Date.now() / 1000));
	let timerInterval: ReturnType<typeof setInterval> | null = $state(null);

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
	const hasToolCallBlocks = (blocks: any[] | null | undefined) =>
		Array.isArray(blocks) &&
		blocks.some(
			(block) =>
				block?.type === 'tool_calls' && Array.isArray(block?.content) && block.content.length > 0
		);

	// Default collapsed in every state (running / done / error / cancelled).
	// The user clicks the header (or the redo button) to expand and watch
	// the subagent's current work. The status badge + spinner in the header
	// still convey progress at a glance without forcing the body open.
	let open = $state(false);

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

	let bodyReady = $state(false);
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
		const blocks = Array.isArray(runContentBlocks) ? runContentBlocks : [];
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
		const blocks = Array.isArray(runContentBlocks) ? runContentBlocks : [];
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
	let fallbackFetching = $state(false);
	let fallbackContent = $state('');
	let fallbackError = $state('');
	// Structured content_blocks of the hidden chat's final assistant message,
	// used to render thinking / tool calls / answer exactly like a live run.
	let fallbackBlocks: any[] | null = $state(null);

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
				let leaf: any = currentId && msgs[currentId] ? msgs[currentId] : null;
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

	// Root element + the scrollable messages container, used to keep this card
	// anchored under the cursor when toggling. Expanding a long subagent grows
	// the page; the chat's global auto-scroll (and natural reflow) would otherwise
	// yank the viewport to the bottom. We pin the card's on-screen position across
	// the expand so the user stays exactly where they were reading.
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
			rootEl?.dispatchEvent(new CustomEvent('subagentexpand', { bubbles: true }));
		} else {
			resetHydration();
			// Tell the chat the user finished reading — it re-enables stream
			// following if they're back near the bottom.
			rootEl?.dispatchEvent(new CustomEvent('subagentcollapse', { bubbles: true }));
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
	let redoMenuOpen = $state(false);
	let redoMenuContainer: HTMLDivElement | null = $state(null);
	let redoBusy = $state(false);
	let stopBusy = $state(false);
	// Branch actions share one chooser. A blocked redo opens it after the
	// `subagent_parent_moved_on` guard; manual answer adoption opens it
	// immediately so every selected repaired answer is installed by ONE atomic
	// rewind commit before the parent resumes.
	let branchActionPrompt: {
		action: 'rerun' | 'adopt';
		scope: SubagentRerunScope;
		message: string;
	} | null = $state(null);
	// Which of the turn's subagents to redo together (entry keys). Initialized to
	// the clicked one when the panel opens; the user can add parallel siblings.
	let redoSelection: Set<string> = $state(new Set());
	// Per-selected subagent redo mode. The clicked card starts with the mode the
	// user chose from its menu; added siblings default to "Redo last subagent turn"
	// so selecting extra workers does not accidentally wipe their whole hidden chats.
	let redoScopes: Record<string, SubagentRerunScope> = $state({});

	const toggleRedoSelection = (key: string) => {
		const next = new Set(redoSelection);
		const nextScopes = { ...redoScopes };
		if (next.has(key)) {
			next.delete(key);
		} else {
			next.add(key);
			nextScopes[key] = nextScopes[key] ?? 'this_turn';
		}
		redoSelection = next;
		redoScopes = nextScopes;
	};

	const setRedoScope = (key: string, scope: SubagentRerunScope) => {
		redoScopes = { ...redoScopes, [key]: scope };
	};

	const onRedoScopeChange = (key: string, event: Event) => {
		const value = (event.currentTarget as HTMLSelectElement)?.value;
		setRedoScope(key, value === 'from_launch' ? 'from_launch' : 'this_turn');
	};

	const markRerunStoppedOptimistically = () => {
		subagentLiveStates.update((s) => {
			const aliases = [
				stateKey,
				run?.entry_key,
				run?.tool_call_id,
				run?.subagent_id,
				run?.chat_id
			].filter(Boolean);
			const cur = findSubagentRunEntry(s, resolvedParentMessageId, aliases)?.[1] ?? null;
			if (!cur) return s;
			const next: any = {
				...cur,
				status: 'cancelled',
				live: false,
				final_text: cur.final_text || cur.previous_final_text,
				ended_at: Math.floor(Date.now() / 1000)
			};
			const out = { ...s };
			setSubagentRunAliases(out, next, aliases, resolvedParentMessageId);
			return out;
		});
	};

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
			const continuations = countUniqueSubagentContinuations($subagentLiveStates, run.subagent_id);
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
		const {
			parentChatId: targetParentChatId,
			parentMessageId: targetParentMessageId,
			entryKey
		} = resolveActionContext();
		const sessionId = $socket?.id;
		if (!targetParentChatId || !targetParentMessageId || !entryKey) {
			console.error('Cannot identify rendered subagent card', {
				targetParentChatId,
				targetParentMessageId,
				entryKey,
				attributes
			});
			toast.error($i18n.t('This subagent card could not be identified. Reload the chat.'));
			return;
		}
		if (!sessionId) {
			toast.error($i18n.t('Cannot redo subagent: the live session is not connected.'));
			return;
		}

		redoBusy = true;
		try {
			const rerunRes = await rerunSubagent(localStorage.token, {
				parent_chat_id: targetParentChatId,
				parent_message_id: targetParentMessageId,
				session_id: sessionId,
				entry_key: entryKey,
				scope
			});
			// Optimistically flip status so the spinner shows while the
			// backend's `chat:subagent:start` event is in flight. The store
			// entry will fully refresh once that event arrives.
			subagentLiveStates.update((s) => {
				const aliases = [
					stateKey,
					entryKey,
					run?.tool_call_id,
					run?.subagent_id,
					run?.chat_id,
					run?.entry_key
				].filter(Boolean);
				const cur = findSubagentRunEntry(s, targetParentMessageId, aliases)?.[1] ?? null;
				if (!cur) return s;
				// A very fast rerun can finish over the socket before the POST
				// response reaches this callback. Never let the late optimistic
				// flip overwrite that terminal state with a permanent spinner.
				if (!shouldApplyRerunOptimisticState(cur, rerunRes?.rerun_id)) {
					return s;
				}
				const next: any = {
					...cur,
					rerun: true,
					rerun_task_id: rerunRes?.task_id,
					rerun_id: rerunRes?.rerun_id,
					status: 'running',
					live: true,
					content_blocks: [],
					content: '',
					previous_final_text: cur.final_text || cur.previous_final_text,
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
				setSubagentRunAliases(out, next, aliases, targetParentMessageId);
				return out;
			});
			// Open the subagent body after redo so the live rerun is visible
			// even after the redo dropdown closes.
			open = true;
			resetHydration();
			bodyReady = true;
		} catch (err: any) {
			console.error(err);
			// The api client unwraps the 409 `detail` → `{ message, code }`. A
			// `subagent_parent_moved_on` code means the parent already continued, so
			// an in-place redo is unsafe — offer the inline "Rewind & redo" flow
			// instead of dead-ending with a toast. Other errors keep the toast.
			const code = err?.code ?? err?.detail?.code;
			const msg = err?.message ?? err?.detail?.message ?? err?.detail ?? err;
			if (code === 'subagent_parent_moved_on') {
				const clickedKey = run?.entry_key || stateKey;
				branchActionPrompt = {
					action: 'rerun',
					scope,
					message: typeof msg === 'string' ? msg : ''
				};
				redoSelection = new Set([clickedKey].filter(Boolean));
				redoScopes = clickedKey ? { [clickedKey]: scope } : {};
				redoMenuOpen = false;
				open = true;
			} else {
				toast.error(`${typeof msg === 'string' ? msg : (err?.detail ?? err)}`);
			}
		} finally {
			redoBusy = false;
		}
	};

	// The full hidden chat uses the ordinary branch UI, so a user can repair a
	// failed subagent there with another model. Always route adoption through the
	// atomic rewind chooser—even when the source response is still the selected
	// leaf. The old single-adopt-then-resume sequence let two repaired siblings
	// race: the first resume could assemble before the second adoption committed.
	const triggerAdopt = () => {
		closeRedoMenu();
		if (redoBusy) return;
		if (status === 'running') {
			toast.error($i18n.t('Subagent is already running.'));
			return;
		}
		const {
			parentChatId: targetParentChatId,
			parentMessageId: targetParentMessageId,
			entryKey
		} = resolveActionContext();
		if (
			!targetParentChatId ||
			!targetParentMessageId ||
			!entryKey ||
			targetParentChatId.startsWith('local:')
		) {
			console.error('Cannot identify rendered subagent card', {
				targetParentChatId,
				targetParentMessageId,
				entryKey,
				attributes
			});
			toast.error($i18n.t('This subagent card could not be identified. Reload the chat.'));
			return;
		}

		branchActionPrompt = {
			action: 'adopt',
			scope: 'this_turn',
			message: ''
		};
		redoSelection = new Set([entryKey]);
		redoScopes = {};
		redoMenuOpen = false;
		open = true;
	};

	const triggerStopRerun = async (e: Event) => {
		e.preventDefault();
		e.stopPropagation();
		if (stopBusy) return;
		const {
			parentChatId: targetParentChatId,
			parentMessageId: targetParentMessageId,
			entryKey
		} = resolveActionContext();
		const subagentId = run?.subagent_id || run?.chat_id || attrSubagentId || '';
		if (!targetParentChatId || !targetParentMessageId || !entryKey) {
			toast.error($i18n.t('This subagent card could not be identified. Reload the chat.'));
			return;
		}
		stopBusy = true;
		try {
			const res = await stopSubagentRerun(localStorage.token, {
				parent_chat_id: targetParentChatId,
				parent_message_id: targetParentMessageId,
				entry_key: entryKey,
				subagent_id: subagentId
			});
			if ((res?.stopped ?? 0) > 0) {
				markRerunStoppedOptimistically();
			} else {
				toast.error($i18n.t('No active subagent redo task was found.'));
			}
		} catch (err: any) {
			console.error(err);
			toast.error(`${err?.message ?? err?.detail ?? err}`);
		} finally {
			stopBusy = false;
		}
	};

	// User confirmed "Rewind & redo" on the moved-on panel: hand off to Chat.svelte
	// (which owns `history`) via a bubbling CustomEvent. Chat.svelte rewinds the
	// parent to ONE sibling branch ending after the selected subagents' tool calls,
	// redoes them in parallel there, then resumes the parent once from the results.
	const triggerRewindRedo = () => {
		const action = branchActionPrompt?.action ?? 'rerun';
		const scope: SubagentRerunScope = branchActionPrompt?.scope ?? 'this_turn';
		const { parentMessageId: targetParentMessageId } = resolveActionContext();
		// Selected siblings, falling back to just the clicked one.
		let entries = turnSubagents
			.filter((t: any) => redoSelection.has(t.entryKey))
			.map((t: any) => ({
				entryKey: t.entryKey,
				toolCallId: t.toolCallId,
				subagentId: t.subagentId,
				scope: redoScopes[t.entryKey] ?? branchActionPrompt?.scope ?? 'this_turn'
			}));
		if (entries.length === 0) {
			const clickedKey = run?.entry_key || stateKey;
			if (clickedKey) {
				entries = [
					{
						entryKey: clickedKey,
						toolCallId: run?.tool_call_id ?? attrToolCallId ?? '',
						subagentId: run?.subagent_id ?? run?.chat_id ?? attrSubagentId ?? '',
						scope: redoScopes[clickedKey] ?? branchActionPrompt?.scope ?? 'this_turn'
					}
				];
			}
		}
		branchActionPrompt = null;
		if (!targetParentMessageId || entries.length === 0) {
			toast.error($i18n.t('Cannot redo subagent: missing chat / message context.'));
			return;
		}
		rootEl?.dispatchEvent(
			new CustomEvent(action === 'adopt' ? 'subagentrewindadopt' : 'subagentrewindredo', {
				bubbles: true,
				detail: { parentMessageId: targetParentMessageId, scope, entries }
			})
		);
	};
	let attrToolCallId = $derived((attributes?.tool_call_id || '') as string);
	let attrSubagentId = $derived((attributes?.id || '') as string);
	let attrArgs = $derived(parseArgs(attributes?.arguments));
	// The persisted placeholder stamps done="true" once the tool call returned.
	// Used as a terminal-state fallback when no live/seeded run exists for this
	// block (e.g. a malformed subagent call that errored before creating a run).
	let attrDone = $derived(String(attributes?.done) === 'true');
	let lookupParentMessageId = $derived(
		parentMessageId || mountedParentMessageId || attributes?.parent_message_id || ''
	);
	let lookupAliases = $derived([attrToolCallId, attrSubagentId].filter(Boolean));
	let directRunEntry = $derived(
		findSubagentRunEntry($subagentLiveStates, lookupParentMessageId, lookupAliases, {
			scan: false
		}) ?? null
	);
	// Hot path is collapsed. Avoid scanning every subagent store entry unless the
	// user expands a block whose direct keys somehow missed after reload.
	let scannedRunEntry = $derived(
		!directRunEntry && open
			? (findSubagentRunEntry($subagentLiveStates, lookupParentMessageId, lookupAliases) ??
					undefined)
			: undefined
	);
	let runEntry = $derived(directRunEntry ?? scannedRunEntry ?? null);
	let stateKey = $derived(runEntry?.[1]?.entry_key || attrToolCallId || attrSubagentId || '');
	let run = $derived(runEntry?.[1]);
	let resolvedParentChatId = $derived(parentChatId || mountedParentChatId || $chatId);
	let resolvedParentMessageId = $derived(
		parentMessageId ||
			mountedParentMessageId ||
			run?.parent_message_id ||
			attributes?.parent_message_id ||
			''
	);
	let stale = $derived(run?.stale === true);
	// A stale entry was orphaned by a "Redo subagent" (from_launch) restart: its
	// own recorded status is frozen at whatever it was before the wipe, so it
	// can't be trusted. The relaunch's outcome lives on the SAME subagent's
	// non-continuation (launch) entry, which the load path re-seeds with the
	// real result. Find that sibling so this card can mirror it instead of
	// inventing a status. If no launch sibling exists, fall back to the entry's
	// own state.
	let staleLaunchSibling = $derived(
		stale && run?.subagent_id
			? ((Object.values($subagentLiveStates) as any[])
					.filter(
						(r: any) =>
							r &&
							(!resolvedParentMessageId ||
								!r.parent_message_id ||
								r.parent_message_id === resolvedParentMessageId) &&
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
	let effectiveRun = $derived(stale ? (staleLaunchSibling ?? run) : run);
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
	let status = $derived(
		(stale
			? // Mirror the relaunch's authoritative outcome (its launch sibling),
				// NOT the frozen status on this orphaned entry. Only show a green
				// "done" when that run truly finished with a final answer; if the
				// relaunch was cancelled / errored / is still running, surface that
				// honestly instead of faking success.
				effectiveRun?.status === 'error'
				? 'error'
				: effectiveRun?.status === 'cancelled'
					? 'cancelled'
					: effectiveRun?.status === 'done' || effectiveRun?.final_text
						? 'done'
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
							: (run?.status ?? 'running')) as 'running' | 'done' | 'error' | 'cancelled'
	);
	let displayName = $derived(run?.name || attrArgs?.name || '');
	let displayNum = $derived(run?.num ?? null);
	let subagentChatId = $derived(run?.chat_id || run?.subagent_id || attrSubagentId || '');
	let continuation = $derived(
		run?.continuation === true || attributes?.name === 'subagent_continue'
	);
	// --- Elapsed-time timer ---------------------------------------------------
	// Timing lives on the run object as `started_at` / `ended_at` (UNIX seconds,
	// stamped by the backend at every lifecycle point and mirrored client-side;
	// both survive reload via the persisted `subagent_runs` map). A stale card
	// (orphaned by a `from_launch` redo) mirrors its launch sibling's timing,
	// exactly like the status / final-text logic above.
	let timingRun = $derived(stale ? (effectiveRun ?? run) : run);
	let startedAt = $derived(typeof timingRun?.started_at === 'number' ? timingRun.started_at : null);
	let endedAt = $derived(typeof timingRun?.ended_at === 'number' ? timingRun.ended_at : null);
	$effect(() => {
		if (typeof window !== 'undefined') {
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
	});
	// Running -> ticks off `nowTs`; terminal -> frozen at `ended_at`. A terminal
	// run with no `ended_at` (only possible via a hard crash mid-flight) yields
	// no duration rather than a runaway clock. Clamp ≥ 0 for sub-second skew
	// between the server's time.time() and the client's Date.now().
	let elapsedSeconds = $derived(
		startedAt == null
			? null
			: status === 'running'
				? Math.max(0, nowTs - startedAt)
				: endedAt != null
					? Math.max(0, endedAt - startedAt)
					: null
	);
	let timerText = $derived(
		elapsedSeconds == null
			? ''
			: formatDurationLong(elapsedSeconds, (key, options) => $i18n.t(key, options))
	);
	// Header line. Every state — running included — reads as one duration phrase
	// ("Researching for 1 minute 23 seconds"); the colored icon conveys which
	// state it is. When timing is unavailable (legacy runs persisted before
	// timing was stamped) we still say "Researched" — NEVER a bare "Done", which
	// reads as a different state from the "Researched for …" cards and is exactly
	// the inconsistency the user reported. New runs always carry both timestamps
	// (the backend stamps them atomically), so they always get the duration.
	let headerStatusText = $derived(
		status === 'running'
			? timerText
				? $i18n.t('Researching for {{duration}}', { duration: timerText })
				: $i18n.t('Researching…')
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
						: $i18n.t('Error')
	);
	let runContentBlocks = $derived(
		(stale ? effectiveRun?.content_blocks : run?.content_blocks) as any[] | null | undefined
	);
	// When a run (re)enters the live running state, drop any cached fallback
	// text/error from a prior terminal view of this same block so the body
	// doesn't keep showing the old result while the rerun streams.
	$effect(() => {
		if (run?.live && status === 'running' && (fallbackContent || fallbackError || fallbackBlocks)) {
			fallbackContent = '';
			fallbackError = '';
			fallbackBlocks = null;
			fallbackFetching = false;
		}
	});
	let fallbackIsRicher = $derived(
		Array.isArray(fallbackBlocks) &&
			fallbackBlocks.length > 0 &&
			(!Array.isArray(runContentBlocks) ||
				runContentBlocks.length === 0 ||
				(!hasToolCallBlocks(runContentBlocks) && hasToolCallBlocks(fallbackBlocks)) ||
				fallbackBlocks.length > runContentBlocks.length)
	);
	let contentBlocks = $derived(
		(open
			? stale
				? ((fallbackIsRicher ? fallbackBlocks : runContentBlocks) ?? [])
				: ((fallbackIsRicher
						? fallbackBlocks
						: Array.isArray(runContentBlocks) && runContentBlocks.length
							? runContentBlocks
							: fallbackBlocks) ?? [])
			: []) as any[]
	);
	let hasContent = $derived(contentBlocks.length > 0);
	let doneRendering = $derived(status !== 'running');
	// Final answer text to show when there are no structured blocks to render.
	// Stale cards mirror their launch sibling's answer.
	let displayFinalText = $derived((stale ? effectiveRun?.final_text : run?.final_text) || '');
	// Index of the subagent's FINAL answer text block (the last non-empty text
	// block, only once the run is terminal). The final answer is the single most
	// important thing a user opens the card to see, but it is the LAST block in a
	// possibly-huge transcript — the block window below renders from the START, so
	// without special handling the answer is hidden behind slow progressive
	// hydration (or entirely, if the main thread never goes idle). We therefore
	// pull it OUT of the windowed transcript and render it as a dedicated,
	// always-visible section. While running, the trailing text is still streaming
	// and stays inline in the transcript (idx = -1).
	let finalTextIdx = $derived(
		doneRendering
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
			: -1
	);
	// Transcript = the work (reasoning + tool calls + any interstitial text),
	// excluding the trailing final-answer block which is rendered separately.
	let transcriptBlocks = $derived(
		doneRendering && finalTextIdx >= 0
			? contentBlocks.filter((_, i) => i !== finalTextIdx)
			: contentBlocks
	);
	let hasTranscript = $derived(transcriptBlocks.length > 0);
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
	let finalAnswerText = $derived(
		doneRendering
			? finalTextIdx >= 0
				? `${contentBlocks[finalTextIdx].content ?? ''}`
				: displayFinalText || (hasTranscript ? '' : fallbackContent) || ''
			: ''
	);
	// Render the whole transcript at once — it is cheap (see above), so there is
	// no window to grow and nothing to "load later".
	let visibleContentBlocks = $derived(transcriptBlocks);
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
	let hasRunContentBlocks = $derived(
		Array.isArray(runContentBlocks) && runContentBlocks.length > 0
	);
	let hasRunToolCallBlocks = $derived(hasToolCallBlocks(runContentBlocks));
	$effect(() => {
		if (
			open &&
			status !== 'running' &&
			subagentChatId &&
			(!hasRunContentBlocks || !hasRunToolCallBlocks) &&
			!fallbackBlocks &&
			!fallbackFetching &&
			!fallbackError
		) {
			fetchFallbackContent();
		}
	});
	// All non-continuation subagents that ran in the SAME parent turn as this one —
	// the candidates for "redo several in parallel". Derived from the live store,
	// de-duped by entry key (the store seeds each run under several alias keys).
	let turnSubagents = $derived(
		branchActionPrompt
			? (() => {
					const states = $subagentLiveStates as Record<string, any>;
					const parentId = resolvedParentMessageId;
					const byKey = new Map<string, any>();
					for (const v of Object.values(states)) {
						if (!v || typeof v !== 'object') continue;
						if (parentId && v.parent_message_id !== parentId) continue;
						if (v.continuation === true) continue;
						const key = v.entry_key || v.subagent_id || v.chat_id || v.tool_call_id;
						if (!key || byKey.has(key)) continue;
						byKey.set(key, {
							entryKey: key,
							toolCallId: v.tool_call_id || '',
							subagentId: v.subagent_id || v.chat_id || '',
							label: v.name || (v.num != null ? `Subagent ${v.num}` : 'Subagent'),
							num: v.num ?? 9999
						});
					}
					const clickedKey = run?.entry_key || stateKey;
					if (clickedKey && !byKey.has(clickedKey)) {
						byKey.set(clickedKey, {
							entryKey: clickedKey,
							toolCallId: run?.tool_call_id || attrToolCallId || '',
							subagentId: run?.subagent_id || run?.chat_id || attrSubagentId || '',
							label: run?.name || 'Subagent',
							num: run?.num ?? 9999
						});
					}
					return [...byKey.values()].sort((a, b) => (a.num || 0) - (b.num || 0));
				})()
			: []
	);
	let isRunningRerun = $derived(
		status === 'running' &&
			(run?.rerun === true || run?.detached_rerun === true || !!run?.rerun_task_id)
	);
</script>

<div
	class="my-2 rounded-2xl border-hairline border-gray-200 dark:border-gray-800 bg-gray-50/60 dark:bg-gray-850/40 subagent-block"
	data-tool-call-id={attributes?.tool_call_id ?? ''}
	data-subagent-id={attributes?.id ?? ''}
	bind:this={rootEl}
>
	<div class="flex items-center gap-2 px-3 py-2">
		<button
			type="button"
			class="flex-1 flex items-center gap-2 text-left hover:opacity-90 transition-opacity py-2 -my-2 max-md:min-h-[44px]"
			data-anchor-on-click
			onclick={preventDefault(toggle)}
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
						class="size-4 text-success dark:text-success-dark"
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
						class="size-4 text-error-brick dark:text-error-brick-dark"
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
				<span
					class="shrink-0 whitespace-nowrap text-sm font-medium text-gray-900 dark:text-gray-100"
				>
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
					? 'text-success dark:text-success-dark'
					: status === 'error'
						? 'text-error-brick dark:text-error-brick-dark'
						: status === 'cancelled'
							? 'text-gray-500'
							: 'text-book-cloth dark:text-kraft'}"
			>
				{headerStatusText}
			</span>
			<!-- No separate elapsed chip while running: the header now carries the
			     duration in prose ("Researching for 1 minute 23 seconds"), exactly
			     like every terminal state, so a chip would just repeat it. -->

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

		{#if isRunningRerun}
			<button
				type="button"
				class="tap-target p-1.5 max-md:p-2.5 rounded-md text-gray-500 hover:text-error-brick dark:hover:text-error-brick-dark hover:bg-gray-100 dark:hover:bg-gray-800 transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
				onclick={triggerStopRerun}
				disabled={stopBusy}
				aria-label={$i18n.t('Stop subagent redo')}
				title={$i18n.t('Stop subagent redo')}
			>
				{#if stopBusy}
					<Spinner className="size-4" />
				{:else}
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
						<rect x="6" y="6" width="12" height="12" rx="1.5" />
					</svg>
				{/if}
			</button>
		{/if}

		<!-- Redo dropdown (sibling of the header toggle, not inside it, so the
			click doesn't bubble up to the collapsible toggle). -->
		<div class="relative shrink-0" bind:this={redoMenuContainer}>
			<button
				type="button"
				class="tap-target p-1.5 max-md:p-2.5 rounded-md text-gray-500 hover:text-gray-900 dark:hover:text-gray-100 hover:bg-gray-100 dark:hover:bg-gray-800 transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
				onclick={toggleRedoMenu}
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
					class="absolute right-0 top-full mt-1 z-10 min-w-[200px] rounded-lg border-hairline border-gray-200 dark:border-gray-800 bg-white dark:bg-gray-900 shadow-lg overflow-hidden"
					transition:slide={{ duration: 120, axis: 'y' }}
				>
					<button
						type="button"
						class="w-full text-left px-3 py-2 text-sm text-gray-900 dark:text-gray-100 hover:bg-gray-100 dark:hover:bg-gray-800 flex flex-col gap-0.5"
						onclick={preventDefault(() => triggerRerun('this_turn'))}
					>
						<span class="font-medium">{$i18n.t('Redo last subagent turn')}</span>
						<span class="text-xs text-gray-500 dark:text-gray-400">
							{$i18n.t('Re-send the same request; keep prior turns intact.')}
						</span>
					</button>
					<div class="h-px bg-gray-100 dark:bg-gray-800"></div>
					<button
						type="button"
						class="w-full text-left px-3 py-2 text-sm text-gray-900 dark:text-gray-100 hover:bg-gray-100 dark:hover:bg-gray-800 flex flex-col gap-0.5"
						onclick={preventDefault(() => triggerRerun('from_launch'))}
					>
						<span class="font-medium">{$i18n.t('Redo subagent')}</span>
						<span class="text-xs text-gray-500 dark:text-gray-400">
							{$i18n.t("Discard the subagent's work; restart from the original prompt.")}
						</span>
					</button>
					<div class="h-px bg-gray-100 dark:bg-gray-800"></div>
					<button
						type="button"
						class="w-full text-left px-3 py-2 text-sm text-gray-900 dark:text-gray-100 hover:bg-gray-100 dark:bg-gray-900 dark:hover:bg-gray-800 flex flex-col gap-0.5"
						onclick={preventDefault(triggerAdopt)}
					>
						<span class="font-medium">{$i18n.t('Use latest answer from full chat')}</span>
						<span class="text-xs text-gray-500 dark:text-gray-400">
							{$i18n.t('Import the selected completed branch and resume the main agent.')}
						</span>
					</button>
				</div>
			{/if}
		</div>
	</div>

	{#if branchActionPrompt}
		<div class="mx-3 mb-2 rounded-xl border-hairline border-warning/25 bg-warning/10 px-3 py-2.5">
			<div class="text-sm font-medium text-warning dark:text-warning-dark">
				{branchActionPrompt.action === 'adopt'
					? $i18n.t('Use repaired subagent answers on a new branch.')
					: $i18n.t('The main agent already continued after this subagent.')}
			</div>
			<div class="mt-1 text-xs leading-relaxed text-warning/90 dark:text-warning-dark/80">
				{#if branchActionPrompt.action === 'adopt' && turnSubagents.length > 1}
					{$i18n.t(
						'Choose every repaired subagent answer to use. One atomic branch adopts the selection together, keeps the current work on its original branch, and then resumes the main agent.'
					)}
				{:else if branchActionPrompt.action === 'adopt'}
					{$i18n.t(
						'A new branch adopts this repaired answer, keeps the current work on its original branch, and then resumes the main agent.'
					)}
				{:else if turnSubagents.length > 1}
					{$i18n.t(
						'Redoing in place isn’t possible. Choose which subagents to redo — the main agent’s responses after them are set aside on a new branch, the selected subagents re-run in parallel, then the main agent continues from the new results.'
					)}
				{:else}
					{$i18n.t(
						'Redoing in place isn’t possible, but you can set aside the main agent’s responses after this subagent (kept as a separate branch), redo the subagent, then let the main agent continue from the new result.'
					)}
				{/if}
			</div>

			{#if turnSubagents.length > 1}
				<div class="mt-2 flex flex-col gap-1.5">
					{#each turnSubagents as ts (ts.entryKey)}
						<div class="flex items-center gap-2 text-xs text-warning dark:text-warning-dark">
							<label class="flex min-w-0 flex-1 items-center gap-2 cursor-pointer select-none">
								<input
									type="checkbox"
									class="accent-book-cloth"
									checked={redoSelection.has(ts.entryKey)}
									onchange={() => toggleRedoSelection(ts.entryKey)}
								/>
								<span class="truncate">{ts.label}</span>
							</label>

							{#if redoSelection.has(ts.entryKey) && branchActionPrompt.action === 'rerun'}
								<select
									class="max-w-[150px] rounded-md border-hairline border-warning/40 bg-warning/10 dark:bg-gray-900 px-1.5 py-1 text-xs outline-hidden"
									value={redoScopes[ts.entryKey] ?? 'this_turn'}
									onchange={(e) => onRedoScopeChange(ts.entryKey, e)}
								>
									<option value="this_turn">{$i18n.t('Redo last turn')}</option>
									<option value="from_launch">{$i18n.t('Redo subagent')}</option>
								</select>
							{/if}
						</div>
					{/each}
				</div>
			{/if}

			<div class="mt-2.5 flex items-center gap-2">
				<button
					type="button"
					class="px-2.5 py-1 max-md:py-2 rounded-lg bg-book-cloth hover:bg-kraft text-white text-xs font-medium transition-colors duration-200 ease-paper disabled:opacity-50 disabled:cursor-not-allowed"
					onclick={preventDefault(triggerRewindRedo)}
					disabled={redoBusy || (turnSubagents.length > 1 && redoSelection.size === 0)}
				>
					{#if branchActionPrompt.action === 'adopt'}
						{turnSubagents.length > 1
							? $i18n.t('Use selected on new branch')
							: $i18n.t('Use latest on new branch')}
					{:else}
						{turnSubagents.length > 1
							? $i18n.t('Rewind & redo selected')
							: $i18n.t('Rewind & redo')}
					{/if}
				</button>
				<button
					type="button"
					class="px-2.5 py-1 max-md:py-2 rounded-lg text-xs font-medium text-warning dark:text-warning-dark hover:bg-warning/15 transition-colors"
					onclick={preventDefault(() => (branchActionPrompt = null))}
				>
					{$i18n.t('Cancel')}
				</button>
			</div>
		</div>
	{/if}

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
						class="rounded-lg bg-error-brick/10 border-hairline border-error-brick/20 px-3 py-2 text-sm text-error-brick dark:text-error-brick-dark"
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
											<!-- Same phrasing as the top-level reasoning row in
											     Collapsible.svelte — spelled out, never abbreviated. -->
											{block.duration < 1
												? $i18n.t('Thought for less than a second')
												: block.duration < 60
													? $i18n.t('Thought for {{DURATION}} seconds', {
															DURATION: block.duration
														})
													: $i18n.t('Thought for {{DURATION}}', {
															DURATION: dayjs.duration(block.duration, 'seconds').humanize()
														})}
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
											class="flex items-baseline gap-2 min-w-0 text-xs font-mono {callErrored
												? 'text-error-brick dark:text-error-brick-dark'
												: 'text-gray-600 dark:text-gray-400'}"
										>
											<span class="shrink-0 w-3 text-center">
												{result === undefined ? '·' : callErrored ? '✗' : '✓'}
											</span>
											<span
												class="min-w-0 max-w-[45%] truncate whitespace-nowrap font-medium {callErrored
													? 'text-error-brick dark:text-error-brick-dark'
													: 'text-gray-800 dark:text-gray-300'}"
											>
												{call?.function?.name ?? 'tool'}
											</span>
											<span
												class="min-w-0 truncate {callErrored
													? 'text-error-brick/80 dark:text-error-brick-dark/80'
													: 'text-gray-500 dark:text-gray-500'}"
											>
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
					<div
						class="markdown-prose"
						class:mt-3={hasTranscript}
						class:pt-3={hasTranscript}
						class:border-t-hairline={hasTranscript}
						class:border-gray-100={hasTranscript}
						class:dark:border-gray-800={hasTranscript}
					>
						<Markdown
							id={`subagent-${stateKey}-final`}
							content={finalAnswerText}
							done={true}
							editCodeBlock={false}
							{sandboxFiles}
						/>
					</div>
				{:else if !hasTranscript && fallbackContent}
					<div class="markdown-prose">
						<Markdown
							id={`subagent-${stateKey}-fallback`}
							content={fallbackContent}
							done={true}
							editCodeBlock={false}
							{sandboxFiles}
						/>
					</div>
				{:else if !hasTranscript && fallbackFetching}
					<div class="text-sm text-gray-500 dark:text-gray-400 italic">
						{$i18n.t('Loading subagent results…')}
					</div>
				{:else if !hasTranscript && fallbackError}
					<div class="text-sm text-error-brick dark:text-error-brick-dark italic">
						{fallbackError}
					</div>
				{:else if !hasTranscript && status === 'running'}
					<!-- The run is live (status seeded by chat:subagent:start). Its
					     inner provider runs non-streaming by default, so the first
					     content-bearing update only lands when the first round
					     finishes (which can take minutes) — until then there is no
					     transcript yet. Do NOT imply the agent hasn't begun ("starting
					     up…"): it IS working, exactly as the header's "Researching…"
					     timer shows. Surface that instead so a long first round reads
					     as progress, not a stall. -->
					<div class="text-sm text-gray-500 dark:text-gray-400 italic">
						{$i18n.t('Researching… output will appear here as the subagent works.')}
					</div>
				{:else if !hasTranscript && status === 'error'}
					<div class="text-sm text-error-brick dark:text-error-brick-dark italic">
						{$i18n.t('Subagent did not produce any output.')}
					</div>
				{/if}

				{#if subagentChatId}
					<div class="mt-3 pt-2 border-t-hairline border-gray-100 dark:border-gray-800">
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
