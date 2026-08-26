<script lang="ts">
	import { getContext } from 'svelte';
	import type { Writable } from 'svelte/store';
	import type { i18n as i18nType } from 'i18next';

	import Clock from '$lib/components/icons/Clock.svelte';
	import {
		DEFAULT_PEAK_NOTE,
		formatBlocks,
		getPeakHoursConfig,
		getPeakStatus,
		peakClock
	} from '$lib/utils/peakHours';

	const i18n = getContext<Writable<i18nType>>('i18n');

	// A full model object (as found in the `$models` store) — reads
	// `model.info.meta.peak_hours`. Anything without a configured, currently-active

	interface Props {
		// (or about-to-start) window renders nothing.
		model?: any;
		className?: string;
	}

	let { model = null, className = '' }: Props = $props();

	let cfg = $derived(getPeakHoursConfig(model));
	let status = $derived(getPeakStatus(cfg, $peakClock));
	let note = $derived((cfg?.note ?? '').trim() || DEFAULT_PEAK_NOTE);
	let schedule = $derived(cfg ? formatBlocks(cfg.blocks ?? []) : '');

	let tint = $derived(
		status.state === 'active'
			? 'bg-warning/10 border-hairline border-warning/25 text-warning dark:text-warning-dark'
			: 'bg-gray-500/10 border-hairline border-gray-200 dark:border-gray-800 text-gray-600 dark:text-gray-300'
	);

	let label = $derived(
		status.state === 'active'
			? $i18n.t('Peak hours right now')
			: $i18n.t('Peak hours start in {{minutes}} min', {
					minutes: Math.max(1, status.minutesUntilStart ?? 1)
				})
	);
</script>

{#if status.state !== 'none'}
	<div
		class="text-xs rounded-lg inline-flex items-center gap-1.5 px-2.5 py-1 max-w-full {tint} {className}"
		role="status"
		title={note}
	>
		<Clock className="size-3.5 shrink-0" strokeWidth="2" />
		<span class="truncate min-w-0">
			<span class="font-medium">{label}</span>
			{#if schedule}<span class="opacity-70"> · {schedule} {$i18n.t('UTC')}</span>{/if}
			{#if note}<span class="opacity-70"> · {note}</span>{/if}
		</span>
	</div>
{/if}
