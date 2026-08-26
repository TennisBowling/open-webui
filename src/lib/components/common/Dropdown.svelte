<script lang="ts">
	import { dispatchComponentEvent } from '$lib/utils/componentEvents';
	import { DropdownMenu } from 'bits-ui';
	import { getContext } from 'svelte';

	const i18n = getContext('i18n');

	import { flyAndScale } from '$lib/utils/transitions';

	interface Props {
		show?: boolean;
		side?: string;
		align?: string;
		closeOnOutsideClick?: boolean;
		children?: import('svelte').Snippet;
		content?: import('svelte').Snippet;
	}

	let {
		show = $bindable(false),
		side = 'bottom',
		align = 'start',
		closeOnOutsideClick = true,
		children,
		content,
		...eventProps
	}: Props & Record<string, unknown> = $props();

	const dispatch = (type: string, detail?: unknown) =>
		dispatchComponentEvent(eventProps, type, detail);
</script>

<DropdownMenu.Root
	bind:open={show}
	closeFocus={false}
	{closeOnOutsideClick}
	onOpenChange={(state) => {
		dispatch('change', state);
	}}
	typeahead={false}
>
	<DropdownMenu.Trigger>
		{@render children?.()}
	</DropdownMenu.Trigger>

	{#if content}{@render content()}{:else}
		<DropdownMenu.Content
			class="w-full max-w-[130px] rounded-xl p-1 border-hairline border-gray-200 dark:border-gray-700 z-50 bg-white dark:bg-gray-850 text-gray-800 dark:text-white shadow-lg"
			sideOffset={8}
			{side}
			{align}
			transition={flyAndScale}
		>
			<DropdownMenu.Item class="flex items-center px-3 py-2 text-sm  font-medium">
				<div class="flex items-center">{$i18n.t('Profile')}</div>
			</DropdownMenu.Item>

			<DropdownMenu.Item class="flex items-center px-3 py-2 text-sm  font-medium">
				<div class="flex items-center">{$i18n.t('Profile')}</div>
			</DropdownMenu.Item>

			<DropdownMenu.Item class="flex items-center px-3 py-2 text-sm  font-medium">
				<div class="flex items-center">{$i18n.t('Profile')}</div>
			</DropdownMenu.Item>
		</DropdownMenu.Content>
	{/if}
</DropdownMenu.Root>
