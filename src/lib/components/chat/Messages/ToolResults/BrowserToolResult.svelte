<script lang="ts">
	import { getContext } from 'svelte';
	import type { Writable } from 'svelte/store';
	import type { i18n as i18nType } from 'i18next';

	import { parseBrowserResult, truncateMiddle, type ParsedBrowserResult } from '$lib/utils/toolResults';
	import Image from '$lib/components/common/Image.svelte';
	import CodePane from './CodePane.svelte';

	const i18n = getContext<Writable<i18nType>>('i18n');

	// Files attached to the browser result. A browser_screenshot carries

	interface Props {
		id?: string;
		resultRaw?: unknown;
		argsRaw?: unknown;
		// `[{ type: 'image', url }]`; we render the first image inline.
		files?: unknown[];
	}

	let { id = '', resultRaw = '', argsRaw = '', files = [] }: Props = $props();

	const SNAPSHOT_PREVIEW_CHARS = 4000;

	let parsed: ParsedBrowserResult | null = $state(null);
	let showFullSnapshot = $state(false);

	$effect(() => {
		// Re-parse whenever the underlying result/args change.
		resultRaw;
		argsRaw;
		parsed = parseBrowserResult(resultRaw, argsRaw);
		showFullSnapshot = false;
	});

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

	let imageUrls = $derived(
		(Array.isArray(files) ? files : []).map(imageUrlFromFile).filter((url): url is string => !!url)
	);

	let snapshotText = $derived(parsed?.snapshot ?? '');
	let snapshotTruncated = $derived(snapshotText.length > SNAPSHOT_PREVIEW_CHARS);
	let visibleSnapshot = $derived(
		snapshotTruncated && !showFullSnapshot
			? snapshotText.slice(0, SNAPSHOT_PREVIEW_CHARS)
			: snapshotText
	);
</script>

<div class="space-y-2" {id}>
	{#if parsed?.url || parsed?.title}
		<div class="min-w-0 px-1">
			{#if parsed.title}
				<div class="break-words text-sm font-medium leading-snug text-gray-900 dark:text-gray-100">
					{parsed.title}
				</div>
			{/if}
			{#if parsed.url}
				<a
					class="mt-0.5 block truncate text-xs text-gray-400 underline-offset-2 transition-colors hover:text-book-cloth hover:underline dark:text-gray-500 dark:hover:text-kraft"
					href={parsed.url}
					target="_blank"
					rel="noreferrer noopener"
				>
					{parsed.domain || truncateMiddle(parsed.url, 72)}
				</a>
			{/if}
		</div>
	{/if}

	{#each imageUrls as url (url)}
		<div
			class="overflow-hidden rounded-xl border-hairline border-gray-200 bg-white dark:border-gray-800 dark:bg-gray-950/50"
		>
			<Image src={url} alt={$i18n.t('Screenshot')} />
		</div>
	{/each}

	{#if snapshotText}
		<div>
			<CodePane text={visibleSnapshot} label={$i18n.t('Page content')} copyable maxHeightClass="max-h-[52vh]" />
			{#if snapshotTruncated}
				<button
					class="mt-1.5 px-1 text-xs font-medium text-gray-500 transition hover:text-gray-900 dark:text-gray-400 dark:hover:text-gray-100"
					type="button"
					data-anchor-on-click
					onclick={() => (showFullSnapshot = !showFullSnapshot)}
				>
					{showFullSnapshot ? $i18n.t('Show less') : $i18n.t('Show full content')}
				</button>
			{/if}
		</div>
	{:else if imageUrls.length === 0}
		<div
			class="rounded-xl border-hairline border-dashed border-gray-200 px-3 py-5 text-center text-xs text-gray-400 dark:border-gray-800 dark:text-gray-500"
		>
			{$i18n.t('No page content returned for this action.')}
		</div>
	{/if}
</div>
