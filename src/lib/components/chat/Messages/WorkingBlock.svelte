<script lang="ts">
	import { preventDefault } from '$lib/utils/eventModifiers';

	import { getContext, onDestroy } from 'svelte';
	import { slide } from 'svelte/transition';
	import { quintOut } from 'svelte/easing';

	import { formatDurationLong } from '$lib/utils';
	import { reasoningBlockOpenState } from '$lib/stores';
	import Spinner from '$lib/components/common/Spinner.svelte';
	import Markdown from './Markdown.svelte';
	import ToolCallsBlock from './ToolCallsBlock.svelte';
	import RewindBoundary from './RewindBoundary.svelte';

	const i18n: any = getContext('i18n');

	// One render member of the burst. `projection` is the pre-computed markdown
	// string for non-tool blocks; `blockRev` changes when a tool_calls block's
	// render-relevant fields mutate in place. Both come straight from
	// ContentRenderer's projection cache (indexed by the original block index) so

	// True while this burst is the live, trailing agentic work in an actively
	// generating message. Drives the spinner, the ticking timer, and (with

	// Id prefix for inner block ids — must match the non-grouped path's

	// `any` (not a typed signature) so passing these to Markdown/ToolCallsBlock —
	// whose handler props svelte-check infers as the narrow `() => void` — doesn't
	// trip strict prop assignability in this .ts component. The runtime callbacks

	// Block-level rewind, shared from ContentRenderer so a composer can sit between
	// bundled rounds. `activeRewindCut` is the block index whose composer is open
	// (or null). `rewindCuts` holds the valid "between requests" cut indices (just
	// AFTER each completed tool_calls block). A boundary after member m cuts at

	// A cut the parent (ContentRenderer) renders as a top-level boundary AFTER this

	interface Props {
		// we never re-project here.
		members?: {
			index: number;
			block: any;
			projection: string;
			blockRev?: string;
		}[];
		// autoExpand) the open state.
		working?: boolean;
		// Variant: auto-expand while working (default) vs stay collapsed.
		autoExpand?: boolean;
		// Whether the message finished streaming a terminal error.
		errored?: boolean;
		// Whether the message was stopped by the user — chooses "Stopped after X".
		messageStopped?: boolean;
		// Passthroughs the inner Markdown / ToolCallsBlock need.
		done?: boolean;
		// `${id}-b${index}` so Markdown's `{#key id}` doesn't remount on toggle.
		idPrefix?: string;
		chatId?: string;
		messageId?: string;
		model?: any;
		save?: boolean;
		preview?: boolean;
		editCodeBlock?: boolean;
		sourceIds?: string[];
		dataVizOverrides?: any;
		sandboxFiles?: any[];
		// are still invoked with their real args.
		onSave?: any;
		onSourceClick?: any;
		onTaskClick?: any;
		onArtifactDetected?: any;
		onPreview?: any;
		// m.index+1 (keeps blocks ≤ m), so a parallel tool batch is never split.
		rewindEnabled?: boolean;
		rewindCuts?: Set<number>;
		// card (the group's "after all the work" point); skip it here to avoid a dup.
		skipCut?: number;
		activeRewindCut?: number | null;
		onRewindActivate?: (cutIndex: number) => void;
		onRewindCancel?: () => void;
		onRewindSubmit?: (cutIndex: number, text: string) => void;
	}

	let {
		members = [],
		working = false,
		autoExpand = true,
		errored = false,
		messageStopped = false,
		done = true,
		idPrefix = '',
		chatId = '',
		messageId = '',
		model = null,
		save = false,
		preview = false,
		editCodeBlock = true,
		sourceIds = [],
		dataVizOverrides = {},
		sandboxFiles = [],
		onSave = () => {},
		onSourceClick = () => {},
		onTaskClick = () => {},
		onArtifactDetected = () => {},
		onPreview = () => {},
		rewindEnabled = false,
		rewindCuts = new Set(),
		skipCut = -1,
		activeRewindCut = null,
		onRewindActivate = () => {},
		onRewindCancel = () => {},
		onRewindSubmit = () => {}
	}: Props = $props();

	const num = (v: unknown): number | null => (typeof v === 'number' && !isNaN(v) ? v : null);

	// --- Timing ---------------------------------------------------------------
	// Each member block carries server-stamped epochs (UNIX seconds, float):
	// reasoning + tool_calls both have started_at, and ended_at once closed.
	// The burst's window is [min(started_at), max(ended_at)]. While working we
	// tick to `now`; once terminal we freeze at the last ended_at.
	let startedAts = $derived(
		members.map((m) => num(m.block?.started_at)).filter((v): v is number => v != null)
	);
	let endedAts = $derived(
		members.map((m) => num(m.block?.ended_at)).filter((v): v is number => v != null)
	);
	let startedAt = $derived(startedAts.length ? Math.min(...startedAts) : null);
	// A member that has started but not ended means the burst is still open even
	// if the caller hasn't flagged `working` yet (brief race at stream tail).
	let hasOpenMember = $derived(
		members.some((m) => num(m.block?.started_at) != null && num(m.block?.ended_at) == null)
	);
	let active = $derived(working || hasOpenMember);
	let endedAt = $derived(active ? null : endedAts.length ? Math.max(...endedAts) : null);

	let nowTs = $state(Math.floor(Date.now() / 1000));
	let timerInterval: ReturnType<typeof setInterval> | null = $state(null);
	// A single self-managing 1s ticker, held ONLY while active. `nowTs` is
	// isolated so a tick recomputes just the timer string, never the heavy
	// body / projections.
	$effect(() => {
		if (typeof window !== 'undefined') {
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
	});

	let elapsedSeconds = $derived(
		startedAt == null
			? null
			: active
				? Math.max(0, nowTs - startedAt)
				: endedAt != null
					? Math.max(0, endedAt - startedAt)
					: null
	);
	let timerText = $derived(
		elapsedSeconds == null
			? ''
			: formatDurationLong(elapsedSeconds, (key, options) => $i18n.t(key, options))
	);

	let headerText = $derived(
		active
			? timerText
				? $i18n.t('Working for {{duration}}', { duration: timerText })
				: $i18n.t('Working…')
			: messageStopped
				? timerText
					? $i18n.t('Stopped after {{duration}}', { duration: timerText })
					: $i18n.t('Stopped')
				: timerText
					? $i18n.t('Worked for {{duration}}', { duration: timerText })
					: $i18n.t('Worked')
	);

	// --- Expand / collapse ----------------------------------------------------
	// Mirrors Collapsible.svelte's reasoning auto-expand: open while working,
	// collapse when done — but stop fighting the user once they click the header.
	// The user's choice is persisted in the shared reasoningBlockOpenState store
	// under a distinct `-wb-` key (never colliding with a member block's
	// `${idPrefix}-b${index}`) so it survives the block→group remount and the
	// windowing of older members, exactly like the inner reasoning blocks.
	let workingKey = $derived(members.length ? `${idPrefix}-wb-${members[0].index}` : '');
	let workingChoice = $derived(
		workingKey && workingKey in $reasoningBlockOpenState
			? $reasoningBlockOpenState[workingKey]
			: undefined
	);

	let open = $state(false);
	$effect.pre(() => {
		if (workingChoice !== undefined) {
			open = workingChoice;
		} else {
			open = autoExpand ? active : false;
		}
	});

	const toggle = () => {
		const next = !open;
		if (workingKey) {
			reasoningBlockOpenState.update((m) => ({ ...m, [workingKey]: next }));
		}
		open = next;
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
	let showAllMembers = $state(false);
	let hiddenMemberCount = $derived(Math.max(0, members.length - WINDOW));
	let visibleMembers = $derived(
		showAllMembers || members.length <= WINDOW ? members : members.slice(members.length - WINDOW)
	);

	onDestroy(() => {
		if (timerInterval) {
			clearInterval(timerInterval);
			timerInterval = null;
		}
	});

	let stepCount = $derived(
		members.filter((m) => m.block?.type === 'reasoning' || m.block?.type === 'tool_calls').length
	);
</script>

<div class="my-2.5 working-block select-none" data-message-id={messageId}>
	<div class="relative flex items-center justify-center py-1">
		<div
			class="absolute inset-x-0 top-1/2 -translate-y-1/2 h-px bg-gray-100 dark:bg-gray-800/80"
		></div>
		<button
			type="button"
			class="relative z-10 inline-flex items-center gap-1.5 text-[10px] font-medium bg-white dark:bg-gray-900 px-2 py-0.5 transition {active
				? 'text-book-cloth dark:text-kraft'
				: 'text-gray-400 dark:text-gray-500 hover:text-gray-700 dark:hover:text-gray-200'}"
			data-anchor-on-click
			onclick={preventDefault(toggle)}
			aria-expanded={open}
		>
			{#if active}
				<Spinner className="size-3" />
			{:else if errored}
				<svg
					xmlns="http://www.w3.org/2000/svg"
					viewBox="0 0 24 24"
					fill="none"
					stroke="currentColor"
					stroke-width="2"
					stroke-linecap="round"
					stroke-linejoin="round"
					class="size-3 shrink-0 text-error-brick dark:text-error-brick-dark"
				>
					<line x1="18" y1="6" x2="6" y2="18" />
					<line x1="6" y1="6" x2="18" y2="18" />
				</svg>
			{/if}

			<!-- shimmer lives on the SPAN, not the button: it sets its own
			     background + background-clip:text, which would replace the
			     button's opaque bg and let the divider line bleed through
			     the label while working. -->
			<span class="truncate tabular-nums {active ? 'shimmer' : ''}">
				{headerText}{stepCount > 0
					? ` · ${stepCount} ${stepCount === 1 ? $i18n.t('step') : $i18n.t('steps')}`
					: ''}
			</span>

			<svg
				xmlns="http://www.w3.org/2000/svg"
				viewBox="0 0 20 20"
				fill="currentColor"
				class="size-3 shrink-0 transition-transform duration-150 {open ? 'rotate-180' : ''}"
				aria-hidden="true"
			>
				<path
					fill-rule="evenodd"
					d="M5.22 8.22a.75.75 0 0 1 1.06 0L10 11.94l3.72-3.72a.75.75 0 1 1 1.06 1.06l-4.25 4.25a.75.75 0 0 1-1.06 0L5.22 9.28a.75.75 0 0 1 0-1.06Z"
					clip-rule="evenodd"
				/>
			</svg>
		</button>
	</div>

	{#if open}
		<div
			transition:slide={{ duration: 250, easing: quintOut }}
			class="mt-1.5 rounded-2xl border-hairline border-gray-200 dark:border-gray-800 bg-gray-50/60 dark:bg-gray-850/40 p-3 space-y-1"
		>
			{#if hiddenMemberCount > 0 && !showAllMembers}
				<button
					type="button"
					class="w-full text-left text-xs text-gray-500 dark:text-gray-400 hover:underline py-1"
					onclick={preventDefault(() => (showAllMembers = true))}
				>
					{$i18n.t('Show {{count}} earlier steps', { count: hiddenMemberCount })}
				</button>
			{/if}
			{#each visibleMembers as member, mi (member.index)}
				{#if member.block?.type === 'tool_calls'}
					<ToolCallsBlock
						id={`${idPrefix}-b${member.index}`}
						block={member.block}
						blockRev={member.blockRev ?? ''}
						{chatId}
						{messageId}
						{dataVizOverrides}
					/>
				{:else if member.block?.type === 'reasoning'}
					<!-- parseImmediately={done}: Markdown's default parse is deferred
					     (50ms timeout / idle for large blocks), which is why a click-expand
					     used to slide open around EMPTY reasoning slots while the tool rows
					     rendered instantly — rows then landed late and jumped into place.
					     Eager parse makes the slide measure the real height once.
					     (Same pattern as ResponseMessage's main content.) -->
					<Markdown
						id={`${idPrefix}-b${member.index}`}
						content={member.projection ?? ''}
						parseImmediately={done}
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
						{onPreview}
					/>
				{/if}
				<!-- empty placeholder text members render nothing -->
				{#if rewindEnabled && mi < visibleMembers.length - 1 && member.index + 1 !== skipCut && rewindCuts.has(member.index + 1)}
					<!-- "Between requests" boundary: just after a completed tool round.
					     The last visible member's trailing boundary (if any) is rendered
					     by ContentRenderer as the gap before the next top-level item. -->
					<RewindBoundary
						{messageId}
						cutIndex={member.index + 1}
						active={activeRewindCut === member.index + 1}
						onActivate={onRewindActivate}
						onCancel={onRewindCancel}
						onSubmit={onRewindSubmit}
					/>
				{/if}
			{/each}
		</div>
	{/if}
</div>

<style>
	.working-block :global(details summary) {
		cursor: pointer;
	}
</style>
