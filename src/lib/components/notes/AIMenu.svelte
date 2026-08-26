<script lang="ts">
	import { DropdownMenu } from 'bits-ui';
	import { getContext, onMount } from 'svelte';

	import { showSettings, mobile, showSidebar, user } from '$lib/stores';
	import { fade, slide } from 'svelte/transition';

	import PencilSquare from '../icons/PencilSquare.svelte';
	import ChatBubbleOval from '../icons/ChatBubbleOval.svelte';
	import Sparkles from '../icons/Sparkles.svelte';

	const i18n = getContext('i18n');

	interface Props {
		show?: boolean;
		className?: string;
		onEdit?: any;
		onChat?: any;
		onChange?: any;
		children?: import('svelte').Snippet;
		content?: import('svelte').Snippet;
	}

	let {
		show = $bindable(false),
		className = 'max-w-[170px]',
		onEdit = () => {},
		onChat = () => {},
		onChange = () => {},
		children,
		content
	}: Props = $props();
</script>

<DropdownMenu.Root bind:open={show} onOpenChange={onChange}>
	<DropdownMenu.Trigger>
		{@render children?.()}
	</DropdownMenu.Trigger>

	{#if content}{@render content()}{:else}
		<DropdownMenu.Content
			class="w-full {className} text-sm rounded-xl p-1 z-50 bg-white dark:bg-gray-850 dark:text-white shadow-lg font-primary"
			sideOffset={8}
			side="bottom"
			align="end"
			transition={(e) => fade(e, { duration: 100 })}
		>
			<button
				class="flex rounded-md py-1.5 px-3 w-full hover:bg-gray-50 dark:hover:bg-gray-800 transition"
				onclick={async () => {
					onEdit();
					show = false;
				}}
			>
				<div class=" self-center mr-2">
					<Sparkles className="size-4" strokeWidth="2" />
				</div>
				<div class=" self-center truncate">{$i18n.t('Enhance')}</div>
			</button>

			<button
				class="flex rounded-md py-1.5 px-3 w-full hover:bg-gray-50 dark:hover:bg-gray-800 transition"
				onclick={() => {
					onChat();
					show = false;
				}}
			>
				<div class=" self-center mr-2">
					<ChatBubbleOval className="size-4" strokeWidth="2" />
				</div>
				<div class=" self-center truncate">{$i18n.t('Chat')}</div>
			</button>
		</DropdownMenu.Content>
	{/if}
</DropdownMenu.Root>
