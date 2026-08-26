<script lang="ts">
	import { DropdownMenu } from 'bits-ui';
	import { flyAndScale } from '$lib/utils/transitions';
	import { getContext } from 'svelte';
	import { config } from '$lib/stores';

	import Dropdown from '$lib/components/common/Dropdown.svelte';
	import GarbageBin from '$lib/components/icons/GarbageBin.svelte';
	import Pencil from '$lib/components/icons/Pencil.svelte';
	import Tooltip from '$lib/components/common/Tooltip.svelte';
	import Tags from '$lib/components/chat/Tags.svelte';
	import Share from '$lib/components/icons/Share.svelte';
	import ArchiveBox from '$lib/components/icons/ArchiveBox.svelte';
	import DocumentDuplicate from '$lib/components/icons/DocumentDuplicate.svelte';
	import Download from '$lib/components/icons/Download.svelte';

	const i18n = getContext('i18n');

	interface Props {
		shareHandler: Function;
		cloneHandler: Function;
		exportHandler: Function;
		deleteHandler: Function;
		onClose: Function;
		children?: import('svelte').Snippet;
	}

	let { shareHandler, cloneHandler, exportHandler, deleteHandler, onClose, children }: Props =
		$props();

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
				class="w-full max-w-[170px] rounded-2xl p-1 border-hairline border-gray-100  dark:border-gray-800 z-50 bg-white dark:bg-gray-850 dark:text-white shadow-lg"
				sideOffset={-2}
				side="bottom"
				align="start"
				transition={flyAndScale}
			>
				{#if $config.features.enable_community_sharing}
					<DropdownMenu.Item
						class="flex gap-2 items-center px-3 py-1.5 text-sm  cursor-pointer hover:bg-gray-50 dark:hover:bg-gray-800  rounded-xl"
						onclick={() => {
							shareHandler();
						}}
					>
						<Share />
						<div class="flex items-center">{$i18n.t('Share')}</div>
					</DropdownMenu.Item>
				{/if}

				<DropdownMenu.Item
					class="flex gap-2 items-center px-3 py-1.5 text-sm  cursor-pointer hover:bg-gray-50 dark:hover:bg-gray-800 rounded-xl"
					onclick={() => {
						cloneHandler();
					}}
				>
					<DocumentDuplicate />

					<div class="flex items-center">{$i18n.t('Clone')}</div>
				</DropdownMenu.Item>

				<DropdownMenu.Item
					class="flex gap-2 items-center px-3 py-1.5 text-sm  cursor-pointer hover:bg-gray-50 dark:hover:bg-gray-800 rounded-xl"
					onclick={() => {
						exportHandler();
					}}
				>
					<Download />

					<div class="flex items-center">{$i18n.t('Export')}</div>
				</DropdownMenu.Item>

				<hr class="border-gray-50 dark:border-gray-850 my-1" />

				<DropdownMenu.Item
					class="flex  gap-2  items-center px-3 py-1.5 text-sm  cursor-pointer hover:bg-gray-50 dark:hover:bg-gray-800 rounded-xl"
					onclick={() => {
						deleteHandler();
					}}
				>
					<GarbageBin />
					<div class="flex items-center">{$i18n.t('Delete')}</div>
				</DropdownMenu.Item>
			</DropdownMenu.Content>
		</div>
	{/snippet}
</Dropdown>
