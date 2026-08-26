<script lang="ts">
	import { getContext } from 'svelte';
	import type { Writable } from 'svelte/store';
	import type { i18n as i18nType } from 'i18next';
	import { formatFileSize } from '$lib/utils';
	import { WEBUI_API_BASE_URL } from '$lib/constants';
	import Modal from './Modal.svelte';
	import XMark from '../icons/XMark.svelte';
	import Spinner from './Spinner.svelte';
	import Image from './Image.svelte';
	import { getFileById } from '$lib/apis/files';

	const i18n = getContext<Writable<i18nType>>('i18n');

	interface Props {
		item: any;
		show?: boolean;
	}

	let { item = $bindable(), show = $bindable(false) }: Props = $props();

	let loading = $state(false);

	let file = $derived(item?.file ?? null);
	let fileId = $derived(item?.id ?? file?.id);
	let name = $derived(item?.name ?? file?.meta?.name ?? file?.filename ?? 'file');
	let size = $derived(item?.size ?? file?.meta?.size);
	let contentType = $derived((file?.meta?.content_type ?? item?.content_type ?? '').toLowerCase());
	let workspaceMeta = $derived(item?.container_workspace ?? file?.data?.container_workspace ?? {});
	let previewFileId = $derived(
		workspaceMeta?.preview_file_id ?? file?.data?.preview_file_id ?? null
	);
	let previewStatus = $derived(workspaceMeta?.preview_status ?? file?.data?.preview_status ?? null);
	let previewUrl = $derived(
		previewFileId
			? `${WEBUI_API_BASE_URL}/files/${previewFileId}/content`
			: `${WEBUI_API_BASE_URL}/files/${fileId}/content`
	);
	let downloadUrl = $derived(`${WEBUI_API_BASE_URL}/files/${fileId}/content?attachment=true`);
	let isPdf = $derived(contentType === 'application/pdf' || name.toLowerCase().endsWith('.pdf'));
	let isImage = $derived(contentType.startsWith('image/'));
	let isAudio = $derived(contentType.startsWith('audio/'));
	let textPreview = $derived((file?.data?.content ?? '').trim());
	let canFramePreview = $derived(Boolean(previewFileId) || isPdf);

	const loadFile = async () => {
		const targetId = fileId;
		if (!show || !targetId) return;
		// Refetch whenever we don't already hold the record for THIS id (the item is
		// a raw descriptor without `.file`). Re-opening the same item is self-healing
		// and a failed fetch leaves nothing stale to short-circuit the next attempt.
		if (item?.file && item.file.id === targetId) return;
		loading = true;
		const res = await getFileById(localStorage.token, targetId).catch((e) => {
			console.error('Error fetching output file:', e);
			return null;
		});
		// Apply only if this is the record we asked for and still the shown item
		// (the user may have switched files mid-fetch). Reassign (not mutate) so
		// Svelte reactivity picks up the hydrated record.
		if (res && res.id === targetId && (item?.id ?? item?.file?.id) === targetId) {
			item = { ...item, file: res };
		}
		loading = false;
	};

	// Re-run on show AND on item identity changes (re-opening the same id hands us
	// a fresh un-hydrated descriptor that must still refetch).
	$effect(() => {
		(show, item, loadFile());
	});
</script>

<Modal bind:show size="xl">
	<div class="font-primary px-4.5 py-3.5 w-full flex flex-col dark:text-gray-400">
		<div class="pb-3 flex items-start justify-between gap-3">
			<div class="min-w-0">
				<div class="font-medium text-lg dark:text-gray-100 line-clamp-1">{name}</div>
				<div class="flex gap-2 text-xs text-gray-500 mt-1">
					{#if size}<span>{formatFileSize(size)}</span>{/if}
					{#if contentType}<span>{contentType}</span>{/if}
					{#if previewStatus === 'failed'}<span class="text-warning dark:text-warning-dark"
							>{$i18n.t('Preview unavailable')}</span
						>{/if}
				</div>
			</div>
			<div class="flex items-center gap-2 shrink-0">
				{#if fileId}
					<a
						class="px-3 py-1.5 rounded-full text-sm bg-book-cloth hover:bg-kraft text-white transition-colors duration-200 ease-paper"
						href={downloadUrl}
						target="_blank"
						rel="noreferrer"
					>
						{$i18n.t('Download')}
					</a>
				{/if}
				<button
					class="text-gray-500 hover:text-gray-700 hover:bg-gray-100 dark:text-gray-400 dark:hover:text-gray-200 dark:hover:bg-gray-850 rounded-full p-1 transition"
					onclick={() => (show = false)}
					aria-label={$i18n.t('Close')}
				>
					<XMark />
				</button>
			</div>
		</div>

		<div
			class="min-h-[20rem] max-h-[75vh] overflow-auto rounded-xl border-hairline border-gray-200 dark:border-gray-800"
		>
			{#if loading}
				<div class="h-80 flex items-center justify-center"><Spinner className="size-5" /></div>
			{:else if canFramePreview}
				<iframe title={name} src={previewUrl} class="w-full h-[72vh] border-0 bg-white"></iframe>
			{:else if isImage}
				<div class="p-3 flex justify-center bg-gray-50 dark:bg-gray-900">
					<Image src={`${WEBUI_API_BASE_URL}/files/${fileId}/content`} alt={name} />
				</div>
			{:else if isAudio}
				<div class="p-4">
					<audio
						src={`${WEBUI_API_BASE_URL}/files/${fileId}/content`}
						class="w-full"
						controls
						playsinline
					></audio>
				</div>
			{:else if textPreview}
				<pre
					class="text-xs whitespace-pre-wrap p-4 bg-gray-50 dark:bg-gray-900 dark:text-gray-100 min-h-80">{textPreview}</pre>
			{:else}
				<div
					class="h-80 flex flex-col items-center justify-center gap-2 text-sm text-gray-500 px-6 text-center"
				>
					<div>{$i18n.t('No inline preview is available for this file type.')}</div>
					{#if fileId}
						<a class="underline" href={downloadUrl} target="_blank" rel="noreferrer"
							>{$i18n.t('Download the file')}</a
						>
					{/if}
				</div>
			{/if}
		</div>
	</div>
</Modal>
