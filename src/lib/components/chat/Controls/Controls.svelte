<script lang="ts">
	import { dispatchComponentEvent } from '$lib/utils/componentEvents';
	import { getContext } from 'svelte';
	const dispatch = (type: string, detail?: unknown) =>
		dispatchComponentEvent(eventProps, type, detail);
	const i18n = getContext('i18n');

	import XMark from '$lib/components/icons/XMark.svelte';
	import AdvancedParams from '../Settings/Advanced/AdvancedParams.svelte';
	import Valves from '$lib/components/chat/Controls/Valves.svelte';
	import FileItem from '$lib/components/common/FileItem.svelte';
	import Collapsible from '$lib/components/common/Collapsible.svelte';

	import { user, settings } from '$lib/stores';
	let { models = [], chatFiles = $bindable([]), params = $bindable({}), ...eventProps } = $props();

	let showValves = $state(false);
</script>

<div class=" dark:text-white">
	<div class=" flex items-center justify-between dark:text-gray-100 mb-2">
		<div class=" text-lg font-medium self-center font-primary">{$i18n.t('Chat Controls')}</div>
		<button
			class="tap-target self-center p-1 rounded-full text-gray-500 hover:text-gray-700 dark:text-gray-400 dark:hover:text-gray-200 hover:bg-gray-100 dark:hover:bg-gray-800 transition"
			onclick={() => {
				dispatch('close');
			}}
		>
			<XMark className="size-3.5" />
		</button>
	</div>

	{#if $user?.role === 'admin' || ($user?.permissions.chat?.controls ?? true)}
		<div class=" dark:text-gray-200 text-sm font-primary py-0.5 px-0.5">
			{#if chatFiles.length > 0}
				<Collapsible title={$i18n.t('Files')} open={true} buttonClassName="w-full">
					{#snippet content()}
						<div class="flex flex-col gap-1 mt-1.5">
							{#each chatFiles as file, fileIdx}
								<FileItem
									className="w-full"
									item={file}
									edit={true}
									url={file?.url ? file.url : null}
									name={file.name}
									type={file.type}
									size={file?.size}
									dismissible={true}
									small={true}
									ondismiss={() => {
										// Remove the file from the chatFiles array

										chatFiles.splice(fileIdx, 1);
										chatFiles = chatFiles;
									}}
									onclick={() => {
										console.log(file);
									}}
								/>
							{/each}
						</div>
					{/snippet}
				</Collapsible>

				<hr class="my-2 border-gray-50 dark:border-gray-700/10" />
			{/if}

			{#if $user?.role === 'admin' || ($user?.permissions.chat?.valves ?? true)}
				<Collapsible bind:open={showValves} title={$i18n.t('Valves')} buttonClassName="w-full">
					{#snippet content()}
						<div class="text-sm">
							<Valves show={showValves} />
						</div>
					{/snippet}
				</Collapsible>

				<hr class="my-2 border-gray-50 dark:border-gray-700/10" />
			{/if}

			{#if $user?.role === 'admin' || ($user?.permissions.chat?.system_prompt ?? true)}
				<Collapsible title={$i18n.t('System Prompt')} open={true} buttonClassName="w-full">
					{#snippet content()}
						<div class="">
							<textarea
								bind:value={params.system}
								class="w-full text-xs outline-hidden resize-vertical {$settings.highContrastMode
									? 'border-2 border-gray-300 dark:border-gray-700 rounded-lg bg-gray-50 dark:bg-gray-800 p-2.5'
									: 'py-1.5 bg-transparent'}"
								rows="4"
								placeholder={$i18n.t('Enter system prompt')}></textarea>
						</div>
					{/snippet}
				</Collapsible>

				<hr class="my-2 border-gray-50 dark:border-gray-700/10" />
			{/if}

			{#if $user?.role === 'admin' || ($user?.permissions.chat?.params ?? true)}
				<Collapsible title={$i18n.t('Advanced Params')} open={true} buttonClassName="w-full">
					{#snippet content()}
						<div class="text-sm mt-1.5">
							<div>
								<AdvancedParams admin={$user?.role === 'admin'} custom={true} bind:params />
							</div>
						</div>
					{/snippet}
				</Collapsible>
			{/if}
		</div>
	{/if}
</div>
