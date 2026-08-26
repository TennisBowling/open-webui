<script lang="ts">
	interface Props {
		className?: string;
		strokeWidth?: string;
	}

	let { className = 'size-4', strokeWidth = '1.5' }: Props = $props();
</script>

<svg
	xmlns="http://www.w3.org/2000/svg"
	fill="none"
	viewBox="0 0 24 24"
	stroke-width={strokeWidth}
	stroke="currentColor"
	class={className}
	><path
		d="M15 10.5V7.5C15 6.39543 14.1046 5.5 13 5.5H4C2.89543 5.5 2 6.39543 2 7.5V16.5C2 17.6046 2.89543 18.5 4 18.5H13C14.1046 18.5 15 17.6046 15 16.5V13.5M15 10.5L21.5528 7.22361C21.8628 7.06861 22 7.19098 22 7.53744V16.4626C22 16.809 21.8628 16.9314 21.5528 16.7764L15 13.5M15 10.5V13.5"
		stroke-linecap="round"
		stroke-linejoin="round"
	></path></svg
>
