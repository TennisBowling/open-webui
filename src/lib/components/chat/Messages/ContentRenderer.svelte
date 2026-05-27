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
		showOverview
	} from '$lib/stores';
	import FloatingButtons from '../ContentRenderer/FloatingButtons.svelte';
	import ToolCallsBlock from './ToolCallsBlock.svelte';
	import { blocksToDisplayMarkdown, createMessagesList } from '$lib/utils';

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

	export let save = false;
	export let preview = false;
	export let floatingButtons = true;

	export let editCodeBlock = true;
	export let topPadding = false;

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
	let blockProjectionSignatures = [];

	const textSig = (value) => {
		const text = value == null ? '' : String(value);
		return `${text.length}:${text.slice(0, 32)}:${text.slice(-32)}`;
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
			// Tool calls render structurally via <ToolCallsBlock>, not markdown.
			return 'tool_calls:direct';
		}

		if (type === 'code_interpreter') {
			return `code:${textSig(block.content)}:${textSig(JSON.stringify(block.output ?? null))}`;
		}

		return `${type}:${textSig(JSON.stringify(block))}`;
	};

	$: {
		/** @type {any[]} */
		const blocks = Array.isArray(content_blocks) ? content_blocks : [];
		if (blocks.length === 0) {
			if (blockProjections.length !== 0) blockProjections = [];
			if (blockProjectionSignatures.length !== 0) blockProjectionSignatures = [];
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
				} else if (signature !== blockProjectionSignatures[i] || blockProjections[i] == null) {
					next[i] = blocksToDisplayMarkdown([blocks[i]]);
				} else {
					next[i] = blockProjections[i];
				}
			}
			blockProjectionSignatures = nextSignatures;
			blockProjections = next;
		}
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
</script>

<div bind:this={contentContainerElement}>
	{#if structuredMode}
		{#each structuredBlocks as block, i (i)}
			{#if block?.type === 'tool_calls'}
				<ToolCallsBlock id={`${id}-b${i}`} {block} />
			{:else}
				<Markdown
					id={`${id}-b${i}`}
					content={blockProjections[i] ?? ''}
					{model}
					{save}
					{preview}
					{done}
					{editCodeBlock}
					topPadding={i === 0 ? topPadding : false}
					{chatId}
					{messageId}
					{dataVizOverrides}
					sourceIds={(sources ?? []).reduce((acc, source) => {
						let ids = [];
						source.document.forEach((document, index) => {
							if (model?.info?.meta?.capabilities?.citations == false) {
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

						// remove duplicates
						return acc.filter((item, index) => acc.indexOf(item) === index);
					}, [])}
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
					}}
				/>
			{/if}
		{/each}
	{:else}
		<Markdown
			{id}
			content={content ?? ''}
			{model}
			{save}
			{preview}
			{done}
			{editCodeBlock}
			{topPadding}
			{chatId}
			{messageId}
			{dataVizOverrides}
			sourceIds={(sources ?? []).reduce((acc, source) => {
				let ids = [];
				source.document.forEach((document, index) => {
					if (model?.info?.meta?.capabilities?.citations == false) {
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
			}, [])}
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
