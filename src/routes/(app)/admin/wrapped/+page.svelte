<script lang="ts">
	import { getContext, onMount } from 'svelte';
	import type { Writable } from 'svelte/store';
	import { goto } from '$app/navigation';
	import { user } from '$lib/stores';
	import {
		formatTokenCount,
		formatCost,
		getGlobalHeatmap,
		getGlobalModelUsage,
		getGlobalSubagentUsage,
		getGlobalUserUsage,
		getGlobalWrapped,
		getGlobalSpend,
		getGlobalSpendTrend,
		getTopChatsByCost,
		type GlobalWrappedSummary,
		type HeatmapDataPoint,
		type HeatmapResponse,
		type ModelUsage,
		type SubagentAnalytics,
		type UserUsage,
		type TotalSpend,
		type DailySpendPoint,
		type TopChat
	} from '$lib/apis/analytics';

	const i18n = getContext<Writable<{ t: (key: string, options?: Record<string, unknown>) => string }>>('i18n');

	let selectedYear: number = new Date().getFullYear();
	const currentYear = new Date().getFullYear();
	const availableYears = Array.from({ length: 5 }, (_, i) => currentYear - i);

	// Date-range selection. 'year' preserves the original year-scoped behavior
	// (and keeps the heatmap, which is year-only). Other presets / custom drive
	// an explicit [start_ts, end_ts) window on the cost + usage endpoints.
	type RangePreset = 'year' | '7d' | '30d' | 'month' | 'all' | 'custom';
	let rangePreset: RangePreset = 'year';
	let customStart = '';
	let customEnd = '';

	let mounted = false;
	let loadedKey: string | null = null;
	let loading = true;
	let error: string | null = null;

	let wrapped: GlobalWrappedSummary | null = null;
	let heatmap: HeatmapResponse | null = null;
	let modelUsage: ModelUsage[] = [];
	let userUsage: UserUsage[] = [];
	let subagents: SubagentAnalytics | null = null;
	let spend: TotalSpend | null = null;
	let spendTrend: DailySpendPoint[] = [];
	let topChatsByCost: TopChat[] = [];

	let search = '';
	let sortKey: 'tokens' | 'cache' | 'messages' | 'days' | 'avg' | 'cost' = 'tokens';
	let selectedUserId: string | null = null;
	let loadSeq = 0;

	const monthNames = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
	const dayNames = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
	const heatColors = [
		'bg-gray-200 dark:bg-gray-800',
		'bg-rose-200 dark:bg-rose-950',
		'bg-orange-300 dark:bg-orange-900',
		'bg-red-400 dark:bg-red-800',
		'bg-fuchsia-500 dark:bg-fuchsia-700'
	];

	onMount(() => {
		mounted = true;
		if (!$user || $user.role !== 'admin') goto('/');
		loadAll();
	});

	// Resolve the active window. Returns null for 'year' (year-scoped path), else
	// a [start_ts, end_ts) in unix seconds. Local dates are converted to UTC.
	function resolveWindow(): { start_ts: number; end_ts: number } | null {
		const now = Date.now();
		const dayMs = 86400000;
		if (rangePreset === 'year') return null;
		if (rangePreset === '7d') return { start_ts: Math.floor((now - 7 * dayMs) / 1000), end_ts: Math.floor(now / 1000) + 1 };
		if (rangePreset === '30d') return { start_ts: Math.floor((now - 30 * dayMs) / 1000), end_ts: Math.floor(now / 1000) + 1 };
		if (rangePreset === 'month') {
			const d = new Date();
			const start = new Date(d.getFullYear(), d.getMonth(), 1);
			return { start_ts: Math.floor(start.getTime() / 1000), end_ts: Math.floor(now / 1000) + 1 };
		}
		if (rangePreset === 'all') return { start_ts: 0, end_ts: Math.floor(now / 1000) + 1 };
		// custom
		if (customStart && customEnd) {
			const s = new Date(customStart + 'T00:00:00Z').getTime();
			const e = new Date(customEnd + 'T23:59:59Z').getTime();
			if (!isNaN(s) && !isNaN(e) && e >= s) {
				return { start_ts: Math.floor(s / 1000), end_ts: Math.floor(e / 1000) };
			}
		}
		return null;
	}

	$: rangeKey = `${rangePreset}:${rangePreset === 'custom' ? `${customStart}-${customEnd}` : ''}:${selectedYear}`;
	$: if (mounted && loadedKey !== null && rangeKey !== loadedKey && !loading) {
		loadAll();
	}

	async function loadAll() {
		const seq = ++loadSeq;
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

			const [wrappedData, heatmapData, modelData, userData, subagentData, spendData, trendData, topChatsData] =
				await Promise.all([
					getGlobalWrapped(token, selectedYear),
					getGlobalHeatmap(token, selectedYear),
					getGlobalModelUsage(token, 25, yearArg, win ?? undefined),
					getGlobalUserUsage(token, yearArg, 500, win ?? undefined),
					getGlobalSubagentUsage(token, selectedYear),
					getGlobalSpend(token, yearArg, win ?? undefined),
					getGlobalSpendTrend(token, yearArg, win ?? undefined),
					getTopChatsByCost(token, 10, yearArg, win ?? undefined)
				]);

			if (seq !== loadSeq) return;
			wrapped = wrappedData;
			heatmap = heatmapData;
			modelUsage = modelData ?? [];
			userUsage = userData ?? [];
			subagents = subagentData;
			spend = spendData;
			spendTrend = trendData ?? [];
			topChatsByCost = topChatsData ?? [];
			selectedUserId = userUsage[0]?.user_id ?? null;
			loadedKey = rangeKey;
		} catch (e) {
			console.error(e);
			error = 'Failed to load admin analytics';
		} finally {
			if (seq === loadSeq) loading = false;
		}
	}

	function pct(part: number, total: number) {
		return total > 0 ? Math.min(100, Math.max(0, (part / total) * 100)) : 0;
	}

	function fmtPct(value: number) {
		return `${value.toFixed(value >= 10 ? 0 : 1)}%`;
	}

	function cachePct(item: { total_cache_read_tokens?: number; total_input_tokens?: number }) {
		return pct(item.total_cache_read_tokens ?? 0, item.total_input_tokens ?? 0);
	}

	function fullNumber(value: number | null | undefined) {
		return (value ?? 0).toLocaleString();
	}

	function modelName(modelId: string | null | undefined) {
		if (!modelId) return 'Unknown model';
		return modelId.split('/').pop() || modelId;
	}

	function userLabel(u: UserUsage | null | undefined) {
		if (!u) return 'Unknown user';
		return u.name || u.email || u.user_id;
	}

	function shortUserId(id: string) {
		return id.length > 10 ? `${id.slice(0, 8)}…` : id;
	}

	function dateKey(date: Date) {
		const y = date.getFullYear();
		const m = `${date.getMonth() + 1}`.padStart(2, '0');
		const d = `${date.getDate()}`.padStart(2, '0');
		return `${y}-${m}-${d}`;
	}

	function asDate(date: string) {
		return new Date(`${date}T00:00:00`);
	}

	function shortDate(date: string) {
		return asDate(date).toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
	}

	function buildWeeks(year: number, data: HeatmapDataPoint[]) {
		const byDate = new Map(data.map((d) => [d.date, d]));
		const start = new Date(year, 0, 1);
		start.setDate(start.getDate() - start.getDay());
		const end = new Date(year, 11, 31);
		end.setDate(end.getDate() + (6 - end.getDay()));

		const weeks: { date: string; tokens: number; level: number; inYear: boolean }[][] = [];
		let week: { date: string; tokens: number; level: number; inYear: boolean }[] = [];
		const cursor = new Date(start);

		while (cursor <= end) {
			const key = dateKey(cursor);
			const point = byDate.get(key);
			week.push({
				date: key,
				tokens: point?.tokens ?? 0,
				level: point?.level ?? 0,
				inYear: cursor.getFullYear() === year
			});
			if (week.length === 7) {
				weeks.push(week);
				week = [];
			}
			cursor.setDate(cursor.getDate() + 1);
		}
		return weeks;
	}

	function topDays(data: HeatmapDataPoint[]) {
		return [...data].filter((d) => d.tokens > 0).sort((a, b) => b.tokens - a.tokens).slice(0, 7);
	}

	function monthTotals(data: HeatmapDataPoint[]) {
		const totals = Array(12).fill(0);
		for (const day of data) totals[asDate(day.date).getMonth()] += day.tokens;
		return totals.map((tokens, index) => ({ month: monthNames[index], tokens }));
	}

	function weekdayTotals(data: HeatmapDataPoint[]) {
		const totals = Array(7).fill(0);
		for (const day of data) totals[asDate(day.date).getDay()] += day.tokens;
		return totals.map((tokens, index) => ({ day: dayNames[index], tokens }));
	}

	function sortValue(u: UserUsage) {
		if (sortKey === 'cache') return u.total_cache_read_tokens;
		if (sortKey === 'messages') return u.message_count;
		if (sortKey === 'days') return u.days_active;
		if (sortKey === 'avg') return u.avg_tokens_per_message;
		if (sortKey === 'cost') return u.cost ?? 0;
		return u.total_tokens;
	}

	$: daily = heatmap?.data ?? [];
	$: weeks = buildWeeks(selectedYear, daily);
	$: days = topDays(daily);
	$: months = monthTotals(daily);
	$: weekdays = weekdayTotals(daily);
	$: maxMonth = Math.max(...months.map((m) => m.tokens), 0);
	$: maxWeekday = Math.max(...weekdays.map((d) => d.tokens), 0);
	$: totalTokens = wrapped?.total_tokens ?? 0;
	$: totalCache = wrapped?.total_cache_read_tokens ?? 0;
	$: totalInput = userUsage.reduce((sum, u) => sum + u.total_input_tokens, 0);
	$: totalOutput = userUsage.reduce((sum, u) => sum + u.total_output_tokens, 0);
	$: activeUsers = wrapped?.total_users_active ?? 0;
	$: totalMessages = wrapped?.total_messages ?? 0;
	$: cacheRate = pct(totalCache, totalInput);
	$: avgPerUser = activeUsers > 0 ? Math.round(totalTokens / activeUsers) : 0;
	$: avgPerMessage = totalMessages > 0 ? Math.round(totalTokens / totalMessages) : 0;
	$: topUser = userUsage[0];
	$: topUserShare = topUser ? pct(topUser.total_tokens, totalTokens) : 0;
	$: matchingUsers = userUsage
		.filter((u) => {
			const q = search.trim().toLowerCase();
			if (!q) return true;
			return `${u.name ?? ''} ${u.email ?? ''} ${u.user_id}`.toLowerCase().includes(q);
		})
		.sort((a, b) => sortValue(b) - sortValue(a));
	$: selectedUser = userUsage.find((u) => u.user_id === selectedUserId) ?? matchingUsers[0] ?? null;
	$: roleBreakdown = Object.entries(
		userUsage.reduce((acc, u) => {
			const role = u.role || 'unknown';
			acc[role] = (acc[role] ?? 0) + 1;
			return acc;
		}, {} as Record<string, number>)
	).map(([role, count]) => ({ role, count }));
	$: cacheUsers = [...userUsage]
		.filter((u) => u.total_cache_read_tokens > 0)
		.sort((a, b) => b.total_cache_read_tokens - a.total_cache_read_tokens)
		.slice(0, 8);
	$: cacheModels = [...modelUsage]
		.filter((m) => (m.total_cache_read_tokens ?? 0) > 0)
		.sort((a, b) => (b.total_cache_read_tokens ?? 0) - (a.total_cache_read_tokens ?? 0))
		.slice(0, 8);
	$: subagentStatuses = Object.entries(subagents?.status_counts ?? {}).sort((a, b) => b[1] - a[1]);
	$: totalCost = spend?.total_cost ?? 0;
	$: maxTrendCost = Math.max(...spendTrend.map((p) => p.cost), 0);
	$: rangeLabel =
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
							: $i18n.t('Custom range');
