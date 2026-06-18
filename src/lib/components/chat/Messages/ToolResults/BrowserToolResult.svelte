<script lang="ts">
	import { getContext } from 'svelte';
	import type { Writable } from 'svelte/store';
	import type { i18n as i18nType } from 'i18next';
	import { toast } from 'svelte-sonner';

	import { copyToClipboard } from '$lib/utils';
	import { parseBrowserResult, type ParsedBrowserResult } from '$lib/utils/toolResults';
	import Image from '$lib/components/common/Image.svelte';

	const i18n = getContext<Writable<i18nType>>('i18n');

	export let id = '';
	export let resultRaw: unknown = '';
	export let argsRaw: unknown = '';
	// Files attached to the browser result. A browser_screenshot carries
	// `[{ type: 'image', url }]`; we render the first image inline.
	export let files: unknown[] = [];

	const SNAPSHOT_PREVIEW_CHARS = 4000;

	let parsed: ParsedBrowserResult | null = null;
	let showFullSnapshot = false;

	$: {
		// Re-parse whenever the underlying result/args change.
		resultRaw;
		argsRaw;
		parsed = parseBrowserResult(resultRaw, argsRaw);
		showFullSnapshot = false;
	}

	const imageUrlFromFile = (file: unknown): string => {
		if (typeof file === 'string') {
			return file.startsWith('data:image/') ? file : '';
		}
		if (file && typeof file === 'object') {
			const f = file as Record<string, unknown>;
			if (f.type === 'image' && typeof f.url === 'string') return f.url;
		}
		return '';
	};

	$: imageUrls = (Array.isArray(files) ? files : [])
		.map(imageUrlFromFile)
		.filter((url): url is string => !!url);

	$: snapshotText = parsed?.snapshot ?? '';
	$: snapshotTruncated = snapshotText.length > SNAPSHOT_PREVIEW_CHARS;
	$: visibleSnapshot =
		snapshotTruncated && !showFullSnapshot
			? snapshotText.slice(0, SNAPSHOT_PREVIEW_CHARS)
			: snapshotText;

	const copy = async (text: string) => {
		if (await copyToClipboard(text)) {
			toast.success($i18n.t('Copied to clipboard'));
		}
	};
</script>

<div class="space-y-3" {id}>
	{#if parsed?.url || parsed?.title}
		<div class="flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between">
			<div class="min-w-0">
				{#if parsed.title}
					<div class="break-words text-sm font-semibold text-gray-900 dark:text-gray-100">
						{parsed.title}
					</div>
				{/if}
				{#if parsed.url}
					<div class="mt-0.5 break-all text-xs text-gray-500 dark:text-gray-500">
						{parsed.domain || parsed.url}
					</div>
				{/if}
			</div>

			{#if parsed.url}
				<div class="flex shrink-0 flex-wrap gap-1.5">
					<a
						class="rounded-lg border border-gray-100 px-2 py-1 text-xs font-medium text-gray-600 transition hover:bg-gray-100 hover:text-gray-900 dark:border-gray-800 dark:text-gray-300 dark:hover:bg-gray-800 dark:hover:text-white"
						href={parsed.url}
						target="_blank"
						rel="noreferrer noopener"
					>
						{$i18n.t('Open')}
					</a>
					<button
						class="rounded-lg border border-gray-100 px-2 py-1 text-xs font-medium text-gray-600 transition hover:bg-gray-100 hover:text-gray-900 dark:border-gray-800 dark:text-gray-300 dark:hover:bg-gray-800 dark:hover:text-white"
						type="button"
						on:click={() => copy(parsed?.url ?? '')}
					>
						{$i18n.t('Copy URL')}
					</button>
				</div>
			{/if}
		</div>
	{/if}

	{#if imageUrls.length > 0}
		<div class="space-y-2">
			{#each imageUrls as url}
				<div
					class="overflow-hidden rounded-2xl border border-gray-100 bg-white dark:border-gray-800 dark:bg-gray-900"
				>
					<Image src={url} alt={$i18n.t('Screenshot')} />
				</div>
			{/each}
		</div>
	{/if}

	{#if snapshotText}
		<div
			class="overflow-hidden rounded-2xl border border-gray-100 bg-gray-50 dark:border-gray-800 dark:bg-gray-950/40"
		>
			<div
				class="flex items-center justify-between gap-2 border-b border-gray-100 bg-white px-3 py-2 text-xs dark:border-gray-800 dark:bg-gray-900"
			>
				<span class="font-medium text-gray-700 dark:text-gray-300">{$i18n.t('Page content')}</span>
				<button
					class="text-gray-500 transition hover:text-gray-900 dark:text-gray-400 dark:hover:text-white"
					type="button"
					on:click={() => copy(snapshotText)}
				>
					{$i18n.t('Copy')}
				</button>
			</div>
			<div
				class="max-h-[60vh] overflow-y-auto px-3 py-3 text-xs leading-relaxed text-gray-800 dark:text-gray-200"
			>
				<pre class="whitespace-pre-wrap break-words font-mono">{visibleSnapshot}</pre>
				{#if snapshotTruncated}
					<button
						class="mt-2 text-xs font-medium text-gray-600 underline decoration-gray-300 underline-offset-2 transition hover:text-gray-900 dark:text-gray-300 dark:decoration-gray-700 dark:hover:text-white"
						type="button"
						on:click={() => (showFullSnapshot = !showFullSnapshot)}
					>
						{showFullSnapshot ? $i18n.t('Show less') : $i18n.t('Show full content')}
					</button>
				{/if}
			</div>
		</div>
	{:else if imageUrls.length === 0}
		<div
			class="rounded-2xl border border-dashed border-gray-200 bg-gray-50 px-4 py-6 text-center text-sm text-gray-500 dark:border-gray-800 dark:bg-gray-900 dark:text-gray-400"
		>
			{$i18n.t('No page content returned for this action.')}
		</div>
	{/if}
</div>
