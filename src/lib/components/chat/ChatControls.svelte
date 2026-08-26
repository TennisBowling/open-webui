<script lang="ts">
	import { SvelteFlowProvider } from '@xyflow/svelte';
	import { slide } from 'svelte/transition';
	import { Pane, PaneResizer } from 'paneforge';

	import { onDestroy, onMount, tick } from 'svelte';
	import {
		mobile,
		showControls,
		showCallOverlay,
		showOverview,
		showArtifacts,
		showBrowserPanel,
		showEmbeds,
		showFilePreview
	} from '$lib/stores';

	import Drawer from '../common/Drawer.svelte';
	// The 6 control overlays (Controls / CallOverlay / Artifacts / BrowserPanel /
	// Embeds / FilePreview) are lazy-loaded via {#await import()} at their render
	// sites below — they only ever appear inside {#if $showControls}, never at
	// first paint, and CallOverlay drags in the heavy kokoro TTS/STT machinery.

	interface Props {
		// Keeping them off the static import graph splits ~10-18 KB out of the cold load.
		history: any;
		models?: any;
		chatId?: any;
		chatFiles?: any;
		params?: any;
		eventTarget: EventTarget;
		submitPrompt: Function;
		stopResponse: Function;
		showMessage: Function;
		files: any;
		modelId: any;
		pane: any;
	}

	let {
		history = $bindable(),
		models = [],
		chatId = null,
		chatFiles = $bindable([]),
		params = $bindable({}),
		eventTarget,
		submitPrompt,
		stopResponse,
		showMessage,
		files = $bindable(),
		modelId,
		pane = $bindable()
	}: Props = $props();

	let mediaQuery;
	let largeScreen = $state(false);
	let dragged = $state(false);

	let minSize = $state(0);

	export const openPane = () => {
		if (parseInt(localStorage?.chatControlsSize)) {
			const container = document.getElementById('chat-container');
			let size = Math.floor(
				(parseInt(localStorage?.chatControlsSize) / container.clientWidth) * 100
			);
			pane.resize(size);
		} else {
			pane.resize(minSize);
		}
	};

	const handleMediaQuery = async (e) => {
		if (e.matches) {
			largeScreen = true;

			if ($showCallOverlay) {
				showCallOverlay.set(false);
				await tick();
				showCallOverlay.set(true);
			}
		} else {
			largeScreen = false;

			if ($showCallOverlay) {
				showCallOverlay.set(false);
				await tick();
				showCallOverlay.set(true);
			}
			pane = null;
		}
	};

	const onMouseDown = (event) => {
		dragged = true;
	};

	const onMouseUp = (event) => {
		dragged = false;
	};

	onMount(() => {
		// listen to resize 1024px
		mediaQuery = window.matchMedia('(min-width: 1024px)');

		mediaQuery.addEventListener('change', handleMediaQuery);
		handleMediaQuery(mediaQuery);

		// Select the container element you want to observe
		const container = document.getElementById('chat-container');

		// initialize the minSize based on the container width
		minSize = Math.floor((350 / container.clientWidth) * 100);

		// Create a new ResizeObserver instance
		const resizeObserver = new ResizeObserver((entries) => {
			for (let entry of entries) {
				const width = entry.contentRect.width;
				// calculate the percentage of 350px
				const percentage = (350 / width) * 100;
				// set the minSize to the percentage, must be an integer
				minSize = Math.floor(percentage);

				if ($showControls) {
					if (pane && pane.isExpanded() && pane.getSize() < minSize) {
						pane.resize(minSize);
					} else {
						let size = Math.floor(
							(parseInt(localStorage?.chatControlsSize) / container.clientWidth) * 100
						);
						if (size < minSize) {
							pane.resize(minSize);
						}
					}
				}
			}
		});

		// Start observing the container's size changes
		resizeObserver.observe(container);

		document.addEventListener('mousedown', onMouseDown);
		document.addEventListener('mouseup', onMouseUp);
	});

	onDestroy(() => {
		showControls.set(false);

		mediaQuery.removeEventListener('change', handleMediaQuery);
		document.removeEventListener('mousedown', onMouseDown);
		document.removeEventListener('mouseup', onMouseUp);
	});

	const closeHandler = () => {
		showControls.set(false);
		showOverview.set(false);
		showArtifacts.set(false);
		showBrowserPanel.set(false);
		showEmbeds.set(false);
		showFilePreview.set(false);

		if ($showCallOverlay) {
			showCallOverlay.set(false);
		}
	};

	$effect(() => {
		if (!chatId) {
			closeHandler();
		}
	});
</script>

