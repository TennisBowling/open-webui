<script lang="ts">
	import { getContext } from 'svelte';

	const i18n = getContext('i18n');

	import { mobile } from '$lib/stores';
	import { WEBUI_BASE_URL } from '$lib/constants';
	import { imageFallback } from '$lib/actions/imageFallback';

	import Tooltip from '$lib/components/common/Tooltip.svelte';
	import PinSlash from '$lib/components/icons/PinSlash.svelte';

	interface Props {
		model?: any;
		shiftKey?: boolean;
		onClick?: any;
		onUnpin?: any;
	}

	let { model = null, shiftKey = false, onClick = () => {}, onUnpin = () => {} }: Props = $props();

	let mouseOver = $state(false);
</script>

{#if model}
	<!-- svelte-ignore a11y_no_static_element_interactions -->
	<div
		class="px-[7px] flex justify-center text-gray-800 dark:text-gray-200 cursor-grab relative group"
		data-id={model?.id}
		onmouseenter={(e) => {
			mouseOver = true;
		}}
		onmouseleave={(e) => {
			mouseOver = false;
		}}
	>
		<a
			class="grow flex items-center space-x-3 rounded-xl px-2.5 py-2 group-hover:bg-manilla/20 dark:group-hover:bg-manilla-dark/50 transition"
			href="/?model={model?.id}"
			onclick={onClick}
			draggable="false"
		>
			<div class="self-center shrink-0">
				<img
					use:imageFallback
					src={model?.info?.meta?.profile_image_url ?? `${WEBUI_BASE_URL}/static/favicon.png`}
					class=" size-5 rounded-full -translate-x-[0.5px]"
					alt="logo"
					loading="lazy"
					decoding="async"
				/>
			</div>

			<div class="flex self-center translate-y-[0.5px]">
				<div class=" self-center text-sm font-primary line-clamp-1">
					{model?.name ?? model.id}
				</div>
			</div>
		</a>

		{#if $mobile || (mouseOver && shiftKey)}
			<div class="absolute right-5 top-2.5">
				<div class=" flex items-center self-center space-x-1.5">
					<Tooltip content={$i18n.t('Unpin')} className="flex items-center">
						<button
							class="tap-target self-center text-gray-500 hover:text-gray-700 dark:text-gray-400 dark:hover:text-white transition"
							onclick={() => {
								onUnpin();
							}}
							type="button"
						>
							<PinSlash className="size-3.5" strokeWidth="2" />
						</button>
					</Tooltip>
				</div>
			</div>
		{/if}
	</div>
{/if}
