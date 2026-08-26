<script lang="ts">
	import { getContext } from 'svelte';
	import type { Writable } from 'svelte/store';
	import type { i18n as i18nType } from 'i18next';

	import Tooltip from '$lib/components/common/Tooltip.svelte';
	import Switch from '$lib/components/common/Switch.svelte';
	import Knobs from '$lib/components/icons/Knobs.svelte';

	const i18n: Writable<i18nType> = getContext('i18n');

	interface Props {
		name: string;
		tooltip?: string;
		state?: boolean;
		onToggle?: () => void;
		showValves?: boolean;
		onValves?: () => void;
		unauthenticated?: boolean;
		icon?: import('svelte').Snippet;
	}

	let {
		name,
		tooltip = '',
		state = false,
		onToggle = () => {},
		showValves = false,
		onValves = () => {},
		unauthenticated = false,
		icon
	}: Props = $props();
</script>

<Tooltip content={tooltip} placement="top-start">
	<button
		class="flex w-full justify-between gap-2 items-center px-3 py-1.5 max-md:py-2.5 text-sm cursor-pointer rounded-xl hover:bg-gray-50 dark:hover:bg-gray-800/50"
		onclick={onToggle}
	>
		<div class="flex-1 truncate {unauthenticated ? 'opacity-60' : ''}">
			<div class="flex flex-1 gap-2 items-center">
				<div class="shrink-0 size-4 flex justify-center items-center">
					{@render icon?.()}
				</div>

				<div class="truncate">{name}</div>
			</div>
		</div>

		{#if showValves}
			<div class="shrink-0">
				<Tooltip content={$i18n.t('Valves')}>
					<button
						class="self-center w-fit text-sm text-gray-600 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-300 transition rounded-full"
						type="button"
						onclick={(e) => {
							e.stopPropagation();
							e.preventDefault();
							onValves();
						}}
					>
						<Knobs />
					</button>
				</Tooltip>
			</div>
		{/if}

		<div class="shrink-0">
			{#if unauthenticated}
				<span class="text-xs font-medium text-book-cloth dark:text-kraft">{$i18n.t('Connect')}</span
				>
			{:else}
				<div class="pointer-events-none">
					<Switch {state} />
				</div>
			{/if}
		</div>
	</button>
</Tooltip>
