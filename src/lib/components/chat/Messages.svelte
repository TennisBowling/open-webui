<script lang="ts">
	import { v4 as uuidv4 } from 'uuid';
	import { config, settings, user as _user, mobile, temporaryChatEnabled } from '$lib/stores';
	import { tick, getContext, onMount, onDestroy } from 'svelte';
	import { toast } from '$lib/utils/toast';
	import {
		getChatMessagesBranch,
		getChatMessagesSiblings,
		patchChat,
		type PatchChatOp
	} from '$lib/apis/chats';
	import { copyToClipboard, extractCurlyBraceWords } from '$lib/utils';
	import { computeChainStructureKey } from '$lib/utils/chainStructureKey';
	import {
		buildHistoryChildrenIndex,
		findDeepestBranchLeaf,
		getOrderedChildIds
	} from '$lib/utils/chatHistoryGraph';

	import Message from './Messages/Message.svelte';
	import Loader from '../common/Loader.svelte';
	import Spinner from '../common/Spinner.svelte';

	import ChatPlaceholder from './ChatPlaceholder.svelte';

	const i18n = getContext('i18n');

	// Bumped by the parent (Chat.svelte) when the message graph changes shape
	// (send/delete/branch/load/reattach) but NOT on per-frame streaming content
	// flushes. Combined with a local structural fingerprint below, this gates the

	// Local structural revision: bumped by THIS component's own graph-shape
	// mutations (pagination/stub hydration/branch navigation). Kept separate from
	// the parent's so neither has to know about the other's mutation sites.
	let localStructureRevision = $state(0);

	// False during the parent's initial settle phase, when the content is hidden
	// and the parent's settle loop owns scroll position. Prevents this component's

	interface Props {
		className?: string;
		chatId?: string;
		user?: any;
		messages?: any[];
		prompt?: any;
		history?: any;
		selectedModels?: any;
		atSelectedModel?: any;
		// expensive chain walk so streaming deltas don't re-render the whole list.
		structureRevision?: number;
		setInputText?: Function;
		sendMessage: Function;
		prepareBranchReplacement?: Function;
		continueResponse: Function;
		regenerateResponse: Function;
		rewindAndInsert?: Function;
		retryWithoutProviderRestrictions?: Function;
		markSkipRemainingRetries?: Function;
		regenerateWithModel?: Function;
		mergeResponses?: Function;
		chatActionHandler?: Function;
		showMessage?: Function;
		submitMessage?: Function;
		readOnly?: boolean;
		editCodeBlock?: boolean;
		topPadding?: boolean;
		bottomPadding?: boolean;
		widescreen?: boolean | null;
		autoScroll: any;
		allowPagination?: boolean;
		// own scroll driver from fighting the settle loop on chat open.
		scrollReady?: boolean;
		onSelect?: any;
		messagesCount?: number | null;
	}

	let {
		className = 'h-full flex pt-8',
		chatId = '',
		user: providedUser = undefined,
		messages = $bindable([]),
		prompt = $bindable(''),
		history = $bindable({}),
		selectedModels = [],
		atSelectedModel = null,
		structureRevision = 0,
		setInputText = () => {},
		sendMessage,
		prepareBranchReplacement = async () => true,
		continueResponse,
		regenerateResponse,
		rewindAndInsert = () => {},
		retryWithoutProviderRestrictions = () => {},
		markSkipRemainingRetries = () => {},
		regenerateWithModel = () => {},
		mergeResponses = () => {},
		chatActionHandler = () => {},
		showMessage = () => {},
		submitMessage = () => {},
		readOnly = false,
		editCodeBlock = false,
		topPadding = false,
		bottomPadding = false,
		widescreen = null,
		autoScroll = $bindable(),
		allowPagination = true,
		scrollReady = true,
		onSelect = (e) => {},
		messagesCount = $bindable(25)
	}: Props = $props();
	let messagesLoading = $state(false);
	// Throttles the pagination Loader: it dispatches `visible` every ~100ms while
	// intersecting, which on a fast scroll-up used to storm advanceFrontier and
	// stack restores. We require a short cooldown between loads so one upward
	// gesture triggers at most a few pages, not a burst.
	let lastPaginationAt = $state(0);
	const PAGINATION_COOLDOWN_MS = 250;
	let paginationChatId = $state('');
	// Tracks the most recent outcome per anchor message id. 'exhausted' is
	// sticky (no more older rows on the server); 'error' is transient (a
	// retry can clear it). Replaces a single `Set<exhaustedBeforeIds>` whose
	// latch trapped users whenever a transient fetch error was indistinguishable
	// from a genuinely empty page. 'offline' is a distinct transient state for
	// a pagination fetch that failed while the device is known offline — same
	// recovery semantics as 'error' (a retry can clear it) but rendered with
	// different copy, and auto-cleared on the browser's `online` event instead
	// of requiring a manual retry click.
	type AnchorState = 'exhausted' | 'error' | 'offline';
	let anchorStates: Map<string, AnchorState> = $state(new Map());

	// Initial render window and per-page hydration size. 7 was too small —
	// on most chats the Loader fires on open before the user can scroll,
	// compounding every other pagination bug. 25 covers a typical "what was
	// I just saying" scrollback without ceremony.
	const MESSAGE_PAGE_SIZE = 25;

	// Discriminated state describing why the display walk stopped. The Loader
	// renders for every kind except `complete`, and routes its action by kind.
	// Before this existed the walk silently terminated and the Loader's gate
	// was the inverse of the very condition the walk had just proved true,
	// trapping users with no recovery affordance.
	//
	// `anchorId` keys per-frontier state in `anchorStates` (so a transient
	// error on one stub doesn't poison adjacent ones). `beforeId` for the
	// stub variant is the youngest *hydrated* ancestor — what the backend
	// `before=` pagination semantic uses; using the stub itself would exclude
	// it from the returned chain and leave it forever a stub.
	type ChainFrontier =
		| { kind: 'complete' }
		| { kind: 'capped' }
		| { kind: 'stub'; anchorId: string; beforeId: string | null }
		| { kind: 'orphan'; missingId: string; anchorId: string }
		| { kind: 'error'; anchorId: string; beforeId: string | null; offline?: boolean };
	let frontier: ChainFrontier = $state({ kind: 'complete' });

	// Auto-recover 'offline'-flagged pagination failures once connectivity
	// returns, without requiring the user to manually retry. Only clears
	// 'offline' anchors — 'error' (a real server/network error while online)
	// and 'exhausted' (sticky, genuinely no more rows) are left alone.
	const handleOnlineRestore = () => {
		let changed = false;
		for (const [id, state] of anchorStates) {
			if (state === 'offline') {
				anchorStates.delete(id);
				changed = true;
			}
		}
		if (changed) {
			anchorStates = new Map(anchorStates);
		}
	};

	onMount(() => {
		window.addEventListener('online', handleOnlineRestore);
	});

	onDestroy(() => {
		window.removeEventListener('online', handleOnlineRestore);
	});

	// Merge a paginated message page back into history.messages, preserving any
	// child stubs that were seeded from sibling_stubs and not part of this page.
	// GET /chats/{id}/messages returns a raw array; accept {messages: [...]} too
	// for resilience. Returns the count of stubs/missing entries that were
	// hydrated into real messages.
	const mergePaginatedMessages = (page: any): number => {
		const incoming = Array.isArray(page)
			? page
			: Array.isArray(page?.messages)
				? page.messages
				: [];
		if (!incoming.length) return 0;
		const h: any = history;
		let hydrated = 0;
		for (const msg of incoming) {
			if (!msg?.id) continue;
			const existing = h.messages?.[msg.id];
			const wasStubOrMissing = !existing || existing._stub;
			h.messages[msg.id] = { ...(existing ?? {}), ...msg, _stub: false };
			if (wasStubOrMissing) hydrated += 1;
		}
		return hydrated;
	};

	// Hydrate the branch ending at `leafId` if any ancestor is still a stub
	// OR truly missing from the map (orphan: parentId set but no entry).
	// Used by branch-switch handlers when the user picks a sibling that
	// wasn't included in the initial loadChat page.
	const hydrateBranchIfNeeded = async (leafId: string | null) => {
		const h: any = history;
		if (!leafId || !chatId || !h?.messages) return;
		let cursor = h.messages[leafId];
		let needsFetch = false;
		while (cursor) {
			if (cursor._stub) {
				needsFetch = true;
				break;
			}
			if (cursor.parentId === null || cursor.parentId === undefined) break;
			const parent = h.messages[cursor.parentId];
			if (!parent) {
				// Orphan chain — parent should exist but doesn't. Previously
				// this loop exited silently (cursor became undefined, _stub
				// check never ran) so branch switches into orphan-chained
				// leaves rendered truncated with no recovery.
				needsFetch = true;
				break;
			}
			cursor = parent;
		}
		if (!needsFetch) return;
		const page = await getChatMessagesBranch(localStorage.token, chatId, {
			leaf: leafId,
			limit: MESSAGE_PAGE_SIZE
		}).catch(() => null);
		if (mergePaginatedMessages(page) > 0) {
			history = history;
			localStructureRevision += 1;
			await tick();
		}
	};

	// Advance the chain by one step, routing on the current frontier kind.
	// Replaces the old `loadMoreMessages` whose single code path conflated
	// "stub ahead" with "older messages exist beyond render window," and
	// whose error handling (`.catch(() => null)`) collapsed network failures
	// into the same "no more rows" outcome as a genuinely empty page.
	const advanceFrontier = async (target: ChainFrontier) => {
		if (messagesLoading) return;
		if (target.kind === 'complete') return;

		// Prepend layout shifts (the new rows growing the content above the
		// reader's viewport, plus their content-visibility realization landing
		// over the following frames) are absorbed by Chat.svelte's
		// scroll-anchoring engine: it tracks an anchor message's offset within
		// the scroll CONTENT and corrects residuals in the ResizeObserver
		// callback — after layout, BEFORE paint — so the shift is never painted
		// and an active fling is never undone (the metric is invariant to
		// scrolling). No per-mutation capture/restore is needed here anymore;
		// a second corrector would double-correct against the engine.
		messagesLoading = true;
		try {
			if (target.kind === 'capped') {
				// All loaded messages already in the map; just grow the window.
				if (messagesCount !== null) {
					messagesCount += MESSAGE_PAGE_SIZE;
				}
				await tick();
				return;
			}

			const leafId = (history as any)?.currentId ?? null;
			if (!chatId || !leafId) return;

			if (target.kind === 'orphan') {
				// Fill the gap: fetch from the missing id so the response
				// includes it and its ancestors (stitching the gap closed).
				// Walk will re-converge on the next reactive pass. Fetch FIRST
				// (no DOM impact), capture the anchor, then apply.
				let page: any = null;
				let fetchFailed = false;
				try {
					page = await getChatMessagesBranch(localStorage.token, chatId, {
						leaf: target.missingId,
						limit: MESSAGE_PAGE_SIZE
					});
				} catch (err) {
					fetchFailed = true;
				}
				if (fetchFailed) {
					const isOffline = typeof navigator !== 'undefined' && navigator.onLine === false;
					anchorStates.set(target.anchorId, isOffline ? 'offline' : 'error');
					anchorStates = new Map(anchorStates);
				} else {
					const hydrated = mergePaginatedMessages(page);
					if (hydrated > 0) {
						history = history;
						localStructureRevision += 1;
						if (messagesCount !== null) messagesCount += hydrated;
						// Anchor recovered — clear any stale error state.
						if (anchorStates.has(target.anchorId)) {
							anchorStates.delete(target.anchorId);
							anchorStates = new Map(anchorStates);
						}
					} else {
						// Server has no row for this id — graph is inconsistent,
						// nothing more we can do client-side. Mark exhausted so
						// we stop trying. Backend repair (_normalize_message_graph)
						// should prevent this from persisting.
						anchorStates.set(target.anchorId, 'exhausted');
						anchorStates = new Map(anchorStates);
					}
				}
				await tick();
				return;
			}

			// target.kind === 'stub' or 'error' → page ancestors of the anchor.
			//
			// `beforeId` is the youngest hydrated message; the backend treats
			// `before` as exclusive, so we page strictly older than that. When
			// the leaf itself is a stub (no hydrated message has been pushed),
			// `beforeId` is null and we instead hydrate by `leaf=frontierAnchorId`,
			// which fetches the anchor + its ancestors. (Named distinctly from the
			// outer scroll `anchorId` — this is the backend pagination frontier id,
			// NOT the viewport scroll anchor.)
			const frontierAnchorId = target.anchorId;
			const beforeId = target.beforeId;
			let hydratedCount = 0;
			let page: any = null;
			let fetchFailed = false;
			try {
				page = beforeId
					? await getChatMessagesBranch(localStorage.token, chatId, {
							leaf: leafId,
							before: beforeId,
							limit: MESSAGE_PAGE_SIZE
						})
					: await getChatMessagesBranch(localStorage.token, chatId, {
							leaf: frontierAnchorId,
							limit: MESSAGE_PAGE_SIZE
						});
			} catch (err) {
				fetchFailed = true;
			}
			if (fetchFailed) {
				// Network/server error. Don't mark exhausted — a retry can
				// recover. The frontier reactive walk will see the 'error'
				// (or 'offline') state on the next pass and the Loader will
				// render an inline retry affordance (or auto-clear on
				// reconnect for 'offline').
				const isOffline = typeof navigator !== 'undefined' && navigator.onLine === false;
				anchorStates.set(frontierAnchorId, isOffline ? 'offline' : 'error');
				anchorStates = new Map(anchorStates);
			} else {
				hydratedCount = mergePaginatedMessages(page);
				if (hydratedCount > 0) {
					history = history;
					localStructureRevision += 1;
					if (anchorStates.has(frontierAnchorId)) {
						anchorStates.delete(frontierAnchorId);
						anchorStates = new Map(anchorStates);
					}
				} else {
					// Genuine empty page — nothing older on this branch.
					anchorStates.set(frontierAnchorId, 'exhausted');
					anchorStates = new Map(anchorStates);
				}
			}

			if (messagesCount !== null) {
				messagesCount += hydratedCount;
			}
			await tick();
		} finally {
			messagesLoading = false;
		}
	};

	const rebuildRenderedChain = () => {
		// Compute both the rendered list and its frontier in a single pass.
		// The frontier carries the reason the walk stopped — `complete`
		// (root), `capped` (hit messagesCount), `stub` (next ancestor is an
		// unhydrated placeholder), `orphan` (parentId set but no row), or
		// `error` (last advance failed for this anchor). Loader visibility
		// + action are driven entirely by this state.
		if (!history?.currentId) {
			messages = [];
			frontier = { kind: 'complete' };
		} else {
			const _messages: any[] = [];
			let message = history.messages[history.currentId];
			let newFrontier: ChainFrontier = { kind: 'complete' };
			// Track the most-recently-pushed hydrated message id so a `stub`
			// frontier can hand the backend a valid `before=` anchor — the
			// stub's own id would be excluded by the backend's exclusive-
			// before semantic, leaving it as a stub forever.
			let lastHydratedId: string | null = null;
			while (message) {
				if (message._stub) {
					newFrontier = {
						kind: 'stub',
						anchorId: message.id,
						beforeId: lastHydratedId
					};
					break;
				}
				if (messagesCount !== null && _messages.length >= messagesCount) {
					newFrontier = { kind: 'capped' };
					break;
				}
				_messages.unshift(message);
				lastHydratedId = message.id;
				if (message.parentId === null || message.parentId === undefined) {
					newFrontier = { kind: 'complete' };
					break;
				}
				const parent = history.messages[message.parentId];
				if (!parent) {
					newFrontier = {
						kind: 'orphan',
						missingId: message.parentId,
						anchorId: message.id
					};
					break;
				}
				message = parent;
			}

			// Promote per-anchor state into the frontier so the Loader can
			// react. 'exhausted' on the current anchor means "no recovery
			// available" → treat as complete (don't render the Loader).
			// 'error' overrides everything else for that anchor.
			const anchorId =
				newFrontier.kind === 'stub' || newFrontier.kind === 'orphan'
					? newFrontier.anchorId
					: _messages.length
						? _messages[0].id
						: null;
			if (anchorId) {
				const state = anchorStates.get(anchorId);
				if (state === 'error' || state === 'offline') {
					// Preserve the beforeId from the stub variant if that's
					// what failed; otherwise this is an error against the
					// oldest rendered id.
					const beforeId =
						newFrontier.kind === 'stub'
							? newFrontier.beforeId
							: _messages.length
								? _messages[0].id
								: null;
					newFrontier = { kind: 'error', anchorId, beforeId, offline: state === 'offline' };
				} else if (state === 'exhausted' && newFrontier.kind !== 'capped') {
					newFrontier = { kind: 'complete' };
				}
			}

			messages = _messages;
			frontier = newFrontier;
		}
	};

	const scrollToBottom = () => {
		const element = document.getElementById('messages-container');
		element.scrollTop = element.scrollHeight;
	};

	const updateChat = async (ops: PatchChatOp | PatchChatOp[] | undefined = undefined) => {
		if ($temporaryChatEnabled) {
			return;
		}
		history = history;
		// Not on the streaming hot path (branch nav / code-block edits); bump so
		// the rebased chain walk re-renders if this carried a structural change.
		localStructureRevision += 1;
		await tick();

		const opList = ops === undefined ? [] : Array.isArray(ops) ? ops : [ops];
		if (opList.length === 0) {
			// Legacy fallback for callers (MultiResponseMessages branch nav,
			// ResponseMessage code-block edits) that haven't yet been migrated to
			// pass explicit ops. Sync the current-branch pointer — covers branch
			// navigation cleanly; content edits from such callers won't persist
			// through this path and need their own PATCH migration in a later
			// unit.
			if (history?.currentId) {
				opList.push({ op: 'set_history_current_id', current_id: history.currentId });
			} else {
				return;
			}
		}

		await patchChat(localStorage.token, chatId, opList);
	};

	const activateMessageBranch = async (startMessageId: string | null) => {
		const previousCurrentId = history.currentId;
		const leafId = findDeepestBranchLeaf(history.messages ?? {}, startMessageId);
		if (!leafId) return false;

		await hydrateBranchIfNeeded(leafId);
		history.currentId = leafId;
		if (leafId !== previousCurrentId) {
			await updateChat({ op: 'set_history_current_id', current_id: leafId });
		}
		await tick();

		if ($settings?.scrollOnBranchChange ?? true) {
			setTimeout(() => {
				if (autoScroll) scrollToBottom();
			}, 100);
		}
		return true;
	};

	const siblingIdsFor = (message: any) =>
		message?.parentId != null
			? getOrderedChildIds(history.messages ?? {}, message.parentId)
			: Object.values(history.messages)
					.filter((candidate: any) => candidate?.parentId == null)
					.map((candidate: any) => candidate.id);

	const gotoMessage = async (message: any, idx: number) => {
		const siblings = siblingIdsFor(message);
		if (siblings.length === 0) return;
		const targetIndex = Math.max(0, Math.min(Number(idx) || 0, siblings.length - 1));
		await activateMessageBranch(siblings[targetIndex]);
	};

	const showPreviousMessage = async (message: any) => {
		const siblings = siblingIdsFor(message);
		if (siblings.length === 0) return;
		const targetIndex = Math.max(0, siblings.indexOf(message.id) - 1);
		await activateMessageBranch(siblings[targetIndex]);
	};

	const showNextMessage = async (message: any) => {
		const siblings = siblingIdsFor(message);
		if (siblings.length === 0) return;
		const targetIndex = Math.min(siblings.length - 1, siblings.indexOf(message.id) + 1);
		await activateMessageBranch(siblings[targetIndex]);
	};

	const rateMessage = async (messageId, rating) => {
		history.messages[messageId].annotation = {
			...history.messages[messageId].annotation,
			rating: rating
		};

		await updateChat({
			op: 'set_message_annotation',
			message_id: messageId,
			annotation: history.messages[messageId].annotation
		});
	};

	const editMessage = async (
		messageId: string,
		{ content, files }: { content: any; files?: any[] },
		submit = true
	) => {
		const originalMessage = history.messages[messageId];
		if (!originalMessage) return false;
		const versionMessageId = uuidv4();
		const selectedVersionModels =
			(selectedModels ?? []).filter((id: any) => id).length > 0
				? selectedModels
				: (originalMessage.models ?? []);

		if (originalMessage.role === 'user') {
			if (submit && (selectedModels ?? []).filter((id: any) => id).length === 0) {
				toast.error($i18n.t('Model not selected'));
				return false;
			}
			// Both Save and Send create a sibling prompt version. Save selects the
			// new prompt without generating; Send additionally creates a response.
			// The old in-place update permanently erased the previous prompt and its
			// provenance even though its response branch still depended on it.
			if (submit && !(await prepareBranchReplacement())) return false;
		}

		let versionMessage: any;
		if (!$temporaryChatEnabled && chatId && !chatId.startsWith('local:')) {
			try {
				const result = await patchChat(localStorage.token, chatId, [
					{
						op: 'fork_message_version',
						message_id: versionMessageId,
						source_message_id: messageId,
						content,
						...(originalMessage.role === 'user' && files !== undefined ? { files } : {}),
						...(originalMessage.role === 'user' ? { models: selectedVersionModels } : {})
					}
				]);
				versionMessage = result?.message;
			} catch (error: any) {
				toast.error(error?.detail?.message ?? error?.detail ?? `${error}`);
				return false;
			}
		}

		// Temporary chats have no durable server graph. Persistent chats use the
		// canonical message returned by the atomic fork operation above; the
		// component only mirrors that committed result into its render state.
		if (!versionMessage) {
			versionMessage = {
				id: versionMessageId,
				parentId: originalMessage.parentId ?? null,
				childrenIds: [],
				role: originalMessage.role,
				content,
				...(originalMessage.role === 'user' && files !== undefined ? { files } : {}),
				...(originalMessage.role === 'user'
					? { models: selectedVersionModels }
					: {
							model: originalMessage.model,
							modelName: originalMessage.modelName,
							modelIdx: originalMessage.modelIdx,
							done: true
						}),
				sourceMessageId: messageId,
				manuallyEdited: true,
				timestamp: Math.floor(Date.now() / 1000)
			};
		}

		history.messages[versionMessage.id] = versionMessage;
		const parentId = versionMessage.parentId ?? null;
		if (parentId && history.messages[parentId]) {
			const children = history.messages[parentId].childrenIds ?? [];
			if (!children.includes(versionMessage.id)) {
				history.messages[parentId].childrenIds = [...children, versionMessage.id];
			}
		}
		history.currentId = versionMessage.id;
		localStructureRevision += 1;
		await tick();

		if (submit && versionMessage.role === 'user') {
			await sendMessage(history, versionMessage.id, {
				scrollBehavior: 'preserve',
				supersedeActiveTurn: true
			});
		}
		return true;
	};

	const actionMessage = async (actionId, message, event = null) => {
		await chatActionHandler(chatId, actionId, message.model, message.id, event);
	};

	const saveMessage = async (messageId, message) => {
		history.messages[messageId] = message;
		await updateChat({
			op: 'update_message_content',
			message_id: messageId,
			content: message?.content,
			...(message?.files !== undefined ? { files: message.files } : {})
		});
	};

	const deleteMessage = async (messageId) => {
		// Deleting a message changes durable ancestry. Quiesce the current
		// generation before applying the optimistic graph mutation so the UI does
		// not briefly remove rows that an acknowledged live writer still owns. The
		// backend independently enforces the same barrier and performs the relink in
		// one DB transaction; this preflight keeps the originating tab converged.
		if (!(await prepareBranchReplacement())) return;

		const messageToDelete = history.messages[messageId];
		const parentMessageId = messageToDelete.parentId;
		const childrenIndex = buildHistoryChildrenIndex(history.messages ?? {});
		const childMessageIds = getOrderedChildIds(history.messages ?? {}, messageId, childrenIndex);

		// Preserve the user's viewport across the delete. Deleting a message must
		// NOT jump the view (the old code scroll-into-view'd the parent). We anchor
		// to the topmost message element that is currently visible AND survives the
		// delete, measured against the scroll container, then restore its on-screen
		// position after the DOM settles. (#messages-container's overflow-anchor is
		// dynamic — none while pinned, auto while reading — but measuring the
		// anchor's ACTUAL position makes this restore a no-op wherever the browser
		// already self-corrected, so it composes either way; Safari has no native
		// anchoring and relies on this entirely.)
		const container = document.getElementById('messages-container');
		const deletedSet = new Set([messageId, ...childMessageIds]);
		const prevScrollTop = container?.scrollTop ?? 0;
		const prevScrollHeight = container?.scrollHeight ?? 0;
		let anchorId: string | null = null;
		let anchorTopBefore = 0;
		if (container) {
			const containerTop = container.getBoundingClientRect().top;
			for (const m of messages) {
				if (deletedSet.has(m.id)) continue; // skip nodes that won't survive
				const el = document.getElementById(`message-${m.id}`);
				if (!el) continue;
				const rect = el.getBoundingClientRect();
				// First surviving message still intersecting / below the viewport top.
				if (rect.bottom > containerTop + 1) {
					anchorId = m.id;
					anchorTopBefore = rect.top;
					break;
				}
			}
		}

		// Collect all grandchildren
		const grandchildrenIds = childMessageIds.flatMap((childId) =>
			getOrderedChildIds(history.messages ?? {}, childId, childrenIndex)
		);

		// Update grandchildren's parent
		grandchildrenIds.forEach((grandchildId) => {
			if (history.messages[grandchildId]) {
				history.messages[grandchildId].parentId = parentMessageId;
			}
		});

		// Delete the message and its children
		[messageId, ...childMessageIds].forEach((id) => {
			delete history.messages[id];
		});

		await tick();

		// Navigate the branch pointer to the surviving leaf (parent's deepest
		// descendant) and persist it — but WITHOUT scrolling. suppressScroll stops
		// showMessage from yanking the viewport to the parent and from writing
		// autoScroll from raw position; we own the viewport below instead.
		await showMessage({ id: parentMessageId }, false, { suppressScroll: true });

		// Restore the viewport. If the user is following the bottom (autoScroll =
		// gesture-owned intent), stay pinned to the new bottom — deleting the tail
		// while tailing should keep you tailing, and the ResizeObserver would pin
		// anyway. Otherwise hold the prior view by re-measuring the anchor and
		// undoing the layout shift the removed node caused. We branch on the
		// EXISTING autoScroll (never write it from position — the gesture-only
		// invariant).
		if (container) {
			if (autoScroll) {
				container.scrollTop = container.scrollHeight;
			} else if (anchorId) {
				const el = document.getElementById(`message-${anchorId}`);
				if (el) {
					container.scrollTop += el.getBoundingClientRect().top - anchorTopBefore;
				} else {
					container.scrollTop = prevScrollTop + (container.scrollHeight - prevScrollHeight);
				}
			} else {
				container.scrollTop = prevScrollTop + (container.scrollHeight - prevScrollHeight);
			}
		}

		// Backend's delete_message op handles the parent/grandchild relinking; we
		// already applied the same mutations locally above for optimistic UI.
		await updateChat({ op: 'delete_message', message_id: messageId });
	};

	const triggerScroll = () => {
		// Branch-nav follow-through (multi-response arrows): if the reader was
		// following the bottom, keep following across the swap. Never rewrite
		// autoScroll from raw position — follow intent is gesture-owned (the old
		// position check here silently stopped following whenever the new branch
		// was taller than the viewport allowance, so the arrows "randomly" broke
		// the stream-follow). Matches gotoMessage's behavior.
		if (autoScroll) {
			setTimeout(() => {
				if (autoScroll) scrollToBottom();
			}, 100);
		}
	};
	// Track $_user reactively rather than capturing it once at component init.
	// During the brief window before the user store hydrates, the captured value
	// would be undefined and never update, breaking child role checks.
	let user = $derived(providedUser ?? $_user);
	$effect(() => {
		if (chatId && chatId !== paginationChatId) {
			paginationChatId = chatId;
			messagesCount = MESSAGE_PAGE_SIZE;
			anchorStates = new Map();
			frontier = { kind: 'complete' };
		}
	});
	// Structural key: changes ONLY when the rendered chain could change shape —
	// the parent's structureRevision (send/delete/load/reattach), this
	// component's localStructureRevision (pagination/stub/branch), the current
	// branch pointer, the pagination cap, or the number of messages in the map.
	// It deliberately does NOT depend on message CONTENT, so a streaming token
	// flush (which mutates the leaf's content in place) does not change this key
	// and therefore does not re-run the O(chain-length) walk below.
	let messageMapSize = $derived(history?.messages ? Object.keys(history.messages).length : 0);
	let chainStructureKey = $derived(
		computeChainStructureKey({
			structureRevision,
			localStructureRevision,
			currentId: history?.currentId,
			messagesCount,
			messageMapSize
		})
	);
	// Re-run the walk only when the structural key or the loader anchor states
	// change — NOT on every history reassignment / content flush.
	$effect(() => {
		(chainStructureKey, anchorStates, rebuildRenderedChain());
	});
	$effect(() => {
		if (scrollReady && autoScroll && bottomPadding) {
			(async () => {
				await tick();
				// autoScroll can flip false (user pulled away) between the guard above and
				// this microtask resolving — re-read it so a late file-chip resize can't
				// yank the user back to the bottom.
				if (autoScroll) scrollToBottom();
			})();
		}
	});
