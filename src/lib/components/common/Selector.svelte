<script lang="ts">
	import { Select } from 'bits-ui';
	import { getContext } from 'svelte';
	import { get, type Writable } from 'svelte/store';
	import type { i18n as I18n } from 'i18next';

	import { flyAndScale } from '$lib/utils/transitions';

	import ChevronDown from '../icons/ChevronDown.svelte';
	import Check from '../icons/Check.svelte';
	import Search from '../icons/Search.svelte';

	interface Item {
		value: string;
		label: string;
		[key: string]: unknown;
	}

	interface Props {
		value?: string;
		placeholder?: string;
		searchEnabled?: boolean;
		searchPlaceholder?: string;
		items?: Item[];
		children?: import('svelte').Snippet;
	}

	const i18n = getContext<Writable<I18n>>('i18n');
	let {
		value = $bindable(''),
		placeholder = get(i18n).t('Select a model'),
		searchEnabled = true,
		searchPlaceholder = get(i18n).t('Search a model'),
		items = [
			{ value: 'mango', label: 'Mango' },
			{ value: 'watermelon', label: 'Watermelon' },
			{ value: 'apple', label: 'Apple' },
			{ value: 'pineapple', label: 'Pineapple' },
			{ value: 'orange', label: 'Orange' }
		],
		children
	}: Props = $props();

	let searchValue = $state('');

	let filteredItems = $derived(
		searchValue
			? items.filter((item) => item.value.toLowerCase().includes(searchValue.toLowerCase()))
			: items
	);
</script>

<Select.Root
	{items}
	onOpenChange={() => {
		searchValue = '';
	}}
	selected={items.find((item) => item.value === value)}
	onSelectedChange={(selectedItem) => {
		value = selectedItem.value;
	}}
>
	<Select.Trigger class="relative w-full" aria-label={placeholder}>
		<Select.Value
			class="inline-flex h-input px-0.5 w-full outline-hidden bg-transparent truncate text-lg font-semibold placeholder-gray-400  focus:outline-hidden"
			{placeholder}
		/>
		<ChevronDown className="absolute end-2 top-1/2 -translate-y-[45%] size-3.5" strokeWidth="2.5" />
	</Select.Trigger>
	<Select.Content
		class="w-full rounded-lg  bg-white dark:bg-gray-850 dark:text-white shadow-lg border-hairline border-gray-200 dark:border-gray-700  outline-hidden"
		transition={flyAndScale}
		sideOffset={4}
	>
		{#if children}
			{@render children()}
		{:else}
			{#if searchEnabled}
				<div class="flex items-center gap-2.5 px-5 mt-3.5 mb-3">
					<Search className="size-4" strokeWidth="2.5" />

					<input
						bind:value={searchValue}
						class="w-full text-sm bg-transparent outline-hidden"
						placeholder={searchPlaceholder}
					/>
				</div>

				<hr class="border-gray-100 dark:border-gray-850" />
			{/if}

			<div class="px-3 my-2 max-h-80 overflow-y-auto">
				{#each filteredItems as item}
					<Select.Item
						class="flex w-full font-medium line-clamp-1 select-none items-center rounded-button py-2 pl-3 pr-1.5 text-sm  text-gray-700 dark:text-gray-100  outline-hidden transition-all duration-75 hover:bg-gray-100 dark:hover:bg-gray-800 rounded-lg cursor-pointer data-highlighted:bg-muted"
						value={item.value}
						label={item.label}
					>
						{item.label}

						{#if value === item.value}
							<div class="ml-auto">
								<Check />
							</div>
						{/if}
					</Select.Item>
				{:else}
					<div>
						<div class="block px-5 py-2 text-sm text-gray-700 dark:text-gray-100">
							{$i18n.t('No results found')}
						</div>
					</div>
				{/each}
			</div>
		{/if}
	</Select.Content>
</Select.Root>
