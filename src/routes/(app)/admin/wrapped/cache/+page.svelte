<script lang="ts">
	import { getContext, onMount } from 'svelte';
	import type { Writable } from 'svelte/store';
	import { goto } from '$app/navigation';
	import { user, WEBUI_NAME } from '$lib/stores';
	import { getGlobalCacheAnalytics, type CacheAnalytics } from '$lib/apis/analytics';
	import CacheIntelligence from '$lib/components/analytics/CacheIntelligence.svelte';

	const i18n =
		getContext<Writable<{ t: (key: string, options?: Record<string, unknown>) => string }>>('i18n');

	type RangePreset = 'year' | '7d' | '30d' | 'month' | 'all' | 'custom';
	let rangePreset: RangePreset = $state('all');
	let customStart = $state('');
	let customEnd = $state('');
	let selectedYear = $state(new Date().getFullYear());
	const currentYear = new Date().getFullYear();
	const availableYears = Array.from({ length: 5 }, (_, i) => currentYear - i);

	let groupBy: 'gateway' | 'vendor' | 'model' = $state('gateway');
	const GROUP_OPTIONS: ['gateway' | 'vendor' | 'model', string][] = [
		['gateway', 'Gateway'],
		['vendor', 'Vendor'],
		['model', 'Model']
	];

	let mounted = $state(false);
	let loading = $state(true);
	let error: string | null = $state(null);
	let data: CacheAnalytics | null = $state(null);
	let loadSeq = 0;
	let loadedKey: string | null = $state(null);

	onMount(() => {
		mounted = true;
		if (!$user || $user.role !== 'admin') {
			goto('/');
			return;
		}
		load();
	});

	function resolveWindow(): { start_ts: number; end_ts: number } | null {
		const now = Date.now();
		const dayMs = 86400000;
		if (rangePreset === 'year') return null;
		if (rangePreset === '7d')
			return { start_ts: Math.floor((now - 7 * dayMs) / 1000), end_ts: Math.floor(now / 1000) + 1 };
		if (rangePreset === '30d')
			return {
				start_ts: Math.floor((now - 30 * dayMs) / 1000),
				end_ts: Math.floor(now / 1000) + 1
			};
		if (rangePreset === 'month') {
			const d = new Date();
			const start = new Date(d.getFullYear(), d.getMonth(), 1);
			return { start_ts: Math.floor(start.getTime() / 1000), end_ts: Math.floor(now / 1000) + 1 };
		}
		if (rangePreset === 'all') return { start_ts: 0, end_ts: Math.floor(now / 1000) + 1 };
		if (customStart && customEnd) {
			const s = new Date(customStart + 'T00:00:00Z').getTime();
			const e = new Date(customEnd + 'T23:59:59Z').getTime();
			if (!isNaN(s) && !isNaN(e) && e >= s)
				return { start_ts: Math.floor(s / 1000), end_ts: Math.floor(e / 1000) };
		}
		return null;
	}

	async function load() {
		const seq = ++loadSeq;
		const key = rangeKey; // snapshot: a control changed mid-load must still refetch
		loading = true;
		error = null;
		try {
			const token = localStorage.getItem('token');
			if (!token) {
				error = 'Not authenticated';
				return;
			}
			const win = resolveWindow();
			const yearArg = win ? undefined : selectedYear;
			const res = await getGlobalCacheAnalytics(token, groupBy, yearArg, win ?? undefined);
			if (seq !== loadSeq) return;
			data = res;
			if (!res) error = 'Failed to load cache analytics';
		} catch (e) {
			console.error(e);
			error = 'Failed to load cache analytics';
		} finally {
			// Mark the key attempted on EVERY exit (incl. no-token return / throw) so
			// the reactive guard can't re-fire load() in a tight loop and freeze the page.
			if (seq === loadSeq) {
				loadedKey = key;
				loading = false;
			}
		}
	}

	let rangeKey = $derived(
		`${rangePreset}:${rangePreset === 'custom' ? `${customStart}-${customEnd}` : ''}:${selectedYear}:${groupBy}`
	);
	$effect(() => {
		if (mounted && loadedKey !== null && rangeKey !== loadedKey && !loading) load();
	});
	let rangeLabel = $derived(
		rangePreset === 'year'
			? `${selectedYear}`
			: rangePreset === '7d'
				? $i18n.t('Last 7 days')
				: rangePreset === '30d'
					? $i18n.t('Last 30 days')
					: rangePreset === 'month'
						? $i18n.t('This month')
						: rangePreset === 'all'
							? $i18n.t('All time')
							: $i18n.t('Custom range')
	);
</script>

<svelte:head>
	<title>Cache Intelligence | {$WEBUI_NAME}</title>
</svelte:head>

