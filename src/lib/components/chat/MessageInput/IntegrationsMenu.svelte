<script lang="ts">
	import { DropdownMenu } from 'bits-ui';
	import { getContext, tick } from 'svelte';
	import type { Writable } from 'svelte/store';
	import type { i18n as i18nType } from 'i18next';
	import { fly } from 'svelte/transition';
	import { toast } from '$lib/utils/toast';
	import { flyAndScale } from '$lib/utils/transitions';
	import { hasEnabledToolServers, loadToolServers } from '$lib/utils/toolServers';

	import { config, tools as _tools, settings, toolServers } from '$lib/stores';

	import { getOAuthClientAuthorizationUrl } from '$lib/apis/configs';
	import { startMCPConnectionOAuth } from '$lib/apis/mcp';
	import { getTools } from '$lib/apis/tools';

	import Dropdown from '$lib/components/common/Dropdown.svelte';
	import Tooltip from '$lib/components/common/Tooltip.svelte';
	import Spinner from '$lib/components/common/Spinner.svelte';
	import Wrench from '$lib/components/icons/Wrench.svelte';
	import ToolIcon from '$lib/components/common/ToolIcon.svelte';
	import Sparkles from '$lib/components/icons/Sparkles.svelte';
	import Photo from '$lib/components/icons/Photo.svelte';
	import ChevronRight from '$lib/components/icons/ChevronRight.svelte';
	import ChevronLeft from '$lib/components/icons/ChevronLeft.svelte';
	import EllipsisHorizontal from '$lib/components/icons/EllipsisHorizontal.svelte';
	import IntegrationsMenuItem from './IntegrationsMenuItem.svelte';

	const i18n: Writable<i18nType> = getContext('i18n');

	// Fired on every explicit user toggle so the chat marks its tool/feature

	interface Props {
		selectedToolIds?: string[];
		toggleFilters?: {
			id: string;
			name: string;
			description?: string;
			icon?: string;
			has_user_valves?: boolean;
		}[];
		selectedFilterIds?: string[];
		showStudyModeButton?: boolean;
		studyModeEnabled?: boolean;
		showDataVizButton?: boolean;
		dataVizEnabled?: boolean;
		showAutomationsButton?: boolean;
		automationsEnabled?: boolean;
		showImageGenerationButton?: boolean;
		imageGenerationEnabled?: boolean;
		onShowValves: Function;
		onShowToolDetails?: () => void;
		onClose: Function;
		closeOnOutsideClick?: boolean;
		// selection as user-curated (a model switch then keeps it). No-op default.
		onSelectionTouched?: () => void;
		children?: import('svelte').Snippet;
	}

	let {
		selectedToolIds = $bindable([]),
		toggleFilters = [],
		selectedFilterIds = $bindable([]),
		showStudyModeButton = false,
		studyModeEnabled = $bindable(false),
		showDataVizButton = false,
		dataVizEnabled = $bindable(false),
		showAutomationsButton = false,
		automationsEnabled = $bindable(false),
		showImageGenerationButton = false,
		imageGenerationEnabled = $bindable(false),
		onShowValves,
		onShowToolDetails = () => {},
		onClose,
		closeOnOutsideClick = true,
		onSelectionTouched = () => {},
		children
	}: Props = $props();

	let show = $state(false);
	let tab = $state('');

	let tools: Record<string, any> = $state({});
	let toolsReady = $state(false);
	let initPromise: Promise<void> | null = null;

	// MCP tools (admin-added `server:mcp:*` and personal `user:mcp:*`) surface
	// directly in the top-level menu next to the feature toggles; only the
	// remaining tools (local/workspace, OpenAPI servers, direct servers) live
	// behind the "Tools" submenu.
	const isMCPToolId = (id: string) => id.startsWith('server:mcp:') || id.startsWith('user:mcp:');

	const toggleFilter = (filterId: string) => {
		if (selectedFilterIds.includes(filterId)) {
			selectedFilterIds = selectedFilterIds.filter((id) => id !== filterId);
		} else {
			selectedFilterIds = [...selectedFilterIds, filterId];
		}
		onSelectionTouched();
	};

	const toggleTool = (toolId: string) => {
		if (selectedToolIds.includes(toolId)) {
			selectedToolIds = selectedToolIds.filter((id) => id !== toolId);
		} else {
			selectedToolIds = Array.from(new Set([...selectedToolIds, toolId]));
		}
		onSelectionTouched();
	};

	// Start the OAuth flow for an unauthenticated MCP tool. Kept in the script
	// (not the inline handler) so TS type annotations are valid.
	const connectToolOAuth = async (toolId: string) => {
		const parts = toolId.split(':');
		const serverId = parts?.at(-1) ?? toolId;
		const name = tools[toolId]?.name ?? serverId;

		try {
			sessionStorage.setItem('mcp-oauth-pending', JSON.stringify({ toolId, ts: Date.now() }));
		} catch (err) {
			// sessionStorage may be unavailable (private mode) — non-fatal.
		}

		const returnTo = `${location.pathname}${location.search}`;
		let authUrl = '';
		try {
			if (toolId.startsWith('user:mcp:')) {
				const res = await startMCPConnectionOAuth(localStorage.token, serverId, returnTo);
				authUrl = res?.authorization_url;
			} else {
				authUrl = getOAuthClientAuthorizationUrl(serverId, 'mcp');
			}
		} catch (err: any) {
			toast.error(
				$i18n.t('Could not start sign-in for {{name}}: {{error}}', {
					name,
					error: err?.detail ?? `${err}`
				})
			);
			return;
		}

		if (!authUrl) {
			toast.error($i18n.t('Could not start sign-in for {{name}}.', { name }));
			return;
		}

		toast.info(
			$i18n.t('Opening sign-in for {{name}} — you’ll return here after authorizing.', { name })
		);
		window.open(authUrl, '_self', 'noopener');
	};

	const notifyToolServerError = (data: any) => {
		toast.error(
			$i18n.t(`Failed to connect to {{URL}} OpenAPI tool server`, {
				URL: data?.url
			})
		);
	};

	const init = async () => {
		if (initPromise) {
			return initPromise;
		}

		initPromise = (async () => {
			// init() is invoked from `$: if (show) init()`, i.e. inside this
			// component's own update cycle. When the tools store is already
			// populated (bootstrap pre-seeds it) and no direct tool servers need
			// loading, this body would otherwise run fully synchronously there —
			// and Svelte 4 drops invalidations issued mid-own-update AFTER their
			// derived statements already ran, leaving `toolIds` permanently stale
			// (empty Tools section even though $_tools has entries). Defer past
			// the in-flight update so the `tools`/`toolsReady` writes schedule a
			// fresh cycle.
			await tick();
			if ($_tools === null) {
				await _tools.set(await getTools(localStorage.token));
			}

			if (hasEnabledToolServers($settings?.toolServers ?? [])) {
				await loadToolServers({ onError: notifyToolServerError });
			}

			const availableTools: Record<string, any> = {};
			if ($_tools) {
				$_tools.reduce((a: Record<string, any>, tool: any) => {
					if (tool.id === containerToolId) return a;
					a[tool.id] = {
						name: tool.name,
						description: tool.meta.description,
						...tool,
						// Flattened out of `meta` so every row — local tool, OpenAPI
						// server, admin MCP, personal MCP, direct server — reads the
						// icon from the same place.
						icon: tool.meta?.icon ?? null
					};
					return a;
				}, availableTools);
			}

			if ($toolServers) {
				for (const serverIdx in $toolServers) {
					const server = $toolServers[serverIdx];
					if (server.info) {
						availableTools[`direct_server:${serverIdx}`] = {
							name: server?.info?.title ?? server.url,
							description: server.info.description ?? '',
							icon: server?.icon ?? null
						};
					}
				}
			}

			tools = availableTools;
			toolsReady = true;

			// Drop selections that no longer exist (a deleted tool / server) so the
			// menu can't show a phantom as enabled. FAIL-SAFE: an EMPTY catalogue
			// means "we couldn't find out" far more often than "you own no tools" —
			// a /tools/ hiccup or a tool-server list that didn't load. Pruning on
			// that answer silently emptied the user's selection, and the selection
			// is now durably persisted, so the wipe would outlive the hiccup.
			// Nothing to prune against ⇒ prune nothing.
			const validIds = new Set([
				...Object.keys(tools),
				...(containerToolId ? [containerToolId] : [])
			]);
			if (validIds.size === 0) return;
			// Direct tool servers are generated client-side from $toolServers, which
			// is only fetched when at least one is enabled — so their ids are not
			// judgeable here unless that fetch ran.
			const canJudgeDirectServers = Array.isArray($toolServers) && $toolServers.length > 0;
			selectedToolIds = selectedToolIds.filter(
				(id) => validIds.has(id) || (id.startsWith('direct_server:') && !canJudgeDirectServers)
			);
		})();

		try {
			return await initPromise;
		} finally {
			initPromise = null;
		}
	};
	let containerFeatures = $derived(($config as any)?.features ?? {});
	let containerToolId = $derived(
		containerFeatures?.container_mcp_server_id
			? `server:mcp:${containerFeatures.container_mcp_server_id}`
			: ''
	);
	let sortedFilters = $derived(
		[...toggleFilters].sort((a, b) =>
			a.name.localeCompare(b.name, undefined, { sensitivity: 'base' })
		)
	);
	let toolIds = $derived(Object.keys(tools));
	let sortedToolIds = $derived(
		[...toolIds].sort((a, b) =>
			(tools[a]?.name ?? '').localeCompare(tools[b]?.name ?? '', undefined, {
				sensitivity: 'base'
			})
		)
	);
	let mcpToolIds = $derived(sortedToolIds.filter(isMCPToolId));
	let otherToolIds = $derived(sortedToolIds.filter((id) => !isMCPToolId(id)));
	let enabledOtherToolCount = $derived(
		otherToolIds.filter((id) => selectedToolIds.includes(id)).length
	);
	let toolCountLabel = $derived(
		enabledOtherToolCount > 0
			? `${enabledOtherToolCount}/${otherToolIds.length}`
			: `${otherToolIds.length}`
	);
	let showFeaturesSection = $derived(
		showStudyModeButton ||
			showDataVizButton ||
			showAutomationsButton ||
			showImageGenerationButton ||
			sortedFilters.length > 0
	);
	$effect(() => {
		if (show) {
			init();
		}
	});
