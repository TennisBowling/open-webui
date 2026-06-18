<script lang="ts">
	import { getContext, onDestroy } from 'svelte';
	import { slide } from 'svelte/transition';
	import { quintOut } from 'svelte/easing';

	import { formatDuration } from '$lib/utils';
	import Spinner from '$lib/components/common/Spinner.svelte';
	import Markdown from './Markdown.svelte';
	import ToolCallsBlock from './ToolCallsBlock.svelte';

	const i18n: any = getContext('i18n');

	// One render member of the burst. `projection` is the pre-computed markdown
	// string for non-tool blocks; `toolPayload` is the JSON string for a
	// tool_calls block. Both come straight from ContentRenderer's projection
	// cache (indexed by the original block index) so we never re-project here.
	export let members: {
		index: number;
		block: any;
		projection: string;
		toolPayload: string;
	}[] = [];

	// True while this burst is the live, trailing agentic work in an actively
	// generating message. Drives the spinner, the ticking timer, and (with
	// autoExpand) the open state.
	export let working = false;
	// Variant: auto-expand while working (default) vs stay collapsed.
	export let autoExpand = true;
	// Whether the message finished streaming a terminal error.
	export let errored = false;
	// Whether the message was stopped by the user — chooses "Stopped after X".
	export let messageStopped = false;

	// Passthroughs the inner Markdown / ToolCallsBlock need.
	export let done = true;
	// Id prefix for inner block ids — must match the non-grouped path's
	// `${id}-b${index}` so Markdown's `{#key id}` doesn't remount on toggle.
	export let idPrefix = '';
	export let chatId = '';
	export let messageId = '';
	export let model: any = null;
	export let save = false;
	export let preview = false;
	export let editCodeBlock = true;
	export let sourceIds: string[] = [];
	export let dataVizOverrides: any = {};
	export let sandboxFiles: any[] = [];
	// `any` (not a typed signature) so passing these to Markdown/ToolCallsBlock —
	// whose handler props svelte-check infers as the narrow `() => void` — doesn't
	// trip strict prop assignability in this .ts component. The runtime callbacks
	// are still invoked with their real args.
	export let onSave: any = () => {};
	export let onSourceClick: any = () => {};
	export let onTaskClick: any = () => {};
	export let onArtifactDetected: any = () => {};
	export let onPreview: any = () => {};

	const num = (v: unknown): number | null => (typeof v === 'number' && !isNaN(v) ? v : null);

	// --- Timing ---------------------------------------------------------------
	// Each member block carries server-stamped epochs (UNIX seconds, float):
	// reasoning + tool_calls both have started_at, and ended_at once closed.
	// The burst's window is [min(started_at), max(ended_at)]. While working we
	// tick to `now`; once terminal we freeze at the last ended_at.
	$: startedAts = members
		.map((m) => num(m.block?.started_at))
		.filter((v): v is number => v != null);
	$: endedAts = members
		.map((m) => num(m.block?.ended_at))
		.filter((v): v is number => v != null);
	$: startedAt = startedAts.length ? Math.min(...startedAts) : null;
	// A member that has started but not ended means the burst is still open even
	// if the caller hasn't flagged `working` yet (brief race at stream tail).
	$: hasOpenMember = members.some(
		(m) => num(m.block?.started_at) != null && num(m.block?.ended_at) == null
	);
	$: active = working || hasOpenMember;
	$: endedAt = active ? null : endedAts.length ? Math.max(...endedAts) : null;

	let nowTs = Math.floor(Date.now() / 1000);
	let timerInterval: ReturnType<typeof setInterval> | null = null;
	// A single self-managing 1s ticker, held ONLY while active. `nowTs` is
	// isolated so a tick recomputes just the timer string, never the heavy
	// body / projections.
	$: if (typeof window !== 'undefined') {
		const shouldTick = active && startedAt != null;
		if (shouldTick && !timerInterval) {
			nowTs = Math.floor(Date.now() / 1000);
			timerInterval = setInterval(() => {
				nowTs = Math.floor(Date.now() / 1000);
			}, 1000);
		} else if (!shouldTick && timerInterval) {
			clearInterval(timerInterval);
			timerInterval = null;
		}
	}

	$: elapsedSeconds =
		startedAt == null
			? null
			: active
				? Math.max(0, nowTs - startedAt)
				: endedAt != null
					? Math.max(0, endedAt - startedAt)
					: null;
	$: timerText = elapsedSeconds == null ? '' : formatDuration(elapsedSeconds);

	$: headerText = active
		? timerText
			? $i18n.t('Working for {{duration}}', { duration: timerText })
			: $i18n.t('Working…')
		: messageStopped
			? timerText
				? $i18n.t('Stopped after {{duration}}', { duration: timerText })
				: $i18n.t('Stopped')
			: timerText
				? $i18n.t('Worked for {{duration}}', { duration: timerText })
				: $i18n.t('Worked');

	// --- Expand / collapse ----------------------------------------------------
	// Mirrors Collapsible.svelte's reasoning auto-expand: open while working,
	// collapse when done — but stop fighting the user once they click the header.
	let open = autoExpand ? working : false;
	let userToggled = false;
	$: if (!userToggled) {
		open = autoExpand ? active : false;
	}

	const toggle = () => {
		userToggled = true;
		open = !open;
	};

	// --- Windowing ------------------------------------------------------------
	// A single agentic burst can absorb every reasoning + tool_calls block of a
	// 100–300 tool-call response. With the group expanded (the default while
	// working), mounting a Collapsible/Markdown for EVERY member at once is what
	// makes long agentic runs freeze on mobile. So when expanded we mount only
	// the most recent `WINDOW` members fully and collapse the older ones behind a
	// one-click "show N earlier steps" affordance. The streaming tail is always
	// in the recent window, so live updates are unaffected.
	const WINDOW = 20;
	let showAllMembers = false;
	$: hiddenMemberCount = Math.max(0, members.length - WINDOW);
	$: visibleMembers =
		showAllMembers || members.length <= WINDOW ? members : members.slice(members.length - WINDOW);

	onDestroy(() => {
		if (timerInterval) {
			clearInterval(timerInterval);
			timerInterval = null;
		}
	});

	$: stepCount = members.filter(
		(m) => m.block?.type === 'reasoning' || m.block?.type === 'tool_calls'
	).length;