<div class="min-h-screen bg-gray-50 text-gray-900 dark:bg-gray-950 dark:text-gray-100">
	<div class="mx-auto max-w-7xl px-4 py-8 sm:px-6 lg:px-8">
		<header class="mb-8 flex flex-col gap-5 md:flex-row md:items-end md:justify-between">
			<div>
				<button
					class="mb-5 inline-flex items-center gap-2 text-sm font-medium text-gray-500 transition hover:text-gray-900 dark:text-gray-400 dark:hover:text-white"
					onclick={() => goto('/admin/wrapped')}
				>
					<span aria-hidden="true">←</span>
					{$i18n.t('Admin analytics')}
				</button>
				<div class="flex flex-wrap items-center gap-3">
					<h1 class="text-4xl font-semibold tracking-tight md:text-5xl">
						{$i18n.t('Cache intelligence')}
					</h1>
					<span
						class="rounded-full border-hairline border-book-cloth/30 bg-book-cloth/10 px-3 py-1 text-xs font-semibold uppercase tracking-wide text-book-cloth dark:text-kraft"
					>
						{$i18n.t('Cache lifetime')}
					</span>
				</div>
				<p class="mt-3 max-w-2xl text-sm leading-6 text-gray-600 dark:text-gray-400">
					{$i18n.t(
						'How long prompt caches actually survive per provider and model — and the dollars caching saves. Tool-loop and conversational reuse are measured separately.'
					)}
				</p>
			</div>

			<div class="flex flex-wrap items-center gap-3">
				<div
					class="inline-flex rounded-xl border-hairline border-gray-200 bg-gray-50 p-1 text-sm dark:border-gray-800 dark:bg-gray-900"
				>
					{#each GROUP_OPTIONS as [val, label]}
						<button
							class="rounded-lg px-3 py-1.5 font-medium transition-colors duration-200 ease-paper {groupBy ===
							val
								? 'bg-white text-gray-900 shadow-sm dark:bg-gray-800 dark:text-white'
								: 'text-gray-500 hover:text-gray-900 dark:text-gray-400 dark:hover:text-white'}"
							onclick={() => (groupBy = val)}
						>
							{$i18n.t(label)}
						</button>
					{/each}
				</div>

				<select
					bind:value={rangePreset}
					class="rounded-lg border-hairline border-gray-200 bg-white px-4 py-2 text-sm font-semibold shadow-sm outline-none transition-colors duration-200 ease-paper focus:border-book-cloth focus:ring-4 focus:ring-book-cloth/10 dark:border-gray-800 dark:bg-gray-900"
				>
					<option value="all">{$i18n.t('All time')}</option>
					<option value="year">{$i18n.t('Year')}</option>
					<option value="30d">{$i18n.t('Last 30 days')}</option>
					<option value="7d">{$i18n.t('Last 7 days')}</option>
					<option value="month">{$i18n.t('This month')}</option>
					<option value="custom">{$i18n.t('Custom')}</option>
				</select>

				{#if rangePreset === 'year'}
					<select
						bind:value={selectedYear}
						class="rounded-lg border-hairline border-gray-200 bg-white px-4 py-2 text-sm font-semibold shadow-sm outline-none transition-colors duration-200 ease-paper focus:border-book-cloth focus:ring-4 focus:ring-book-cloth/10 dark:border-gray-800 dark:bg-gray-900"
					>
						{#each availableYears as year}
							<option value={year}>{year}</option>
						{/each}
					</select>
				{:else if rangePreset === 'custom'}
					<input
						type="date"
						bind:value={customStart}
						class="rounded-lg border-hairline border-gray-200 bg-white px-3 py-2 text-sm shadow-sm outline-none transition-colors duration-200 ease-paper focus:border-book-cloth focus:ring-4 focus:ring-book-cloth/10 dark:border-gray-800 dark:bg-gray-900"
					/>
					<span class="text-gray-400">→</span>
					<input
						type="date"
						bind:value={customEnd}
						class="rounded-lg border-hairline border-gray-200 bg-white px-3 py-2 text-sm shadow-sm outline-none transition-colors duration-200 ease-paper focus:border-book-cloth focus:ring-4 focus:ring-book-cloth/10 dark:border-gray-800 dark:bg-gray-900"
					/>
				{/if}
			</div>
		</header>

		{#if error}
			<div
				class="rounded-2xl border-hairline border-error-brick/20 bg-error-brick/10 p-8 text-error-brick dark:text-error-brick-dark"
			>
				<div class="text-lg font-semibold">{$i18n.t('Couldn’t load cache analytics')}</div>
				<div class="mt-1 text-sm">{error}</div>
			</div>
		{:else}
			<div class="mb-4 text-sm text-gray-500 dark:text-gray-400">{rangeLabel}</div>
			<CacheIntelligence {data} {loading} {groupBy} />
		{/if}
	</div>
</div>
