<script lang="ts">
	import { DropdownMenu } from 'bits-ui';
	import { flyAndScale } from '$lib/utils/transitions';
	import { getContext } from 'svelte';

	import Dropdown from '$lib/components/common/Dropdown.svelte';
	import GarbageBin from '$lib/components/icons/GarbageBin.svelte';
	import Pencil from '$lib/components/icons/Pencil.svelte';
	import Tooltip from '$lib/components/common/Tooltip.svelte';

	const i18n = getContext('i18n');

	interface Props {
		active: boolean;
		editHandler: Function;
		toggleHandler: Function;
		runHandler: Function;
		runsHandler: Function;
		deleteHandler: Function;
		onClose: Function;
		children?: import('svelte').Snippet;
	}

	let {
		active,
		editHandler,
		toggleHandler,
		runHandler,
		runsHandler,
		deleteHandler,
		onClose,
		children
	}: Props = $props();

	let show = $state(false);
</script>

<Dropdown
	bind:show
	onchange={(e) => {
		if (e.detail === false) {
			onClose();
		}
	}}
>
	<Tooltip content={$i18n.t('More')}>
		{@render children?.()}
	</Tooltip>

	{#snippet content()}
		<div>
			<DropdownMenu.Content
				class="w-full max-w-[170px] rounded-2xl px-1 py-1 border-hairline border-gray-100  dark:border-gray-800 z-50 bg-white dark:bg-gray-850 dark:text-white shadow-lg"
				sideOffset={-2}
				side="bottom"
				align="start"
				transition={flyAndScale}
			>
				<DropdownMenu.Item
					class="flex gap-2 items-center px-3 py-1.5 text-sm  cursor-pointer hover:bg-gray-50 dark:hover:bg-gray-800  rounded-xl"
					onclick={() => {
						editHandler();
					}}
				>
					<Pencil strokeWidth="2" />
					<div class="flex items-center">{$i18n.t('Edit')}</div>
				</DropdownMenu.Item>

				<DropdownMenu.Item
					class="flex gap-2 items-center px-3 py-1.5 text-sm  cursor-pointer hover:bg-gray-50 dark:hover:bg-gray-800  rounded-xl"
					onclick={() => {
						toggleHandler();
					}}
				>
					<svg
						xmlns="http://www.w3.org/2000/svg"
						viewBox="0 0 24 24"
						fill="none"
						stroke="currentColor"
						stroke-width="2"
						stroke-linecap="round"
						stroke-linejoin="round"
						class="size-4"
					>
						{#if active}
							<path d="M10 4v16M14 4v16" />
						{:else}
							<path d="M6 4l14 8-14 8V4z" />
						{/if}
					</svg>
					<div class="flex items-center">{active ? $i18n.t('Pause') : $i18n.t('Resume')}</div>
				</DropdownMenu.Item>

				<DropdownMenu.Item
					class="flex gap-2 items-center px-3 py-1.5 text-sm  cursor-pointer hover:bg-gray-50 dark:hover:bg-gray-800  rounded-xl"
					onclick={() => {
						runHandler();
					}}
				>
					<svg
						xmlns="http://www.w3.org/2000/svg"
						viewBox="0 0 24 24"
						fill="none"
						stroke="currentColor"
						stroke-width="2"
						stroke-linecap="round"
						stroke-linejoin="round"
						class="size-4"
					>
						<path d="M4 12a8 8 0 1 1 2.34 5.66" />
						<path d="M4 8v4h4" />
					</svg>
					<div class="flex items-center">{$i18n.t('Run now')}</div>
				</DropdownMenu.Item>

				<DropdownMenu.Item
					class="flex gap-2 items-center px-3 py-1.5 text-sm  cursor-pointer hover:bg-gray-50 dark:hover:bg-gray-800  rounded-xl"
					onclick={() => {
						runsHandler();
					}}
				>
					<svg
						xmlns="http://www.w3.org/2000/svg"
						viewBox="0 0 24 24"
						fill="none"
						stroke="currentColor"
						stroke-width="2"
						stroke-linecap="round"
						stroke-linejoin="round"
						class="size-4"
					>
						<path d="M4 6h16M4 12h16M4 18h10" />
					</svg>
					<div class="flex items-center">{$i18n.t('Run history')}</div>
				</DropdownMenu.Item>

				<DropdownMenu.Item
					class="flex gap-2 items-center px-3 py-1.5 text-sm  cursor-pointer hover:bg-gray-50 dark:hover:bg-gray-800  rounded-xl"
					onclick={() => {
						deleteHandler();
					}}
				>
					<GarbageBin strokeWidth="2" />
					<div class="flex items-center">{$i18n.t('Delete')}</div>
				</DropdownMenu.Item>
			</DropdownMenu.Content>
		</div>
	{/snippet}
</Dropdown>
