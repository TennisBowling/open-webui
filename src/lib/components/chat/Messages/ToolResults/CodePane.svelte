<script lang="ts">
	import { getContext } from 'svelte';
	import type { Writable } from 'svelte/store';
	import type { i18n as i18nType } from 'i18next';
	import { toast } from '$lib/utils/toast';

	import { copyToClipboard } from '$lib/utils';

	const i18n = getContext<Writable<i18nType>>('i18n');

	interface Props {
		text?: string;
		label?: string;
		meta?: string;
		copyable?: boolean;
		tone?: 'default' | 'error' | 'success';
		maxHeightClass?: string;
	}

	let {
		text = '',
		label = '',
		meta = '',
		copyable = false,
		tone = 'default',
		maxHeightClass = 'max-h-[44vh]'
	}: Props = $props();

	const copy = async () => {
		if (await copyToClipboard(text)) {
			toast.success($i18n.t('Copied to clipboard'));
		}
	};

	const toneText = {
		default: 'text-gray-700 dark:text-gray-300',
		error: 'text-error-brick dark:text-error-brick-dark',
		success: 'text-gray-700 dark:text-gray-300'
	};
</script>

<div
	class="overflow-hidden rounded-xl border-hairline border-gray-200 bg-white dark:border-gray-800 dark:bg-gray-950/50"
>
	{#if label}
		<div
			class="flex items-center justify-between gap-2 border-b-hairline border-gray-200 px-3 py-1.5 dark:border-gray-800"
		>
			<div class="flex min-w-0 items-baseline gap-2">
				<span
					class="text-[10px] font-medium uppercase tracking-[0.08em] text-gray-400 dark:text-gray-500"
					>{label}</span
				>
				{#if meta}
					<span class="truncate text-[11px] text-gray-400 dark:text-gray-500">{meta}</span>
				{/if}
			</div>
			{#if copyable}
				<button
					class="shrink-0 rounded-md px-1.5 py-0.5 text-[11px] text-gray-400 transition hover:bg-gray-100 hover:text-gray-700 dark:text-gray-500 dark:hover:bg-gray-800 dark:hover:text-gray-200"
					type="button"
					onclick={copy}
				>
					{$i18n.t('Copy')}
				</button>
			{/if}
		</div>
	{/if}

	<div class="overflow-auto {maxHeightClass} px-3 py-2.5">
		<pre
			class="whitespace-pre-wrap break-words font-mono text-xs leading-relaxed {toneText[
				tone
			]}">{text}</pre>
	</div>
</div>
