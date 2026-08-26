<script lang="ts">
	import { getContext } from 'svelte';
	import type { Writable } from 'svelte/store';
	import type { i18n as i18nType } from 'i18next';

	import Switch from '$lib/components/common/Switch.svelte';
	import XMark from '$lib/components/icons/XMark.svelte';
	import Tooltip from '$lib/components/common/Tooltip.svelte';
	import {
		DEFAULT_PEAK_NOTE,
		formatBlocks,
		isValidBlock,
		type PeakBlock
	} from '$lib/utils/peakHours';

	const i18n = getContext<Writable<i18nType>>('i18n');

	interface Props {
		enabled?: boolean;
		blocks?: PeakBlock[];
		note?: string;
	}

	let {
		enabled = $bindable(false),
		blocks = $bindable([]),
		note = $bindable('')
	}: Props = $props();

	let schedulePreview = $derived(formatBlocks(blocks));
	let hasInvalidBlock = $derived(blocks.some((b) => !isValidBlock(b)));

	const refresh = () => {
		// Reassign so the schedule preview / validation recompute after an edit.
		blocks = blocks;
	};

	const addBlock = () => {
		blocks = [...blocks, { start: '01:00', end: '04:00' }];
	};

	const removeBlock = (index: number) => {
		blocks = blocks.filter((_, i) => i !== index);
	};

	const onToggle = () => {
		// First time on: seed a concrete starter window and the default note so the
		// admin has something editable instead of an empty form.
		if (enabled) {
			if (blocks.length === 0) {
				blocks = [{ start: '01:00', end: '04:00' }];
			}
			if ((note ?? '').trim() === '') {
				note = DEFAULT_PEAK_NOTE;
			}
		}
	};
</script>

<div class="px-4 py-3 bg-gray-50 dark:bg-gray-950 rounded-2xl">
	<div class="flex w-full justify-between items-center">
		<div class="self-center text-sm font-semibold">{$i18n.t('Peak Hours')}</div>
		<div class="pr-2">
			<Switch bind:state={enabled} onchange={onToggle} />
		</div>
	</div>
	<div class="mt-1 text-xs text-gray-500 dark:text-gray-500">
		{$i18n.t(
			'Shows users a soft heads-up while this model is in high-demand hours. All times are in UTC.'
		)}
	</div>

	{#if enabled}
		<div class="mt-3">
			<div class="text-xs font-semibold mb-1">{$i18n.t('Time blocks (UTC)')}</div>

			{#if blocks.length === 0}
				<div class="text-xs text-gray-400 dark:text-gray-600 mb-2">
					{$i18n.t('No time blocks yet. Add one below.')}
				</div>
			{/if}

			<div class="flex flex-col gap-2">
				{#each blocks as block, index}
					<div class="flex items-center gap-2">
						<input
							type="time"
							aria-label={$i18n.t('Start time (UTC)')}
							bind:value={block.start}
							onchange={refresh}
							class="text-sm bg-transparent outline-hidden border border-gray-100 dark:border-gray-800 rounded-lg px-2.5 py-1.5 {isValidBlock(
								block
							)
								? ''
								: 'border-error-brick/50 dark:border-error-brick/50'}"
						/>
						<span class="text-xs text-gray-400">{$i18n.t('to')}</span>
						<input
							type="time"
							aria-label={$i18n.t('End time (UTC)')}
							bind:value={block.end}
							onchange={refresh}
							class="text-sm bg-transparent outline-hidden border border-gray-100 dark:border-gray-800 rounded-lg px-2.5 py-1.5 {isValidBlock(
								block
							)
								? ''
								: 'border-error-brick/50 dark:border-error-brick/50'}"
						/>

						{#if !isValidBlock(block)}
							<Tooltip content={$i18n.t('Start and end must be different times.')}>
								<span class="text-xs text-error-brick dark:text-error-brick-dark"
									>{$i18n.t('Invalid')}</span
								>
							</Tooltip>
						{/if}

						<button
							type="button"
							class="ml-auto p-1 text-gray-400 hover:text-gray-700 dark:hover:text-gray-200 transition"
							aria-label={$i18n.t('Remove time block')}
							onclick={() => removeBlock(index)}
						>
							<XMark className="size-4" />
						</button>
					</div>
				{/each}
			</div>

			<button
				type="button"
				class="mt-2 flex items-center gap-1 text-xs text-gray-600 dark:text-gray-300 hover:text-gray-900 dark:hover:text-white transition"
				onclick={addBlock}
			>
				<svg
					xmlns="http://www.w3.org/2000/svg"
					viewBox="0 0 20 20"
					fill="currentColor"
					class="size-4"
				>
					<path
						d="M10.75 4.75a.75.75 0 0 0-1.5 0v4.5h-4.5a.75.75 0 0 0 0 1.5h4.5v4.5a.75.75 0 0 0 1.5 0v-4.5h4.5a.75.75 0 0 0 0-1.5h-4.5v-4.5Z"
					/>
				</svg>
				{$i18n.t('Add time block')}
			</button>

			{#if schedulePreview}
				<div class="mt-3 text-xs text-gray-500 dark:text-gray-500">
					{$i18n.t('Peak')}: {schedulePreview}
					{$i18n.t('UTC')}
				</div>
			{:else if hasInvalidBlock}
				<div class="mt-3 text-xs text-error-brick dark:text-error-brick-dark">
					{$i18n.t('Fix the highlighted time blocks to enable the notice.')}
				</div>
			{/if}

			<div class="mt-3">
				<div class="text-xs font-semibold mb-1">{$i18n.t('Note (optional)')}</div>
				<div class="text-xs text-gray-500 dark:text-gray-500 mb-2">
					{$i18n.t('Shown to users during peak hours. Leave blank to use the default.')}
				</div>
				<input
					type="text"
					bind:value={note}
					placeholder={DEFAULT_PEAK_NOTE}
					class="text-sm w-full bg-transparent outline-hidden border-hairline border-gray-100 dark:border-gray-800 rounded-lg px-3 py-2"
				/>
			</div>
		</div>
	{/if}
</div>
