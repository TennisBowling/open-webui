<script lang="ts">
	import { getContext, onDestroy, onMount, tick } from 'svelte';
	import { goto } from '$app/navigation';

	import Modal from '$lib/components/common/Modal.svelte';
	import SearchInput from './Sidebar/SearchInput.svelte';
	import {
		getChatMessagesBranch,
		getChatMeta,
		getChatCount,
		searchChats,
		type ChatSearchHit,
		type ChatSearchFacets,
		type ChatSearchResponse
	} from '$lib/apis/chats';
	import Spinner from '../common/Spinner.svelte';
	import { searchHistoryAdd, sanitizeMarkSnippet } from '$lib/utils';

	import dayjs from '$lib/dayjs';
	import calendar from 'dayjs/plugin/calendar';
	import relativeTime from 'dayjs/plugin/relativeTime';
	dayjs.extend(calendar);
	dayjs.extend(relativeTime);

	import { createMessagesList } from '$lib/utils';
	import { chats as chatsStore, config, user } from '$lib/stores';
	import Messages from '../chat/Messages.svelte';
	import PencilSquare from '../icons/PencilSquare.svelte';
	import PageEdit from '../icons/PageEdit.svelte';
	import FilterChips from './SearchModal/FilterChips.svelte';
	import ResultRow from './SearchModal/ResultRow.svelte';
	import RecentSearches from './SearchModal/RecentSearches.svelte';

	const i18n = getContext('i18n');

	interface Props {
		show?: boolean;
		onClose?: any;
	}

	let { show = $bindable(false), onClose = () => {} }: Props = $props();

	// ── Inputs ────────────────────────────────────────────────────────────
	let query = $state('');

	// Filter chip state
	let archived: boolean | null = $state(null);
	let pinned: boolean | null = $state(null);
	let folderIds: string[] = $state([]);
	let tagIds: string[] = $state([]);
	let modelIds: string[] = [];
	let datePreset: 'all' | 'today' | '7d' | '30d' | 'year' = $state('all');
	let sort: 'relevance' | 'recent' = $state('relevance');

	// ── Results ───────────────────────────────────────────────────────────
	let response: ChatSearchResponse | null = $state(null);

	let optimisticHits: ChatSearchHit[] = $state([]);

	let loading = $state(false);
	let loadingMore = $state(false);
	let searchError = $state(false);
	let page = $state(1);
	let chatCount: number | null = $state(null);
	const PAGE_SIZE = 30;

	let recentSearchesRef: { reload: () => void } | undefined = $state();

	// ── Debounce / abort plumbing ──────────────────────────────────────────
	let debounceTimer: ReturnType<typeof setTimeout> | null = null;
	let firstCharFired = false;
	let inFlightController: AbortController | null = null;
	let searchRequestId = 0;

	// ── Keyboard nav ──────────────────────────────────────────────────────
	let actions: { label: string; onClick: () => Promise<void>; icon: any }[] = $state([]);
	let selectedIdx: number | null = $state(null);

	// ── Preview pane ──────────────────────────────────────────────────────
	// The preview shows only the matched message + a few ancestors (fetched via
	// the lightweight branch endpoint), NOT the whole multi-MB chat blob through
	// the full renderer — that froze/crashed the tab on big agentic chats.
	const PREVIEW_LIMIT = 6;
	const PREVIEW_CACHE_MAX = 30;
	let selectedHit: ChatSearchHit | null = $state(null);
	let selectedModels = $state(['']);
	let history: any = $state(null);
	let messages: any[] | null = $state(null);
	let chatCache: Map<string, any> = new Map();
	let previewDebounce: ReturnType<typeof setTimeout> | null = null;
	let previewRequestId = 0;
	let previewController: AbortController | null = null;

	const chatCacheSet = (id: string, value: any) => {
		if (chatCache.has(id)) chatCache.delete(id);
		chatCache.set(id, value);
		while (chatCache.size > PREVIEW_CACHE_MAX) {
			const oldest = chatCache.keys().next().value;
			if (oldest === undefined) break;
			chatCache.delete(oldest);
		}
	};

	// Build a minimal history ({ currentId, messages }) from a branch page so the
	// <Messages> renderer only ever mounts the handful of fetched messages.
	const buildPreviewHistory = (branch: any, leaf: string | null) => {
		const arr = Array.isArray(branch)
			? branch
			: Array.isArray(branch?.messages)
				? branch.messages
				: [];
		if (!arr.length || !leaf) return null;
		const messagesMap: Record<string, any> = {};
		for (const m of arr) {
			if (m?.id) messagesMap[m.id] = { ...m };
		}
		if (!messagesMap[leaf]) return null;
		// We only fetched the matched message + a few ancestors. The oldest fetched
		// message's parentId points at a message we deliberately did NOT fetch, which
		// makes the renderer treat the chain as "incomplete" and show a perpetual
		// loading spinner (pagination is disabled in the preview). Root any dangling
		// parent so the fetched window is a self-contained, complete thread.
		for (const m of Object.values(messagesMap)) {
			if (m.parentId && !messagesMap[m.parentId]) m.parentId = null;
		}
		return { currentId: leaf, messages: messagesMap };
	};

	const computeDateRange = (): { updated_after: number | null; updated_before: number | null } => {
		const now = Math.floor(Date.now() / 1000);
		const day = 86400;
		switch (datePreset) {
			case 'today':
				return { updated_after: now - day, updated_before: null };
			case '7d':
				return { updated_after: now - 7 * day, updated_before: null };
			case '30d':
				return { updated_after: now - 30 * day, updated_before: null };
			case 'year':
				return { updated_after: now - 365 * day, updated_before: null };
			default:
				return { updated_after: null, updated_before: null };
		}
	};

	const runSearch = (immediate = false, append = false) => {
		if (!show) return;
		if (append && (loading || loadingMore || !response || response.hits.length >= response.total)) {
			return;
		}
		if (debounceTimer) {
			clearTimeout(debounceTimer);
			debounceTimer = null;
		}
		inFlightController?.abort();
		inFlightController = null;
		const requestId = ++searchRequestId;
		const requestedPage = append ? page + 1 : 1;

		if (append) {
			loadingMore = true;
		} else {
			// Never leave results from the previous query clickable during the
			// debounce window. The small sidebar-title result set below fills that
			// gap instantly for the first character.
			response = null;
			page = 1;
			loading = true;
			searchError = false;
			selectedIdx = null;
			selectedHit = null;
			messages = null;
			history = null;
		}

		const fire = async () => {
			const controller = new AbortController();
			inFlightController = controller;
			const { updated_after, updated_before } = computeDateRange();
			try {
				const res = await searchChats(
					localStorage.token,
					{
						text: query,
						page: requestedPage,
						limit: PAGE_SIZE,
						folder_ids: folderIds,
						tag_ids: tagIds,
						pinned,
						archived,
						updated_after,
						updated_before,
						sort
					},
					controller.signal
				);
				if (requestId !== searchRequestId) return;
				if (append) {
					const existing = response?.hits ?? [];
					const seen = new Set(existing.map((hit) => hit.id));
					response = {
						...res,
						hits: [...existing, ...res.hits.filter((hit) => !seen.has(hit.id))],
						facets: response?.facets ?? res.facets
					};
					page = requestedPage;
				} else {
					response = res;
					selectedIdx = res.hits.length > 0 ? actions.length : null;
					schedulePreview(selectedIdx);
					optimisticHits = [];
				}
			} catch (err: any) {
				if (err?.name === 'AbortError') return;
				console.error(err);
				if (requestId === searchRequestId) searchError = true;
			} finally {
				if (requestId === searchRequestId) {
					loading = false;
					loadingMore = false;
				}
				if (inFlightController === controller) inFlightController = null;
			}
		};

		// First-character optimism: render title-prefix matches from the
		// already-loaded sidebar chats immediately for instant feedback, then let
		// the normal debounce fire the backend search. (We no longer fire a special
		// 50ms backend request on the first character — the cheapest, least useful
		// query was hammering the DB on every search session.)
		const text = query.trim();
		if (text.length > 0) {
			firstCharFired = true;
			const loaded = ($chatsStore ?? []) as { id: string; title: string; updated_at: number }[];
			const lower = text.toLowerCase();
			optimisticHits = loaded
				.filter((c) => (c.title ?? '').toLowerCase().includes(lower))
				.slice(0, 8)
				.map((c) => ({
					id: c.id,
					title: c.title ?? '',
					updated_at: c.updated_at,
					created_at: 0,
					archived: false,
					pinned: false,
					folder_id: null,
					snippet: null,
					match_count: 0,
					matched_message_id: null,
					matched_role: null,
					score: 0
				}));
			if (optimisticHits.length > 0) selectedIdx = actions.length;
		}

		if (text.length === 0) {
			firstCharFired = false;
			optimisticHits = [];
		}

		if (immediate) {
			void fire();
		} else {
			debounceTimer = setTimeout(() => void fire(), 220);
		}
	};

	const schedulePreview = (idx: number | null) => {
		if (previewDebounce) clearTimeout(previewDebounce);
		previewDebounce = setTimeout(() => {
			loadChatPreview(idx);
		}, 150);
	};

	const loadChatPreview = async (idx: number | null) => {
		if (idx === null) {
			selectedHit = null;
			messages = null;
			history = null;
			return;
		}
		const hitIdx = idx - actions.length;
		if (hitIdx < 0) {
			selectedHit = null;
			return;
		}
		const hit = displayHits[hitIdx];
		if (!hit) return;
		const requestId = ++previewRequestId;

		// Abort any preview fetch still in flight for a previously-selected hit.
		if (previewController) previewController.abort();
		previewController = new AbortController();
		const signal = previewController.signal;

		let previewHistory = chatCache.get(hit.id);
		if (!previewHistory) {
			try {
				let leaf = hit.matched_message_id;
				if (!leaf) {
					// Title-only match (no matched message): preview the chat's tail.
					const meta = await getChatMeta(localStorage.token, hit.id).catch(() => null);
					leaf = meta?.history?.currentId ?? null;
				}
				const branch = leaf
					? await getChatMessagesBranch(
							localStorage.token,
							hit.id,
							{ leaf, limit: PREVIEW_LIMIT },
							signal
						)
					: [];
				previewHistory = buildPreviewHistory(branch, leaf);
				if (previewHistory) chatCacheSet(hit.id, previewHistory);
			} catch (err: any) {
				if (err?.name === 'AbortError') return;
				console.error(err);
				previewHistory = null;
			}
		}
		if (requestId !== previewRequestId) return;

		if (previewHistory && previewHistory.currentId) {
			selectedHit = hit;
			selectedModels = [''];
			history = previewHistory;
			messages = createMessagesList(previewHistory, previewHistory.currentId);

			await tick();
			scrollPreviewToMatch(hit.matched_message_id);
		} else {
			selectedHit = hit;
			messages = null;
			history = null;
		}
	};

	// Scroll the preview pane to the matched message (the "mind-reading" bit).
	const scrollPreviewToMatch = async (messageId: string | null) => {
		const container = document.getElementById('chat-preview');
		if (!container) return;
		if (!messageId) {
			container.scrollTop = container.scrollHeight;
			return;
		}
		// Messages render with `id={message-${id}}` in Messages.svelte's child markup.
		// We use a broad attribute selector to be resilient to small naming shifts.
		let target = container.querySelector(
			`[data-message-id="${CSS.escape(messageId)}"], #message-${CSS.escape(messageId)}`
		) as HTMLElement | null;
		if (!target) {
			// Fall back: try to find any element whose id contains the message id
			target = container.querySelector(`[id*="${CSS.escape(messageId)}"]`) as HTMLElement | null;
		}
		if (target) {
			target.scrollIntoView({ block: 'center', behavior: 'instant' });
			target.classList.add('search-match-flash');
			setTimeout(() => target?.classList.remove('search-match-flash'), 1400);
		} else {
			container.scrollTop = container.scrollHeight;
		}
	};

	const onKeyDown = (e: KeyboardEvent) => {
		if (!show) return;
		if (e.code === 'Escape') {
			show = false;
			onClose();
			return;
		}
		if (e.code === 'Enter') {
			e.preventDefault();
			const el = document.querySelector(`[data-arrow-selected="true"]`) as HTMLElement | null;
			if (el) {
				if (query.trim()) searchHistoryAdd(query.trim());
				el.click();
				show = false;
			}
			return;
		}
		if (e.code === 'ArrowDown') {
			e.preventDefault();
			const total = actions.length + displayHits.length;
			selectedIdx = Math.min((selectedIdx ?? -1) + 1, total - 1);
			schedulePreview(selectedIdx);
			const el = document.querySelector(`[data-arrow-selected="true"]`) as HTMLElement | null;
			el?.scrollIntoView({ block: 'center', behavior: 'instant' });
		} else if (e.code === 'ArrowUp') {
			e.preventDefault();
			selectedIdx = Math.max((selectedIdx ?? 0) - 1, 0);
			schedulePreview(selectedIdx);
			const el = document.querySelector(`[data-arrow-selected="true"]`) as HTMLElement | null;
			el?.scrollIntoView({ block: 'center', behavior: 'instant' });
		}
	};

	const onChipsChange = () => {
		runSearch(true);
	};

	const onSearchInput = () => {
		runSearch(false);
	};

	const inputKeydown = (e: KeyboardEvent) => {
		if (e.code === 'Enter' || e.code === 'ArrowDown' || e.code === 'ArrowUp') {
			// The modal also has document-level navigation for when focus is on a
			// result row. Stop this input event from bubbling there and applying the
			// same key twice.
			e.stopPropagation();
			e.preventDefault();
		}
		if (e.code === 'Enter' && displayHits.length > 0) {
			const el = document.querySelector(`[data-arrow-selected="true"]`) as HTMLElement | null;
			if (el) {
				if (query.trim()) searchHistoryAdd(query.trim());
				el.click();
				show = false;
			}
			return;
		}
		if (e.code === 'ArrowDown') {
			selectedIdx = Math.min((selectedIdx ?? -1) + 1, actions.length + displayHits.length - 1);
		} else if (e.code === 'ArrowUp') {
			selectedIdx = Math.max((selectedIdx ?? 0) - 1, 0);
		}
		schedulePreview(selectedIdx);
		const el = document.querySelector(`[data-arrow-selected="true"]`) as HTMLElement | null;
		el?.scrollIntoView({ block: 'center', behavior: 'instant' });
	};

	const onRecentPick = (e: CustomEvent<string>) => {
		query = e.detail;
		runSearch(true);
	};

	const initOpen = async () => {
		await tick();
		// Reset transient state on each open
		response = null;
		optimisticHits = [];
		selectedHit = null;
		messages = null;
		history = null;
		selectedIdx = null;
		firstCharFired = false;
		page = 1;
		searchError = false;
		// Drop cached preview histories so the modal doesn't hold large message
		// maps across opens.
		chatCache.clear();
		// Counting the library is decorative; it must never delay the first useful
		// result request or replay a query the user already started typing.
		void getChatCount(localStorage.token).then(
			(count) => (chatCount = count),
			() => (chatCount = null)
		);
		recentSearchesRef?.reload();
		// Initial: fetch with whatever query/filters are set (usually empty)
		runSearch(true);
	};

	onMount(() => {
		actions = [
			{
				label: $i18n.t('Start a new conversation'),
				onClick: async () => {
					await goto(`/${query ? `?q=${encodeURIComponent(query)}` : ''}`);
					show = false;
					onClose();
				},
				icon: PencilSquare
			},
			...(($config?.features?.enable_notes ?? false) &&
			($user?.role === 'admin' || ($user?.permissions?.features?.notes ?? true))
				? [
						{
							label: $i18n.t('Create a new note'),
							onClick: async () => {
								await goto(`/notes${query ? `?content=${encodeURIComponent(query)}` : ''}`);
								show = false;
								onClose();
							},
							icon: PageEdit
						}
					]
				: [])
		];
		document.addEventListener('keydown', onKeyDown);
	});

	onDestroy(() => {
		searchRequestId += 1;
		if (debounceTimer) clearTimeout(debounceTimer);
		if (previewDebounce) clearTimeout(previewDebounce);
		if (inFlightController) inFlightController.abort();
		if (previewController) previewController.abort();
		document.removeEventListener('keydown', onKeyDown);
	});
	let hits = $derived((response?.hits ?? []) as ChatSearchHit[]);
	let facets = $derived((response?.facets ?? null) as ChatSearchFacets | null);
	let usedFuzzy = $derived(response?.used_fuzzy ?? false);
	let totalCount = $derived(response?.total ?? 0);
	let displayHits = $derived(hits.length > 0 ? hits : optimisticHits);
	let hasMore = $derived(Boolean(response && response.hits.length < response.total));
	$effect(() => {
		if (show) {
			void initOpen();
		} else {
			searchRequestId += 1;
			if (debounceTimer) clearTimeout(debounceTimer);
			debounceTimer = null;
			inFlightController?.abort();
			inFlightController = null;
			loading = false;
			loadingMore = false;
		}
	});
