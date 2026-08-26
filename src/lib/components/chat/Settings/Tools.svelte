<script lang="ts">
	import { preventDefault } from '$lib/utils/eventModifiers';

	import { toast } from '$lib/utils/toast';
	import { onMount, getContext } from 'svelte';
	import type { Writable } from 'svelte/store';
	import type { i18n as i18nType } from 'i18next';
	import { loadToolServers } from '$lib/utils/toolServers';

	const i18n: Writable<i18nType> = getContext('i18n');

	import { settings } from '$lib/stores';

	import Spinner from '$lib/components/common/Spinner.svelte';
	import Tooltip from '$lib/components/common/Tooltip.svelte';
	import Plus from '$lib/components/icons/Plus.svelte';
	import Connection from './Tools/Connection.svelte';
	import PersonalMCPConnections from './Tools/PersonalMCPConnections.svelte';

	import AddToolServerModal from '$lib/components/AddToolServerModal.svelte';

	interface Props {
		saveSettings: Function;
	}

	let { saveSettings }: Props = $props();

	let servers = $state(null);
	let showConnectionModal = $state(false);

	const addConnectionHandler = async (server) => {
		servers = [...servers, server];
		await updateHandler();
	};

	const updateHandler = async () => {
		await saveSettings({
			toolServers: servers
		});

		await loadToolServers({
			force: true,
			onError: (data) => {
				toast.error(
					$i18n.t(`Failed to connect to {{URL}} OpenAPI tool server`, {
						URL: data?.url
					})
				);
			}
		});
	};

	onMount(async () => {
		servers = $settings?.toolServers ?? [];
	});
</script>

<AddToolServerModal bind:show={showConnectionModal} onSubmit={addConnectionHandler} direct />

<form
	id="tab-tools"
	class="flex flex-col h-full justify-between text-sm"
	onsubmit={preventDefault(() => {
		updateHandler();
	})}
>
	<div class=" overflow-y-scroll scrollbar-hidden max-h-[28rem] md:max-h-full">
		{#if servers !== null}
			<div class="">
				<div class="pr-1.5">
					<!-- {$i18n.t(`Failed to connect to {{URL}} OpenAPI tool server`, {
						URL: 'server?.url'
					})} -->
					<div class="">
						<div class="flex justify-between items-center mb-0.5">
							<div class="font-medium">{$i18n.t('Manage Tool Servers')}</div>

							<Tooltip content={$i18n.t(`Add Connection`)}>
								<button
									aria-label={$i18n.t(`Add Connection`)}
									class="px-1"
									onclick={() => {
										showConnectionModal = true;
									}}
									type="button"
								>
									<Plus />
								</button>
							</Tooltip>
						</div>

						<div class="flex flex-col gap-1.5">
							{#each servers as server, idx}
								<Connection
									bind:connection={servers[idx]}
									direct
									onSubmit={() => {
										updateHandler();
									}}
									onDelete={() => {
										servers = servers.filter((_, i) => i !== idx);
										updateHandler();
									}}
								/>
							{/each}
						</div>
					</div>

					<div class="my-1.5">
						<div
							class={`text-xs 
								${($settings?.highContrastMode ?? false) ? 'text-gray-800 dark:text-gray-100' : 'text-gray-500'}`}
						>
							{$i18n.t('Connect to your own OpenAPI compatible external tool servers.')}
							<br />
							{$i18n.t(
								'CORS must be properly configured by the provider to allow requests from {{WEBUI_NAME}}.'
							)}
						</div>
					</div>

					<div class=" text-xs text-gray-600 dark:text-gray-300 mb-2">
						<a
							class="underline"
							href="https://github.com/open-webui/openapi-servers"
							target="_blank">{$i18n.t('Learn more about OpenAPI tool servers.')}</a
						>
					</div>

					<PersonalMCPConnections />
				</div>
			</div>
		{:else}
			<div class="flex h-full justify-center">
				<div class="my-auto">
					<Spinner className="size-6" />
				</div>
			</div>
		{/if}
	</div>

	<div class="flex justify-end pt-3 text-sm font-medium">
		<button
			class="px-3.5 py-1.5 text-sm font-medium bg-book-cloth hover:bg-kraft text-white dark:bg-book-cloth dark:text-white dark:hover:bg-kraft transition-colors duration-200 ease-paper rounded-full"
			type="submit"
		>
			{$i18n.t('Save')}
		</button>
	</div>
</form>
