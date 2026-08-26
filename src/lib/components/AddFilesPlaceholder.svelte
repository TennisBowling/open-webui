<script lang="ts">
	import { getContext } from 'svelte';

	interface Props {
		title?: string;
		content?: string;
		children?: import('svelte').Snippet;
	}

	let { title = '', content = '', children }: Props = $props();
	const i18n = getContext('i18n');
</script>

<div class="px-3">
	<div class="text-center dark:text-white text-2xl font-medium z-50">
		{#if title}
			{title}
		{:else}
			{$i18n.t('Add Files')}
		{/if}
	</div>

	{#if children}{@render children()}{:else}<div
			class="px-2 mt-2 text-center text-gray-700 dark:text-gray-200 w-full"
		>
			{#if content}
				{content}
			{:else}
				{$i18n.t('Drop any files here to upload')}
			{/if}
		</div>
	{/if}
</div>
