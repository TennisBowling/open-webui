<script lang="ts">
	import { getContext, onDestroy } from 'svelte';
	import {
		browserLiveStates,
		showBrowserPanel,
		showControls,
		browserPanelDismissed
	} from '$lib/stores';
	import Image from '$lib/components/common/Image.svelte';
	import XMark from '$lib/components/icons/XMark.svelte';

	const i18n: any = getContext('i18n');

	export let history: any = { messages: {}, currentId: null };

	// browserLiveStates is keyed by per-agent browser SESSION (one entry per
	// concurrent browsing agent/tab). With parallel browsing the parent ("main")
	// and each subagent each have their own entry, so we render a TAB STRIP and let
	// the user pick which tab to watch; we auto-follow the live/most-recent one
	// until the user explicitly selects a tab.
	$: states = $browserLiveStates;

	// Ordered list of tabs (sessions) for the strip. Oldest-first so a newly
	// spawned subagent's tab appears at the end and positions stay stable.
	$: tabs = Object.entries(states ?? {})
		.map(([id, v]) => ({ id, ...(v as any) }))
		.sort((a, b) => (a.startedAt ?? 0) - (b.startedAt ?? 0));

	// The user's explicit tab pick (null = auto-follow). Reset to auto when the
	// picked tab disappears (e.g. its page was reclaimed).
	let selectedId: string | null = null;
	$: if (selectedId && !states?.[selectedId]) selectedId = null;

	$: autoId = pickActiveId(states, history?.currentId);
	$: activeId = selectedId ?? autoId;

	function pickActiveId(s: Record<string, any>, currentId: string | null): string | null {
		// Prefer a live (not-done) tab — the most recently started one — so the
		// panel follows whatever is actively browsing right now.
		let best: string | null = null;
		let bestStart = -1;
		for (const [id, v] of Object.entries(s ?? {})) {
			if (v?.done) continue;
			const st = v?.startedAt ?? 0;
			if (st >= bestStart) {
				bestStart = st;
				best = id;
			}
		}
		if (best) return best;
		// No live tab: keep the current message's own tab if present, else the most
		// recent (possibly done) so the last frame lingers.
		if (currentId && s[currentId]) return currentId;
		let recent: string | null = null;
		let recentStart = -1;
		for (const [id, v] of Object.entries(s ?? {})) {
			const st = v?.startedAt ?? 0;
			if (st >= recentStart) {
				recentStart = st;
				recent = id;
			}
		}
		return recent;
	}

	function tabLabel(tab: any): string {
		if (tab?.label) return tab.label;
		if (tab?.session === 'main' || tab?.id === 'main') return $i18n.t('Main');
		// Fall back to the page title, then a short id, so a tab is always nameable.
		if (tab?.title) return String(tab.title).slice(0, 24);
		return String(tab?.id ?? '').slice(0, 8);
	}

	const selectTab = (id: string) => {
		selectedId = id;
	};

	$: state = activeId ? states[activeId] : null;

	// Elapsed timer — isolated tick so only the timer string recomputes, never the
	// frame image. Ticks only while the active tab is live (not done).
	let nowTs = Date.now();
	let timerInterval: ReturnType<typeof setInterval> | null = null;
	$: {
		const shouldTick = !!state && !state.done && (state.startedAt ?? 0) > 0;
		if (typeof window !== 'undefined') {
			if (shouldTick && !timerInterval) {
				nowTs = Date.now();
				timerInterval = setInterval(() => (nowTs = Date.now()), 1000);
			} else if (!shouldTick && timerInterval) {
				clearInterval(timerInterval);
				timerInterval = null;
			}
		}
	}
	onDestroy(() => {
		if (timerInterval) clearInterval(timerInterval);
	});

	$: elapsedMs = state ? (state.done ? state.elapsedMs ?? 0 : Math.max(0, nowTs - (state.startedAt ?? nowTs))) : 0;
	$: elapsedText = formatElapsed(elapsedMs);

	function formatElapsed(ms: number): string {
		const s = Math.floor(ms / 1000);
		if (s < 60) return `${s}s`;
		const m = Math.floor(s / 60);
		const rem = s % 60;
		return `${m}m ${rem}s`;
	}

	function phaseLabel(phase: string | undefined): string {
		switch (phase) {
			case 'navigating':
				return 'Navigating';
			case 'loaded':
				return 'Loaded';
			case 'acting':
				return 'Acting';
			case 'done':
				return 'Idle';
			default:
				return phase ?? '';
		}
	}

	function phaseClass(phase: string | undefined): string {
		switch (phase) {
			case 'navigating':
				return 'bg-blue-500/15 text-blue-600 dark:text-blue-400';
			case 'loaded':
				return 'bg-green-500/15 text-green-600 dark:text-green-400';
			case 'acting':
				return 'bg-amber-500/15 text-amber-600 dark:text-amber-400';
			default:
				return 'bg-gray-500/15 text-gray-500 dark:text-gray-400';
		}
	}

	const close = () => {
		// Remember the user dismissed it so a later frame doesn't reopen it this turn.
		browserPanelDismissed.set(true);
		showBrowserPanel.set(false);
		showControls.set(false);
	};
