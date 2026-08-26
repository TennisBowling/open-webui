<script lang="ts">
	import { WEBUI_BASE_URL } from '$lib/constants';
	import { shortCodesToEmojis } from '$lib/stores';

	interface Props {
		shortCode: any;
		className?: string;
	}

	let { shortCode, className = 'size-4' }: Props = $props();
</script>

{#if $shortCodesToEmojis[shortCode]}
	<img
		src="{WEBUI_BASE_URL}/assets/emojis/{$shortCodesToEmojis[shortCode].toLowerCase()}.svg"
		alt={shortCode}
		class={className}
		loading="lazy"
	/>
{:else}
	<div>
		{shortCode}
	</div>
{/if}
