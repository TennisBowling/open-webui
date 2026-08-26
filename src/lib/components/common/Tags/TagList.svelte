<script lang="ts">
	import { dispatchComponentEvent } from '$lib/utils/componentEvents';
	import { getContext } from 'svelte';
	const i18n = getContext('i18n');

	import TagItem from './TagItem.svelte';
	const dispatch = (type: string, detail?: unknown) =>
		dispatchComponentEvent(eventProps, type, detail);

	let { tags = [], ...eventProps } = $props();
</script>

{#each tags as tag}
	<TagItem
		{tag}
		onDelete={() => {
			dispatch('delete', tag.name);
		}}
	/>
{/each}
