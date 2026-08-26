<script lang="ts">
	import { getContext, tick } from 'svelte';
	const i18n = getContext('i18n');

	import Tooltip from '$lib/components/common/Tooltip.svelte';
	import SensitiveInput from '$lib/components/common/SensitiveInput.svelte';
	import Cog6 from '$lib/components/icons/Cog6.svelte';
	import ConfirmDialog from '$lib/components/common/ConfirmDialog.svelte';
	import AddToolServerModal from '$lib/components/AddToolServerModal.svelte';
	import ToolIcon from '$lib/components/common/ToolIcon.svelte';
	import ArrowPath from '$lib/components/icons/ArrowPath.svelte';
	import Spinner from '$lib/components/common/Spinner.svelte';

	interface Props {
		onDelete?: any;
		onSubmit?: any;
		onRestart?: any;
		connection?: any;
		direct?: boolean;
	}

	let {
		onDelete = () => {},
		onSubmit = () => {},
		onRestart = null,
		connection = $bindable(null),
		direct = false
	}: Props = $props();

	let showConfigModal = $state(false);
	let showDeleteConfirmDialog = $state(false);
	let restarting = $state(false);
</script>

<AddToolServerModal
	edit
	{direct}
	bind:show={showConfigModal}
	{connection}
	onDelete={() => {
		showDeleteConfirmDialog = true;
	}}
	onSubmit={(c) => {
		connection = c;
		onSubmit(c);
	}}
/>

<ConfirmDialog
	bind:show={showDeleteConfirmDialog}
	onconfirm={() => {
		onDelete();
		showConfigModal = false;
	}}
/>

<div class="flex w-full gap-2 items-center min-w-0">
	<Tooltip className="w-full relative min-w-0" content={''} placement="top-start">
		<div class="flex w-full min-w-0">
			<div
				class="flex-1 min-w-0 relative flex gap-1.5 items-center {!(
					connection?.config?.enable ?? true
				)
					? 'opacity-50'
					: ''}"
			>
				<Tooltip content={connection?.type === 'mcp' ? $i18n.t('MCP') : $i18n.t('OpenAPI')}>
					<div class="shrink-0">
						<ToolIcon
							src={connection?.info?.icon}
							alt={connection?.info?.name ?? connection?.url ?? ''}
						/>
					</div>
				</Tooltip>

				{#if connection?.info?.name}
					<div class="capitalize outline-hidden w-full min-w-0 truncate bg-transparent">
						{connection?.info?.name ?? connection?.url}
						<span class="text-gray-500">{connection?.info?.id ?? ''}</span>
					</div>
				{:else}
					<div class="min-w-0 truncate">
						{connection?.url}
					</div>
				{/if}
			</div>
		</div>
	</Tooltip>

	<div class="flex gap-1">
		{#if onRestart && connection?.type === 'mcp' && connection?.config?.command}
			<Tooltip content={$i18n.t('Restart server')} className="self-start">
				<button
					class="self-center p-1 bg-transparent hover:bg-gray-100 dark:bg-gray-900 dark:hover:bg-gray-850 rounded-lg transition"
					type="button"
					disabled={restarting}
					onclick={async () => {
						restarting = true;
						try {
							await onRestart(connection);
						} finally {
							restarting = false;
						}
					}}
				>
					{#if restarting}<Spinner className="size-5" />{:else}<ArrowPath className="size-5" />{/if}
				</button>
			</Tooltip>
		{/if}
		<Tooltip content={$i18n.t('Configure')} className="self-start">
			<button
				class="self-center p-1 bg-transparent hover:bg-gray-100 dark:bg-gray-900 dark:hover:bg-gray-850 rounded-lg transition"
				onclick={() => {
					showConfigModal = true;
				}}
				type="button"
			>
				<Cog6 />
			</button>
		</Tooltip>
	</div>
</div>
