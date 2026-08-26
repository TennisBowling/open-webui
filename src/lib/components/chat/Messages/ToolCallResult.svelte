<script lang="ts">
	import { getContext } from 'svelte';
	import type { Writable } from 'svelte/store';
	import type { i18n as i18nType } from 'i18next';
	import { toast } from '$lib/utils/toast';

	import { copyToClipboard } from '$lib/utils';
	import { decodeToolResultText, formatToolValue, isBrowserToolName } from '$lib/utils/toolResults';
	import { loadLazyBody, peekLazyBody } from '$lib/utils/lazyBlockBodies';
	import SmoothResize from '$lib/components/common/SmoothResize.svelte';
	import WebFetchResult from './ToolResults/WebFetchResult.svelte';
	import WebSearchResult from './ToolResults/WebSearchResult.svelte';
	import BrowserToolResult from './ToolResults/BrowserToolResult.svelte';
	import GenericResult from './ToolResults/GenericResult.svelte';
	import ToolArgs from './ToolResults/ToolArgs.svelte';
	import ResultSkeleton from './ToolResults/ResultSkeleton.svelte';

	const i18n = getContext<Writable<i18nType>>('i18n');

	interface Props {
		id?: string;
		name?: string;
		argsRaw?: unknown;
		resultRaw?: unknown;
		resultLazy?: boolean;
		chatId?: string;
		messageId?: string;
		toolCallId?: string;
		done?: boolean;
		files?: unknown[];
		error?: boolean;
		errorReason?: string;
	}

	let {
		id = '',
		name = '',
		argsRaw = '',
		resultRaw = '',
		resultLazy = false,
		chatId = '',
		messageId = '',
		toolCallId = '',
		done = true,
		files = [],
		error = false,
		errorReason = ''
	}: Props = $props();

	const normalizeLiveResultRaw = (raw: unknown) => {
		if (raw && typeof raw === 'object' && !Array.isArray(raw)) {
			const obj = raw as Record<string, unknown>;
			if ('content' in obj) return obj.content;
			if ('result' in obj) return obj.result;
		}
		return raw;
	};

	type Tab = 'result' | 'request' | 'raw';
	const tabs: Tab[] = ['result', 'request', 'raw'];
	let activeTab: Tab = $state('result');

	// Seed from the shared cache during init so a re-expanded (or hovered, hence
	// prefetched) result is complete in the first frame the block opens — the
	// panel then opens straight to its finished size instead of animating through
	// the skeleton. See `$lib/utils/lazyBlockBodies`.
	const seededResultRaw = resultLazy
		? (() => {
				const cached = peekLazyBody(chatId, messageId, toolCallId);
				return cached === undefined ? null : normalizeLiveResultRaw(cached);
			})()
		: null;

	let fetchedResultRaw: unknown = $state(seededResultRaw);
	let fetchError = $state('');
	let fetchPromise: Promise<void> | null = null;

	const ensureLazyResult = async () => {
		if (
			!resultLazy ||
			fetchedResultRaw !== null ||
			fetchPromise ||
			!chatId ||
			!messageId ||
			!toolCallId
		) {
			return;
		}
		fetchPromise = loadLazyBody(chatId, messageId, toolCallId)
			.then((res) => {
				fetchedResultRaw = normalizeLiveResultRaw(res);
				fetchError = '';
			})
			.catch((err) => {
				fetchError = String(err?.detail ?? err ?? 'Failed to load tool result');
			})
			.finally(() => {
				fetchPromise = null;
			});
		await fetchPromise;
	};

	$effect(() => {
		if (resultLazy && (activeTab === 'result' || activeTab === 'raw')) {
			void ensureLazyResult();
		}
	});

	let normalizedResultRaw = $derived(
		normalizeLiveResultRaw(resultLazy ? (fetchedResultRaw ?? resultRaw) : resultRaw)
	);

	// Avoid eagerly decoding very large web_fetch results just to render the
	// chrome. Decode request/raw text only when that tab is visible; the rich
	// Result tab parses its own data lazily inside the specialized component.
	let resultText = $derived(activeTab === 'raw' ? decodeToolResultText(normalizedResultRaw) : '');

	let awaitingBody = $derived(resultLazy && fetchedResultRaw === null && !fetchError);

	const labelForTab = (tab: Tab) => {
		if (tab === 'result') return $i18n.t('Result');
		if (tab === 'request') return $i18n.t('Request');
		return $i18n.t('Raw');
	};

	const getActiveText = () => {
		if (activeTab === 'request') return formatToolValue(argsRaw);
		if (resultLazy && fetchedResultRaw === null) return '';
		return decodeToolResultText(normalizedResultRaw);
	};

	const copyActive = async () => {
		if (await copyToClipboard(getActiveText())) {
			toast.success($i18n.t('Copied to clipboard'));
		}
	};
