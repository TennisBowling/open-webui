<script lang="ts" module>
	const MARKDOWN_TOKEN_CACHE_MAX_ENTRIES = 128;
	const MARKDOWN_TOKEN_CACHE_MAX_CHARS = 2_000_000;
	const markdownTokenCache = new Map<string, { tokens: any[]; cost: number }>();
	let markdownTokenCacheCost = 0;

	const hashString = (value: string) => {
		let hash = 2166136261;
		for (let i = 0; i < value.length; i += 1) {
			hash ^= value.charCodeAt(i);
			hash = Math.imul(hash, 16777619);
		}
		return (hash >>> 0).toString(36);
	};

	const markdownCacheKey = (content: string) => `${content.length}:${hashString(content)}`;

	export const getCachedMarkdownTokens = (content: string) => {
		const key = markdownCacheKey(content);
		const entry = markdownTokenCache.get(key);
		if (!entry) return null;
		markdownTokenCache.delete(key);
		markdownTokenCache.set(key, entry);
		return entry.tokens;
	};

	export const setCachedMarkdownTokens = (content: string, tokens: any[]) => {
		const cost = content.length;
		if (cost > MARKDOWN_TOKEN_CACHE_MAX_CHARS) return tokens;
		const key = markdownCacheKey(content);
		const existing = markdownTokenCache.get(key);
		if (existing) {
			markdownTokenCacheCost -= existing.cost;
			markdownTokenCache.delete(key);
		}
		markdownTokenCache.set(key, { tokens, cost });
		markdownTokenCacheCost += cost;
		while (
			markdownTokenCache.size > MARKDOWN_TOKEN_CACHE_MAX_ENTRIES ||
			markdownTokenCacheCost > MARKDOWN_TOKEN_CACHE_MAX_CHARS
		) {
			const oldestKey = markdownTokenCache.keys().next().value;
			if (!oldestKey) break;
			const oldest = markdownTokenCache.get(oldestKey);
			markdownTokenCache.delete(oldestKey);
			markdownTokenCacheCost -= oldest?.cost ?? 0;
		}
		return tokens;
	};
</script>

<script lang="ts">
	import { marked } from 'marked';
	import { replaceTokens, processResponseContent } from '$lib/utils';
	import { user } from '$lib/stores';
	import { onMount, onDestroy, untrack } from 'svelte';

	import markedExtension from '$lib/utils/marked/extension';
	import markedKatexExtension from '$lib/utils/marked/katex-extension';
	import { mentionExtension } from '$lib/utils/marked/mention-extension';
	import { streamPerfEnd, streamPerfStart } from '$lib/utils/streamPerf';

	import MarkdownTokens from './Markdown/MarkdownTokens.svelte';

	interface Props {
		id?: string;
		content: any;
		done?: boolean;
		model?: any;
		save?: boolean;
		preview?: boolean;
		parseImmediately?: boolean;
		chatId?: string;
		messageId?: string;
		dataVizOverrides?: any;
		sandboxFiles?: any;
		editCodeBlock?: boolean;
		topPadding?: boolean;
		sourceIds?: any;
		onSave?: any;
		onUpdate?: any;
		onPreview?: any;
		onSourceClick?: any;
		onTaskClick?: any;
	}

	let {
		id = '',
		content,
		done = true,
		model = null,
		save = false,
		preview = false,
		parseImmediately = false,
		chatId = '',
		messageId = '',
		dataVizOverrides = {},
		sandboxFiles = [],
		editCodeBlock = true,
		topPadding = false,
		sourceIds = [],
		onSave = () => {},
		onUpdate = () => {},
		onPreview = () => {},
		onSourceClick = () => {},
		onTaskClick = () => {}
	}: Props = $props();

	let tokens = $state([]);
	let pendingContent = $state(null);
	let parseTimeout = null;
	let parseIdle = null;
	let lastParsedContent = '';

	// Throttle interval in ms - parse less frequently during streaming
	const STREAMING_THROTTLE = 100; // 10 updates per second max during streaming
	const DONE_DELAY = 50; // Small delay when done to ensure final parse
	const DONE_IDLE_PARSE_CHARS = 80_000;

	// Scale the streaming re-parse throttle up for very large in-flight blocks
	// (e.g. a huge generated document) so re-lexing the whole thing doesn't
	// dominate CPU during streaming. Small/typical messages are unaffected.
	const getStreamingThrottleMs = (contentLength) => {
		if (contentLength > 60_000) return 400;
		if (contentLength > 20_000) return 200;
		return STREAMING_THROTTLE;
	};

	const clearScheduledParse = () => {
		if (parseTimeout) {
			clearTimeout(parseTimeout);
			parseTimeout = null;
		}
		const idleWindow = typeof window !== 'undefined' ? window : null;
		if (parseIdle !== null && idleWindow?.cancelIdleCallback) {
			idleWindow.cancelIdleCallback(parseIdle);
			parseIdle = null;
		}
	};

	const runPendingParse = () => {
		if (pendingContent !== null) {
			parseContent(pendingContent);
			pendingContent = null;
		}
		parseTimeout = null;
		parseIdle = null;
	};

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
			const prepared = replaceTokens(
				processResponseContent(contentToParse),
				sourceIds,
				model?.name,
				$user?.name
			);
			const cached = done ? getCachedMarkdownTokens(prepared) : null;
			tokens = cached ?? marked.lexer(prepared);
			if (done && !cached) {
				setCachedMarkdownTokens(prepared, tokens);
			}
			streamPerfEnd(done ? 'markdown.parse.done' : 'markdown.parse.streaming', perf);
		}
	};

	const scheduleParse = () => {
		// Static, already-complete disclosure bodies need their final height before
		// the parent slide transition measures them. Keep the default small done
		// delay for normal message rendering, but let callers opt into eager parse.
		if (done && parseImmediately) {
			clearScheduledParse();
			runPendingParse();
			return;
		}

		// If done, clear any pending timeouts and parse with minimal delay. Very
		// large completed blocks use idle time (with a timeout) so toggling/opening
		// historical tool/reasoning content doesn't monopolize the main thread.
		if (done) {
			clearScheduledParse();
			const idleWindow = typeof window !== 'undefined' ? window : null;
			if (
				idleWindow?.requestIdleCallback &&
				content?.length > DONE_IDLE_PARSE_CHARS &&
				idleWindow.requestIdleCallback
			) {
				parseIdle = idleWindow.requestIdleCallback(runPendingParse, { timeout: 500 });
			} else {
				parseTimeout = setTimeout(runPendingParse, DONE_DELAY);
			}
			return;
		}

		// During streaming, act as a true throttle (not a debounce)
		// If a timeout is already scheduled, do nothing and let it fire
		if (parseTimeout) {
			return;
		}

		parseTimeout = setTimeout(runPendingParse, getStreamingThrottleMs(pendingContent?.length ?? 0));
	};

	// Use a reactive statement that just schedules parsing instead of doing it immediately
	$effect(() => {
		const nextContent = content;
		// These are deliberate parse triggers. The scheduler and parser also
		// read/write their own queue, token, timeout, and cache state; keep those
		// implementation details out of this effect's dependency set so an eager
		// parse cannot recursively schedule itself.
		void done;
		void parseImmediately;
		if (nextContent) {
			untrack(() => {
				pendingContent = nextContent;
				scheduleParse();
			});
		}
	});

	onDestroy(() => {
		clearScheduledParse();
	});
</script>

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
