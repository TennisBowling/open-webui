<script lang="ts">
	import { copyToClipboard, unescapeHtml } from '$lib/utils';
	import { toast } from '$lib/utils/toast';
	import { fade } from 'svelte/transition';

	import { getContext } from 'svelte';

	const i18n = getContext('i18n');

	interface Props {
		token: any;
		done?: boolean;
	}

	let { token, done = true }: Props = $props();
</script>

<!-- svelte-ignore a11y_click_events_have_key_events -->
<!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
{#if done}
	<code
		class="codespan cursor-pointer"
		onclick={() => {
			copyToClipboard(unescapeHtml(token.text));
			toast.success($i18n.t('Copied to clipboard'));
		}}>{unescapeHtml(token.text)}</code
	>
{:else}
	<code
		transition:fade={{ duration: 100 }}
		class="codespan cursor-pointer"
		onclick={() => {
			copyToClipboard(unescapeHtml(token.text));
			toast.success($i18n.t('Copied to clipboard'));
		}}>{unescapeHtml(token.text)}</code
	>
{/if}
