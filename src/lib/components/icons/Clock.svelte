<script lang="ts">
	interface Props {
		className?: string;
		strokeWidth?: string;
	}

	let { className = 'w-4 h-4', strokeWidth = '1.5' }: Props = $props();
</script>

<svg
	class={className}
	aria-hidden="true"
	xmlns="http://www.w3.org/2000/svg"
	stroke-width={strokeWidth}
	stroke="currentColor"
	fill="none"
	viewBox="0 0 24 24"
	><path d="M12 7V12L15 14" stroke-linecap="round" stroke-linejoin="round"></path><path
		d="M21 12C21 16.9706 16.9706 21 12 21C7.02944 21 3 16.9706 3 12C3 7.02944 7.02944 3 12 3C16.9706 3 21 7.02944 21 12Z"
		stroke-linecap="round"
		stroke-linejoin="round"
	></path></svg
>