</script>

<Modal size="xl" bind:show>
	<div class="py-3 dark:text-gray-300 text-gray-700">
		<div class="px-4 pb-1.5">
			<SearchInput
				bind:value={query}
				oninput={onSearchInput}
				placeholder={$i18n.t('Search conversations and messages')}
				showClearButton={true}
				onFocus={() => {
					selectedIdx = null;
					messages = null;
				}}
				onKeydown={inputKeydown}
			/>
		</div>

		<FilterChips
			bind:archived
			bind:pinned
			bind:folderIds
			bind:tagIds
			bind:datePreset
			bind:sort
			{facets}
			onchange={onChipsChange}
		/>

		{#if !query && response && hits.length === 0}
			<RecentSearches bind:this={recentSearchesRef} onpick={onRecentPick} />
		{/if}

		{#if usedFuzzy && response?.did_you_mean}
			<div
				class="mx-4 mb-2 px-3 py-1.5 text-xs rounded-lg bg-warning/10 border-hairline border-warning/25 text-warning dark:text-warning-dark"
			>
				{$i18n.t('No exact matches for')} <strong>{response.did_you_mean}</strong>.
				{$i18n.t('Showing similar matches.')}
			</div>
		{/if}

		{#if chatCount !== null && !query}
			<div class="px-6 pb-2 text-xs text-gray-400 dark:text-gray-500">
				{$i18n.t('You have {{count}} chats', { count: chatCount.toLocaleString() })}
			</div>
		{/if}

		{#if response && (query || archived !== null || pinned !== null || folderIds.length || tagIds.length || datePreset !== 'all')}
			<div class="px-6 pb-1 text-xs text-gray-500 dark:text-gray-500">
				{#if loading}
					{$i18n.t('Searching...')}
				{:else if totalCount === 0}
					{$i18n.t('No matches')}
				{:else}
					{$i18n.t('{{count}} results', { count: totalCount.toLocaleString() })}
				{/if}
			</div>
		{/if}

		<div class="flex px-4 pb-1">
			<div
				class="flex flex-col overflow-y-auto h-96 md:h-[40rem] max-h-full scrollbar-hidden w-full flex-1 pr-2"
			>
				{#if actions.length > 0}
					<div class="w-full text-xs text-gray-500 dark:text-gray-500 font-medium pb-2 px-2">
						{$i18n.t('Actions')}
					</div>
					{#each actions as action, idx (action.label)}
						<button
							class="w-full flex items-center rounded-xl text-sm py-2 px-3 hover:bg-gray-50 dark:hover:bg-gray-850 {selectedIdx ===
							idx
								? 'bg-gray-50 dark:bg-gray-850'
								: ''}"
							data-arrow-selected={selectedIdx === idx ? 'true' : undefined}
							onmouseenter={() => {
								selectedIdx = idx;
								schedulePreview(idx);
							}}
							onclick={async () => {
								await action.onClick();
							}}
							type="button"
						>
							<div class="pr-2">
								<action.icon />
							</div>
							<div class="flex-1 text-left text-ellipsis line-clamp-1">
								{action.label}
							</div>
						</button>
					{/each}
				{/if}

				{#if searchError}
					<div class="text-xs text-gray-500 dark:text-gray-400 text-center px-5 py-8">
						<div>{$i18n.t('Search failed. Please try again.')}</div>
						<button
							class="mt-2 px-3 py-1.5 rounded-lg bg-gray-100 dark:bg-gray-800 hover:bg-gray-200 dark:hover:bg-gray-700"
							type="button"
							onclick={() => runSearch(true)}>{$i18n.t('Retry')}</button
						>
					</div>
				{:else if response || optimisticHits.length > 0}
					<hr class="border-gray-50 dark:border-gray-850 my-3" />
					{#if displayHits.length === 0 && !loading}
						<div class="text-xs text-gray-500 dark:text-gray-400 text-center px-5 py-4">
							{$i18n.t('No matches')}
						</div>
					{:else}
						{#each displayHits as hit, idx (hit.id)}
							<ResultRow
								{hit}
								selected={selectedIdx === idx + actions.length}
								onmouseenter={() => {
									selectedIdx = idx + actions.length;
									schedulePreview(selectedIdx);
								}}
								onclick={async () => {
									if (query.trim()) searchHistoryAdd(query.trim());
									await goto(`/c/${hit.id}`);
									show = false;
									onClose();
								}}
							/>
						{/each}
						{#if hasMore}
							<button
								class="mx-2 mt-2 mb-4 rounded-xl py-2 text-xs font-medium text-gray-600 dark:text-gray-300 bg-gray-50 dark:bg-gray-850 hover:bg-gray-100 dark:hover:bg-gray-800 disabled:opacity-60"
								type="button"
								disabled={loadingMore}
								onclick={() => runSearch(true, true)}
							>
								{loadingMore ? $i18n.t('Loading...') : $i18n.t('Load more')}
							</button>
						{/if}
					{/if}
				{:else}
					<div class="w-full h-full flex justify-center items-center">
						<Spinner className="size-5" />
					</div>
				{/if}
			</div>

			<div
				id="chat-preview"
				class="hidden md:flex md:flex-1 w-full overflow-y-auto h-96 md:h-[40rem] scrollbar-hidden"
			>
				{#if messages === null}
					<div
						class="w-full h-full flex justify-center items-center text-gray-500 dark:text-gray-400 text-sm"
					>
						{$i18n.t('Select a conversation to preview')}
					</div>
				{:else}
					<div class="w-full h-full flex flex-col">
						<Messages
							className="h-full flex pt-4 pb-8 w-full"
							chatId={`chat-preview-${selectedHit?.id ?? ''}`}
							user={$user}
							readOnly={true}
							{selectedModels}
							bind:history
							bind:messages
							autoScroll={false}
							allowPagination={false}
							sendMessage={() => {}}
							continueResponse={() => {}}
							regenerateResponse={() => {}}
						/>
					</div>
				{/if}
			</div>
		</div>
	</div>
</Modal>

<style>
	:global(.search-match-flash) {
		animation: search-match-flash 1.2s ease-out;
	}
	@keyframes search-match-flash {
		0% {
			background-color: rgba(204, 120, 92, 0.28);
		}
		100% {
			background-color: transparent;
		}
	}
</style>
