<script lang="ts">
	import { getContext } from 'svelte';
	import Checkbox from '$lib/components/common/Checkbox.svelte';
	import Tooltip from '$lib/components/common/Tooltip.svelte';
	import { marked } from 'marked';

	const i18n = getContext('i18n');

	// Toolkits available in the Tools workspace (incl. the Container MCP server,
	// which is just a normal toolkit). Each `{ id, name, ... }`.
	export let tools: { id: string; name: string }[] = [];
	export let selectedToolIds: string[] = [];

	// Capability-gated features that can start ON in a new chat. A feature is only
	// selectable when the model's corresponding capability (set in the separate
	// Capabilities section) is enabled — otherwise the model can't do it at all.
	export let capabilities: Record<string, boolean> = {};
	export let featureIds: string[] = [];

	const FEATURES = ['web_search', 'image_generation'] as const;
	const featureLabels: Record<string, { label: string; description: string }> = {
		web_search: {
			label: $i18n.t('Web Search'),
			description: $i18n.t('Model can search the web for information')
		},
		image_generation: {
			label: $i18n.t('Image Generation'),
			description: $i18n.t('Model can generate images based on text prompts')
		}
	};

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

	<!-- Toolkits (incl. Container) -->
	<div class="mt-2">
		<div class=" text-xs font-medium text-gray-600 dark:text-gray-400 mb-1">{$i18n.t('Tools')}</div>
		{#if tools.length > 0}
			<div class="flex items-center flex-wrap">
				{#each tools as tool (tool.id)}
					<div class=" flex items-center gap-2 mr-3 mb-1">
						<Checkbox
							state={selectedToolIds.includes(tool.id) ? 'checked' : 'unchecked'}
							on:change={(e) => toggleTool(tool.id, e.detail === 'checked')}
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

	<!-- Capability-gated features -->
	<div class="mt-3">
		<div class=" text-xs font-medium text-gray-600 dark:text-gray-400 mb-1">
			{$i18n.t('Features')}
		</div>
		<div class="flex items-center flex-wrap">
			{#each FEATURES as feature}
				{@const capabilityOn = capabilities?.[feature] === true}
				<div class=" flex items-center gap-2 mr-3 mb-1 {capabilityOn ? '' : 'opacity-50'}">
					<Checkbox
						state={capabilityOn && featureIds.includes(feature) ? 'checked' : 'unchecked'}
						disabled={!capabilityOn}
						on:change={(e) => toggleFeature(feature, e.detail === 'checked')}
					/>
					<div class=" py-0.5 text-sm">
						<Tooltip
							content={capabilityOn
								? marked.parse(featureLabels[feature].description)
								: $i18n.t('Enable the {{NAME}} capability above to default it on.', {
										NAME: featureLabels[feature].label
									})}
						>
							{featureLabels[feature].label}
						</Tooltip>
					</div>
				</div>
			{/each}
		</div>
	</div>
</div>