</script>

<Dropdown
	bind:show
	{closeOnOutsideClick}
	onchange={(e) => {
		if (e.detail === false) {
			onClose();
		}
	}}
>
	<Tooltip content={$i18n.t('Integrations')} placement="top">
		{@render children?.()}
	</Tooltip>
	{#snippet content()}
		<div>
			<DropdownMenu.Content
				class="w-72 rounded-2xl px-1 py-1  border-hairline border-gray-100  dark:border-gray-800 z-50 bg-white dark:bg-gray-850 dark:text-white shadow-lg max-h-96 overflow-y-auto overflow-x-hidden scrollbar-thin"
				sideOffset={4}
				alignOffset={-6}
				side="bottom"
				align="start"
				transition={flyAndScale}
			>
				{#if tab === ''}
					<div in:fly={{ x: -20, duration: 150 }}>
						{#if showFeaturesSection}
							<div class="px-3 pt-1.5 pb-1 text-xs font-medium text-gray-500 dark:text-gray-400">
								{$i18n.t('Features')}
							</div>

							{#if showStudyModeButton}
								<IntegrationsMenuItem
									name={$i18n.t('Study Mode')}
									tooltip={$i18n.t('Study Mode')}
									state={studyModeEnabled}
									onToggle={() => {
										studyModeEnabled = !studyModeEnabled;
										onSelectionTouched();
									}}
								>
									{#snippet icon()}
										<svg
											xmlns="http://www.w3.org/2000/svg"
											viewBox="0 0 24 24"
											fill="currentColor"
											class="size-4"
										>
											<path
												d="M11.25 4.533A9.707 9.707 0 0 0 6 3a9.735 9.735 0 0 0-3.25.555.75.75 0 0 0-.5.707v14.25a.75.75 0 0 0 1 .707A8.237 8.237 0 0 1 6 18.75c1.995 0 3.823.707 5.25 1.886V4.533ZM12.75 20.636A8.214 8.214 0 0 1 18 18.75c.966 0 1.89.166 2.75.47a.75.75 0 0 0 1-.708V4.262a.75.75 0 0 0-.5-.707A9.735 9.735 0 0 0 18 3a9.707 9.707 0 0 0-5.25 1.533v16.103Z"
											/>
										</svg>
									{/snippet}
								</IntegrationsMenuItem>
							{/if}

							{#if showDataVizButton}
								<IntegrationsMenuItem
									name={$i18n.t('Data Visualization')}
									tooltip={$i18n.t('Data Visualization')}
									state={dataVizEnabled}
									onToggle={() => {
										dataVizEnabled = !dataVizEnabled;
										onSelectionTouched();
									}}
								>
									{#snippet icon()}
										<svg
											xmlns="http://www.w3.org/2000/svg"
											viewBox="0 0 24 24"
											fill="none"
											stroke="currentColor"
											stroke-width="1.75"
											stroke-linecap="round"
											stroke-linejoin="round"
											class="size-4"
										>
											<path d="M3 3v18h18" />
											<path d="M7 14l4-4 4 4 5-6" />
										</svg>
									{/snippet}
								</IntegrationsMenuItem>
							{/if}

							{#if showAutomationsButton}
								<IntegrationsMenuItem
									name={$i18n.t('Automations')}
									tooltip={$i18n.t('Let the model schedule prompts to run later')}
									state={automationsEnabled}
									onToggle={() => {
										automationsEnabled = !automationsEnabled;
										onSelectionTouched();
									}}
								>
									{#snippet icon()}
										<svg
											xmlns="http://www.w3.org/2000/svg"
											viewBox="0 0 24 24"
											fill="none"
											stroke="currentColor"
											stroke-width="1.75"
											stroke-linecap="round"
											stroke-linejoin="round"
											class="size-4"
										>
											<circle cx="12" cy="12" r="9" />
											<path d="M12 7v5l3 2" />
										</svg>
									{/snippet}
								</IntegrationsMenuItem>
							{/if}

							{#if showImageGenerationButton}
								<IntegrationsMenuItem
									name={$i18n.t('Image')}
									tooltip={$i18n.t('Generate an image')}
									state={imageGenerationEnabled}
									onToggle={() => {
										imageGenerationEnabled = !imageGenerationEnabled;
										onSelectionTouched();
									}}
								>
									{#snippet icon()}
										<Photo className="size-4" strokeWidth="1.5" />
									{/snippet}
								</IntegrationsMenuItem>
							{/if}

							{#each sortedFilters as filter (filter.id)}
								<IntegrationsMenuItem
									name={filter?.name ?? ''}
									tooltip={filter?.description ?? ''}
									state={selectedFilterIds.includes(filter.id)}
									onToggle={() => toggleFilter(filter.id)}
									showValves={!!filter?.has_user_valves}
									onValves={() => onShowValves({ type: 'function', id: filter.id })}
								>
									{#snippet icon()}
										{#if filter?.icon}
											<img
												src={filter.icon}
												class="size-3.5 {filter.icon.includes('svg') ? 'dark:invert-[80%]' : ''}"
												style="fill: currentColor;"
												alt={filter.name}
											/>
										{:else}
											<Sparkles className="size-4" strokeWidth="1.75" />
										{/if}
									{/snippet}
								</IntegrationsMenuItem>
							{/each}
						{/if}

						{#if toolsReady}
							{#if toolIds.length > 0}
								<div
									class="px-3 pt-1.5 pb-1 flex items-center justify-between text-xs font-medium text-gray-500 dark:text-gray-400"
								>
									{$i18n.t('Tools')}

									<Tooltip content={$i18n.t('Tool details')}>
										<button
											class="shrink-0 p-0.5 rounded-full hover:bg-gray-50 dark:hover:bg-gray-800/50"
											type="button"
											onclick={(e) => {
												e.stopPropagation();
												onShowToolDetails();
												show = false;
											}}
										>
											<EllipsisHorizontal className="size-4" />
										</button>
									</Tooltip>
								</div>

								{#each mcpToolIds as toolId (toolId)}
									<IntegrationsMenuItem
										name={tools[toolId]?.name ?? ''}
										tooltip={tools[toolId]?.description ?? ''}
										state={selectedToolIds.includes(toolId)}
										unauthenticated={!(tools[toolId]?.authenticated ?? true)}
										onToggle={() => {
											if (!(tools[toolId]?.authenticated ?? true)) {
												connectToolOAuth(toolId);
											} else {
												toggleTool(toolId);
											}
										}}
										showValves={!!tools[toolId]?.has_user_valves}
										onValves={() => onShowValves({ type: 'tool', id: toolId })}
									>
										{#snippet icon()}
											<ToolIcon src={tools[toolId]?.icon} alt={tools[toolId]?.name ?? ''}>
												{#snippet fallback()}
													<Wrench />
												{/snippet}
											</ToolIcon>
										{/snippet}
									</IntegrationsMenuItem>
								{/each}

								{#if otherToolIds.length > 0}
									<button
										class="flex w-full justify-between gap-2 items-center px-3 py-1.5 max-md:py-2.5 text-sm cursor-pointer rounded-xl hover:bg-gray-50 dark:hover:bg-gray-800/50"
										onclick={() => {
											tab = 'tools';
										}}
									>
										<div class="shrink-0 size-4 flex justify-center items-center">
											<Wrench />
										</div>

										<div class="flex items-center w-full justify-between">
											<div class="line-clamp-1">
												{$i18n.t('Tools')}
											</div>

											<div class="flex items-center gap-1 text-gray-500">
												<span>{toolCountLabel}</span>
												<ChevronRight />
											</div>
										</div>
									</button>
								{/if}
							{/if}
						{:else}
							<div class="py-4">
								<Spinner />
							</div>
						{/if}
					</div>
				{:else if tab === 'tools' && toolsReady}
					<div in:fly={{ x: 20, duration: 150 }}>
						<div class="flex w-full justify-between gap-2 items-center px-1 py-1">
							<button
								class="flex flex-1 justify-between gap-2 items-center px-2 py-1.5 max-md:py-2.5 text-sm cursor-pointer rounded-xl hover:bg-gray-50 dark:hover:bg-gray-800/50"
								onclick={() => {
									tab = '';
								}}
							>
								<ChevronLeft />

								<div class="flex items-center w-full justify-between">
									<div>
										{$i18n.t('Tools')}
									</div>

									<div class="text-gray-500">
										{toolCountLabel}
									</div>
								</div>
							</button>
						</div>

						{#each otherToolIds as toolId (toolId)}
							<IntegrationsMenuItem
								name={tools[toolId]?.name ?? ''}
								tooltip={tools[toolId]?.description ?? ''}
								state={selectedToolIds.includes(toolId)}
								unauthenticated={!(tools[toolId]?.authenticated ?? true)}
								onToggle={() => {
									if (!(tools[toolId]?.authenticated ?? true)) {
										connectToolOAuth(toolId);
									} else {
										toggleTool(toolId);
									}
								}}
								showValves={!!tools[toolId]?.has_user_valves}
								onValves={() => onShowValves({ type: 'tool', id: toolId })}
							>
								{#snippet icon()}
									<ToolIcon src={tools[toolId]?.icon} alt={tools[toolId]?.name ?? ''}>
										{#snippet fallback()}
											<Wrench />
										{/snippet}
									</ToolIcon>
								{/snippet}
							</IntegrationsMenuItem>
						{/each}
					</div>
				{/if}
			</DropdownMenu.Content>
		</div>
	{/snippet}
</Dropdown>
