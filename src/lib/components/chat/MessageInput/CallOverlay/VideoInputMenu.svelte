<script lang="ts">
	import { dispatchComponentEvent } from '$lib/utils/componentEvents';
	import { DropdownMenu } from 'bits-ui';
	import { flyAndScale } from '$lib/utils/transitions';
	import { getContext } from 'svelte';

	const i18n = getContext('i18n');
	const dispatch = (type: string, detail?: unknown) =>
		dispatchComponentEvent(eventProps, type, detail);

	import Dropdown from '$lib/components/common/Dropdown.svelte';

	interface Props {
		onClose?: Function;
		devices: any;
		children?: import('svelte').Snippet;
	}

	let {
		onClose = () => {},
		devices,
		children,
		...eventProps
	}: Props & Record<string, unknown> = $props();

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
	{@render children?.()}

	{#snippet content()}
		<div>
			<DropdownMenu.Content
				class="w-full max-w-[180px] rounded-lg p-1 border-hairline border-gray-100 dark:border-gray-800 z-9999 bg-white dark:bg-gray-900 dark:text-white shadow-xs"
				sideOffset={6}
				side="top"
				align="start"
				transition={flyAndScale}
			>
				{#each devices as device}
					<DropdownMenu.Item
						class="flex gap-2 items-center px-3 py-1.5 text-sm  cursor-pointer hover:bg-gray-50 dark:hover:bg-gray-800 rounded-md"
						onclick={() => {
							dispatch('change', device.deviceId);
						}}
					>
						<div class="flex items-center">
							<div class=" line-clamp-1">
								{device?.label ?? 'Camera'}
							</div>
						</div>
					</DropdownMenu.Item>
				{/each}
			</DropdownMenu.Content>
		</div>
	{/snippet}
</Dropdown>
