<script lang="ts">
	import Collapsible from '$lib/components/common/Collapsible.svelte';

	export let id = '';
	export let block: any = {};
	export let chatId = '';
	export let messageId = '';

	const subagentToolNames = new Set([
		'subagent_launch',
		'subagent_continue',
		'subagent_agent_launch'
	]);

	$: calls = Array.isArray(block?.content) ? block.content : [];
	$: results = Array.isArray(block?.results) ? block.results : [];

	const getResultForCall = (call: any) => {
		const callId = call?.id ?? call?.tool_call_id ?? '';
		return results.find((result: any) => result?.tool_call_id === callId);
	};

	const hasOwn = (value: object, key: string) => Object.prototype.hasOwnProperty.call(value, key);

	const hasResultPayload = (result: any) => {
		if (!result || typeof result !== 'object') return false;
		return (
			hasOwn(result, 'content') ||
			(Array.isArray(result.files) && result.files.length > 0) ||
			(Array.isArray(result.embeds) && result.embeds.length > 0) ||
			!!result.subagent_id
		);
	};

	const stringifyIfNeeded = (value: any) => {
		if (value == null) return '';
		return typeof value === 'string' ? value : JSON.stringify(value);
	};

	const attributesForCall = (call: any) => {
		const result = getResultForCall(call);
		const name = call?.function?.name ?? '';
		const isSubagent = subagentToolNames.has(name);
		const resultPayloadReady = isSubagent ? result !== undefined : hasResultPayload(result);

		return {
			type: isSubagent ? 'subagent_launch' : 'tool_calls',
			done: resultPayloadReady ? 'true' : 'false',
			id: result?.subagent_id ?? call?.id ?? call?.tool_call_id ?? '',
			tool_call_id: call?.id ?? call?.tool_call_id ?? '',
			chat_id: chatId,
			message_id: messageId,
			name,
			arguments: stringifyIfNeeded(call?.function?.arguments ?? ''),
			result: result?.content ?? '',
			result_ref: result?.result_ref ?? '',
			result_lazy: result?.result_lazy ? 'true' : 'false',
			size: result?.size ?? '',
			sha256: result?.sha256 ?? '',
			summary: result?.summary ? JSON.stringify(result.summary) : '',
			files: result?.files ? JSON.stringify(result.files) : '',
			embeds: result?.embeds ? JSON.stringify(result.embeds) : ''
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
