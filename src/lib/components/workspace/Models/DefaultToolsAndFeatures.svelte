<script lang="ts">
	import { getContext } from 'svelte';
	import Checkbox from '$lib/components/common/Checkbox.svelte';
	import Tooltip from '$lib/components/common/Tooltip.svelte';
	import { config } from '$lib/stores';
	import { marked } from 'marked';

	const i18n = getContext('i18n');

	// Everything selectable here is a per-model DEFAULT: it starts enabled in new
	// chats with this model, and users can still toggle it per-chat. This panel is
	// the single source of truth for those defaults — builtin features (web
	// search, image generation, subagents, data viz, study mode), admin-added
	// tool servers (OpenAPI + MCP, incl. the Container workspace server), and

	// Capability-gated features (from the Capabilities section above). A feature

	interface Props {
		// workspace toolkits all live here.
		tools?: { id: string; name: string; meta?: any }[];
		selectedToolIds?: string[];
		// is only selectable when the model can actually do it.
		capabilities?: Record<string, boolean>;
		featureIds?: string[];
	}

	let {
		tools = [],
		selectedToolIds = $bindable([]),
		capabilities = {},
		featureIds = $bindable([])
	}: Props = $props();

	let instanceFeatures = $derived(($config as any)?.features ?? {});
	let containerToolId = $derived(
		instanceFeatures?.container_mcp_server_id
			? `server:mcp:${instanceFeatures.container_mcp_server_id}`
			: ''
	);

	// Builtin features. `capability` gates on the model's capabilities;
	// `configFlag` gates on the instance-level admin feature flag — a feature the
	// whole instance has off can't be defaulted on for a model.
	type FeatureDef = {
		id: string;
		label: string;
		description: string;
		capability?: string;
		configFlag?: string;
	};
	let features: FeatureDef[] = $state([]);
	$effect(() => {
		features = [
			{
				id: 'web_search',
				label: $i18n.t('Web Search'),
				description: $i18n.t('Model can search the web for information'),
				capability: 'web_search',
				configFlag: 'enable_web_search'
			},
			{
				id: 'image_generation',
				label: $i18n.t('Image Generation'),
				description: $i18n.t('Model can generate images based on text prompts'),
				capability: 'image_generation',
				configFlag: 'enable_image_generation'
			},
			{
				id: 'subagents',
				label: $i18n.t('Subagents'),
				description: $i18n.t('Model can spawn isolated research subagents'),
				configFlag: 'enable_subagents'
			},
			{
				id: 'data_viz',
				label: $i18n.t('Data Visualization'),
				description: $i18n.t('Model can render interactive chart widgets'),
				configFlag: 'enable_data_viz'
			},
			{
				id: 'automations',
				label: $i18n.t('Automations'),
				description: $i18n.t('Model can schedule prompts to run later'),
				configFlag: 'enable_automations'
			},
			{
				id: 'study_mode',
				label: $i18n.t('Study Mode'),
				description: $i18n.t('Guided learning system prompt'),
				configFlag: 'enable_study_mode'
			}
		];
	});

	const featureAvailable = (feature: FeatureDef): boolean => {
		if (feature.capability && capabilities?.[feature.capability] !== true) {
			return false;
		}
		if (feature.configFlag && !instanceFeatures?.[feature.configFlag]) {
			return false;
		}
		return true;
	};

	const featureDisabledReason = (feature: FeatureDef): string => {
		if (feature.capability && capabilities?.[feature.capability] !== true) {
			return $i18n.t('Enable the {{NAME}} capability above to default it on.', {
				NAME: feature.label
			});
		}
		return $i18n.t('This feature is disabled instance-wide in Admin Settings.');
	};

	// Admin-added tool servers (OpenAPI `server:` + MCP `server:mcp:`) vs
	// workspace toolkits. Personal MCP connections (`user:mcp:`) are excluded —
	// they belong to ONE user and would silently do nothing for everyone else.
	let serverTools = $derived(
		(tools ?? []).filter(
			(tool) => tool.id.startsWith('server:') && !tool.id.startsWith('user:mcp:')
		)
	);
	let workspaceTools = $derived(
		(tools ?? []).filter(
			(tool) => !tool.id.startsWith('server:') && !tool.id.startsWith('user:mcp:')
		)
	);

	const toggleTool = (id: string, checked: boolean) => {
		if (checked) {
			selectedToolIds = Array.from(new Set([...selectedToolIds, id]));
		} else {
			selectedToolIds = selectedToolIds.filter((toolId) => toolId !== id);
		}
	};

	const toggleFeature = (id: string, checked: boolean) => {
		if (checked) {
			featureIds = Array.from(new Set([...featureIds, id]));
		} else {
			featureIds = featureIds.filter((featureId) => featureId !== id);
		}
	};
