<script lang="ts">
	import { SvelteFlowProvider } from '@xyflow/svelte';
	import View from './Overview/View.svelte';

	let { history, chatId = null, onClose, onNodeClick } = $props();
</script>

<SvelteFlowProvider>
	<View {history} {chatId} {onClose} {onNodeClick} />
</SvelteFlowProvider>
