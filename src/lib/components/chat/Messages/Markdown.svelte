<script>
	import { marked } from 'marked';
	import { replaceTokens, processResponseContent } from '$lib/utils';
	import { user } from '$lib/stores';
	import { onMount, onDestroy } from 'svelte';

	import markedExtension from '$lib/utils/marked/extension';
	import markedKatexExtension from '$lib/utils/marked/katex-extension';
	import { mentionExtension } from '$lib/utils/marked/mention-extension';
	import { streamPerfEnd, streamPerfStart } from '$lib/utils/streamPerf';

	import MarkdownTokens from './Markdown/MarkdownTokens.svelte';

	export let id = '';
	export let content;
	export let done = true;
	export let model = null;
	export let save = false;
	export let preview = false;
	export let parseImmediately = false;
	export let allowStreamingPlainText = false;

	export let chatId = '';
	export let messageId = '';
	export let dataVizOverrides = {};
	export let sandboxFiles = [];

	export let editCodeBlock = true;
	export let topPadding = false;

	export let sourceIds = [];

	export let onSave = () => {};
	export let onUpdate = () => {};

	export let onPreview = () => {};
	export let onSourceClick = () => {};
	export let onTaskClick = () => {};

	let tokens = [];
	let pendingContent = null;
	let parseTimeout = null;
	let lastParsedContent = '';

	// Throttle interval in ms - parse less frequently during streaming
	const STREAMING_THROTTLE = 100; // 10 updates per second max during streaming
	const DONE_DELAY = 50; // Small delay when done to ensure final parse
	const STREAMING_PLAINTEXT_CHARS = 4000;

	$: streamingPlainText =
		allowStreamingPlainText &&
		!done &&
		typeof content === 'string' &&
		content.length > STREAMING_PLAINTEXT_CHARS;

	const options = {
		throwOnError: false,
		breaks: true
	};

	marked.use(markedKatexExtension(options));
	marked.use(markedExtension(options));
	marked.use({
		extensions: [mentionExtension({ triggerChar: '@' }), mentionExtension({ triggerChar: '#' })]
	});

	const parseContent = (contentToParse) => {
		if (contentToParse && contentToParse !== lastParsedContent) {
			const perf = streamPerfStart();
			lastParsedContent = contentToParse;
			tokens = marked.lexer(
				replaceTokens(processResponseContent(contentToParse), sourceIds, model?.name, $user?.name)
			);
			streamPerfEnd(done ? 'markdown.parse.done' : 'markdown.parse.streaming', perf);
		}
	};

	const scheduleParse = () => {
		// Static, already-complete disclosure bodies need their final height before
		// the parent slide transition measures them. Keep the default small done
		// delay for normal message rendering, but let callers opt into eager parse.
		if (done && parseImmediately) {
			if (parseTimeout) {
				clearTimeout(parseTimeout);
			}
			if (pendingContent !== null) {
				parseContent(pendingContent);
				pendingContent = null;
			}
			parseTimeout = null;
			return;
		}

		// If done, clear any pending timeouts and parse immediately with minimal delay
		if (done) {
			if (parseTimeout) {
				clearTimeout(parseTimeout);
			}
			parseTimeout = setTimeout(() => {
				if (pendingContent !== null) {
					parseContent(pendingContent);
					pendingContent = null;
				}
				parseTimeout = null;
			}, DONE_DELAY);
			return;
		}

		// During streaming, act as a true throttle (not a debounce)
		// If a timeout is already scheduled, do nothing and let it fire
		if (parseTimeout) {
			return;
		}

		parseTimeout = setTimeout(() => {
			if (pendingContent !== null) {
				parseContent(pendingContent);
				pendingContent = null;
			}
			parseTimeout = null;
		}, STREAMING_THROTTLE);
	};

	// Use a reactive statement that just schedules parsing instead of doing it immediately
	$: {
		if (streamingPlainText) {
			if (parseTimeout) {
				clearTimeout(parseTimeout);
				parseTimeout = null;
			}
			pendingContent = null;
			tokens = [];
		} else if (content) {
			pendingContent = content;
			scheduleParse();
		}
	}

	onDestroy(() => {
		if (parseTimeout) {
			clearTimeout(parseTimeout);
		}
	});
</script>

{#if streamingPlainText}
	<div dir="auto" class="whitespace-pre-wrap break-words">{content}</div>
{:else}
	{#key id}
		<MarkdownTokens
			{tokens}
			{id}
			{done}
			{save}
			{preview}
			{editCodeBlock}
			{topPadding}
			{chatId}
			{messageId}
			{dataVizOverrides}
			{sandboxFiles}
			{onTaskClick}
			{onSourceClick}
			{onSave}
			{onUpdate}
			{onPreview}
		/>
	{/key}
{/if}
