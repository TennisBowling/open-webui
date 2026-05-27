<script lang="ts">
	import Collapsible from '$lib/components/common/Collapsible.svelte';

	export let id = '';
	export let block: any = {};

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

	const stringifyIfNeeded = (value: any) => {
		if (value == null) return '';
		return typeof value === 'string' ? value : JSON.stringify(value);
	};

	const attributesForCall = (call: any) => {
		const result = getResultForCall(call);
		const name = call?.function?.name ?? '';
		const isSubagent = subagentToolNames.has(name);

		return {
			type: isSubagent ? 'subagent_launch' : 'tool_calls',
			done: result !== undefined ? 'true' : 'false',
			id: result?.subagent_id ?? call?.id ?? call?.tool_call_id ?? '',
			tool_call_id: call?.id ?? call?.tool_call_id ?? '',
			name,
			arguments: stringifyIfNeeded(call?.function?.arguments ?? ''),
			result: result?.content ?? '',
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
