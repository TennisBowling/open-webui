<script lang="ts">
	import { getContext } from 'svelte';
	import { formatFileSize } from '$lib/utils';

	import Spinner from '$lib/components/common/Spinner.svelte';
	import Tooltip from '$lib/components/common/Tooltip.svelte';
	import XMark from '$lib/components/icons/XMark.svelte';
	import VideoCamera from '$lib/components/icons/VideoCamera.svelte';

	const i18n = getContext('i18n');

	interface Props {
		item: any;
		ondismiss?: () => void;
	}

	let { item, ondismiss = () => {} }: Props = $props();

	const processing = $derived(item?.status === 'processing');
	const failed = $derived(item?.status === 'failed');
	const done = $derived(item?.status === 'uploaded');

	const percent = $derived(
		typeof item?.percent === 'number' && item.percent >= 0
			? Math.max(0, Math.min(item.percent, 100))
			: null
	);

	const formatDuration = (s?: number) => {
		if (!s || s <= 0) return '';
		const total = Math.round(s);
		const m = Math.floor(total / 60);
		const sec = total % 60;
		return `${m}:${String(sec).padStart(2, '0')}`;
	};

	// Completed clips describe the result; in-flight ones describe the stage, so
	// the chip always answers "what is happening / what did I get".
	const subtitle = $derived.by(() => {
		if (failed) return item?.error || $i18n.t('Failed');
		if (done) {
			const bits: string[] = [];
			const duration = formatDuration(item?.meta?.duration);
			if (duration) bits.push(duration);
			if (item?.meta?.height) bits.push(`${item.meta.height}p`);
			if (item?.meta?.frames) bits.push($i18n.t('{{n}} frames', { n: item.meta.frames }));
			if (item?.size) bits.push(formatFileSize(item.size));
			return bits.join(' · ');
		}
		const label = item?.stageLabel || $i18n.t('Working');
		return item?.stageDetail ? `${label} · ${item.stageDetail}` : label;
	});
</script>

<div class="relative group">
	<div
		class="h-16 w-72 flex items-center gap-2.5 px-2.5 rounded-2xl text-left bg-white dark:bg-gray-850 border-hairline border-gray-50 dark:border-gray-800"
	>
		<div
			class="shrink-0 size-9 rounded-xl flex items-center justify-center {failed
				? 'bg-red-50 text-red-500 dark:bg-red-900/30 dark:text-red-400'
				: 'bg-gray-100 text-gray-600 dark:bg-gray-800 dark:text-gray-300'}"
		>
			{#if processing}
				<Spinner className="size-4" />
			{:else}
				<VideoCamera className="size-4.5" />
			{/if}
		</div>

		<div class="flex flex-col justify-center -space-y-0.5 min-w-0 w-full">
			<div class="flex items-center gap-1.5">
				<div class="text-sm font-medium dark:text-gray-100 line-clamp-1 flex-1">
					{item?.name || $i18n.t('Video')}
				</div>
				{#if item?.fallbackUsed}
					<Tooltip
						content={$i18n.t('Downloaded via {{source}} after the primary downloader failed.', {
							source: item?.sourceKind || 'fallback'
						})}
					>
						<div
							class="shrink-0 text-[9px] uppercase tracking-wide px-1 py-px rounded bg-gray-100 text-gray-500 dark:bg-gray-800 dark:text-gray-400"
						>
							{$i18n.t('alt')}
						</div>
					</Tooltip>
				{/if}
			</div>

			<div
				class="text-[11px] line-clamp-1 {failed
					? 'text-red-500 dark:text-red-400'
					: 'text-gray-500 dark:text-gray-400'}"
				title={subtitle}
			>
				{subtitle}
			</div>

			{#if processing}
				<div class="pt-1.5">
					<div class="h-1 w-full rounded-full bg-gray-100 dark:bg-gray-800 overflow-hidden">
						{#if percent !== null}
							<div
								class="h-full rounded-full bg-book-cloth transition-[width] duration-300 ease-out"
								style="width: {percent}%"
							></div>
						{:else}
							<!-- Stages without a measurable percentage (resolving,
							     probing, saving) still need to look alive. -->
							<div
								class="h-full w-1/3 rounded-full bg-book-cloth video-progress-indeterminate"
							></div>
						{/if}
					</div>
				</div>
			{/if}
		</div>
	</div>

	<div class=" absolute -top-1 -right-1">
		<button
			class="bg-white text-black border-hairline border-gray-50 rounded-full group-hover:visible invisible transition dark:bg-gray-850 dark:text-white dark:border-gray-800 size-5 flex items-center justify-center"
			type="button"
			aria-label={$i18n.t('Remove video')}
			onclick={(e) => {
				e.stopPropagation();
				ondismiss();
			}}
		>
			<XMark className="size-4" />
		</button>
	</div>
</div>

<style>
	.video-progress-indeterminate {
		animation: video-progress-slide 1.4s ease-in-out infinite;
	}

	@keyframes video-progress-slide {
		0% {
			transform: translateX(-100%);
		}
		100% {
			transform: translateX(300%);
		}
	}

	@media (prefers-reduced-motion: reduce) {
		.video-progress-indeterminate {
			animation: none;
			width: 100%;
			opacity: 0.4;
		}
	}
</style>