</script>

<div
	{id}
	class="my-2 overflow-hidden rounded-2xl border-hairline border-gray-200 bg-gray-50/60 text-sm dark:border-gray-800 dark:bg-gray-850/40"
>
	<!-- Header: quiet text tabs (an underline marks the active one) and a single
	     ghost copy action. A segmented pill here competed with the content. -->
	<div
		class="flex items-center justify-between gap-2 border-b-hairline border-gray-200 px-3 dark:border-gray-800"
	>
		<div class="flex items-center gap-3.5 text-xs">
			{#each tabs as tab (tab)}
				<button
					class="relative py-2 transition-colors {activeTab === tab
						? 'font-medium text-gray-900 after:absolute after:inset-x-0 after:bottom-0 after:h-px after:bg-gray-900 dark:text-gray-100 dark:after:bg-gray-100'
						: 'text-gray-500 hover:text-gray-800 dark:text-gray-500 dark:hover:text-gray-300'}"
					type="button"
					data-anchor-on-click
					onclick={() => (activeTab = tab)}
				>
					{labelForTab(tab)}
				</button>
			{/each}
		</div>

		<button
			class="shrink-0 rounded-lg px-2 py-1 text-xs text-gray-500 transition hover:bg-gray-200/60 hover:text-gray-900 dark:text-gray-500 dark:hover:bg-gray-800 dark:hover:text-gray-100"
			type="button"
			title={$i18n.t('Copy {{TAB}}', { TAB: labelForTab(activeTab) })}
			onclick={copyActive}
		>
			{$i18n.t('Copy')}
		</button>
	</div>

	<!-- The body owns its own height animation: the panel slides open around the
	     skeleton, then grows into the loaded result instead of snapping. -->
	<SmoothResize>
		<div class="p-2.5">
			{#if activeTab === 'result'}
				{#if error}
					<div
						class="mb-2 flex gap-2.5 rounded-xl border-hairline border-error-brick/20 bg-error-brick/5 px-3 py-2.5 text-error-brick dark:bg-error-brick/10 dark:text-error-brick-dark"
					>
						<svg
							xmlns="http://www.w3.org/2000/svg"
							viewBox="0 0 20 20"
							fill="currentColor"
							class="mt-px size-4 shrink-0"
							aria-hidden="true"
						>
							<path
								fill-rule="evenodd"
								d="M10 18a8 8 0 1 0 0-16 8 8 0 0 0 0 16ZM5.5 5.5l9 9-1 1-9-9 1-1Z"
								clip-rule="evenodd"
							/>
						</svg>
						<div class="min-w-0">
							<div class="text-sm font-medium leading-snug">
								{$i18n.t('This tool call returned an error')}
							</div>
							{#if errorReason}
								<div class="mt-0.5 break-words text-xs opacity-80">{errorReason}</div>
							{/if}
						</div>
					</div>
				{/if}

				{#if !done}
					<div
						class="rounded-xl border-hairline border-dashed border-gray-200 px-3 py-5 text-center text-xs text-gray-400 dark:border-gray-800 dark:text-gray-500"
					>
						{$i18n.t('Still running — the result appears here when it finishes.')}
					</div>
				{:else if awaitingBody}
					<ResultSkeleton />
				{:else if fetchError}
					<div
						class="rounded-xl border-hairline border-error-brick/20 bg-error-brick/5 px-3 py-2.5 text-xs text-error-brick dark:bg-error-brick/10 dark:text-error-brick-dark"
					>
						{fetchError}
					</div>
				{:else if name === 'web_search'}
					<WebSearchResult id={`${id}-web-search`} resultRaw={normalizedResultRaw} {argsRaw} />
				{:else if name === 'web_fetch'}
					<WebFetchResult id={`${id}-web-fetch`} resultRaw={normalizedResultRaw} />
				{:else if isBrowserToolName(name)}
					<BrowserToolResult id={`${id}-browser`} resultRaw={normalizedResultRaw} {argsRaw} {files} />
				{:else}
					<GenericResult
						id={`${id}-generic`}
						resultRaw={normalizedResultRaw}
						errored={error}
					/>
				{/if}
			{:else if activeTab === 'request'}
				<ToolArgs id={`${id}-request`} {name} {argsRaw} />
			{:else if awaitingBody}
				<ResultSkeleton />
			{:else}
				<div
					class="max-h-[52vh] overflow-auto rounded-xl border-hairline border-gray-200 bg-white px-3 py-2.5 dark:border-gray-800 dark:bg-gray-950/50"
				>
					<pre
						class="whitespace-pre-wrap break-words font-mono text-xs leading-relaxed text-gray-700 dark:text-gray-300">{resultText}</pre>
				</div>
			{/if}
		</div>
	</SmoothResize>
</div>
