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
	import Github from '$lib/components/icons/Github.svelte';
	import Plus from '$lib/components/icons/Plus.svelte';
	import Pencil from '$lib/components/icons/Pencil.svelte';
	import PencilSolid from '$lib/components/icons/PencilSolid.svelte';
	import Link from '$lib/components/icons/Link.svelte';

	const i18n = getContext('i18n');

	interface Props {
		createHandler: Function;
		importFromLinkHandler: Function;
		onClose?: Function;
		children?: import('svelte').Snippet;
	}

	let { createHandler, importFromLinkHandler, onClose = () => {}, children }: Props = $props();

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
	<Tooltip content={$i18n.t('Create')}>
		{@render children?.()}
	</Tooltip>

	{#snippet content()}
		<div>
			<DropdownMenu.Content
				class="w-full max-w-[190px] rounded-2xl px-1 py-1 border-hairline border-gray-100  dark:border-gray-800 z-50 bg-white dark:bg-gray-850 dark:text-white shadow-lg"
				sideOffset={6}
				side="bottom"
				align="start"
				transition={flyAndScale}
			>
				<button
					class="flex gap-2 items-center px-3 py-1.5 text-sm cursor-pointer hover:bg-gray-50 dark:hover:bg-gray-800 rounded-xl w-full"
					onclick={async () => {
						createHandler();
						show = false;
					}}
				>
					<div class=" self-center mr-2">
						<Pencil />
					</div>
					<div class=" self-center truncate">{$i18n.t('New Tool')}</div>
				</button>

				<button
					class="flex gap-2 items-center px-3 py-1.5 text-sm cursor-pointer hover:bg-gray-50 dark:hover:bg-gray-800 rounded-xl w-full"
					onclick={async () => {
						importFromLinkHandler();
						show = false;
					}}
				>
					<div class=" self-center mr-2">
						<Link />
					</div>
					<div class=" self-center truncate">{$i18n.t('Import From Link')}</div>
				</button>
			</DropdownMenu.Content>
		</div>
	{/snippet}
</Dropdown>
