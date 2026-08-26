<script lang="ts">
	import { preventDefault } from '$lib/utils/eventModifiers';
	import Tooltip from '$lib/components/common/Tooltip.svelte';
	import XMark from '$lib/components/icons/XMark.svelte';

	interface Props {
		active?: boolean;
		tooltip?: string;
		ariaLabel?: string;
		onClick?: () => void;
		children?: import('svelte').Snippet;
	}

	let {
		active = false,
		tooltip = '',
		ariaLabel = '',
		onClick = () => {},
		children
	}: Props = $props();
</script>

<Tooltip content={tooltip} placement="top">
	<button
		onclick={preventDefault(onClick)}
		onmousedown={preventDefault()}
		type="button"
		aria-label={ariaLabel || tooltip}
		aria-pressed={active}
		class="group shrink-0 p-2 max-md:p-2.5 flex gap-1.5 items-center text-sm rounded-full transition-colors duration-300 focus:outline-hidden max-w-full overflow-hidden {active
			? ' text-gray-900 dark:text-gray-100 bg-manilla/60 hover:bg-manilla/80 dark:bg-manilla-dark dark:hover:bg-manilla-dark/80 border-hairline border-book-cloth/30 dark:border-book-cloth/40'
			: 'bg-transparent text-gray-600 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-800 '}"
	>
		{@render children?.()}
		{#if active}
			<div class="hidden group-hover:block">
				<XMark className="size-4" strokeWidth="1.75" />
			</div>
		{/if}
	</button>
</Tooltip>
