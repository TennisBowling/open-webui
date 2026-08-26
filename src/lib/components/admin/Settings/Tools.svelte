<script lang="ts">
	import { preventDefault } from '$lib/utils/eventModifiers';

	import { toast } from '$lib/utils/toast';
	import { onMount, getContext, tick } from 'svelte';
	import { getModels as _getModels } from '$lib/apis';
	const i18n = getContext('i18n');

	import { models, settings, user } from '$lib/stores';

	import Switch from '$lib/components/common/Switch.svelte';
	import Spinner from '$lib/components/common/Spinner.svelte';
	import Tooltip from '$lib/components/common/Tooltip.svelte';
	import Plus from '$lib/components/icons/Plus.svelte';
	import Connection from '$lib/components/chat/Settings/Tools/Connection.svelte';

	import AddToolServerModal from '$lib/components/AddToolServerModal.svelte';
	import {
		getToolServerConnections,
		restartToolServerConnection,
		setToolServerConnections
	} from '$lib/apis/configs';

	interface Props {
		saveSettings: Function;
	}

	let { saveSettings }: Props = $props();

	let servers = $state(null);
	let mcpToolCallTimeout = $state(900);
	let showConnectionModal = $state(false);

	const addConnectionHandler = async (server) => {
		servers = [...servers, server];
		await updateHandler();
	};

	const updateHandler = async () => {
		const res = await setToolServerConnections(localStorage.token, {
			TOOL_SERVER_CONNECTIONS: servers,
			MCP_TOOL_CALL_TIMEOUT: Number.isFinite(Number(mcpToolCallTimeout))
				? Math.max(0, Math.floor(Number(mcpToolCallTimeout)))
				: 900
		}).catch((err) => {
			toast.error($i18n.t('Failed to save connections'));

			return null;
		});

		if (res) {
			toast.success($i18n.t('Connections saved successfully'));
		}
	};

	const restartHandler = async (server) => {
		try {
			const res = await restartToolServerConnection(localStorage.token, server?.info?.id);
			toast.success(
				$i18n.t('{{name}} restarted — {{count}} tool(s) available.', {
					name: server?.info?.name ?? server?.info?.id,
					count: res?.specs?.length ?? 0
				})
			);
		} catch (err) {
			toast.error(err?.detail ?? `${err}`);
		}
	};

	onMount(async () => {
		const res = await getToolServerConnections(localStorage.token);
		servers = res.TOOL_SERVER_CONNECTIONS;
		mcpToolCallTimeout = res.MCP_TOOL_CALL_TIMEOUT ?? 900;
	});
</script>

<AddToolServerModal bind:show={showConnectionModal} onSubmit={addConnectionHandler} />

<form
	class="flex flex-col h-full justify-between text-sm"
	onsubmit={preventDefault(() => {
		updateHandler();
	})}
>
	<div class=" overflow-y-scroll scrollbar-hidden h-full">
		{#if servers !== null}
			<div class="">
				<div class="mb-3">
					<div class=" mb-2.5 text-base font-medium">{$i18n.t('General')}</div>

					<hr class=" border-gray-100 dark:border-gray-850 my-2" />

					<div class="mb-3 flex flex-col gap-1.5">
						<label class="text-xs font-medium" for="mcp-tool-call-timeout">
							{$i18n.t('Default MCP Tool Timeout')}
						</label>
						<div class="flex items-center gap-2">
							<input
								id="mcp-tool-call-timeout"
								class="w-28 rounded-lg border border-gray-200 dark:border-gray-800 bg-transparent px-2 py-1 text-sm outline-hidden"
								type="number"
								min="0"
								step="1"
								bind:value={mcpToolCallTimeout}
							/>
							<span class="text-xs text-gray-500">{$i18n.t('seconds')}</span>
						</div>
						<div class="text-xs text-gray-500 dark:text-gray-400 leading-relaxed">
							{$i18n.t(
								'Applies to MCP tools by default. Set 0 for no generic timeout. bash, web_search, and web_fetch are always exempt.'
							)}
						</div>
					</div>

					<div class="mb-2.5 flex flex-col w-full justify-between">
						<!-- {$i18n.t(`Failed to connect to {{URL}} OpenAPI tool server`, {
							URL: 'server?.url'
						})} -->
						<div class="flex justify-between items-center mb-0.5">
							<div class="font-medium">{$i18n.t('Manage Tool Servers')}</div>

							<Tooltip content={$i18n.t(`Add Connection`)}>
								<button
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

						<div class="flex flex-col gap-1">
							{#each servers as server, idx}
								<Connection
									bind:connection={servers[idx]}
									onRestart={restartHandler}
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

						<div class="my-1.5">
							<div class="text-xs text-gray-500">
								{$i18n.t('Connect to your own OpenAPI compatible external tool servers.')}
							</div>
						</div>
					</div>

					<!-- <div class="mb-2.5 flex w-full justify-between">
						<div class=" text-xs font-medium">{$i18n.t('Arena Models')}</div>

						<Tooltip content={$i18n.t(`Message rating should be enabled to use this feature`)}>
							<Switch bind:state={evaluationConfig.ENABLE_EVALUATION_ARENA_MODELS} />
						</Tooltip>
					</div> -->
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
