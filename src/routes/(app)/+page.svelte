<script lang="ts">
	import { onMount } from 'svelte';
	import { toast } from '$lib/utils/toast';

	import Chat from '$lib/components/chat/Chat.svelte';
	import { page } from '$app/state';

	onMount(() => {
		if (page.url.searchParams.get('error')) {
			toast.error(page.url.searchParams.get('error') || 'An unknown error occurred.');
		}
	});
</script>

<Chat />
