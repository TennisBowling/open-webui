<script lang="ts">
	import { dispatchComponentEvent } from '$lib/utils/componentEvents';
	import type { Banner } from '$lib/types';
	import { onMount, getContext } from 'svelte';
	import { fade } from 'svelte/transition';
	import DOMPurify from 'dompurify';
	import { marked } from 'marked';
	import { WEBUI_BASE_URL } from '$lib/constants';
	import { statusChipClass } from '$lib/utils/statusColors';

	const dispatch = (type: string, detail?: unknown) =>
		dispatchComponentEvent(eventProps, type, detail);
	const i18n = getContext('i18n');

	interface Props {
		banner?: Banner;
		className?: string;
		dismissed?: boolean;
	}

	let {
		banner = {
			id: '',
			type: 'info',
			title: '',
			content: '',
			url: '',
			dismissible: true,
			timestamp: Math.floor(Date.now() / 1000)
		},
		className = 'mx-2 px-2 rounded-lg',
		dismissed = $bindable(false),
		...eventProps
	}: Props & Record<string, unknown> = $props();

	let mounted = $state(false);

	const dismiss = (id) => {
		dismissed = true;
		dispatch('dismiss', id);
	};

	onMount(() => {
		mounted = true;

		console.log('Banner mounted:', banner);
	});
</script>

{#if !dismissed}
	{#if mounted}
		<div
			class="{className} top-0 left-0 right-0 py-1 flex justify-center items-center relative border border-transparent text-gray-800 dark:text-gray-100 bg-transparent backdrop-blur-xl z-30"
			transition:fade={{ delay: 100, duration: 300 }}
		>
			<div class=" flex flex-col md:flex-row md:items-center flex-1 text-sm w-fit gap-1.5">
				<div class="flex justify-between self-start">
					<div
						class=" text-xs font-semibold {statusChipClass[banner.type] ??
							statusChipClass['info']}  w-fit px-2 rounded-md uppercase line-clamp-1 mr-0.5"
					>
						{#if banner.type.toLowerCase() === 'info'}
							{$i18n.t('Info')}
						{:else if banner.type.toLowerCase() === 'warning'}
							{$i18n.t('Warning')}
						{:else if banner.type.toLowerCase() === 'error'}
							{$i18n.t('Error')}
						{:else if banner.type.toLowerCase() === 'success'}
							{$i18n.t('Success')}
						{:else}
							{banner.type}
						{/if}
					</div>

					{#if banner.url}
						<div class="flex md:hidden group w-fit md:items-center">
							<a
								class="text-gray-700 dark:text-white text-xs font-semibold underline"
								href="{WEBUI_BASE_URL}/assets/files/whitepaper.pdf"
								target="_blank"
							>
								{$i18n.t('Learn More')}
							</a>

							<div
								class=" ml-1 text-gray-400 group-hover:text-gray-600 dark:group-hover:text-white"
							>
								<!--  -->
								<svg
									aria-hidden="true"
									xmlns="http://www.w3.org/2000/svg"
									viewBox="0 0 16 16"
									fill="currentColor"
									class="w-4 h-4"
								>
									<path
										fill-rule="evenodd"
										d="M4.22 11.78a.75.75 0 0 1 0-1.06L9.44 5.5H5.75a.75.75 0 0 1 0-1.5h5.5a.75.75 0 0 1 .75.75v5.5a.75.75 0 0 1-1.5 0V6.56l-5.22 5.22a.75.75 0 0 1-1.06 0Z"
										clip-rule="evenodd"
									/>
								</svg>
							</div>
						</div>
					{/if}
				</div>
				<div class="flex-1 text-xs text-gray-700 dark:text-white max-h-60 overflow-y-auto">
					{@html marked.parse(DOMPurify.sanitize((banner?.content ?? '').replace(/\n/g, '<br>')))}
				</div>
			</div>

			{#if banner.url}
				<div class="hidden md:flex group w-fit md:items-center">
					<a
						class="text-gray-700 dark:text-white text-xs font-semibold underline"
						href="/"
						target="_blank"
					>
						{$i18n.t('Learn More')}
					</a>

					<div class=" ml-1 text-gray-400 group-hover:text-gray-600 dark:group-hover:text-white">
						<!--  -->
						<svg
							aria-hidden="true"
							xmlns="http://www.w3.org/2000/svg"
							viewBox="0 0 16 16"
							fill="currentColor"
							class="size-4"
						>
							<path
								fill-rule="evenodd"
								d="M4.22 11.78a.75.75 0 0 1 0-1.06L9.44 5.5H5.75a.75.75 0 0 1 0-1.5h5.5a.75.75 0 0 1 .75.75v5.5a.75.75 0 0 1-1.5 0V6.56l-5.22 5.22a.75.75 0 0 1-1.06 0Z"
								clip-rule="evenodd"
							/>
						</svg>
					</div>
				</div>
			{/if}
			<div class="flex self-start">
				<button
					aria-label={$i18n.t('Close Banner')}
					onclick={() => {
						dismiss(banner.id);
					}}
					class="  -mt-1 -mb-2 -translate-y-[1px] ml-1.5 mr-1 text-gray-400 hover:text-gray-600 dark:hover:text-white"
					>&times;</button
				>
			</div>
		</div>
	{/if}
{/if}
