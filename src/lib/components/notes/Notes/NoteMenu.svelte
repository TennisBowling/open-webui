<script lang="ts">
	import { DropdownMenu } from 'bits-ui';
	import { getContext, onMount } from 'svelte';

	import { flyAndScale } from '$lib/utils/transitions';
	import { fade, slide } from 'svelte/transition';

	import { showSettings, mobile, showSidebar, user } from '$lib/stores';

	import Tooltip from '$lib/components/common/Tooltip.svelte';
	import ArchiveBox from '$lib/components/icons/ArchiveBox.svelte';
	import Download from '$lib/components/icons/Download.svelte';
	import GarbageBin from '$lib/components/icons/GarbageBin.svelte';
	import DocumentDuplicate from '$lib/components/icons/DocumentDuplicate.svelte';
	import Share from '$lib/components/icons/Share.svelte';
	import Link from '$lib/components/icons/Link.svelte';

	const i18n = getContext('i18n');

	interface Props {
		show?: boolean;
		className?: string;
		onDownload?: any;
		onDelete?: any;
		onCopyLink?: any;
		onCopyToClipboard?: any;
		onChange?: any;
		children?: import('svelte').Snippet;
		content?: import('svelte').Snippet;
	}

	let {
		show = $bindable(false),
		className = 'max-w-[180px]',
		onDownload = (type) => {},
		onDelete = () => {},
		onCopyLink = null,
		onCopyToClipboard = null,
		onChange = () => {},
		children,
		content
	}: Props = $props();
</script>

<DropdownMenu.Root
	bind:open={show}
	onOpenChange={(state) => {
		onChange(state);
	}}
>
	<DropdownMenu.Trigger>
		{@render children?.()}
	</DropdownMenu.Trigger>

	{#if content}{@render content()}{:else}
		<DropdownMenu.Content
			class="w-full {className} text-sm rounded-2xl px-1 py-1 border-hairline border-gray-100  dark:border-gray-800  z-50 bg-white dark:bg-gray-850 dark:text-white shadow-lg"
			sideOffset={6}
			side="bottom"
			align="end"
			transition={(e) => fade(e, { duration: 100 })}
		>
			<DropdownMenu.Sub>
				<DropdownMenu.SubTrigger
					class="flex gap-2 items-center px-3 py-1.5 text-sm  cursor-pointer hover:bg-gray-50 dark:hover:bg-gray-800 rounded-xl"
				>
					<Download strokeWidth="2" />

					<div class="flex items-center">{$i18n.t('Download')}</div>
				</DropdownMenu.SubTrigger>
				<DropdownMenu.SubContent
					class="w-full rounded-xl p-1 z-50 bg-white dark:bg-gray-850 dark:text-white shadow-lg"
					transition={flyAndScale}
					sideOffset={8}
					align="end"
				>
					<DropdownMenu.Item
						class="flex gap-2 items-center px-3 py-1.5 text-sm  cursor-pointer hover:bg-gray-50 dark:hover:bg-gray-800 rounded-xl"
						onclick={() => {
							onDownload('txt');
						}}
					>
						<div class="flex items-center line-clamp-1">{$i18n.t('Plain text (.txt)')}</div>
					</DropdownMenu.Item>

					<DropdownMenu.Item
						class="flex gap-2 items-center px-3 py-1.5 text-sm  cursor-pointer hover:bg-gray-50 dark:hover:bg-gray-800 rounded-xl"
						onclick={() => {
							onDownload('md');
						}}
					>
						<div class="flex items-center line-clamp-1">{$i18n.t('Plain text (.md)')}</div>
					</DropdownMenu.Item>

					<DropdownMenu.Item
						class="flex gap-2 items-center px-3 py-1.5 text-sm  cursor-pointer hover:bg-gray-50 dark:hover:bg-gray-800 rounded-xl"
						onclick={() => {
							onDownload('pdf');
						}}
					>
						<div class="flex items-center line-clamp-1">{$i18n.t('PDF document (.pdf)')}</div>
					</DropdownMenu.Item>
				</DropdownMenu.SubContent>
			</DropdownMenu.Sub>

			{#if onCopyLink || onCopyToClipboard}
				<DropdownMenu.Sub>
					<DropdownMenu.SubTrigger
						class="flex gap-2 items-center px-3 py-1.5 text-sm  cursor-pointer hover:bg-gray-50 dark:hover:bg-gray-800 rounded-xl"
					>
						<Share strokeWidth="2" />

						<div class="flex items-center">{$i18n.t('Share')}</div>
					</DropdownMenu.SubTrigger>
					<DropdownMenu.SubContent
						class="w-full rounded-xl p-1 z-50 bg-white dark:bg-gray-850 dark:text-white shadow-lg"
						transition={flyAndScale}
						sideOffset={8}
						align="end"
					>
						{#if onCopyLink}
							<DropdownMenu.Item
								class="flex gap-2 items-center px-3 py-1.5 text-sm  cursor-pointer hover:bg-gray-50 dark:hover:bg-gray-800 rounded-xl"
								onclick={() => {
									onCopyLink();
								}}
							>
								<Link />
								<div class="flex items-center">{$i18n.t('Copy link')}</div>
							</DropdownMenu.Item>
						{/if}

						{#if onCopyToClipboard}
							<DropdownMenu.Item
								class="flex gap-2 items-center px-3 py-1.5 text-sm  cursor-pointer hover:bg-gray-50 dark:hover:bg-gray-800 rounded-xl"
								onclick={() => {
									onCopyToClipboard();
								}}
							>
								<DocumentDuplicate strokeWidth="2" />
								<div class="flex items-center">{$i18n.t('Copy to clipboard')}</div>
							</DropdownMenu.Item>
						{/if}
					</DropdownMenu.SubContent>
				</DropdownMenu.Sub>
			{/if}

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
	{/if}
</DropdownMenu.Root>
