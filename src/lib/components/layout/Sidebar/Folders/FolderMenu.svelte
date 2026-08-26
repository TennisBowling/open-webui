<script lang="ts">
	import { dispatchComponentEvent } from '$lib/utils/componentEvents';
	import { DropdownMenu } from 'bits-ui';
	import { flyAndScale } from '$lib/utils/transitions';
	import { getContext } from 'svelte';

	const i18n = getContext('i18n');
	const dispatch = (type: string, detail?: unknown) =>
		dispatchComponentEvent(eventProps, type, detail);

	import Dropdown from '$lib/components/common/Dropdown.svelte';
	import GarbageBin from '$lib/components/icons/GarbageBin.svelte';
	import Pencil from '$lib/components/icons/Pencil.svelte';
	import Tooltip from '$lib/components/common/Tooltip.svelte';
	import Download from '$lib/components/icons/Download.svelte';

	interface Props {
		align?: 'start' | 'end';
		onEdit?: any;
		onExport?: any;
		onDelete?: any;
		children?: import('svelte').Snippet;
	}

	let {
		align = 'start',
		onEdit = () => {},
		onExport = () => {},
		onDelete = () => {},
		children,
		...eventProps
	}: Props & Record<string, unknown> = $props();

	let show = $state(false);
</script>

<Dropdown
	bind:show
	onchange={(e) => {
		if (e.detail === false) {
			dispatch('close');
		}
	}}
>
	<Tooltip content={$i18n.t('More')}>
		<button
			onclick={(e) => {
				e.stopPropagation();
				show = !show;
			}}
		>
			{@render children?.()}
		</button>
	</Tooltip>

	{#snippet content()}
		<div>
			<DropdownMenu.Content
				class="w-full max-w-[170px] rounded-2xl px-1 py-1 border-hairline border-gray-200 dark:border-gray-800   z-50 bg-white dark:bg-gray-850 dark:text-white shadow-lg"
				sideOffset={-2}
				side="bottom"
				{align}
				transition={flyAndScale}
			>
				<DropdownMenu.Item
					class="flex gap-2 items-center px-3 py-1.5 text-sm  cursor-pointer hover:bg-gray-50 dark:hover:bg-gray-800 rounded-xl"
					onclick={() => {
						onEdit();
					}}
				>
					<Pencil />
					<div class="flex items-center">{$i18n.t('Edit')}</div>
				</DropdownMenu.Item>

				<DropdownMenu.Item
					class="flex gap-2 items-center px-3 py-1.5 text-sm  cursor-pointer hover:bg-gray-50 dark:hover:bg-gray-800 rounded-xl"
					onclick={() => {
						onExport();
					}}
				>
					<Download />

					<div class="flex items-center">{$i18n.t('Export')}</div>
				</DropdownMenu.Item>

				<DropdownMenu.Item
					class="flex  gap-2  items-center px-3 py-1.5 text-sm  cursor-pointer hover:bg-gray-50 dark:hover:bg-gray-800 rounded-xl"
					onclick={() => {
						onDelete();
					}}
				>
					<GarbageBin />
					<div class="flex items-center">{$i18n.t('Delete')}</div>
				</DropdownMenu.Item>
			</DropdownMenu.Content>
		</div>
	{/snippet}
</Dropdown>
