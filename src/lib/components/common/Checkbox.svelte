<script lang="ts">
	interface Props {
		state?: string;
		indeterminate?: boolean;
		disabled?: boolean;
		onchange?: (state: string) => void;
	}

	// NB: the prop is aliased on purpose. A binding literally named `state`
	// shadows the `$state` rune, so `$state('unchecked')` below silently
	// compiles to `$state()('unchecked')` — a store subscription on the prop —
	// and every render throws "state.subscribe is not a function". The compiler
	// gives no warning for this, so keep the local name off `state`.
	let {
		state: checkedState = 'unchecked',
		indeterminate = false,
		disabled = false,
		onchange
	}: Props = $props();

	let internalState = $state('unchecked');
	$effect.pre(() => {
		internalState = checkedState;
	});
</script>

<button
	class=" outline -outline-offset-1 outline-[1.5px] outline-gray-200 dark:outline-gray-600 {checkedState !==
	'unchecked'
		? 'bg-book-cloth outline-book-cloth '
		: 'hover:outline-gray-500 hover:bg-gray-50 dark:hover:bg-gray-800'} text-white transition-all rounded-sm inline-block w-3.5 h-3.5 relative {disabled
		? 'opacity-50 cursor-not-allowed'
		: ''}"
	onclick={() => {
		if (disabled) return;

		if (internalState === 'unchecked') {
			internalState = 'checked';
			onchange?.(internalState);
		} else if (internalState === 'checked') {
			internalState = 'unchecked';
			if (!indeterminate) {
				onchange?.(internalState);
			}
		} else if (indeterminate) {
			internalState = 'checked';
			onchange?.(internalState);
		}
	}}
	type="button"
	{disabled}
>
	<div class="top-0 left-0 absolute w-full flex justify-center">
		{#if internalState === 'checked'}
			<svg
				class="w-3.5 h-3.5"
				aria-hidden="true"
				xmlns="http://www.w3.org/2000/svg"
				fill="none"
				viewBox="0 0 24 24"
			>
				<path
					stroke="currentColor"
					stroke-linecap="round"
					stroke-linejoin="round"
					stroke-width="3"
					d="m5 12 4.7 4.5 9.3-9"
				/>
			</svg>
		{:else if indeterminate}
			<svg
				class="w-3 h-3.5 text-gray-800 dark:text-white"
				aria-hidden="true"
				xmlns="http://www.w3.org/2000/svg"
				fill="none"
				viewBox="0 0 24 24"
			>
				<path
					stroke="currentColor"
					stroke-linecap="round"
					stroke-linejoin="round"
					stroke-width="3"
					d="M5 12h14"
				/>
			</svg>
		{/if}
	</div>

	<!-- {checked} -->
</button>
