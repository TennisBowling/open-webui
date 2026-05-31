<script lang="ts">
	import { getContext, onMount } from 'svelte';
	import { toast } from 'svelte-sonner';
	import {
		createMCPConnection,
		deleteMCPConnection,
		discoverMCP,
		getMCPConnections,
		getMCPConnectionTemplates,
		startMCPConnectionOAuth,
		verifyMCPConnection
	} from '$lib/apis/mcp';
	import { tools } from '$lib/stores';
	import { getTools } from '$lib/apis/tools';
	import Spinner from '$lib/components/common/Spinner.svelte';
	import Switch from '$lib/components/common/Switch.svelte';

	const i18n = getContext('i18n');

	let loading = true;
	let saving = false;
	let connections: any[] = [];
	let templates: Record<string, any> = {};

	let name = '';
	let transport = 'remote_http';
	let url = '';
	let auth_type = 'oauth_2.1';
	let key = '';
	let headerText = '';
	let template = 'outlook-assistant';
	let enable_write_tools = false;
	let allow_localhost_oauth = false;
	let envText = '';

	const refreshTools = async () => {
		tools.set(await getTools(localStorage.token));
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

	const parseEnv = () => {
		const env: Record<string, string> = {};
		for (const line of envText.split('\n')) {
			const trimmed = line.trim();
			if (!trimmed || trimmed.startsWith('#')) continue;
			const idx = trimmed.indexOf('=');
			if (idx === -1) continue;
			env[trimmed.slice(0, idx).trim()] = trimmed.slice(idx + 1).trim();
		}
		return env;
	};

	const parseHeaders = () => {
		const headers: { key: string; value: string }[] = [];
		for (const line of headerText.split('\n')) {
			const trimmed = line.trim();
			if (!trimmed || trimmed.startsWith('#')) continue;
			const idx = trimmed.indexOf(':');
			if (idx === -1) continue;
			headers.push({ key: trimmed.slice(0, idx).trim(), value: trimmed.slice(idx + 1).trim() });
		}
		return headers;
	};

	const create = async () => {
		if (!name.trim()) {
			toast.error($i18n.t('Please enter a name'));
			return;
		}
		saving = true;
		try {
			const body: any = {
				name,
				description: transport === 'stdio' ? templates[template]?.name : url,
				transport,
				auth_type: transport === 'stdio' ? 'none' : auth_type,
				url: transport === 'stdio' ? undefined : url,
				key: auth_type === 'bearer' ? key : undefined,
				headers: auth_type === 'headers' ? parseHeaders() : [],
				policy: { enable_write_tools, allow_localhost_oauth },
				meta: transport === 'stdio' ? { template } : {},
				env: parseEnv()
			};
			await createMCPConnection(localStorage.token, body);
			name = '';
			url = '';
			key = '';
			headerText = '';
			envText = '';
			await load();
			await refreshTools();
			toast.success($i18n.t('MCP connection saved'));
		} catch (err: any) {
			toast.error(err?.detail ?? `${err}`);
		} finally {
			saving = false;
		}
	};

	const discover = async () => {
		if (!url.trim()) return;
		try {
			const res = await discoverMCP(localStorage.token, url);
			if (res?.endpoint) url = res.endpoint;
			if (res?.name && !name) name = res.name;
			toast.success($i18n.t('MCP endpoint discovered'));
		} catch (err: any) {
			toast.error(err?.detail ?? `${err}`);
		}
	};

	const verify = async (connection: any) => {
		try {
			const res = await verifyMCPConnection(localStorage.token, connection.id);
			if (res?.auth_required && res?.authorization_url) {
				window.open(res.authorization_url, '_self', 'noopener');
				return;
			}
			toast.success($i18n.t('Connection successful'));
		} catch (err: any) {
			toast.error(err?.detail ?? `${err}`);
		}
	};

	const connectOAuth = async (connection: any) => {
		try {
			const res = await startMCPConnectionOAuth(localStorage.token, connection.id);
			if (res?.authorization_url) window.open(res.authorization_url, '_self', 'noopener');
		} catch (err: any) {
			toast.error(err?.detail ?? `${err}`);
		}
	};

	const remove = async (connection: any) => {
		await deleteMCPConnection(localStorage.token, connection.id);
		await load();
		await refreshTools();
	};

	onMount(load);
</script>

<div class="mt-4 pt-3 border-t border-gray-100 dark:border-gray-850">
	<div class="font-medium mb-1">{$i18n.t('Personal MCP Connections')}</div>
	<div class="text-xs text-gray-500 mb-2">
		{$i18n.t('Connect remote OAuth MCP servers or isolated local stdio MCP templates.')}
	</div>

	{#if loading}
		<div class="py-3"><Spinner className="size-5" /></div>
	{:else}
		<div class="flex flex-col gap-1.5 mb-3">
			{#each connections as connection}
				<div class="rounded-xl border border-gray-100 dark:border-gray-800 px-3 py-2">
					<div class="flex justify-between gap-2">
						<div class="min-w-0">
							<div class="text-sm font-medium truncate">{connection.name}</div>
							<div class="text-xs text-gray-500 truncate">{connection.transport} · {connection.auth_type}</div>
						</div>
						<div class="flex gap-2 text-xs shrink-0">
							{#if connection.auth_type === 'oauth_2.1' && !connection.authenticated}
								<button type="button" class="underline" on:click={() => connectOAuth(connection)}>{$i18n.t('Connect')}</button>
							{/if}
							<button type="button" class="underline" on:click={() => verify(connection)}>{$i18n.t('Verify')}</button>
							<button type="button" class="underline" on:click={() => remove(connection)}>{$i18n.t('Delete')}</button>
						</div>
					</div>
				</div>
			{/each}
		</div>
	{/if}

	<div class="rounded-xl bg-gray-50 dark:bg-gray-950 px-3 py-3 flex flex-col gap-2">
		<input class="text-sm bg-transparent" bind:value={name} placeholder={$i18n.t('Name')} />
		<select class="text-sm bg-transparent" bind:value={transport}>
			<option value="remote_http">Remote HTTP</option>
			<option value="remote_sse">Remote SSE</option>
			<option value="stdio">Local stdio template</option>
		</select>

		{#if transport === 'stdio'}
			<select class="text-sm bg-transparent" bind:value={template}>
				{#each Object.keys(templates) as templateId}
					<option value={templateId}>{templates[templateId]?.name ?? templateId}</option>
				{/each}
			</select>
		{:else}
			<div class="flex gap-2">
				<input class="text-sm bg-transparent flex-1 min-w-0" bind:value={url} placeholder="https://mcp.notion.com/mcp" />
				<button class="text-xs underline shrink-0" type="button" on:click={discover}>{$i18n.t('Discover')}</button>
			</div>
			<select class="text-sm bg-transparent" bind:value={auth_type}>
				<option value="oauth_2.1">OAuth 2.1</option>
				<option value="bearer">Bearer token</option>
				<option value="headers">Custom headers</option>
				<option value="none">None</option>
			</select>
			{#if auth_type === 'bearer'}
				<input class="text-sm bg-transparent" bind:value={key} placeholder={$i18n.t('Bearer token')} />
			{:else if auth_type === 'headers'}
				<textarea class="text-sm bg-transparent min-h-16" bind:value={headerText} placeholder={'X-API-Key: ...'} />
			{/if}
		{/if}

		<textarea class="text-sm bg-transparent min-h-20" bind:value={envText} placeholder={'ENV_KEY=value\nOUTLOOK_CLIENT_ID=...'} />

		<div class="flex justify-between items-center text-xs">
			<span>{$i18n.t('Enable write/destructive tools')}</span>
			<Switch bind:state={enable_write_tools} />
		</div>
		<div class="flex justify-between items-center text-xs">
			<span>{$i18n.t('Allow localhost OAuth metadata')}</span>
			<Switch bind:state={allow_localhost_oauth} />
		</div>

		<div class="flex justify-end">
			<button class="px-3 py-1.5 rounded-full bg-black text-white dark:bg-white dark:text-black text-sm" disabled={saving} type="button" on:click={create}>
				{$i18n.t('Add MCP')}
			</button>
		</div>
	</div>
</div>
