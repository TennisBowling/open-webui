<script lang="ts">
	import { onDestroy, onMount } from 'svelte';
	import { tick, getContext } from 'svelte';

	import { models } from '$lib/stores';
	import { WEBUI_BASE_URL } from '$lib/constants';
	import Tooltip from '$lib/components/common/Tooltip.svelte';
	import { imageFallback } from '$lib/actions/imageFallback';

	const i18n = getContext('i18n');

	let selectedIdx = $state(0);
	interface Props {
		query?: string;
		onSelect?: any;
		filteredItems?: any;
	}

	let { query = '', onSelect = (e) => {}, filteredItems = $bindable([]) }: Props = $props();

	let fuse = $state(null);

	// fuse.js is lazy-loaded (kept off the cold-load path). The search below guards
	// on `fuse`, so the list is unfiltered for the one tick before it loads — which
	// happens as the command palette mounts, before the user finishes typing.
	const loadFuse = async () => {
		const { default: Fuse } = await import('fuse.js');
		fuse = new Fuse(
			$models
				.filter((model) => !model?.info?.meta?.hidden)
				.map((model) => {
					const _item = {
						...model,
						modelName: model?.name,
						tags: model?.info?.meta?.tags?.map((tag) => tag.name).join(' '),
						desc: model?.info?.meta?.description
					};
					return _item;
				}),
			{
				keys: ['value', 'tags', 'modelName'],
				threshold: 0.5
			}
		);
	};
	loadFuse();

	$effect(() => {
		filteredItems =
			query && fuse
				? fuse.search(query).map((e) => {
						return e.item;
					})
				: $models.filter((model) => !model?.info?.meta?.hidden);
	});

	$effect(() => {
		if (query) {
			selectedIdx = 0;
		}
	});

	export const selectUp = () => {
		selectedIdx = Math.max(0, selectedIdx - 1);
	};

	export const selectDown = () => {
		selectedIdx = Math.min(selectedIdx + 1, filteredItems.length - 1);
	};

	export const select = async () => {
		const model = filteredItems[selectedIdx];
		if (model) {
			onSelect({ type: 'model', data: model });
		}
	};
</script>

<div class="px-2 text-xs text-gray-500 py-1">
	{$i18n.t('Models')}
</div>

{#if filteredItems.length > 0}
	{#each filteredItems as model, modelIdx}
		<Tooltip content={model.id} placement="top-start">
			<button
				class="px-2.5 py-1.5 rounded-xl w-full text-left {modelIdx === selectedIdx
					? 'bg-gray-50 dark:bg-gray-800 selected-command-option-button'
					: ''}"
				type="button"
				onclick={() => {
					onSelect({ type: 'model', data: model });
				}}
				onmousemove={() => {
					selectedIdx = modelIdx;
				}}
				onfocus={() => {}}
				data-selected={modelIdx === selectedIdx}
			>
				<div class="flex text-gray-900 dark:text-gray-100 line-clamp-1">
					<img
						use:imageFallback
						src={model?.info?.meta?.profile_image_url ?? `${WEBUI_BASE_URL}/static/favicon.png`}
						alt={model?.name ?? model.id}
						class="rounded-full size-5 items-center mr-2"
						loading="lazy"
						decoding="async"
					/>
					<div class="truncate">
						{model.name}
					</div>
				</div>
			</button>
		</Tooltip>
	{/each}
{/if}
