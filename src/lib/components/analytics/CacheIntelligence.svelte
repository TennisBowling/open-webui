<script lang="ts">
	import { getContext } from 'svelte';
	import type { Writable } from 'svelte/store';
	import {
		formatTokenCount,
		formatCost,
		type CacheAnalytics,
		type CacheGroupStats
	} from '$lib/apis/analytics';

	interface Props {
		data?: CacheAnalytics | null;
		loading?: boolean;
		groupBy?: 'gateway' | 'vendor' | 'model';
	}

	let { data = null, loading = false, groupBy = 'gateway' }: Props = $props();

	const i18n =
		getContext<Writable<{ t: (k: string, o?: Record<string, unknown>) => string }>>('i18n');

	type Scenario = 'conversational' | 'agentic' | 'compare';
	let scenario: Scenario = $state('conversational');
	const SCENARIOS: [Scenario, string][] = [
		['conversational', 'Conversational'],
		['agentic', 'Agentic'],
		['compare', 'Compare']
	];

	// Distinct, legible line colors assigned by the backend's volume order.
	// Warm-leaning ramp harmonized with the ink-on-paper palette.
	const PALETTE = [
		'#CC785C',
		'#5C7048',
		'#A8783E',
		'#D4A27F',
		'#8C6A56',
		'#9CB07F',
		'#BF4D43',
		'#666663',
		'#33332E',
		'#B0A08C'
	];

	let hidden: Record<string, boolean> = $state({});
	let focusKey: string | null = $state(null);

	let groups = $derived(data?.groups ?? []);
	let buckets = $derived(data?.buckets ?? []);
	let colorOf = $derived(
		(() => {
			const m: Record<string, string> = {};
			groups.forEach((g, i) => (m[g.key] = PALETTE[i % PALETTE.length]));
			return m;
		})()
	);

	// Reset the visible set (top 5) whenever the grouping or window changes.
	let _lastSig = '';
	function resetVisibility(sig: string) {
		if (sig === _lastSig) return;
		_lastSig = sig;
		const h: Record<string, boolean> = {};
		groups.forEach((g, i) => {
			if (i >= 5) h[g.key] = true;
		});
		hidden = h;
		focusKey = null;
	}
	$effect(() => {
		resetVisibility(
			`${groupBy}:${data?.start_ts}:${data?.end_ts}:${groups.map((g) => g.key).join(',')}`
		);
	});

	let visibleGroups = $derived(groups.filter((g) => !hidden[g.key]));

	// ---- chart geometry ----
	const W = 820,
		H = 340,
		padL = 46,
		padR = 14,
		padT = 16,
		padB = 44;
	let plotW = $derived(W - padL - padR);
	let plotH = $derived(H - padT - padB);
	let n = $derived(buckets.length);
	const xAt = (i: number) => padL + (n <= 1 ? plotW / 2 : i * (plotW / (n - 1)));
	const yAt = (hit: number) => padT + (1 - Math.max(0, Math.min(1, hit))) * plotH;

	const curveOf = (g: CacheGroupStats, series: 'conv' | 'agentic') =>
		series === 'agentic' ? g.curve_agentic : g.curve;
	const seriesRequests = (g: CacheGroupStats, series: 'conv' | 'agentic') =>
		curveOf(g, series).reduce((s, p) => s + p.requests, 0);

	// Polyline segments that skip zero-request buckets (so an empty agentic curve
	// renders as nothing, not a misleading flat-0 line).
	function segments(g: CacheGroupStats, series: 'conv' | 'agentic') {
		const pts = curveOf(g, series);
		const segs: { x: number; y: number; hit: number; requests: number; bi: number }[][] = [];
		let cur: { x: number; y: number; hit: number; requests: number; bi: number }[] = [];
		pts.forEach((p, i) => {
			if (p.requests > 0)
				cur.push({ x: xAt(i), y: yAt(p.hit_ratio), hit: p.hit_ratio, requests: p.requests, bi: i });
			else if (cur.length) {
				segs.push(cur);
				cur = [];
			}
		});
		if (cur.length) segs.push(cur);
		return segs;
	}
	const pathOf = (seg: { x: number; y: number }[]) =>
		seg.map((q, i) => `${i ? 'L' : 'M'} ${q.x.toFixed(1)} ${q.y.toFixed(1)}`).join(' ');

	// Conversational-TTL marker x from seconds (interpolated over bucket centres).
	const CENTERS = [15, 45, 150, 450, 1200, 2700, 9000, 43200];
	function ttlX(secs: number | null) {
		if (secs == null) return null;
		if (secs <= CENTERS[0]) return xAt(0);
		for (let i = 0; i < CENTERS.length - 1; i++) {
			if (secs <= CENTERS[i + 1])
				return xAt(i + (secs - CENTERS[i]) / (CENTERS[i + 1] - CENTERS[i]));
		}
		return xAt(CENTERS.length - 1);
	}

	// What gets drawn for the active scenario.
	let drawList = $derived(
		scenario === 'compare'
			? visibleGroups.flatMap((g) => [
					{ g, series: 'conv' as const, dash: false },
					{ g, series: 'agentic' as const, dash: true }
				])
			: visibleGroups.map((g) => ({
					g,
					series: (scenario === 'agentic' ? 'agentic' : 'conv') as 'conv' | 'agentic',
					dash: false
				}))
	);

	// ---- hover ----
	let hoverIdx: number | null = $state(null);
	function onMove(e: MouseEvent) {
		const el = e.currentTarget as SVGElement;
		const rect = el.getBoundingClientRect();
		const x = (e.clientX - rect.left) * (W / rect.width);
		let best = 0,
			bd = Infinity;
		for (let i = 0; i < n; i++) {
			const d = Math.abs(xAt(i) - x);
			if (d < bd) {
				bd = d;
				best = i;
			}
		}
		hoverIdx = best;
	}

	let hoverRows = $derived(
		hoverIdx == null
			? []
			: (drawList
					.map(({ g, series, dash }) => {
						const p = curveOf(g, series)[hoverIdx as number];
						return p && p.requests > 0
							? {
									key: g.key + series,
									label: g.label,
									color: colorOf[g.key],
									dash,
									hit: p.hit_ratio,
									requests: p.requests,
									series
								}
							: null;
					})
					.filter(Boolean) as {
					key: string;
					label: string;
					color: string;
					dash: boolean;
					hit: number;
					requests: number;
					series: string;
				}[])
	);

	// ---- formatting ----
	const fmtPct = (x: number | null | undefined) => `${(x ?? 0).toFixed((x ?? 0) >= 10 ? 0 : 1)}%`;
	function fmtTTL(g: CacheGroupStats) {
		const s = g.est_ttl_seconds;
		if (s == null) return '—';
		let v: string;
		if (s < 60) v = `${Math.round(s)}s`;
		else if (s < 3600) v = `${Math.round(s / 60)} min`;
		else if (s < 86400) v = `${(s / 3600).toFixed(s < 36000 ? 1 : 0)} hr`;
		else v = `${Math.round(s / 86400)} d`;
		return (g.est_ttl_capped ? '> ' : '~') + v;
	}
	const toggle = (key: string) => (hidden = { ...hidden, [key]: !hidden[key] });

	let yTicks = $derived([0, 0.25, 0.5, 0.75, 1]);
	let convCacheShare = $derived(
		data && data.conversational_cache_read_tokens + data.agentic_cache_read_tokens > 0
			? (data.conversational_cache_read_tokens /
					(data.conversational_cache_read_tokens + data.agentic_cache_read_tokens)) *
					100
			: 0
	);

	const groupByLabel: Record<string, string> = {
		gateway: 'Provider · gateway',
		vendor: 'Provider · vendor',
		model: 'Model'
	};