</script>

<div>
	<div class="flex w-full justify-between mb-1">
		<div class=" self-center text-sm font-semibold">{$i18n.t('Default Tools & Features')}</div>
	</div>

	<div class=" text-xs text-gray-500 dark:text-gray-400 mb-1">
		{$i18n.t(
			'These start enabled in new chats with this model. Users can still toggle them per-chat.'
		)}
	</div>

	<!-- Builtin features -->
	<div class="mt-2">
		<div class=" text-xs font-medium text-gray-600 dark:text-gray-400 mb-1">
			{$i18n.t('Features')}
		</div>
		<div class="flex items-center flex-wrap">
			{#each features as feature (feature.id)}
				{@const available = featureAvailable(feature)}
				<div class=" flex items-center gap-2 mr-3 mb-1 {available ? '' : 'opacity-50'}">
					<Checkbox
						state={available && featureIds.includes(feature.id) ? 'checked' : 'unchecked'}
						disabled={!available}
						onchange={(state) => toggleFeature(feature.id, state === 'checked')}
					/>
					<div class=" py-0.5 text-sm">
						<Tooltip
							content={available
								? marked.parse(feature.description)
								: featureDisabledReason(feature)}
						>
							{feature.label}
						</Tooltip>
					</div>
				</div>
			{/each}
		</div>
	</div>

	<!-- Admin tool servers (OpenAPI + MCP, incl. the Container workspace server) -->
	{#if serverTools.length > 0}
		<div class="mt-3">
			<div class=" text-xs font-medium text-gray-600 dark:text-gray-400 mb-1">
				{$i18n.t('Tool Servers')}
			</div>
			<div class="flex items-center flex-wrap">
				{#each serverTools as tool (tool.id)}
					<div class=" flex items-center gap-2 mr-3 mb-1">
						<Checkbox
							state={selectedToolIds.includes(tool.id) ? 'checked' : 'unchecked'}
							onchange={(state) => toggleTool(tool.id, state === 'checked')}
						/>
						<div class=" py-0.5 text-sm font-medium flex items-center gap-1">
							<Tooltip content={tool?.meta?.description ?? ''}>
								{tool.name}
							</Tooltip>
							{#if tool.id === containerToolId}
								<span
									class="px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide rounded-full bg-gray-100 dark:bg-gray-850 text-gray-500 dark:text-gray-400"
								>
									{$i18n.t('Container')}
								</span>
							{/if}
						</div>
					</div>
				{/each}
			</div>
		</div>
	{/if}

	<!-- Workspace toolkits -->
	<div class="mt-3">
		<div class=" text-xs font-medium text-gray-600 dark:text-gray-400 mb-1">
			{$i18n.t('Toolkits')}
		</div>
		{#if workspaceTools.length > 0}
			<div class="flex items-center flex-wrap">
				{#each workspaceTools as tool (tool.id)}
					<div class=" flex items-center gap-2 mr-3 mb-1">
						<Checkbox
							state={selectedToolIds.includes(tool.id) ? 'checked' : 'unchecked'}
							onchange={(state) => toggleTool(tool.id, state === 'checked')}
						/>
						<div class=" py-0.5 text-sm capitalize font-medium">{tool.name}</div>
					</div>
				{/each}
			</div>
		{:else}
			<div class=" text-xs text-gray-500 dark:text-gray-500">
				{$i18n.t('To select toolkits here, add them to the "Tools" workspace first.')}
			</div>
		{/if}
	</div>
</div>
