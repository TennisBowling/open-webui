<script lang="ts">
	import { dispatchComponentEvent } from '$lib/utils/componentEvents';
	import { preventDefault } from '$lib/utils/eventModifiers';

	import { getContext, tick } from 'svelte';
	import { formatFileSize } from '$lib/utils';
	import { WEBUI_API_BASE_URL } from '$lib/constants';
	import { config } from '$lib/stores';
	import { toast } from '$lib/utils/toast';

	const i18n = getContext('i18n');
	const dispatch = (type: string, detail?: unknown) =>
		dispatchComponentEvent(eventProps, type, detail);

	import Modal from './Modal.svelte';
	import XMark from '../icons/XMark.svelte';
	import Spinner from './Spinner.svelte';
	import { getFileById, getFileContentById, updateFileProcessingMode } from '$lib/apis/files';

	const EXTRACTABLE_EXTS = new Set([
		'docx',
		'doc',
		'odt',
		'rtf',
		'pptx',
		'ppt',
		'xlsx',
		'xls',
		'html',
		'htm',
		'epub'
	]);

	const getExt = (filename: string) => {
		const dot = (filename || '').toLowerCase().lastIndexOf('.');
		return dot >= 0 ? (filename || '').slice(dot + 1).toLowerCase() : '';
	};

	const TEXT_FILE_EXTS = new Set([
		'txt',
		'md',
		'markdown',
		'rst',
		'csv',
		'tsv',
		'json',
		'jsonl',
		'ndjson',
		'yaml',
		'yml',
		'toml',
		'ini',
		'cfg',
		'conf',
		'env',
		'log',
		'xml',
		'svg',
		'py',
		'pyi',
		'ipynb',
		'js',
		'mjs',
		'cjs',
		'ts',
		'tsx',
		'jsx',
		'vue',
		'svelte',
		'java',
		'kt',
		'kts',
		'scala',
		'groovy',
		'c',
		'cc',
		'cpp',
		'cxx',
		'h',
		'hpp',
		'hxx',
		'rs',
		'go',
		'rb',
		'php',
		'pl',
		'pm',
		'lua',
		'r',
		'jl',
		'dart',
		'swift',
		'm',
		'mm',
		'cs',
		'fs',
		'fsx',
		'ex',
		'exs',
		'erl',
		'hs',
		'ml',
		'mli',
		'clj',
		'cljs',
		'sh',
		'bash',
		'zsh',
		'fish',
		'ps1',
		'bat',
		'cmd',
		'sql',
		'graphql',
		'gql',
		'proto',
		'css',
		'scss',
		'sass',
		'less',
		'tex',
		'bib',
		'srt',
		'vtt',
		'patch',
		'diff',
		'gitignore',
		'dockerignore',
		'editorconfig'
	]);

	const isTextLikeFile = (name: string, contentType: string) => {
		const n = (name || '').toLowerCase();
		if (n.endsWith('.pdf')) return false;
		const dot = n.lastIndexOf('.');
		const ext = dot >= 0 ? n.slice(dot + 1) : n;
		if (ext && TEXT_FILE_EXTS.has(ext)) return true;
		const ct = (contentType || '').toLowerCase();
		return ct.startsWith('text/') && !ct.includes('html');
	};

	interface Props {
		item: any;
		show?: boolean;
		edit?: boolean;
		containerMode?: boolean;
		allowContainer?: boolean;
	}

	let {
		item = $bindable(),
		show = $bindable(false),
		edit = false,
		containerMode = false,
		allowContainer = false,
		...eventProps
	}: Props & Record<string, unknown> = $props();

	let isPDF = $state(false);
	let isAudio = $state(false);
	let loading = $state(false);

	$effect(() => {
		isPDF =
			item?.meta?.content_type === 'application/pdf' ||
			(item?.name && item?.name.toLowerCase().endsWith('.pdf'));
	});

	$effect(() => {
		isAudio =
			(item?.meta?.content_type ?? '').startsWith('audio/') ||
			(item?.name && item?.name.toLowerCase().endsWith('.mp3')) ||
			(item?.name && item?.name.toLowerCase().endsWith('.wav')) ||
			(item?.name && item?.name.toLowerCase().endsWith('.ogg')) ||
			(item?.name && item?.name.toLowerCase().endsWith('.m4a')) ||
			(item?.name && item?.name.toLowerCase().endsWith('.webm'));
	});

	let itemExt = $derived(getExt(item?.name || item?.file?.filename || ''));
	let showModeToggle = $derived(
		item?.type === 'file' &&
			!item?.temporary &&
			!item?.locked &&
			!containerMode &&
			EXTRACTABLE_EXTS.has(itemExt)
	);
	let currentMode = $derived((item?.processing_mode as 'text' | 'pdf') || 'text');
	let pdfConversionAvailable = $derived(
		($config as any)?.features?.pdf_conversion_available ?? true
	);
	let extractedContent = $derived((item?.file?.data?.content ?? '').trim());
	let extractionStatus = $derived(
		item?.file?.data?.status as 'pending' | 'processing' | 'completed' | 'failed' | undefined
	);
	let extractionError = $derived(item?.file?.data?.error as string | undefined);
	let escapedName = $derived(
		(item?.name || item?.file?.filename || 'file')
			.replace(/&/g, '&amp;')
			.replace(/"/g, '&quot;')
			.replace(/</g, '&lt;')
			.replace(/>/g, '&gt;')
	);
	let previewEnvelope = $derived(
		extractedContent ? `<document filename="${escapedName}">\n${extractedContent}\n</document>` : ''
	);

	let previewExpanded = $state(true);

	const handleModeChange = async (mode: 'text' | 'pdf' | 'container') => {
		if (mode === 'container') {
			dispatch('modeChange', { mode: 'container' });
			show = false;
			return;
		}
		if (mode === currentMode) return;
		if (mode === 'pdf' && !pdfConversionAvailable) {
			toast.error(
				$i18n.t('PDF conversion is unavailable on this server. Install LibreOffice to enable it.')
			);
			return;
		}
		if (item) item.processing_mode = mode;
		if (item?.id) {
			try {
				await updateFileProcessingMode(localStorage.token, item.id, mode);
			} catch (e) {
				console.error('Failed to update processing mode:', e);
			}
		}
	};

	const loadContent = async () => {
		if (item?.type === 'file' && !item?.temporary) {
			loading = true;

			const file = await getFileById(localStorage.token, item.id).catch((e) => {
				console.error('Error fetching file:', e);
				return null;
			});

			if (file) {
				item.file = file || {};
			}

			const existing = (item?.file?.data?.content ?? '').trim();
			const name = item?.name || item?.file?.filename || '';
			const ct = item?.meta?.content_type || item?.file?.meta?.content_type || '';
			if (!existing && !isPDF && !isAudio && isTextLikeFile(name, ct)) {
				try {
					const blob = await getFileContentById(item.id);
					const text = blob ? await blob.text() : '';
					if (!item.file) item.file = {};
					if (!item.file.data) item.file.data = {};
					item.file.data.content = text;
				} catch (e) {
					console.error('Error fetching file text content:', e);
				}
			}

			loading = false;
		}

		await tick();
	};

	$effect(() => {
		if (show) {
			loadContent();
		}
	});
</script>

<Modal bind:show size="lg">
	<div class="font-primary px-4.5 py-3.5 w-full flex flex-col justify-center dark:text-gray-400">
		<div class=" pb-2">
			<div class="flex items-start justify-between">
				<div>
					<div class=" font-medium text-lg dark:text-gray-100">
						<a
							href="#"
							class="hover:underline line-clamp-1"
							onclick={preventDefault(() => {
								if (!isPDF && item.url) {
									window.open(
										item.type === 'file' ? `${item.url}/content` : `${item.url}`,
										'_blank'
									);
								}
							})}
						>
							{item?.name ?? 'File'}
						</a>
					</div>
				</div>

				<div>
					<button
						class="text-gray-500 hover:text-gray-700 hover:bg-gray-100 dark:text-gray-400 dark:hover:text-gray-200 dark:hover:bg-gray-850 rounded-full p-1 transition"
						onclick={() => {
							show = false;
						}}
					>
						<XMark />
					</button>
				</div>
			</div>

			<div>
				<div class="flex flex-col items-center md:flex-row gap-1 justify-between w-full">
					<div class=" flex flex-wrap text-xs gap-1 text-gray-500">
						{#if item.size}
							<div class="capitalize shrink-0">{formatFileSize(item.size)}</div>
						{/if}
					</div>
				</div>
			</div>
		</div>

		{#if showModeToggle}
			<div class="pb-3">
				<div class="text-xs text-gray-500 dark:text-gray-400 mb-2">
					{$i18n.t('How should the model read this file?')}
				</div>
				<div class="grid grid-cols-1 {allowContainer ? 'sm:grid-cols-3' : 'sm:grid-cols-2'} gap-2">
					<button
						type="button"
						class="text-left p-3 rounded-xl border-hairline transition
							{currentMode === 'text'
							? 'border-gray-300 dark:border-gray-600 bg-gray-50 dark:bg-gray-850'
							: 'border-gray-200 dark:border-gray-800 hover:border-gray-300 dark:hover:border-gray-700'}"
						onclick={() => handleModeChange('text')}
					>
						<div class="flex items-center gap-2 mb-1">
							<span
								class="size-4 shrink-0 rounded-full border border-gray-300 dark:border-gray-600 flex items-center justify-center"
							>
								{#if currentMode === 'text'}
									<span class="size-2 rounded-full bg-current"></span>
								{/if}
							</span>
							<span class="font-medium text-sm dark:text-gray-100">
								{$i18n.t('Extract text')}
							</span>
						</div>
						<div class="text-xs text-gray-500 dark:text-gray-400 pl-6">
							{$i18n.t('Faster — but loses images, tables, and formatting.')}
						</div>
					</button>

					<button
						type="button"
						class="text-left p-3 rounded-xl border-hairline transition
							{currentMode === 'pdf'
							? 'border-gray-300 dark:border-gray-600 bg-gray-50 dark:bg-gray-850'
							: 'border-gray-200 dark:border-gray-800 hover:border-gray-300 dark:hover:border-gray-700'}
							{pdfConversionAvailable ? '' : 'opacity-50 cursor-not-allowed'}"
						onclick={() => handleModeChange('pdf')}
						disabled={!pdfConversionAvailable}
					>
						<div class="flex items-center gap-2 mb-1">
							<span
								class="size-4 shrink-0 rounded-full border border-gray-300 dark:border-gray-600 flex items-center justify-center"
							>
								{#if currentMode === 'pdf'}
									<span class="size-2 rounded-full bg-current"></span>
								{/if}
							</span>
							<span class="font-medium text-sm dark:text-gray-100">
								{$i18n.t('Convert to PDF')}
							</span>
						</div>
						<div class="text-xs text-gray-500 dark:text-gray-400 pl-6">
							{$i18n.t('Slower — but preserves images, tables, and layout.')}
						</div>
						{#if !pdfConversionAvailable}
							<div class="text-xs text-warning dark:text-warning-dark pl-6 mt-1">
								{$i18n.t('Unavailable: LibreOffice is not installed on the server.')}
							</div>
						{/if}
					</button>

					{#if allowContainer}
						<button
							type="button"
							class="text-left p-3 rounded-xl border-hairline border-gray-200 dark:border-gray-800 hover:border-gray-300 dark:hover:border-gray-700 transition"
							onclick={() => handleModeChange('container')}
						>
							<div class="flex items-center gap-2 mb-1">
								<span
									class="size-4 shrink-0 rounded-full border border-gray-300 dark:border-gray-600"
								></span>
								<span class="font-medium text-sm dark:text-gray-100">
									{$i18n.t('Use container')}
								</span>
							</div>
							<div class="text-xs text-gray-500 dark:text-gray-400 pl-6">
								{$i18n.t(
									'Enable the container tool and read the original file from /workspace/inputs.'
								)}
							</div>
						</button>
					{/if}
				</div>
			</div>
		{/if}

		<div class="max-h-[75vh] overflow-auto">
			{#if !loading}
				{#if isPDF}
					<iframe
						title={item?.name}
						src={item?.temporary && item?.url
							? item.url
							: `${WEBUI_API_BASE_URL}/files/${item.id}/content`}
						class="w-full h-[70vh] border-0 rounded-lg"
					></iframe>
				{:else}
					{#if isAudio}
						<audio
							src={`${WEBUI_API_BASE_URL}/files/${item.id}/content`}
							class="w-full border-0 rounded-lg mb-2"
							controls
							playsinline
						></audio>
					{/if}

					{#if showModeToggle}
						<!-- Show what the model will actually see in Text mode: the
							 exact <document filename="..."> envelope. Useful for
							 debugging "why did the model miss this paragraph?" -->
						<details class="mb-2" bind:open={previewExpanded}>
							<summary
								class="cursor-pointer text-xs text-gray-500 dark:text-gray-400 py-1 select-none"
							>
								{currentMode === 'pdf'
									? $i18n.t('View extracted text (Text mode preview)')
									: $i18n.t('View extracted text')}
							</summary>
							<div class="mt-2">
								{#if extractionStatus === 'processing' || extractionStatus === 'pending'}
									<div
										class="flex items-center gap-2 text-xs text-gray-500 dark:text-gray-400 py-2"
									>
										<Spinner className="size-3.5" />
										{$i18n.t('Extracting…')}
									</div>
								{:else if extractionStatus === 'failed'}
									<div class="text-xs text-error-brick dark:text-error-brick-dark py-2">
										{$i18n.t('Extraction failed')}: {extractionError ?? $i18n.t('unknown error')}
									</div>
								{:else if previewEnvelope}
									<pre
										class="max-h-96 overflow-auto scrollbar-hidden text-xs whitespace-pre-wrap p-2 rounded-lg bg-gray-50 dark:bg-gray-850 font-mono">{previewEnvelope}</pre>
								{:else}
									<div class="text-xs text-gray-500 dark:text-gray-400 py-2">
										{$i18n.t('No content extracted yet.')}
									</div>
								{/if}
							</div>
						</details>
					{:else if item?.file?.data}
						<div class="max-h-96 overflow-scroll scrollbar-hidden text-xs whitespace-pre-wrap">
							{(item?.file?.data?.content ?? '').trim() || 'No content'}
						</div>
					{/if}
				{/if}
			{:else}
				<div class="flex items-center justify-center py-6">
					<Spinner className="size-5" />
				</div>
			{/if}
		</div>
	</div>
</Modal>