</script>

<div class="w-full h-full flex flex-col">
	<div class="flex items-center justify-between gap-2 px-1 pb-2">
		<div class="flex items-center gap-2 min-w-0">
			<div class="text-sm font-medium shrink-0">{$i18n.t('Browser')}</div>
			{#if state?.phase}
				<span class="text-[11px] px-1.5 py-0.5 rounded-full font-medium {phaseClass(state.phase)}">
					{phaseLabel(state.phase)}
				</span>
			{/if}
			{#if state && !state.done}
				<span class="relative flex h-2 w-2 shrink-0" title="live">
					<span class="animate-ping absolute inline-flex h-full w-full rounded-full bg-green-400 opacity-75"></span>
					<span class="relative inline-flex rounded-full h-2 w-2 bg-green-500"></span>
				</span>
			{/if}
		</div>
		<button
			class="self-center p-1 rounded-full hover:bg-gray-100 dark:hover:bg-gray-800 transition shrink-0"
			on:click={close}
			aria-label="Close"
		>
			<XMark className="size-4" />
		</button>
	</div>

	{#if tabs.length > 1}
		<!-- Tab strip: one tab per concurrent browser session (parent + subagents).
		     Clicking pins the panel to that tab (auto-follow resumes only if the tab
		     disappears). A pulsing dot marks tabs that are still live. -->
		<div class="flex items-center gap-1 overflow-x-auto pb-2 scrollbar-none">
			{#each tabs as tab (tab.id)}
				<button
					class="flex items-center gap-1.5 px-2 py-1 rounded-lg text-xs whitespace-nowrap transition shrink-0
						{tab.id === activeId
							? 'bg-gray-200 dark:bg-gray-800 text-gray-900 dark:text-gray-100'
							: 'text-gray-500 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-850'}"
					on:click={() => selectTab(tab.id)}
					title={tab.url ?? tabLabel(tab)}
				>
					{#if !tab.done}
						<span class="relative flex h-1.5 w-1.5 shrink-0">
							<span class="animate-ping absolute inline-flex h-full w-full rounded-full bg-green-400 opacity-75"></span>
							<span class="relative inline-flex rounded-full h-1.5 w-1.5 bg-green-500"></span>
						</span>
					{:else}
						<span class="inline-flex rounded-full h-1.5 w-1.5 bg-gray-400 shrink-0"></span>
					{/if}
					<span class="truncate max-w-[10rem]">{tabLabel(tab)}</span>
				</button>
			{/each}
		</div>
	{/if}

	{#if state}
		<div class="flex-1 min-h-0 overflow-hidden rounded-lg bg-gray-50 dark:bg-gray-900 flex items-center justify-center">
			{#if state.frame}
				<!-- No {#key}: updating src in place avoids remounting <Image> (and its
				     zoom preview) every frame, which would flicker ~1/s. className sizes
				     the button wrapper; imageClassName sizes the <img> itself. -->
				<Image
					src={state.frame}
					alt="Live browser view"
					className="max-w-full max-h-full flex"
					imageClassName="rounded-lg max-w-full max-h-full object-contain"
				/>
			{:else}
				<div class="text-xs text-gray-400 p-4">{$i18n.t('Waiting for the first frame…')}</div>
			{/if}
		</div>

		<div class="pt-2 space-y-1">
			{#if state.url}
				<div class="text-xs text-gray-500 dark:text-gray-400 truncate" title={state.url}>
					{state.url}
				</div>
			{/if}
			<div class="flex items-center justify-between text-xs text-gray-400">
				<span class="truncate">{state.title ?? ''}</span>
				<span class="shrink-0 tabular-nums">{elapsedText}</span>
			</div>
		</div>
	{:else}
		<div class="flex-1 flex items-center justify-center text-xs text-gray-400">
			{$i18n.t('No active browser session.')}
		</div>
	{/if}
</div>
