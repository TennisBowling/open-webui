<script lang="ts" module>
	// Lazy + cached: keeps the DataViz chunk off the first-render path while
	// handing {#await} a stable promise (a fresh promise per render would
	// re-enter the pending branch on every streaming re-render).
	let dataVizModulePromise: Promise<any> | null = null;
	const loadDataVizWidget = () =>
		(dataVizModulePromise ??= import('$lib/components/chat/Messages/DataVizWidget.svelte'));
</script>

<script lang="ts">
	import Collapsible from '$lib/components/common/Collapsible.svelte';

	// Reload-time persisted auto-repair overrides for data-viz widgets, keyed by a
	// hash of the ORIGINAL widget_code. Threaded down to any show_widget call so a

	// True once the parent assistant message is terminally done / stopped /
	// errored. The ask_user card uses it to detect an ORPHANED question (the
	// generation ended — e.g. the user pressed Stop — while the card was still

	interface Props {
		id?: string;
		block?: any;
		blockJson?: string;
		blockRev?: string;
		chatId?: string;
		messageId?: string;
		// widget that errored and was auto-fixed renders the working version on reload.
		dataVizOverrides?: Record<string, string>;
		// waiting) so it can disable the form instead of pretending it's live.
		messageTerminated?: boolean;
	}

	let {
		id = '',
		block = {},
		blockJson = '',
		blockRev = '',
		chatId = '',
		messageId = '',
		dataVizOverrides = {},
		messageTerminated = false
	}: Props = $props();

	const subagentToolNames = new Set([
		'subagent_launch',
		'subagent_continue',
		'subagent_agent_launch'
	]);

	let parsedBlock: any = $state({});
	let parsedBlockJson = $state('');

	$effect(() => {
		if (blockJson !== parsedBlockJson) {
			parsedBlockJson = blockJson;
			try {
				parsedBlock = blockJson ? JSON.parse(blockJson) : {};
			} catch {
				parsedBlock = {};
			}
		}
	});

	let renderBlock = $derived(blockJson ? parsedBlock : (blockRev, block));
	let calls = $derived(Array.isArray(renderBlock?.content) ? renderBlock.content : []);
	let results = $derived(Array.isArray(renderBlock?.results) ? renderBlock.results : []);
	let resultByCallId: Map<string, any> = $state(new Map());
	$effect(() => {
		resultByCallId = new Map<string, any>(
			results
				.filter((result: any) => result?.tool_call_id)
				.map((result: any) => [result.tool_call_id, result])
		);
	});

	const getResultForCall = (call: any) => {
		const callId = call?.id ?? call?.tool_call_id ?? '';
		return resultByCallId.get(callId);
	};

	const hasOwn = (value: object, key: string) => Object.prototype.hasOwnProperty.call(value, key);

	const hasResultPayload = (result: any) => {
		if (!result || typeof result !== 'object') return false;
		return (
			hasOwn(result, 'content') ||
			(Array.isArray(result.files) && result.files.length > 0) ||
			(Array.isArray(result.embeds) && result.embeds.length > 0) ||
			!!result.subagent_id ||
			result.error === true
		);
	};

	const stringifyIfNeeded = (value: any) => {
		if (value == null) return '';
		return typeof value === 'string' ? value : JSON.stringify(value);
	};

	// The built-in show_widget data-viz tool renders an inline sandboxed-iframe
	// visualization (DataVizWidget) instead of a generic tool-call row. Its
	// title / widget_code / loading_messages live in the call arguments (a JSON
	// string per the OpenAI tool-call spec — occasionally double-encoded). These
	// come straight from content_blocks (NOT an html-escaped attribute), so no
	// entity-decoding is needed — just parse the JSON, tolerating both shapes and
	// partial/streaming fragments (returns {} so the widget shows its loading
	// state until the arguments finish streaming).
	const isShowWidget = (call: any) => (call?.function?.name ?? '') === 'show_widget';

	const parseCallArguments = (call: any): Record<string, any> => {
		const raw = call?.function?.arguments;
		if (raw == null) return {};
		try {
			let value: any = raw;
			if (typeof value === 'string') value = JSON.parse(value);
			if (typeof value === 'string') value = JSON.parse(value);
			return value && typeof value === 'object' ? value : {};
		} catch {
			return {};
		}
	};

	const attributesForCall = (call: any) => {
		const result = getResultForCall(call);
		const name = call?.function?.name ?? '';
		// A subagent call that returned a result but never produced a subagent_id
		// is a MALFORMED call — the tool errored before creating a subagent (e.g.
		// missing name/prompt args). Render it as a normal tool result, not a
		// subagent card, so it doesn't get stuck on "Researching…/Subagent is
		// starting up…" for a call that already failed.
		const isMalformedSubagent =
			subagentToolNames.has(name) && result !== undefined && !result?.subagent_id;
		const isSubagent = subagentToolNames.has(name) && !isMalformedSubagent;
		// The built-in ask_user tool renders its own interactive question card
		// (AskUserBlock) instead of a generic tool-call row. The question lives in
		// the call arguments; the answer (once the user submits) is the result.
		const isAskUser = name === 'ask_user';
		const resultPayloadReady = isSubagent ? result !== undefined : hasResultPayload(result);

		return {
			type: isSubagent ? 'subagent_launch' : isAskUser ? 'ask_user' : 'tool_calls',
			done: resultPayloadReady ? 'true' : 'false',
			id: result?.subagent_id ?? call?.id ?? call?.tool_call_id ?? '',
			tool_call_id: call?.id ?? call?.tool_call_id ?? '',
			chat_id: chatId,
			message_id: messageId,
			// The MCP `mcp_<8hex>_` alias is kept here on purpose: the summary layer
			// de-aliases it for display AND uses the prefix to tell MCP tools apart
			// from built-ins when picking a glyph.
			name,
			arguments: stringifyIfNeeded(call?.function?.arguments ?? ''),
			result: result?.content ?? '',
			result_ref: result?.result_ref ?? '',
			result_lazy: result?.result_lazy ? 'true' : 'false',
			size: result?.size ?? '',
			sha256: result?.sha256 ?? '',
			summary: result?.summary ? JSON.stringify(result.summary) : '',
			files: result?.files ? JSON.stringify(result.files) : '',
			embeds: result?.embeds ? JSON.stringify(result.embeds) : '',
			error: result?.error ? 'true' : 'false',
			error_reason: result?.error_reason ?? '',
			notice: result?.notice ?? '',
			message_terminated: messageTerminated ? 'true' : 'false'
		};
	};
</script>

<div class="space-y-1">
	{#each calls as call, idx (call?.id ?? call?.tool_call_id ?? idx)}
		{#if isShowWidget(call)}
			{@const wargs = parseCallArguments(call)}
			{#await loadDataVizWidget() then DataVizWidget}
				<DataVizWidget.default
					title={wargs.title ?? 'widget'}
					widgetCode={wargs.widget_code ?? ''}
					loadingMessages={Array.isArray(wargs.loading_messages) ? wargs.loading_messages : []}
					streaming={!hasResultPayload(getResultForCall(call)) && !messageTerminated}
					{chatId}
					{messageId}
					{dataVizOverrides}
				/>
			{/await}
		{:else}
			<Collapsible
				id={`${id}-tool-call-${call?.id ?? call?.tool_call_id ?? idx}`}
				attributes={attributesForCall(call)}
				className="w-full space-y-1"
				buttonClassName="w-fit max-w-full"
			/>
		{/if}
	{/each}
</div>
