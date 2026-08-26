<script lang="ts">
	import { getContext, onMount } from 'svelte';
	import type { Writable } from 'svelte/store';
	import type { i18n as i18nType } from 'i18next';

	import { loadLazyBody, peekLazyBody } from '$lib/utils/lazyBlockBodies';
	import Markdown from './Markdown.svelte';
	import Spinner from '../../common/Spinner.svelte';

	const i18n = getContext<Writable<i18nType>>('i18n');

	// Lazy reasoning body: the server ships closed reasoning blocks as
	// "Thought for N seconds" stubs (content_lazy/content_ref — see backend
	// utils/lazy_blocks.py) and this component fetches the text the moment the
	// user actually expands the block. Mounted by Collapsible only while open,
	// mirroring the ToolCallResult lazy pattern. Fetched text stays component-
	// local ON PURPOSE: writing it back into `content_blocks` would change the

	interface Props {
		// projection signature and could ride a client save back to the server.
		id?: string;
		chatId?: string;
		messageId?: string;
		contentRef?: string;
	}

	let { id = '', chatId = '', messageId = '', contentRef = '' }: Props = $props();

	const bodyText = (res: unknown): string =>
		typeof (res as any)?.content === 'string' ? (res as any).content : '';

	// Seed from the shared cache DURING INIT, not in onMount. A block the user has
	// opened before (or hovered, which prefetches) therefore renders its full text
	// in the very first frame after `open` flips — so `Collapsible`'s slide
	// transition measures the real height once and animates once. Reading it in
	// onMount instead is what produced the stutter: the transition started against
	// a ~20px spinner shell and the body then snapped in on top of it.
	let text: string | null = $state(bodyText(peekLazyBody(chatId, messageId, contentRef)) || null);
	let error = $state('');
	let loading = false;

	const quote = (raw: string) =>
		raw
			.split('\n')
			.map((line) => (line.startsWith('>') ? line : `> ${line}`))
			.join('\n');

	const load = async () => {
		if (loading || text !== null || !chatId || !messageId || !contentRef) return;
		loading = true;
		error = '';
		try {
			text = bodyText(await loadLazyBody(chatId, messageId, contentRef));
		} catch (err) {
			error =
				typeof navigator !== 'undefined' && navigator.onLine === false
					? $i18n.t('Reasoning text is not available offline')
					: String((err as any)?.detail ?? err ?? $i18n.t('Failed to load reasoning'));
		} finally {
			loading = false;
		}
	};

	onMount(() => {
		void load();
	});
</script>

{#if text !== null}
	<Markdown {id} parseImmediately={true} content={quote(text)} />
{:else if error}
	<div class="lazy-body-shell flex items-center gap-2 text-xs text-gray-500 dark:text-gray-400">
		<span>{error}</span>
		<button
			class="underline underline-offset-2 hover:text-gray-700 dark:hover:text-gray-200"
			onclick={() => {
				error = '';
				void load();
			}}
		>
			{$i18n.t('Retry')}
		</button>
	</div>
{:else}
	<!-- Cold open. The shell carries a min-height so the slide animates to a
	     panel-sized box rather than a hairline — one deliberate open, then the
	     text fills it, instead of two visible growth steps. -->
	<div class="lazy-body-shell flex items-center gap-2 text-xs text-gray-500 dark:text-gray-400">
		<Spinner className="size-3" />
		<span>{$i18n.t('Loading...')}</span>
	</div>
{/if}

<style>
	.lazy-body-shell {
		min-height: 4.5rem;
		margin-block: 0.375rem;
	}
</style>
