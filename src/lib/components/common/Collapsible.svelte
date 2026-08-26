<script lang="ts" module>
	// Resolved component is kept alongside the promise so a second open (or any
	// later tool call) can render it SYNCHRONOUSLY. Going through `{#await}` for
	// an already-loaded chunk still paints the pending branch for a frame, which
	// is exactly the flash-then-jump this panel is trying to avoid.
	let toolCallResultModulePromise: Promise<any> | null = null;
	let resolvedToolCallResult: any = null;
	const loadToolCallResultModule = () =>
		(toolCallResultModulePromise ??= import('../chat/Messages/ToolCallResult.svelte').then(
			(module) => {
				resolvedToolCallResult = module.default;
				return module;
			}
		));
	let subagentBlockModulePromise: Promise<any> | null = null;
	const loadSubagentBlock = () =>
		(subagentBlockModulePromise ??= import('../chat/Messages/Markdown/SubagentBlock.svelte'));
</script>

<script lang="ts">
	import { decode } from 'html-entities';
	import { v4 as uuidv4 } from 'uuid';

	import { getContext, onMount } from 'svelte';
	import type { Writable } from 'svelte/store';
	import type { i18n as i18nType } from 'i18next';
	const i18n = getContext<Writable<i18nType>>('i18n');

	import dayjs, { setDayjsLocale } from '$lib/dayjs';
	import duration from 'dayjs/plugin/duration';
	import relativeTime from 'dayjs/plugin/relativeTime';

	dayjs.extend(duration);
	dayjs.extend(relativeTime);

	// Assuming $i18n.languages is an array of language codes
	$effect(() => {
		setDayjsLocale($i18n.languages);
	});

	import { slide } from 'svelte/transition';
	import { quintOut } from 'svelte/easing';

	import ChevronDown from '../icons/ChevronDown.svelte';
	import Spinner from './Spinner.svelte';
	import AskUserBlock from '../chat/Messages/Markdown/AskUserBlock.svelte';
	import Image from './Image.svelte';
	import FullHeightIframe from './FullHeightIframe.svelte';
	import { settings, reasoningBlockOpenState } from '$lib/stores';
	import { decodeToolResultText, getToolCallSummary, isBrowserToolName } from '$lib/utils/toolResults';
	import { prefetchLazyBody } from '$lib/utils/lazyBlockBodies';
	import ToolCallRow from '../chat/Messages/ToolCallRow.svelte';
	import TranscriptRow from '../chat/Messages/TranscriptRow.svelte';
	import ResultSkeleton from '../chat/Messages/ToolResults/ResultSkeleton.svelte';

	// Stable per-block identity (`${chatId}-${messageId}-b${index}`) for reasoning
	// blocks. When set, the user's manual open/close choice is persisted in the
	// `reasoningBlockOpenState` store so it survives this component being torn down
	// and re-mounted across stream deltas / the block→group regroup. Empty for all

	interface Props {
		open?: boolean;
		className?: string;
		buttonClassName?: string;
		id?: string;
		title?: any;
		attributes?: Record<string, any> | null;
		// non-reasoning Collapsibles.
		reasoningKey?: string;
		chevron?: boolean;
		grow?: boolean;
		disabled?: boolean;
		hide?: boolean;
		onChange?: Function;
		content?: import('svelte').Snippet;
		children?: import('svelte').Snippet;
	}

	let {
		open = $bindable(false),
		className = '',
		buttonClassName = 'w-fit text-gray-500 hover:text-gray-700 dark:hover:text-gray-300 transition',
		id = '',
		title = null,
		attributes = null,
		reasoningKey = '',
		chevron = false,
		grow = false,
		disabled = false,
		hide = false,
		onChange = () => {},
		content,
		children
	}: Props = $props();

	let isReasoning = $derived(attributes?.type === 'reasoning');

	// The user's durable choice for THIS reasoning block, or undefined when they
	// have never touched it (then the auto-expand / collapse-on-done behavior
	// governs). Lives in a module store so a remount can't silently reset it.
	let reasoningChoice = $derived(
		isReasoning && reasoningKey && reasoningKey in $reasoningBlockOpenState
			? $reasoningBlockOpenState[reasoningKey]
			: undefined
	);

	$effect(() => {
		onChange(open);
	});

	// Reasoning open-state authority. A manual choice (durable) always wins —
	// including over a parent re-pushing `open` every delta and over a fresh mount
	// after the block→group flip. Untouched blocks follow the auto-expand setting
	// (open while thinking, collapse when done), else the static `expandDetails`
	// default. Only governs reasoning; every other Collapsible variant keeps its
	// parent-driven `open`.
	$effect(() => {
		if (isReasoning) {
			if (reasoningChoice !== undefined) {
				open = reasoningChoice;
			} else if ($settings?.autoExpandReasoningDuringStreaming) {
				open = attributes?.done !== 'true';
			} else {
				open = $settings?.expandDetails ?? false;
			}
		}
	});

	function setOpenFromUser(nextOpen: boolean) {
		if (isReasoning && reasoningKey) {
			reasoningBlockOpenState.update((m) => ({ ...m, [reasoningKey]: nextOpen }));
		}
		open = nextOpen;
	}

	// The body this block will have to fetch when it opens, if any. Reasoning and
	// tool results are both shipped as stubs by the backend (`utils/lazy_blocks.py`)
	// and their real text arrives from a separate request.
	let lazyBodyTarget = $derived.by(() => {
		const attrs = attributes;
		if (!attrs) return null;
		const chatId = attrs.chat_id ?? '';
		const messageId = attrs.message_id ?? '';
		if (!chatId || !messageId) return null;
		if (attrs.type === 'reasoning' && attrs.content_lazy === 'true' && attrs.content_ref) {
			return { chatId, messageId, ref: String(attrs.content_ref) };
		}
		if (attrs.type === 'tool_calls' && attrs.result_lazy === 'true') {
			const ref = attrs.tool_call_id ?? attrs.id ?? '';
			if (ref) return { chatId, messageId, ref: String(ref) };
		}
		return null;
	});

	// Start the fetch before the click, so the body is normally cached by the time
	// `open` flips and the block renders complete on its first frame — the slide
	// transition then measures the real height ONCE. Without this the transition
	// always measured a spinner shell and the body snapped in on top of it: the
	// "expands a bit, expands again, then renders" stutter. No-ops when already
	// cached or in flight.
	//
	// Hover is gated on a short DWELL. A transcript can hold dozens of collapsed
	// tool calls, and firing on bare pointerenter would let one sweep of the
	// cursor down the page kick off a request for every one of them. Resting on a
	// block is intent; passing over it is not. Pointer-down skips the dwell — by
	// then the intent is not in question.
	const PREFETCH_DWELL_MS = 140;
	let prefetchDwellTimer: ReturnType<typeof setTimeout> | null = null;

	const cancelPrefetchDwell = () => {
		if (prefetchDwellTimer === null) return;
		clearTimeout(prefetchDwellTimer);
		prefetchDwellTimer = null;
	};

	const prefetchBody = () => {
		cancelPrefetchDwell();
		const target = lazyBodyTarget;
		if (target) prefetchLazyBody(target.chatId, target.messageId, target.ref);
	};

	const prefetchBodyOnDwell = () => {
		if (!lazyBodyTarget || prefetchDwellTimer !== null) return;
		prefetchDwellTimer = setTimeout(prefetchBody, PREFETCH_DWELL_MS);
	};

	onMount(() => cancelPrefetchDwell);

	const collapsibleId = uuidv4();

	// Every tool call now expands into the same panel, so the chunk is worth
	// warming for all of them (not just the web/browser ones).
	let ToolCallResultComponent: any = $state(resolvedToolCallResult);

	function preloadToolCallResult() {
		if (ToolCallResultComponent) return;
		void loadToolCallResultModule()
			.then((module) => {
				ToolCallResultComponent = module.default;
			})
			.catch(() => {
				// Leave it null; the panel shows its skeleton until a later attempt.
			});
	}

	function toggleToolCallOpen() {
		if (disabled) return;

		if (open) {
			setOpenFromUser(false);
			return;
		}

		// Open on the click, never after an await. The panel's two-step reveal —
		// slide open around a skeleton, then grow into the body — is what covers
		// the chunk/body load, so blocking here only makes the tap feel dead.
		preloadToolCallResult();
		setOpenFromUser(true);
	}

	onMount(() => {
		if (attributes?.type !== 'tool_calls') return;

		const idleWindow = window as any;
		if (typeof idleWindow.requestIdleCallback === 'function') {
			const idleId = idleWindow.requestIdleCallback(preloadToolCallResult, { timeout: 2000 });
			return () => idleWindow.cancelIdleCallback?.(idleId);
		}

		const timeout = window.setTimeout(preloadToolCallResult, 1200);
		return () => window.clearTimeout(timeout);
	});

	function parseJSONString(str) {
		try {
			return parseJSONString(JSON.parse(str));
		} catch (e) {
			return str;
		}
	}

