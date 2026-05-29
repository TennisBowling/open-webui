<script lang="ts">
	import { createEventDispatcher, onDestroy, onMount } from 'svelte';
	const dispatch = createEventDispatcher();

	// `intervalMs > 0` (default) preserves the legacy "fire repeatedly while
	// the loader stays in view" semantic — needed by infinite-scroll lists
	// (sidebar, chats modal) where loading a batch may not push the loader
	// off-screen. Set to 0 for one-shot dispatch per visibility transition.
	export let intervalMs: number = 100;

	// Explicit scroll container. Default `null` = viewport. Set this when the
	// scrollable container is smaller than the viewport — otherwise on a list
	// shorter than the page the loader is permanently "intersecting" and
	// (with intervalMs > 0) storms the page handler.
	export let root: Element | null = null;

	let loaderElement: HTMLElement;
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
				rootMargin: '0px',
				threshold: 0.1
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
	<slot />
</div>
