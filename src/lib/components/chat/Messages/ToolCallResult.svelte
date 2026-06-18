<script lang="ts">
	import { getContext } from 'svelte';
	import type { Writable } from 'svelte/store';
	import type { i18n as i18nType } from 'i18next';
	import { toast } from 'svelte-sonner';

	import { copyToClipboard } from '$lib/utils';
	import { decodeToolResultText, formatToolValue, isBrowserToolName } from '$lib/utils/toolResults';
	import { getChatMessageToolResult } from '$lib/apis/chats';
	import WebFetchResult from './ToolResults/WebFetchResult.svelte';
	import WebSearchResult from './ToolResults/WebSearchResult.svelte';
	import BrowserToolResult from './ToolResults/BrowserToolResult.svelte';

	const i18n = getContext<Writable<i18nType>>('i18n');

	export let id = '';
	export let name = '';
	export let argsRaw: unknown = '';
	export let resultRaw: unknown = '';
	export let resultLazy = false;
	export let chatId = '';
	export let messageId = '';
	export let toolCallId = '';
	export let done = true;
	export let files: unknown[] = [];
	export let error = false;
	export let errorReason = '';

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
	let activeTab: Tab = 'result';

	let fetchedResultRaw: unknown = null;
	let fetchError = '';
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
		fetchPromise = getChatMessageToolResult(localStorage.token, chatId, messageId, toolCallId)
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

	$: if (resultLazy && (activeTab === 'result' || activeTab === 'raw')) {
		void ensureLazyResult();
	}

	$: normalizedResultRaw = normalizeLiveResultRaw(
		resultLazy ? (fetchedResultRaw ?? resultRaw) : resultRaw
	);

	// Avoid eagerly decoding very large web_fetch results just to render the
	// chrome. Decode request/raw text only when that tab is visible; the rich
	// Result tab parses its own data lazily inside the specialized component.
	$: requestText = activeTab === 'request' ? formatToolValue(argsRaw) : '';
	$: genericResultText =
		activeTab === 'result' &&
		name !== 'web_search' &&
		name !== 'web_fetch' &&
		!isBrowserToolName(name)
			? decodeToolResultText(normalizedResultRaw)
			: '';
	$: resultText = activeTab === 'raw' ? decodeToolResultText(normalizedResultRaw) : '';

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
	class="my-2 overflow-hidden rounded-2xl border border-gray-100 bg-gray-50/70 text-sm dark:border-gray-800 dark:bg-gray-950/40"
>
	<div
		class="flex flex-col gap-2 border-b border-gray-100 bg-white px-2.5 py-2 dark:border-gray-800 dark:bg-gray-900 sm:flex-row sm:items-center sm:justify-between"
	>
		<div class="flex w-fit rounded-xl bg-gray-100 p-0.5 text-xs dark:bg-gray-800">
			{#each tabs as tab}
				<button
					class="rounded-lg px-2.5 py-1 font-medium transition {activeTab === tab
						? 'bg-white text-gray-900 shadow-xs dark:bg-gray-700 dark:text-white'
						: 'text-gray-500 hover:text-gray-900 dark:text-gray-400 dark:hover:text-white'}"
					type="button"
					on:click={() => (activeTab = tab)}
				>
					{labelForTab(tab)}
				</button>
			{/each}
		</div>

		<button
			class="w-fit rounded-lg px-2 py-1 text-xs font-medium text-gray-500 transition hover:bg-gray-100 hover:text-gray-900 dark:text-gray-400 dark:hover:bg-gray-800 dark:hover:text-white"
			type="button"
			on:click={copyActive}
		>
			{$i18n.t('Copy')}
			{labelForTab(activeTab)}
		</button>
	</div>

	<div class="p-3">
		{#if activeTab === 'result'}
			{#if error}
				<div
					class="mb-2 rounded-xl border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700 dark:border-red-900/50 dark:bg-red-950/30 dark:text-red-300"
				>
					<div class="font-medium">{$i18n.t('This tool call returned an error.')}</div>
					{#if errorReason}
						<div class="mt-1 text-xs opacity-90">{errorReason}</div>
					{/if}
				</div>
			{/if}
			{#if !done}
				<div
					class="rounded-xl border border-gray-100 bg-white px-4 py-3 text-sm text-gray-500 dark:border-gray-800 dark:bg-gray-900 dark:text-gray-400"
				>
					{$i18n.t('The tool is still running. The result will appear here when it finishes.')}
				</div>
			{:else if resultLazy && fetchedResultRaw === null && !fetchError}
				<div
					class="rounded-xl border border-gray-100 bg-white px-4 py-3 text-sm text-gray-500 dark:border-gray-800 dark:bg-gray-900 dark:text-gray-400"
				>
					{$i18n.t('Loading tool result…')}
				</div>
			{:else if fetchError}
				<div
					class="rounded-xl border border-red-100 bg-red-50 px-4 py-3 text-sm text-red-600 dark:border-red-900 dark:bg-red-950/30 dark:text-red-300"
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
				<div
					class="max-h-[48vh] overflow-y-auto rounded-xl bg-white p-3 text-xs text-gray-900 dark:bg-gray-900 dark:text-gray-100"
				>
					<pre class="whitespace-pre-wrap break-words font-mono">{genericResultText || ''}</pre>
				</div>
			{/if}
		{:else if activeTab === 'request'}
			<div class="max-h-[48vh] overflow-y-auto rounded-xl bg-gray-950 p-3 text-xs text-gray-100">
				<pre class="whitespace-pre-wrap break-words font-mono">{requestText || '{}'}</pre>
			</div>
		{:else}
			<div class="max-h-[60vh] overflow-y-auto rounded-xl bg-gray-950 p-3 text-xs text-gray-100">
				<pre class="whitespace-pre-wrap break-words font-mono">{resultText || ''}</pre>
			</div>
		{/if}
	</div>
</div>
