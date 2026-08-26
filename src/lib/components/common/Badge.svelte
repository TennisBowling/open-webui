<script lang="ts">
	import { statusChipClass } from '$lib/utils/statusColors';

	interface Props {
		type?: string;
		content?: string;
	}

	let { type = 'info', content = '' }: Props = $props();
</script>

<div
	class=" text-xs font-semibold {statusChipClass[type] ??
		statusChipClass['info']}  w-fit px-2 rounded-md uppercase line-clamp-1 mr-0.5"
>
	{content}
</div>
