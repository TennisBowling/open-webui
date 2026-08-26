<script lang="ts">
	import { getContext, onMount } from 'svelte';

	import { config, tools as _tools } from '$lib/stores';
	import { getTools } from '$lib/apis/tools';

	import Checkbox from '$lib/components/common/Checkbox.svelte';
	import Tooltip from '$lib/components/common/Tooltip.svelte';

	const i18n = getContext('i18n');

	interface Props {
		selectedToolIds?: string[];
		features?: Record<string, boolean>;
	}

	let { selectedToolIds = $bindable([]), features = $bindable({}) }: Props = $props();

	// Everything an unattended run can't use is absent here — data viz needs a
	// live frontend to render into, ask_user waits on a human, subagents fan out
	// unwatched. The server strips them from every run regardless; not offering
	// them is just honesty about what a scheduled run can do.
	let availableFeatures = $derived(
		[
			{
				id: 'web_search',
				label: $i18n.t('Web Search'),
				description: $i18n.t('Let the run search the web'),
				configFlag: 'enable_web_search'
			},
			{
				id: 'image_generation',
				label: $i18n.t('Image Generation'),
				description: $i18n.t('Let the run generate images'),
				configFlag: 'enable_image_generation'
			}
		].filter((feature) => ($config as any)?.features?.[feature.configFlag])
	);

	// Personal MCP connections (`user:mcp:`) belong to this user and work fine in
	// a background run, so unlike the per-model defaults panel they stay.
	// `direct_server:` ids are excluded: their specs live in the browser payload,
	// so the server could never call them without a tab open.
	let selectableTools = $derived(
		($_tools ?? []).filter((tool: any) => !tool.id.startsWith('direct_server:'))
	);

	const toggleTool = (id: string, checked: boolean) => {
		selectedToolIds = checked
			? Array.from(new Set([...selectedToolIds, id]))
			: selectedToolIds.filter((toolId) => toolId !== id);
	};

	const toggleFeature = (id: string, checked: boolean) => {
		features = { ...features, [id]: checked };
	};

	onMount(async () => {
		if ($_tools === null) {
			_tools.set(await getTools(localStorage.token));
		}
	});
</script>

<div>
	{#if availableFeatures.length > 0}
		<div class=" text-xs font-medium text-gray-600 dark:text-gray-400 mb-1">
			{$i18n.t('Features')}
		</div>
		<div class="flex items-center flex-wrap mb-2">
			{#each availableFeatures as feature (feature.id)}
				<div class=" flex items-center gap-2 mr-3 mb-1">
					<Checkbox
						state={features?.[feature.id] ? 'checked' : 'unchecked'}
						onchange={(state) => toggleFeature(feature.id, state === 'checked')}
					/>
					<div class=" py-0.5 text-sm">
						<Tooltip content={feature.description}>
							{feature.label}
						</Tooltip>
					</div>
				</div>
			{/each}
		</div>
	{/if}

	<div class=" text-xs font-medium text-gray-600 dark:text-gray-400 mb-1">
		{$i18n.t('Tools')}
	</div>
	{#if selectableTools.length > 0}
		<div class="flex items-center flex-wrap">
			{#each selectableTools as tool (tool.id)}
				<div class=" flex items-center gap-2 mr-3 mb-1">
					<Checkbox
						state={selectedToolIds.includes(tool.id) ? 'checked' : 'unchecked'}
						onchange={(state) => toggleTool(tool.id, state === 'checked')}
					/>
					<div class=" py-0.5 text-sm font-medium">
						<Tooltip content={tool?.meta?.description ?? tool.id}>
							{tool.name}
						</Tooltip>
					</div>
				</div>
			{/each}
		</div>
	{:else}
		<div class=" text-xs text-gray-500 dark:text-gray-500">
			{$i18n.t('No tools are available to this account.')}
		</div>
	{/if}
</div>
