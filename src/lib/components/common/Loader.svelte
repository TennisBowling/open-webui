<script lang="ts">
	import { dispatchComponentEvent } from '$lib/utils/componentEvents';
	import { onDestroy, onMount } from 'svelte';
	const dispatch = (type: string, detail?: unknown) =>
		dispatchComponentEvent(eventProps, type, detail);

	// `intervalMs > 0` (default) preserves the legacy "fire repeatedly while
	// the loader stays in view" semantic — needed by infinite-scroll lists
	// (sidebar, chats modal) where loading a batch may not push the loader

	// Explicit scroll container. Default `null` = viewport. Set this when the
	// scrollable container is smaller than the viewport — otherwise on a list
	// shorter than the page the loader is permanently "intersecting" and

	// Prefetch margin: expands the intersection box so `visible` fires BEFORE
	// the loader actually scrolls into view (e.g. '600px 0px 0px 0px' starts a
	// top-pagination load while the user is still 600px away, so the page lands

	interface Props {
		// off-screen. Set to 0 for one-shot dispatch per visibility transition.
		intervalMs?: number;
		// (with intervalMs > 0) storms the page handler.
		root?: Element | null;
		// off-screen instead of mid-gesture). Default keeps the legacy fire-on-view.
		rootMargin?: string;
		children?: import('svelte').Snippet;
	}

	let {
		intervalMs = 100,
		root = null,
		rootMargin = '0px',
		children,
		...eventProps
	}: Props & Record<string, unknown> = $props();

	let loaderElement: HTMLElement = $state();
	let observer: IntersectionObserver | null = null;
	let intervalId: ReturnType<typeof setInterval> | null = null;
	let wasIntersecting = false;

	onMount(() => {
		observer = new IntersectionObserver(
			(entries) => {
				entries.forEach((entry) => {
					if (entry.isIntersecting) {
						if (intervalMs > 0) {
							if (intervalId === null) {
								intervalId = setInterval(() => {
									dispatch('visible');
								}, intervalMs);
							}
						} else if (!wasIntersecting) {
							dispatch('visible');
						}
						wasIntersecting = true;
					} else {
						if (intervalId !== null) {
							clearInterval(intervalId);
							intervalId = null;
						}
						wasIntersecting = false;
					}
				});
			},
			{
				root,
				rootMargin,
				threshold: 0
			}
		);

		observer.observe(loaderElement);
	});

	onDestroy(() => {
		observer?.disconnect();
		if (intervalId !== null) {
			clearInterval(intervalId);
			intervalId = null;
		}
	});
</script>

<div bind:this={loaderElement}>
	{@render children?.()}
</div>
