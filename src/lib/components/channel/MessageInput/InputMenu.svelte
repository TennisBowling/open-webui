<script lang="ts">
	import { DropdownMenu } from 'bits-ui';
	import { flyAndScale } from '$lib/utils/transitions';
	import { getContext, onMount, tick } from 'svelte';

	import { config, user, tools as _tools, mobile } from '$lib/stores';
	import { getTools } from '$lib/apis/tools';

	import Dropdown from '$lib/components/common/Dropdown.svelte';
	import Tooltip from '$lib/components/common/Tooltip.svelte';
	import DocumentArrowUpSolid from '$lib/components/icons/DocumentArrowUpSolid.svelte';
	import Switch from '$lib/components/common/Switch.svelte';
	import GlobeAltSolid from '$lib/components/icons/GlobeAltSolid.svelte';
	import WrenchSolid from '$lib/components/icons/WrenchSolid.svelte';
	import CameraSolid from '$lib/components/icons/CameraSolid.svelte';
	import Camera from '$lib/components/icons/Camera.svelte';
	import Clip from '$lib/components/icons/Clip.svelte';

	const i18n = getContext('i18n');

	interface Props {
		screenCaptureHandler: Function;
		uploadFilesHandler: Function;
		onClose?: Function;
		children?: import('svelte').Snippet;
	}

	let { screenCaptureHandler, uploadFilesHandler, onClose = () => {}, children }: Props = $props();

	let show = $state(false);

	const init = async () => {};
	$effect(() => {
		if (show) {
			init();
		}
	});
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
				class="w-full max-w-[200px] rounded-2xl px-1 py-1  border-hairline border-gray-100  dark:border-gray-800 z-999 bg-white dark:bg-gray-850 dark:text-white shadow-lg transition"
				sideOffset={4}
				alignOffset={-6}
				side="bottom"
				align="start"
				transition={flyAndScale}
			>
				<DropdownMenu.Item
					class="flex gap-2 items-center px-3 py-1.5 text-sm cursor-pointer hover:bg-gray-50 dark:hover:bg-gray-800/50 rounded-xl"
					onclick={() => {
						uploadFilesHandler();
					}}
				>
					<Clip />
					<div class="line-clamp-1">{$i18n.t('Upload Files')}</div>
				</DropdownMenu.Item>

				<DropdownMenu.Item
					class="flex gap-2 items-center px-3 py-1.5 text-sm  cursor-pointer hover:bg-gray-50 dark:hover:bg-gray-800/50  rounded-xl"
					onclick={() => {
						screenCaptureHandler();
					}}
				>
					<Camera />
					<div class=" line-clamp-1">{$i18n.t('Capture')}</div>
				</DropdownMenu.Item>
			</DropdownMenu.Content>
		</div>
	{/snippet}
</Dropdown>
