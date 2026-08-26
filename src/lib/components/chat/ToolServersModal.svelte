<script lang="ts">
	import { getContext } from 'svelte';
	import type { Writable } from 'svelte/store';
	import type { i18n as i18nType } from 'i18next';
	import { toolServers, toolServersLoaded, tools } from '$lib/stores';
	import { loadToolServers } from '$lib/utils/toolServers';

	import { toast } from '$lib/utils/toast';

	import Modal from '../common/Modal.svelte';
	import Collapsible from '../common/Collapsible.svelte';
	import Spinner from '../common/Spinner.svelte';
	import ToolIcon from '../common/ToolIcon.svelte';
	import XMark from '$lib/components/icons/XMark.svelte';

	interface Props {
		show?: boolean;
		selectedToolIds?: string[];
	}

	let { show = $bindable(false), selectedToolIds = [] }: Props = $props();

	let selectedTools: any[] = $state([]);

	$effect(() => {
		selectedTools = ($tools ?? []).filter((tool) => selectedToolIds.includes(tool.id));
	});

	const i18n: Writable<i18nType> = getContext('i18n');

	let loadPromise: Promise<any[]> | null = $state(null);
	$effect(() => {
		if (show && !$toolServersLoaded && !loadPromise) {
			loadPromise = loadToolServers({
				onError: (data) => {
					toast.error(
						$i18n.t(`Failed to connect to {{URL}} OpenAPI tool server`, {
							URL: data?.url
						})
					);
				}
			}).finally(() => {
				loadPromise = null;
			});
		}
	});
</script>

<Modal bind:show size="md">
	<div>
		<div class=" flex justify-between dark:text-gray-300 px-5 pt-4 pb-0.5">
			<div class=" text-lg font-medium self-center">{$i18n.t('Available Tools')}</div>
			<button
				class="self-center p-1 rounded-full text-gray-500 hover:text-gray-700 dark:text-gray-400 dark:hover:text-gray-200 hover:bg-gray-100 dark:hover:bg-gray-800 transition"
				onclick={() => {
					show = false;
				}}
			>
				<XMark className={'size-5'} />
			</button>
		</div>

		{#if selectedTools.length > 0}
			{#if $toolServers.length > 0}
				<div class=" flex justify-between dark:text-gray-300 px-5 pb-1">
					<div class=" text-base font-medium self-center">{$i18n.t('Tools')}</div>
				</div>
			{/if}

			<div class="px-5 pb-3 w-full flex flex-col justify-center">
				<div class=" text-sm dark:text-gray-300 mb-1">
					{#each selectedTools as tool}
						<Collapsible buttonClassName="w-full mb-0.5">
							<div class="flex items-center gap-2 min-w-0 text-left">
								<div class="shrink-0 text-gray-400">
									<ToolIcon src={tool?.meta?.icon} alt={tool?.name ?? ''} />
								</div>

								<div class="truncate">
									<div class="text-sm font-medium dark:text-gray-100 text-gray-800 truncate">
										{tool?.name}
									</div>

									{#if tool?.meta?.description}
										<div class="text-xs text-gray-500">
											{tool?.meta?.description}
										</div>
									{/if}
								</div>
							</div>

							<!-- <div slot="content">
							{JSON.stringify(tool, null, 2)}
						</div> -->
						</Collapsible>
					{/each}
				</div>
			</div>
		{/if}

		{#if !$toolServersLoaded}
			<div class="px-5 pb-5 w-full flex justify-center">
				<Spinner className="size-5" />
			</div>
		{:else if $toolServers.length > 0}
			<div class=" flex justify-between dark:text-gray-300 px-5 pb-0.5">
				<div class=" text-base font-medium self-center">{$i18n.t('Tool Servers')}</div>
			</div>

			<div class="px-5 pb-5 w-full flex flex-col justify-center">
				<div class=" text-xs text-gray-600 dark:text-gray-300 mb-2">
					{$i18n.t('{{WEBUI_NAME}} can use tools provided by any OpenAPI server.')} <br /><a
						class="underline"
						href="https://github.com/open-webui/openapi-servers"
						target="_blank">{$i18n.t('Learn more about OpenAPI tool servers.')}</a
					>
				</div>
				<div class=" text-sm dark:text-gray-300 mb-1">
					{#each $toolServers as toolServer}
						<Collapsible buttonClassName="w-full" chevron>
							<div class="flex items-center gap-2 min-w-0 text-left">
								<div class="shrink-0 text-gray-400">
									<ToolIcon src={toolServer?.icon} alt={toolServer?.openapi?.info?.title ?? ''} />
								</div>

								<div class="min-w-0">
									<div class="text-sm font-medium dark:text-gray-100 text-gray-800">
										{toolServer?.openapi?.info?.title} - v{toolServer?.openapi?.info?.version}
									</div>

									<div class="text-xs text-gray-500">
										{toolServer?.openapi?.info?.description}
									</div>

									<div class="text-xs text-gray-500">
										{toolServer?.url}
									</div>
								</div>
							</div>

							{#snippet content()}
								<div>
									{#each toolServer?.specs ?? [] as tool_spec}
										<div class="my-1">
											<div class="font-medium text-gray-800 dark:text-gray-100">
												{tool_spec?.name}
											</div>

											<div>
												{tool_spec?.description}
											</div>
										</div>
									{/each}
								</div>
							{/snippet}
						</Collapsible>
					{/each}
				</div>
			</div>
		{/if}
	</div>
</Modal>
