<script lang="ts" module>
	import { getChatTokenStats } from '$lib/apis/analytics';

	// Two instances of this component mount per view (navbar pill + composer
	// stats). Share one in-flight request per chat id so a chat open costs a
	// single analytics fetch instead of two identical ones.
	const inflightStats = new Map<string, Promise<any>>();
	const fetchStatsShared = (token: string, id: string) => {
		let p = inflightStats.get(id);
		if (!p) {
			p = getChatTokenStats(token, id).finally(() => {
				inflightStats.delete(id);
			});
			inflightStats.set(id, p);
		}
		return p;
	};
</script>

<script lang="ts">
	import { getContext, onDestroy, onMount } from 'svelte';
	import { get } from 'svelte/store';
	import {
		chatId as currentChatIdStore,
		chatTokenStats,
		chatTokenStatsRefreshTrigger
	} from '$lib/stores';
	import { formatTokenCount, formatCost } from '$lib/apis/analytics';
	import Tooltip from '../common/Tooltip.svelte';

	const i18n = getContext('i18n');

	interface Props {
		chatId?: string;
	}

	let { chatId = '' }: Props = $props();

	let lastTrigger = $state(0);
	let trackedChatId = $state('');
	let lastTrackedChatId = $state('');
	// Trailing-debounce timer for refresh-trigger fetches. The trigger bumps once
	// per usage delta (i.e. once per tool-call round), so a 300-round agentic run
	// would otherwise fire a steady stream of backend fetches. A trailing debounce
	// collapses each burst into a SINGLE reconciliation fetch after activity
	// settles. The live per-round numbers are already shown via tokenUsageGroups;
	// this backend fetch is only the authoritative reconciliation.
	let refreshDebounceTimer: ReturnType<typeof setTimeout> | null = $state(null);
	const REFRESH_DEBOUNCE_MS = 1200;

	// Retry-on-failure state. A transient failure (network blip, backend indexing
	// lag for a freshly persisted chat, momentary auth window during boot) used to
	// silently nuke the counter and never recover until the user changed chats or
	// reloaded. Now we back off and retry; if every retry still fails we keep the
	// last good values rendered instead of disappearing.
	const RETRY_DELAYS_MS = [1000, 3000, 8000];
	let retryCount = $state(0);
	let retryTimer: ReturnType<typeof setTimeout> | null = null;

	function clearRetry() {
		if (retryTimer !== null) {
			clearTimeout(retryTimer);
			retryTimer = null;
		}
	}

	// Belt-and-suspenders: the reactive comparison above can silently miss the
	// initial fetch in two cases:
	//   1. The component mounts with chatId already at its final value, and the
	//      reactive batches the assignments such that trackedChatId and
	//      lastTrackedChatId are both '' on the only run that fires.
	//   2. The component is freshly mounted right after a previous instance was
	//      destroyed (which `set(null)`'d the store) and our prop is the same
	//      string we ended on — no transition for the comparison to detect.
	// An explicit onMount fetch closes both holes. fetchTokenStats is internally
	// idempotent (a stale-id check guards against double-application).
	onMount(() => {
		const initial = chatId || get(currentChatIdStore) || '';
		if (initial && !initial.startsWith('local:')) {
			fetchTokenStats(initial);
		}
	});

	function scheduleRetry(id: string) {
		clearRetry();

		// Drop the loading flag while we wait so the UI doesn't pulse forever.
		// Preserves the last good values so the box stays visible.
		chatTokenStats.update((current) => (current ? { ...current, loading: false } : current));

		if (retryCount >= RETRY_DELAYS_MS.length) {
			// Retries exhausted. The next chatId change or refresh trigger will
			// reset retry state and try again from scratch.
			return;
		}

		const delay = RETRY_DELAYS_MS[retryCount];
		retryCount += 1;

		retryTimer = setTimeout(() => {
			retryTimer = null;
			if (id === trackedChatId) {
				fetchTokenStats(id);
			}
		}, delay);
	}

	async function fetchTokenStats(id: string) {
		if (!id || id.startsWith('local:')) {
			chatTokenStats.set(null);
			return;
		}

		// Set loading state. Preserve last good values so the box doesn't flicker
		// empty during refetch — but ONLY when those values belong to THIS chat.
		// After a chat switch `current` still holds the PREVIOUS chat's stats;
		// stamping the new id onto them would (a) show the old chat's "Latest
		// Input" until the fetch lands and (b) trip the staleResponse guard below
		// (old totals > new totals), pinning the wrong values for the whole retry
		// window. Seed zeros across a switch so the placeholder is neutral.
		chatTokenStats.update((current) => {
			const carry = current?.chat_id === id ? current : null;
			return {
				chat_id: id,
				total_input_tokens: carry?.total_input_tokens ?? 0,
				total_output_tokens: carry?.total_output_tokens ?? 0,
				total_tokens: carry?.total_tokens ?? 0,
				total_cache_read_tokens: carry?.total_cache_read_tokens ?? 0,
				last_input_tokens: carry?.last_input_tokens ?? 0,
				last_output_tokens: carry?.last_output_tokens ?? 0,
				last_cache_read_tokens: carry?.last_cache_read_tokens ?? 0,
				message_count: carry?.message_count ?? 0,
				cost: carry?.cost ?? 0,
				loading: true
			};
		});

		const requestedChatId = id;
		const token = localStorage.getItem('token');
		if (!token) {
			// Token not in storage yet (rare race during boot, or just-logged-out).
			// Retry — by the time backoff fires, auth has either landed a token or
			// the user has been redirected away from this view.
			scheduleRetry(id);
			return;
		}

		try {
			const stats = await fetchStatsShared(token, requestedChatId);

			if (requestedChatId !== trackedChatId) {
				return; // Stale: user moved to a different chat mid-fetch
			}

			if (stats) {
				const next = {
					chat_id: stats.chat_id,
					total_input_tokens: stats.total_input_tokens,
					total_output_tokens: stats.total_output_tokens,
					total_tokens: stats.total_tokens,
					total_cache_read_tokens: stats.total_cache_read_tokens ?? 0,
					last_input_tokens: stats.last_input_tokens,
					last_output_tokens: stats.last_output_tokens,
					last_cache_read_tokens: stats.last_cache_read_tokens ?? 0,
					message_count: stats.message_count,
					cost: stats.cost ?? 0,
					loading: false
				};

				// If a live usage payload already advanced the counter, don't let a
				// lagging analytics read reset it to zero/old totals; retry until DB catches up.
				//
				// Staleness is gated on `message_count` ALONE, not on any of the token
				// totals. `message_count` is the DB event count and is monotonic — if
				// next.message_count >= current.message_count, this read is AT LEAST as
				// new as what's displayed, so it must be accepted in full even if one of
				// the displayed totals happens to be higher right now. That "higher"
				// case is exactly the optimistic-apply double-count bug (a live round
				// applied on top of an already-authoritative push): gating on the
				// individual totals used to keep those inflated numbers pinned forever,
				// because every subsequent DB read looked "stale" by that same
				// comparison and got rejected — the numbers only ever corrected on a
				// full page reload. Accepting on message_count lets the correct DB
				// numbers win and the inflated live total correct downward.
				let staleResponse = false;
				const isFinalRetryAttempt = retryCount >= RETRY_DELAYS_MS.length;
				chatTokenStats.update((current) => {
					staleResponse =
						current?.chat_id === next.chat_id &&
						current.message_count > next.message_count &&
						// Belt and suspenders: once retries are exhausted there is no
						// more DB catch-up coming on this chat visit, so keeping stale
						// live totals pinned forever is strictly worse than accepting
						// whatever the DB says now — surface the authoritative read.
						!isFinalRetryAttempt;

					if (staleResponse && current) {
						// The live TOKEN totals ran ahead of this DB read, so keep them
						// (a retry reconciles later). But NEVER drop the freshly-fetched
						// cost — that asymmetry is exactly why the $ segment used to
						// update only on reload (on reload `current` is null so this
						// branch never runs). Take the higher of the live cost and the
						// read so a lagging read can't regress it.
						return {
							...current,
							cost: Math.max(current.cost ?? 0, next.cost ?? 0),
							loading: false
						};
					}
					// Non-stale (or retries exhausted): this read is authoritative for
					// this chat — trust it fully, INCLUDING a legitimately lower cost
					// (e.g. after an admin pricing edit) or lower totals (e.g. correcting
					// an optimistic double-count), so numbers can still correct downward
					// here. (The live-push apply keeps Math.max only for out-of-order
					// reorder safety; this authoritative read is the path that heals it.)
					return next;
				});

				if (staleResponse) {
					scheduleRetry(requestedChatId);
				} else {
					clearRetry();
					retryCount = 0;
				}
			} else {
				// Null can mean: stats don't exist yet (brand-new chat with no
				// usage-bearing messages), backend indexing lag, or a transient
				// upstream error swallowed by the API helper. Retry with backoff.
				scheduleRetry(id);
			}
		} catch (error) {
			console.error('[ChatTokenStats] Error fetching token stats:', error);
			scheduleRetry(id);
		}
	}

	// Function to refresh stats (can be called from parent)
	export function refresh() {
		if (trackedChatId && !trackedChatId.startsWith('local:')) {
			clearRetry();
			retryCount = 0;
			fetchTokenStats(trackedChatId);
		}
	}

	onDestroy(() => {
		clearRetry();
		if (refreshDebounceTimer !== null) {
			clearTimeout(refreshDebounceTimer);
			refreshDebounceTimer = null;
		}
		// Intentionally do NOT clear the store on destroy. If a new ChatTokenStats
		// instance has already mounted and started fetching for a different chat,
		// `set(null)` here would race-clobber its loading state and the box would
		// vanish until the next reactive trigger. The next instance overwrites the
		// store on mount (via fetchTokenStats), so leaving stale data here is fine
		// — at worst the new instance flashes the previous chat's numbers for a
		// few ms before its own fetch completes.
	});
	$effect(() => {
		trackedChatId = chatId || $currentChatIdStore || '';
	});
	// Reset retry state and (re)start a fetch each time we move to a different chat.
	$effect(() => {
		if (trackedChatId !== lastTrackedChatId) {
			lastTrackedChatId = trackedChatId;
			clearRetry();
			retryCount = 0;
			if (trackedChatId && !trackedChatId.startsWith('local:')) {
				fetchTokenStats(trackedChatId);
			} else {
				chatTokenStats.set(null);
			}
		}
	});
	// Reactive fetch when refresh trigger changes (trailing-debounced so a burst
	// of per-round usage deltas collapses into one fetch after activity settles).
	$effect(() => {
		if (
			$chatTokenStatsRefreshTrigger > lastTrigger &&
			trackedChatId &&
			!trackedChatId.startsWith('local:')
		) {
			lastTrigger = $chatTokenStatsRefreshTrigger;
			clearRetry();
			retryCount = 0;
			if (refreshDebounceTimer !== null) {
				clearTimeout(refreshDebounceTimer);
			}
			refreshDebounceTimer = setTimeout(() => {
				refreshDebounceTimer = null;
				if (trackedChatId && !trackedChatId.startsWith('local:')) {
					fetchTokenStats(trackedChatId);
				}
			}, REFRESH_DEBOUNCE_MS);
		}
	});
	// True when the store holds real, displayable numbers (as opposed to the
	// all-zero placeholder a cold first fetch starts with). During streaming the
	// refresh trigger fires on every usage delta, and each fetch flips
	// `loading: true` while preserving the last good values. We use this to keep
	// the populated box rendered through those refreshes instead of swapping in
	// the `···` pulse — otherwise the box flashes in and out on every delta.
	let hasData = $derived(
		!!$chatTokenStats &&
			($chatTokenStats.total_tokens > 0 ||
				$chatTokenStats.message_count > 0 ||
				$chatTokenStats.last_input_tokens > 0 ||
				$chatTokenStats.last_output_tokens > 0 ||
				($chatTokenStats.cost ?? 0) > 0)
	);
