<script lang="ts">
	import { getContext, onMount } from 'svelte';
	import { toast } from '$lib/utils/toast';

	import {
		deleteMCPConnection,
		getMCPConnections,
		getMCPConnectionTemplates,
		startMCPConnectionOAuth,
		restartMCPConnection,
		verifyMCPConnection
	} from '$lib/apis/mcp';
	import { tools, user } from '$lib/stores';
	import { getTools } from '$lib/apis/tools';

	import Spinner from '$lib/components/common/Spinner.svelte';
	import Tooltip from '$lib/components/common/Tooltip.svelte';
	import ConfirmDialog from '$lib/components/common/ConfirmDialog.svelte';
	import Plus from '$lib/components/icons/Plus.svelte';
	import Link from '$lib/components/icons/Link.svelte';
	import ArrowPath from '$lib/components/icons/ArrowPath.svelte';
	import GarbageBin from '$lib/components/icons/GarbageBin.svelte';
	import WrenchAlt from '$lib/components/icons/WrenchAlt.svelte';
	import ToolIcon from '$lib/components/common/ToolIcon.svelte';
	import Cog6 from '$lib/components/icons/Cog6.svelte';
	import AddMCPConnectionModal from './AddMCPConnectionModal.svelte';

	const i18n = getContext('i18n');

	let loading = $state(true);
	let connections: any[] = $state([]);
	let templates: Record<string, any> = $state({});

	let showAddModal = $state(false);
	let showEditModal = $state(false);
	let editConnection: any = $state(null);
	let verifyingId = $state('');
	let restartingId = $state('');

	let showDeleteConfirm = $state(false);
	let pendingDelete: any = $state(null);

	let isAdmin = $derived($user?.role === 'admin');
	let canCustomStdio = $derived(isAdmin || !!$user?.permissions?.features?.mcp_stdio_custom);

	const refreshTools = async () => {
		tools.set(await getTools(localStorage.token));
	};

	const onSaved = async () => {
		await load();
		await refreshTools();
	};

	const openEdit = (connection: any) => {
		editConnection = connection;
		showEditModal = true;
	};

	const load = async () => {
		loading = true;
		try {
			[connections, templates] = await Promise.all([
				getMCPConnections(localStorage.token),
				getMCPConnectionTemplates(localStorage.token)
			]);
		} finally {
			loading = false;
		}
	};

	const transportLabel = (connection: any) => {
		if (connection.transport === 'stdio') {
			return templates[connection?.meta?.template]?.name ?? $i18n.t('Local stdio');
		}
		if (connection.transport === 'remote_sse') return $i18n.t('Remote SSE');
		return $i18n.t('Remote HTTP');
	};

	const errText = (err: any): string => {
		const d = err?.detail ?? err;
		if (typeof d === 'string') return d;
		if (Array.isArray(d)) return d.map((e) => e?.msg ?? `${e}`).join(', ');
		// Network-level fetch failures throw an Error whose enumerable props are
		// empty, so JSON.stringify would render a useless "{}". Show the message.
		if (d instanceof Error) return d.message;
		try {
			return JSON.stringify(d);
		} catch (e) {
			return `${d}`;
		}
	};

	const verify = async (connection: any) => {
		verifyingId = connection.id;
		try {
			const res = await verifyMCPConnection(localStorage.token, connection.id);
			if (res?.auth_required && res?.authorization_url) {
				toast.info(
					$i18n.t('Sign-in required for {{name}} — opening authorization…', {
						name: connection.name ?? connection.id
					})
				);
				window.open(res.authorization_url, '_self', 'noopener');
				return;
			}
			const count = res?.specs?.length ?? 0;
			const hidden = res?.hidden_by_policy ?? 0;
			if (count === 0 && hidden > 0) {
				toast.info(
					$i18n.t(
						'{{name}} works, but all {{hidden}} of its tools count as write/destructive (no read-only annotations) and are hidden. Turn on "Enable write / destructive tools" in its settings to use them.',
						{ name: connection.name, hidden }
					)
				);
				return;
			}
			if (hidden > 0) {
				toast.success(
					$i18n.t(
						'{{name}} is working — {{count}} tool(s) available, {{hidden}} hidden by the read-only policy.',
						{ name: connection.name, count, hidden }
					)
				);
				return;
			}
			toast.success(
				$i18n.t('{{name}} is working — {{count}} tool(s) available.', {
					name: connection.name,
					count
				})
			);
		} catch (err: any) {
			toast.error(errText(err));
		} finally {
			verifyingId = '';
		}
	};

	const restart = async (connection: any) => {
		restartingId = connection.id;
		try {
			const res = await restartMCPConnection(localStorage.token, connection.id);
			toast.success(
				$i18n.t('{{name}} restarted — {{count}} tool(s) available.', {
					name: connection.name,
					count: res?.specs?.length ?? 0
				})
			);
		} catch (err: any) {
			toast.error(errText(err));
		} finally {
			restartingId = '';
		}
	};

	const connectOAuth = async (connection: any) => {
		try {
			const returnTo = `${location.pathname}${location.search}`;
			const res = await startMCPConnectionOAuth(localStorage.token, connection.id, returnTo);
			if (res?.authorization_url) {
				window.open(res.authorization_url, '_self', 'noopener');
			} else {
				toast.error($i18n.t('Could not start sign-in for {{name}}.', { name: connection.name }));
			}
		} catch (err: any) {
			toast.error(errText(err));
		}
	};

	const confirmRemove = (connection: any) => {
		pendingDelete = connection;
		showDeleteConfirm = true;
	};

	const remove = async () => {
		if (!pendingDelete) return;
		try {
			await deleteMCPConnection(localStorage.token, pendingDelete.id);
			await load();
			await refreshTools();
		} catch (err: any) {
			toast.error(errText(err));
		} finally {
			pendingDelete = null;
		}
	};

	onMount(load);
