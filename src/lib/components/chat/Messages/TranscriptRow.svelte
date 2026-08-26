<script lang="ts">
	import { getContext } from 'svelte';
	import type { Writable } from 'svelte/store';
	import type { i18n as i18nType } from 'i18next';

	import Spinner from '$lib/components/common/Spinner.svelte';
	import BlockGlyph, { type GlyphName } from './BlockGlyph.svelte';

	const i18n = getContext<Writable<i18nType>>('i18n');

	interface Props {
		icon?: GlyphName;
		label: string;
		// The one thing this row acted on — a query, a path, a command.
		detail?: string;
		detailMono?: boolean;
		// Trailing count/size chip.
		meta?: string;
		metaError?: boolean;
		// Muted trailer that gives way on narrow screens (an error reason, a notice).
		trailing?: string;
		trailingTone?: 'error' | 'warning';
		open?: boolean;
		pending?: boolean;
		errored?: boolean;
	}

	let {
		icon = 'tool',
		label,
		detail = '',
		detailMono = false,
		meta = '',
		metaError = false,
		trailing = '',
		trailingTone = 'error',
		open = false,
		pending = false,
		errored = false
	}: Props = $props();
</script>

<!--
	The transcript's meta line: one per tool call, one per thinking block. It is
	deliberately `w-fit` — a full-width row with the caret pinned to the right
	edge reads as a form control, not as part of the conversation. Everything
	sits on one baseline (glyph · what happened · what it acted on · count) and
	the caret follows the text, so a column of these has a straight left edge
	whatever kind of row it is.
-->
<div
	class="-mx-2 flex w-fit max-w-full items-center gap-2 rounded-lg px-2 py-1 transition-colors duration-150 hover:bg-gray-100/70 dark:hover:bg-gray-850/70"
>
	{#if pending}
		<Spinner className="size-3.5 shrink-0" />
	{:else if errored}
		<svg
			xmlns="http://www.w3.org/2000/svg"
			viewBox="0 0 20 20"
			fill="currentColor"
			class="size-3.5 shrink-0 text-error-brick dark:text-error-brick-dark"
			aria-hidden="true"
		>
			<path
				fill-rule="evenodd"
				d="M10 18a8 8 0 1 0 0-16 8 8 0 0 0 0 16ZM5.5 5.5l9 9-1 1-9-9 1-1Z"
				clip-rule="evenodd"
			/>
		</svg>
	{:else}
		<BlockGlyph {icon} className="size-3.5 shrink-0 text-gray-400 dark:text-gray-500" />
	{/if}

	<span
		class="text-sm {icon === 'reasoning'
			? 'shrink-0 whitespace-nowrap'
			: 'min-w-0 truncate'} {errored
			? 'text-error-brick dark:text-error-brick-dark'
			: pending
				? 'shimmer'
				: 'text-gray-600 dark:text-gray-400'}"
	>
		{label}
		{#if detail}
			<span
				class="{detailMono ? 'font-mono text-[0.8125rem]' : ''} {errored
					? 'opacity-80'
					: pending
						? ''
						: 'text-gray-500 dark:text-gray-500'}">{detail}</span
			>
		{/if}
	</span>

	{#if trailing}
		<span
			class="hidden shrink truncate text-xs sm:inline {trailingTone === 'warning'
				? 'text-warning dark:text-warning-dark'
				: 'text-error-brick/70 dark:text-error-brick-dark/70'}"
			title={trailing}
		>
			{trailing}
		</span>
	{:else if meta}
		<span
			class="shrink-0 text-xs {metaError
				? 'text-error-brick dark:text-error-brick-dark'
				: 'text-gray-400 dark:text-gray-500'}">{meta}</span
		>
	{/if}

	<svg
		xmlns="http://www.w3.org/2000/svg"
		fill="none"
		viewBox="0 0 24 24"
		stroke-width="2.5"
		stroke="currentColor"
		class="size-3 shrink-0 text-gray-400 transition-transform duration-200 ease-paper dark:text-gray-500 {open
			? 'rotate-180'
			: ''}"
		aria-hidden="true"
	>
		<path stroke-linecap="round" stroke-linejoin="round" d="m19.5 8.25-7.5 7.5-7.5-7.5" />
	</svg>

	<span class="sr-only">{open ? $i18n.t('Collapse') : $i18n.t('Expand')}</span>
</div>