</script>

<!-- svelte-ignore a11y_no_static_element_interactions -->
<div
	{id}
	class={className}
	onpointerenter={prefetchBodyOnDwell}
	onpointerleave={cancelPrefetchDwell}
	onpointerdown={prefetchBody}
	onfocusin={prefetchBody}
>
	{#if attributes?.type === 'subagent_launch'}
		<!-- Subagent block: rendered as a self-contained card with its own
			collapsible chrome (own header/spinner/caret + recursive markdown
			body for the inner content_blocks). Bypasses the generic
			tool_calls / reasoning branches below — the SubagentBlock component
			reads its state live from `$subagentLiveStates` keyed on the
			tool_call_id attribute that `serialize_content_blocks` stamps.
			Lazy-imported (stable shared promise) to keep its sizable chunk off
			the first-render path; see MarkdownTokens.svelte. -->
		{#await loadSubagentBlock() then SubagentBlock}
			<SubagentBlock.default attributes={attributes ?? {}} />
		{/await}
	{:else if attributes?.type === 'ask_user'}
		<!-- Ask-user block: an interactive question card. The question lives in
			the tool-call `arguments`; the answer (once submitted) is the tool
			`result`/`done`. The card autosaves drafts and submits the final
			answer through the durable set_question_state patch op so the running
			generation resumes. Reads/writes draft state from `$questionStates`. -->
		<AskUserBlock attributes={attributes ?? {}} />
	{:else if attributes?.type === 'tool_calls'}
		{@const args = decode(attributes?.arguments)}
		{@const rawResult = attributes?.result ?? ''}
		{@const rawFiles = attributes?.files ?? ''}
		{@const embeds = parseJSONString(decode(attributes?.embeds ?? ''))}
		{@const summary = parseJSONString(decode(attributes?.summary ?? ''))}
		{@const toolDone = attributes?.done === 'true'}
		{@const toolErrored = toolDone && attributes?.error === 'true'}
		{@const toolErrorReason = decode(attributes?.error_reason ?? '')}
		{@const toolNotice = !toolErrored && toolDone ? decode(attributes?.notice ?? '') : ''}
		{@const toolSummary = getToolCallSummary(
			attributes?.name ?? '',
			args,
			summary ? { summary } : rawResult,
			toolDone
		)}
		<!-- Most failing tools report the reason as the result body ("Error: path
		     escapes workspace…") and leave error_reason empty, which used to
		     surface as a bare "Tool call failed". Fall back to the body's first
		     line so the row says what actually went wrong. -->
		{@const toolErrorText =
			toolErrorReason ||
			(toolErrored
				? decodeToolResultText(rawResult).trim().split('\n')[0].replace(/^Error:\s*/i, '')
				: '')}

		{#if embeds && Array.isArray(embeds) && embeds.length > 0}
			<div class="py-1 w-full cursor-pointer">
				<div class=" w-full text-xs text-gray-500">
					<div class="">
						{attributes.name}
					</div>
				</div>

				{#each embeds as embed, idx}
					<div class="my-2" id={`${collapsibleId}-tool-calls-${attributes?.id}-embed-${idx}`}>
						<FullHeightIframe
							src={embed}
							{args}
							allowScripts={true}
							allowForms={true}
							allowSameOrigin={true}
							allowPopups={true}
						/>
					</div>
				{/each}
			</div>
		{:else}
			<!-- Toggle on click, NOT pointerup: on iOS a tap that arrests a momentum
			     scroll (or ends a near-scroll drag) still fires pointerup but never
			     click, so pointerup-toggling collapsed things while scrolling. -->
			<div
				class="{buttonClassName} cursor-pointer"
				data-anchor-on-click
				onpointerenter={preloadToolCallResult}
				onpointerdown={preloadToolCallResult}
				onclick={toggleToolCallOpen}
			>
				<ToolCallRow
					summary={toolSummary}
					{open}
					done={toolDone}
					errored={toolErrored}
					errorReason={toolErrorText}
					notice={toolNotice}
				/>
			</div>

			{#if !grow}
				{#if open && !hide}
					<!-- Step one of the open: slide down around whatever the panel can
					     render right now (usually a skeleton). The panel's own
					     SmoothResize plays step two once the body lands. -->
					<div transition:slide={{ duration: 260, easing: quintOut, axis: 'y' }}>
						{#if ToolCallResultComponent}
							{@const toolFiles = parseJSONString(decode(rawFiles))}
							<ToolCallResultComponent
								id={`${collapsibleId}-tool-calls-${attributes?.id}-result`}
								name={attributes.name}
								argsRaw={args}
								resultRaw={rawResult}
								resultLazy={attributes?.result_lazy === 'true'}
								chatId={attributes?.chat_id ?? ''}
								messageId={attributes?.message_id ?? ''}
								toolCallId={attributes?.tool_call_id ?? attributes?.id ?? ''}
								done={toolDone}
								files={Array.isArray(toolFiles) ? toolFiles : []}
								error={toolErrored}
								errorReason={toolErrorText}
							/>
						{:else}
							<div
								class="my-2 overflow-hidden rounded-2xl border-hairline border-gray-200 bg-gray-50/60 p-2.5 text-sm dark:border-gray-800 dark:bg-gray-850/40"
							>
								<ResultSkeleton />
							</div>
						{/if}
					</div>
				{/if}
			{/if}
		{/if}

		<!-- Image results (view_image, image generators) stay visible below the
		     collapsed row — they ARE the result. Browser screenshots are excluded:
		     BrowserToolResult renders those inside the panel. -->
		{#if attributes?.done === 'true' && !isBrowserToolName(attributes?.name)}
			{@const files = parseJSONString(decode(rawFiles))}
			{#if typeof files === 'object'}
				{#each files ?? [] as file, idx}
					{#if typeof file === 'string'}
						{#if file.startsWith('data:image/')}
							<Image
								id={`${collapsibleId}-tool-calls-${attributes?.id}-result-${idx}`}
								src={file}
								alt="Image"
							/>
						{/if}
					{:else if typeof file === 'object'}
						{#if file.type === 'image' && file.url}
							<Image
								id={`${collapsibleId}-tool-calls-${attributes?.id}-result-${idx}`}
								src={file.url}
								alt="Image"
							/>
						{/if}
					{/if}
				{/each}
			{/if}
		{/if}
	{:else}
		{#if isReasoning}
			<!-- Thinking is a transcript meta line, exactly like a tool call — same
			     row so a column of them shares one left edge and one weight. -->
			{@const thinkingDone = attributes?.done === 'true' && attributes?.duration}
			<!-- svelte-ignore a11y_no_static_element_interactions -->
			<!-- svelte-ignore a11y_click_events_have_key_events -->
			<div
				class="w-fit max-w-full cursor-pointer"
				data-anchor-on-click
				onclick={() => {
					if (!disabled) {
						setOpenFromUser(!open);
					}
				}}
			>
				<TranscriptRow
					icon="reasoning"
					label={thinkingDone
						? attributes.duration < 1
							? $i18n.t('Thought for less than a second')
							: attributes.duration < 60
								? $i18n.t('Thought for {{DURATION}} seconds', {
										DURATION: attributes.duration
									})
								: $i18n.t('Thought for {{DURATION}}', {
										DURATION: dayjs.duration(attributes.duration, 'seconds').humanize()
									})
						: $i18n.t('Thinking...')}
					{open}
					pending={attributes?.done === 'false'}
				/>
			</div>
		{:else if title !== null}
			<!-- svelte-ignore a11y_no_static_element_interactions -->
			<!-- svelte-ignore a11y_click_events_have_key_events -->
			<div
				class="{buttonClassName} cursor-pointer"
				data-anchor-on-click
				onclick={() => {
					if (!disabled) {
						setOpenFromUser(!open);
					}
				}}
			>
				<div
					class=" w-full font-medium flex items-center justify-between gap-2 {attributes?.done &&
					attributes?.done !== 'true'
						? 'shimmer'
						: ''}
			"
				>
					{#if attributes?.done && attributes?.done !== 'true'}
						<div>
							<Spinner className="size-4" />
						</div>
					{/if}

					<div class="">{title}</div>

					<div class="flex self-center translate-y-[1px]">
						<ChevronDown
							strokeWidth="3.5"
							className="size-3.5 transition-transform duration-200 ease-paper {open
								? 'rotate-180'
								: ''}"
						/>
					</div>
				</div>
			</div>
		{:else}
			<!-- svelte-ignore a11y_no_static_element_interactions -->
			<!-- svelte-ignore a11y_click_events_have_key_events -->
			<div
				class="{buttonClassName} cursor-pointer"
				data-anchor-on-click
				onclick={(e) => {
					e.stopPropagation();
					if (!disabled) {
						setOpenFromUser(!open);
					}
				}}
			>
				<div>
					<div class="flex items-start justify-between">
						{@render children?.()}

						{#if chevron}
							<div class="flex self-start translate-y-1">
								<ChevronDown
									strokeWidth="3.5"
									className="size-3.5 transition-transform duration-200 ease-paper {open
										? 'rotate-180'
										: ''}"
								/>
							</div>
						{/if}
					</div>

					{#if grow}
						{#if open && !hide}
							<div
								transition:slide={{ duration: 300, easing: quintOut, axis: 'y' }}
								onclick={(e) => {
									e.stopPropagation();
								}}
							>
								{@render content?.()}
							</div>
						{/if}
					{/if}
				</div>
			</div>
		{/if}

		{#if !grow}
			{#if open && !hide}
				<div transition:slide={{ duration: 300, easing: quintOut, axis: 'y' }}>
					{@render content?.()}
				</div>
			{/if}
		{/if}
	{/if}
</div>
