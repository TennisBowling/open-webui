<script lang="ts">
	import { createEventDispatcher, getContext } from 'svelte';
	import type { Writable } from 'svelte/store';
	import type { i18n as i18nType } from 'i18next';
	import { formatFileSize } from '$lib/utils';
	import DocumentPage from '../icons/DocumentPage.svelte';
	import Tooltip from './Tooltip.svelte';
	import {
		previewFile,
		showArtifacts,
		showCallOverlay,
		showControls,
		showEmbeds,
		showFilePreview,
		showOverview
	} from '$lib/stores';

	const i18n = getContext<Writable<i18nType>>('i18n');
	const dispatch = createEventDispatcher();

	export let item: any;
	export let className = 'w-72 min-h-20';

	$: name = item?.name ?? item?.file?.meta?.name ?? item?.file?.filename ?? 'file';
	$: size = item?.size ?? item?.file?.meta?.size;
	$: workspacePath = item?.container_workspace?.workspace_path ?? item?.file?.data?.container_workspace?.workspace_path;
	$: version = item?.container_workspace?.version ?? item?.file?.data?.container_workspace?.version;
</script>

<button
	type="button"
	class="relative group p-3 {className} flex items-center gap-3 bg-white dark:bg-gray-850 border border-gray-100 dark:border-gray-800 rounded-2xl text-left hover:bg-gray-50 dark:hover:bg-gray-800 transition"
	on:click={() => {
		previewFile.set(item);
		showOverview.set(false);
		showArtifacts.set(false);
		showEmbeds.set(false);
		showCallOverlay.set(false);
		showFilePreview.set(true);
		showControls.set(true);
		dispatch('click');
	}}
>
	<div class="size-12 shrink-0 flex justify-center items-center bg-black/10 dark:bg-white/10 rounded-xl text-gray-700 dark:text-gray-200">
		<DocumentPage />
	</div>
	<div class="min-w-0 flex-1">
		<div class="font-medium text-sm dark:text-gray-100 line-clamp-1">{name}</div>
		<div class="text-xs text-gray-500 mt-1 flex flex-wrap gap-x-2 gap-y-0.5">
			<span>{$i18n.t('Generated file')}</span>
			{#if size}<span>{formatFileSize(size)}</span>{/if}
			{#if version}<span>v{version}</span>{/if}
		</div>
		{#if workspacePath}
			<Tooltip content={workspacePath} placement="top-start">
				<div class="text-[11px] text-gray-400 mt-1 line-clamp-1 font-mono">{workspacePath}</div>
			</Tooltip>
		{/if}
	</div>
</button>
