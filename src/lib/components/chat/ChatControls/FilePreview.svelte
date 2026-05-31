<script lang="ts">
	import { getContext } from 'svelte';
	import { previewFile, showControls, showFilePreview } from '$lib/stores';
	import { formatFileSize } from '$lib/utils';
	import { WEBUI_API_BASE_URL } from '$lib/constants';
	import { getFileById } from '$lib/apis/files';
	import Spinner from '$lib/components/common/Spinner.svelte';
	import Image from '$lib/components/common/Image.svelte';
	import XMark from '$lib/components/icons/XMark.svelte';
	import type { Writable } from 'svelte/store';
	import type { i18n as i18nType } from 'i18next';

	const i18n = getContext<Writable<i18nType>>('i18n');

	let loading = false;
	let loadedId: string | null = null;
	let item: any = null;

	$: item = $previewFile;
	$: file = item?.file ?? null;
	$: fileId = item?.id ?? file?.id;
	$: name = item?.name ?? file?.meta?.name ?? file?.filename ?? 'file';
	$: size = item?.size ?? file?.meta?.size;
	$: contentType = (file?.meta?.content_type ?? item?.content_type ?? '').toLowerCase();
	$: workspaceMeta = item?.container_workspace ?? file?.data?.container_workspace ?? {};
	$: previewFileId =
		workspaceMeta?.preview_file_id ??
		file?.data?.preview_file_id ??
		file?.data?.pdf_file_id ??
		null;
	$: previewStatus = workspaceMeta?.preview_status ?? file?.data?.preview_status ?? file?.data?.pdf_status;
	$: previewError = workspaceMeta?.preview_error ?? file?.data?.preview_error ?? file?.data?.pdf_error;
	$: textPreview = (file?.data?.content ?? '').trim();
	$: isPdf = contentType === 'application/pdf' || name.toLowerCase().endsWith('.pdf');
	$: isImage = contentType.startsWith('image/');
	$: isAudio = contentType.startsWith('audio/');
	$: needsDocumentPreview = /\.(docx?|odt|rtf|pptx?|odp|xlsx?|ods)$/i.test(name) || contentType.includes('officedocument') || contentType.includes('msword') || contentType.includes('ms-excel') || contentType.includes('ms-powerpoint') || contentType.includes('opendocument');
	$: frameFileId = previewFileId || (isPdf ? fileId : null);
	$: frameUrl = frameFileId ? `${WEBUI_API_BASE_URL}/files/${frameFileId}/content` : '';
	$: downloadUrl = fileId ? `${WEBUI_API_BASE_URL}/files/${fileId}/content?attachment=true` : '';

	const close = () => {
		showFilePreview.set(false);
		previewFile.set(null);
		showControls.set(false);
	};

	const loadFile = async () => {
		if (!fileId || loadedId === fileId) return;
		loading = true;
		const res = await getFileById(localStorage.token, fileId).catch((e) => {
			console.error('Error fetching preview file:', e);
			return null;
		});
		if (res) {
			item = { ...item, file: res };
			previewFile.set(item);
			loadedId = fileId;
		}
		loading = false;
	};

	$: if (fileId) {
		loadFile();
	}
</script>

<div class="h-full flex flex-col bg-white dark:bg-gray-850 text-gray-800 dark:text-gray-100">
	<div class="px-4 py-3 border-b border-gray-100 dark:border-gray-800 flex items-start justify-between gap-3">
		<div class="min-w-0">
			<div class="font-medium text-base line-clamp-1">{name}</div>
			<div class="flex flex-wrap gap-x-2 gap-y-0.5 text-xs text-gray-500 mt-1">
				{#if size}<span>{formatFileSize(size)}</span>{/if}
				{#if contentType}<span>{contentType}</span>{/if}
			</div>
		</div>
		<div class="flex items-center gap-2 shrink-0">
			{#if downloadUrl}
				<a class="px-3 py-1.5 rounded-full text-xs bg-gray-900 text-white dark:bg-white dark:text-gray-900" href={downloadUrl} target="_blank" rel="noreferrer">
					{$i18n.t('Download')}
				</a>
			{/if}
			<button on:click={close} aria-label={$i18n.t('Close')}>
				<XMark />
			</button>
		</div>
	</div>

	<div class="flex-1 min-h-0 overflow-auto">
		{#if loading && !file}
			<div class="h-full flex items-center justify-center"><Spinner className="size-5" /></div>
		{:else if frameUrl}
			<iframe title={name} src={frameUrl} class="w-full h-full min-h-[70vh] border-0 bg-white" />
		{:else if isImage}
			<div class="p-3 flex justify-center bg-gray-50 dark:bg-gray-900 min-h-full">
				<Image src={`${WEBUI_API_BASE_URL}/files/${fileId}/content`} alt={name} />
			</div>
		{:else if isAudio}
			<div class="p-4">
				<audio src={`${WEBUI_API_BASE_URL}/files/${fileId}/content`} class="w-full" controls playsinline />
			</div>
		{:else if textPreview && !needsDocumentPreview}
			<pre class="text-xs whitespace-pre-wrap p-4 bg-gray-50 dark:bg-gray-900 dark:text-gray-100 min-h-full">{textPreview}</pre>
		{:else}
			<div class="h-full min-h-80 flex flex-col items-center justify-center gap-2 text-sm text-gray-500 px-6 text-center">
				<div>
					{previewError ||
						(needsDocumentPreview
							? $i18n.t('Document preview was not generated for this file.')
							: $i18n.t('No inline preview is available for this file type.'))}
				</div>
				{#if downloadUrl}<a class="underline" href={downloadUrl} target="_blank" rel="noreferrer">{$i18n.t('Download the file')}</a>{/if}
			</div>
		{/if}
	</div>
</div>
