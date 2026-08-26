<script lang="ts">
	import { dispatchComponentEvent } from '$lib/utils/componentEvents';
	const dispatch = (type: string, detail?: unknown) =>
		dispatchComponentEvent(eventProps, type, detail);

	import RecursiveFolder from './RecursiveFolder.svelte';

	interface Props {
		folderRegistry?: any;
		folders?: any;
		shiftKey?: boolean;
		activeChatId?: string | null;
		onDelete?: any;
	}

	let {
		folderRegistry = $bindable({}),
		folders = {},
		shiftKey = false,
		activeChatId = null,
		onDelete = (folderId) => {},
		...eventProps
	}: Props & Record<string, unknown> = $props();

	let folderList = $state([]);
	// Get the list of folders that have no parent, sorted by name alphabetically
	$effect(() => {
		folderList = Object.keys(folders)
			.filter((key) => folders[key].parent_id === null)
			.sort((a, b) =>
				folders[a].name.localeCompare(folders[b].name, undefined, {
					numeric: true,
					sensitivity: 'base'
				})
			);
	});

	const onItemMove = (e) => {
		if (e.originFolderId) {
			folderRegistry[e.originFolderId]?.setFolderItems();
		}
	};
</script>

{#each folderList as folderId (folderId)}
	<RecursiveFolder
		className=""
		bind:folderRegistry
		{folders}
		{folderId}
		{shiftKey}
		{activeChatId}
		{onDelete}
		{onItemMove}
		onactivate={(e) => {
			dispatch('activate', e.detail);
		}}
		onimport={(e) => {
			dispatch('import', e.detail);
		}}
		onupdate={(e) => {
			dispatch('update', e.detail);
		}}
		onchange={(e) => {
			dispatch('change', e.detail);
		}}
	/>
{/each}