</script>

{#if hasData}
	<Tooltip
		content={`
			<div class="text-xs space-y-1">
				<div class="font-semibold mb-1">${$i18n.t('Token Usage')}</div>
				<div class="flex justify-between gap-4">
					<span>${$i18n.t('Latest Input')}:</span>
					<span class="font-mono">${$chatTokenStats.last_input_tokens.toLocaleString()}</span>
				</div>
				<div class="flex justify-between gap-4">
					<span>${$i18n.t('Latest Output')}:</span>
					<span class="font-mono">${$chatTokenStats.last_output_tokens.toLocaleString()}</span>
				</div>
				<div class="flex justify-between gap-4">
					<span>${$i18n.t('Cache Read')}:</span>
					<span class="font-mono">${$chatTokenStats.last_cache_read_tokens.toLocaleString()}</span>
				</div>
				<div class="flex justify-between gap-4 border-t-hairline border-gray-600 pt-1 mt-1">
					<span>${$i18n.t('Request Total')}:</span>
					<span class="font-mono font-semibold">${$chatTokenStats.total_tokens.toLocaleString()}</span>
				</div>
				${
					($chatTokenStats.cost ?? 0) > 0
						? `<div class="flex justify-between gap-4">
						<span>${$i18n.t('Cost')}:</span>
						<span class="font-mono font-semibold text-success-dark">${formatCost($chatTokenStats.cost)}</span>
					</div>`
						: ''
				}
				<div class="text-gray-400 text-[10px] mt-2">
					${$chatTokenStats.message_count} ${$i18n.t('messages')}
				</div>
			</div>
		`}
		placement="bottom"
	>
		<div
			class="flex items-center gap-1.5 px-2 py-1 rounded-lg text-[11px] font-mono text-gray-500 dark:text-gray-400 bg-gray-50 dark:bg-gray-850 border-hairline border-gray-100 dark:border-gray-800 hover:bg-gray-100 dark:hover:bg-gray-800 transition-colors cursor-default select-none"
		>
			<!-- Latest input tokens -->
			<span class="flex items-center gap-0.5" title={$i18n.t('Latest input tokens')}>
				<svg
					xmlns="http://www.w3.org/2000/svg"
					viewBox="0 0 16 16"
					fill="currentColor"
					class="size-3 text-book-cloth dark:text-kraft"
				>
					<path
						fill-rule="evenodd"
						d="M8 14a.75.75 0 0 1-.75-.75V4.56L4.03 7.78a.75.75 0 0 1-1.06-1.06l4.5-4.5a.75.75 0 0 1 1.06 0l4.5 4.5a.75.75 0 0 1-1.06 1.06L8.75 4.56v8.69A.75.75 0 0 1 8 14Z"
						clip-rule="evenodd"
					/>
				</svg>
				<span>{formatTokenCount($chatTokenStats.last_input_tokens)}</span>
			</span>

			<span class="text-gray-300 dark:text-gray-600">·</span>

			<!-- Latest output tokens -->
			<span class="flex items-center gap-0.5" title={$i18n.t('Latest output tokens')}>
				<svg
					xmlns="http://www.w3.org/2000/svg"
					viewBox="0 0 16 16"
					fill="currentColor"
					class="size-3 text-success dark:text-success-dark"
				>
					<path
						fill-rule="evenodd"
						d="M8 2a.75.75 0 0 1 .75.75v8.69l3.22-3.22a.75.75 0 1 1 1.06 1.06l-4.5 4.5a.75.75 0 0 1-1.06 0l-4.5-4.5a.75.75 0 0 1 1.06-1.06l3.22 3.22V2.75A.75.75 0 0 1 8 2Z"
						clip-rule="evenodd"
					/>
				</svg>
				<span>{formatTokenCount($chatTokenStats.last_output_tokens)}</span>
			</span>

			<span class="text-gray-300 dark:text-gray-600">·</span>

			<!-- Cached input tokens -->
			<span
				class="flex items-center gap-0.5 font-medium text-book-cloth dark:text-kraft"
				title={$i18n.t('Cached input tokens')}
			>
				<span>R</span>
				<span>{formatTokenCount($chatTokenStats.last_cache_read_tokens)}</span>
			</span>

			<span class="text-gray-300 dark:text-gray-600">·</span>

			<!-- Total request tokens -->
			<span
				class="flex items-center gap-0.5 font-medium text-gray-600 dark:text-gray-300"
				title={$i18n.t('Total request tokens')}
			>
				<svg
					xmlns="http://www.w3.org/2000/svg"
					viewBox="0 0 16 16"
					fill="currentColor"
					class="size-3"
				>
					<path
						d="M13.488 2.513a1.75 1.75 0 0 0-2.475 0L6.75 6.774a2.75 2.75 0 0 0-.596.892l-.848 2.047a.75.75 0 0 0 .98.98l2.047-.848a2.75 2.75 0 0 0 .892-.596l4.261-4.262a1.75 1.75 0 0 0 0-2.474Z"
					/>
					<path
						d="M4.75 3.5c-.69 0-1.25.56-1.25 1.25v6.5c0 .69.56 1.25 1.25 1.25h6.5c.69 0 1.25-.56 1.25-1.25V9A.75.75 0 0 1 14 9v2.25A2.75 2.75 0 0 1 11.25 14h-6.5A2.75 2.75 0 0 1 2 11.25v-6.5A2.75 2.75 0 0 1 4.75 2H7a.75.75 0 0 1 0 1.5H4.75Z"
					/>
				</svg>
				<span>{formatTokenCount($chatTokenStats.total_tokens)}</span>
			</span>

			{#if ($chatTokenStats.cost ?? 0) > 0}
				<span class="text-gray-300 dark:text-gray-600">·</span>

				<!-- Estimated cost -->
				<span
					class="flex items-center gap-0.5 font-semibold text-success dark:text-success-dark"
					title={$i18n.t('Estimated cost')}
				>
					<span>{formatCost($chatTokenStats.cost)}</span>
				</span>
			{/if}
		</div>
	</Tooltip>
{:else if $chatTokenStats?.loading}
	<div
		class="flex items-center gap-1.5 px-2 py-1 rounded-lg text-[11px] font-mono text-gray-400 dark:text-gray-500 bg-gray-50 dark:bg-gray-850 border-hairline border-gray-100 dark:border-gray-800 animate-pulse"
	>
		<span>···</span>
	</div>
{/if}
