<script lang="ts">
	// The first of the two steps an opening tool call plays: the panel slides
	// open around this (deliberately short, fixed-shape) placeholder, and
	// SmoothResize then grows it into the real body. Keep its height stable —
	// if it grew with the content it would defeat the point.
	interface Props {
		lines?: number;
	}

	let { lines = 3 }: Props = $props();

	const widths = ['w-2/5', 'w-4/5', 'w-3/5', 'w-2/3'];
</script>

<div class="space-y-2.5 px-1 py-2" aria-busy="true">
	{#each Array(lines) as _, index}
		<div class="skeleton h-2.5 rounded-full {widths[index % widths.length]}"></div>
	{/each}
</div>
