<script lang="ts">
	import { getContext } from 'svelte';
	import Checkbox from '$lib/components/common/Checkbox.svelte';
	import Tooltip from '$lib/components/common/Tooltip.svelte';
	import { marked } from 'marked';

	const i18n = getContext('i18n');

	const capabilityLabels = {
		vision: {
			label: $i18n.t('Vision'),
			description: $i18n.t('Model accepts image inputs')
		},
		video: {
			label: $i18n.t('Video'),
			description: $i18n.t(
				'Model accepts video inputs.\nLeave unchecked to auto-detect from the provider’s reported input modalities; check it to force video on for a model the provider has not tagged.'
			)
		},
		file_upload: {
			label: $i18n.t('File Upload'),
			description: $i18n.t('Model accepts file inputs')
		},
		web_search: {
			label: $i18n.t('Web Search'),
			description: $i18n.t('Model can search the web for information')
		},
		image_generation: {
			label: $i18n.t('Image Generation'),
			description: $i18n.t('Model can generate images based on text prompts')
		},
		usage: {
			label: $i18n.t('Usage'),
			description: $i18n.t(
				'Sends `stream_options: { include_usage: true }` in the request.\nSupported providers will return token usage information in the response when set.'
			)
		},
		citations: {
			label: $i18n.t('Citations'),
			description: $i18n.t('Displays citations in the response')
		},
		status_updates: {
			label: $i18n.t('Status Updates'),
			description: $i18n.t('Displays status updates (e.g., web search progress) in the response')
		}
	};

	interface Props {
		capabilities?: {
			vision?: boolean;
			video?: boolean;
			file_upload?: boolean;
			web_search?: boolean;
			image_generation?: boolean;
			usage?: boolean;
			citations?: boolean;
			status_updates?: boolean;
		};
		/**
		 * Capabilities the provider already reports (e.g. video, from OpenRouter's
		 * input modalities). Used only for display: a box with no explicit value
		 * shows the detected state, so the UI never claims a model lacks a
		 * capability it demonstrably has. Ticking or unticking still writes an
		 * explicit override, which wins everywhere.
		 */
		autoDetected?: Record<string, boolean>;
	}

	let { capabilities = $bindable({}), autoDetected = {} }: Props = $props();

	const effectiveState = (capability: string) => {
		const explicit = capabilities[capability];
		if (explicit !== undefined && explicit !== null) return !!explicit;
		return !!autoDetected[capability];
	};

	// True when the box is only ticked because the provider says so.
	const isInherited = (capability: string) =>
		(capabilities[capability] === undefined || capabilities[capability] === null) &&
		!!autoDetected[capability];
</script>

<div>
	<div class="flex w-full justify-between mb-1">
		<div class=" self-center text-sm font-semibold">{$i18n.t('Capabilities')}</div>
	</div>
	<div class="flex items-center mt-2 flex-wrap">
		{#each Object.keys(capabilityLabels) as capability}
			<div class=" flex items-center gap-2 mr-3">
				<Checkbox
					state={effectiveState(capability) ? 'checked' : 'unchecked'}
					onchange={(state) => {
						capabilities[capability] = state === 'checked';
					}}
				/>

				<div class=" py-0.5 text-sm capitalize">
					<Tooltip
						content={marked.parse(
							capabilityLabels[capability].description +
								(isInherited(capability)
									? '\n\n' + $i18n.t('Detected automatically from the provider.')
									: '')
						)}
					>
						{$i18n.t(capabilityLabels[capability].label)}
					</Tooltip>
				</div>
			</div>
		{/each}
	</div>
</div>
