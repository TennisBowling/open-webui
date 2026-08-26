<script lang="ts">
	import Bolt from '$lib/components/icons/Bolt.svelte';
	import { onMount, getContext } from 'svelte';
	import { settings, WEBUI_NAME } from '$lib/stores';
	import { WEBUI_VERSION } from '$lib/constants';

	const i18n = getContext('i18n');

	interface Props {
		suggestionPrompts?: any;
		className?: string;
		inputValue?: string;
		onSelect?: any;
	}

	let {
		suggestionPrompts = [],
		className = '',
		inputValue = '',
		onSelect = (e) => {}
	}: Props = $props();

	let sortedPrompts = $derived([...(suggestionPrompts ?? [])].sort(() => Math.random() - 0.5));

	const fuseOptions = {
		keys: ['content', 'title'],
		threshold: 0.5
	};

	let fuse = $state();

	// Initialize Fuse — lazy-loaded off the cold path. fuse stays undefined until
	// the import resolves; the search consumer below already guards on `fuse`, so
	// suggestions are simply unfiltered for the one tick before it loads. Rebuilds
	// whenever sortedPrompts changes, exactly as before.
	let FuseCtor: any = null;
	const rebuildFuse = async (prompts) => {
		if (!FuseCtor) {
			FuseCtor = (await import('fuse.js')).default;
		}
		fuse = new FuseCtor(prompts, fuseOptions);
	};

	let filteredPrompts = $derived.by(() => {
		if (inputValue.length > 500) {
			return [];
		}

		return inputValue.trim() && fuse
			? fuse.search(inputValue.trim()).map((result) => result.item)
			: sortedPrompts;
	});

	$effect(() => {
		void rebuildFuse(sortedPrompts);
	});
</script>

<div class="mb-1 flex gap-1 text-xs font-medium items-center text-gray-600 dark:text-gray-400">
	{#if filteredPrompts.length > 0}
		<Bolt />
		{$i18n.t('Suggested')}
	{:else}
		<!-- Keine Vorschläge -->

		<div
			class="flex w-full {$settings?.landingPageMode === 'chat'
				? ' -mt-1'
				: 'text-center items-center justify-center'}  self-start text-gray-600 dark:text-gray-400"
		>
			{$WEBUI_NAME} ‧ v{WEBUI_VERSION}
		</div>
	{/if}
</div>

<!-- The 10rem reserve keeps the composer from jumping as suggestions filter down
	 while you type. It only earns that when suggestions exist at all: with none
	 configured it was 160px of dead white space under the version line on every
	 new chat. Keyed off the unfiltered list so filtering to zero results still
	 holds the space. -->
<div class="w-full {sortedPrompts.length > 0 ? 'h-40' : ''}">
	{#if filteredPrompts.length > 0}
		<div role="list" class="max-h-40 overflow-auto scrollbar-none items-start {className}">
			{#each filteredPrompts as prompt, idx (prompt.id || prompt.content)}
				<!-- svelte-ignore a11y_no_interactive_element_to_noninteractive_role -->
				<button
					role="listitem"
					class="waterfall flex flex-col flex-1 shrink-0 w-full justify-between
				       px-3 py-2 rounded-xl border-hairline border-gray-200 dark:border-gray-700
				       hover:border-book-cloth/40 hover:bg-manilla/30 dark:hover:bg-manilla-dark
				       transition-colors duration-200 ease-paper group"
					style="animation-delay: {idx * 60}ms"
					onclick={() => onSelect({ type: 'prompt', data: prompt.content })}
				>
					<div class="flex flex-col text-left">
						{#if prompt.title && prompt.title[0] !== ''}
							<div
								class="font-medium dark:text-gray-300 dark:group-hover:text-gray-200 transition line-clamp-1"
							>
								{prompt.title[0]}
							</div>
							<div class="text-xs text-gray-600 dark:text-gray-400 font-normal line-clamp-1">
								{prompt.title[1]}
							</div>
						{:else}
							<div
								class="font-medium dark:text-gray-300 dark:group-hover:text-gray-200 transition line-clamp-1"
							>
								{prompt.content}
							</div>
							<div class="text-xs text-gray-600 dark:text-gray-400 font-normal line-clamp-1">
								{$i18n.t('Prompt')}
							</div>
						{/if}
					</div>
				</button>
			{/each}
		</div>
	{/if}
</div>

<style>
	/* Waterfall animation for the suggestions */
	@keyframes fadeInUp {
		0% {
			opacity: 0;
			transform: translateY(20px);
		}
		100% {
			opacity: 1;
			transform: translateY(0);
		}
	}

	.waterfall {
		opacity: 0;
		animation-name: fadeInUp;
		animation-duration: 200ms;
		animation-fill-mode: forwards;
		animation-timing-function: ease;
	}
</style>
