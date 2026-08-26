<script lang="ts">
	import { getContext } from 'svelte';
	import { toast } from '$lib/utils/toast';

	import {
		createMCPConnection,
		updateMCPConnection,
		discoverMCP,
		getMCPConnectionTools
	} from '$lib/apis/mcp';

	import Modal from '$lib/components/common/Modal.svelte';
	import Switch from '$lib/components/common/Switch.svelte';
	import SensitiveInput from '$lib/components/common/SensitiveInput.svelte';
	import Spinner from '$lib/components/common/Spinner.svelte';
	import Tooltip from '$lib/components/common/Tooltip.svelte';
	import XMark from '$lib/components/icons/XMark.svelte';
	import ToolIcon from '$lib/components/common/ToolIcon.svelte';
	import { ICON_ACCEPT, fileToIconDataUrl } from '$lib/utils/toolIcon';

	const i18n = getContext('i18n');

	interface Props {
		show?: boolean;
		templates?: Record<string, any>;
		isAdmin?: boolean;
		canCustomStdio?: boolean;
		onSaved?: () => void;
		edit?: boolean;
		connection?: any;
	}

	let {
		show = $bindable(false),
		templates = {},
		isAdmin = false,
		canCustomStdio = false,
		onSaved = () => {},
		edit = false,
		connection = null
	}: Props = $props();

	let saving = $state(false);
	let discovering = $state(false);
	let advancedOpen = $state(false);

	// Sentinel template id for "bring your own command" stdio connections.
	const CUSTOM = '__custom__';

	// Form state
	let name = $state('');
	// Optional data URL shown wherever this connection appears. Lives in the
	// connection's free-form `meta`, alongside `template`.
	let icon = $state('');
	let iconInputElement: HTMLInputElement | null = $state(null);
	let transport = $state('remote_http');
	let url = $state('');
	let auth_type = $state('oauth_2.1');
	let key = $state('');
	let headerText = $state('');
	let template = $state('');
	let commandText = $state('');
	let cwd = $state('');
	let envText = $state('');
	let enable_write_tools = $state(false);
	let allow_localhost_oauth = $state(false);

	// Tool-management state (edit mode only)
	let toolsOpen = $state(false);
	let toolsLoading = $state(false);
	let toolsLoaded = $state(false);
	let toolsDirty = $state(false);
	let toolsError = $state('');
	let toolsAuthRequired = $state(false);
	let toolSpecs: any[] = $state([]);
	let toolEnabled: Record<string, boolean> = $state({});

	// Well-known hosted MCP servers (all OAuth + Streamable HTTP). Just a head
	// start — the user can edit the URL after picking one.
	const PRESETS = [
		{ id: 'notion', name: 'Notion', url: 'https://mcp.notion.com/mcp' },
		{ id: 'linear', name: 'Linear', url: 'https://mcp.linear.app/mcp' },
		{ id: 'sentry', name: 'Sentry', url: 'https://mcp.sentry.dev/mcp' }
	];

	// Hydrate (edit) / clear (add) exactly once each time the modal opens.
	let prevShow = $state(false);

	const reset = () => {
		name = '';
		icon = '';
		transport = 'remote_http';
		url = '';
		auth_type = 'oauth_2.1';
		key = '';
		headerText = '';
		template = '';
		commandText = '';
		cwd = '';
		envText = '';
		enable_write_tools = false;
		allow_localhost_oauth = false;
		advancedOpen = false;
		toolsOpen = false;
		toolsLoading = false;
		toolsLoaded = false;
		toolsDirty = false;
		toolsError = '';
		toolsAuthRequired = false;
		toolSpecs = [];
		toolEnabled = {};
	};

	const hydrate = () => {
		reset();
		name = connection?.name ?? '';
		icon = connection?.meta?.icon ?? '';
		transport = connection?.transport ?? 'remote_http';
		url = connection?.url ?? '';
		auth_type = connection?.auth_type ?? 'none';
		template = connection?.meta?.template ?? '';
		if (transport === 'stdio' && !template && connection?.command) {
			template = CUSTOM;
			commandText = formatCommandLine(connection.command, connection?.args ?? []);
			cwd = connection?.cwd ?? '';
		}
		enable_write_tools = !!connection?.policy?.enable_write_tools;
		allow_localhost_oauth = !!connection?.policy?.allow_localhost_oauth;
		headerText = Array.isArray(connection?.headers)
			? connection.headers
					.filter((entry: any) => entry && typeof entry === 'object' && entry.key)
					.map((entry: any) => `${entry.key}: ${entry.value ?? ''}`)
					.join('\n')
			: '';
		envText =
			connection?.env && typeof connection.env === 'object'
				? Object.entries(connection.env)
						.map(([k, v]) => `${k}=${v}`)
						.join('\n')
				: '';
		// Bearer keys remain write-only — a single opaque value, nothing to show back.
	};

	const applyPreset = (preset: { name: string; url: string }) => {
		transport = 'remote_http';
		auth_type = 'oauth_2.1';
		url = preset.url;
		if (!name.trim()) name = preset.name;
	};

	// Split a command line into [command, ...args], honoring single/double quotes
	// so args containing spaces survive the round trip.
	const parseCommandLine = (line: string): string[] => {
		const out: string[] = [];
		let current = '';
		let quote: string | null = null;
		let quoted = false;
		for (const ch of line) {
			if (quote) {
				if (ch === quote) quote = null;
				else current += ch;
			} else if (ch === '"' || ch === "'") {
				quote = ch;
				quoted = true;
			} else if (/\s/.test(ch)) {
				if (current || quoted) out.push(current);
				current = '';
				quoted = false;
			} else {
				current += ch;
			}
		}
		if (current || quoted) out.push(current);
		return out;
	};

	const quoteArg = (arg: string) => (/[\s"']/.test(arg) ? `"${arg.replace(/"/g, '\\"')}"` : arg);
	const formatCommandLine = (command: string, args: string[]) =>
		[command, ...args].map(quoteArg).join(' ');

	// Every MCP server README ships a Claude-Desktop-style `mcpServers` JSON
	// block — accept it pasted straight into the command field and expand it
	// into command/args/env/name instead of making the user re-type it.
	const importJsonConfig = (text: string): boolean => {
		if (!text.trim().startsWith('{')) return false;
		try {
			const parsed = JSON.parse(text);
			const servers = parsed?.mcpServers ?? (parsed?.command ? { [name.trim()]: parsed } : null);
			if (!servers || typeof servers !== 'object') return false;
			const entry = Object.entries(servers).find(([, cfg]: [string, any]) => cfg?.command);
			if (!entry) return false;
			const [serverName, cfg] = entry as [string, any];
			commandText = formatCommandLine(cfg.command, Array.isArray(cfg.args) ? cfg.args : []);
			if (cfg.env && typeof cfg.env === 'object' && Object.keys(cfg.env).length) {
				envText = Object.entries(cfg.env)
					.map(([k, v]) => `${k}=${v}`)
					.join('\n');
			}
			if (typeof cfg.cwd === 'string') cwd = cfg.cwd;
			if (!name.trim() && serverName) name = serverName;
			toast.success($i18n.t('Imported MCP server config'));
			return true;
		} catch {
			return false;
		}
	};

	const onCommandPaste = (event: ClipboardEvent) => {
		const text = event.clipboardData?.getData('text') ?? '';
		if (importJsonConfig(text)) event.preventDefault();
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

	const errText = (err: any): string => {
		const d = err?.detail ?? err;
		if (typeof d === 'string') return d;
		if (Array.isArray(d)) return d.map((e) => e?.msg ?? `${e}`).join(', ');
		// A network-level fetch failure throws an Error (e.g. TypeError "Failed to
		// fetch") whose own enumerable props are empty, so JSON.stringify would
		// render a useless "{}". Surface the message instead.
		if (d instanceof Error) return d.message;
		try {
			return JSON.stringify(d);
		} catch (e) {
			return `${d}`;
		}
	};

	const discover = async () => {
		if (!url.trim()) return;
		discovering = true;
		try {
			const res = await discoverMCP(localStorage.token, url);
			if (res?.endpoint) url = res.endpoint;
			if (res?.name && !name.trim()) name = res.name;
			toast.success($i18n.t('MCP endpoint discovered'));
		} catch (err: any) {
			toast.error(errText(err));
		} finally {
			discovering = false;
		}
	};

	const isWriteTool = (spec: any) => spec?.annotations?.readOnlyHint !== true;

	const loadTools = async () => {
		if (!connection?.id) return;
		toolsLoading = true;
		toolsError = '';
		toolsAuthRequired = false;
		try {
			// all=true → the FULL upstream catalog, so disabled tools still show.
			const res = await getMCPConnectionTools(localStorage.token, connection.id, true);
			if (res?.auth_required) {
				toolsAuthRequired = true;
				return;
			}
			toolSpecs = res?.specs ?? [];
			const filters = connection?.tool_filters ?? {};
			const includeRaw: string[] | null | undefined = filters?.include;
			const exclude: string[] = filters?.exclude ?? [];
			const map: Record<string, boolean> = {};
			for (const spec of toolSpecs) {
				const n = spec.name;
				// Allowlist read-back: an ABSENT include means all-enabled; a PRESENT
				// include (even empty) means only its members are enabled.
				map[n] = (includeRaw == null || includeRaw.includes(n)) && !exclude.includes(n);
			}
			toolEnabled = map;
			toolsDirty = false;
			toolsLoaded = true;
		} catch (err: any) {
			toolsError = errText(err);
		} finally {
			toolsLoading = false;
		}
	};

	const toggleTools = () => {
		toolsOpen = !toolsOpen;
		if (toolsOpen && !toolsLoaded && !toolsLoading) loadTools();
	};

	const setAllTools = (val: boolean) => {
		const map: Record<string, boolean> = {};
		for (const spec of toolSpecs) map[spec.name] = val;
		toolEnabled = map;
		toolsDirty = true;
	};

	const submit = async () => {
		if (!name.trim()) {
			toast.error($i18n.t('Please enter a name'));
			return;
		}
		if (transport !== 'stdio' && !url.trim()) {
			toast.error($i18n.t('Please enter a server URL'));
			return;
		}
		const isCustomStdio = transport === 'stdio' && template === CUSTOM;
		const commandTokens = isCustomStdio ? parseCommandLine(commandText) : [];
		if (isCustomStdio && commandTokens.length === 0) {
			toast.error($i18n.t('Please enter a command'));
			return;
		}
		saving = true;
		try {
			const body: any = {
				name,
				description:
					transport === 'stdio'
						? isCustomStdio
							? commandText.trim()
							: templates[template]?.name
						: url,
				transport,
				auth_type: transport === 'stdio' ? 'none' : auth_type,
				url: transport === 'stdio' ? undefined : url,
				policy: {
					enable_write_tools,
					allow_localhost_oauth: isAdmin ? allow_localhost_oauth : false
				},
				meta: {
					...(transport === 'stdio' && !isCustomStdio ? { template } : {}),
					...(icon ? { icon } : {})
				}
			};

			if (transport === 'stdio') {
				if (isCustomStdio) {
					body.command = commandTokens[0];
					body.args = commandTokens.slice(1);
					body.cwd = cwd.trim() || null;
				} else {
					// Switching (back) to a template must clear any stored custom
					// command — the backend rejects template + command outright.
					body.command = null;
					body.args = [];
					body.cwd = null;
				}
			}

			// The bearer key is write-only: only send when the user typed a value,
			// so an edit that leaves it blank keeps the existing secret. On add,
			// send the (possibly empty) value explicitly.
			if (transport !== 'stdio' && auth_type === 'bearer') {
				if (key.trim()) body.key = key;
				else if (!edit) body.key = key;
			}
			// Headers/request metadata and env are returned to the owner and
			// hydrated above, so they round-trip like any other form field — an
			// empty edit now intentionally clears all saved entries.
			body.headers = parseHeaders();
			if (transport === 'stdio') body.env = parseEnv();

			// Per-tool selection (allowlist) — only when the user actually toggled
			// a tool, so opening the manager (or editing other fields) never
			// converts the connection into an explicit allowlist by itself.
			if (edit && toolsDirty) {
				body.tool_filters = {
					include: toolSpecs.filter((s) => toolEnabled[s.name]).map((s) => s.name)
				};
			}

			if (edit) {
				await updateMCPConnection(localStorage.token, connection.id, body);
				toast.success($i18n.t('MCP connection updated'));
			} else {
				await createMCPConnection(localStorage.token, body);
				toast.success($i18n.t('MCP connection added'));
			}
			reset();
			show = false;
			onSaved();
		} catch (err: any) {
			toast.error(errText(err));
		} finally {
			saving = false;
		}
	};

	const fieldClass =
		'w-full text-sm bg-gray-50 dark:bg-gray-850 rounded-lg px-3 py-2 outline-hidden';

	const iconChangeHandler = async (e: Event) => {
		const input = e.target as HTMLInputElement;
		const file = input?.files?.[0];
		if (!file) return;

		try {
			icon = await fileToIconDataUrl(file);
		} catch (err: any) {
			toast.error(err?.message ?? $i18n.t('Could not read that image'));
		} finally {
			// Always clear, so re-picking the same file fires `change` again.
			input.value = '';
		}
	};
	$effect(() => {
		if (show && transport === 'stdio' && !template) {
			template = Object.keys(templates)[0] ?? (canCustomStdio ? CUSTOM : '');
		}
	});
	$effect(() => {
		if (show && !prevShow) {
			if (edit && connection) hydrate();
			else reset();
		}
		prevShow = show;
	});
	let enabledToolCount = $derived(toolSpecs.filter((s) => toolEnabled[s.name]).length);
</script>

<Modal size="sm" bind:show>
	<div>
		<div class="flex justify-between dark:text-gray-100 px-5 pt-4 pb-1">
			<h1 class="text-lg font-medium self-center font-primary">
				{edit ? $i18n.t('Edit MCP Connection') : $i18n.t('Add MCP Connection')}
			</h1>
			<button class="self-center" aria-label={$i18n.t('Close')} onclick={() => (show = false)}>
				<XMark className="size-5" />
			</button>
		</div>

		<div class="flex flex-col gap-3 px-5 pb-5 pt-1 max-h-[70vh] overflow-y-auto">
			{#if !edit && transport !== 'stdio'}
				<div>
					<div class="text-xs text-gray-500 mb-1.5">{$i18n.t('Quick add')}</div>
					<div class="flex flex-wrap gap-1.5">
						{#each PRESETS as preset}
							<button
								type="button"
								class="px-2.5 py-1 text-xs rounded-full border border-gray-100 dark:border-gray-800 hover:bg-gray-50 dark:hover:bg-gray-850 transition {url ===
								preset.url
									? 'bg-gray-100 dark:bg-gray-800'
									: ''}"
								onclick={() => applyPreset(preset)}
							>
								{preset.name}
							</button>
						{/each}
					</div>
				</div>
			{/if}

			<input
				bind:this={iconInputElement}
				type="file"
				hidden
				accept={ICON_ACCEPT}
				onchange={iconChangeHandler}
			/>

			<div>
				<div class="text-xs text-gray-500 mb-1">{$i18n.t('Name')}</div>
				<div class="flex items-center gap-2">
					<input class={fieldClass} bind:value={name} placeholder={$i18n.t('e.g. Notion')} />

					<div class="relative shrink-0">
						<Tooltip content={icon ? $i18n.t('Change icon') : $i18n.t('Upload icon')}>
							<button
								type="button"
								class="size-9 rounded-lg flex items-center justify-center bg-gray-50 dark:bg-gray-850 hover:bg-gray-100 dark:hover:bg-gray-800 text-gray-400 transition"
								aria-label={icon ? $i18n.t('Change icon') : $i18n.t('Upload icon')}
								onclick={() => iconInputElement?.click()}
							>
								<ToolIcon src={icon} alt={name} className="size-5" />
							</button>
						</Tooltip>

						{#if icon}
							<button
								type="button"
								class="absolute -top-1.5 -right-1.5 size-4 rounded-full flex items-center justify-center bg-gray-100 dark:bg-gray-800 border-hairline border-gray-200 dark:border-gray-700 text-gray-500 hover:text-gray-700 dark:hover:text-gray-300 transition"
								aria-label={$i18n.t('Remove icon')}
								onclick={() => (icon = '')}
							>
								<XMark className="size-2.5" />
							</button>
						{/if}
					</div>
				</div>
			</div>

			<div>
				<div class="text-xs text-gray-500 mb-1">{$i18n.t('Connection type')}</div>
				<select class={fieldClass} bind:value={transport}>
					<option value="remote_http">{$i18n.t('Remote — Streamable HTTP')}</option>
					<option value="remote_sse">{$i18n.t('Remote — SSE (legacy)')}</option>
					<option value="stdio">{$i18n.t('Local — stdio command')}</option>
				</select>
			</div>

			{#if transport === 'stdio'}
				<div>
					<div class="text-xs text-gray-500 mb-1">{$i18n.t('Template')}</div>
					<select class={fieldClass} bind:value={template}>
						{#each Object.keys(templates) as templateId}
							<option value={templateId}>{templates[templateId]?.name ?? templateId}</option>
						{/each}
						{#if canCustomStdio}
							<option value={CUSTOM}>{$i18n.t('Custom command…')}</option>
						{/if}
					</select>
				</div>
				{#if template === CUSTOM}
					<div>
						<div class="text-xs text-gray-500 mb-1">{$i18n.t('Command')}</div>
						<input
							class="{fieldClass} font-mono"
							bind:value={commandText}
							onpaste={onCommandPaste}
							placeholder={'npx -y @scope/some-mcp-server'}
							autocomplete="off"
							spellcheck="false"
						/>
						<div class="text-xs text-gray-400 mt-1">
							{$i18n.t(
								'The full command line to launch the server — or paste an "mcpServers" JSON block to import it.'
							)}
						</div>
					</div>
					<div>
						<div class="text-xs text-gray-500 mb-1">
							{$i18n.t('Working directory (optional)')}
						</div>
						<input
							class="{fieldClass} font-mono"
							bind:value={cwd}
							placeholder={'/path/to/dir'}
							autocomplete="off"
							spellcheck="false"
						/>
					</div>
				{/if}
				<div>
					<div class="text-xs text-gray-500 mb-1">{$i18n.t('Environment variables')}</div>
					<textarea
						class="{fieldClass} min-h-20 font-mono"
						bind:value={envText}
						placeholder={'KEY=value\nOUTLOOK_CLIENT_ID=...'}></textarea>
				</div>
				<div>
					<div class="text-xs text-gray-500 mb-1">{$i18n.t('Request metadata')}</div>
					<textarea
						class="{fieldClass} min-h-16 font-mono"
						bind:value={headerText}
						placeholder={'X-Chat-Id: {{CHAT_ID}}\nX-User-Id: {{USER_ID}}'}></textarea>
					<div class="text-xs text-gray-400 mt-1 leading-snug">
						{$i18n.t(
							'Stdio has no HTTP headers. These values are sent with every tool call in MCP _meta. Dynamic values: {{CHAT_ID}}, {{MESSAGE_ID}}, {{SESSION_ID}}, {{USER_ID}}, {{USER_NAME}}, {{BROWSER_SESSION}}.'
						)}
					</div>
				</div>
			{:else}
				<div>
					<div class="text-xs text-gray-500 mb-1">{$i18n.t('Server URL')}</div>
					<div class="flex items-center gap-2 bg-gray-50 dark:bg-gray-850 rounded-lg px-3 py-2">
						<input
							class="w-full flex-1 min-w-0 text-sm bg-transparent outline-hidden"
							bind:value={url}
							placeholder="https://mcp.notion.com/mcp"
							autocomplete="off"
						/>
						<Tooltip content={$i18n.t('Discover endpoint from /.well-known/mcp.json')}>
							<button
								type="button"
								class="shrink-0 text-xs px-2 py-0.5 rounded-md hover:bg-gray-100 dark:hover:bg-gray-800 transition {discovering
									? 'cursor-wait opacity-60'
									: ''}"
								disabled={discovering}
								onclick={discover}
							>
								{#if discovering}
									<Spinner className="size-3.5" />
								{:else}
									{$i18n.t('Discover')}
								{/if}
							</button>
						</Tooltip>
					</div>
				</div>

				<div>
					<div class="text-xs text-gray-500 mb-1">{$i18n.t('Authentication')}</div>
					<select class={fieldClass} bind:value={auth_type}>
						<option value="oauth_2.1">{$i18n.t('OAuth 2.1 (recommended)')}</option>
						<option value="bearer">{$i18n.t('Bearer token')}</option>
						<option value="headers">{$i18n.t('Custom headers')}</option>
						<option value="none">{$i18n.t('None')}</option>
					</select>
					{#if auth_type === 'oauth_2.1'}
						<div class="text-xs text-gray-400 mt-1">
							{$i18n.t("You'll sign in after saving — no secrets needed.")}
						</div>
					{/if}
				</div>

				{#if auth_type === 'bearer'}
					<div>
						<div class="text-xs text-gray-500 mb-1">{$i18n.t('Bearer token')}</div>
						<SensitiveInput
							bind:value={key}
							placeholder={edit ? $i18n.t('Leave blank to keep current') : $i18n.t('Token')}
							outerClassName="flex flex-1 bg-gray-50 dark:bg-gray-850 rounded-lg px-3 py-2"
						/>
					</div>
				{/if}

				<!-- Custom headers: applied for ANY auth type (extra headers on top of
				     OAuth/bearer, or the sole auth when auth_type is "headers"). -->
				<div>
					<div class="text-xs text-gray-500 mb-1">{$i18n.t('Custom headers')}</div>
					<textarea
						class="{fieldClass} min-h-16 font-mono"
						bind:value={headerText}
						placeholder={'X-Chat-Id: {{CHAT_ID}}\nX-API-Key: ...'}></textarea>
					<div class="text-xs text-gray-400 mt-1">
						{$i18n.t('Sent with every request — one per line: Header-Name: value')}
					</div>
					<div class="text-xs text-gray-400 mt-1 leading-snug">
						{$i18n.t(
							'Dynamic values: {{CHAT_ID}}, {{MESSAGE_ID}}, {{SESSION_ID}}, {{USER_ID}}, {{USER_NAME}}, {{BROWSER_SESSION}}. Example: X-Chat-Id: {{CHAT_ID}}'
						)}
					</div>
				</div>
			{/if}

			<!-- Tools (edit mode) — the backend lists stdio catalogs too, so the
			     per-tool allowlist works for local servers as well as remote. -->
			{#if edit}
				<div class="border-t border-gray-100 dark:border-gray-850 pt-2">
					<button
						type="button"
						class="text-xs text-gray-500 hover:text-gray-700 dark:hover:text-gray-300 transition"
						onclick={toggleTools}
					>
						{toolsOpen ? '▾' : '▸'}
						{$i18n.t('Tools')}{#if toolsLoaded}
							<span class="text-gray-400"> ({enabledToolCount}/{toolSpecs.length})</span>
						{/if}
					</button>
					{#if toolsOpen}
						<div class="mt-2">
							{#if toolsLoading}
								<div class="py-3 flex justify-center"><Spinner className="size-4" /></div>
							{:else if toolsAuthRequired}
								<div class="text-xs text-gray-500">
									{$i18n.t('Sign in to the server first, then reopen this to manage its tools.')}
								</div>
							{:else if toolsError}
								<div class="text-xs text-error-brick dark:text-error-brick-dark">
									{toolsError}
									<button class="underline ml-1" onclick={loadTools}>{$i18n.t('Retry')}</button>
								</div>
							{:else if !toolsLoaded}
								<button class="text-xs underline text-gray-500" onclick={loadTools}>
									{$i18n.t('Load tools')}
								</button>
							{:else if toolSpecs.length === 0}
								<div class="text-xs text-gray-500">{$i18n.t('This server exposes no tools.')}</div>
							{:else}
								<div class="text-xs text-gray-400 mb-1.5">
									{$i18n.t(
										'Enable only the tools the model should use. Tools the server adds later stay off until you enable them.'
									)}
								</div>
								<div class="flex gap-3 text-xs mb-2">
									<button
										class="text-gray-500 hover:text-gray-700 dark:hover:text-gray-300"
										onclick={() => setAllTools(true)}>{$i18n.t('Enable all')}</button
									>
									<button
										class="text-gray-500 hover:text-gray-700 dark:hover:text-gray-300"
										onclick={() => setAllTools(false)}>{$i18n.t('Disable all')}</button
									>
								</div>
								<div class="flex flex-col gap-1 max-h-56 overflow-y-auto pr-1">
									{#each toolSpecs as spec (spec.name)}
										<div class="flex items-center justify-between gap-2 py-1">
											<div class="min-w-0">
												<div class="text-sm truncate flex items-center gap-1.5">
													{spec.name}
													{#if isWriteTool(spec)}
														<span
															class="text-[10px] px-1 py-px rounded bg-warning/10 border-hairline border-warning/25 text-warning dark:text-warning-dark"
															>{$i18n.t('write')}</span
														>
													{/if}
												</div>
												{#if spec.description}
													<div class="text-xs text-gray-400 truncate">{spec.description}</div>
												{/if}
											</div>
											<Switch
												state={!!toolEnabled[spec.name]}
												onchange={(e) => {
													toolEnabled = { ...toolEnabled, [spec.name]: e.detail };
													toolsDirty = true;
												}}
											/>
										</div>
									{/each}
								</div>
								{#if !enable_write_tools && toolSpecs.some((s) => isWriteTool(s) && toolEnabled[s.name])}
									<div class="text-xs text-warning dark:text-warning-dark mt-1.5">
										{$i18n.t(
											'Some enabled tools are write/destructive — also turn on "Enable write / destructive tools" below for them to be offered.'
										)}
									</div>
								{/if}
							{/if}
						</div>
					{/if}
				</div>
			{/if}

			<div
				class="border-t border-gray-100 dark:border-gray-850 pt-3 flex justify-between items-center gap-2"
			>
				<div class="text-xs">
					<div>{$i18n.t('Enable write / destructive tools')}</div>
					<div class="text-gray-400">
						{$i18n.t(
							'Off by default — only read-only tools are exposed. Tools without a read-only annotation count as write tools, so servers that ship no annotations need this on.'
						)}
					</div>
				</div>
				<Switch bind:state={enable_write_tools} />
			</div>

			<!-- Advanced -->
			{#if isAdmin && transport !== 'stdio'}
				<div class="border-t border-gray-100 dark:border-gray-850 pt-2">
					<button
						type="button"
						class="text-xs text-gray-500 hover:text-gray-700 dark:hover:text-gray-300 transition"
						onclick={() => (advancedOpen = !advancedOpen)}
					>
						{advancedOpen ? '▾' : '▸'}
						{$i18n.t('Advanced')}
					</button>
					{#if advancedOpen}
						<div class="flex flex-col gap-2.5 mt-2">
							<div class="flex justify-between items-center gap-2">
								<div class="text-xs">
									<div>{$i18n.t('Allow localhost OAuth metadata')}</div>
									<div class="text-gray-400">
										{$i18n.t('Admin only — permits loopback OAuth targets (dev).')}
									</div>
								</div>
								<Switch bind:state={allow_localhost_oauth} />
							</div>
						</div>
					{/if}
				</div>
			{/if}
		</div>

		<div class="flex justify-end gap-2 px-5 pb-4">
			<button
				type="button"
				class="px-3.5 py-1.5 text-sm rounded-full border border-gray-100 dark:border-gray-800 hover:bg-gray-50 dark:hover:bg-gray-850 transition"
				onclick={() => (show = false)}
			>
				{$i18n.t('Cancel')}
			</button>
			<button
				type="button"
				class="px-3.5 py-1.5 text-sm rounded-full bg-book-cloth hover:bg-kraft text-white transition-colors duration-200 ease-paper flex items-center gap-1.5 {saving
					? 'cursor-wait opacity-60'
					: ''}"
				disabled={saving}
				onclick={submit}
			>
				{#if saving}
					<Spinner className="size-3.5" />
				{/if}
				{edit ? $i18n.t('Save') : $i18n.t('Add connection')}
			</button>
		</div>
	</div>
</Modal>
