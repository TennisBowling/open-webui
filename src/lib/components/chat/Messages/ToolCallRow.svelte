<script lang="ts">
	// Adapter: a tool-call summary onto the shared transcript row, so a tool call
	// and a thinking block render as the same kind of line.
	import type { ToolCallSummary } from '$lib/utils/toolResults';
	import TranscriptRow from './TranscriptRow.svelte';

	interface Props {
		summary: ToolCallSummary;
		open?: boolean;
		done?: boolean;
		errored?: boolean;
		errorReason?: string;
		notice?: string;
	}

	let {
		summary,
		open = false,
		done = true,
		errored = false,
		errorReason = '',
		notice = ''
	}: Props = $props();
</script>

<TranscriptRow
	icon={summary.icon}
	label={summary.title}
	detail={summary.detail ?? ''}
	detailMono={summary.detailMono ?? false}
	meta={done ? (summary.meta ?? '') : ''}
	metaError={summary.metaError ?? false}
	trailing={done ? (errored ? errorReason : notice) : ''}
	trailingTone={errored ? 'error' : 'warning'}
	{open}
	pending={!done}
	{errored}
/>