{#if !largeScreen}
	{#if $showControls}
		<Drawer
			show={$showControls}
			onClose={() => {
				showControls.set(false);
			}}
		>
			<div
				class=" {$showCallOverlay ||
				$showOverview ||
				$showArtifacts ||
				$showBrowserPanel ||
				$showEmbeds ||
				$showFilePreview
					? ' h-screen  w-full pt-safe pl-safe pr-safe pb-safe'
					: 'px-4 py-3'} h-full"
			>
				{#if $showCallOverlay}
					<div
						class=" h-full max-h-[100dvh] bg-white text-gray-700 dark:bg-gray-900 dark:text-gray-300 flex justify-center"
					>
						{#await import('./MessageInput/CallOverlay.svelte') then { default: CallOverlay }}
							<CallOverlay
								bind:files
								{submitPrompt}
								{stopResponse}
								{modelId}
								{chatId}
								{eventTarget}
								onclose={() => {
									showControls.set(false);
								}}
							/>
						{/await}
					</div>
				{:else if $showEmbeds}
					{#await import('./ChatControls/Embeds.svelte') then { default: Embeds }}
						<Embeds />
					{/await}
				{:else if $showFilePreview}
					{#await import('./ChatControls/FilePreview.svelte') then { default: FilePreview }}
						<FilePreview />
					{/await}
				{:else if $showBrowserPanel}
					{#await import('./BrowserPanel.svelte') then { default: BrowserPanel }}
						<BrowserPanel {history} {chatId} />
					{/await}
				{:else if $showArtifacts}
					{#await import('./Artifacts.svelte') then { default: Artifacts }}
						<Artifacts {history} {chatId} />
					{/await}
				{:else if $showOverview}
					{#await import('./Overview.svelte') then { default: Overview }}
						<Overview
							{history}
							{chatId}
							onNodeClick={(e) => {
								const node = e.node;
								showMessage(node.data.message, true);
							}}
							onClose={() => {
								showControls.set(false);
							}}
						/>
					{/await}
				{:else}
					{#await import('./Controls/Controls.svelte') then { default: Controls }}
						<Controls
							onclose={() => {
								showControls.set(false);
							}}
							{models}
							bind:chatFiles
							bind:params
						/>
					{/await}
				{/if}
			</div>
		</Drawer>
	{/if}
{:else}
	<!-- if $showControls -->

	{#if $showControls}
		<PaneResizer
			class="relative flex items-center justify-center group border-l-hairline border-gray-50 dark:border-gray-850 hover:border-gray-200 dark:hover:border-gray-800 transition z-20"
			id="controls-resizer"
		>
			<div
				class=" absolute -left-1.5 -right-1.5 -top-0 -bottom-0 z-20 cursor-col-resize bg-transparent"
			></div>
		</PaneResizer>
	{/if}

	<Pane
		bind:pane
		defaultSize={0}
		onResize={(size) => {
			if ($showControls && pane.isExpanded()) {
				if (size < minSize) {
					pane.resize(minSize);
				}

				if (size < minSize) {
					localStorage.chatControlsSize = 0;
				} else {
					// save the size in  pixels to localStorage
					const container = document.getElementById('chat-container');
					localStorage.chatControlsSize = Math.floor((size / 100) * container.clientWidth);
				}
			}
		}}
		onCollapse={() => {
			showControls.set(false);
		}}
		collapsible={true}
		class=" z-10 bg-white dark:bg-gray-850"
	>
		{#if $showControls}
			<div class="flex max-h-full min-h-full">
				<div
					class="w-full {($showOverview ||
						$showArtifacts ||
						$showBrowserPanel ||
						$showEmbeds ||
						$showFilePreview) &&
					!$showCallOverlay
						? ' '
						: 'px-4 py-3 bg-white dark:shadow-lg dark:bg-gray-850 '} z-40 pointer-events-auto overflow-y-auto scrollbar-hidden"
					id="controls-container"
				>
					{#if $showCallOverlay}
						<div class="w-full h-full flex justify-center">
							{#await import('./MessageInput/CallOverlay.svelte') then { default: CallOverlay }}
								<CallOverlay
									bind:files
									{submitPrompt}
									{stopResponse}
									{modelId}
									{chatId}
									{eventTarget}
									onclose={() => {
										showControls.set(false);
									}}
								/>
							{/await}
						</div>
					{:else if $showEmbeds}
						{#await import('./ChatControls/Embeds.svelte') then { default: Embeds }}
							<Embeds overlay={dragged} />
						{/await}
					{:else if $showFilePreview}
						{#await import('./ChatControls/FilePreview.svelte') then { default: FilePreview }}
							<FilePreview />
						{/await}
					{:else if $showBrowserPanel}
						{#await import('./BrowserPanel.svelte') then { default: BrowserPanel }}
							<BrowserPanel {history} {chatId} />
						{/await}
					{:else if $showArtifacts}
						{#await import('./Artifacts.svelte') then { default: Artifacts }}
							<Artifacts {history} {chatId} overlay={dragged} />
						{/await}
					{:else if $showOverview}
						{#await import('./Overview.svelte') then { default: Overview }}
							<Overview
								{history}
								{chatId}
								onNodeClick={(e) => {
									const node = e.node;
									if (node?.data?.message?.favorite) {
										history.messages[node.data.message.id].favorite = true;
									} else {
										history.messages[node.data.message.id].favorite = null;
									}

									showMessage(node.data.message, true);
								}}
								onClose={() => {
									showControls.set(false);
								}}
							/>
						{/await}
					{:else}
						{#await import('./Controls/Controls.svelte') then { default: Controls }}
							<Controls
								onclose={() => {
									showControls.set(false);
								}}
								{models}
								bind:chatFiles
								bind:params
							/>
						{/await}
					{/if}
				</div>
			</div>
		{/if}
	</Pane>
{/if}
