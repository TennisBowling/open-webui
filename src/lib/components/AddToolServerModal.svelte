<script lang="ts">
	import { v4 as uuidv4 } from 'uuid';

	import fileSaver from 'file-saver';
	const { saveAs } = fileSaver;

	import { toast } from 'svelte-sonner';
	import { getContext, onMount } from 'svelte';
	const i18n = getContext('i18n');

	import { settings } from '$lib/stores';
	import Modal from '$lib/components/common/Modal.svelte';
	import Plus from '$lib/components/icons/Plus.svelte';
	import Minus from '$lib/components/icons/Minus.svelte';
	import PencilSolid from '$lib/components/icons/PencilSolid.svelte';
	import SensitiveInput from '$lib/components/common/SensitiveInput.svelte';
	import Tooltip from '$lib/components/common/Tooltip.svelte';
	import Switch from '$lib/components/common/Switch.svelte';
	import Collapsible from '$lib/components/common/Collapsible.svelte';
	import Tags from './common/Tags.svelte';
	import { getToolServerData } from '$lib/apis';
	import { verifyToolServerConnection, registerOAuthClient } from '$lib/apis/configs';
	import AccessControl from './workspace/common/AccessControl.svelte';
	import Spinner from '$lib/components/common/Spinner.svelte';
	import XMark from '$lib/components/icons/XMark.svelte';

	export let onSubmit: Function = () => {};
	export let onDelete: Function = () => {};

	export let show = false;
	export let edit = false;

	export let direct = false;
	export let connection = null;

	let inputElement = null;

	let type = 'openapi'; // 'openapi', 'mcp'

	let url = '';

	let spec_type = 'url'; // 'url', 'json'
	let spec = ''; // used when spec_type is 'json'
	let path = 'openapi.json';

	let auth_type = 'bearer';
	let key = '';

	let headers: { key: string; value: string }[] = [];

	let accessControl = {};

	let id = '';
	let name = '';
	let description = '';

	let oauthClientInfo = null;

	let enable = true;
	let parallelizable = false;
	let loading = false;
	let verifying = false;

	// Discovered tool specs from the most recent successful verify, rendered
	// below the URL row. Cleared whenever the user changes a field that
	// would affect what's actually reachable, so stale results don't linger.
	let verifiedSpecs: any[] | null = null;
	let verifiedAt: number | null = null;
	let lastVerifyError: string | null = null;

	$: if (url || type || auth_type || key || path || spec_type || spec) {
		// Reset on ANY connection-shaping field change. Cheap and avoids the
		// "I changed the URL but the old tool list is still here" trap.
		verifiedSpecs = null;
		verifiedAt = null;
		lastVerifyError = null;
	}

	const registerOAuthClientHandler = async () => {
		if (url === '') {
			toast.error($i18n.t('Please enter a valid URL'));
			return;
		}

		if (id === '') {
			toast.error($i18n.t('Please enter a valid ID'));
			return;
		}

		const res = await registerOAuthClient(
			localStorage.token,
			{
				url: url,
				client_id: id
			},
			'mcp'
		).catch((err) => {
			toast.error($i18n.t('Registration failed'));
			return null;
		});

		if (res) {
			toast.warning(
				$i18n.t(
					'Please save the connection to persist the OAuth client information and do not change the ID'
				)
			);
			toast.success($i18n.t('Registration successful'));

			console.debug('Registration successful', res);
			oauthClientInfo = res?.oauth_client_info ?? null;
		}
	};

	// The connection object posted to the backend. Extracted so verify and
	// submit send the EXACT SAME shape — previously verify sent a partial
	// object missing spec_type/spec/parallelizable/config.command|args|env,
	// so "verify passed" did not reliably mean "runtime will work."
	const buildConnection = () => ({
		type,
		url,
		spec_type,
		spec,
		path,
		auth_type,
		key,
		headers: headers.map((h) => ({ key: h.key.trim(), value: h.value })).filter((h) => h.key !== ''),
		parallelizable,
		config: {
			enable,
			access_control: accessControl
		},
		info: {
			id,
			name,
			description,
			...(oauthClientInfo ? { oauth_client_info: oauthClientInfo } : {})
		}
	});

	const extractErrorMessage = (err: any): string => {
		if (!err) return $i18n.t('Connection failed');
		if (typeof err === 'string') return err;
		if (typeof err.detail === 'string') return err.detail;
		if (typeof err.message === 'string') return err.message;
		try {
			return JSON.stringify(err);
		} catch {
			return $i18n.t('Connection failed');
		}
	};

	const verifyHandler = async () => {
		if (url === '') {
			toast.error($i18n.t('Please enter a valid URL'));
			return;
		}

		if (['openapi', ''].includes(type)) {
			if (spec_type === 'json' && spec === '') {
				toast.error($i18n.t('Please enter a valid JSON spec'));
				return;
			}

			if (spec_type === 'url' && path === '') {
				toast.error($i18n.t('Please enter a valid path'));
				return;
			}
		}

		verifying = true;
		verifiedSpecs = null;
		verifiedAt = null;
		lastVerifyError = null;
		try {
			if (direct) {
				let res;
				try {
					res = await getToolServerData(
						auth_type === 'bearer' ? key : localStorage.token,
						path.includes('://') ? path : `${url}${path.startsWith('/') ? '' : '/'}${path}`
					);
				} catch (err) {
					lastVerifyError = extractErrorMessage(err);
					toast.error(lastVerifyError);
					return;
				}

				if (res) {
					toast.success($i18n.t('Connection successful'));
					console.debug('Connection successful', res);
				}
			} else {
				let res;
				try {
					res = await verifyToolServerConnection(localStorage.token, buildConnection());
				} catch (err) {
					lastVerifyError = extractErrorMessage(err);
					toast.error(lastVerifyError);
					return;
				}

				if (res) {
					if (res.auth_required) {
						// OAuth 2.1 server pre-authorization: metadata is
						// reachable but we don't yet have a token, so we
						// can't list tools. Tell the user the next step
						// instead of silently rendering an empty list.
						toast.success($i18n.t('OAuth metadata reachable — register & authorize to list tools'));
					} else if (Array.isArray(res.specs)) {
						verifiedSpecs = res.specs;
						verifiedAt = Date.now();
						toast.success(
							res.specs.length === 1
								? $i18n.t('Connection successful — 1 tool discovered')
								: $i18n.t('Connection successful — {{count}} tools discovered', {
										count: res.specs.length
									})
						);
					} else {
						toast.success($i18n.t('Connection successful'));
					}
					console.debug('Connection successful', res);
				}
			}
		} finally {
			verifying = false;
		}
	};

	const importHandler = async (e) => {
		const file = e.target.files[0];
		if (!file) return;

		const reader = new FileReader();
		reader.onload = (event) => {
			const json = event.target.result;
			console.log('importHandler', json);

			try {
				let data = JSON.parse(json);
				// validate data
				if (Array.isArray(data)) {
					if (data.length === 0) {
						toast.error($i18n.t('Please select a valid JSON file'));
						return;
					}
					data = data[0];
				}

				if (data.type) type = data.type;
				if (data.url) url = data.url;

				if (data.spec_type) spec_type = data.spec_type;
				if (data.spec) spec = data.spec;
				if (data.path) path = data.path;

				if (data.auth_type) auth_type = data.auth_type;
				if (data.key) key = data.key;

				if (Array.isArray(data.headers)) {
					headers = data.headers
						.filter((h) => h && typeof h === 'object')
						.map((h) => ({ key: String(h.key ?? ''), value: String(h.value ?? '') }));
				}

				if (typeof data.parallelizable === 'boolean') {
					parallelizable = data.parallelizable;
				}

				if (data.info) {
					id = data.info.id ?? '';
					name = data.info.name ?? '';
					description = data.info.description ?? '';
				}

				if (data.config) {
					enable = data.config.enable ?? true;
					accessControl = data.config.access_control ?? {};
				}

				toast.success($i18n.t('Import successful'));
			} catch (error) {
				toast.error($i18n.t('Please select a valid JSON file'));
			}
		};
		reader.readAsText(file);
	};

	const exportHandler = async () => {
		// export current connection as json file
		const json = JSON.stringify([
			{
				type,
				url,

				spec_type,
				spec,
				path,

				auth_type,
				key,

				headers: headers.filter((h) => h.key.trim() !== ''),

				parallelizable,

				info: {
					id: id,
					name: name,
					description: description
				}
			}
		]);

		const blob = new Blob([json], {
			type: 'application/json'
		});

		saveAs(blob, `tool-server-${id || name || 'export'}.json`);
	};

	const submitHandler = async () => {
		loading = true;

		// remove trailing slash from url
		url = url.replace(/\/$/, '');
		if (id.includes(':') || id.includes('|')) {
			toast.error($i18n.t('ID cannot contain ":" or "|" characters'));
			loading = false;
			return;
		}

		if (type === 'mcp' && auth_type === 'oauth_2.1' && !oauthClientInfo) {
			toast.error($i18n.t('Please register the OAuth client'));
			loading = false;
			return;
		}

		// validate spec
		if (spec_type === 'json') {
			try {
				const specJSON = JSON.parse(spec);
				spec = JSON.stringify(specJSON, null, 2);
			} catch (e) {
				toast.error($i18n.t('Please enter a valid JSON spec'));
				loading = false;
				return;
			}
		}

		const connection = buildConnection();

		await onSubmit(connection);

		loading = false;
		show = false;

		// reset form
		type = 'openapi';
		url = '';

		spec_type = 'url';
		spec = '';
		path = 'openapi.json';

		key = '';
		auth_type = 'bearer';
		headers = [];

		id = '';
		name = '';
		description = '';
		oauthClientInfo = null;

		enable = true;
		parallelizable = false;
		accessControl = null;
	};

	const init = () => {
		if (connection) {
			type = connection?.type ?? 'openapi';
			url = connection.url;

			spec_type = connection?.spec_type ?? 'url';
			spec = connection?.spec ?? '';
			path = connection?.path ?? 'openapi.json';

			auth_type = connection?.auth_type ?? 'bearer';
			key = connection?.key ?? '';

			headers = Array.isArray(connection?.headers)
				? connection.headers
						.filter((h) => h && typeof h === 'object')
						.map((h) => ({ key: String(h.key ?? ''), value: String(h.value ?? '') }))
				: [];

			parallelizable = connection?.parallelizable ?? false;

			id = connection.info?.id ?? '';
			name = connection.info?.name ?? '';
			description = connection.info?.description ?? '';
			oauthClientInfo = connection.info?.oauth_client_info ?? null;

			enable = connection.config?.enable ?? true;
			accessControl = connection.config?.access_control ?? null;
		}
	};

	$: if (show) {
		init();
	}

	onMount(() => {
		init();
	});
