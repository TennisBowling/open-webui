<script lang="ts">
	import { getContext } from 'svelte';

	const i18n: any = getContext('i18n');

	interface ToolEntry {
		id?: string;
		name?: string;
	}

	interface Props {
		block: {
			added?: ToolEntry[];
			removed?: ToolEntry[];
		};
	}

	let { block }: Props = $props();

	let added = $derived(
		(block?.added ?? []).map((item) => item?.name || item?.id).filter(Boolean) as string[]
	);
	let removed = $derived(
		(block?.removed ?? []).map((item) => item?.name || item?.id).filter(Boolean) as string[]
	);
	let summary = $derived(
		[
			added.length ? $i18n.t('Added {{tools}}', { tools: added.join(', ') }) : '',
			removed.length ? $i18n.t('Removed {{tools}}', { tools: removed.join(', ') }) : ''
		]
			.filter(Boolean)
			.join(' · ')
	);
</script>

{#if summary}
	<div class="flex justify-end my-2.5" dir="ltr">
		<div
			class="inline-flex max-w-[85%] items-center gap-1.5 rounded-full border-hairline border-gray-200/80 bg-gray-50/80 px-2.5 py-1 text-[11px] leading-4 text-gray-500 dark:border-gray-700/80 dark:bg-gray-850/80 dark:text-gray-400"
			aria-label={$i18n.t('Tool selection changed')}
		>
			<svg
				xmlns="http://www.w3.org/2000/svg"
				viewBox="0 0 20 20"
				fill="currentColor"
				class="size-3 shrink-0"
				aria-hidden="true"
			>
				<path
					d="M11.983 1.904a.75.75 0 0 0-.832.173L8.6 4.628a3.75 3.75 0 0 0-4.978 4.978l-1.545 1.545a.75.75 0 0 0 0 1.06l5.712 5.712a.75.75 0 0 0 1.06 0l1.545-1.545a3.75 3.75 0 0 0 4.978-4.978l2.551-2.551a.75.75 0 0 0 .173-.832 6.75 6.75 0 0 1-6.113-6.113ZM6.25 6.5a2.25 2.25 0 0 1 1.57.638l-2.682 2.681A2.25 2.25 0 0 1 6.25 6.5Zm7.25 7.25a2.25 2.25 0 0 1-3.319 1.112l2.681-2.681A2.25 2.25 0 0 1 13.5 13.75Z"
				/>
			</svg>
			<span class="truncate">{summary}</span>
		</div>
	</div>
{/if}
