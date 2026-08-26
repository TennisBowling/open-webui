<script lang="ts">
	import WrenchAlt from '$lib/components/icons/WrenchAlt.svelte';

	// One renderer for every place a tool / tool-server icon appears (integrations
	// menu, settings rows, tool details) so the broken-image handling, sizing and
	// decode hints stay identical everywhere. Each call site keeps its own
	// un-iconed look by passing `fallback`.
	interface Props {
		src?: string | null;
		alt?: string;
		className?: string;
		fallback?: import('svelte').Snippet;
	}

	let { src = null, alt = '', className = 'size-4', fallback }: Props = $props();

	// The src that failed to load, so a broken or since-removed icon falls back
	// instead of showing a broken-image glyph. Comparing it against the current
	// src self-resets when the icon changes — no effect, no stale state.
	let broken = $state('');
</script>

{#if src && broken !== src}
	<img
		{src}
		{alt}
		class="{className} object-contain"
		loading="lazy"
		decoding="async"
		draggable="false"
		onerror={() => (broken = src)}
	/>
{:else if fallback}
	{@render fallback()}
{:else}
	<WrenchAlt {className} />
{/if}
