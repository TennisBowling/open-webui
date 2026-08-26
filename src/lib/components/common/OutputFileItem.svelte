<script lang="ts">
	import { dispatchComponentEvent } from '$lib/utils/componentEvents';
	import { getContext } from 'svelte';
	import type { Writable } from 'svelte/store';
	import type { i18n as i18nType } from 'i18next';
	import { formatFileSize } from '$lib/utils';
	import DocumentPage from '../icons/DocumentPage.svelte';
	import Tooltip from './Tooltip.svelte';
	import { openFilePreview } from '$lib/stores';

	const i18n = getContext<Writable<i18nType>>('i18n');
	const dispatch = (type: string, detail?: unknown) =>
		dispatchComponentEvent(eventProps, type, detail);

	// Sibling generated files (the owning message's files) so sandbox: links inside

	interface Props {
		item: any;
		className?: string;
		// this file resolve when it is previewed.
		sandboxFiles?: any[];
	}

	let {
		item,
		className = 'w-72 min-h-20',
		sandboxFiles = [],
		...eventProps
	}: Props & Record<string, unknown> = $props();

	let name = $derived(item?.name ?? item?.file?.meta?.name ?? item?.file?.filename ?? 'file');
	let size = $derived(item?.size ?? item?.file?.meta?.size);
	let workspacePath = $derived(
		item?.container_workspace?.workspace_path ??
			item?.file?.data?.container_workspace?.workspace_path
	);
	let version = $derived(
		item?.container_workspace?.version ?? item?.file?.data?.container_workspace?.version
	);
</script>

<button
	type="button"
	class="relative group p-3 {className} flex items-center gap-3 bg-white dark:bg-gray-850 border-hairline border-gray-100 dark:border-gray-800 rounded-2xl text-left hover:bg-gray-50 dark:hover:bg-gray-800 transition"
	onclick={() => {
		openFilePreview(item, sandboxFiles);
		dispatch('click');
	}}
>
	<div
		class="size-12 shrink-0 flex justify-center items-center bg-black/10 dark:bg-white/10 rounded-xl text-gray-700 dark:text-gray-200"
	>
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