</script>

<div
	class="my-2 rounded-2xl border border-gray-200 dark:border-gray-800 bg-white/40 dark:bg-gray-900/40 overflow-hidden working-block"
	data-message-id={messageId}
>
	<button
		type="button"
		class="w-full flex items-center gap-2 px-3 py-2 text-left hover:opacity-90 transition-opacity"
		on:click|preventDefault={toggle}
		aria-expanded={open}
	>
		<!-- Status icon -->
		<span class="shrink-0 inline-flex items-center justify-center size-5">
			{#if active}
				<Spinner className="size-4" />
			{:else if errored}
				<svg
					xmlns="http://www.w3.org/2000/svg"
					viewBox="0 0 24 24"
					fill="none"
					stroke="currentColor"
					stroke-width="2"
					stroke-linecap="round"
					stroke-linejoin="round"
					class="size-4 text-red-600 dark:text-red-500"
				>
					<line x1="18" y1="6" x2="6" y2="18" />
					<line x1="6" y1="6" x2="18" y2="18" />
				</svg>
			{:else if messageStopped}
				<svg
					xmlns="http://www.w3.org/2000/svg"
					viewBox="0 0 24 24"
					fill="none"
					stroke="currentColor"
					stroke-width="2"
					stroke-linecap="round"
					stroke-linejoin="round"
					class="size-4 text-gray-500"
				>
					<circle cx="12" cy="12" r="10" />
					<line x1="4.93" y1="4.93" x2="19.07" y2="19.07" />
				</svg>
			{:else}
				<svg
					xmlns="http://www.w3.org/2000/svg"
					viewBox="0 0 24 24"
					fill="none"
					stroke="currentColor"
					stroke-width="2"
					stroke-linecap="round"
					stroke-linejoin="round"
					class="size-4 text-gray-500 dark:text-gray-400"
				>
					<circle cx="12" cy="12" r="10" />
					<polyline points="12 6 12 12 16 14" />
				</svg>
			{/if}
		</span>

		<!-- Header text -->
		<span
			class="flex-1 min-w-0 text-sm font-medium {active
				? 'text-blue-600 dark:text-blue-400 shimmer'
				: 'text-gray-700 dark:text-gray-300'} truncate"
		>
			{headerText}
		</span>

		{#if stepCount > 0}
			<span class="shrink-0 text-xs text-gray-400 dark:text-gray-500 tabular-nums">
				{stepCount}
				{stepCount === 1 ? $i18n.t('step') : $i18n.t('steps')}
			</span>
		{/if}

		<!-- Caret -->
		<svg
			xmlns="http://www.w3.org/2000/svg"
			viewBox="0 0 24 24"
			fill="none"
			stroke="currentColor"
			stroke-width="2"
			stroke-linecap="round"
			stroke-linejoin="round"
			class="size-4 shrink-0 transition-transform text-gray-500 {open ? 'rotate-180' : ''}"
		>
			<polyline points="6 9 12 15 18 9" />
		</svg>
	</button>

	{#if open}
		<div transition:slide={{ duration: 250, easing: quintOut }} class="px-3 pb-3 space-y-1">
			{#if hiddenMemberCount > 0 && !showAllMembers}
				<button
					type="button"
					class="w-full text-left text-xs text-gray-500 dark:text-gray-400 hover:underline py-1"
					on:click|preventDefault={() => (showAllMembers = true)}
				>
					{$i18n.t('Show {{count}} earlier steps', { count: hiddenMemberCount })}
				</button>
			{/if}
			{#each visibleMembers as member (member.index)}
				{#if member.block?.type === 'tool_calls'}
					<ToolCallsBlock
						id={`${idPrefix}-b${member.index}`}
						blockJson={member.toolPayload ?? ''}
						{chatId}
						{messageId}
					/>
				{:else if member.block?.type === 'reasoning'}
					<Markdown
						id={`${idPrefix}-b${member.index}`}
						content={member.projection ?? ''}
						{model}
						{save}
						{preview}
						{done}
						{editCodeBlock}
						{chatId}
						{messageId}
						{dataVizOverrides}
						{sandboxFiles}
						{sourceIds}
						{onSourceClick}
						{onTaskClick}
						{onSave}
						onUpdate={onArtifactDetected}
						onPreview={onPreview}
					/>
				{/if}
				<!-- empty placeholder text members render nothing -->
			{/each}
		</div>
	{/if}
</div>

<style>
	.working-block :global(details summary) {
		cursor: pointer;
	}
</style>
