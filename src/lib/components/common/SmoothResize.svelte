<script lang="ts">
	// Animates its own height whenever the content inside it changes size.
	//
	// Tool-call bodies are lazy on two axes: the panel component is code-split
	// and the result body itself is fetched on demand. Without this, opening a
	// tool call played the slide transition against a loading shell and then
	// SNAPPED to the real body the moment it landed — the "opens a bit, stutters,
	// then jumps" feel. Wrapping the body in this component turns that second
	// step into its own animation: the panel slides open to the skeleton, then
	// grows smoothly into the loaded result.
	//
	// Height is only pinned while animating. At rest the wrapper is `height:auto`
	// with visible overflow so nothing inside is clipped or forced to re-layout.
	import { onMount } from 'svelte';

	interface Props {
		// Floor for the growth animation. Larger jumps get proportionally longer
		// (see `durationFor`) so a 600px reveal doesn't cover most of its distance
		// in the first two frames the way a fixed-duration ease does.
		duration?: number;
		class?: string;
		children?: import('svelte').Snippet;
	}

	let { duration = 200, class: className = '', children }: Props = $props();

	const MAX_DURATION = 420;
	const durationFor = (delta: number) =>
		Math.round(Math.min(MAX_DURATION, duration + Math.abs(delta) * 0.22));

	let outer: HTMLDivElement | null = $state(null);
	let inner: HTMLDivElement | null = $state(null);

	onMount(() => {
		if (!outer || !inner) return;

		const prefersReducedMotion =
			typeof window !== 'undefined' &&
			!!window.matchMedia?.('(prefers-reduced-motion: reduce)')?.matches;

		let lastHeight = inner.offsetHeight;
		let settleTimer: ReturnType<typeof setTimeout> | null = null;

		const release = () => {
			settleTimer = null;
			if (!outer) return;
			outer.style.height = '';
			outer.style.overflow = '';
			outer.style.transition = '';
		};

		const observer = new ResizeObserver(() => {
			if (!outer || !inner) return;
			const nextHeight = inner.offsetHeight;
			// Sub-pixel churn from font loading / scrollbar reflow is not a change.
			if (Math.abs(nextHeight - lastHeight) < 2) return;

			const fromHeight = lastHeight;
			lastHeight = nextHeight;
			if (prefersReducedMotion) return;

			const ms = durationFor(nextHeight - fromHeight);
			if (settleTimer) clearTimeout(settleTimer);
			outer.style.overflow = 'hidden';
			outer.style.transition = 'none';
			outer.style.height = `${fromHeight}px`;
			// Flush the start height so the next assignment is a transition and not
			// a same-frame no-op.
			void outer.offsetHeight;
			// Evenly-paced ease, NOT the app's front-loaded `ease-paper`: measured on
			// a 570px reveal, the decelerate curve covered a third of the distance in
			// its first frame, which reads as a pop rather than a growth.
			outer.style.transition = `height ${ms}ms cubic-bezier(0.4, 0, 0.2, 1)`;
			outer.style.height = `${nextHeight}px`;
			settleTimer = setTimeout(release, ms + 60);
		});

		observer.observe(inner);

		return () => {
			observer.disconnect();
			if (settleTimer) clearTimeout(settleTimer);
		};
	});
</script>

<div bind:this={outer} class={className}>
	<!-- flow-root so the child's margins are measured inside the box instead of
	     collapsing through it (which would make every measurement short). -->
	<div bind:this={inner} class="flow-root">
		{@render children?.()}
	</div>
</div>