</script>

<svelte:head>
	<title>Admin {selectedYear} Analytics | Open WebUI</title>
</svelte:head>

<div class="min-h-screen bg-gray-50 text-gray-900 dark:bg-gray-950 dark:text-gray-100">
	<div class="mx-auto max-w-7xl px-4 py-8 sm:px-6 lg:px-8">
		<header class="mb-8 flex flex-col gap-5 md:flex-row md:items-end md:justify-between">
			<div>
				<button
					class="mb-5 inline-flex items-center gap-2 text-sm font-medium text-gray-500 transition hover:text-gray-900 dark:text-gray-400 dark:hover:text-white"
					on:click={() => goto('/admin')}
				>
					<span aria-hidden="true">←</span>
					{$i18n.t('Admin console')}
				</button>
				<div class="flex flex-wrap items-center gap-3">
					<h1 class="text-4xl font-semibold tracking-tight md:text-6xl">Admin analytics</h1>
					<span class="rounded-full border border-red-200 bg-red-50 px-3 py-1 text-xs font-semibold uppercase tracking-wide text-red-700 dark:border-red-900/70 dark:bg-red-950/60 dark:text-red-300">
						Site-wide
					</span>
				</div>
				<p class="mt-3 max-w-2xl text-sm leading-6 text-gray-600 dark:text-gray-400">
					Global usage, cache efficiency, model mix, active-user distribution, and per-user drilldown.
				</p>
			</div>

			<div class="flex flex-wrap items-center gap-3">
				<div class="flex items-center gap-2">
					<label for="range-select" class="text-sm font-medium text-gray-500 dark:text-gray-400">{$i18n.t('Range')}</label>
					<select
						id="range-select"
						bind:value={rangePreset}
						class="rounded-xl border border-gray-200 bg-white px-4 py-2 text-sm font-semibold shadow-sm outline-none transition focus:border-red-500 focus:ring-4 focus:ring-red-500/10 dark:border-gray-800 dark:bg-gray-900"
					>
						<option value="year">{$i18n.t('Year')}</option>
						<option value="7d">{$i18n.t('Last 7 days')}</option>
						<option value="30d">{$i18n.t('Last 30 days')}</option>
						<option value="month">{$i18n.t('This month')}</option>
						<option value="all">{$i18n.t('All time')}</option>
						<option value="custom">{$i18n.t('Custom')}</option>
					</select>
				</div>

				{#if rangePreset === 'year'}
					<select
						id="year-select"
						bind:value={selectedYear}
						class="rounded-xl border border-gray-200 bg-white px-4 py-2 text-sm font-semibold shadow-sm outline-none transition focus:border-red-500 focus:ring-4 focus:ring-red-500/10 dark:border-gray-800 dark:bg-gray-900"
					>
						{#each availableYears as year}
							<option value={year}>{year}</option>
						{/each}
					</select>
				{:else if rangePreset === 'custom'}
					<input
						type="date"
						bind:value={customStart}
						class="rounded-xl border border-gray-200 bg-white px-3 py-2 text-sm shadow-sm outline-none transition focus:border-red-500 dark:border-gray-800 dark:bg-gray-900"
					/>
					<span class="text-gray-400">→</span>
					<input
						type="date"
						bind:value={customEnd}
						class="rounded-xl border border-gray-200 bg-white px-3 py-2 text-sm shadow-sm outline-none transition focus:border-red-500 dark:border-gray-800 dark:bg-gray-900"
					/>
				{/if}

				<button
					class="rounded-xl border border-gray-200 bg-white px-4 py-2 text-sm font-semibold shadow-sm transition hover:bg-gray-50 dark:border-gray-800 dark:bg-gray-900 dark:hover:bg-gray-800"
					on:click={() => goto('/admin/settings/pricing')}
					title={$i18n.t('Manage model pricing')}
				>
					{$i18n.t('Pricing')}
				</button>
			</div>
		</header>

		{#if loading}
			<div class="grid gap-4 md:grid-cols-4">
				{#each Array(12) as _}
					<div class="h-36 animate-pulse rounded-3xl border border-gray-200 bg-white dark:border-gray-800 dark:bg-gray-900"></div>
				{/each}
			</div>
		{:else if error}
			<div class="rounded-3xl border border-red-200 bg-red-50 p-8 text-red-700 dark:border-red-900/70 dark:bg-red-950/40 dark:text-red-300">
				<div class="text-lg font-semibold">Couldn’t load admin analytics</div>
				<div class="mt-1 text-sm">{error}</div>
			</div>
		{:else if wrapped}
			<section class="mb-6 grid gap-4 md:grid-cols-2 xl:grid-cols-5">
				<div class="rounded-3xl border border-emerald-200 bg-emerald-50 p-6 shadow-sm dark:border-emerald-900/60 dark:bg-emerald-950/40">
					<div class="text-sm font-medium text-emerald-700 dark:text-emerald-300">{$i18n.t('Total spend')}</div>
					<div class="mt-3 text-4xl font-semibold tracking-tight text-emerald-700 dark:text-emerald-200">{formatCost(totalCost)}</div>
					<div class="mt-4 text-xs text-emerald-700/80 dark:text-emerald-300/80">
						{$i18n.t('embedded')} {formatCost(spend?.embedded_cost ?? 0)} · {$i18n.t('rate-card')} {formatCost(spend?.rate_card_cost ?? 0)}
					</div>
					{#if (spend?.unpriced_model_count ?? 0) > 0}
						<button
							class="mt-2 text-xs font-medium text-amber-600 underline-offset-2 hover:underline dark:text-amber-400"
							on:click={() => goto('/admin/settings/pricing')}
						>
							{spend?.unpriced_model_count} {$i18n.t('models unpriced — map them')}
						</button>
					{/if}
				</div>
				<div class="rounded-3xl border border-gray-200 bg-white p-6 shadow-sm dark:border-gray-800 dark:bg-gray-900">
					<div class="text-sm font-medium text-gray-500 dark:text-gray-400">Global request tokens</div>
					<div class="mt-3 text-4xl font-semibold tracking-tight">{formatTokenCount(totalTokens)}</div>
					<div class="mt-4 text-xs text-gray-500 dark:text-gray-400">{fullNumber(totalTokens)} total tokens</div>
				</div>
				<div class="rounded-3xl border border-gray-200 bg-white p-6 shadow-sm dark:border-gray-800 dark:bg-gray-900">
					<div class="text-sm font-medium text-gray-500 dark:text-gray-400">Active users</div>
					<div class="mt-3 text-4xl font-semibold tracking-tight text-red-600 dark:text-red-300">{activeUsers.toLocaleString()}</div>
					<div class="mt-4 text-xs text-gray-500 dark:text-gray-400">Avg/user: {formatTokenCount(avgPerUser)}</div>
				</div>
				<div class="rounded-3xl border border-gray-200 bg-white p-6 shadow-sm dark:border-gray-800 dark:bg-gray-900">
					<div class="text-sm font-medium text-gray-500 dark:text-gray-400">Cache reads</div>
					<div class="mt-3 text-4xl font-semibold tracking-tight text-violet-600 dark:text-violet-300">{formatTokenCount(totalCache)}</div>
					<div class="mt-4 h-2 overflow-hidden rounded-full bg-gray-100 dark:bg-gray-800">
						<div class="h-full rounded-full bg-violet-500" style="width: {cacheRate}%"></div>
					</div>
					<div class="mt-2 text-xs text-gray-500 dark:text-gray-400">{fmtPct(cacheRate)} of prompt tokens cached</div>
				</div>
				<div class="rounded-3xl border border-gray-200 bg-white p-6 shadow-sm dark:border-gray-800 dark:bg-gray-900">
					<div class="text-sm font-medium text-gray-500 dark:text-gray-400">Messages</div>
					<div class="mt-3 text-4xl font-semibold tracking-tight text-emerald-600 dark:text-emerald-300">{formatTokenCount(totalMessages)}</div>
					<div class="mt-4 text-xs text-gray-500 dark:text-gray-400">Avg/message: {formatTokenCount(avgPerMessage)}</div>
				</div>
			</section>

			<section class="mb-6 grid gap-4 lg:grid-cols-3">
				<div class="rounded-3xl border border-gray-200 bg-white p-6 shadow-sm dark:border-gray-800 dark:bg-gray-900 lg:col-span-2">
					<div class="mb-5 flex items-center justify-between gap-4">
						<div>
							<h2 class="text-xl font-semibold">Site token flow</h2>
							<p class="text-sm text-gray-500 dark:text-gray-400">Prompt, output, and cache-reuse volume across all users.</p>
						</div>
						<div class="text-right text-sm text-gray-500 dark:text-gray-400">Top user share: {fmtPct(topUserShare)}</div>
					</div>
					<div class="space-y-5">
						<div>
							<div class="mb-2 flex justify-between text-sm"><span class="font-medium">Prompt tokens</span><span class="text-gray-500 dark:text-gray-400">{formatTokenCount(totalInput)}</span></div>
							<div class="h-3 overflow-hidden rounded-full bg-gray-100 dark:bg-gray-800"><div class="h-full rounded-full bg-blue-500" style="width: {pct(totalInput, totalTokens)}%"></div></div>
						</div>
						<div>
							<div class="mb-2 flex justify-between text-sm"><span class="font-medium">Completion tokens</span><span class="text-gray-500 dark:text-gray-400">{formatTokenCount(totalOutput)}</span></div>
							<div class="h-3 overflow-hidden rounded-full bg-gray-100 dark:bg-gray-800"><div class="h-full rounded-full bg-emerald-500" style="width: {pct(totalOutput, totalTokens)}%"></div></div>
						</div>
						<div>
							<div class="mb-2 flex justify-between text-sm"><span class="font-medium">Cached prompt reads</span><span class="text-gray-500 dark:text-gray-400">{formatTokenCount(totalCache)}</span></div>
							<div class="h-3 overflow-hidden rounded-full bg-gray-100 dark:bg-gray-800"><div class="h-full rounded-full bg-violet-500" style="width: {cacheRate}%"></div></div>
						</div>
					</div>
				</div>

				<div class="rounded-3xl border border-gray-200 bg-white p-6 shadow-sm dark:border-gray-800 dark:bg-gray-900">
					<h2 class="text-xl font-semibold">Population</h2>
					<div class="mt-5 space-y-3">
						{#each roleBreakdown as role}
							<div class="flex items-center justify-between rounded-2xl bg-gray-50 px-4 py-3 dark:bg-gray-950/60">
								<span class="text-sm font-medium capitalize">{role.role}</span>
								<span class="text-sm text-gray-500 dark:text-gray-400">{role.count}</span>
							</div>
						{/each}
						<div class="rounded-2xl bg-gray-50 p-4 dark:bg-gray-950/60">
							<div class="text-xs text-gray-500 dark:text-gray-400">Highest-volume user</div>
							<div class="mt-2 truncate text-lg font-semibold">{userLabel(topUser)}</div>
							<div class="mt-1 text-xs text-gray-500 dark:text-gray-400">{formatTokenCount(topUser?.total_tokens ?? 0)} tokens</div>
						</div>
					</div>
				</div>
			</section>

			{#if subagents}
				<section class="mb-6 grid gap-4 xl:grid-cols-[minmax(0,1.1fr)_minmax(0,1.4fr)]">
					<div class="rounded-3xl border border-gray-200 bg-white p-6 shadow-sm dark:border-gray-800 dark:bg-gray-900">
						<div class="mb-5 flex items-start justify-between gap-4">
							<div>
								<h2 class="text-xl font-semibold">Subagent usage</h2>
								<p class="text-sm text-gray-500 dark:text-gray-400">Hidden worker chats folded into visible parent conversations.</p>
							</div>
							<span class="rounded-full bg-violet-50 px-3 py-1 text-xs font-semibold text-violet-700 dark:bg-violet-950/50 dark:text-violet-300">{subagents.token_share_percent.toFixed(1)}% of tokens</span>
						</div>

						<div class="grid grid-cols-2 gap-3">
							<div class="rounded-2xl bg-gray-50 p-4 dark:bg-gray-950/60"><div class="text-xs text-gray-500 dark:text-gray-400">Subagents spawned</div><div class="mt-2 text-2xl font-semibold">{subagents.total_subagent_chats.toLocaleString()}</div></div>
							<div class="rounded-2xl bg-gray-50 p-4 dark:bg-gray-950/60"><div class="text-xs text-gray-500 dark:text-gray-400">Parent chats</div><div class="mt-2 text-2xl font-semibold">{subagents.parent_chat_count.toLocaleString()}</div></div>
							<div class="rounded-2xl bg-gray-50 p-4 dark:bg-gray-950/60"><div class="text-xs text-gray-500 dark:text-gray-400">Model calls</div><div class="mt-2 text-2xl font-semibold">{subagents.request_count.toLocaleString()}</div></div>
							<div class="rounded-2xl bg-gray-50 p-4 dark:bg-gray-950/60"><div class="text-xs text-gray-500 dark:text-gray-400">Avg/subagent</div><div class="mt-2 text-2xl font-semibold">{formatTokenCount(subagents.avg_tokens_per_subagent)}</div></div>
						</div>

						<div class="mt-5 space-y-3 text-sm">
							<div class="flex justify-between border-b border-gray-100 pb-2 dark:border-gray-800"><span class="text-gray-500 dark:text-gray-400">Total tokens</span><span>{formatTokenCount(subagents.total_tokens)}</span></div>
							<div class="flex justify-between border-b border-gray-100 pb-2 dark:border-gray-800"><span class="text-gray-500 dark:text-gray-400">Prompt / output</span><span>{formatTokenCount(subagents.total_input_tokens)} / {formatTokenCount(subagents.total_output_tokens)}</span></div>
							<div class="flex justify-between border-b border-gray-100 pb-2 dark:border-gray-800"><span class="text-gray-500 dark:text-gray-400">Cache reads</span><span>{formatTokenCount(subagents.total_cache_read_tokens)}</span></div>
							<div class="flex justify-between"><span class="text-gray-500 dark:text-gray-400">Avg requests/subagent</span><span>{subagents.avg_requests_per_subagent.toFixed(1)}</span></div>
						</div>

						{#if subagentStatuses.length > 0}
							<div class="mt-5">
								<div class="mb-2 text-sm font-medium text-gray-500 dark:text-gray-400">Run status</div>
								<div class="flex flex-wrap gap-2">
									{#each subagentStatuses as [status, count]}
										<span class="rounded-full bg-gray-100 px-3 py-1 text-xs font-medium capitalize dark:bg-gray-800">{status}: {count}</span>
									{/each}
								</div>
							</div>
						{/if}
					</div>

					<div class="rounded-3xl border border-gray-200 bg-white p-6 shadow-sm dark:border-gray-800 dark:bg-gray-900">
						<h2 class="text-xl font-semibold">Top subagent parent chats</h2>
						<div class="mt-5 space-y-3">
							{#each subagents.top_parent_chats.slice(0, 8) as chat}
								<button class="w-full rounded-2xl p-3 text-left transition hover:bg-gray-50 dark:hover:bg-gray-950/60" on:click={() => goto(`/c/${chat.chat_id}`)}>
									<div class="flex items-start justify-between gap-4">
										<div class="min-w-0"><div class="truncate text-sm font-medium">{chat.title}</div><div class="mt-1 text-xs text-gray-500 dark:text-gray-400">{chat.subagent_count} subagents · {chat.request_count} calls · {formatTokenCount(chat.total_cache_read_tokens ?? 0)} cached</div></div>
										<div class="shrink-0 text-sm font-semibold">{formatTokenCount(chat.total_tokens)}</div>
									</div>
								</button>
							{/each}
						</div>
					</div>
				</section>

				<section class="mb-6 grid gap-4 lg:grid-cols-2">
					<div class="rounded-3xl border border-gray-200 bg-white p-6 shadow-sm dark:border-gray-800 dark:bg-gray-900">
						<h2 class="text-xl font-semibold">Top individual subagents</h2>
						<div class="mt-5 space-y-3">
							{#each subagents.top_subagents.slice(0, 10) as sa}
								<a class="block rounded-2xl p-3 transition hover:bg-gray-50 dark:hover:bg-gray-950/60" href={`/c/${sa.subagent_chat_id}`} target="_blank" rel="noopener noreferrer">
									<div class="flex items-start justify-between gap-4"><div class="min-w-0"><div class="truncate text-sm font-medium">{sa.title}</div><div class="mt-1 truncate text-xs text-gray-500 dark:text-gray-400">parent: {sa.parent_title} · {sa.request_count} calls · {sa.source_type}</div></div><div class="shrink-0 text-sm font-semibold">{formatTokenCount(sa.total_tokens)}</div></div>
								</a>
							{/each}
						</div>
					</div>
					<div class="rounded-3xl border border-gray-200 bg-white p-6 shadow-sm dark:border-gray-800 dark:bg-gray-900">
						<h2 class="text-xl font-semibold">Subagent model mix</h2>
						<div class="mt-5 space-y-4">
							{#each subagents.top_models.slice(0, 8) as model}
								<div><div class="mb-1 flex items-center justify-between gap-3 text-sm"><span class="truncate font-medium">{modelName(model.model_id)}</span><span class="text-gray-500 dark:text-gray-400">{model.percentage.toFixed(1)}%</span></div><div class="h-2 overflow-hidden rounded-full bg-gray-100 dark:bg-gray-800"><div class="h-full rounded-full bg-violet-500" style="width: {model.percentage}%"></div></div><div class="mt-1 text-xs text-gray-500 dark:text-gray-400">{formatTokenCount(model.total_tokens)} tokens · {formatTokenCount(model.total_cache_read_tokens ?? 0)} cached · {fmtPct(cachePct(model))} of input</div></div>
							{/each}
						</div>
					</div>
				</section>
			{/if}

			<section class="mb-6 rounded-3xl border border-gray-200 bg-white p-6 shadow-sm dark:border-gray-800 dark:bg-gray-900">
				<div class="mb-5 flex flex-col gap-2 md:flex-row md:items-end md:justify-between">
					<div>
						<h2 class="text-xl font-semibold">Global activity heatmap</h2>
						<p class="text-sm text-gray-500 dark:text-gray-400">Busiest day: {wrapped.busiest_day ? `${shortDate(wrapped.busiest_day.date)} · ${formatTokenCount(wrapped.busiest_day.tokens)}` : '—'}</p>
					</div>
					<div class="flex items-center gap-1 text-xs text-gray-500 dark:text-gray-400"><span>Less</span>{#each heatColors as color}<span class="h-3 w-3 rounded-sm {color}"></span>{/each}<span>More</span></div>
				</div>
				<div class="overflow-x-auto pb-2">
					<div class="inline-flex min-w-max gap-1">
						{#each weeks as week}
							<div class="flex flex-col gap-1">
								{#each week as day}
									<div class="h-3 w-3 rounded-sm {day.inYear ? heatColors[day.level] : 'bg-transparent'}" title={`${day.date}: ${day.tokens ? fullNumber(day.tokens) + ' tokens' : 'No activity'}`}></div>
								{/each}
							</div>
						{/each}
					</div>
				</div>
			</section>

			<section class="mb-6 grid gap-4 lg:grid-cols-3">
				<div class="rounded-3xl border border-gray-200 bg-white p-6 shadow-sm dark:border-gray-800 dark:bg-gray-900">
					<h2 class="text-xl font-semibold">Top global days</h2>
					<div class="mt-5 space-y-3">
						{#each days as day, index}
							<div class="flex items-center gap-3">
								<div class="flex h-7 w-7 items-center justify-center rounded-full bg-gray-100 text-xs font-semibold dark:bg-gray-800">{index + 1}</div>
								<div class="min-w-0 flex-1"><div class="truncate text-sm font-medium">{shortDate(day.date)}</div><div class="mt-1 h-1.5 overflow-hidden rounded-full bg-gray-100 dark:bg-gray-800"><div class="h-full rounded-full bg-red-500" style="width: {pct(day.tokens, days[0]?.tokens ?? 0)}%"></div></div></div>
								<div class="text-sm font-semibold">{formatTokenCount(day.tokens)}</div>
							</div>
						{/each}
					</div>
				</div>

				<div class="rounded-3xl border border-gray-200 bg-white p-6 shadow-sm dark:border-gray-800 dark:bg-gray-900">
					<h2 class="text-xl font-semibold">Monthly load</h2>
					<div class="mt-5 space-y-2">
						{#each months as month}
							<div class="grid grid-cols-[2.5rem_1fr_4rem] items-center gap-3 text-sm"><span class="text-gray-500 dark:text-gray-400">{month.month}</span><div class="h-2 overflow-hidden rounded-full bg-gray-100 dark:bg-gray-800"><div class="h-full rounded-full bg-orange-500" style="width: {pct(month.tokens, maxMonth)}%"></div></div><span class="text-right text-xs text-gray-500 dark:text-gray-400">{formatTokenCount(month.tokens)}</span></div>
						{/each}
					</div>
				</div>

				<div class="rounded-3xl border border-gray-200 bg-white p-6 shadow-sm dark:border-gray-800 dark:bg-gray-900">
					<h2 class="text-xl font-semibold">Weekday load</h2>
					<div class="mt-5 space-y-3">
						{#each weekdays as day}
							<div class="grid grid-cols-[2.5rem_1fr_4rem] items-center gap-3 text-sm"><span class="text-gray-500 dark:text-gray-400">{day.day}</span><div class="h-2 overflow-hidden rounded-full bg-gray-100 dark:bg-gray-800"><div class="h-full rounded-full bg-fuchsia-500" style="width: {pct(day.tokens, maxWeekday)}%"></div></div><span class="text-right text-xs text-gray-500 dark:text-gray-400">{formatTokenCount(day.tokens)}</span></div>
						{/each}
					</div>
				</div>
			</section>

			<section class="mb-6 grid gap-4 lg:grid-cols-[minmax(0,1.5fr)_minmax(0,1fr)]">
				<div class="rounded-3xl border border-gray-200 bg-white p-6 shadow-sm dark:border-gray-800 dark:bg-gray-900">
					<div class="mb-5 flex items-center justify-between gap-4">
						<div>
							<h2 class="text-xl font-semibold">{$i18n.t('Cost over time')}</h2>
							<p class="text-sm text-gray-500 dark:text-gray-400">{$i18n.t('Daily spend')} · {rangeLabel}</p>
						</div>
						<div class="text-right">
							<div class="text-2xl font-semibold text-emerald-600 dark:text-emerald-400">{formatCost(totalCost)}</div>
							<div class="text-xs text-gray-500 dark:text-gray-400">{$i18n.t('total')}</div>
						</div>
					</div>
					{#if spendTrend.length > 0 && maxTrendCost > 0}
						<div class="flex h-40 items-end gap-px">
							{#each spendTrend as p}
								<div
									class="flex-1 rounded-t bg-gradient-to-t from-emerald-500 to-emerald-300 transition hover:opacity-80"
									style="height: {Math.max(2, (p.cost / maxTrendCost) * 100)}%"
									title="{p.date}: {formatCost(p.cost)}"
								></div>
							{/each}
						</div>
					{:else}
						<div class="flex h-40 items-center justify-center text-sm text-gray-400">{$i18n.t('No spend in range')}</div>
					{/if}
				</div>

				<div class="rounded-3xl border border-gray-200 bg-white p-6 shadow-sm dark:border-gray-800 dark:bg-gray-900">
					<h2 class="text-xl font-semibold">{$i18n.t('Most expensive chats')}</h2>
					<div class="mt-4 space-y-2">
						{#each topChatsByCost as chat, i}
							<button
								class="flex w-full items-center justify-between gap-3 rounded-2xl bg-gray-50 px-4 py-2.5 text-left transition hover:bg-gray-100 dark:bg-gray-950/60 dark:hover:bg-gray-800"
								on:click={() => goto(`/c/${chat.chat_id}`)}
							>
								<span class="flex min-w-0 items-center gap-2">
									<span class="text-xs font-semibold text-gray-400">{i + 1}</span>
									<span class="truncate text-sm font-medium">{chat.title || $i18n.t('Untitled')}</span>
								</span>
								<span class="shrink-0 text-sm font-semibold text-emerald-600 dark:text-emerald-400">{formatCost(chat.cost)}</span>
							</button>
						{:else}
							<div class="py-8 text-center text-sm text-gray-400">{$i18n.t('No chats in range')}</div>
						{/each}
					</div>
				</div>
			</section>

			<section class="mb-6 grid gap-4 lg:grid-cols-2">
				<div class="rounded-3xl border border-gray-200 bg-white p-6 shadow-sm dark:border-gray-800 dark:bg-gray-900">
					<h2 class="text-xl font-semibold">Model leaderboard</h2>
					<div class="mt-5 space-y-4">
						{#each modelUsage.slice(0, 10) as model}
							<div>
								<div class="mb-1 flex items-center justify-between gap-3 text-sm">
									<span class="truncate font-medium">{modelName(model.model_id)}</span>
									<span class="flex items-center gap-2">
										{#if (model.unpriced_tokens ?? 0) > 0}
											<span class="rounded-full bg-amber-100 px-2 py-0.5 text-[10px] font-semibold text-amber-700 dark:bg-amber-950/60 dark:text-amber-300">unmapped</span>
										{:else}
											<span class="font-semibold text-emerald-600 dark:text-emerald-400">{formatCost(model.cost)}</span>
										{/if}
										<span class="text-gray-500 dark:text-gray-400">{model.percentage.toFixed(1)}%</span>
									</span>
								</div>
								<div class="h-2 overflow-hidden rounded-full bg-gray-100 dark:bg-gray-800"><div class="h-full rounded-full bg-gradient-to-r from-red-500 to-fuchsia-500" style="width: {model.percentage}%"></div></div>
								<div class="mt-1 text-xs text-gray-500 dark:text-gray-400">{formatTokenCount(model.total_tokens)} tokens · {formatTokenCount(model.total_cache_read_tokens ?? 0)} cached · {fmtPct(cachePct(model))} of input</div>
							</div>
						{/each}
					</div>
				</div>

				<div class="rounded-3xl border border-gray-200 bg-white p-6 shadow-sm dark:border-gray-800 dark:bg-gray-900">
					<h2 class="text-xl font-semibold">Cache leaders</h2>
					<div class="mt-5 grid gap-5 md:grid-cols-2">
						<div class="space-y-3">
							<div class="text-sm font-medium text-gray-500 dark:text-gray-400">Users</div>
							{#each cacheUsers as u}
								<div><div class="mb-1 flex justify-between gap-3 text-sm"><span class="truncate font-medium">{userLabel(u)}</span><span class="text-gray-500 dark:text-gray-400">{formatTokenCount(u.total_cache_read_tokens)}</span></div><div class="h-1.5 overflow-hidden rounded-full bg-gray-100 dark:bg-gray-800"><div class="h-full rounded-full bg-violet-500" style="width: {pct(u.total_cache_read_tokens, cacheUsers[0]?.total_cache_read_tokens ?? 0)}%"></div></div></div>
							{/each}
						</div>
						<div class="space-y-3">
							<div class="text-sm font-medium text-gray-500 dark:text-gray-400">Models</div>
							{#each cacheModels as m}
								<div><div class="mb-1 flex justify-between gap-3 text-sm"><span class="truncate font-medium">{modelName(m.model_id)}</span><span class="text-gray-500 dark:text-gray-400">{formatTokenCount(m.total_cache_read_tokens)} · {fmtPct(cachePct(m))}</span></div><div class="h-1.5 overflow-hidden rounded-full bg-gray-100 dark:bg-gray-800"><div class="h-full rounded-full bg-violet-500" style="width: {pct(m.total_cache_read_tokens, cacheModels[0]?.total_cache_read_tokens ?? 0)}%"></div></div></div>
							{/each}
						</div>
					</div>
				</div>
			</section>

			<section class="grid gap-4 xl:grid-cols-[minmax(0,1.6fr)_minmax(360px,0.9fr)]">
				<div class="rounded-3xl border border-gray-200 bg-white p-6 shadow-sm dark:border-gray-800 dark:bg-gray-900">
					<div class="mb-5 flex flex-col gap-3 md:flex-row md:items-center md:justify-between">
						<div><h2 class="text-xl font-semibold">Per-user analytics</h2><p class="text-sm text-gray-500 dark:text-gray-400">Search, sort, and click a user for details.</p></div>
						<div class="flex flex-col gap-2 sm:flex-row">
							<input bind:value={search} placeholder="Search user/email/id" class="rounded-xl border border-gray-200 bg-white px-3 py-2 text-sm outline-none focus:border-red-500 focus:ring-4 focus:ring-red-500/10 dark:border-gray-800 dark:bg-gray-950" />
							<select bind:value={sortKey} class="rounded-xl border border-gray-200 bg-white px-3 py-2 text-sm outline-none focus:border-red-500 focus:ring-4 focus:ring-red-500/10 dark:border-gray-800 dark:bg-gray-950">
								<option value="tokens">Sort: tokens</option>
								<option value="cost">Sort: cost</option>
								<option value="cache">Sort: cache</option>
								<option value="messages">Sort: messages</option>
								<option value="days">Sort: days</option>
								<option value="avg">Sort: avg/msg</option>
							</select>
						</div>
					</div>

					<div class="overflow-x-auto">
						<table class="w-full min-w-[820px] text-left text-sm">
							<thead class="text-xs uppercase tracking-wide text-gray-500 dark:text-gray-400"><tr><th class="py-3 pr-3">User</th><th class="px-3 py-3 text-right">Cost</th><th class="px-3 py-3 text-right">Tokens</th><th class="px-3 py-3 text-right">Cache</th><th class="px-3 py-3 text-right">Messages</th><th class="px-3 py-3 text-right">Chats</th><th class="px-3 py-3 text-right">Days</th></tr></thead>
							<tbody class="divide-y divide-gray-100 dark:divide-gray-800">
								{#each matchingUsers.slice(0, 100) as u}
									<tr
										class="cursor-pointer transition hover:bg-gray-50 dark:hover:bg-gray-950/70 {selectedUser?.user_id === u.user_id ? 'bg-red-50 dark:bg-red-950/20' : ''}"
										on:click={() => (selectedUserId = u.user_id)}
									>
										<td class="py-3 pr-3"><div class="font-medium">{userLabel(u)}</div><div class="text-xs text-gray-500 dark:text-gray-400">{u.email || shortUserId(u.user_id)}</div></td>
										<td class="px-3 py-3 text-right font-semibold text-emerald-600 dark:text-emerald-400">{formatCost(u.cost ?? 0)}</td>
										<td class="px-3 py-3 text-right font-medium">{formatTokenCount(u.total_tokens)}</td>
										<td class="px-3 py-3 text-right text-violet-600 dark:text-violet-300">{formatTokenCount(u.total_cache_read_tokens)}</td>
										<td class="px-3 py-3 text-right">{u.message_count.toLocaleString()}</td>
										<td class="px-3 py-3 text-right">{u.conversation_count.toLocaleString()}</td>
										<td class="px-3 py-3 text-right">{u.days_active}</td>
									</tr>
								{/each}
							</tbody>
						</table>
					</div>
				</div>

				<div class="rounded-3xl border border-gray-200 bg-white p-6 shadow-sm dark:border-gray-800 dark:bg-gray-900">
					<h2 class="text-xl font-semibold">User drilldown</h2>
					{#if selectedUser}
						<div class="mt-5">
							<div class="text-2xl font-semibold">{userLabel(selectedUser)}</div>
							<div class="mt-1 text-sm text-gray-500 dark:text-gray-400">{selectedUser.email || selectedUser.user_id}</div>
							<div class="mt-5 grid grid-cols-2 gap-3">
								<div class="rounded-2xl bg-emerald-50 p-4 dark:bg-emerald-950/40"><div class="text-xs text-emerald-700 dark:text-emerald-300">Spend</div><div class="mt-2 text-2xl font-semibold text-emerald-700 dark:text-emerald-200">{formatCost(selectedUser.cost ?? 0)}</div></div>
								<div class="rounded-2xl bg-gray-50 p-4 dark:bg-gray-950/60"><div class="text-xs text-gray-500 dark:text-gray-400">Tokens</div><div class="mt-2 text-2xl font-semibold">{formatTokenCount(selectedUser.total_tokens)}</div></div>
								<div class="rounded-2xl bg-gray-50 p-4 dark:bg-gray-950/60"><div class="text-xs text-gray-500 dark:text-gray-400">Cache</div><div class="mt-2 text-2xl font-semibold text-violet-600 dark:text-violet-300">{formatTokenCount(selectedUser.total_cache_read_tokens)}</div></div>
								<div class="rounded-2xl bg-gray-50 p-4 dark:bg-gray-950/60"><div class="text-xs text-gray-500 dark:text-gray-400">Cache rate</div><div class="mt-2 text-2xl font-semibold">{selectedUser.cache_read_rate.toFixed(1)}%</div></div>
							</div>
							<div class="mt-5 space-y-3 text-sm">
								<div class="flex justify-between border-b border-gray-100 pb-2 dark:border-gray-800"><span class="text-gray-500 dark:text-gray-400">Prompt</span><span>{formatTokenCount(selectedUser.total_input_tokens)}</span></div>
								<div class="flex justify-between border-b border-gray-100 pb-2 dark:border-gray-800"><span class="text-gray-500 dark:text-gray-400">Output</span><span>{formatTokenCount(selectedUser.total_output_tokens)}</span></div>
								<div class="flex justify-between border-b border-gray-100 pb-2 dark:border-gray-800"><span class="text-gray-500 dark:text-gray-400">Active days</span><span>{selectedUser.days_active}</span></div>
								<div class="flex justify-between"><span class="text-gray-500 dark:text-gray-400">Role</span><span class="capitalize">{selectedUser.role || 'unknown'}</span></div>
							</div>
						</div>
					{:else}
						<div class="mt-5 text-sm text-gray-500 dark:text-gray-400">Select a user to inspect them.</div>
					{/if}
				</div>
			</section>
		{:else}
			<div class="rounded-3xl border border-gray-200 bg-white p-8 text-gray-500 dark:border-gray-800 dark:bg-gray-900 dark:text-gray-400">
				No admin analytics data for {selectedYear} yet.
			</div>
		{/if}
	</div>
</div>
