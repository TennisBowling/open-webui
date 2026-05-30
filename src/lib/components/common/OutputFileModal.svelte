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

	export let item: any;
	export let show = false;

	let loading = false;
	let loadedId: string | null = null;

	$: file = item?.file ?? null;
	$: fileId = item?.id ?? file?.id;
	$: name = item?.name ?? file?.meta?.name ?? file?.filename ?? 'file';
	$: size = item?.size ?? file?.meta?.size;
	$: contentType = (file?.meta?.content_type ?? item?.content_type ?? '').toLowerCase();
	$: workspaceMeta = item?.container_workspace ?? file?.data?.container_workspace ?? {};
	$: previewFileId = workspaceMeta?.preview_file_id ?? file?.data?.preview_file_id ?? null;
	$: previewStatus = workspaceMeta?.preview_status ?? file?.data?.preview_status ?? null;
	$: previewUrl = previewFileId
		? `${WEBUI_API_BASE_URL}/files/${previewFileId}/content`
		: `${WEBUI_API_BASE_URL}/files/${fileId}/content`;
	$: downloadUrl = `${WEBUI_API_BASE_URL}/files/${fileId}/content?attachment=true`;
	$: isPdf = contentType === 'application/pdf' || name.toLowerCase().endsWith('.pdf');
	$: isImage = contentType.startsWith('image/');
	$: isAudio = contentType.startsWith('audio/');
	$: textPreview = (file?.data?.content ?? '').trim();
	$: canFramePreview = Boolean(previewFileId) || isPdf;

	const loadFile = async () => {
		if (!show || !fileId || loadedId === fileId) return;
		loading = true;
		const res = await getFileById(localStorage.token, fileId).catch((e) => {
			console.error('Error fetching output file:', e);
			return null;
		});
		if (res) {
			item.file = res;
			loadedId = fileId;
		}
		loading = false;
	};

	$: if (show) {
		loadFile();
	}
</script>

<Modal bind:show size="xl">
	<div class="font-primary px-4.5 py-3.5 w-full flex flex-col dark:text-gray-400">
		<div class="pb-3 flex items-start justify-between gap-3">
			<div class="min-w-0">
				<div class="font-medium text-lg dark:text-gray-100 line-clamp-1">{name}</div>
				<div class="flex gap-2 text-xs text-gray-500 mt-1">
					{#if size}<span>{formatFileSize(size)}</span>{/if}
					{#if contentType}<span>{contentType}</span>{/if}
					{#if previewStatus === 'failed'}<span class="text-amber-600">{$i18n.t('Preview unavailable')}</span>{/if}
				</div>
			</div>
			<div class="flex items-center gap-2 shrink-0">
				{#if fileId}
					<a
						class="px-3 py-1.5 rounded-full text-sm bg-gray-900 text-white dark:bg-white dark:text-gray-900"
						href={downloadUrl}
						target="_blank"
						rel="noreferrer"
					>
						{$i18n.t('Download')}
					</a>
				{/if}
				<button on:click={() => (show = false)} aria-label={$i18n.t('Close')}>
					<XMark />
				</button>
			</div>
		</div>

		<div class="min-h-[20rem] max-h-[75vh] overflow-auto rounded-xl border border-gray-100 dark:border-gray-800">
			{#if loading}
				<div class="h-80 flex items-center justify-center"><Spinner className="size-5" /></div>
			{:else if canFramePreview}
				<iframe title={name} src={previewUrl} class="w-full h-[72vh] border-0 bg-white" />
			{:else if isImage}
				<div class="p-3 flex justify-center bg-gray-50 dark:bg-gray-900">
					<Image src={`${WEBUI_API_BASE_URL}/files/${fileId}/content`} alt={name} />
				</div>
			{:else if isAudio}
				<div class="p-4">
					<audio src={`${WEBUI_API_BASE_URL}/files/${fileId}/content`} class="w-full" controls playsinline />
				</div>
			{:else if textPreview}
				<pre class="text-xs whitespace-pre-wrap p-4 bg-gray-50 dark:bg-gray-900 dark:text-gray-100 min-h-80">{textPreview}</pre>
			{:else}
				<div class="h-80 flex flex-col items-center justify-center gap-2 text-sm text-gray-500 px-6 text-center">
					<div>{$i18n.t('No inline preview is available for this file type.')}</div>
					{#if fileId}
						<a class="underline" href={downloadUrl} target="_blank" rel="noreferrer">{$i18n.t('Download the file')}</a>
					{/if}
				</div>
			{/if}
		</div>
	</div>
</Modal>
