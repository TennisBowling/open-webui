<script lang="ts">
	import { onMount, tick } from 'svelte';

	interface Props {
		value?: string;
		placeholder?: string;
		rows?: number;
		minSize?: any;
		maxSize?: any;
		required?: boolean;
		readonly?: boolean;
		className?: string;
		onBlur?: any;
	}

	let {
		value = $bindable(''),
		placeholder = '',
		rows = 1,
		minSize = null,
		maxSize = null,
		required = false,
		readonly = false,
		className = 'w-full rounded-lg px-3.5 py-2 text-sm bg-white border-hairline border-gray-300 dark:text-gray-300 dark:bg-gray-900 dark:border-gray-700 outline-hidden focus-visible:ring-2 focus-visible:ring-book-cloth/40 focus-visible:border-book-cloth transition-colors duration-200 ease-paper h-full',
		onBlur = () => {}
	}: Props = $props();
	let textareaElement = $state();

	// Adjust height on mount and after setting the element.
	onMount(async () => {
		await tick();
		resize();

		requestAnimationFrame(() => {
			// setInterveal to cehck until textareaElement is set
			const interval = setInterval(() => {
				if (textareaElement) {
					clearInterval(interval);
					resize();
				}
			}, 100);
		});
	});

	const resize = () => {
		if (textareaElement) {
			textareaElement.style.height = '';

			let height = textareaElement.scrollHeight;
			if (maxSize && height > maxSize) {
				height = maxSize;
			}
			if (minSize && height < minSize) {
				height = minSize;
			}

			textareaElement.style.height = `${height}px`;
		}
	};
</script>

<textarea
	bind:this={textareaElement}
	bind:value
	{placeholder}
	class={className}
	style="field-sizing: content;"
	{rows}
	{required}
	{readonly}
	oninput={(e) => {
		resize();
	}}
	onfocus={() => {
		resize();
	}}
	onblur={onBlur}></textarea>