</script>

<div class={className}>
	{#if Object.keys(history?.messages ?? {}).length == 0}
		<ChatPlaceholder modelIds={selectedModels} {atSelectedModel} {onSelect} />
	{:else}
		<div class="w-full pt-2">
			<!-- No {#key chatId} wrapper here (removed): rows are already keyed by
			     message.id in the each-block, so switching chats remounts every row
			     anyway (different ids) — the key added nothing there. But it DID
			     remount the whole just-rendered list when a brand-new chat's id
			     resolved '' → real id mid-first-send (draft persist + replaceState),
			     which visibly jiggled/flashed the fresh assistant header (avatar,
			     name) right after sending. Pagination state resets via the
			     chatId !== paginationChatId reactive above. -->
			<section class="w-full" aria-labelledby="chat-conversation">
				<h2 class="sr-only" id="chat-conversation">{$i18n.t('Chat Conversation')}</h2>
				{#if frontier.kind !== 'complete'}
					<Loader
						root={typeof document !== 'undefined'
							? document.getElementById('messages-container')
							: null}
						rootMargin="1200px 0px 0px 0px"
						onvisible={() => {
							if (allowPagination && !messagesLoading && frontier.kind !== 'error') {
								// Throttle the Loader's ~100ms re-fire so a fast scroll-up
								// doesn't storm advanceFrontier and stack scroll restores.
								// The manual 'Retry' button below bypasses this (it calls
								// advanceFrontier directly).
								const now = Date.now();
								if (now - lastPaginationAt < PAGINATION_COOLDOWN_MS) return;
								lastPaginationAt = now;
								advanceFrontier(frontier);
							}
						}}
					>
						{#if frontier.kind === 'error' && frontier.offline}
							<div class="w-full flex justify-center py-1 text-xs items-center gap-2 text-gray-500">
								{$i18n.t('Offline — older messages not available')}
							</div>
						{:else if frontier.kind === 'error'}
							<div class="w-full flex justify-center py-1 text-xs items-center gap-2">
								<button
									type="button"
									class="underline text-error-brick dark:text-error-brick-dark"
									onclick={() => advanceFrontier(frontier)}
								>
									{$i18n.t('Failed to load older messages. Retry')}
								</button>
							</div>
						{:else}
							<div class="w-full flex justify-center py-1 text-xs animate-pulse items-center gap-2">
								<Spinner className=" size-4" />
								<div class=" ">{$i18n.t('Loading...')}</div>
							</div>
						{/if}
					</Loader>
				{/if}
				<!-- id: observed by Chat.svelte's ResizeObserver — this list's box is
				     the only element whose height actually equals the message content
				     (the bound content wrapper is a FIXED-height flex item, so its box
				     never fires for content growth: prepends, content-visibility
				     realization, streaming). The scroll-anchoring engine and the
				     bottom-pin both depend on this signal. -->
				<ul
					id="messages-list"
					role="log"
					aria-live="polite"
					aria-relevant="additions"
					aria-atomic="false"
				>
					{#each messages as message, messageIdx (message.id)}
						<Message
							{chatId}
							bind:history
							{selectedModels}
							messageId={message.id}
							idx={messageIdx}
							{user}
							{setInputText}
							{gotoMessage}
							{activateMessageBranch}
							{showPreviousMessage}
							{showNextMessage}
							{updateChat}
							{editMessage}
							{deleteMessage}
							{rateMessage}
							{actionMessage}
							{saveMessage}
							{submitMessage}
							{regenerateResponse}
							{rewindAndInsert}
							{retryWithoutProviderRestrictions}
							{markSkipRemainingRetries}
							{regenerateWithModel}
							{continueResponse}
							{mergeResponses}
							{triggerScroll}
							{readOnly}
							{editCodeBlock}
							{topPadding}
							{widescreen}
						/>
					{/each}
				</ul>
			</section>
			<!-- Trailing room under the last message. This stacks with the row's own
			     mb-4 and the scroller's pb-2.5, so anything generous here reads as a
			     dead band between the last reply's action row (info/copy/read-aloud)
			     and the composer — "the conversation stops well short of the input".
			     Desktop keeps a hair more breathing room than mobile; both are just
			     the row margin plus a little. -->
		<div class="pb-2 md:pb-4"></div>
		{#if bottomPadding}
			<div class="  pb-6"></div>
		{/if}
		<!-- Composer-shrink compensation spacer, owned by Chat.svelte's
		     ResizeObserver. It must live HERE (the true bottom of the scroll
		     content) — anywhere outside the content flow its height wouldn't
		     extend scrollHeight, and the whole point is keeping the scroll
		     range constant while the composer resizes below. -->
		<div id="composer-compensation-spacer" class="shrink-0" style="height: 0px" aria-hidden="true"></div>
		</div>
	{/if}
</div>
