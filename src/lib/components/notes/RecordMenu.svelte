<script lang="ts">
	import { dispatchComponentEvent } from '$lib/utils/componentEvents';
	import { DropdownMenu } from 'bits-ui';
	import { getContext, onMount } from 'svelte';

	import { showSettings, mobile, showSidebar, user } from '$lib/stores';
	import { fade, slide } from 'svelte/transition';

	import Mic from '../icons/Mic.svelte';
	import CursorArrowRays from '../icons/CursorArrowRays.svelte';
	import DocumentArrowUp from '../icons/DocumentArrowUp.svelte';
	import CloudArrowUp from '../icons/CloudArrowUp.svelte';

	const i18n = getContext('i18n');

	interface Props {
		show?: boolean;
		className?: string;
		onRecord?: any;
		onCaptureAudio?: any;
		onUpload?: any;
		children?: import('svelte').Snippet;
		content?: import('svelte').Snippet;
	}

	let {
		show = $bindable(false),
		className = 'max-w-[170px]',
		onRecord = () => {},
		onCaptureAudio = () => {},
		onUpload = () => {},
		children,
		content,
		...eventProps
	}: Props & Record<string, unknown> = $props();

	const dispatch = (type: string, detail?: unknown) =>
		dispatchComponentEvent(eventProps, type, detail);
</script>

<DropdownMenu.Root
	bind:open={show}
	onOpenChange={(state) => {
		dispatch('change', state);
	}}
>
	<DropdownMenu.Trigger>
		{@render children?.()}
	</DropdownMenu.Trigger>

	{#if content}{@render content()}{:else}
		<DropdownMenu.Content
			class="w-full {className} text-sm rounded-xl p-1 z-50 bg-white dark:bg-gray-850 dark:text-white shadow-lg font-primary"
			sideOffset={8}
			side="bottom"
			align="start"
			transition={(e) => fade(e, { duration: 100 })}
		>
			<button
				class="flex rounded-md py-1.5 px-3 w-full hover:bg-gray-50 dark:hover:bg-gray-800 transition"
				onclick={async () => {
					onRecord();
					show = false;
				}}
			>
				<div class=" self-center mr-2">
					<Mic className="size-4" strokeWidth="2" />
				</div>
				<div class=" self-center truncate">{$i18n.t('Record')}</div>
			</button>

			<button
				class="flex rounded-md py-1.5 px-3 w-full hover:bg-gray-50 dark:hover:bg-gray-800 transition"
				onclick={() => {
					onCaptureAudio();
					show = false;
				}}
			>
				<div class=" self-center mr-2">
					<CursorArrowRays className="size-4" strokeWidth="2" />
				</div>
				<div class=" self-center truncate">{$i18n.t('Capture Audio')}</div>
			</button>

			<button
				class="flex rounded-md py-1.5 px-3 w-full hover:bg-gray-50 dark:hover:bg-gray-800 transition"
				onclick={() => {
					onUpload();
					show = false;
				}}
			>
				<div class=" self-center mr-2">
					<CloudArrowUp className="size-4" strokeWidth="2" />
				</div>
				<div class=" self-center truncate">{$i18n.t('Upload Audio')}</div>
			</button>
		</DropdownMenu.Content>
	{/if}
</DropdownMenu.Root>
