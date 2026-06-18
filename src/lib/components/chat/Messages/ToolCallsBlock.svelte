<script lang="ts">
	import Collapsible from '$lib/components/common/Collapsible.svelte';

	export let id = '';
	export let block: any = {};
	export let blockJson = '';
	export let chatId = '';
	export let messageId = '';

	const subagentToolNames = new Set([
		'subagent_launch',
		'subagent_continue',
		'subagent_agent_launch'
	]);

	// Backend prefixes MCP tool names with `mcp_<8hex>_` to satisfy the
	// OpenAI/OpenRouter 64-char function-name limit. The model still sees
	// (and uses) the alias on the wire, but in the UI we'd rather show the
	// real tool name. Falls through unchanged for non-MCP / non-aliased
	// names.
	const MCP_ALIAS_RE = /^mcp_[0-9a-f]{8}_(.+)$/;
	const friendlyName = (name: string) => {
		if (!name) return '';
		const m = name.match(MCP_ALIAS_RE);
		return m ? m[1] : name;
	};

	let parsedBlock: any = {};
	let parsedBlockJson = '';

	$: if (blockJson !== parsedBlockJson) {
		parsedBlockJson = blockJson;
		try {
			parsedBlock = blockJson ? JSON.parse(blockJson) : {};
		} catch {
			parsedBlock = {};
		}
	}

	$: renderBlock = blockJson ? parsedBlock : block;
	$: calls = Array.isArray(renderBlock?.content) ? renderBlock.content : [];
	$: results = Array.isArray(renderBlock?.results) ? renderBlock.results : [];
	let resultByCallId: Map<string, any> = new Map();
	$: resultByCallId = new Map<string, any>(
		results
			.filter((result: any) => result?.tool_call_id)
			.map((result: any) => [result.tool_call_id, result])
	);

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
		const resultPayloadReady = isSubagent ? result !== undefined : hasResultPayload(result);

		return {
			type: isSubagent ? 'subagent_launch' : 'tool_calls',
			done: resultPayloadReady ? 'true' : 'false',
			id: result?.subagent_id ?? call?.id ?? call?.tool_call_id ?? '',
			tool_call_id: call?.id ?? call?.tool_call_id ?? '',
			chat_id: chatId,
			message_id: messageId,
			name: friendlyName(name),
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
			notice: result?.notice ?? ''
		};
	};
</script>

<div class="space-y-1">
	{#each calls as call, idx (call?.id ?? call?.tool_call_id ?? idx)}
		<Collapsible
			id={`${id}-tool-call-${call?.id ?? call?.tool_call_id ?? idx}`}
			attributes={attributesForCall(call)}
			className="w-full space-y-1"
		/>
	{/each}
</div>
