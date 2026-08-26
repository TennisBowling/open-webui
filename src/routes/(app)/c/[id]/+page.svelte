<script lang="ts">
	import { page } from '$app/stores';

	import Chat from '$lib/components/chat/Chat.svelte';

	let { data } = $props();

	// Local-first open: the promises stay SEPARATE so Chat.svelte can paint from
	// the local IDB copy the moment it reads (~ms) and let the chat body
	// revalidate it in the background. Task/stream state rides inside the open
	// response itself (meta.active / the 304 proven-idle invariant).
	let preloaded = $derived(
		data.chatPromise
			? {
					chatId: data.chatId,
					localEntryPromise: data.localEntryPromise ?? null,
					chatPromise: data.chatPromise
				}
			: null
	);
</script>

<Chat chatIdProp={$page.params.id} {preloaded} />
