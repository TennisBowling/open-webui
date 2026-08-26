<script lang="ts">
	import { getContext } from 'svelte';
	import {
		browserLiveStates,
		showBrowserPanel,
		showControls,
		browserPanelDismissed,
		type BrowserLiveState
	} from '$lib/stores';
	import Image from '$lib/components/common/Image.svelte';
	import XMark from '$lib/components/icons/XMark.svelte';
	import { browserHumanHandoff, browserLiveFrame } from '$lib/apis/chats';
	import { toast } from '$lib/utils/toast';
	import { fitRect, isDragGesture, mapClientToImage } from '$lib/utils/browserGeometry';

	const i18n: any = getContext('i18n');

	interface Props {
		history?: any;
		chatId?: string | null;
	}

	let { history = { messages: {}, currentId: null }, chatId = null }: Props = $props();

	// browserLiveStates is keyed by per-agent browser SESSION (one entry per
	// concurrent browsing agent/tab). With parallel browsing the parent ("main")
	// and each subagent each have their own entry, so we render a TAB STRIP and let
	// the user pick which tab to watch; we auto-follow the live/most-recent one
	// until the user explicitly selects a tab.
	let states = $derived($browserLiveStates);

	// Ordered list of tabs (sessions) for the strip. Oldest-first so a newly
	// spawned subagent's tab appears at the end and positions stay stable.
	let tabs = $derived(
		Object.entries(states ?? {})
			.map(([id, v]) => ({ id, ...(v as any) }))
			.sort((a, b) => (a.startedAt ?? 0) - (b.startedAt ?? 0))
	);

	// The user's explicit tab pick (null = auto-follow). Reset to auto when the
	// picked tab disappears (e.g. its page was reclaimed).
	let selectedId: string | null = $state(null);
	$effect(() => {
		if (selectedId && !states?.[selectedId]) selectedId = null;
	});

	let autoId = $derived(pickActiveId(states, history?.currentId));
	let activeId = $derived(selectedId ?? autoId);

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

	let activeState = $derived(activeId ? states[activeId] : null);
	let handoffBusy = $state(false);

	// --- handoff state merging + toast -----------------------------------------
	// ONE merge path for both sources (POST handoff responses AND the GET live
	// poll), and ONE transition rule: toast exactly once per session when
	// requiresHuman flips true -> false (solve, dismiss, or daemon auto-clear).
	type HandoffPayload =
		| { action: 'snapshot' }
		| { action: 'click'; x: number; y: number }
		| { action: 'drag'; x: number; y: number; x2: number; y2: number }
		| { action: 'scroll'; delta_y: number; delta_x?: number }
		| { action: 'type'; text: string; x?: number; y?: number }
		| { action: 'dismiss' };

	const clearedToasted: Record<string, boolean> = {};

	function notifyIfCleared(
		sessionId: string,
		wasRequired: boolean,
		nowClear: boolean,
		dismissed: boolean,
		autoSolved = false
	) {
		if (!wasRequired || !nowClear) return;
		if (clearedToasted[sessionId]) return;
		clearedToasted[sessionId] = true;
		toast.success(
			dismissed
				? $i18n.t('Verification skipped. Tell the agent to continue.')
				: autoSolved
					? $i18n.t('Auto-solved by CapSolver.')
					: $i18n.t('Verification complete. Tell the agent to continue.')
		);
	}

	const autoSolved = (state?: BrowserLiveState | null) =>
		state?.verification?.solver?.state === 'solved';

	function applyHandoffResponse(
		sessionId: string,
		response: { state?: BrowserLiveState; frame?: string },
		action: string
	) {
		const state = response?.state ?? {};
		const wasRequired = !!states?.[sessionId]?.requiresHuman;
		if (state.requiresHuman === true) clearedToasted[sessionId] = false;
		browserLiveStates.update((current) => ({
			...current,
			[sessionId]: {
				...(current[sessionId] ?? {}),
				...state,
				...(response?.frame ? { frame: response.frame } : {}),
				startedAt: current[sessionId]?.startedAt ?? Date.now()
			}
		}));
		notifyIfCleared(
			sessionId,
			wasRequired,
			state.requiresHuman === false,
			action === 'dismiss',
			autoSolved(state)
		);
	}

	function applyLivePayload(payload: BrowserLiveState) {
		const sessionId = payload?.session;
		if (!sessionId) return;
		const wasRequired = !!states?.[sessionId]?.requiresHuman;
		if (payload.requiresHuman === true) clearedToasted[sessionId] = false;
		browserLiveStates.update((current) => ({
			...current,
			[sessionId]: {
				...(current[sessionId] ?? {}),
				...payload,
				startedAt: payload?.startedAt ?? current[sessionId]?.startedAt ?? Date.now()
			}
		}));
		notifyIfCleared(
			sessionId,
			wasRequired,
			payload?.requiresHuman === false,
			false,
			autoSolved(payload)
		);
	}

	// --- handoff send: a FIFO drain, NOT a drop-when-busy guard -----------------
	// Multi-tile challenges need several clicks in a row; the old in-flight guard
	// silently DROPPED clicks while one was in flight, which is exactly why tile
	// grids felt unsolvable. Gestures queue and drain in order; the busy overlay
	// is pointer-transparent so queuing keeps working mid-drain.
	const MAX_HANDOFF_QUEUE = 8;
	let handoffQueue: {
		chatId: string;
		payload: HandoffPayload;
		sessionId: string;
		silent: boolean;
	}[] = [];
	let handoffDraining = false;

	const runHandoff = (
		payload: HandoffPayload,
		sessionId: string | null = activeId,
		silent = false
	) => {
		if (!chatId || !sessionId) return;
		if (handoffQueue.length >= MAX_HANDOFF_QUEUE) return;
		handoffQueue.push({ chatId, payload, sessionId, silent });
		void drainHandoff();
	};

	const drainHandoff = async () => {
		if (handoffDraining) return;
		handoffDraining = true;
		handoffBusy = true;
		try {
			while (handoffQueue.length) {
				const item = handoffQueue.shift()!;
				try {
					const response = await browserHumanHandoff(localStorage.token, item.chatId, {
						session: item.sessionId,
						...item.payload
					});
					applyHandoffResponse(item.sessionId, response, item.payload.action ?? 'click');
				} catch (error: unknown) {
					if (!item.silent) {
						toast.error(
							(error as { detail?: string } | null)?.detail ??
								$i18n.t('Browser verification action failed')
						);
					}
				}
			}
		} finally {
			handoffDraining = false;
			handoffBusy = false;
		}
	};

	// --- gesture layer ----------------------------------------------------------
	// The verification image is interactive: pointer sequences classify as tap vs
	// drag (slider captchas), wheel scrolls the page (below-fold widgets), and a
	// type box sends keystrokes (text/audio captchas). All coordinates map
	// through the object-contain FIT rect, so letterbox clicks can never land
	// off-target.
	let wrapEl: HTMLDivElement | null = $state(null);
	let imgEl: HTMLImageElement | null = $state(null);

	let gestureStart: { clientX: number; clientY: number; x: number; y: number } | null =
		$state(null);
	let isDragging = $state(false);
	let trail: { sx: number; sy: number; ex: number; ey: number } | null = $state(null);
	let lastClickPoint: { x: number; y: number } | null = null;
	let typeText = $state('');
	let gesturePauseUntil = 0;

	let scrollAccum = 0;
	let scrollTimer: ReturnType<typeof setTimeout> | null = null;

	const markGesture = () => {
		// Polling pauses for a beat after any gesture so a live-frame refresh can't
		// swap the image under the user's cursor mid-solve.
		gesturePauseUntil = Date.now() + 5000;
	};

	const pointForEvent = (event: PointerEvent): { x: number; y: number } | null => {
		const el = imgEl;
		if (!el?.naturalWidth || !el?.naturalHeight) return null;
		const rect = el.getBoundingClientRect();
		if (!rect.width || !rect.height) return null;
		const fit = fitRect(rect.width, rect.height, el.naturalWidth, el.naturalHeight);
		if (!fit) return null;
		return mapClientToImage(
			event.clientX,
			event.clientY,
			rect,
			fit,
			el.naturalWidth,
			el.naturalHeight
		);
	};

	const localPoint = (event: PointerEvent): { x: number; y: number } | null => {
		const r = wrapEl?.getBoundingClientRect();
		if (!r) return null;
		return { x: event.clientX - r.left, y: event.clientY - r.top };
	};

	const handlePointerDown = (event: PointerEvent) => {
		if (!activeState?.requiresHuman || handoffDraining) return;
		const pt = pointForEvent(event);
		if (!pt) return;
		gestureStart = { clientX: event.clientX, clientY: event.clientY, ...pt };
		isDragging = false;
		const local = localPoint(event);
		if (local) trail = { sx: local.x, sy: local.y, ex: local.x, ey: local.y };
		(event.currentTarget as HTMLElement).setPointerCapture(event.pointerId);
	};

	const handlePointerMove = (event: PointerEvent) => {
		if (!gestureStart) return;
		if (!isDragging && isDragGesture(gestureStart, { x: event.clientX, y: event.clientY })) {
			isDragging = true;
		}
		const local = localPoint(event);
		if (local && trail) trail = { ...trail, ex: local.x, ey: local.y };
	};

	const handlePointerUp = (event: PointerEvent) => {
		if (!gestureStart || !activeState?.requiresHuman) {
			gestureStart = null;
			isDragging = false;
			trail = null;
			return;
		}
		const client = { x: event.clientX, y: event.clientY };
		const drag = isDragging || isDragGesture(gestureStart, client);
		const endPt = pointForEvent(event);
		const start = gestureStart;
		gestureStart = null;
		isDragging = false;
		trail = null;
		markGesture();
		if (drag) {
			if (start && endPt) {
				void runHandoff({
					action: 'drag',
					x: start.x,
					y: start.y,
					x2: endPt.x,
					y2: endPt.y
				});
			}
		} else if (endPt) {
			lastClickPoint = { ...endPt };
			void runHandoff({ action: 'click', x: endPt.x, y: endPt.y });
		}
	};

	const handlePointerCancel = () => {
		gestureStart = null;
		isDragging = false;
		trail = null;
	};

	const handleWheel = (event: WheelEvent) => {
		if (!activeState?.requiresHuman) return;
		event.preventDefault();
		scrollAccum += event.deltaY;
		if (scrollTimer) return;
		scrollTimer = setTimeout(async () => {
			scrollTimer = null;
			const dy = scrollAccum;
			scrollAccum = 0;
			if (!dy) return;
			markGesture();
			void runHandoff({ action: 'scroll', delta_y: dy });
		}, 120);
	};

	const sendType = () => {
		const text = typeText.trim();
		if (!text || !activeState?.requiresHuman) return;
		typeText = '';
		markGesture();
		// Focus the last place the user clicked (challenge inputs don't always
		// auto-focus), then type.
		void runHandoff({ action: 'type', text, ...(lastClickPoint ?? {}) });
	};

	// --- live poll while the handoff is armed -----------------------------------
	// GETs the daemon's host-side live state instead of POSTing snapshot handoffs:
	// no container round-trip, no detection re-run, no per-session mutex
	// contention with the user's own gestures. This is ALSO what surfaces the
	// daemon's auto-clear (solve detected by the watcher) to the panel.
	$effect(() => {
		const sessionId = activeId;
		if (!chatId || !sessionId || !activeState?.requiresHuman) return;
		let stopped = false;
		let timer: ReturnType<typeof setTimeout>;

		const tick = async () => {
			if (stopped) return;
			if (Date.now() >= gesturePauseUntil) {
				try {
					const res = await browserLiveFrame(localStorage.token, chatId);
					if (stopped || !res) return;
					const mine =
						(res?.sessions ?? []).find((s: BrowserLiveState) => s?.session === sessionId) ??
						(res?.session === sessionId ? res : null);
					if (mine) applyLivePayload(mine);
				} catch {
					// Poll failures are silent: the next tick (or a POST response) recovers.
				}
			}
			timer = setTimeout(tick, 1000);
		};
		timer = setTimeout(tick, 1000);
		return () => {
			stopped = true;
			clearTimeout(timer);
		};
	});

	// Elapsed timer — isolated tick so only the timer string recomputes, never the
	// frame image. Ticks only while the active tab is live (not done).
	let nowTs = $state(Date.now());
	$effect(() => {
		const shouldTick = !!activeState && !activeState.done && (activeState.startedAt ?? 0) > 0;
		if (!shouldTick) return;

		nowTs = Date.now();
		const timerInterval = setInterval(() => (nowTs = Date.now()), 1000);
		return () => clearInterval(timerInterval);
	});

	let elapsedMs = $derived(
		activeState
			? activeState.done
				? (activeState.elapsedMs ?? 0)
				: Math.max(0, nowTs - (activeState.startedAt ?? nowTs))
			: 0
	);
	let elapsedText = $derived(formatElapsed(elapsedMs));

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
			case 'verification_required':
				return 'Verification needed';
			case 'done':
				return 'Idle';
			default:
				return phase ?? '';
		}
	}

	function phaseClass(phase: string | undefined): string {
		switch (phase) {
			case 'navigating':
				return 'bg-book-cloth/15 text-book-cloth dark:text-kraft';
			case 'loaded':
				return 'bg-success/15 text-success dark:text-success-dark';
			case 'acting':
				return 'bg-warning/15 text-warning dark:text-warning-dark';
			case 'verification_required':
				return 'bg-warning/15 text-warning dark:text-warning-dark';
			default:
				return 'bg-gray-500/15 text-gray-500 dark:text-gray-400';
		}
	}

	// The phase badge + banner override while the auto-solver is working: the
	// handoff is still armed (the human can grab it any time) but the visible
	// state should say what the daemon is doing right now.
	const solverState = $derived(activeState?.verification?.solver?.state ?? null);
	const solverProvider = $derived(activeState?.verification?.solver?.provider ?? '');
	const solverMessage = $derived(activeState?.verification?.solver?.message ?? '');

	function badgeLabel(): string {
		if (solverState === 'solving') return $i18n.t('Auto-solving…');
		if (activeState?.phase === 'verification_required') return $i18n.t('Verification needed');
		return phaseLabel(activeState?.phase);
	}

	const close = () => {
		if (activeState?.requiresHuman) {
			// A verification handoff is BLOCKING: hiding the panel doesn't
			// abandon it. Chat.svelte's re-open guard brings it back until the
			// challenge is solved or dismissed ("No challenge here"), so closing
			// here is only a temporary hide — say so instead of silently
			// bricking the chat.
			toast.info(
				$i18n.t(
					'Verification is still pending — the panel will reopen. Use “No challenge here” to skip it.'
				)
			);
		} else {
			// Remember the user dismissed it so a later frame doesn't reopen it
			// this turn (auto-open still works next turn).
			browserPanelDismissed.set(true);
		}
		showBrowserPanel.set(false);
		showControls.set(false);
	};
