<script>
	import { onDestroy, onMount, tick, getContext } from 'svelte';
	const i18n = getContext('i18n');

	import Markdown from './Markdown.svelte';
	import {
		artifactCode,
		mobile,
		settings,
		showArtifacts,
		showControls,
		showEmbeds,
		showFilePreview,
		showOverview
	} from '$lib/stores';
	import FloatingButtons from '../ContentRenderer/FloatingButtons.svelte';
	import ToolCallsBlock from './ToolCallsBlock.svelte';
	import WorkingBlock from './WorkingBlock.svelte';
	import Skeleton from './Skeleton.svelte';
	import { blocksToDisplayMarkdown, createMessagesList } from '$lib/utils';
	import { computeAgenticRenderItems } from '$lib/utils/agenticGroups';
	import { streamPerfEnd, streamPerfStart } from '$lib/utils/streamPerf';

	export let id;
	export let content;
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
	// content_blocks migration and for non-message renderings.
	export let content_blocks = null;

	export let history;
	export let messageId;
	export let chatId = '';
	export let dataVizOverrides = {};

	export let selectedModels = [];

	export let done = true;
	export let model = null;
	export let sources = null;
	export let sandboxFiles = [];

	export let save = false;
	export let preview = false;
	export let floatingButtons = true;

	export let editCodeBlock = true;
	export let topPadding = false;
	// When true, done messages parse their markdown synchronously instead of on a
	// short debounce. Used on initial chat load so historical content reaches its
	// final height before the settle loop reveals the messages.
	export let parseImmediately = false;

	// Clean terminal signals for the "Working for X" agentic-step bundling.
	// `done` (above) is polluted by the chatFadeStreamingText setting upstream,
	// so it can't be used to tell whether the message is still generating.
	// `messageDone` is the unfiltered message.done; `messageStopped` /
	// `messageErrored` choose the frozen header wording on a terminal burst.
	export let messageDone = false;
	export let messageStopped = false;
	export let messageErrored = false;

	export let onSave = (e) => {};
	export let onSourceClick = (e) => {};
	export let onTaskClick = (e) => {};
	export let onAddMessages = (e) => {};

	let contentContainerElement;
	let floatingButtonsElement;

	// Cached per-block markdown projections. The important nuance for v2 is that
	// not only the tail block can change: a tool_calls block may receive results
	// after later text/reasoning blocks have opened. Track a cheap signature per
	// block and only re-project blocks whose render-relevant data changed.
	/** @type {string[]} */
	let blockProjections = [];
	/** @type {string[]} */
	let blockToolPayloads = [];
	/** @type {string[]} */
	let blockProjectionSignatures = [];
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

	const toolBlockPayload = (block) => {
		const calls = Array.isArray(block?.content)
			? block.content.map((call) => ({
					id: call?.id ?? '',
					tool_call_id: call?.tool_call_id ?? '',
					function: {
						name: call?.function?.name ?? '',
						arguments: call?.function?.arguments ?? ''
					}
				}))
			: [];
		const results = Array.isArray(block?.results)
			? block.results.map((result) => ({
					tool_call_id: result?.tool_call_id ?? '',
					content: result?.content ?? '',
					result_ref: result?.result_ref ?? '',
					result_lazy: result?.result_lazy === true,
					size: result?.size ?? '',
					sha256: result?.sha256 ?? '',
					summary: result?.summary ?? null,
					files: Array.isArray(result?.files) ? result.files : [],
					embeds: Array.isArray(result?.embeds) ? result.embeds : [],
					subagent_id: result?.subagent_id ?? '',
					error: result?.error === true,
					error_reason: result?.error_reason ?? '',
					notice: result?.notice ?? ''
				}))
			: [];

		return JSON.stringify({ content: calls, results });
	};

	const blockProjectionSignature = (block) => {
		if (!block || typeof block !== 'object') return 'null';
		const type = block.type ?? '';

		if (type === 'text') {
			return `text:${textSig(block.content)}`;
		}

		if (type === 'reasoning') {
			return [
				'reasoning',
				textSig(block.content),
				block.started_at ?? '',
				block.ended_at ?? '',
				block.duration ?? ''
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

	$: {
		const perf = streamPerfStart();
		/** @type {any[]} */
		const blocks = Array.isArray(content_blocks) ? content_blocks : [];
		if (blocks.length === 0) {
			if (blockProjections.length !== 0) blockProjections = [];
			if (blockToolPayloads.length !== 0) blockToolPayloads = [];
			if (blockProjectionSignatures.length !== 0) blockProjectionSignatures = [];
		} else {
			/** @type {string[]} */
			const next = [];
			/** @type {string[]} */
			const nextToolPayloads = [];
			/** @type {string[]} */
			const nextSignatures = [];
			for (let i = 0; i < blocks.length; i++) {
				const signature = blockProjectionSignature(blocks[i]);
				nextSignatures[i] = signature;
				if (blocks[i]?.type === 'tool_calls') {
					next[i] = '';
					nextToolPayloads[i] =
						signature !== blockProjectionSignatures[i] || blockToolPayloads[i] == null
							? toolBlockPayload(blocks[i])
							: blockToolPayloads[i];
				} else if (signature !== blockProjectionSignatures[i] || blockProjections[i] == null) {
					next[i] = blocksToDisplayMarkdown([blocks[i]]);
					nextToolPayloads[i] = '';
				} else {
					next[i] = blockProjections[i];
					nextToolPayloads[i] = '';
				}
			}
			blockProjectionSignatures = nextSignatures;
			blockProjections = next;
			blockToolPayloads = nextToolPayloads;
		}
		streamPerfEnd('render.content_projection', perf, blocks.length || 1);
	}

	// Single render path: per-block projections when `content_blocks` is
	// provided, otherwise a one-element array carrying the legacy `content`
	// string. Keeps the `<Markdown>` invocation in exactly one place.
	//
	// `structuredMode` stays true for the duration of a structured render so
	// each block's id is `${id}-b${i}` from the start — preventing a re-mount
	// of block-0 when a second block gets appended (which would otherwise
	// flip the id from `${id}` to `${id}-b0`, triggering Markdown's `{#key
	// id}` to tear down and rebuild the rendered DOM).
	$: structuredBlocks = Array.isArray(content_blocks) ? content_blocks : [];
	$: structuredMode = structuredBlocks.length > 0;

	// "Working for X" agentic-step bundling. When enabled, contiguous bursts of
	// reasoning+tool_calls collapse into one WorkingBlock; otherwise the legacy
	// flat per-block layout. Pure O(N) pass over the block array — the projection
	// cache above is untouched, so streaming stays O(changed-blocks).
	$: bundleAgentic = $settings?.bundleAgenticSteps ?? true;
	$: agenticAutoExpand = $settings?.agenticStepsAutoExpand ?? true;
	$: renderItems = computeAgenticRenderItems(structuredBlocks, bundleAgentic);
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
	$: tailItem = renderItems.length ? renderItems[renderItems.length - 1] : null;
	$: tailBlock = tailItem && tailItem.kind === 'block' ? structuredBlocks[tailItem.index] : null;
	$: tailIsSilent =
		tailBlock != null &&
		((tailBlock.type === 'text' && `${tailBlock.content ?? ''}`.trim().length === 0) ||
			tailBlock.type === 'user_steer');
	$: showTailCursor =
		structuredMode && !messageDone && !messageStopped && !messageErrored && tailIsSilent;
	/** @type {string[]} */
	let sourceIds = [];

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

	$: {
		model;
		sourceIds = getSourceIds(sources ?? []);
	}

	const updateButtonPosition = (event) => {
		const buttonsContainerElement = document.getElementById(`floating-buttons-${id}`);
		if (
			!contentContainerElement?.contains(event.target) &&
			!buttonsContainerElement?.contains(event.target)
		) {
			closeFloatingButtons();
			return;
		}

		setTimeout(async () => {
			await tick();

			if (!contentContainerElement?.contains(event.target)) return;

			let selection = window.getSelection();

			if (selection.toString().trim().length > 0) {
				const range = selection.getRangeAt(0);
				const rect = range.getBoundingClientRect();

				const parentRect = contentContainerElement.getBoundingClientRect();

				// Adjust based on parent rect
				const top = rect.bottom - parentRect.top;
				const left = rect.left - parentRect.left;

				if (buttonsContainerElement) {
					buttonsContainerElement.style.display = 'block';

					// Calculate space available on the right
					const spaceOnRight = parentRect.width - left;
					let halfScreenWidth = $mobile ? window.innerWidth / 2 : window.innerWidth / 3;

					if (spaceOnRight < halfScreenWidth) {
						const right = parentRect.right - rect.right;
						buttonsContainerElement.style.right = `${right}px`;
						buttonsContainerElement.style.left = 'auto'; // Reset left
					} else {
						// Enough space, position using 'left'
						buttonsContainerElement.style.left = `${left}px`;
						buttonsContainerElement.style.right = 'auto'; // Reset right
					}
					buttonsContainerElement.style.top = `${top + 5}px`; // +5 to add some spacing
				}
			} else {
				closeFloatingButtons();
			}
		}, 0);
	};

	const closeFloatingButtons = () => {
		const buttonsContainerElement = document.getElementById(`floating-buttons-${id}`);
		if (buttonsContainerElement) {
			buttonsContainerElement.style.display = 'none';
		}

		if (floatingButtonsElement) {
			// check if closeHandler is defined

			if (typeof floatingButtonsElement?.closeHandler === 'function') {
				// call the closeHandler function
				floatingButtonsElement?.closeHandler();
			}
		}
	};

	const keydownHandler = (e) => {
		if (e.key === 'Escape') {
			closeFloatingButtons();
		}
	};

	onMount(() => {
		if (floatingButtons) {
			contentContainerElement?.addEventListener('mouseup', updateButtonPosition);
			document.addEventListener('mouseup', updateButtonPosition);
			document.addEventListener('keydown', keydownHandler);
		}
	});

	onDestroy(() => {
		if (floatingButtons) {
			contentContainerElement?.removeEventListener('mouseup', updateButtonPosition);
			document.removeEventListener('mouseup', updateButtonPosition);
			document.removeEventListener('keydown', keydownHandler);
		}
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
		{#each renderItems as item (renderItemKey(item))}
			{#if item.kind === 'group'}
				{@const lastItem = renderItems[renderItems.length - 1]}
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
					members={item.indices.map((bi) => ({
						index: bi,
						block: structuredBlocks[bi],
						projection: blockProjections[bi] ?? '',
						toolPayload: blockToolPayloads[bi] ?? ''
					}))}
				/>
			{:else if structuredBlocks[item.index]?.type === 'tool_calls'}
				<ToolCallsBlock
					id={`${id}-b${item.index}`}
					blockJson={blockToolPayloads[item.index] ?? ''}
					{chatId}
					{messageId}
				/>
			{:else if structuredBlocks[item.index]?.type === 'user_steer'}
				<!-- Mid-task user interjection (steering): the user sent this while the
				     model was working and the backend injected it at a tool-call
				     boundary. Render as an inline user bubble so the transcript shows
				     exactly what the model saw, in order. -->
				<div class="flex justify-end my-2.5" dir="ltr">
					<div
						class="flex flex-col items-end max-w-[80%]"
						aria-label="Steered message"
					>
						<div
							class="flex items-center gap-1 mb-1 text-[11px] text-gray-400 dark:text-gray-500 pr-0.5"
						>
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
							<span>{$i18n.t('Steered')}</span>
						</div>
						<div
							class="rounded-2xl px-3.5 py-2 bg-gray-50 dark:bg-gray-850 text-gray-800 dark:text-gray-100 text-sm whitespace-pre-wrap break-words"
						>
							{structuredBlocks[item.index]?.content ?? ''}
						</div>
					</div>
				</div>
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
		{#if showTailCursor}
			<Skeleton size="md" />
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

{#if floatingButtons && model}
	<FloatingButtons
		bind:this={floatingButtonsElement}
		{id}
		{messageId}
		{chatId}
		actions={$settings?.floatingActionButtons ?? []}
		model={(selectedModels ?? []).includes(model?.id)
			? model?.id
			: (selectedModels ?? []).length > 0
				? selectedModels.at(0)
				: model?.id}
		messages={createMessagesList(history, messageId)}
		onAdd={({ modelId, parentId, messages }) => {
			console.log(modelId, parentId, messages);
			onAddMessages({ modelId, parentId, messages });
			closeFloatingButtons();
		}}
	/>
{/if}
