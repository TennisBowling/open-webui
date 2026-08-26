<script lang="ts">
	import Tooltip from '$lib/components/common/Tooltip.svelte';
	import { tick, getContext, onMount, onDestroy } from 'svelte';
	import { toast } from '$lib/utils/toast';

	const i18n = getContext('i18n');

	let selectedPromptIdx = $state(0);
	interface Props {
		query?: string;
		prompts?: any;
		onSelect?: any;
		filteredItems?: any;
	}

	let {
		query = '',
		prompts = [],
		onSelect = (e) => {},
		filteredItems = $bindable([])
	}: Props = $props();

	$effect(() => {
		filteredItems = prompts
			.filter((p) => p.command.toLowerCase().includes(query.toLowerCase()))
			.sort((a, b) => a.title.localeCompare(b.title));
	});

	$effect(() => {
		if (query) {
			selectedPromptIdx = 0;
		}
	});

	export const selectUp = () => {
		selectedPromptIdx = Math.max(0, selectedPromptIdx - 1);
	};
	export const selectDown = () => {
		selectedPromptIdx = Math.min(selectedPromptIdx + 1, filteredItems.length - 1);
	};

	export const select = async () => {
		const command = filteredItems[selectedPromptIdx];
		if (command) {
			onSelect({ type: 'prompt', data: command });
		}
	};
</script>

<div class="px-2 text-xs text-gray-500 py-1">
	{$i18n.t('Prompts')}
</div>

{#if filteredItems.length > 0}
	<div class=" space-y-0.5 scrollbar-hidden">
		{#each filteredItems as promptItem, promptIdx}
			<Tooltip content={promptItem.title} placement="top-start">
				<button
					class=" px-3 py-1 rounded-xl w-full text-left {promptIdx === selectedPromptIdx
						? '  bg-gray-50 dark:bg-gray-800 selected-command-option-button'
						: ''} truncate"
					type="button"
					onclick={() => {
						onSelect({ type: 'prompt', data: promptItem });
					}}
					onmousemove={() => {
						selectedPromptIdx = promptIdx;
					}}
					onfocus={() => {}}
					data-selected={promptIdx === selectedPromptIdx}
				>
					<span class=" font-medium text-gray-900 dark:text-gray-100">
						{promptItem.command}
					</span>

					<span class=" text-xs text-gray-600 dark:text-gray-100">
						{promptItem.title}
					</span>
				</button>
			</Tooltip>
		{/each}
	</div>
{/if}
