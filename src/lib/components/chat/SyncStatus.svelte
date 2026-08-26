<script lang="ts">
	import { getContext } from 'svelte';
	import { fade } from 'svelte/transition';
	import type { Writable } from 'svelte/store';
	import type { i18n as i18nType } from 'i18next';

	import { chatFreshness } from '$lib/stores';
	import Tooltip from '../common/Tooltip.svelte';

	const i18n = getContext<Writable<i18nType>>('i18n');

	// The freshness mark: a small ink dot + one quiet word, sitting next to the
	// model name. Three moments, one arc:
	//   syncing  — the dot breathes an "ink bloom" (a hairline ring wicking
	//              outward) while we confirm the view is current / pull changes;
	//   offline  — the dot goes hollow and still: showing saved data, the pen
	//              is lifted until the connection returns;
	//   settled  — a component-local afterglow of syncing→fresh: the dot fills
	//              sage and "Up to date" holds for a beat, then the mark
	//              exhales away. That beat is what teaches the vocabulary.
	// Fresh (the 99% steady state) renders nothing at all.
	let shown: 'syncing' | 'offline' | 'settled' | null = $state(null);
	let settleTimer: ReturnType<typeof setTimeout> | null = $state(null);
	let prev = $state('fresh');
	$effect(() => {
		const next = $chatFreshness;
		if (next !== prev) {
			if (settleTimer) {
				clearTimeout(settleTimer);
				settleTimer = null;
			}
			if (next === 'fresh' && prev === 'syncing') {
				shown = 'settled';
				settleTimer = setTimeout(() => {
					shown = null;
					settleTimer = null;
				}, 900);
			} else if (next === 'fresh') {
				shown = null;
			} else {
				shown = next;
			}
			prev = next;
		}
	});

	let label = $derived(
		shown === 'syncing'
			? $i18n.t('Syncing…')
			: shown === 'offline'
				? $i18n.t('Offline')
				: shown === 'settled'
					? $i18n.t('Up to date')
					: ''
	);
	let tip = $derived(
		shown === 'syncing'
			? $i18n.t('Checking for the latest messages')
			: shown === 'offline'
				? $i18n.t('Offline — showing saved data. It refreshes when you reconnect.')
				: $i18n.t('Up to date')
	);
</script>

{#if shown}
	<div class="flex-none self-center mr-1.5" transition:fade={{ duration: 200 }}>
		<Tooltip content={tip}>
			<div class="flex items-center gap-1.5 select-none cursor-default" aria-live="polite">
				<span
					class="sync-dot relative inline-flex size-1.5 rounded-full
						{shown === 'syncing'
						? 'syncing bg-book-cloth/80 text-book-cloth dark:bg-kraft/80 dark:text-kraft'
						: ''}
						{shown === 'offline' ? 'ring-1 ring-inset ring-gray-400 dark:ring-gray-500' : ''}
						{shown === 'settled' ? 'bg-success dark:bg-success-dark' : ''}"
				></span>
				<span class="text-[11px] leading-none text-gray-500 dark:text-gray-400 whitespace-nowrap">
					{label}
				</span>
			</div>
		</Tooltip>
	</div>
{/if}

<style>
	/* The ink bloom: a hairline ring wicks outward from the dot like ink into
	   paper — quiet enough to ignore, alive enough to say "working". */
	.sync-dot.syncing::after {
		content: '';
		position: absolute;
		inset: -1px;
		border-radius: 9999px;
		border: 1px solid currentColor;
		opacity: 0;
		animation: ink-bloom 1.8s ease-out infinite;
	}
	@keyframes ink-bloom {
		0% {
			transform: scale(1);
			opacity: 0.55;
		}
		70% {
			transform: scale(2.4);
			opacity: 0;
		}
		100% {
			transform: scale(2.4);
			opacity: 0;
		}
	}
	@media (prefers-reduced-motion: reduce) {
		.sync-dot.syncing::after {
			animation: none;
		}
	}
</style>
