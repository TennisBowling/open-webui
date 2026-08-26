<script lang="ts">
	import { getContext } from 'svelte';
	import {
		previewFile,
		previewFileSiblings,
		showControls,
		showFilePreview,
		sharedContext
	} from '$lib/stores';
	import { formatFileSize } from '$lib/utils';
	import { WEBUI_API_BASE_URL } from '$lib/constants';
	import { getFileById, getSharedFileById } from '$lib/apis/files';
	import { fileContentUrl } from '$lib/utils/sandbox';
	import Spinner from '$lib/components/common/Spinner.svelte';
	import Image from '$lib/components/common/Image.svelte';
	import Markdown from '$lib/components/chat/Messages/Markdown.svelte';
	import XMark from '$lib/components/icons/XMark.svelte';
	import type { Writable } from 'svelte/store';
	import type { i18n as i18nType } from 'i18next';

	const i18n = getContext<Writable<i18nType>>('i18n');

	let loading = $state(false);
	let item: any = $state(null);

	let shareId = $derived($sharedContext.shareId);
	$effect(() => {
		item = $previewFile;
	});
	let file = $derived(item?.file ?? null);
	let fileId = $derived(item?.id ?? file?.id);
	let name = $derived(item?.name ?? file?.meta?.name ?? file?.filename ?? 'file');
	let size = $derived(item?.size ?? file?.meta?.size);
	let contentType = $derived((file?.meta?.content_type ?? item?.content_type ?? '').toLowerCase());
	let workspaceMeta = $derived(item?.container_workspace ?? file?.data?.container_workspace ?? {});
	let previewFileId = $derived(
		workspaceMeta?.preview_file_id ?? file?.data?.preview_file_id ?? file?.data?.pdf_file_id ?? null
	);
	let previewStatus = $derived(
		workspaceMeta?.preview_status ?? file?.data?.preview_status ?? file?.data?.pdf_status
	);
	let previewError = $derived(
		workspaceMeta?.preview_error ?? file?.data?.preview_error ?? file?.data?.pdf_error
	);
	let textPreview = $derived((file?.data?.content ?? '').trim());
	let isPdf = $derived(contentType === 'application/pdf' || name.toLowerCase().endsWith('.pdf'));
	let isImage = $derived(contentType.startsWith('image/'));
	let isAudio = $derived(contentType.startsWith('audio/'));
	let isMarkdown = $derived(
		/\.(md|markdown|mdown|mkd|mdx)$/i.test(name) ||
			contentType === 'text/markdown' ||
			contentType === 'text/x-markdown'
	);
	let needsDocumentPreview = $derived(
		/\.(docx?|odt|rtf|pptx?|odp|xlsx?|ods)$/i.test(name) ||
			contentType.includes('officedocument') ||
			contentType.includes('msword') ||
			contentType.includes('ms-excel') ||
			contentType.includes('ms-powerpoint') ||
			contentType.includes('opendocument')
	);
	let frameFileId = $derived(previewFileId || (isPdf ? fileId : null));
	let frameUrl = $derived(
		frameFileId ? fileContentUrl(frameFileId, WEBUI_API_BASE_URL, { shareId }) : ''
	);
	let downloadUrl = $derived(
		fileId ? fileContentUrl(fileId, WEBUI_API_BASE_URL, { shareId, attachment: true }) : ''
	);

	const close = () => {
		showFilePreview.set(false);
		previewFile.set(null);
		previewFileSiblings.set([]);
		showControls.set(false);
	};

	const loadFile = async () => {
		// Capture the id we are about to fetch: previewFile can move to another file
		// while this awaits, and we must not apply a stale result to the new file.
		const targetId = fileId;
		if (!targetId) return;
		// Already hold the hydrated record for THIS id → nothing to do. Re-clicking
		// the same card re-sets previewFile to a RAW message.files descriptor with no
		// `.file`, so this is false and we refetch — fixing the "blank on re-open"
		// stick (the descriptor's id string is unchanged, so a fileId-keyed reactive
		// block would never re-run; we key on item identity below instead).
		if (item?.file && item.file.id === targetId) return;
		loading = true;
		const res = await (
			shareId
				? getSharedFileById(localStorage.token ?? '', shareId, targetId)
				: getFileById(localStorage.token, targetId)
		).catch((e) => {
			console.error('Error fetching preview file:', e);
			return null;
		});
		// Apply only if this is the record we asked for AND the panel is still
		// showing that id (the user may have switched files mid-fetch).
		if (res && res.id === targetId && (item?.id ?? item?.file?.id) === targetId) {
			item = { ...item, file: res };
			previewFile.set(item);
		}
		loading = false;
	};

	// Re-run on every item change (identity), not only when the fileId STRING
	// changes: re-clicking the same file hands us a fresh un-hydrated descriptor
	// with the same id, which must still trigger a refetch.
	$effect(() => {
		(item, loadFile());
	});
