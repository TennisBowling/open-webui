<script lang="ts">
	import { getContext, onDestroy } from 'svelte';
	import type { Writable } from 'svelte/store';
	import type { i18n as i18nType } from 'i18next';
	import { toast } from '$lib/utils/toast';

	import { copyToClipboard } from '$lib/utils';
	import {
		cleanExcerpt,
		formatCharacterCount,
		parseWebFetchResult,
		previewText,
		truncateMiddle,
		type ParsedWebFetchResult,
		type WebFetchPage
	} from '$lib/utils/toolResults';
	import ResultSkeleton from './ResultSkeleton.svelte';

	const i18n = getContext<Writable<i18nType>>('i18n');

	interface Props {
		id?: string;
		resultRaw?: unknown;
	}

	let { id = '', resultRaw = '' }: Props = $props();

	const PREVIEW_CHARS = 420;
	const EAGER_PARSE_MAX_CHARS = 350_000;

	let filter = $state('');
	let expandedPage: number | null = $state(null);
	let parsed: ParsedWebFetchResult | null = $state(null);
	let parseGeneration = 0;
	let parseFrame: number | null = null;
	let parseTimeout: ReturnType<typeof setTimeout> | null = null;
	let parseIdle: number | null = null;

	const clearScheduledParse = () => {
		const idleWindow = typeof window !== 'undefined' ? (window as any) : null;
		if (parseFrame !== null) {
			window.cancelAnimationFrame(parseFrame);
			parseFrame = null;
		}
		if (parseTimeout !== null) {
			clearTimeout(parseTimeout);
			parseTimeout = null;
		}
		if (parseIdle !== null && idleWindow?.cancelIdleCallback) {
			idleWindow.cancelIdleCallback(parseIdle);
			parseIdle = null;
		}
	};

	const estimateResultSize = (raw: unknown) => {
		if (typeof raw === 'string') return raw.length;
		if (raw && typeof raw === 'object' && !Array.isArray(raw)) {
			const obj = raw as Record<string, unknown>;
			if (typeof obj.content === 'string') return obj.content.length;
			if (typeof obj.result === 'string') return obj.result.length;
		}
		try {
			return JSON.stringify(raw ?? '').length;
		} catch {
			return 0;
		}
	};

	const scheduleParse = () => {
		clearScheduledParse();
		parsed = null;
		expandedPage = null;
		const generation = ++parseGeneration;

		if (typeof window === 'undefined' || estimateResultSize(resultRaw) <= EAGER_PARSE_MAX_CHARS) {
			parsed = parseWebFetchResult(resultRaw);
			return;
		}

		// Truly large web_fetch outputs can still be expensive. Keep those on the
		// idle path — the skeleton holds the panel's shape and SmoothResize grows
		// it into the parsed body when it lands.
		parseFrame = window.requestAnimationFrame(() => {
			parseFrame = null;
			const idleWindow = window as any;
			const run = () => {
				parseIdle = null;
				parseTimeout = null;
				if (generation === parseGeneration) {
					parsed = parseWebFetchResult(resultRaw);
				}
			};

			if (typeof idleWindow.requestIdleCallback === 'function') {
				parseIdle = idleWindow.requestIdleCallback(run, { timeout: 450 });
			} else {
				parseTimeout = setTimeout(run, 0);
			}
		});
	};

	$effect(() => {
		resultRaw;
		scheduleParse();
	});

	const pageMatches = (page: WebFetchPage, query: string) => {
		const preview = previewText(page.content, 2200);
		return [page.title, page.url, page.domain, page.published, page.author, page.description, preview]
			.filter(Boolean)
			.some((value) => value.toLowerCase().includes(query));
	};

	const togglePage = (pageIndex: number) => {
		expandedPage = expandedPage === pageIndex ? null : pageIndex;
	};

	const copy = async (text: string) => {
		if (await copyToClipboard(text)) {
			toast.success($i18n.t('Copied to clipboard'));
		}
	};

	let normalizedFilter = $derived(filter.trim().toLowerCase());
	let filteredPages = $derived(
		parsed
			? normalizedFilter
				? parsed.pages.filter((page) => pageMatches(page, normalizedFilter))
				: parsed.pages
			: []
	);

	onDestroy(() => {
		clearScheduledParse();
		parseGeneration += 1;
	});
