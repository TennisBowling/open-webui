<script>
	import { onDestroy, tick, getContext, untrack } from 'svelte';
	const i18n = getContext('i18n');

	import Markdown from './Markdown.svelte';
	import {
		artifactCode,
		messageEditingIds,
		mobile,
		settings,
		showArtifacts,
		showControls,
		showEmbeds,
		showFilePreview,
		showOverview
	} from '$lib/stores';
	import ToolCallsBlock from './ToolCallsBlock.svelte';
	import ToolSelectionChange from './ToolSelectionChange.svelte';
	import CompactionBlock from './CompactionBlock.svelte';
	import WorkingBlock from './WorkingBlock.svelte';
	import RewindBoundary from './RewindBoundary.svelte';
	import Spinner from '$lib/components/common/Spinner.svelte';
	import Skeleton from './Skeleton.svelte';
	import Tooltip from '$lib/components/common/Tooltip.svelte';
	import { blocksToDisplayMarkdown } from '$lib/utils';
	import { computeAgenticRenderItems } from '$lib/utils/agenticGroups';
	import {
		autoGrowEditTextarea,
		captureEditEntryAnchor,
		placeEditBoxForKeyboard
	} from '$lib/utils/editScroll';
	import { getRewindCutIndices } from '$lib/utils/retryLastRequest';
	import { streamPerfEnd, streamPerfStart } from '$lib/utils/streamPerf';

	// Optional structured content_blocks. When present we render block-by-block
	// with cached per-block projections — older blocks (completed reasoning,
	// completed tool_calls, prior text blocks) reuse the same `<Markdown>`
	// instance with an unchanged content string, so Svelte's prop-equality
	// check short-circuits the marked.parse + {@html} wipe for them. Only the
	// last block (the one actively streaming) re-parses per chunk. Net cost
	// per stream chunk: O(last_block_size) instead of O(total_message_size).
	//
	// When unset (or empty), we fall back to a single `<Markdown content>`
	// render — the legacy path, used for messages that pre-date the

	// When true, done messages parse their markdown synchronously instead of on a
	// short debounce. Used on initial chat load so historical content reaches its

	// Clean terminal signals for the "Working for X" agentic-step bundling.
	// `done` (above) is polluted by the chatFadeStreamingText setting upstream,
	// so it can't be used to tell whether the message is still generating.
	// `messageDone` is the unfiltered message.done; `messageStopped` /

	// Block-level rewind: (cutIndex, text) => Promise<void> | void, or null when
	// rewind is unavailable (read-only, or no structured content_blocks). Keep

	/**
	 * @typedef {Object} Props
	 * @property {any} id
	 * @property {any} content
	 * @property {any} [content_blocks] - content_blocks migration and for non-message renderings.
	 * @property {any} messageId
	 * @property {string} [chatId]
	 * @property {any} [dataVizOverrides]
	 * @property {boolean} [done]
	 * @property {any} [model]
	 * @property {any} [sources]
	 * @property {any} [sandboxFiles]
	 * @property {boolean} [save]
	 * @property {boolean} [preview]
	 * @property {boolean} [editCodeBlock]
	 * @property {boolean} [topPadding]
	 * @property {boolean} [parseImmediately] - final height before the settle loop reveals the messages.
	 * @property {boolean} [messageDone] - `messageErrored` choose the frozen header wording on a terminal burst.
	 * @property {boolean} [messageStopped]
	 * @property {boolean} [messageErrored]
	 * @property {any} [onSave]
	 * @property {any} [onSourceClick]
	 * @property {any} [onTaskClick]
	 * @property {any} [onRewind] - blocks before `cutIndex`, inject `text` there, resume inline as a sibling.
	 */

	/** @type {Props} */
	let {
		id,
		content,
		content_blocks = null,
		messageId,
		chatId = '',
		dataVizOverrides = {},
		done = true,
		model = null,
		sources = null,
		sandboxFiles = [],
		save = false,
		preview = false,
		editCodeBlock = true,
		topPadding = false,
		parseImmediately = false,
		messageDone = false,
		messageStopped = false,
		messageErrored = false,
		onSave = (e) => {},
		onSourceClick = (e) => {},
		onTaskClick = (e) => {},
		onRewind = null
	} = $props();

	// Which block boundary's inline composer is open (block index = first
	// discarded block), or null. Only one open per message at a time. Shared with
	// WorkingBlock so a composer can sit between bundled steps too.
	let activeRewindCut = $state(null);
	const activateRewind = (cutIndex) => {
		activeRewindCut = cutIndex;
		editingSteerIndex = null;
	};
	const cancelRewind = () => {
		activeRewindCut = null;
	};
	// Close the composer only once the rewind has actually committed. Closing it
	// up-front made every failure — and every slow commit — look identical to
	// "the button did nothing": the box vanished, the transcript was unchanged,
	// and the error (if any) was an unhandled rejection nobody saw. RewindBoundary
	// shows a working state for the duration and keeps the draft on failure.
	const submitRewind = async (cutIndex, text) => {
		const cb = onRewind;
		if (typeof cb !== 'function') return false;
		const committed = await cb(cutIndex, text);
		if (committed !== false) activeRewindCut = null;
		return committed;
	};

	// Edit-and-resend for an injected `user_steer` block. Editing a steer IS a
	// rewind at the steer's own block index: keep every block before it, drop the
	// old steer and everything that followed, inject the new text there, and
	// resume as a sibling branch — the exact machinery the boundary composers
	// use, so the original turn survives as a navigable branch. Only one editor
	// open per message at a time (shared exclusivity with activeRewindCut).
	//
	// This is the same "edit box" experience as the top-level message editors in
	// UserMessage/ResponseMessage — same entry anchor (no viewport yank on the
	// markdown->textarea swap) and same on-screen-keyboard top-alignment — routed
	// through the shared editScroll.ts helpers instead of a one-off reimplementation.
	let editingSteerIndex = $state(null);
	let editingSteerText = $state('');
	const steerEditPlacementId = (blockIndex) => `${messageId}-steer-${blockIndex}`;

	// The message can still be actively streaming later blocks while an earlier
	// steer is being edited, so autoScroll must be disengaged the same way a
	// top-level message edit disengages it (see messageEditingIds in Chat.svelte)
	// — otherwise incoming tokens fight the placement the moment it's set. Keyed
	// distinctly from the top-level edit registration (`${messageId}-steer`) so
	// this doesn't clobber/get clobbered by UserMessage/ResponseMessage's own
	// registration for the same messageId.
	let registeredSteerEditId = null;
	const syncSteerEditRegistration = (editing) => {
		const target = editing ? `${messageId}-steer` : null;
		if (target === registeredSteerEditId) return;
		messageEditingIds.update((ids) => {
			const next = new Set(ids);
			if (registeredSteerEditId !== null) next.delete(registeredSteerEditId);
			if (target !== null) next.add(target);
			return next;
		});
		registeredSteerEditId = target;
	};
	$effect(() => {
		syncSteerEditRegistration(editingSteerIndex !== null);
	});
	onDestroy(() => syncSteerEditRegistration(false));

	const startSteerEdit = async (blockIndex) => {
		editingSteerIndex = blockIndex;
		editingSteerText = structuredBlocks?.[blockIndex]?.content ?? '';
		activeRewindCut = null;

		// Anchor the message's on-screen position before the inline editor's
		// height mutates the layout — captured BEFORE the await so it reflects
		// the pre-edit DOM, mirroring UserMessage/ResponseMessage's editMessageHandler.
		const restoreAnchor = captureEditEntryAnchor(messageId);

		await tick();
		// steerEditorInit (below) focuses the textarea as it mounts.
		await tick();
		restoreAnchor();

		// Top-align the editor once any on-screen keyboard shows up (already open
		// from the composer, or arriving after focus); expires quietly on desktop.
		placeEditBoxForKeyboard(steerEditPlacementId(blockIndex));
	};
	let submittingSteerEdit = $state(false);
	const cancelSteerEdit = () => {
		editingSteerIndex = null;
		editingSteerText = '';
		submittingSteerEdit = false;
	};
	// Editing a steer IS a rewind, so it gets the same commit-then-close contract
	// as the boundary composer (see submitRewind).
	const submitSteerEdit = async () => {
		const text = (editingSteerText ?? '').trim();
		// Unlike a boundary composer, empty is NOT a pure rewind here — it would
		// silently delete the steer. Require text; the boundary pill covers the
		// rewind-without-message case.
		if (!text || editingSteerIndex === null || submittingSteerEdit) return;
		const idx = editingSteerIndex;
		const cb = onRewind;
		if (typeof cb !== 'function') return;
		submittingSteerEdit = true;
		try {
			const committed = await cb(idx, text);
			if (committed !== false) cancelSteerEdit();
		} finally {
			submittingSteerEdit = false;
		}
	};
	const steerEditorInit = (node) => {
		node.focus({ preventScroll: true });
		const len = node.value?.length ?? 0;
		try {
			node.setSelectionRange(len, len);
		} catch {}
		autoGrowEditTextarea(node);
	};
	const onSteerEditKeydown = (e) => {
		if (e.key === 'Enter' && !e.shiftKey) {
			e.preventDefault();
			submitSteerEdit();
		} else if (e.key === 'Escape') {
			e.preventDefault();
			cancelSteerEdit();
		}
	};
	// First underlying block index of a render item (group → its first member).
	const itemFirstIndex = (item) => (item?.kind === 'group' ? (item.indices?.[0] ?? 0) : item.index);

	// The "after the last completed tool round" cut for a bundled group, surfaced as
	// a top-level boundary AFTER the card so it's visible WITHOUT expanding (the most
	// natural redirect point: "after all the work in this card, before what follows").
	// Returns -1 when that cut sits at the group's edge (it's then already rendered as
	// the before-next-item boundary). WorkingBlock skips this same cut (skipCut) to
	// avoid a duplicate when the card is expanded.
	const groupTailCut = (indices) => {
		if (!indices || indices.length === 0) return -1;
		let cut = -1;
		for (const i of indices) if (rewindCuts.has(i + 1)) cut = i + 1;
		const last = indices[indices.length - 1];
		return cut > 0 && cut <= last ? cut : -1; // cut<=last ⟹ trailing non-tool blocks ⟹ interior
	};

	let contentContainerElement = $state();

	// Cached per-block markdown projections. The important nuance for v2.1 is that
	// not only the tail block can change: a tool_calls block may receive results
	// after later text/reasoning blocks have opened. Track a cheap signature per
	// block and only re-project blocks whose render-relevant data changed.
	/** @type {string[]} */
	let blockProjections = $state([]);
	/** @type {string[]} */
	let blockProjectionSignatures = $state([]);
	/** @type {WeakMap<object, { tag: string; signature: string }>} */
	let toolBlockSignatureCache = new WeakMap();

	const textSig = (value) => {
		const text = value == null ? '' : String(value);
		return `${text.length}:${text.slice(0, 32)}:${text.slice(-32)}`;
	};

	const jsonSig = (value) => {
		try {
			return textSig(JSON.stringify(value ?? null));
		} catch {
			return textSig(value);
		}
	};

	const listSig = (value) => {
		if (!Array.isArray(value)) return '0';
		return `${value.length}:${value
			.map((item) => {
				if (!item || typeof item !== 'object') return textSig(item);
				return [
					item.id ?? '',
					item.url ?? '',
					item.name ?? '',
					item.type ?? '',
					textSig(item.content)
				].join('|');
			})
			.join(';')}`;
	};

	// Cheap fingerprint that DOES change on the in-place mutations the streaming
	// path performs: tool_call_add pushes onto block.content, tool_call_args_append
	// grows the last call's arguments string, and tool results grow block.results —
	// all WITHOUT changing the array object identity. Keying the cache on array
	// identity alone (the previous approach) returned a stale signature while
	// arguments streamed in, stalling the live tool-call display. This tag is O(1):
	// it samples only the counts and the last element's growing fields.
	const toolBlockMutationTag = (block) => {
		const calls = Array.isArray(block?.content) ? block.content : [];
		const results = Array.isArray(block?.results) ? block.results : [];
		const lastCall = calls.length ? calls[calls.length - 1] : null;
		const lastResult = results.length ? results[results.length - 1] : null;
		const lastArgs = lastCall?.function?.arguments;
		const lastArgsLen = typeof lastArgs === 'string' ? lastArgs.length : 0;
		const lastResultContent = lastResult?.content;
		const lastResultLen = typeof lastResultContent === 'string' ? lastResultContent.length : 0;
		// Include the last result's error flag: a result can flip non-error →
		// error (or gain a notice) without changing its content length, and the
		// collapsed row must re-project to show it.
		const lastResultError = lastResult?.error ? '1' : '0';
		const lastResultNotice = lastResult?.notice ? '1' : '0';
		return `${calls.length}|${lastArgsLen}|${results.length}|${lastResultLen}|${lastResultError}|${lastResultNotice}`;
	};

	const toolBlockSignature = (block) => {
		const tag = toolBlockMutationTag(block);
		const cached = toolBlockSignatureCache.get(block);
		if (cached && cached.tag === tag) {
			return cached.signature;
		}

		const calls = Array.isArray(block?.content) ? block.content : [];
		const results = Array.isArray(block?.results) ? block.results : [];

		const signature = [
			'tool_calls',
			calls
				.map((call) => {
					const fn = call?.function ?? {};
					return [
						call?.id ?? '',
						call?.tool_call_id ?? '',
						fn.name ?? '',
						textSig(fn.arguments)
					].join(',');
				})
				.join(';'),
			results
				.map((result) =>
					[
						result?.tool_call_id ?? '',
						textSig(result?.content),
						result?.result_ref ?? '',
						result?.result_lazy ? '1' : '0',
						result?.size ?? '',
						result?.sha256 ?? '',
						jsonSig(result?.summary),
						listSig(result?.files),
						listSig(result?.embeds),
						result?.subagent_id ?? '',
						result?.error ? '1' : '0',
						textSig(result?.error_reason),
						textSig(result?.notice)
					].join(',')
				)
				.join(';')
		].join(':');

		if (block && typeof block === 'object') {
			toolBlockSignatureCache.set(block, {
				tag,
				signature
			});
		}

		return signature;
	};

	const blockProjectionSignature = (block) => {
		if (!block || typeof block !== 'object') return 'null';
		const type = block.type ?? '';
		if (typeof block.__owui_rev === 'number') {
			return `${type}:rev:${block.__owui_rev}`;
		}

		if (type === 'text') {
			return `text:${textSig(block.content)}`;
		}

		if (type === 'reasoning') {
			return [
				'reasoning',
				textSig(block.content),
				block.started_at ?? '',
				block.ended_at ?? '',
				block.duration ?? '',
				// Lazy stubs (server withheld the text): a live→stub flip must
				// re-project even though `content` stays empty-ish.
				block.content_lazy ? '1' : '0',
				block.content_ref ?? ''
			].join(':');
		}

		if (type === 'tool_calls') {
			// Tool calls render structurally via <ToolCallsBlock>. Keep a stable
			// string payload for unchanged blocks so completed calls do not receive
			// fresh object props on every token streamed after them.
			return toolBlockSignature(block);
		}

		return `${type}:${textSig(JSON.stringify(block))}`;
	};

	$effect(() => {
		const perf = streamPerfStart();
		/** @type {any[]} */
		const blocks = Array.isArray(content_blocks) ? content_blocks : [];
		// The previous projections are this effect's OWN output — an accumulator,
		// not an input. They must be read untracked: every run assigns fresh
		// arrays, so a tracked read would make the effect depend on its own write
		// and re-run forever (Svelte 4's `$:` excluded self-assigned variables
		// from its dependency list; `$effect` does not). Snapshot to plain arrays
		// so no later element read touches the $state proxy while tracking.
		const prevProjections = untrack(() => blockProjections.slice());
		const prevSignatures = untrack(() => blockProjectionSignatures.slice());
		if (blocks.length === 0) {
			if (prevProjections.length !== 0) blockProjections = [];
			if (prevSignatures.length !== 0) blockProjectionSignatures = [];
		} else {
			/** @type {string[]} */
			const next = [];
			/** @type {string[]} */
			const nextSignatures = [];
			for (let i = 0; i < blocks.length; i++) {
				const signature = blockProjectionSignature(blocks[i]);
				nextSignatures[i] = signature;
				if (blocks[i]?.type === 'tool_calls') {
					next[i] = '';
				} else if (signature !== prevSignatures[i] || prevProjections[i] == null) {
					next[i] = blocksToDisplayMarkdown([blocks[i]], { chatId, messageId });
				} else {
					next[i] = prevProjections[i];
				}
			}
			blockProjectionSignatures = nextSignatures;
			blockProjections = next;
		}
		streamPerfEnd('render.content_projection', perf, blocks.length || 1);
	});

	// Single render path: per-block projections when `content_blocks` is
	// provided, otherwise a one-element array carrying the legacy `content`
	// string. Keeps the `<Markdown>` invocation in exactly one place.
	//
	// `structuredMode` stays true for the duration of a structured render so
	// each block's id is `${id}-b${i}` from the start — preventing a re-mount
	// of block-0 when a second block gets appended (which would otherwise
	// flip the id from `${id}` to `${id}-b0`, triggering Markdown's `{#key
	// id}` to tear down and rebuild the rendered DOM).
	let structuredBlocks = $derived(Array.isArray(content_blocks) ? content_blocks : []);
	let structuredMode = $derived(structuredBlocks.length > 0);
	// Valid rewind cut points = indices immediately AFTER a completed tool_calls
	// block (a "between requests" boundary). Recomputed as blocks stream in: a
	// boundary only appears once that round's whole (possibly parallel) tool batch
	// has all its results, so it can never split a parallel batch or land mid-round.
	let rewindCuts = $derived(onRewind ? getRewindCutIndices(structuredBlocks) : new Set());

	// "Working for X" agentic-step bundling. When enabled, contiguous bursts of
	// reasoning+tool_calls collapse into one WorkingBlock; otherwise the legacy
	// flat per-block layout. Pure O(N) pass over the block array — the projection
	// cache above is untouched, so streaming stays O(changed-blocks).
	let bundleAgentic = $derived($settings?.bundleAgenticSteps ?? true);
	let agenticAutoExpand = $derived($settings?.agenticStepsAutoExpand ?? true);
	let renderItems = $derived(computeAgenticRenderItems(structuredBlocks, bundleAgentic));
	const renderItemKey = (/** @type {any} */ item) =>
		item.kind === 'group' ? `g${item.indices[0]}` : `b${item.index}`;

	// Tail liveness cursor. The per-burst working spinner, a streaming text block,
	// and the "Thinking…"/"Executing…" reasoning+tool indicators are the normal
	// signs the model is alive. But two tail states render *nothing*: the empty
	// placeholder `text("")` the backend parks as the next stream target after a
	// tool round, and a `user_steer` block injected mid-task (which also pushes
	// the working burst out of the last slot, so its spinner switches off). In
	// those states the message looks frozen even though generation continues, so
	// park the same typewriter cursor at the tail — where the model writes next —
	// but only while actually generating.
	let tailItem = $derived(renderItems.length ? renderItems[renderItems.length - 1] : null);
	let tailBlock = $derived(
		tailItem && tailItem.kind === 'block' ? structuredBlocks[tailItem.index] : null
	);
	let tailIsSilent = $derived(
		tailBlock != null &&
			((tailBlock.type === 'text' && `${tailBlock.content ?? ''}`.trim().length === 0) ||
				tailBlock.type === 'user_steer')
	);
	let showTailCursor = $derived(
		structuredMode && !messageDone && !messageStopped && !messageErrored && tailIsSilent
	);
	/** @type {string[]} */
	let sourceIds = $state([]);

	/** @param {any[]} sourceList */
	const getSourceIds = (sourceList = []) =>
		(sourceList ?? []).reduce((acc, source) => {
			const currentModel = /** @type {any} */ (model);
			/** @type {string[]} */
			let ids = [];
			source.document.forEach((document, index) => {
				if (currentModel?.info?.meta?.capabilities?.citations == false) {
					ids.push('N/A');
					return ids;
				}

				const metadata = source.metadata?.[index];
				const id = metadata?.source ?? 'N/A';

				if (metadata?.name) {
					ids.push(metadata.name);
					return ids;
				}

				if (id.startsWith('http://') || id.startsWith('https://')) {
					ids.push(id);
				} else {
					ids.push(source?.source?.name ?? id);
				}

				return ids;
			});

			acc = [...acc, ...ids];
			return acc.filter((item, index) => acc.indexOf(item) === index);
		}, []);

	$effect(() => {
		model;
		sourceIds = getSourceIds(sources ?? []);
	});

	// Shared by the inline-block render path and the bundled WorkingBlock so
	// artifact auto-detection / preview behaves identically inside or outside a
	// "Working for X" group.
	const handleMarkdownArtifact = (/** @type {any} */ token) => {
		const { lang, text: code } = token;
		if (
			($settings?.detectArtifacts ?? true) &&
			(['html', 'svg'].includes(lang) || (lang === 'xml' && code.includes('svg'))) &&
			!$mobile &&
			chatId
		) {
			showArtifacts.set(true);
			showFilePreview.set(false);
			showControls.set(true);
		}
	};

	const handleMarkdownPreview = async (/** @type {any} */ value) => {
		await artifactCode.set(value);
		await showControls.set(true);
		await showArtifacts.set(true);
		await showOverview.set(false);
		await showEmbeds.set(false);
		await showFilePreview.set(false);
	};
