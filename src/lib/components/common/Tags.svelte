<script lang="ts">
	import { dispatchComponentEvent } from '$lib/utils/componentEvents';
	import TagInput from './Tags/TagInput.svelte';
	import TagList from './Tags/TagList.svelte';
	import { getContext } from 'svelte';
	const dispatch = (type: string, detail?: unknown) =>
		dispatchComponentEvent(eventProps, type, detail);

	const i18n = getContext('i18n');

	let { tags = $bindable([]), ...eventProps } = $props();
</script>

<ul class="flex flex-row flex-wrap gap-[0.3rem] line-clamp-1">
	<TagList
		{tags}
		ondelete={(e) => {
			dispatch('delete', e.detail);
		}}
	/>

	<TagInput
		label={tags.length == 0 ? $i18n.t('Add Tags') : ''}
		onadd={(e) => {
			dispatch('add', e.detail);
		}}
	/>
</ul>
