<script lang="ts">
	import { DropdownMenu } from 'bits-ui';
	import { flyAndScale } from '$lib/utils/transitions';
	import { getContext } from 'svelte';

	import Dropdown from '$lib/components/common/Dropdown.svelte';
	import GarbageBin from '$lib/components/icons/GarbageBin.svelte';
	import Tooltip from '$lib/components/common/Tooltip.svelte';
	import Share from '$lib/components/icons/Share.svelte';
	import DocumentDuplicate from '$lib/components/icons/DocumentDuplicate.svelte';
	import Download from '$lib/components/icons/Download.svelte';
	import Switch from '$lib/components/common/Switch.svelte';
	import GlobeAlt from '$lib/components/icons/GlobeAlt.svelte';

	const i18n = getContext('i18n');

	interface Props {
		func: any;
		editHandler: Function;
		shareHandler: Function;
		cloneHandler: Function;
		exportHandler: Function;
		deleteHandler: Function;
		toggleGlobalHandler: Function;
		onClose: Function;
		children?: import('svelte').Snippet;
	}

	let {
		func = $bindable(),
		editHandler,
		shareHandler,
		cloneHandler,
		exportHandler,
		deleteHandler,
		toggleGlobalHandler,
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
				class="w-full max-w-[180px] rounded-xl p-1 border-hairline border-gray-100  dark:border-gray-800 z-50 bg-white dark:bg-gray-850 dark:text-white shadow-lg"
				sideOffset={-2}
				side="bottom"
				align="start"
				transition={flyAndScale}
			>
				{#if ['filter', 'action'].includes(func.type)}
					<div
						class="flex gap-2 justify-between items-center px-3 py-1.5 text-sm font-medium cursor-pointer rounded-md"
					>
						<div class="flex gap-2 items-center">
							<GlobeAlt />

							<div class="flex items-center">{$i18n.t('Global')}</div>
						</div>

						<div>
							<Switch onchange={toggleGlobalHandler} bind:state={func.is_global} />
						</div>
					</div>

					<hr class="border-gray-50 dark:border-gray-800 my-1" />
				{/if}

				<DropdownMenu.Item
					class="flex gap-2 items-center px-3 py-1.5 text-sm  font-medium cursor-pointer hover:bg-gray-50 dark:hover:bg-gray-800  rounded-md"
					onclick={() => {
						editHandler();
					}}
				>
					<svg
						xmlns="http://www.w3.org/2000/svg"
						fill="none"
						viewBox="0 0 24 24"
						stroke-width="1.5"
						stroke="currentColor"
						class="w-4 h-4"
					>
						<path
							stroke-linecap="round"
							stroke-linejoin="round"
							d="M16.862 4.487l1.687-1.688a1.875 1.875 0 112.652 2.652L6.832 19.82a4.5 4.5 0 01-1.897 1.13l-2.685.8.8-2.685a4.5 4.5 0 011.13-1.897L16.863 4.487zm0 0L19.5 7.125"
						/>
					</svg>

					<div class="flex items-center">{$i18n.t('Edit')}</div>
				</DropdownMenu.Item>

				<DropdownMenu.Item
					class="flex gap-2 items-center px-3 py-1.5 text-sm  font-medium cursor-pointer hover:bg-gray-50 dark:hover:bg-gray-800  rounded-md"
					onclick={() => {
						shareHandler();
					}}
				>
					<Share />
					<div class="flex items-center">{$i18n.t('Share')}</div>
				</DropdownMenu.Item>

				<DropdownMenu.Item
					class="flex gap-2 items-center px-3 py-1.5 text-sm  font-medium cursor-pointer hover:bg-gray-50 dark:hover:bg-gray-800 rounded-md"
					onclick={() => {
						cloneHandler();
					}}
				>
					<DocumentDuplicate />

					<div class="flex items-center">{$i18n.t('Clone')}</div>
				</DropdownMenu.Item>

				<DropdownMenu.Item
					class="flex gap-2 items-center px-3 py-1.5 text-sm  font-medium cursor-pointer hover:bg-gray-50 dark:hover:bg-gray-800 rounded-md"
					onclick={() => {
						exportHandler();
					}}
				>
					<Download />

					<div class="flex items-center">{$i18n.t('Export')}</div>
				</DropdownMenu.Item>

				<hr class="border-gray-50 dark:border-gray-850 my-1" />

				<DropdownMenu.Item
					class="flex  gap-2  items-center px-3 py-1.5 text-sm  font-medium cursor-pointer hover:bg-gray-50 dark:hover:bg-gray-800 rounded-md"
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
