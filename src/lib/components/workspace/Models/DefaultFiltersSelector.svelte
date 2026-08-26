<script lang="ts">
	import { getContext, onMount } from 'svelte';
	import Checkbox from '$lib/components/common/Checkbox.svelte';
	import Tooltip from '$lib/components/common/Tooltip.svelte';

	const i18n = getContext('i18n');

	let { filters = [], selectedFilterIds = $bindable([]) } = $props();

	let _filters = $state({});

	onMount(() => {
		_filters = filters.reduce((acc, filter) => {
			acc[filter.id] = {
				...filter,
				selected: selectedFilterIds.includes(filter.id)
			};

			return acc;
		}, {});
	});
</script>

<div>
	<div class="flex w-full justify-between mb-1">
		<div class=" self-center text-sm font-semibold">{$i18n.t('Default Filters')}</div>
	</div>

	<div class="flex flex-col">
		{#if filters.length > 0}
			<div class=" flex items-center flex-wrap">
				{#each Object.keys(_filters) as filter, filterIdx}
					<div class=" flex items-center gap-2 mr-3">
						<div class="self-center flex items-center">
							<Checkbox
								state={_filters[filter].selected ? 'checked' : 'unchecked'}
								onchange={(state) => {
									_filters[filter].selected = state === 'checked';
									selectedFilterIds = Object.keys(_filters).filter((t) => _filters[t].selected);
								}}
							/>
						</div>

						<div class=" py-0.5 text-sm w-full capitalize font-medium">
							<Tooltip content={_filters[filter].meta.description}>
								{_filters[filter].name}
							</Tooltip>
						</div>
					</div>
				{/each}
			</div>
		{/if}
	</div>
</div>
