<script lang="ts">
	import { getContext } from 'svelte';
	import type { Writable } from 'svelte/store';
	import type { i18n as i18nType } from 'i18next';
	import { toast } from '$lib/utils/toast';

	import { copyToClipboard } from '$lib/utils';
	import {
		cleanExcerpt,
		formatCount,
		parseWebSearchResult,
		truncateMiddle,
		type ParsedWebSearchResult,
		type WebSearchItem
	} from '$lib/utils/toolResults';
	import ResultSkeleton from './ResultSkeleton.svelte';

	const i18n = getContext<Writable<i18nType>>('i18n');

	interface Props {
		id?: string;
		resultRaw?: unknown;
		argsRaw?: unknown;
	}

	let { id = '', resultRaw = '', argsRaw = '' }: Props = $props();

	const PAGE_SIZE = 8;
	const SNIPPET_CHARS = 260;

	let filter = $state('');
	let visibleCount = $state(PAGE_SIZE);
	let parsed: ParsedWebSearchResult | null = $state(null);

	const scheduleParse = () => {
		// Search results are small; parsing them synchronously avoids the slide
		// transition measuring a short skeleton and then growing a second time.
		parsed = parseWebSearchResult(resultRaw, argsRaw);
	};

	const searchResultMatches = (result: WebSearchItem, query: string) => {
		return [result.title, result.url, result.domain, result.snippet]
			.filter(Boolean)
			.some((value) => value.toLowerCase().includes(query));
	};

	const resetVisibleCount = () => {
		visibleCount = PAGE_SIZE;
	};

	const showMore = () => {
		visibleCount += PAGE_SIZE;
	};

	const copy = async (text: string) => {
		if (await copyToClipboard(text)) {
			toast.success($i18n.t('Copied to clipboard'));
		}
	};
	$effect(() => {
		resultRaw;
		argsRaw;
		scheduleParse();
	});
	let normalizedFilter = $derived(filter.trim().toLowerCase());
	let filteredResults = $derived(
		parsed
			? normalizedFilter
				? parsed.results.filter((result) => searchResultMatches(result, normalizedFilter))
				: parsed.results
			: []
	);
	let visibleResults = $derived(filteredResults.slice(0, visibleCount));
</script>

{#if parsed === null}
	<ResultSkeleton lines={4} />
{:else if parsed.ok}
	<div {id}>
		{#if parsed.results.length > 5}
			<div class="mb-1 flex items-center gap-2 px-1">
				<label class="relative min-w-0 flex-1">
					<span class="sr-only">{$i18n.t('Filter results')}</span>
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
						placeholder={$i18n.t('Filter results')}
						bind:value={filter}
						oninput={resetVisibleCount}
					/>
				</label>
				<span class="shrink-0 text-xs tabular-nums text-gray-400 dark:text-gray-500">
					{visibleResults.length}/{parsed.declaredCount ?? filteredResults.length}
				</span>
			</div>
		{/if}

		{#if filteredResults.length === 0}
			<div
				class="rounded-xl border-hairline border-dashed border-gray-200 px-3 py-5 text-center text-xs text-gray-400 dark:border-gray-800 dark:text-gray-500"
			>
				{$i18n.t('No results match your filter.')}
			</div>
		{:else}
			<!-- A reference list, not a stack of cards: hairline rules carry the
			     separation so the panel has exactly one border weight in it. -->
			<ul class="divide-y-hairline divide-gray-200/70 dark:divide-gray-800/80">
				{#each visibleResults as result (result.index)}
					<li
						class="group/row flex gap-2.5 rounded-lg px-1 py-2.5 transition-colors hover:bg-white dark:hover:bg-gray-900/60"
					>
						<span
							class="w-3.5 shrink-0 pt-0.5 text-right text-[11px] tabular-nums text-gray-400 dark:text-gray-600"
							>{result.index}</span
						>

						<div class="min-w-0 flex-1">
							<div class="flex min-w-0 items-start gap-2">
								{#if result.url}
									<a
										class="min-w-0 flex-1 text-sm font-medium leading-snug text-gray-900 underline-offset-2 transition-colors hover:text-book-cloth hover:underline dark:text-gray-100 dark:hover:text-kraft"
										href={result.url}
										target="_blank"
										rel="noreferrer noopener">{result.title}</a
									>
								{:else}
									<span class="min-w-0 flex-1 text-sm font-medium leading-snug text-gray-900 dark:text-gray-100"
										>{result.title}</span
									>
								{/if}

								{#if result.url}
									<button
										class="shrink-0 rounded-md p-1 text-gray-400 opacity-0 transition hover:bg-gray-100 hover:text-gray-700 focus-visible:opacity-100 group-hover/row:opacity-100 dark:text-gray-500 dark:hover:bg-gray-800 dark:hover:text-gray-200 max-md:opacity-100"
										type="button"
										title={$i18n.t('Copy URL')}
										aria-label={$i18n.t('Copy URL')}
										onclick={() => copy(result.url)}
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
												d="M13.19 8.688a4.5 4.5 0 0 1 1.242 7.244l-4.5 4.5a4.5 4.5 0 0 1-6.364-6.364l1.757-1.757m13.35-.622 1.757-1.757a4.5 4.5 0 0 0-6.364-6.364l-4.5 4.5a4.5 4.5 0 0 0 1.242 7.244"
											/>
										</svg>
									</button>
								{/if}
							</div>

							{#if result.url}
								<div class="mt-0.5 truncate text-xs text-gray-400 dark:text-gray-500">
									{result.domain || truncateMiddle(result.url, 72)}
								</div>
							{/if}

							{#if result.snippet}
								<!-- Snippets arrive as raw scrape: markdown links, image refs and
								     `[...]` elision markers. cleanExcerpt strips that noise. -->
								<p class="mt-1.5 line-clamp-3 text-xs leading-relaxed text-gray-500 dark:text-gray-400">
									{cleanExcerpt(result.snippet, SNIPPET_CHARS, result.title)}
								</p>
							{/if}
						</div>
					</li>
				{/each}
			</ul>
		{/if}

		{#if visibleResults.length < filteredResults.length}
			<button
				class="mt-1 w-full rounded-lg py-2 text-xs font-medium text-gray-500 transition hover:bg-gray-100 hover:text-gray-900 dark:text-gray-400 dark:hover:bg-gray-800 dark:hover:text-gray-100"
				type="button"
				data-anchor-on-click
				onclick={showMore}
			>
				{$i18n.t('Show {{COUNT}} more', {
					COUNT: Math.min(PAGE_SIZE, filteredResults.length - visibleResults.length)
				})}
			</button>
		{/if}
	</div>
{:else}
	<div
		class="rounded-xl border-hairline border-warning/25 bg-warning/10 px-3 py-2.5 text-xs text-warning dark:text-warning-dark"
	>
		<div class="font-medium">{$i18n.t('Could not parse web_search results.')}</div>
		<div class="mt-0.5 opacity-80">
			{$i18n.t('Use the Raw tab to inspect the original tool output.')}
		</div>
	</div>
{/if}
