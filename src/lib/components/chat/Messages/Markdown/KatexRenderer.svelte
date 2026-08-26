<script lang="ts">
	import type { renderToString as katexRenderToString } from 'katex';
	import { onMount } from 'svelte';

	interface Props {
		content: string;
		displayMode?: boolean;
	}

	let { content, displayMode = false }: Props = $props();

	let renderToString: typeof katexRenderToString | null = $state(null);

	onMount(async () => {
		const [katex] = await Promise.all([
			import('katex'),
			import('katex/contrib/mhchem'),
			import('katex/dist/katex.min.css')
		]);
		renderToString = katex.renderToString;
	});
</script>

{#if renderToString}
	{@html renderToString(content, { displayMode, throwOnError: false })}
{:else}
	<!-- The katex chunk (+ its CSS) is code-split, so the first math on screen
	     renders one or two frames late. Rendering NOTHING in the meantime
	     collapses the box to zero height and then shoves the rest of the
	     conversation down when it lands — a display equation is a whole line,
	     and inline math re-wraps the paragraph around it. Holding the raw
	     source in place (invisible, so there's no LaTeX flash) keeps a
	     same-order-of-magnitude box, which the scroll-anchoring engine can
	     absorb instead of a full-height jump. -->
	<span class="katex-pending" style="visibility: hidden;" aria-hidden="true">{content}</span>
{/if}