</script>

<div bind:this={contentContainerElement}>
	{#if structuredMode}
		{#each renderItems as item, ri (renderItemKey(item))}
			{@const ci = itemFirstIndex(item)}
			{#if onRewind && ri > 0 && rewindCuts.has(ci)}
				<RewindBoundary
					{messageId}
					cutIndex={ci}
					active={activeRewindCut === ci}
					onActivate={activateRewind}
					onCancel={cancelRewind}
					onSubmit={submitRewind}
					bare={renderItems[ri - 1]?.kind === 'group'}
				/>
			{/if}
			{#if item.kind === 'group'}
				{@const lastItem = renderItems[renderItems.length - 1]}
				{@const tailCut = onRewind ? groupTailCut(item.indices) : -1}
				<WorkingBlock
					idPrefix={id}
					{messageId}
					{chatId}
					{model}
					{save}
					{preview}
					{done}
					{editCodeBlock}
					{dataVizOverrides}
					{sandboxFiles}
					{sourceIds}
					{onSourceClick}
					{onTaskClick}
					{onSave}
					onArtifactDetected={handleMarkdownArtifact}
					onPreview={handleMarkdownPreview}
					working={!messageDone && item === lastItem}
					autoExpand={agenticAutoExpand}
					messageStopped={messageStopped && item === lastItem}
					errored={messageErrored && item === lastItem}
					rewindEnabled={!!onRewind}
					{rewindCuts}
					skipCut={tailCut}
					{activeRewindCut}
					onRewindActivate={activateRewind}
					onRewindCancel={cancelRewind}
					onRewindSubmit={submitRewind}
					members={item.indices.map((bi) => ({
						index: bi,
						block: structuredBlocks[bi],
						projection: blockProjections[bi] ?? '',
						blockRev: blockProjectionSignatures[bi] ?? ''
					}))}
				/>
				{#if tailCut > 0}
					<!-- "After all the work in this card" cut, surfaced at the top level so
					     it's visible without expanding the card (e.g. after the last
					     subagent round, before the questions that follow). bare: the
					     WorkingBlock's own divider line is right above, so no second line. -->
					<RewindBoundary
						{messageId}
						cutIndex={tailCut}
						active={activeRewindCut === tailCut}
						onActivate={activateRewind}
						onCancel={cancelRewind}
						onSubmit={submitRewind}
						bare
					/>
				{/if}
			{:else if structuredBlocks[item.index]?.type === 'tool_calls'}
				<ToolCallsBlock
					id={`${id}-b${item.index}`}
					block={structuredBlocks[item.index]}
					blockRev={blockProjectionSignatures[item.index] ?? ''}
					{chatId}
					{messageId}
					{dataVizOverrides}
					messageTerminated={messageDone || messageStopped || messageErrored}
				/>
			{:else if structuredBlocks[item.index]?.type === 'tool_selection_change'}
				<ToolSelectionChange block={structuredBlocks[item.index]} />
			{:else if structuredBlocks[item.index]?.type === 'compaction'}
				<!-- Conversation compaction anchor. Everything above this point was
				     replaced by a summary in the OUTBOUND payload only; nothing was
				     deleted, so the blocks above still render in full. -->
				<CompactionBlock
					block={structuredBlocks[item.index]}
					{chatId}
					{messageId}
					blockIndex={item.index}
				/>
			{:else if structuredBlocks[item.index]?.type === 'user_steer'}
				<!-- Mid-task user interjection (steering): the user sent this while the
				     model was working and the backend injected it at a tool-call
				     boundary. Render as an inline user bubble so the transcript shows
				     exactly what the model saw, in order. -->
				{#if onRewind && editingSteerIndex === item.index}
					<!-- Inline steer editor: submit rewinds at this block with the new
					     text (edit-and-resend, sibling branch — original preserved). -->
					<div class="flex justify-end my-2.5" dir="ltr">
						<div
							class="message-edit-box w-full max-w-[80%] rounded-2xl border-hairline border-gray-100 dark:border-gray-800 bg-gray-50 dark:bg-gray-850 p-2.5 shadow-sm"
						>
							<div
								class="flex items-center justify-between mb-1.5 text-[11px] text-gray-500 dark:text-gray-400 px-0.5"
							>
								<div class="flex items-center gap-1">
									<svg
										xmlns="http://www.w3.org/2000/svg"
										viewBox="0 0 16 16"
										fill="currentColor"
										class="size-3"
									>
										<path
											d="M8 1.5 14.5 8 8 14.5 6.94 13.44l4.3-4.3H1.5v-1.5h9.74l-4.3-4.3L8 1.5Z"
										/>
									</svg>
									<span>{$i18n.t('Edit steered message')}</span>
								</div>
								<button
									type="button"
									class="p-0.5 max-md:p-2 rounded hover:bg-gray-100 dark:hover:bg-gray-800"
									aria-label={$i18n.t('Cancel')}
									onclick={cancelSteerEdit}
								>
									<svg
										xmlns="http://www.w3.org/2000/svg"
										viewBox="0 0 20 20"
										fill="currentColor"
										class="size-3.5"
									>
										<path
											d="M6.28 5.22a.75.75 0 0 0-1.06 1.06L8.94 10l-3.72 3.72a.75.75 0 1 0 1.06 1.06L10 11.06l3.72 3.72a.75.75 0 1 0 1.06-1.06L11.06 10l3.72-3.72a.75.75 0 0 0-1.06-1.06L10 8.94 6.28 5.22Z"
										/>
									</svg>
								</button>
							</div>

							<textarea
								id="message-edit-{steerEditPlacementId(item.index)}"
								use:steerEditorInit
								bind:value={editingSteerText}
								onkeydown={onSteerEditKeydown}
								oninput={(e) => autoGrowEditTextarea(e.currentTarget)}
								disabled={submittingSteerEdit}
								rows="2"
								class="w-full resize-none bg-transparent text-sm text-gray-800 dark:text-gray-100 placeholder-gray-400 dark:placeholder-gray-500 outline-none px-1.5 py-1"
							></textarea>

							<div class="flex items-center justify-between mt-1 px-0.5">
								<span class="text-[10px] text-gray-400 dark:text-gray-500">
									{$i18n.t('Enter to resend from here · Esc to cancel')}
								</span>
								<button
									type="button"
									class="inline-flex items-center gap-1.5 text-xs font-medium px-3 py-1 rounded-full bg-book-cloth hover:bg-kraft text-white transition-colors duration-200 ease-paper disabled:opacity-60"
									disabled={!editingSteerText.trim() || submittingSteerEdit}
									onclick={submitSteerEdit}
								>
									{#if submittingSteerEdit}
										<Spinner className="size-3" />
										{$i18n.t('Rewinding…')}
									{:else}
										{$i18n.t('Send')}
									{/if}
								</button>
							</div>
						</div>
					</div>
				{:else}
					<div class="group/steer flex justify-end items-center gap-0.5 my-2.5" dir="ltr">
						{#if onRewind}
							<Tooltip content={$i18n.t('Edit steered message')} placement="bottom">
								<button
									type="button"
									class="{($settings?.highContrastMode ?? false)
										? ''
										: 'invisible group-hover/steer:visible'} p-1.5 max-md:p-2.5 self-end text-gray-400 dark:text-gray-500 hover:bg-black/5 dark:hover:bg-white/5 rounded-lg dark:hover:text-white hover:text-black transition"
									aria-label={$i18n.t('Edit steered message')}
									onclick={() => startSteerEdit(item.index)}
								>
									<svg
										xmlns="http://www.w3.org/2000/svg"
										fill="none"
										viewBox="0 0 24 24"
										stroke-width="2.3"
										stroke="currentColor"
										class="w-4 h-4"
									>
										<path
											stroke-linecap="round"
											stroke-linejoin="round"
											d="M16.862 4.487l1.687-1.688a1.875 1.875 0 112.652 2.652L6.832 19.82a4.5 4.5 0 01-1.897 1.13l-2.685.8.8-2.685a4.5 4.5 0 011.13-1.897L16.863 4.487zm0 0L19.5 7.125"
										/>
									</svg>
								</button>
							</Tooltip>
						{/if}
						<div class="flex flex-col items-end max-w-[80%]" aria-label="Steered message">
							<div
								class="flex items-center gap-1 mb-1 text-[11px] text-gray-400 dark:text-gray-500 pr-0.5"
							>
								<svg
									xmlns="http://www.w3.org/2000/svg"
									viewBox="0 0 16 16"
									fill="currentColor"
									class="size-3"
								>
									<path d="M8 1.5 14.5 8 8 14.5 6.94 13.44l4.3-4.3H1.5v-1.5h9.74l-4.3-4.3L8 1.5Z" />
								</svg>
								<span>{$i18n.t('Steered')}</span>
							</div>
							<div
								class="rounded-2xl px-3.5 py-2 bg-gray-50 dark:bg-gray-850 text-gray-800 dark:text-gray-100 text-sm whitespace-pre-wrap break-words"
							>
								{structuredBlocks[item.index]?.content ?? ''}
							</div>
						</div>
					</div>
				{/if}
			{:else}
				<Markdown
					id={`${id}-b${item.index}`}
					content={blockProjections[item.index] ?? ''}
					{model}
					{save}
					{preview}
					{done}
					{parseImmediately}
					{editCodeBlock}
					topPadding={item.index === 0 ? topPadding : false}
					{chatId}
					{messageId}
					{dataVizOverrides}
					{sandboxFiles}
					{sourceIds}
					{onSourceClick}
					{onTaskClick}
					{onSave}
					onUpdate={handleMarkdownArtifact}
					onPreview={handleMarkdownPreview}
				/>
			{/if}
		{/each}
		{#if onRewind && rewindCuts.has(structuredBlocks.length)}
			<!-- Trailing "between requests" boundary for a turn that ENDS on a completed
			     tool round (no final text block — the stream cleanup strips the trailing
			     empty placeholder). cut == blocks.length keeps the whole turn and resumes
			     inline. No other affordance renders this: the per-item loop only emits a
			     boundary BEFORE an item (cut < length), and WorkingBlock skips its last
			     member. Can't collide — its index exceeds every item's first index. -->
			<RewindBoundary
				{messageId}
				cutIndex={structuredBlocks.length}
				active={activeRewindCut === structuredBlocks.length}
				onActivate={activateRewind}
				onCancel={cancelRewind}
				onSubmit={submitRewind}
				bare={renderItems[renderItems.length - 1]?.kind === 'group'}
			/>
		{/if}
		{#if showTailCursor}
			<!-- Awaiting the model's next output on a turn that ALREADY has content:
			     the empty placeholder text block parked after a tool round, or an
			     injected/rewound user_steer. Both render nothing of their own, so
			     without this the turn looks frozen — indistinguishable from idle,
			     which is what made a rewind or a steer read as "nothing happened".
			     This is the SAME indicator a brand-new empty response shows
			     (ResponseMessage/MultiResponseMessages), deliberately: "sent, waiting
			     for the model" must look identical whether it's a first send, a
			     regenerate, a steer, or a rewind. -->
			<div class="flex items-center" aria-live="polite">
				<Skeleton />
				<span class="sr-only">{$i18n.t('Generating…')}</span>
			</div>
		{/if}
	{:else}
		<Markdown
			{id}
			content={content ?? ''}
			{model}
			{save}
			{preview}
			{done}
			{parseImmediately}
			{editCodeBlock}
			{topPadding}
			{chatId}
			{messageId}
			{dataVizOverrides}
			{sandboxFiles}
			{sourceIds}
			{onSourceClick}
			{onTaskClick}
			{onSave}
			onUpdate={(token) => {
				const { lang, text: code } = token;

				if (
					($settings?.detectArtifacts ?? true) &&
					(['html', 'svg'].includes(lang) || (lang === 'xml' && code.includes('svg'))) &&
					!$mobile &&
					chatId
				) {
					showArtifacts.set(true);
					showFilePreview.set(false);
					showControls.set(true);
				}
			}}
			onPreview={async (value) => {
				console.log('Preview', value);
				await artifactCode.set(value);
				await showControls.set(true);
				await showArtifacts.set(true);
				await showOverview.set(false);
				await showEmbeds.set(false);
				await showFilePreview.set(false);
			}}
		/>
	{/if}
</div>
