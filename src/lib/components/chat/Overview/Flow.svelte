<script lang="ts">
	import { dispatchComponentEvent } from '$lib/utils/componentEvents';
	import { theme } from '$lib/stores';
	import {
		Background,
		BackgroundVariant,
		ControlButton,
		Controls,
		SvelteFlow
	} from '@xyflow/svelte';
	import AlignVertical from '$lib/components/icons/AlignVertical.svelte';
	import AlignHorizontal from '$lib/components/icons/AlignHorizontal.svelte';

	let {
		nodes = [],
		nodeTypes,
		edges = [],
		layoutDirection,
		setLayoutDirection,
		...eventProps
	} = $props();
	const dispatch = (type: string, detail?: unknown) =>
		dispatchComponentEvent(eventProps, type, detail);
</script>

<div class="h-full w-full pt-16">
	<SvelteFlow
		{nodes}
		{nodeTypes}
		{edges}
		fitView
		fitViewOptions={{ padding: 0.35 }}
		minZoom={0.05}
		maxZoom={1.5}
		colorMode={$theme.includes('dark')
			? 'dark'
			: $theme === 'system'
				? window.matchMedia('(prefers-color-scheme: dark)').matches
					? 'dark'
					: 'light'
				: 'light'}
		nodesConnectable={false}
		nodesDraggable={false}
		elementsSelectable={true}
		onnodeclick={(event) => dispatch('nodeclick', event)}
	>
		<Controls showLock={false} position="bottom-right">
			<ControlButton
				onclick={() => setLayoutDirection('vertical')}
				title="Vertical layout"
				aria-label="Vertical layout"
				class={layoutDirection === 'vertical' ? '!bg-gray-200 dark:!bg-gray-700' : ''}
			>
				<AlignVertical className="size-4" />
			</ControlButton>
			<ControlButton
				onclick={() => setLayoutDirection('horizontal')}
				title="Horizontal layout"
				aria-label="Horizontal layout"
				class={layoutDirection === 'horizontal' ? '!bg-gray-200 dark:!bg-gray-700' : ''}
			>
				<AlignHorizontal className="size-4" />
			</ControlButton>
		</Controls>
		<Background variant={BackgroundVariant.Dots} gap={18} size={1} />
	</SvelteFlow>
</div>

<style>
	:global(.overview-edge .svelte-flow__edge-path) {
		stroke: #d1d5db;
		stroke-width: 1.5;
	}

	:global(.overview-edge-active .svelte-flow__edge-path) {
		stroke: #b45309;
		stroke-width: 2.25;
	}

	:global(.dark .overview-edge .svelte-flow__edge-path) {
		stroke: #4b5563;
	}

	:global(.dark .overview-edge-active .svelte-flow__edge-path) {
		stroke: #d6a85f;
	}
</style>
