<script lang="ts">
	import { dispatchComponentEvent } from '$lib/utils/componentEvents';
	import { getContext } from 'svelte';
	const dispatch = (type: string, detail?: unknown) =>
		dispatchComponentEvent(eventProps, type, detail);
	const i18n = getContext('i18n');

	import Cog6 from '$lib/components/icons/Cog6.svelte';
	import ArenaModelModal from './ArenaModelModal.svelte';
	let { model, ...eventProps } = $props();

	let showModel = $state(false);
</script>

<ArenaModelModal
	bind:show={showModel}
	edit={true}
	{model}
	onsubmit={async (e) => {
		dispatch('edit', e.detail);
	}}
	ondelete={async () => {
		dispatch('delete');
	}}
/>

<div class="py-0.5">
	<div class="flex justify-between items-center mb-1">
		<div class="flex flex-col flex-1">
			<div class="flex gap-2.5 items-center">
				<img
					src={model.meta.profile_image_url}
					alt={model.name}
					class="size-8 rounded-full object-cover shrink-0"
				/>

				<div class="w-full flex flex-col">
					<div class="flex items-center gap-1">
						<div class=" line-clamp-1">
							{model.name}
						</div>
					</div>

					<div class="flex items-center gap-1">
						<div class=" text-xs w-full text-gray-500 bg-transparent line-clamp-1">
							{model?.meta?.description ?? model.id}
						</div>
					</div>
				</div>
			</div>
		</div>

		<div class="flex items-center">
			<button
				class="self-center w-fit text-sm p-1.5 dark:text-gray-300 dark:hover:text-white hover:bg-gray-100 dark:hover:bg-gray-850 rounded-xl"
				type="button"
				onclick={() => {
					showModel = true;
				}}
			>
				<Cog6 />
			</button>
		</div>
	</div>
</div>
