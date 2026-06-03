<script lang="ts">
	import { v4 as uuidv4 } from 'uuid';
	import {
		config,
		settings,
		user as _user,
		mobile,
		temporaryChatEnabled
	} from '$lib/stores';
	import { tick, getContext, onMount, createEventDispatcher } from 'svelte';
	const dispatch = createEventDispatcher();

	import { toast } from 'svelte-sonner';
	import {
		getChatMessagesBranch,
		getChatMessagesSiblings,
		patchChat,
		updateChatById,
		type PatchChatOp
	} from '$lib/apis/chats';
	import { copyToClipboard, extractCurlyBraceWords } from '$lib/utils';

	import Message from './Messages/Message.svelte';
	import Loader from '../common/Loader.svelte';
	import Spinner from '../common/Spinner.svelte';

	import ChatPlaceholder from './ChatPlaceholder.svelte';

	const i18n = getContext('i18n');

	export let className = 'h-full flex pt-8';

	export let chatId = '';
	// Track $_user reactively rather than capturing it once at component init.
	// During the brief window before the user store hydrates, the captured value
	// would be undefined and never update, breaking child role checks.
	$: user = $_user;

	export let prompt;
	export let history = {};
	export let selectedModels;
	export let atSelectedModel;

	let messages = [];

	export let setInputText: Function = () => {};

	export let sendMessage: Function;
	export let continueResponse: Function;
	export let regenerateResponse: Function;
	export let retryWithoutProviderRestrictions: Function = () => {};
	export let markSkipRemainingRetries: Function = () => {};
	export let regenerateWithModel: Function = () => {};
	export let mergeResponses: Function;

	export let chatActionHandler: Function;
	export let showMessage: Function = () => {};
	export let submitMessage: Function = () => {};
	export let addMessages: Function = () => {};

	export let readOnly = false;
	export let editCodeBlock = true;

	export let topPadding = false;
	export let bottomPadding = false;
	export let autoScroll;
	export let allowPagination = true;

	export let onSelect = (e) => {};

	export let messagesCount: number | null = 25;
	let messagesLoading = false;
	let paginationChatId = '';
	// Tracks the most recent outcome per anchor message id. 'exhausted' is
	// sticky (no more older rows on the server); 'error' is transient (a
	// retry can clear it). Replaces a single `Set<exhaustedBeforeIds>` whose
	// latch trapped users whenever a transient fetch error was indistinguishable
	// from a genuinely empty page.
	type AnchorState = 'exhausted' | 'error';
	let anchorStates: Map<string, AnchorState> = new Map();

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
		| { kind: 'error'; anchorId: string; beforeId: string | null };
	let frontier: ChainFrontier = { kind: 'complete' };

	$: if (chatId && chatId !== paginationChatId) {
		paginationChatId = chatId;
		messagesCount = MESSAGE_PAGE_SIZE;
		anchorStates = new Map();
		frontier = { kind: 'complete' };
	}

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
			await tick();
		}
	};

	// Hydrate a chain rooted at `missingId` — used when the display walk
	// detects an orphan parent. Fetches by `leaf=missingId` so the response
	// includes that node plus its ancestors, stitching the gap closed.
	const hydrateFromLeaf = async (missingId: string): Promise<number> => {
		if (!chatId || !missingId) return 0;
		const page = await getChatMessagesBranch(localStorage.token, chatId, {
			leaf: missingId,
			limit: MESSAGE_PAGE_SIZE
		});
		const hydrated = mergePaginatedMessages(page);
		if (hydrated > 0) history = history;
		return hydrated;
	};

	// Advance the chain by one step, routing on the current frontier kind.
	// Replaces the old `loadMoreMessages` whose single code path conflated
	// "stub ahead" with "older messages exist beyond render window," and
	// whose error handling (`.catch(() => null)`) collapsed network failures
	// into the same "no more rows" outcome as a genuinely empty page.
	const advanceFrontier = async (target: ChainFrontier) => {
		if (messagesLoading) return;
		if (target.kind === 'complete') return;

		const element = document.getElementById('messages-container');
		const previousScrollHeight = element ? element.scrollHeight : 0;
		const previousScrollTop = element ? element.scrollTop : 0;

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
				// includes it and its ancestors. Walk will re-converge on
				// the next reactive pass.
				try {
					const hydrated = await hydrateFromLeaf(target.missingId);
					if (hydrated > 0) {
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
				} catch (err) {
					anchorStates.set(target.anchorId, 'error');
					anchorStates = new Map(anchorStates);
				}
				await tick();
				return;
			}

			// target.kind === 'stub' or 'error' → page ancestors of the anchor.
			//
			// `beforeId` is the youngest hydrated message; the backend treats
			// `before` as exclusive, so we page strictly older than that. When
			// the leaf itself is a stub (no hydrated message has been pushed),
			// `beforeId` is null and we instead hydrate by `leaf=anchorId`,
			// which fetches the anchor + its ancestors.
			const anchorId = target.anchorId;
			const beforeId = target.beforeId;
			let hydratedCount = 0;
			try {
				const page = beforeId
					? await getChatMessagesBranch(localStorage.token, chatId, {
							leaf: leafId,
							before: beforeId,
							limit: MESSAGE_PAGE_SIZE
						})
					: await getChatMessagesBranch(localStorage.token, chatId, {
							leaf: anchorId,
							limit: MESSAGE_PAGE_SIZE
						});
				hydratedCount = mergePaginatedMessages(page);
				if (hydratedCount > 0) {
					history = history;
					if (anchorStates.has(anchorId)) {
						anchorStates.delete(anchorId);
						anchorStates = new Map(anchorStates);
					}
				} else {
					// Genuine empty page — nothing older on this branch.
					anchorStates.set(anchorId, 'exhausted');
					anchorStates = new Map(anchorStates);
				}
			} catch (err) {
				// Network/server error. Don't mark exhausted — a retry can
				// recover. The frontier reactive walk will see the 'error'
				// state on the next pass and the Loader will render an
				// inline retry affordance.
				anchorStates.set(anchorId, 'error');
				anchorStates = new Map(anchorStates);
			}

			if (messagesCount !== null) {
				messagesCount += hydratedCount;
			}
			await tick();
		} finally {
			if (element) {
				element.scrollTop = previousScrollTop + (element.scrollHeight - previousScrollHeight);
			}
			messagesLoading = false;
		}
	};

	$: {
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
				if (state === 'error') {
					// Preserve the beforeId from the stub variant if that's
					// what failed; otherwise this is an error against the
					// oldest rendered id.
					const beforeId =
						newFrontier.kind === 'stub'
							? newFrontier.beforeId
							: _messages.length
								? _messages[0].id
								: null;
					newFrontier = { kind: 'error', anchorId, beforeId };
				} else if (state === 'exhausted' && newFrontier.kind !== 'capped') {
					newFrontier = { kind: 'complete' };
				}
			}

			messages = _messages;
			frontier = newFrontier;
		}
	}

	$: if (autoScroll && bottomPadding) {
		(async () => {
			await tick();
			scrollToBottom();
		})();
	}

	const scrollToBottom = () => {
		const element = document.getElementById('messages-container');
		element.scrollTop = element.scrollHeight;
	};

	const updateChat = async (ops: PatchChatOp | PatchChatOp[] | undefined = undefined) => {
		if ($temporaryChatEnabled) {
			return;
		}
		history = history;
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

	const gotoMessage = async (message, idx) => {
		// Determine the correct sibling list (either parent's children or root messages)
		let siblings;
		if (message.parentId !== null) {
			siblings = history.messages[message.parentId].childrenIds;
		} else {
			siblings = Object.values(history.messages)
				.filter((msg) => msg.parentId === null)
				.map((msg) => msg.id);
		}

		// Clamp index to a valid range
		idx = Math.max(0, Math.min(idx, siblings.length - 1));

		let messageId = siblings[idx];

		// If we're navigating to a different message
		if (message.id !== messageId) {
			// Drill down to the deepest child of that branch
			let messageChildrenIds = history.messages[messageId].childrenIds;
			while (messageChildrenIds.length !== 0) {
				messageId = messageChildrenIds.at(-1);
				messageChildrenIds = history.messages[messageId].childrenIds;
			}

			await hydrateBranchIfNeeded(messageId);
			history.currentId = messageId;
		}

		await tick();

		// Optional auto-scroll
		if ($settings?.scrollOnBranchChange ?? true) {
			const element = document.getElementById('messages-container');
			autoScroll = element.scrollHeight - element.scrollTop <= element.clientHeight + 50;

			setTimeout(() => {
				scrollToBottom();
			}, 100);
		}
	};

	const showPreviousMessage = async (message) => {
		if (message.parentId !== null) {
			let messageId =
				history.messages[message.parentId].childrenIds[
					Math.max(history.messages[message.parentId].childrenIds.indexOf(message.id) - 1, 0)
				];

			if (message.id !== messageId) {
				let messageChildrenIds = history.messages[messageId].childrenIds;

				while (messageChildrenIds.length !== 0) {
					messageId = messageChildrenIds.at(-1);
					messageChildrenIds = history.messages[messageId].childrenIds;
				}

				await hydrateBranchIfNeeded(messageId);
				history.currentId = messageId;
			}
		} else {
			let childrenIds = Object.values(history.messages)
				.filter((message) => message.parentId === null)
				.map((message) => message.id);
			let messageId = childrenIds[Math.max(childrenIds.indexOf(message.id) - 1, 0)];

			if (message.id !== messageId) {
				let messageChildrenIds = history.messages[messageId].childrenIds;

				while (messageChildrenIds.length !== 0) {
					messageId = messageChildrenIds.at(-1);
					messageChildrenIds = history.messages[messageId].childrenIds;
				}

				await hydrateBranchIfNeeded(messageId);
				history.currentId = messageId;
			}
		}

		await tick();

		if ($settings?.scrollOnBranchChange ?? true) {
			const element = document.getElementById('messages-container');
			autoScroll = element.scrollHeight - element.scrollTop <= element.clientHeight + 50;

			setTimeout(() => {
				scrollToBottom();
			}, 100);
		}
	};

	const showNextMessage = async (message) => {
		if (message.parentId !== null) {
			let messageId =
				history.messages[message.parentId].childrenIds[
					Math.min(
						history.messages[message.parentId].childrenIds.indexOf(message.id) + 1,
						history.messages[message.parentId].childrenIds.length - 1
					)
				];

			if (message.id !== messageId) {
				let messageChildrenIds = history.messages[messageId].childrenIds;

				while (messageChildrenIds.length !== 0) {
					messageId = messageChildrenIds.at(-1);
					messageChildrenIds = history.messages[messageId].childrenIds;
				}

				await hydrateBranchIfNeeded(messageId);
				history.currentId = messageId;
			}
		} else {
			let childrenIds = Object.values(history.messages)
				.filter((message) => message.parentId === null)
				.map((message) => message.id);
			let messageId =
				childrenIds[Math.min(childrenIds.indexOf(message.id) + 1, childrenIds.length - 1)];

			if (message.id !== messageId) {
				let messageChildrenIds = history.messages[messageId].childrenIds;

				while (messageChildrenIds.length !== 0) {
					messageId = messageChildrenIds.at(-1);
					messageChildrenIds = history.messages[messageId].childrenIds;
				}

				await hydrateBranchIfNeeded(messageId);
				history.currentId = messageId;
			}
		}

		await tick();

		if ($settings?.scrollOnBranchChange ?? true) {
			const element = document.getElementById('messages-container');
			autoScroll = element.scrollHeight - element.scrollTop <= element.clientHeight + 50;

			setTimeout(() => {
				scrollToBottom();
			}, 100);
		}
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

	const editMessage = async (messageId, { content, files }, submit = true) => {
		if ((selectedModels ?? []).filter((id) => id).length === 0) {
			toast.error($i18n.t('Model not selected'));
			return;
		}
		if (history.messages[messageId].role === 'user') {
			if (submit) {
				// New user message
				let userPrompt = content;
				let userMessageId = uuidv4();

				let userMessage = {
					id: userMessageId,
					parentId: history.messages[messageId].parentId,
					childrenIds: [],
					role: 'user',
					content: userPrompt,
					...(files && { files: files }),
					models: selectedModels,
					timestamp: Math.floor(Date.now() / 1000) // Unix epoch
				};

				let messageParentId = history.messages[messageId].parentId;

				if (messageParentId !== null) {
					history.messages[messageParentId].childrenIds = [
						...history.messages[messageParentId].childrenIds,
						userMessageId
					];
				}

				history.messages[userMessageId] = userMessage;
				history.currentId = userMessageId;

				await tick();
				await sendMessage(history, userMessageId);
			} else {
				// Edit user message
				history.messages[messageId].content = content;
				history.messages[messageId].files = files;
				await updateChat({
					op: 'update_message_content',
					message_id: messageId,
					content,
					...(files !== undefined ? { files } : {})
				});
			}
		} else {
			if (submit) {
				// New response message
				const responseMessageId = uuidv4();
				const message = history.messages[messageId];
				const parentId = message.parentId;

				const responseMessage = {
					...message,
					id: responseMessageId,
					parentId: parentId,
					childrenIds: [],
					files: undefined,
					content: content,
					timestamp: Math.floor(Date.now() / 1000) // Unix epoch
				};

				history.messages[responseMessageId] = responseMessage;
				history.currentId = responseMessageId;

				// Append messageId to childrenIds of parent message
				if (parentId !== null) {
					history.messages[parentId].childrenIds = [
						...history.messages[parentId].childrenIds,
						responseMessageId
					];
				}

				await updateChat([
					{
						op: 'append_message',
						message_id: responseMessageId,
						parent_id: parentId,
						role: responseMessage.role,
						content: responseMessage.content,
						model: responseMessage.model,
						modelName: responseMessage.modelName,
						modelIdx: responseMessage.modelIdx,
						timestamp: responseMessage.timestamp
					},
					{ op: 'set_history_current_id', current_id: responseMessageId }
				]);
			} else {
				// Edit response message
				history.messages[messageId].originalContent = history.messages[messageId].content;
				history.messages[messageId].content = content;
				await updateChat({
					op: 'update_message_content',
					message_id: messageId,
					content
				});
			}
		}
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
		const messageToDelete = history.messages[messageId];
		const parentMessageId = messageToDelete.parentId;
		const childMessageIds = messageToDelete.childrenIds ?? [];

		// Collect all grandchildren
		const grandchildrenIds = childMessageIds.flatMap(
			(childId) => history.messages[childId]?.childrenIds ?? []
		);

		// Update parent's children
		if (parentMessageId && history.messages[parentMessageId]) {
			history.messages[parentMessageId].childrenIds = [
				...history.messages[parentMessageId].childrenIds.filter((id) => id !== messageId),
				...grandchildrenIds
			];
		}

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

		showMessage({ id: parentMessageId });

		// Backend's delete_message op handles the parent/grandchild relinking; we
		// already applied the same mutations locally above for optimistic UI.
		await updateChat({ op: 'delete_message', message_id: messageId });
	};

	const triggerScroll = () => {
		if (autoScroll) {
			const element = document.getElementById('messages-container');
			autoScroll = element.scrollHeight - element.scrollTop <= element.clientHeight + 50;
			setTimeout(() => {
				scrollToBottom();
			}, 100);
		}
	};
</script>

<div class={className}>
	{#if Object.keys(history?.messages ?? {}).length == 0}
		<ChatPlaceholder modelIds={selectedModels} {atSelectedModel} {onSelect} />
	{:else}
		<div class="w-full pt-2">
			{#key chatId}
				<section class="w-full" aria-labelledby="chat-conversation">
					<h2 class="sr-only" id="chat-conversation">{$i18n.t('Chat Conversation')}</h2>
					{#if frontier.kind !== 'complete'}
						<Loader
							root={typeof document !== 'undefined'
								? document.getElementById('messages-container')
								: null}
							on:visible={() => {
								if (allowPagination && !messagesLoading && frontier.kind !== 'error') {
									advanceFrontier(frontier);
								}
							}}
						>
							{#if frontier.kind === 'error'}
								<div
									class="w-full flex justify-center py-1 text-xs items-center gap-2"
								>
									<button
										type="button"
										class="underline text-red-600 dark:text-red-400"
										on:click={() => advanceFrontier(frontier)}
									>
										{$i18n.t('Failed to load older messages. Retry')}
									</button>
								</div>
							{:else}
								<div
									class="w-full flex justify-center py-1 text-xs animate-pulse items-center gap-2"
								>
									<Spinner className=" size-4" />
									<div class=" ">{$i18n.t('Loading...')}</div>
								</div>
							{/if}
						</Loader>
					{/if}
					<ul role="log" aria-live="polite" aria-relevant="additions" aria-atomic="false">
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
								{retryWithoutProviderRestrictions}
								{markSkipRemainingRetries}
								{regenerateWithModel}
								{continueResponse}
								{mergeResponses}
								{addMessages}
								{triggerScroll}
								{readOnly}
								{editCodeBlock}
								{topPadding}
							/>
						{/each}
					</ul>
				</section>
				<div class="pb-18" />
				{#if bottomPadding}
					<div class="  pb-6" />
				{/if}
			{/key}
		</div>
	{/if}
</div>
