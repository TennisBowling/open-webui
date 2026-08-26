<script lang="ts">
	import { dispatchComponentEvent } from '$lib/utils/componentEvents';
	import { getContext } from 'svelte';
	import Search from '$lib/components/icons/Search.svelte';
	import XMark from '$lib/components/icons/XMark.svelte';

	const dispatch = (type: string, detail?: unknown) =>
		dispatchComponentEvent(eventProps, type, detail);
	const i18n = getContext('i18n');

	interface Props {
		placeholder?: string;
		value?: string;
		showClearButton?: boolean;
		onFocus?: () => void;
		onKeydown?: (e: KeyboardEvent) => void;
	}

	let {
		placeholder = '',
		value = $bindable(''),
		showClearButton = false,
		onFocus = () => {},
		onKeydown = () => {},
		...eventProps
	}: Props & Record<string, unknown> = $props();

	const clearSearchInput = () => {
		value = '';
		dispatch('input');
	};
</script>

<div class="px-1 mb-1 flex justify-center space-x-2 relative z-10" id="search-container">
	<div class="flex w-full rounded-xl" id="chat-search">
		<div class="self-center py-1.5 rounded-l-xl bg-transparent dark:text-gray-300">
			<Search className="size-4.5" />
		</div>

		<input
			id="search-input"
			class="w-full rounded-r-xl py-1.5 pl-2.5 text-sm bg-transparent dark:text-gray-300 outline-hidden"
			placeholder={placeholder ? placeholder : $i18n.t('Search')}
			autocomplete="off"
			bind:value
			oninput={() => {
				dispatch('input');
			}}
			onfocus={() => {
				onFocus();
			}}
			onkeydown={onKeydown}
		/>

		{#if showClearButton && value}
			<div class="self-center pl-1.5 rounded-l-xl bg-transparent">
				<button
					class="inline-flex items-center justify-center p-0.5 max-md:p-2 rounded-full hover:bg-gray-100 dark:hover:bg-gray-850 transition"
					onclick={clearSearchInput}
				>
					<XMark className="size-3" strokeWidth="2" />
				</button>
			</div>
		{/if}
	</div>
</div>