</script>

<ConfirmDialog
	bind:show={showDeleteConfirm}
	title={$i18n.t('Delete MCP connection')}
	message={$i18n.t('Delete “{{name}}”? This removes the connection and any saved authorization.', {
		name: pendingDelete?.name ?? ''
	})}
	confirmLabel={$i18n.t('Delete')}
	onConfirm={remove}
/>

<AddMCPConnectionModal bind:show={showAddModal} {templates} {isAdmin} {canCustomStdio} {onSaved} />
<AddMCPConnectionModal
	bind:show={showEditModal}
	edit={true}
	connection={editConnection}
	{templates}
	{isAdmin}
	{canCustomStdio}
	{onSaved}
/>

<div class="mt-4 pt-3 border-t border-gray-100 dark:border-gray-850">
	<div class="flex items-center justify-between gap-2 mb-0.5">
		<div class="font-medium">{$i18n.t('Personal MCP Connections')}</div>
		<button
			type="button"
			class="flex items-center gap-1 text-xs px-2.5 py-1 rounded-full border border-gray-100 dark:border-gray-800 hover:bg-gray-50 dark:hover:bg-gray-850 transition"
			onclick={() => (showAddModal = true)}
		>
			<Plus className="size-3.5" />
			{$i18n.t('Add')}
		</button>
	</div>
	<div class="text-xs text-gray-500 mb-2.5">
		{$i18n.t('Connect remote OAuth MCP servers (Notion, Linear, …) or isolated local stdio tools.')}
	</div>

	{#if loading}
		<div class="py-4 flex justify-center"><Spinner className="size-5" /></div>
	{:else if connections.length === 0}
		<button
			type="button"
			class="w-full rounded-xl border border-dashed border-gray-200 dark:border-gray-800 px-3 py-6 text-center text-sm text-gray-500 hover:bg-gray-50 dark:hover:bg-gray-850 transition"
			onclick={() => (showAddModal = true)}
		>
			<div class="flex justify-center mb-1.5 text-gray-400"><WrenchAlt className="size-5" /></div>
			{$i18n.t('No MCP connections yet — add one to give the model new tools.')}
		</button>
	{:else}
		<div class="flex flex-col gap-1.5">
			{#each connections as connection (connection.id)}
				<div
					class="group rounded-xl border border-gray-100 dark:border-gray-800 px-3 py-2.5 flex items-center gap-3"
				>
					<div class="shrink-0 text-gray-400">
						<ToolIcon src={connection?.meta?.icon} alt={connection.name ?? ''} />
					</div>

					<div class="min-w-0 flex-1">
						<div class="flex items-center gap-2">
							<div class="text-sm font-medium truncate">{connection.name}</div>
							{#if connection.auth_type === 'oauth_2.1'}
								{#if connection.authenticated}
									<span
										class="shrink-0 inline-flex items-center gap-1 text-[10px] font-medium px-1.5 py-0.5 rounded-full bg-success/10 border-hairline border-success/25 text-success dark:text-success-dark"
									>
										<span class="size-1.5 rounded-full bg-success dark:bg-success-dark"></span>
										{$i18n.t('Connected')}
									</span>
								{:else}
									<span
										class="shrink-0 text-[10px] font-medium px-1.5 py-0.5 rounded-full bg-warning/10 border-hairline border-warning/25 text-warning dark:text-warning-dark"
									>
										{$i18n.t('Sign in required')}
									</span>
								{/if}
							{/if}
						</div>
						<div class="text-xs text-gray-500 truncate">
							{transportLabel(connection)}{connection.url
								? ` · ${connection.url}`
								: connection.command
									? ` · ${[connection.command, ...(connection.args ?? [])].join(' ')}`
									: ''}
						</div>
					</div>

					<div class="flex items-center gap-0.5 shrink-0">
						{#if connection.transport === 'stdio'}
							<Tooltip content={$i18n.t('Restart server')}>
								<button
									type="button"
									aria-label={$i18n.t('Restart server')}
									class="p-1.5 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-800 transition"
									disabled={restartingId === connection.id}
									onclick={() => restart(connection)}
								>
									{#if restartingId === connection.id}<Spinner
											className="size-4"
										/>{:else}<ArrowPath className="size-4" />{/if}
								</button>
							</Tooltip>
						{/if}
						{#if connection.auth_type === 'oauth_2.1' && !connection.authenticated}
							<Tooltip content={$i18n.t('Connect')}>
								<button
									type="button"
									aria-label={$i18n.t('Connect')}
									class="p-1.5 rounded-lg text-book-cloth dark:text-kraft hover:bg-book-cloth/10 transition"
									onclick={() => connectOAuth(connection)}
								>
									<Link className="size-4" />
								</button>
							</Tooltip>
						{/if}
						<Tooltip content={$i18n.t('Configure tools & headers')}>
							<button
								type="button"
								aria-label={$i18n.t('Configure tools & headers')}
								class="p-1.5 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-800 transition"
								onclick={() => openEdit(connection)}
							>
								<Cog6 className="size-4" />
							</button>
						</Tooltip>
						<Tooltip content={$i18n.t('Test connection')}>
							<button
								type="button"
								aria-label={$i18n.t('Test connection')}
								class="p-1.5 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-800 transition {verifyingId ===
								connection.id
									? 'cursor-wait opacity-60'
									: ''}"
								disabled={verifyingId === connection.id}
								onclick={() => verify(connection)}
							>
								{#if verifyingId === connection.id}
									<Spinner className="size-4" />
								{:else}
									<ArrowPath className="size-4" />
								{/if}
							</button>
						</Tooltip>
						<Tooltip content={$i18n.t('Delete')}>
							<button
								type="button"
								aria-label={$i18n.t('Delete')}
								class="p-1.5 rounded-lg text-gray-400 hover:text-error-brick dark:hover:text-error-brick-dark hover:bg-error-brick/10 transition"
								onclick={() => confirmRemove(connection)}
							>
								<GarbageBin className="size-4" />
							</button>
						</Tooltip>
					</div>
				</div>
			{/each}
		</div>
	{/if}
</div>