</script>

{#if parsed === null}
	<ResultSkeleton lines={4} />
{:else if parsed.ok}
	<div {id}>
		{#if parsed.pages.length > 2}
			<div class="mb-1 flex items-center gap-2 px-1">
				<label class="relative min-w-0 flex-1">
					<span class="sr-only">{$i18n.t('Filter fetched pages')}</span>
					<svg
						xmlns="http://www.w3.org/2000/svg"
						fill="none"
						viewBox="0 0 24 24"
						stroke-width="1.75"
						stroke="currentColor"
						class="pointer-events-none absolute left-2.5 top-1/2 size-3.5 -translate-y-1/2 text-gray-400 dark:text-gray-500"
						aria-hidden="true"
					>
						<path
							stroke-linecap="round"
							stroke-linejoin="round"
							d="m21 21-5.197-5.197m0 0A7.5 7.5 0 1 0 5.196 5.196a7.5 7.5 0 0 0 10.607 10.607Z"
						/>
					</svg>
					<input
						class="w-full rounded-lg bg-gray-100/70 py-1.5 pl-8 pr-3 text-xs text-gray-800 outline-hidden transition placeholder:text-gray-400 focus:bg-gray-100 dark:bg-gray-900/70 dark:text-gray-100 dark:placeholder:text-gray-500 dark:focus:bg-gray-900"
						placeholder={$i18n.t('Filter pages')}
						bind:value={filter}
					/>
				</label>
				<span class="shrink-0 text-xs tabular-nums text-gray-400 dark:text-gray-500">
					{filteredPages.length}/{parsed.pages.length}
				</span>
			</div>
		{/if}

		{#if filteredPages.length === 0}
			<div
				class="rounded-xl border-hairline border-dashed border-gray-200 px-3 py-5 text-center text-xs text-gray-400 dark:border-gray-800 dark:text-gray-500"
			>
				{$i18n.t('No fetched pages match your filter.')}
			</div>
		{:else}
			<ul class="divide-y-hairline divide-gray-200/70 dark:divide-gray-800/80">
				{#each filteredPages as page (page.index)}
					<li class="group/row rounded-lg px-1 py-2.5">
						<div class="flex min-w-0 items-start gap-2">
							<div class="min-w-0 flex-1">
								{#if page.url}
									<a
										class="text-sm font-medium leading-snug text-gray-900 underline-offset-2 transition-colors hover:text-book-cloth hover:underline dark:text-gray-100 dark:hover:text-kraft"
										href={page.url}
										target="_blank"
										rel="noreferrer noopener">{page.title}</a
									>
								{:else}
									<span class="text-sm font-medium leading-snug text-gray-900 dark:text-gray-100"
										>{page.title}</span
									>
								{/if}

								<div
									class="mt-0.5 flex flex-wrap items-center gap-x-1.5 gap-y-0.5 text-xs text-gray-400 dark:text-gray-500"
								>
									{#if page.url}
										<span class="truncate">{page.domain || truncateMiddle(page.url, 64)}</span>
										<span aria-hidden="true">·</span>
									{/if}
									<span>{formatCharacterCount(page.characters)}</span>
									{#if page.published}
										<span aria-hidden="true">·</span>
										<span>{page.published}</span>
									{/if}
									{#if page.author}
										<span aria-hidden="true">·</span>
										<span class="truncate">{page.author}</span>
									{/if}
								</div>
							</div>

							<button
								class="shrink-0 rounded-md p-1 text-gray-400 opacity-0 transition hover:bg-gray-100 hover:text-gray-700 focus-visible:opacity-100 group-hover/row:opacity-100 dark:text-gray-500 dark:hover:bg-gray-800 dark:hover:text-gray-200 max-md:opacity-100"
								type="button"
								title={$i18n.t('Copy page')}
								aria-label={$i18n.t('Copy page')}
								onclick={() => copy(page.content)}
							>
								<svg
									xmlns="http://www.w3.org/2000/svg"
									fill="none"
									viewBox="0 0 24 24"
									stroke-width="1.75"
									stroke="currentColor"
									class="size-3.5"
									aria-hidden="true"
								>
									<path
										stroke-linecap="round"
										stroke-linejoin="round"
										d="M15.666 3.888A2.25 2.25 0 0 0 13.5 2.25h-3c-1.03 0-1.9.693-2.166 1.638m7.332 0c.055.194.084.4.084.612a.75.75 0 0 1-.75.75H9a.75.75 0 0 1-.75-.75c0-.212.03-.418.084-.612m7.332 0c.646.049 1.288.11 1.927.184 1.1.128 1.907 1.077 1.907 2.185V19.5a2.25 2.25 0 0 1-2.25 2.25H6.75A2.25 2.25 0 0 1 4.5 19.5V6.257c0-1.108.806-2.057 1.907-2.185a48.208 48.208 0 0 1 1.927-.184"
									/>
								</svg>
							</button>
						</div>

						{#if expandedPage !== page.index}
							{#if page.description || page.content}
								<p class="mt-1.5 line-clamp-3 text-xs leading-relaxed text-gray-500 dark:text-gray-400">
									{cleanExcerpt(page.description || page.content, PREVIEW_CHARS, page.title)}
								</p>
							{/if}
							{#if page.content}
								<button
									class="mt-1.5 flex items-center gap-1 text-xs font-medium text-gray-500 transition hover:text-gray-900 dark:text-gray-400 dark:hover:text-gray-100"
									type="button"
									data-anchor-on-click
									onclick={() => togglePage(page.index)}
								>
									{$i18n.t('Show full page')}
									<svg
										xmlns="http://www.w3.org/2000/svg"
										fill="none"
										viewBox="0 0 24 24"
										stroke-width="2.5"
										stroke="currentColor"
										class="size-3"
										aria-hidden="true"
									>
										<path stroke-linecap="round" stroke-linejoin="round" d="m19.5 8.25-7.5 7.5-7.5-7.5" />
									</svg>
								</button>
							{/if}
						{:else}
							<div
								class="mt-2 overflow-hidden rounded-xl border-hairline border-gray-200 bg-white dark:border-gray-800 dark:bg-gray-950/50"
							>
								<div
									class="max-h-[52vh] overflow-y-auto whitespace-pre-wrap px-3 py-2.5 text-xs leading-relaxed text-gray-700 dark:text-gray-300"
								>
									{page.content || $i18n.t('No content returned for this page.')}
								</div>
							</div>
							<button
								class="mt-1.5 flex items-center gap-1 text-xs font-medium text-gray-500 transition hover:text-gray-900 dark:text-gray-400 dark:hover:text-gray-100"
								type="button"
								data-anchor-on-click
								onclick={() => togglePage(page.index)}
							>
								{$i18n.t('Collapse')}
								<svg
									xmlns="http://www.w3.org/2000/svg"
									fill="none"
									viewBox="0 0 24 24"
									stroke-width="2.5"
									stroke="currentColor"
									class="size-3 rotate-180"
									aria-hidden="true"
								>
									<path stroke-linecap="round" stroke-linejoin="round" d="m19.5 8.25-7.5 7.5-7.5-7.5" />
								</svg>
							</button>
						{/if}
					</li>
				{/each}
			</ul>
		{/if}
	</div>
{:else}
	<div
		class="rounded-xl border-hairline border-warning/25 bg-warning/10 px-3 py-2.5 text-xs text-warning dark:text-warning-dark"
	>
		<div class="font-medium">{$i18n.t('Could not parse web_fetch results.')}</div>
		<div class="mt-0.5 opacity-80">
			{$i18n.t('Use the Raw tab to inspect the original tool output.')}
		</div>
	</div>
{/if}
