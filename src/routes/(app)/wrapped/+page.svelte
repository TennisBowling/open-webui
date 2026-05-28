<script lang="ts">
	import { getContext, onMount } from 'svelte';
	import type { Writable } from 'svelte/store';
	import { goto } from '$app/navigation';
	import { user } from '$lib/stores';
	import {
		formatTokenCount,
		getUserHeatmap,
		getUserModelUsage,
		getUserTopChats,
		getUserWrapped,
		type HeatmapDataPoint,
		type HeatmapResponse,
		type ModelUsage,
		type TopChat,
		type WrappedSummary
	} from '$lib/apis/analytics';

	const i18n = getContext<Writable<{ t: (key: string, options?: Record<string, unknown>) => string }>>('i18n');

	let selectedYear: number = new Date().getFullYear();
	const currentYear = new Date().getFullYear();
	const availableYears = Array.from({ length: 5 }, (_, i) => currentYear - i);

	let mounted = false;
	let loadedYear: number | null = null;
	let loading = true;
	let error: string | null = null;

	let wrapped: WrappedSummary | null = null;
	let heatmap: HeatmapResponse | null = null;
	let modelUsage: ModelUsage[] = [];
	let topChats: TopChat[] = [];

	let loadSeq = 0;

	const dayNames = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
	const monthNames = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
	const heatColors = [
		'bg-gray-200 dark:bg-gray-800',
		'bg-sky-200 dark:bg-sky-950',
		'bg-sky-300 dark:bg-sky-800',
		'bg-blue-400 dark:bg-blue-700',
		'bg-violet-500 dark:bg-violet-600'
	];

	onMount(() => {
		mounted = true;
		if (!$user) goto('/auth');
		loadAll();
	});

	$: if (mounted && loadedYear !== null && selectedYear !== loadedYear && !loading) {
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

			const [wrappedData, heatmapData, modelsData, chatsData] = await Promise.all([
				getUserWrapped(token, selectedYear),
				getUserHeatmap(token, selectedYear),
				getUserModelUsage(token, selectedYear),
				getUserTopChats(token, selectedYear, 10)
			]);

			if (seq !== loadSeq) return;
			wrapped = wrappedData;
			heatmap = heatmapData;
			modelUsage = modelsData ?? [];
			topChats = chatsData ?? [];
			loadedYear = selectedYear;
		} catch (e) {
			console.error(e);
			error = 'Failed to load Wrapped data';
		} finally {
			if (seq === loadSeq) loading = false;
		}
	}

	function modelName(modelId: string | null | undefined) {
		if (!modelId) return 'Unknown model';
		return modelId.split('/').pop() || modelId;
	}

	function asDate(date: string) {
		return new Date(`${date}T00:00:00`);
	}

	function dateKey(date: Date) {
		const y = date.getFullYear();
		const m = `${date.getMonth() + 1}`.padStart(2, '0');
		const d = `${date.getDate()}`.padStart(2, '0');
		return `${y}-${m}-${d}`;
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

	function daysInYear(year: number) {
		return new Date(year, 1, 29).getMonth() === 1 ? 366 : 365;
	}

	function dayOfYear(date: Date) {
		const start = new Date(date.getFullYear(), 0, 0);
		return Math.floor((date.getTime() - start.getTime()) / 86400000);
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

	function getStreaks(data: HeatmapDataPoint[]) {
		let longest = 0;
		let current = 0;
		let run = 0;
		for (const day of data) {
			if (day.tokens > 0) {
				run += 1;
				longest = Math.max(longest, run);
			} else {
				run = 0;
			}
		}
		for (let i = data.length - 1; i >= 0; i--) {
			if (data[i].tokens > 0) current += 1;
			else break;
		}
		return { longest, current };
	}

	function busiestMonth(data: HeatmapDataPoint[]) {
		const totals = Array(12).fill(0);
		for (const day of data) {
			totals[asDate(day.date).getMonth()] += day.tokens;
		}
		const max = Math.max(...totals);
		const idx = Math.max(0, totals.indexOf(max));
		return { label: monthNames[idx], tokens: max };
	}

	function busiestWeekday(data: HeatmapDataPoint[]) {
		const totals = Array(7).fill(0);
		for (const day of data) {
			totals[asDate(day.date).getDay()] += day.tokens;
		}
		const max = Math.max(...totals);
		const idx = Math.max(0, totals.indexOf(max));
		return { label: dayNames[idx], tokens: max };
	}

	function topDays(data: HeatmapDataPoint[]) {
		return [...data].filter((d) => d.tokens > 0).sort((a, b) => b.tokens - a.tokens).slice(0, 5);
	}

	function shortDate(date: string) {
		return asDate(date).toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
	}

	$: daily = heatmap?.data ?? [];
	$: weeks = buildWeeks(selectedYear, daily);
	$: streaks = getStreaks(daily);
	$: hotMonth = busiestMonth(daily);
	$: hotWeekday = busiestWeekday(daily);
	$: bestDays = topDays(daily);
	$: totalTokens = wrapped?.total_tokens ?? 0;
	$: totalInput = wrapped?.total_input_tokens ?? 0;
	$: totalOutput = wrapped?.total_output_tokens ?? 0;
	$: totalCached = wrapped?.total_cache_read_tokens ?? 0;
	$: cacheRate = pct(totalCached, totalInput);
	$: outputRatio = pct(totalOutput, totalTokens);
	$: activeDays = wrapped?.days_active ?? 0;
	$: messages = wrapped?.total_messages ?? 0;
	$: conversations = wrapped?.total_conversations ?? 0;
	$: avgPerActiveDay = activeDays > 0 ? Math.round(totalTokens / activeDays) : 0;
	$: avgPerConversation = conversations > 0 ? Math.round(totalTokens / conversations) : 0;
	$: avgPerMessage = messages > 0 ? Math.round(totalTokens / messages) : 0;
	$: elapsedDays = selectedYear === currentYear ? dayOfYear(new Date()) : daysInYear(selectedYear);
	$: projectedTokens = selectedYear === currentYear && elapsedDays > 0 ? Math.round((totalTokens / elapsedDays) * daysInYear(selectedYear)) : totalTokens;
	$: topModel = modelUsage[0];
	$: cacheModels = [...modelUsage]
		.filter((m) => (m.total_cache_read_tokens ?? 0) > 0)
		.sort((a, b) => (b.total_cache_read_tokens ?? 0) - (a.total_cache_read_tokens ?? 0))
		.slice(0, 5);
	$: cacheChats = [...topChats]
		.filter((c) => (c.total_cache_read_tokens ?? 0) > 0)
		.sort((a, b) => (b.total_cache_read_tokens ?? 0) - (a.total_cache_read_tokens ?? 0))
		.slice(0, 5);
	$: topChat = topChats[0];
</script>

<svelte:head>
	<title>{selectedYear} Wrapped | Open WebUI</title>
</svelte:head>

<div class="min-h-screen bg-gray-50 text-gray-900 dark:bg-gray-950 dark:text-gray-100">
	<div class="mx-auto max-w-7xl px-4 py-8 sm:px-6 lg:px-8">
		<header class="mb-8 flex flex-col gap-5 md:flex-row md:items-end md:justify-between">
			<div>
				<button
					class="mb-5 inline-flex items-center gap-2 text-sm font-medium text-gray-500 transition hover:text-gray-900 dark:text-gray-400 dark:hover:text-white"
					on:click={() => goto('/')}
				>
					<span aria-hidden="true">←</span>
					{$i18n.t('Back to chat')}
				</button>
				<div class="flex flex-wrap items-center gap-3">
					<h1 class="text-4xl font-semibold tracking-tight md:text-6xl">{selectedYear} Wrapped</h1>
					<span class="rounded-full border border-blue-200 bg-blue-50 px-3 py-1 text-xs font-semibold uppercase tracking-wide text-blue-700 dark:border-blue-900/70 dark:bg-blue-950/60 dark:text-blue-300">
						Usage intelligence
					</span>
				</div>
				<p class="mt-3 max-w-2xl text-sm leading-6 text-gray-600 dark:text-gray-400">
					A clean readout of your model spend, cache reuse, active days, top conversations, and model mix.
				</p>
			</div>

			<div class="flex flex-wrap items-center gap-3">
				{#if $user?.role === 'admin'}
					<button
						class="rounded-xl border border-red-200 bg-red-50 px-4 py-2 text-sm font-semibold text-red-700 shadow-sm transition hover:bg-red-100 dark:border-red-900/70 dark:bg-red-950/50 dark:text-red-300 dark:hover:bg-red-950"
						on:click={() => goto('/admin/wrapped')}
					>
						Admin analytics
					</button>
				{/if}
				<label for="year-select" class="text-sm font-medium text-gray-500 dark:text-gray-400">Year</label>
				<select
					id="year-select"
					bind:value={selectedYear}
					class="rounded-xl border border-gray-200 bg-white px-4 py-2 text-sm font-semibold shadow-sm outline-none transition focus:border-blue-500 focus:ring-4 focus:ring-blue-500/10 dark:border-gray-800 dark:bg-gray-900"
				>
					{#each availableYears as year}
						<option value={year}>{year}</option>
					{/each}
				</select>
			</div>
		</header>

		{#if loading}
			<div class="grid gap-4 md:grid-cols-4">
				{#each Array(8) as _}
					<div class="h-36 animate-pulse rounded-3xl border border-gray-200 bg-white dark:border-gray-800 dark:bg-gray-900"></div>
				{/each}
			</div>
		{:else if error}
			<div class="rounded-3xl border border-red-200 bg-red-50 p-8 text-red-700 dark:border-red-900/70 dark:bg-red-950/40 dark:text-red-300">
				<div class="text-lg font-semibold">Couldn’t load Wrapped</div>
				<div class="mt-1 text-sm">{error}</div>
			</div>
		{:else if wrapped}
			<section class="mb-6 grid gap-4 md:grid-cols-2 xl:grid-cols-4">
				<div class="rounded-3xl border border-gray-200 bg-white p-6 shadow-sm dark:border-gray-800 dark:bg-gray-900">
					<div class="text-sm font-medium text-gray-500 dark:text-gray-400">Total request tokens</div>
					<div class="mt-3 text-4xl font-semibold tracking-tight">{formatTokenCount(totalTokens)}</div>
					<div class="mt-4 text-xs text-gray-500 dark:text-gray-400">Projected year pace: {formatTokenCount(projectedTokens)}</div>
				</div>
				<div class="rounded-3xl border border-gray-200 bg-white p-6 shadow-sm dark:border-gray-800 dark:bg-gray-900">
					<div class="text-sm font-medium text-gray-500 dark:text-gray-400">Cache reads</div>
					<div class="mt-3 text-4xl font-semibold tracking-tight text-violet-600 dark:text-violet-300">{formatTokenCount(totalCached)}</div>
					<div class="mt-4 h-2 overflow-hidden rounded-full bg-gray-100 dark:bg-gray-800">
						<div class="h-full rounded-full bg-violet-500" style="width: {cacheRate}%"></div>
					</div>
					<div class="mt-2 text-xs text-gray-500 dark:text-gray-400">{fmtPct(cacheRate)} of prompt tokens were cached</div>
				</div>
				<div class="rounded-3xl border border-gray-200 bg-white p-6 shadow-sm dark:border-gray-800 dark:bg-gray-900">
					<div class="text-sm font-medium text-gray-500 dark:text-gray-400">Generated output</div>
					<div class="mt-3 text-4xl font-semibold tracking-tight text-emerald-600 dark:text-emerald-300">{formatTokenCount(totalOutput)}</div>
					<div class="mt-4 text-xs text-gray-500 dark:text-gray-400">{fmtPct(outputRatio)} of total tokens</div>
				</div>
				<div class="rounded-3xl border border-gray-200 bg-white p-6 shadow-sm dark:border-gray-800 dark:bg-gray-900">
					<div class="text-sm font-medium text-gray-500 dark:text-gray-400">Active days</div>
					<div class="mt-3 text-4xl font-semibold tracking-tight">{activeDays}</div>
					<div class="mt-4 text-xs text-gray-500 dark:text-gray-400">Longest streak: {streaks.longest} days</div>
				</div>
			</section>

			<section class="mb-6 grid gap-4 lg:grid-cols-3">
				<div class="rounded-3xl border border-gray-200 bg-white p-6 shadow-sm dark:border-gray-800 dark:bg-gray-900 lg:col-span-2">
					<div class="mb-5 flex items-center justify-between gap-4">
						<div>
							<h2 class="text-xl font-semibold">Token mix</h2>
							<p class="text-sm text-gray-500 dark:text-gray-400">Prompt volume, generated output, and cache reuse.</p>
						</div>
						<div class="text-right text-sm text-gray-500 dark:text-gray-400">
							<div>{fullNumber(totalTokens)} total</div>
						</div>
					</div>

					<div class="space-y-5">
						<div>
							<div class="mb-2 flex justify-between text-sm">
								<span class="font-medium">Prompt tokens</span>
								<span class="text-gray-500 dark:text-gray-400">{formatTokenCount(totalInput)}</span>
							</div>
							<div class="h-3 overflow-hidden rounded-full bg-gray-100 dark:bg-gray-800">
								<div class="h-full rounded-full bg-blue-500" style="width: {pct(totalInput, totalTokens)}%"></div>
							</div>
						</div>
						<div>
							<div class="mb-2 flex justify-between text-sm">
								<span class="font-medium">Completion tokens</span>
								<span class="text-gray-500 dark:text-gray-400">{formatTokenCount(totalOutput)}</span>
							</div>
							<div class="h-3 overflow-hidden rounded-full bg-gray-100 dark:bg-gray-800">
								<div class="h-full rounded-full bg-emerald-500" style="width: {pct(totalOutput, totalTokens)}%"></div>
							</div>
						</div>
						<div>
							<div class="mb-2 flex justify-between text-sm">
								<span class="font-medium">Prompt cache reads</span>
								<span class="text-gray-500 dark:text-gray-400">{formatTokenCount(totalCached)}</span>
							</div>
							<div class="h-3 overflow-hidden rounded-full bg-gray-100 dark:bg-gray-800">
								<div class="h-full rounded-full bg-violet-500" style="width: {cacheRate}%"></div>
							</div>
						</div>
					</div>
				</div>

				<div class="rounded-3xl border border-gray-200 bg-white p-6 shadow-sm dark:border-gray-800 dark:bg-gray-900">
					<h2 class="text-xl font-semibold">Efficiency</h2>
					<div class="mt-5 grid grid-cols-2 gap-3">
						<div class="rounded-2xl bg-gray-50 p-4 dark:bg-gray-950/60">
							<div class="text-xs text-gray-500 dark:text-gray-400">Avg / active day</div>
							<div class="mt-2 text-2xl font-semibold">{formatTokenCount(avgPerActiveDay)}</div>
						</div>
						<div class="rounded-2xl bg-gray-50 p-4 dark:bg-gray-950/60">
							<div class="text-xs text-gray-500 dark:text-gray-400">Avg / chat</div>
							<div class="mt-2 text-2xl font-semibold">{formatTokenCount(avgPerConversation)}</div>
						</div>
						<div class="rounded-2xl bg-gray-50 p-4 dark:bg-gray-950/60">
							<div class="text-xs text-gray-500 dark:text-gray-400">Avg / message</div>
							<div class="mt-2 text-2xl font-semibold">{formatTokenCount(avgPerMessage)}</div>
						</div>
						<div class="rounded-2xl bg-gray-50 p-4 dark:bg-gray-950/60">
							<div class="text-xs text-gray-500 dark:text-gray-400">Messages / chat</div>
							<div class="mt-2 text-2xl font-semibold">{conversations > 0 ? (messages / conversations).toFixed(1) : '0'}</div>
						</div>
					</div>
				</div>
			</section>

			<section class="mb-6 rounded-3xl border border-gray-200 bg-white p-6 shadow-sm dark:border-gray-800 dark:bg-gray-900">
				<div class="mb-5 flex flex-col gap-2 md:flex-row md:items-end md:justify-between">
					<div>
						<h2 class="text-xl font-semibold">Activity map</h2>
						<p class="text-sm text-gray-500 dark:text-gray-400">{activeDays} active days · peak month {hotMonth.label} · strongest weekday {hotWeekday.label}</p>
					</div>
					<div class="flex items-center gap-1 text-xs text-gray-500 dark:text-gray-400">
						<span>Less</span>
						{#each heatColors as color}
							<span class="h-3 w-3 rounded-sm {color}"></span>
						{/each}
						<span>More</span>
					</div>
				</div>

				<div class="overflow-x-auto pb-2">
					<div class="inline-flex min-w-max gap-1">
						{#each weeks as week}
							<div class="flex flex-col gap-1">
								{#each week as day}
									<div
										class="h-3 w-3 rounded-sm {day.inYear ? heatColors[day.level] : 'bg-transparent'}"
										title={`${day.date}: ${day.tokens ? fullNumber(day.tokens) + ' tokens' : 'No activity'}`}
									></div>
								{/each}
							</div>
						{/each}
					</div>
				</div>
			</section>

			<section class="mb-6 grid gap-4 lg:grid-cols-3">
				<div class="rounded-3xl border border-gray-200 bg-white p-6 shadow-sm dark:border-gray-800 dark:bg-gray-900">
					<h2 class="text-xl font-semibold">Highlights</h2>
					<div class="mt-5 space-y-4">
						<div class="flex justify-between gap-4 border-b border-gray-100 pb-3 dark:border-gray-800">
							<span class="text-sm text-gray-500 dark:text-gray-400">Peak day</span>
							<span class="text-right text-sm font-medium">{wrapped.most_active_day ? `${shortDate(wrapped.most_active_day.date)} · ${formatTokenCount(wrapped.most_active_day.tokens)}` : '—'}</span>
						</div>
						<div class="flex justify-between gap-4 border-b border-gray-100 pb-3 dark:border-gray-800">
							<span class="text-sm text-gray-500 dark:text-gray-400">Top model</span>
							<span class="text-right text-sm font-medium">{topModel ? modelName(topModel.model_id) : '—'}</span>
						</div>
						<div class="flex justify-between gap-4 border-b border-gray-100 pb-3 dark:border-gray-800">
							<span class="text-sm text-gray-500 dark:text-gray-400">Biggest chat</span>
							<span class="text-right text-sm font-medium">{topChat?.title ?? '—'}</span>
						</div>
						<div class="flex justify-between gap-4">
							<span class="text-sm text-gray-500 dark:text-gray-400">Current streak</span>
							<span class="text-right text-sm font-medium">{streaks.current} days</span>
						</div>
					</div>
				</div>

				<div class="rounded-3xl border border-gray-200 bg-white p-6 shadow-sm dark:border-gray-800 dark:bg-gray-900">
					<h2 class="text-xl font-semibold">Top days</h2>
					<div class="mt-5 space-y-3">
						{#each bestDays as day, index}
							<div class="flex items-center gap-3">
								<div class="flex h-7 w-7 items-center justify-center rounded-full bg-gray-100 text-xs font-semibold dark:bg-gray-800">{index + 1}</div>
								<div class="min-w-0 flex-1">
									<div class="truncate text-sm font-medium">{shortDate(day.date)}</div>
									<div class="mt-1 h-1.5 overflow-hidden rounded-full bg-gray-100 dark:bg-gray-800">
										<div class="h-full rounded-full bg-blue-500" style="width: {pct(day.tokens, bestDays[0]?.tokens ?? 0)}%"></div>
									</div>
								</div>
								<div class="text-sm font-semibold">{formatTokenCount(day.tokens)}</div>
							</div>
						{/each}
						{#if bestDays.length === 0}
							<div class="text-sm text-gray-500 dark:text-gray-400">No active days yet.</div>
						{/if}
					</div>
				</div>

				<div class="rounded-3xl border border-gray-200 bg-white p-6 shadow-sm dark:border-gray-800 dark:bg-gray-900">
					<h2 class="text-xl font-semibold">Cache leaders</h2>
					<div class="mt-5 space-y-3">
						{#each cacheModels as model}
							<div>
								<div class="mb-1 flex justify-between gap-3 text-sm">
									<span class="truncate font-medium">{modelName(model.model_id)}</span>
									<span class="text-gray-500 dark:text-gray-400">{formatTokenCount(model.total_cache_read_tokens)} · {fmtPct(cachePct(model))}</span>
								</div>
								<div class="h-1.5 overflow-hidden rounded-full bg-gray-100 dark:bg-gray-800">
									<div class="h-full rounded-full bg-violet-500" style="width: {pct(model.total_cache_read_tokens, cacheModels[0]?.total_cache_read_tokens ?? 0)}%"></div>
								</div>
							</div>
						{/each}
						{#if cacheModels.length === 0}
							<div class="text-sm text-gray-500 dark:text-gray-400">No cache reads recorded.</div>
						{/if}
					</div>
				</div>
			</section>

			<section class="grid gap-4 lg:grid-cols-2">
				<div class="rounded-3xl border border-gray-200 bg-white p-6 shadow-sm dark:border-gray-800 dark:bg-gray-900">
					<h2 class="text-xl font-semibold">Model mix</h2>
					<div class="mt-5 space-y-4">
						{#each modelUsage.slice(0, 8) as model}
							<div>
								<div class="mb-1 flex items-center justify-between gap-3 text-sm">
									<span class="truncate font-medium">{modelName(model.model_id)}</span>
									<span class="text-gray-500 dark:text-gray-400">{model.percentage.toFixed(1)}%</span>
								</div>
								<div class="h-2 overflow-hidden rounded-full bg-gray-100 dark:bg-gray-800">
									<div class="h-full rounded-full bg-gradient-to-r from-blue-500 to-violet-500" style="width: {model.percentage}%"></div>
								</div>
								<div class="mt-1 text-xs text-gray-500 dark:text-gray-400">{formatTokenCount(model.total_tokens)} tokens · {formatTokenCount(model.total_cache_read_tokens ?? 0)} cached · {fmtPct(cachePct(model))} of input</div>
							</div>
						{/each}
					</div>
				</div>

				<div class="rounded-3xl border border-gray-200 bg-white p-6 shadow-sm dark:border-gray-800 dark:bg-gray-900">
					<h2 class="text-xl font-semibold">Top conversations</h2>
					<div class="mt-5 space-y-3">
						{#each topChats.slice(0, 8) as chat, index}
							<button class="w-full rounded-2xl p-3 text-left transition hover:bg-gray-50 dark:hover:bg-gray-950/60" on:click={() => goto(`/c/${chat.chat_id}`)}>
								<div class="flex items-start gap-3">
									<div class="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-gray-100 text-xs font-semibold dark:bg-gray-800">{index + 1}</div>
									<div class="min-w-0 flex-1">
										<div class="truncate text-sm font-medium">{chat.title || 'Untitled chat'}</div>
										<div class="mt-1 text-xs text-gray-500 dark:text-gray-400">{formatTokenCount(chat.total_tokens)} tokens · {formatTokenCount(chat.total_cache_read_tokens ?? 0)} cached</div>
									</div>
								</div>
							</button>
						{/each}
					</div>
				</div>
			</section>
		{:else}
			<div class="rounded-3xl border border-gray-200 bg-white p-8 text-gray-500 dark:border-gray-800 dark:bg-gray-900 dark:text-gray-400">
				No Wrapped data for {selectedYear} yet.
			</div>
		{/if}

		{#if $user?.role === 'admin'}
			<footer class="mt-10 rounded-3xl border border-red-200 bg-red-50 p-5 text-sm text-red-800 dark:border-red-900/70 dark:bg-red-950/30 dark:text-red-200">
				<div class="flex flex-col gap-3 md:flex-row md:items-center md:justify-between">
					<div>
						<div class="font-semibold">Admin view available</div>
						<div class="mt-1 text-red-700/80 dark:text-red-300/80">Open the site-wide dashboard for per-user, model, cache, and subagent analytics.</div>
					</div>
					<button
						class="rounded-xl bg-red-600 px-4 py-2 font-semibold text-white transition hover:bg-red-700"
						on:click={() => goto('/admin/wrapped')}
					>
						Open admin analytics
					</button>
				</div>
			</footer>
		{/if}
	</div>
</div>