</script>

<div class="h-full flex flex-col bg-white dark:bg-gray-850 text-gray-800 dark:text-gray-100">
	<div
		class="px-4 py-3 border-b-hairline border-gray-100 dark:border-gray-800 flex items-start justify-between gap-3"
	>
		<div class="min-w-0">
			<div class="font-medium text-base line-clamp-1">{name}</div>
			<div class="flex flex-wrap gap-x-2 gap-y-0.5 text-xs text-gray-500 mt-1">
				{#if size}<span>{formatFileSize(size)}</span>{/if}
				{#if contentType}<span>{contentType}</span>{/if}
			</div>
		</div>
		<div class="flex items-center gap-2 shrink-0">
			{#if downloadUrl}
				<a
					class="px-3 py-1.5 rounded-full text-xs bg-book-cloth hover:bg-kraft text-white transition-colors duration-200 ease-paper"
					href={downloadUrl}
					target="_blank"
					rel="noreferrer"
				>
					{$i18n.t('Download')}
				</a>
			{/if}
			<button
				onclick={close}
				aria-label={$i18n.t('Close')}
				class="self-center p-1 max-md:p-2 rounded-full text-gray-500 hover:text-gray-700 hover:bg-gray-100 dark:text-gray-400 dark:hover:text-gray-200 dark:hover:bg-gray-800 transition"
			>
				<XMark />
			</button>
		</div>
	</div>

	<div class="flex-1 min-h-0 overflow-auto">
		{#if loading && !file}
			<div class="h-full flex items-center justify-center"><Spinner className="size-5" /></div>
		{:else if frameUrl}
			<iframe title={name} src={frameUrl} class="w-full h-full min-h-[70vh] border-0 bg-white"
			></iframe>
		{:else if isImage}
			<div class="p-3 flex justify-center bg-gray-50 dark:bg-gray-900 min-h-full">
				<Image src={fileContentUrl(fileId, WEBUI_API_BASE_URL, { shareId })} alt={name} />
			</div>
		{:else if isAudio}
			<div class="p-4">
				<audio
					src={fileContentUrl(fileId, WEBUI_API_BASE_URL, { shareId })}
					class="w-full"
					controls
					playsinline
				></audio>
			</div>
		{:else if textPreview && isMarkdown}
			<div class="markdown-prose !w-full !max-w-none p-4 text-sm dark:text-gray-100 min-h-full">
				<Markdown
					id={`file-preview-${fileId}`}
					content={textPreview}
					done={true}
					editCodeBlock={false}
					sandboxFiles={$previewFileSiblings}
				/>
			</div>
		{:else if textPreview && !needsDocumentPreview}
			<pre
				class="text-xs whitespace-pre-wrap p-4 bg-gray-50 dark:bg-gray-900 dark:text-gray-100 min-h-full">{textPreview}</pre>
		{:else}
			<div
				class="h-full min-h-80 flex flex-col items-center justify-center gap-2 text-sm text-gray-500 px-6 text-center"
			>
				<div>
					{previewError ||
						(needsDocumentPreview
							? $i18n.t('Document preview was not generated for this file.')
							: $i18n.t('No inline preview is available for this file type.'))}
				</div>
				{#if downloadUrl}<a class="underline" href={downloadUrl} target="_blank" rel="noreferrer"
						>{$i18n.t('Download the file')}</a
					>{/if}
			</div>
		{/if}
	</div>
</div>