</script>

<div class="w-full h-full flex flex-col">
	<div class="flex items-center justify-between gap-2 px-1 pb-2">
		<div class="flex items-center gap-2 min-w-0">
			<div class="text-sm font-medium shrink-0">{$i18n.t('Browser')}</div>
			{#if activeState?.phase || solverState}
				<span
					class="text-[11px] px-1.5 py-0.5 rounded-full font-medium {phaseClass(
						solverState === 'solving' ? 'acting' : activeState.phase
					)}"
				>
					{badgeLabel()}
				</span>
			{/if}
			{#if activeState && !activeState.done}
				<span class="relative flex h-2 w-2 shrink-0" title="live">
					<span
						class="animate-ping absolute inline-flex h-full w-full rounded-full bg-kraft opacity-75"
					></span>
					<span class="relative inline-flex rounded-full h-2 w-2 bg-book-cloth"></span>
				</span>
			{/if}
		</div>
		<button
			class="self-center p-1 max-md:p-2 rounded-full hover:bg-gray-100 dark:hover:bg-gray-800 transition shrink-0"
			onclick={close}
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
					class="flex items-center gap-1.5 px-2 py-1 max-md:py-2 rounded-lg text-xs whitespace-nowrap transition shrink-0
						{tab.id === activeId
						? 'bg-gray-200 dark:bg-gray-800 text-gray-900 dark:text-gray-100'
						: 'text-gray-500 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-850'}"
					onclick={() => selectTab(tab.id)}
					title={tab.url ?? tabLabel(tab)}
				>
					{#if !tab.done}
						<span class="relative flex h-1.5 w-1.5 shrink-0">
							<span
								class="animate-ping absolute inline-flex h-full w-full rounded-full bg-kraft opacity-75"
							></span>
							<span class="relative inline-flex h-1.5 w-1.5 bg-book-cloth"></span>
						</span>
					{:else}
						<span class="inline-flex rounded-full h-1.5 w-1.5 bg-gray-400 shrink-0"></span>
					{/if}
					<span class="truncate max-w-[10rem]">{tabLabel(tab)}</span>
				</button>
			{/each}
		</div>
	{/if}

	{#if activeState}
		{#if activeState.requiresHuman}
			<div
				class="mb-2 rounded-lg border border-warning/30 bg-warning/10 px-3 py-2 text-xs text-gray-700 dark:text-gray-200"
			>
				<div class="font-medium">
					{$i18n.t('Human verification required')}
					{#if activeState.verification?.provider}
						· {activeState.verification.provider}
					{/if}
				</div>
				<div class="mt-1 text-gray-500 dark:text-gray-400">
					{$i18n.t(
						'Click the tiles, drag sliders, scroll with the wheel, or type the code. Agent clicks and typing are paused; this tab and its cookies will be preserved.'
					)}
				</div>
				{#if solverState === 'solving'}
					<div
						class="mt-2 flex items-center gap-1.5 text-xs font-medium text-warning dark:text-warning-dark"
					>
						<span class="relative flex h-1.5 w-1.5 shrink-0">
							<span
								class="animate-ping absolute inline-flex h-full w-full rounded-full bg-warning opacity-75"
							></span>
							<span class="relative inline-flex rounded-full h-1.5 w-1.5 bg-warning"></span>
						</span>
						{$i18n.t('Auto-solving')}
						{#if solverProvider}{solverProvider}{:else}{$i18n.t('the challenge')}{/if}…
					</div>
				{:else if solverState === 'failed'}
					<div class="mt-2 text-xs text-red-600 dark:text-red-400">
						{$i18n.t('Auto-solve failed')}: {solverMessage} — {$i18n.t(
							'you can still solve it manually below'
						)}.
					</div>
				{/if}
				<div class="mt-2 flex flex-wrap items-center gap-2">
					<input
						class="w-40 rounded-md border border-gray-200 bg-white px-2 py-1 text-xs text-gray-800 placeholder-gray-400 focus:outline-none focus:border-book-cloth dark:border-gray-700 dark:bg-gray-800 dark:text-gray-100"
						type="text"
						bind:value={typeText}
						placeholder={$i18n.t('Type the code…')}
						onkeydown={(e) => {
							if (e.key === 'Enter') sendType();
						}}
					/>
					<button
						class="rounded-md border-hairline border-gray-200 px-2 py-1 font-medium hover:bg-white dark:border-gray-700 dark:hover:bg-gray-800"
						type="button"
						disabled={!typeText.trim()}
						onclick={sendType}
					>
						{$i18n.t('Send')}
					</button>
					<button
						class="rounded-md border-hairline border-gray-200 px-2 py-1 font-medium hover:bg-white dark:border-gray-700 dark:hover:bg-gray-800"
						type="button"
						disabled={handoffBusy}
						onclick={() => void runHandoff({ action: 'snapshot' })}
					>
						{$i18n.t('Refresh')}
					</button>
					<button
						class="rounded-md border-hairline border-gray-200 px-2 py-1 font-medium text-gray-500 hover:bg-white dark:border-gray-700 dark:hover:bg-gray-800"
						type="button"
						onclick={() => void runHandoff({ action: 'dismiss' })}
					>
						{$i18n.t('No challenge here — continue')}
					</button>
				</div>
			</div>
		{/if}
		<div
			bind:this={wrapEl}
			class="flex-1 min-h-0 overflow-hidden rounded-lg bg-gray-50 dark:bg-gray-900 flex items-center justify-center"
		>
			{#if activeState.frame}
				{#if activeState.requiresHuman}
					<div class="relative max-w-full max-h-full flex">
						<img
							bind:this={imgEl}
							src={activeState.frame}
							alt={$i18n.t('Interactive browser verification')}
							class="rounded-lg max-w-full max-h-full object-contain cursor-crosshair select-none"
							draggable="false"
							style="touch-action: none"
							onpointerdown={handlePointerDown}
							onpointermove={handlePointerMove}
							onpointerup={handlePointerUp}
							onpointercancel={handlePointerCancel}
							onwheel={handleWheel}
						/>
						{#if trail}
							<svg class="absolute inset-0 w-full h-full pointer-events-none" style="z-index: 10">
								<line
									x1={trail.sx}
									y1={trail.sy}
									x2={trail.ex}
									y2={trail.ey}
									stroke="rgba(249,115,22,0.9)"
									stroke-width="2"
									stroke-dasharray="4 3"
								/>
								<circle cx={trail.ex} cy={trail.ey} r="4" fill="rgba(249,115,22,0.9)" />
							</svg>
						{/if}
						{#if handoffBusy}
							<div
								class="absolute inset-0 flex items-center justify-center rounded-lg bg-black/20 text-xs font-medium text-white pointer-events-none"
							>
								{$i18n.t('Updating…')}
							</div>
						{/if}
					</div>
				{:else}
					<!-- No {#key}: updating src in place avoids remounting <Image> (and its
					     zoom preview) every frame, which would flicker ~1/s. -->
					<Image
						src={activeState.frame}
						alt="Live browser view"
						className="max-w-full max-h-full flex"
						imageClassName="rounded-lg max-w-full max-h-full object-contain"
					/>
				{/if}
			{:else}
				<div class="text-xs text-gray-400 p-4">{$i18n.t('Waiting for the first frame…')}</div>
			{/if}
		</div>

		<div class="pt-2 space-y-1">
			{#if solverState === 'solved' && !activeState.requiresHuman}
				<div class="text-xs text-success dark:text-success-dark">
					{solverMessage || $i18n.t('Auto-solved by CapSolver')}
				</div>
			{/if}
			{#if activeState.url}
				<div class="text-xs text-gray-500 dark:text-gray-400 truncate" title={activeState.url}>
					{activeState.url}
				</div>
			{/if}
			<div class="flex items-center justify-between text-xs text-gray-400">
				<span class="truncate">{activeState.title ?? ''}</span>
				<span class="shrink-0 tabular-nums">{elapsedText}</span>
			</div>
		</div>
	{:else}
		<div class="flex-1 flex items-center justify-center text-xs text-gray-400">
			{$i18n.t('No active browser session.')}
		</div>
	{/if}
</div>