</script>

<div class="space-y-6">
	<!-- KPI row -->
	<section class="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
		<div
			class="rounded-2xl border-hairline border-book-cloth/20 bg-book-cloth/10 p-6 shadow-sm dark:border-book-cloth/30 dark:bg-book-cloth/10"
		>
			<div class="text-sm font-medium text-book-cloth dark:text-kraft">
				{$i18n.t('Cache reads')}
			</div>
			<div class="mt-2 text-3xl font-semibold tracking-tight text-book-cloth dark:text-kraft">
				{formatTokenCount(data?.cache_read_tokens ?? 0)}
			</div>
			<div class="mt-1 text-xs text-book-cloth/80 dark:text-kraft/80">
				{fmtPct(data?.hit_rate ?? 0)}
				{$i18n.t('of prompt tokens served from cache')}
			</div>
		</div>

		<div
			class="rounded-2xl border-hairline border-success/25 bg-success/10 p-6 shadow-sm dark:border-success-dark/30 dark:bg-success/10"
		>
			<div class="text-sm font-medium text-success dark:text-success-dark">
				{$i18n.t('Saved by caching')}
			</div>
			<div class="mt-2 text-3xl font-semibold tracking-tight text-success dark:text-success-dark">
				{formatCost(data?.savings_usd ?? 0)}
			</div>
			<div class="mt-1 text-xs text-success/80 dark:text-success-dark/80">
				{$i18n.t('vs. paying full prompt rate')}{(data?.unpriced_cache_tokens ?? 0) > 0
					? ` · ${formatTokenCount(data?.unpriced_cache_tokens ?? 0)} ${$i18n.t('unpriced')}`
					: ''}
			</div>
		</div>

		<div
			class="rounded-2xl border-hairline border-gray-200 bg-white p-6 shadow-sm dark:border-gray-800 dark:bg-gray-900"
		>
			<div class="text-sm font-medium text-gray-500 dark:text-gray-400">
				{$i18n.t('Conversational hit')}
			</div>
			<div class="mt-2 text-3xl font-semibold tracking-tight text-book-cloth dark:text-kraft">
				{fmtPct(data?.conversational_hit_rate ?? 0)}
			</div>
			<div class="mt-1 text-xs text-gray-500 dark:text-gray-400">
				{formatTokenCount(data?.conversational_request_count ?? 0)}
				{$i18n.t('new-turn requests')}
			</div>
		</div>

		<div
			class="rounded-2xl border-hairline border-gray-200 bg-white p-6 shadow-sm dark:border-gray-800 dark:bg-gray-900"
		>
			<div class="text-sm font-medium text-gray-500 dark:text-gray-400">
				{$i18n.t('Agentic hit')}
			</div>
			<div class="mt-2 text-3xl font-semibold tracking-tight text-warning dark:text-warning-dark">
				{fmtPct(data?.agentic_hit_rate ?? 0)}
			</div>
			<div class="mt-1 text-xs text-gray-500 dark:text-gray-400">
				{formatTokenCount(data?.agentic_request_count ?? 0)}
				{$i18n.t('tool-loop requests')}
			</div>
		</div>
	</section>

	<!-- conv vs agentic cache split bar -->
	{#if data && data.conversational_cache_read_tokens + data.agentic_cache_read_tokens > 0}
		<section
			class="rounded-2xl border-hairline border-gray-200 bg-white p-5 shadow-sm dark:border-gray-800 dark:bg-gray-900"
		>
			<div class="mb-2 flex items-center justify-between text-sm">
				<span class="font-medium">{$i18n.t('Where cache hits come from')}</span>
				<span class="text-gray-500 dark:text-gray-400"
					>{fmtPct(convCacheShare)} {$i18n.t('conversational')}</span
				>
			</div>
			<div class="flex h-3 overflow-hidden rounded-full bg-gray-100 dark:bg-gray-800">
				<div
					class="h-full bg-book-cloth"
					style="width: {convCacheShare}%"
					title="{$i18n.t('Conversational')}: {formatTokenCount(
						data.conversational_cache_read_tokens
					)}"
				></div>
				<div
					class="h-full bg-warning"
					style="width: {100 - convCacheShare}%"
					title="{$i18n.t('Agentic')}: {formatTokenCount(data.agentic_cache_read_tokens)}"
				></div>
			</div>
			<div class="mt-2 flex items-center gap-4 text-xs text-gray-500 dark:text-gray-400">
				<span class="inline-flex items-center gap-1.5"
					><span class="h-2 w-2 rounded-full bg-book-cloth"></span>{$i18n.t(
						'Conversational (new turn)'
					)}</span
				>
				<span class="inline-flex items-center gap-1.5"
					><span class="h-2 w-2 rounded-full bg-warning"></span>{$i18n.t(
						'Agentic (tool loop)'
					)}</span
				>
			</div>
		</section>
	{/if}

	<!-- survival curve -->
	<section
		class="rounded-2xl border-hairline border-gray-200 bg-white p-6 shadow-sm dark:border-gray-800 dark:bg-gray-900"
	>
		<div class="mb-4 flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
			<div>
				<h2 class="text-xl font-semibold">{$i18n.t('Cache survival curve')}</h2>
				<p class="mt-1 max-w-2xl text-sm text-gray-500 dark:text-gray-400">
					{$i18n.t('Cache hit rate vs. the idle gap since the same model’s previous call, by')}
					{groupByLabel[groupBy]}.
					{$i18n.t('Where a line crosses ~50% is roughly how long that cache lives.')}
				</p>
			</div>
			<div
				class="inline-flex shrink-0 rounded-xl border-hairline border-gray-200 bg-gray-50 p-1 text-sm dark:border-gray-800 dark:bg-gray-950"
			>
				{#each SCENARIOS as [val, label]}
					<button
						class="rounded-lg px-3 py-1.5 font-medium transition-colors duration-200 ease-paper {scenario ===
						val
							? 'bg-white text-gray-900 shadow-sm dark:bg-gray-800 dark:text-white'
							: 'text-gray-500 hover:text-gray-900 dark:text-gray-400 dark:hover:text-white'}"
						onclick={() => (scenario = val)}
					>
						{$i18n.t(label)}
					</button>
				{/each}
			</div>
		</div>

		{#if loading}
			<div class="h-80 animate-pulse rounded-2xl bg-gray-100 dark:bg-gray-800"></div>
		{:else if groups.length === 0}
			<div class="flex h-80 items-center justify-center text-sm text-gray-400">
				{$i18n.t('No cache activity in this range.')}
			</div>
		{:else}
			<div class="relative">
				<div class="overflow-x-auto scrollbar-none">
					<svg
						viewBox="0 0 {W} {H}"
						class="w-full min-w-[520px]"
						role="img"
						aria-label="Cache survival curve"
						onmousemove={onMove}
						onmouseleave={() => (hoverIdx = null)}
					>
						<!-- y gridlines + labels -->
						{#each yTicks as t}
							<line
								x1={padL}
								y1={yAt(t)}
								x2={W - padR}
								y2={yAt(t)}
								stroke="currentColor"
								class="text-gray-200 dark:text-gray-800"
								stroke-width="1"
							/>
							<text
								x={padL - 8}
								y={yAt(t) + 3}
								text-anchor="end"
								class="fill-gray-400"
								font-size="10">{Math.round(t * 100)}%</text
							>
						{/each}
						<!-- x labels -->
						{#each buckets as b, i}
							<text
								x={xAt(i)}
								y={H - padB + 16}
								text-anchor="middle"
								class="fill-gray-400"
								font-size="10">{b.label}</text
							>
						{/each}
						<!-- hover guide -->
						{#if hoverIdx != null}
							<line
								x1={xAt(hoverIdx)}
								y1={padT}
								x2={xAt(hoverIdx)}
								y2={padT + plotH}
								stroke="currentColor"
								class="text-gray-300 dark:text-gray-700"
								stroke-width="1"
								stroke-dasharray="3 3"
							/>
						{/if}
						<!-- lines -->
						{#each drawList as d (d.g.key + d.series)}
							{#each segments(d.g, d.series) as seg}
								<path
									d={pathOf(seg)}
									fill="none"
									stroke={colorOf[d.g.key]}
									stroke-width={focusKey && focusKey === d.g.key ? 3 : 2}
									stroke-dasharray={d.dash ? '5 4' : 'none'}
									stroke-linejoin="round"
									stroke-linecap="round"
									opacity={focusKey && focusKey !== d.g.key ? 0.25 : 0.95}
								/>
							{/each}
							<!-- points -->
							{#each segments(d.g, d.series) as seg}
								{#each seg as q}
									<circle
										cx={q.x}
										cy={q.y}
										r={hoverIdx === q.bi ? 4 : 2.5}
										fill={colorOf[d.g.key]}
										opacity={focusKey && focusKey !== d.g.key ? 0.25 : 1}
									/>
								{/each}
							{/each}
							<!-- conversational TTL marker -->
							{#if d.series === 'conv' && d.g.est_ttl_seconds != null && (!focusKey || focusKey === d.g.key)}
								{@const tx = ttlX(d.g.est_ttl_seconds)}
								{#if tx != null}
									<polygon
										points="{tx - 4},{padT + plotH + 4} {tx + 4},{padT + plotH + 4} {tx},{padT +
											plotH -
											1}"
										fill={colorOf[d.g.key]}
										opacity="0.85"
									/>
								{/if}
							{/if}
						{/each}
					</svg>
				</div>

				<!-- tooltip -->
				{#if hoverIdx != null && hoverRows.length > 0}
					<div
						class="pointer-events-none absolute left-1/2 top-2 z-10 -translate-x-1/2 rounded-xl border-hairline border-gray-200 bg-white/95 px-3 py-2 text-xs shadow-lg backdrop-blur dark:border-gray-700 dark:bg-gray-900/95"
					>
						<div class="mb-1 font-semibold">{buckets[hoverIdx]?.label} {$i18n.t('idle')}</div>
						<div class="space-y-0.5">
							{#each hoverRows as r}
								<div class="flex items-center justify-between gap-3">
									<span class="inline-flex items-center gap-1.5">
										<span class="inline-block h-2 w-2 rounded-full" style="background:{r.color}"
										></span>
										<span class="max-w-[140px] truncate"
											>{r.label}{r.dash
												? ' · agentic'
												: scenario === 'compare'
													? ' · conv'
													: ''}</span
										>
									</span>
									<span class="font-medium"
										>{fmtPct(r.hit * 100)} <span class="text-gray-400">({r.requests})</span></span
									>
								</div>
							{/each}
						</div>
					</div>
				{/if}
			</div>

			<!-- legend -->
			<div class="mt-4 flex flex-wrap gap-x-5 gap-y-2">
				{#each groups as g (g.key)}
					{@const agenticEmpty = seriesRequests(g, 'agentic') === 0}
					{@const noData = scenario === 'agentic' && agenticEmpty}
					<button
						class="group inline-flex items-center gap-2 text-sm transition {hidden[g.key]
							? 'opacity-40'
							: ''}"
						onclick={() => toggle(g.key)}
						onmouseenter={() => (focusKey = g.key)}
						onmouseleave={() => (focusKey = null)}
						title={hidden[g.key] ? $i18n.t('Show') : $i18n.t('Hide')}
					>
						<span class="h-2.5 w-2.5 rounded-full" style="background:{colorOf[g.key]}"></span>
						<span class="font-medium {hidden[g.key] ? 'line-through' : ''}">{g.label}</span>
						<span
							class="rounded-full bg-gray-100 px-2 py-0.5 text-[11px] font-semibold text-gray-600 dark:bg-gray-800 dark:text-gray-300"
						>
							{noData ? $i18n.t('no tool-loop data') : fmtTTL(g)}
						</span>
					</button>
				{/each}
			</div>
		{/if}
	</section>

	<!-- per-group table -->
	<section
		class="rounded-2xl border-hairline border-gray-200 bg-white p-6 shadow-sm dark:border-gray-800 dark:bg-gray-900"
	>
		<h2 class="mb-4 text-xl font-semibold">{$i18n.t('Cache by')} {groupByLabel[groupBy]}</h2>
		<div class="overflow-x-auto">
			<table class="w-full min-w-[760px] text-left text-sm">
				<thead class="text-xs uppercase tracking-wide text-gray-500 dark:text-gray-400">
					<tr>
						<th class="py-3 pr-3">{$i18n.t('Group')}</th>
						<th class="px-3 py-3 text-right">{$i18n.t('Cache TTL')}</th>
						<th class="px-3 py-3 text-right">{$i18n.t('Conv. hit')}</th>
						<th class="px-3 py-3 text-right">{$i18n.t('Agentic hit')}</th>
						<th class="px-3 py-3 text-right">{$i18n.t('Cache reads')}</th>
						<th class="px-3 py-3 text-right">{$i18n.t('Saved')}</th>
					</tr>
				</thead>
				<tbody class="divide-y divide-gray-100 dark:divide-gray-800">
					{#each groups as g (g.key)}
						<tr
							class="cursor-pointer transition hover:bg-gray-50 dark:hover:bg-gray-950/60 {focusKey ===
							g.key
								? 'bg-gray-50 dark:bg-gray-950/60'
								: ''}"
							onmouseenter={() => (focusKey = g.key)}
							onmouseleave={() => (focusKey = null)}
							onclick={() => (hidden = { ...hidden, [g.key]: !hidden[g.key] })}
						>
							<td class="py-3 pr-3">
								<span class="inline-flex items-center gap-2">
									<span
										class="h-2.5 w-2.5 rounded-full"
										style="background:{colorOf[g.key]}; opacity:{hidden[g.key] ? 0.4 : 1}"
									></span>
									<span class="font-medium {hidden[g.key] ? 'line-through opacity-50' : ''}"
										>{g.label}</span
									>
								</span>
							</td>
							<td class="px-3 py-3 text-right font-semibold">{fmtTTL(g)}</td>
							<td class="px-3 py-3 text-right text-book-cloth dark:text-kraft"
								>{g.conversational_requests > 0 ? fmtPct(g.conversational_hit_rate) : '—'}</td
							>
							<td class="px-3 py-3 text-right text-warning dark:text-warning-dark"
								>{g.agentic_requests > 0 ? fmtPct(g.agentic_hit_rate) : '—'}</td
							>
							<td class="px-3 py-3 text-right">{formatTokenCount(g.cache_read_tokens)}</td>
							<td class="px-3 py-3 text-right font-medium text-success dark:text-success-dark">
								{g.unpriced_cache_tokens > 0 && g.savings_usd === 0
									? '—'
									: formatCost(g.savings_usd)}
							</td>
						</tr>
					{/each}
				</tbody>
			</table>
		</div>
		<p class="mt-3 text-xs text-gray-400">
			{$i18n.t(
				'TTL is the conversational half-life: the idle gap at which cache reuse falls to half its warm value. “—” means too few samples.'
			)}
		</p>
	</section>

	<!-- user leaders -->
	{#if (data?.users ?? []).length > 0}
		<section
			class="rounded-2xl border-hairline border-gray-200 bg-white p-6 shadow-sm dark:border-gray-800 dark:bg-gray-900"
		>
			<h2 class="mb-4 text-xl font-semibold">{$i18n.t('Cache leaders by user')}</h2>
			<div class="grid gap-x-8 gap-y-3 md:grid-cols-2">
				{#each data?.users ?? [] as u}
					<div>
						<div class="mb-1 flex items-center justify-between gap-3 text-sm">
							<span class="truncate font-medium">{u.name || u.email || u.user_id}</span>
							<span class="text-gray-500 dark:text-gray-400">
								{formatTokenCount(u.cache_read_tokens)} · {fmtPct(u.hit_rate)} · {formatCost(
									u.savings_usd
								)}
							</span>
						</div>
						<div class="h-1.5 overflow-hidden rounded-full bg-gray-100 dark:bg-gray-800">
							<div
								class="h-full rounded-full bg-book-cloth"
								style="width: {Math.min(100, u.hit_rate)}%"
							></div>
						</div>
					</div>
				{/each}
			</div>
		</section>
	{/if}
</div>