</script>

<Modal size="sm" bind:show>
	<div>
		<div class=" flex justify-between dark:text-gray-100 px-5 pt-4 pb-2">
			<h1 class=" text-lg font-medium self-center font-primary">
				{#if edit}
					{$i18n.t('Edit Connection')}
				{:else}
					{$i18n.t('Add Connection')}
				{/if}
			</h1>

			<div class="flex items-center gap-3">
				<div class="flex gap-1.5 text-xs justify-end">
					<button
						class=" hover:underline"
						type="button"
						on:click={() => {
							inputElement?.click();
						}}
					>
						{$i18n.t('Import')}
					</button>

					<button class=" hover:underline" type="button" on:click={exportHandler}>
						{$i18n.t('Export')}
					</button>
				</div>
				<button
					class="self-center"
					aria-label={$i18n.t('Close Configure Connection Modal')}
					on:click={() => {
						show = false;
					}}
				>
					<XMark className={'size-5'} />
				</button>
			</div>
		</div>

		<div class="flex flex-col md:flex-row w-full px-4 pb-4 md:space-x-4 dark:text-gray-200">
			<div class=" flex flex-col w-full sm:flex-row sm:justify-center sm:space-x-6">
				<input
					bind:this={inputElement}
					type="file"
					hidden
					accept=".json"
					on:change={(e) => {
						importHandler(e);
					}}
				/>

				<form
					class="flex flex-col w-full"
					on:submit={(e) => {
						e.preventDefault();
						submitHandler();
					}}
				>
					<div class="px-1">
						{#if !direct}
							<div class="flex gap-2 mb-1.5">
								<div class="flex w-full justify-between items-center">
									<div class=" text-xs text-gray-500">{$i18n.t('Type')}</div>

									<div class="">
										<button
											on:click={() => {
												type = ['', 'openapi'].includes(type) ? 'mcp' : 'openapi';
											}}
											type="button"
											class=" text-xs text-gray-700 dark:text-gray-300"
										>
											{#if ['', 'openapi'].includes(type)}
												{$i18n.t('OpenAPI')}
											{:else if type === 'mcp'}
												{$i18n.t('MCP')}
												<span class="text-gray-500">{$i18n.t('Streamable HTTP')}</span>
											{/if}
										</button>
									</div>
								</div>
							</div>
						{/if}

						<div class="flex gap-2">
							<div class="flex flex-col w-full">
								<div class="flex justify-between mb-0.5">
									<label
										for="api-base-url"
										class={`text-xs ${($settings?.highContrastMode ?? false) ? 'text-gray-800 dark:text-gray-100' : 'text-gray-500'}`}
										>{$i18n.t('URL')}</label
									>
								</div>

								<div class="flex flex-1 items-center">
									<input
										id="api-base-url"
										class={`w-full flex-1 text-sm bg-transparent ${($settings?.highContrastMode ?? false) ? 'placeholder:text-gray-700 dark:placeholder:text-gray-100' : 'outline-hidden placeholder:text-gray-300 dark:placeholder:text-gray-700'}`}
										type="text"
										bind:value={url}
										placeholder={$i18n.t('API Base URL')}
										autocomplete="off"
										required
									/>

									<Tooltip
										content={$i18n.t('Verify Connection')}
										className="shrink-0 flex items-center mr-1"
									>
										<button
											class="self-center p-1 bg-transparent hover:bg-gray-100 dark:bg-gray-900 dark:hover:bg-gray-850 rounded-lg transition {verifying
												? 'cursor-wait opacity-60'
												: ''}"
											on:click={() => {
												verifyHandler();
											}}
											aria-label={$i18n.t('Verify Connection')}
											type="button"
											disabled={verifying}
										>
											{#if verifying}
												<Spinner className="size-4" />
											{:else}
												<svg
													xmlns="http://www.w3.org/2000/svg"
													viewBox="0 0 20 20"
													fill="currentColor"
													class="w-4 h-4"
													aria-hidden="true"
												>
													<path
														fill-rule="evenodd"
														d="M15.312 11.424a5.5 5.5 0 01-9.201 2.466l-.312-.311h2.433a.75.75 0 000-1.5H3.989a.75.75 0 00-.75.75v4.242a.75.75 0 001.5 0v-2.43l.31.31a7 7 0 0011.712-3.138.75.75 0 00-1.449-.39zm1.23-3.723a.75.75 0 00.219-.53V2.929a.75.75 0 00-1.5 0V5.36l-.31-.31A7 7 0 003.239 8.188a.75.75 0 101.448.389A5.5 5.5 0 0113.89 6.11l.311.31h-2.432a.75.75 0 000 1.5h4.243a.75.75 0 00.53-.219z"
														clip-rule="evenodd"
													/>
												</svg>
											{/if}
										</button>
									</Tooltip>

									<Tooltip content={enable ? $i18n.t('Enabled') : $i18n.t('Disabled')}>
										<Switch bind:state={enable} />
									</Tooltip>
								</div>
							</div>
						</div>

						{#if verifiedSpecs && verifiedSpecs.length > 0}
							<div class="mt-2 px-2 py-2 rounded-xl bg-gray-50 dark:bg-gray-950/50 border border-gray-100 dark:border-gray-850">
								<Collapsible buttonClassName="w-full" chevron open>
									<div class="flex items-center justify-between text-xs">
										<div class="font-medium text-gray-700 dark:text-gray-200">
											{verifiedSpecs.length === 1
												? $i18n.t('1 tool discovered')
												: $i18n.t('{{count}} tools discovered', {
														count: verifiedSpecs.length
													})}
										</div>
									</div>
									<div slot="content" class="mt-1.5 text-xs space-y-1.5">
										{#each verifiedSpecs as toolSpec}
											<div class="px-2 py-1.5 rounded-md bg-white dark:bg-gray-900 border border-gray-100 dark:border-gray-850">
												<div class="font-mono text-[11px] font-medium text-gray-800 dark:text-gray-100">
													{toolSpec?.name}
												</div>
												{#if toolSpec?.description}
													<div class="text-gray-500 mt-0.5 whitespace-pre-wrap break-words">
														{toolSpec.description}
													</div>
												{/if}
											</div>
										{/each}
									</div>
								</Collapsible>
							</div>
						{:else if verifiedSpecs && verifiedSpecs.length === 0}
							<div class="mt-2 px-2 py-2 rounded-xl bg-yellow-500/10 text-yellow-700 dark:text-yellow-200 text-xs">
								{$i18n.t('Connection succeeded but the server reported no tools.')}
							</div>
						{:else if lastVerifyError}
							<div class="mt-2 px-2 py-2 rounded-xl bg-red-500/10 text-red-700 dark:text-red-300 text-xs break-words">
								<span class="font-medium">{$i18n.t('Verify failed')}:</span>
								{lastVerifyError}
							</div>
						{/if}

						{#if ['', 'openapi'].includes(type)}
							<div class="flex gap-2 mt-2">
								<div class="flex flex-col w-full">
									<div class="flex justify-between items-center mb-0.5">
										<div class="flex gap-2 items-center">
											<div
												for="select-bearer-or-session"
												class={`text-xs ${($settings?.highContrastMode ?? false) ? 'text-gray-800 dark:text-gray-100' : 'text-gray-500'}`}
											>
												{$i18n.t('OpenAPI Spec')}
											</div>
										</div>
									</div>

									<div class="flex gap-2">
										<div class="flex-shrink-0 self-start">
											<select
												id="select-bearer-or-session"
												class={`w-full text-sm bg-transparent pr-5 ${($settings?.highContrastMode ?? false) ? 'placeholder:text-gray-700 dark:placeholder:text-gray-100' : 'outline-hidden placeholder:text-gray-300 dark:placeholder:text-gray-700'}`}
												bind:value={spec_type}
											>
												<option value="url">{$i18n.t('URL')}</option>
												<option value="json">{$i18n.t('JSON')}</option>
											</select>
										</div>

										<div class="flex flex-1 items-center">
											{#if spec_type === 'url'}
												<div class="flex-1 flex items-center">
													<label for="url-or-path" class="sr-only"
														>{$i18n.t('openapi.json URL or Path')}</label
													>
													<input
														class={`w-full text-sm bg-transparent ${($settings?.highContrastMode ?? false) ? 'placeholder:text-gray-700 dark:placeholder:text-gray-100' : 'outline-hidden placeholder:text-gray-300 dark:placeholder:text-gray-700'}`}
														type="text"
														id="url-or-path"
														bind:value={path}
														placeholder={$i18n.t('openapi.json URL or Path')}
														autocomplete="off"
														required
													/>
												</div>
											{:else if spec_type === 'json'}
												<div
													class={`text-xs w-full self-center translate-y-[1px] ${($settings?.highContrastMode ?? false) ? 'text-gray-800 dark:text-gray-100' : 'text-gray-500'}`}
												>
													<label for="url-or-path" class="sr-only">{$i18n.t('JSON Spec')}</label>
													<textarea
														class={`w-full text-sm bg-transparent ${($settings?.highContrastMode ?? false) ? 'placeholder:text-gray-700 dark:placeholder:text-gray-100' : 'outline-hidden placeholder:text-gray-300 dark:placeholder:text-gray-700 text-black dark:text-white'}`}
														bind:value={spec}
														placeholder={$i18n.t('JSON Spec')}
														autocomplete="off"
														required
														rows="5"
													/>
												</div>
											{/if}
										</div>
									</div>

									{#if ['', 'url'].includes(spec_type)}
										<div
											class={`text-xs mt-1 ${($settings?.highContrastMode ?? false) ? 'text-gray-800 dark:text-gray-100' : 'text-gray-500'}`}
										>
											{$i18n.t(`WebUI will make requests to "{{url}}"`, {
												url: path.includes('://')
													? path
													: `${url}${path.startsWith('/') ? '' : '/'}${path}`
											})}
										</div>
									{/if}
								</div>
							</div>
						{/if}

						<div class="flex gap-2 mt-2">
							<div class="flex flex-col w-full">
								<div class="flex justify-between items-center">
									<div class="flex gap-2 items-center">
										<div
											for="select-bearer-or-session"
											class={`text-xs ${($settings?.highContrastMode ?? false) ? 'text-gray-800 dark:text-gray-100' : 'text-gray-500'}`}
										>
											{$i18n.t('Auth')}
										</div>
									</div>

									{#if auth_type === 'oauth_2.1'}
										<div class="flex items-center gap-2">
											<div class="flex flex-col justify-end items-center shrink-0">
												<Tooltip
													content={oauthClientInfo
														? $i18n.t('Register Again')
														: $i18n.t('Register Client')}
												>
													<button
														class=" text-xs underline dark:text-gray-500 dark:hover:text-gray-200 text-gray-700 hover:text-gray-900 transition"
														type="button"
														on:click={() => {
															registerOAuthClientHandler();
														}}
													>
														{$i18n.t('Register Client')}
													</button>
												</Tooltip>
											</div>

											{#if !oauthClientInfo}
												<div
													class="text-xs font-medium px-1.5 rounded-md bg-yellow-500/20 text-yellow-700 dark:text-yellow-200"
												>
													{$i18n.t('Not Registered')}
												</div>
											{:else}
												<div
													class="text-xs font-medium px-1.5 rounded-md bg-green-500/20 text-green-700 dark:text-green-200"
												>
													{$i18n.t('Registered')}
												</div>
											{/if}
										</div>
									{/if}
								</div>

								<div class="flex gap-2">
									<div class="flex-shrink-0 self-start">
										<select
											id="select-bearer-or-session"
											class={`w-full text-sm bg-transparent pr-5 ${($settings?.highContrastMode ?? false) ? 'placeholder:text-gray-700 dark:placeholder:text-gray-100' : 'outline-hidden placeholder:text-gray-300 dark:placeholder:text-gray-700'}`}
											bind:value={auth_type}
										>
											<option value="none">{$i18n.t('None')}</option>

											<option value="bearer">{$i18n.t('Bearer')}</option>
											<option value="session">{$i18n.t('Session')}</option>

											{#if !direct}
												<option value="system_oauth">{$i18n.t('OAuth')}</option>
												{#if type === 'mcp'}
													<option value="oauth_2.1">{$i18n.t('OAuth 2.1')}</option>
												{/if}
											{/if}
										</select>
									</div>

									<div class="flex flex-1 items-center">
										{#if auth_type === 'bearer'}
											<SensitiveInput
												bind:value={key}
												placeholder={$i18n.t('API Key')}
												required={false}
											/>
										{:else if auth_type === 'none'}
											<div
												class={`text-xs self-center translate-y-[1px] ${($settings?.highContrastMode ?? false) ? 'text-gray-800 dark:text-gray-100' : 'text-gray-500'}`}
											>
												{$i18n.t('No authentication')}
											</div>
										{:else if auth_type === 'session'}
											<div
												class={`text-xs self-center translate-y-[1px] ${($settings?.highContrastMode ?? false) ? 'text-gray-800 dark:text-gray-100' : 'text-gray-500'}`}
											>
												{$i18n.t('Forwards system user session credentials to authenticate')}
											</div>
										{:else if auth_type === 'system_oauth'}
											<div
												class={`text-xs self-center translate-y-[1px] ${($settings?.highContrastMode ?? false) ? 'text-gray-800 dark:text-gray-100' : 'text-gray-500'}`}
											>
												{$i18n.t('Forwards system user OAuth access token to authenticate')}
											</div>
										{:else if auth_type === 'oauth_2.1'}
											<div
												class={`flex items-center text-xs self-center translate-y-[1px] ${($settings?.highContrastMode ?? false) ? 'text-gray-800 dark:text-gray-100' : 'text-gray-500'}`}
											>
												{$i18n.t('Uses OAuth 2.1 Dynamic Client Registration')}
											</div>
										{/if}
									</div>
								</div>
							</div>
						</div>

						<div class="flex gap-2 mt-3">
							<div class="flex flex-col w-full">
								<div class="flex w-full justify-between items-center">
									<Tooltip
										content={$i18n.t(
											'When enabled, calls to tools from this server can run in parallel with other parallelizable tool calls in the same response. Leave off for tools that mutate state or depend on call ordering.'
										)}
										placement="top-start"
									>
										<div
											class={`text-xs ${($settings?.highContrastMode ?? false) ? 'text-gray-800 dark:text-gray-100' : 'text-gray-500'}`}
										>
											{$i18n.t('Allow parallel execution')}
										</div>
									</Tooltip>

									<Switch bind:state={parallelizable} />
								</div>
							</div>
						</div>

						<div class="flex flex-col w-full mt-3">
							<div class="flex w-full justify-between items-center mb-1">
								<div
									class={`text-xs ${($settings?.highContrastMode ?? false) ? 'text-gray-800 dark:text-gray-100' : 'text-gray-500'}`}
								>
									{$i18n.t('Custom Headers')}
								</div>
								<button
									class="px-2 py-0.5 text-xs rounded-md bg-gray-100 dark:bg-gray-900 hover:bg-gray-200 dark:hover:bg-gray-850 transition"
									type="button"
									on:click={() => {
										headers = [...headers, { key: '', value: '' }];
									}}
								>
									{$i18n.t('Add header')}
								</button>
							</div>

							{#if headers.length > 0}
								<div class="flex flex-col gap-1.5">
									{#each headers as header, idx (idx)}
										<div class="flex gap-1.5 items-center">
											<input
												class={`flex-1 min-w-0 text-sm bg-transparent border border-gray-100 dark:border-gray-800 rounded-md px-2 py-1 ${($settings?.highContrastMode ?? false) ? 'placeholder:text-gray-700 dark:placeholder:text-gray-100' : 'outline-hidden placeholder:text-gray-300 dark:placeholder:text-gray-700'}`}
												type="text"
												bind:value={header.key}
												placeholder={$i18n.t('Header name')}
												autocomplete="off"
												spellcheck="false"
											/>
											<input
												class={`flex-[2] min-w-0 text-sm bg-transparent border border-gray-100 dark:border-gray-800 rounded-md px-2 py-1 ${($settings?.highContrastMode ?? false) ? 'placeholder:text-gray-700 dark:placeholder:text-gray-100' : 'outline-hidden placeholder:text-gray-300 dark:placeholder:text-gray-700'}`}
												type="text"
												bind:value={header.value}
												placeholder={$i18n.t('Value')}
												autocomplete="off"
												spellcheck="false"
											/>
											<button
												class="shrink-0 p-1 rounded-md bg-transparent hover:bg-gray-100 dark:hover:bg-gray-850 transition"
												type="button"
												aria-label={$i18n.t('Remove header')}
												on:click={() => {
													headers = headers.filter((_, i) => i !== idx);
												}}
											>
												<Minus className={'size-3.5'} />
											</button>
										</div>
									{/each}
								</div>
							{/if}

							<div
								class={`text-xs mt-1 leading-snug ${($settings?.highContrastMode ?? false) ? 'text-gray-800 dark:text-gray-100' : 'text-gray-500'}`}
							>
								{$i18n.t(
									'Variables: {{CHAT_ID}}, {{MESSAGE_ID}}, {{SESSION_ID}}, {{USER_ID}}, {{USER_NAME}}. Reserved headers (Authorization, Content-Type, Accept, Cookie) are ignored.'
								)}
							</div>
						</div>

						{#if !direct}
							<hr class=" border-gray-100 dark:border-gray-700/10 my-2.5 w-full" />

							<div class="flex gap-2">
								<div class="flex flex-col w-full">
									<label
										for="enter-id"
										class={`mb-0.5 text-xs ${($settings?.highContrastMode ?? false) ? 'text-gray-800 dark:text-gray-100' : 'text-gray-500'}`}
										>{$i18n.t('ID')}

										{#if type !== 'mcp'}
											<span class="text-xs text-gray-200 dark:text-gray-800 ml-0.5"
												>{$i18n.t('Optional')}</span
											>
										{/if}
									</label>

									<div class="flex-1">
										<input
											id="enter-id"
											class={`w-full text-sm bg-transparent ${($settings?.highContrastMode ?? false) ? 'placeholder:text-gray-700 dark:placeholder:text-gray-100' : 'outline-hidden placeholder:text-gray-300 dark:placeholder:text-gray-700'}`}
											type="text"
											bind:value={id}
											placeholder={$i18n.t('Enter ID')}
											autocomplete="off"
											required={type === 'mcp'}
										/>
									</div>
								</div>
							</div>

							<div class="flex gap-2 mt-2">
								<div class="flex flex-col w-full">
									<label
										for="enter-name"
										class={`mb-0.5 text-xs ${($settings?.highContrastMode ?? false) ? 'text-gray-800 dark:text-gray-100' : 'text-gray-500'}`}
										>{$i18n.t('Name')}
									</label>

									<div class="flex-1">
										<input
											id="enter-name"
											class={`w-full text-sm bg-transparent ${($settings?.highContrastMode ?? false) ? 'placeholder:text-gray-700 dark:placeholder:text-gray-100' : 'outline-hidden placeholder:text-gray-300 dark:placeholder:text-gray-700'}`}
											type="text"
											bind:value={name}
											placeholder={$i18n.t('Enter name')}
											autocomplete="off"
											required
										/>
									</div>
								</div>
							</div>

							<div class="flex flex-col w-full mt-2">
								<label
									for="description"
									class={`mb-1 text-xs ${($settings?.highContrastMode ?? false) ? 'text-gray-800 dark:text-gray-100 placeholder:text-gray-700 dark:placeholder:text-gray-100' : 'outline-hidden placeholder:text-gray-300 dark:placeholder:text-gray-700 text-gray-500'}`}
									>{$i18n.t('Description')}</label
								>

								<div class="flex-1">
									<input
										id="description"
										class={`w-full text-sm bg-transparent ${($settings?.highContrastMode ?? false) ? 'placeholder:text-gray-700 dark:placeholder:text-gray-100' : 'outline-hidden placeholder:text-gray-300 dark:placeholder:text-gray-700'}`}
										type="text"
										bind:value={description}
										placeholder={$i18n.t('Enter description')}
										autocomplete="off"
									/>
								</div>
							</div>

							<hr class=" border-gray-100 dark:border-gray-700/10 my-2.5 w-full" />

							<div class="my-2 -mx-2">
								<div class="px-4 py-3 bg-gray-50 dark:bg-gray-950 rounded-3xl">
									<AccessControl bind:accessControl />
								</div>
							</div>
						{/if}
					</div>

					{#if type === 'mcp'}
						<div
							class=" bg-yellow-500/20 text-yellow-700 dark:text-yellow-200 rounded-2xl text-xs px-4 py-3 mb-2"
						>
							<span class="font-medium">
								{$i18n.t('Warning')}:
							</span>
							{$i18n.t(
								'MCP support is experimental and its specification changes often, which can lead to incompatibilities. OpenAPI specification support is directly maintained by the Open WebUI team, making it the more reliable option for compatibility.'
							)}

							<a
								class="font-medium underline"
								href="https://docs.openwebui.com/features/mcp"
								target="_blank">{$i18n.t('Read more →')}</a
							>
						</div>
					{/if}

					<div class="flex justify-between pt-3 text-sm font-medium gap-1.5">
						<div></div>
						<div class="flex gap-1.5">
							{#if edit}
								<button
									class="px-3.5 py-1.5 text-sm font-medium dark:bg-black dark:hover:bg-gray-900 dark:text-white bg-white text-black hover:bg-gray-100 transition rounded-full flex flex-row space-x-1 items-center"
									type="button"
									on:click={() => {
										onDelete();
										show = false;
									}}
								>
									{$i18n.t('Delete')}
								</button>
							{/if}

							<button
								class="px-3.5 py-1.5 text-sm font-medium bg-book-cloth hover:bg-kraft text-white dark:bg-book-cloth dark:text-white dark:hover:bg-kraft transition-colors duration-200 ease-paper rounded-full flex flex-row space-x-1 items-center {loading
									? ' cursor-not-allowed'
									: ''}"
								type="submit"
								disabled={loading}
							>
								{$i18n.t('Save')}

								{#if loading}
									<div class="ml-2 self-center">
										<Spinner />
									</div>
								{/if}
							</button>
						</div>
					</div>
				</form>
			</div>
		</div>
	</div>
</Modal>
