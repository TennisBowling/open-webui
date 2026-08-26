<script lang="ts">
	import { preventDefault } from '$lib/utils/eventModifiers';

	import { toast } from '$lib/utils/toast';

	import { onMount, getContext, tick } from 'svelte';
	import { models, tools, functions, user } from '$lib/stores';
	import { WEBUI_BASE_URL } from '$lib/constants';

	import { getTools } from '$lib/apis/tools';
	import { getFunctions } from '$lib/apis/functions';
	import {
		REASONING_EFFORT_ORDER,
		BASE_REASONING_EFFORTS,
		orderReasoningEfforts
	} from '$lib/constants/reasoning';
	import { getModelReasoning } from '$lib/apis/openai';

	import AdvancedParams from '$lib/components/chat/Settings/Advanced/AdvancedParams.svelte';
	import Tags from '$lib/components/common/Tags.svelte';
	import FiltersSelector from '$lib/components/workspace/Models/FiltersSelector.svelte';
	import ActionsSelector from '$lib/components/workspace/Models/ActionsSelector.svelte';
	import Capabilities from '$lib/components/workspace/Models/Capabilities.svelte';
	import Textarea from '$lib/components/common/Textarea.svelte';
	import AccessControl from '../common/AccessControl.svelte';
	import Spinner from '$lib/components/common/Spinner.svelte';
	import Switch from '$lib/components/common/Switch.svelte';
	import XMark from '$lib/components/icons/XMark.svelte';
	import DefaultFiltersSelector from './DefaultFiltersSelector.svelte';
	import DefaultToolsAndFeatures from './DefaultToolsAndFeatures.svelte';
	import OpenRouterProviderSelector from './OpenRouterProviderSelector.svelte';
	import PeakHours from './PeakHours.svelte';
	import { DEFAULT_PEAK_NOTE, isValidBlock, type PeakBlock } from '$lib/utils/peakHours';

	const i18n = getContext('i18n');

	interface Props {
		onSubmit: Function;
		onBack?: null | Function;
		model?: any;
		edit?: boolean;
		preset?: boolean;
	}

	let {
		onSubmit,
		onBack = null,
		model = $bindable(null),
		edit = false,
		preset = true
	}: Props = $props();

	let loading = $state(false);
	let success = false;

	let filesInputElement = $state();
	let inputFiles = $state();

	let showAdvanced = $state(false);
	let showPreview = $state(false);

	let loaded = $state(false);

	// ///////////
	// model
	// ///////////

	let id = $state('');
	let name = $state('');

	let enableDescription = $state(true);

	let system = $state('');
	let info = $state({
		id: '',
		base_model_id: null,
		name: '',
		meta: {
			profile_image_url: `${WEBUI_BASE_URL}/static/favicon.png`,
			description: '',
			suggestion_prompts: null,
			tags: []
		},
		params: {
			system: ''
		}
	});

	let params = $state({
		system: ''
	});

	let toolIds = $state([]);

	let filterIds = $state([]);
	let defaultFilterIds = $state([]);

	let capabilities = $state({
		vision: true,
		// Undefined means "auto-detect from the provider's input modalities"
		// (same tri-state pattern as `usage`). Ticking the box forces video on
		// for a model the provider hasn't tagged.
		video: undefined,
		file_upload: true,
		web_search: true,
		image_generation: true,
		citations: true,
		status_updates: true,
		usage: undefined
	});
	let visionPreprocessorModels = $state([]);
	let vision_preprocessor_model_id = $state('');
	let vision_preprocessor_prompt = $state('');
	let visionPlaceholder = `e.g. 'Extract all text via OCR and describe image relevant to user query: {query}'`;
	let defaultFeatureIds = $state([]);

	let actionIds = $state([]);
	let accessControl = $state({});

	// Reasoning model configuration (per-model). `reasoningMode` decides how the
	// chat effort selector is populated:
	//   'auto'   → use the efforts discovered from the provider (OpenRouter),
	//              updating automatically as the provider changes.
	//   'manual' → use an explicit admin-picked set (`manualReasoningEfforts`).
	let reasoningModelEnabled = $state(true); // default to enabled
	let reasoningMode: 'auto' | 'manual' = $state('auto');
	let manualReasoningEfforts: string[] = $state([]);
	// Live discovery (fetched from OpenRouter's catalog for this model).
	let discoveredReasoning: any = $state(null);
	let discoveringReasoning = $state(false);
	let reasoningDiscoveryTried = false;
	let _lastReasoningSlug = '';
	let _reasoningReqSeq = 0;
	let cacheControlEphemeralEnabled = $state(true);

	// Full effort vocabulary for the manual-override editor, strongest first.
	const ALL_REASONING_EFFORTS = [...REASONING_EFFORT_ORDER].reverse();

	const fetchDiscoveredReasoning = async (slug: string, force = false) => {
		// Monotonic token so an out-of-order/superseded response (admin switched
		// base model, or cleared it, while a fetch was in flight) can't clobber
		// the current selection. Bumped for every call, including the early exits.
		const reqId = ++_reasoningReqSeq;
		if (!slug) {
			discoveredReasoning = null;
			discoveringReasoning = false; // this (latest) request isn't fetching
			return;
		}
		if (!force && slug === _lastReasoningSlug && reasoningDiscoveryTried) {
			discoveringReasoning = false; // served from prior state, not fetching
			return;
		}
		_lastReasoningSlug = slug;
		discoveringReasoning = true;
		try {
			const res = await getModelReasoning(localStorage.token, slug, force);
			if (reqId !== _reasoningReqSeq) return;
			discoveredReasoning = res?.data?.reasoning ?? null;
		} catch (e) {
			if (reqId === _reasoningReqSeq) discoveredReasoning = null;
		} finally {
			if (reqId === _reasoningReqSeq) {
				discoveringReasoning = false;
				reasoningDiscoveryTried = true;
			}
		}
	};

	const toggleManualEffort = (effort: string) => {
		if (manualReasoningEfforts.includes(effort)) {
			manualReasoningEfforts = manualReasoningEfforts.filter((e) => e !== effort);
		} else {
			manualReasoningEfforts = orderReasoningEfforts([...manualReasoningEfforts, effort]);
		}
	};

	// Service tier selector visibility (per-model)
	let serviceTierEnabled = $state(true); // default to enabled for non-ollama models
	// Comma-separated list of tier names this model accepts. Empty == use the
	// OpenAI default ['default', 'flex', 'priority']. Gemini wants
	// ['standard', 'flex', 'priority']; other providers may differ.
	let serviceTierValues = $state('');

	// Peak hours (per-model). Soft, non-blocking heads-up shown to users while the
	// model is in high-demand UTC windows. Times are "HH:MM" in UTC.
	let peakHoursEnabled = $state(false);
	let peakHoursBlocks: PeakBlock[] = $state([]);
	let peakHoursNote = $state('');

	// OpenRouter provider routing
	let openrouterProviderOnly: string[] = $state([]);
	let openrouterProviderOrder: string[] = $state([]);
	let openrouterBaseModelId: string = $state('');

	// Capabilities the provider already advertises. Only `video` is discoverable
	// today (from OpenRouter's input modalities, flattened onto the model by the
	// backend); the others have no provider-reported equivalent.
	let autoDetectedCapabilities = $derived.by(() => {
		const source = $models.find((m) => m.id === (model?.id ?? id)) ?? model;
		const modalities =
			source?.input_modalities ??
			source?.architecture?.input_modalities ??
			source?.openai?.architecture?.input_modalities ??
			[];
		return { video: Array.isArray(modalities) && modalities.includes('video') };
	});

	const addUsage = (base_model_id) => {
		const baseModel = $models.find((m) => m.id === base_model_id);

		if (baseModel) {
			if (baseModel.owned_by === 'openai') {
				capabilities.usage = baseModel?.meta?.capabilities?.usage ?? false;
			} else {
				delete capabilities.usage;
			}
			capabilities = capabilities;
		}
	};

	const submitHandler = async () => {
		loading = true;

		info.id = id;
		info.name = name;

		if (id === '') {
			toast.error($i18n.t('Model ID is required.'));
			loading = false;

			return;
		}

		if (name === '') {
			toast.error($i18n.t('Model Name is required.'));
			loading = false;

			return;
		}

		info.params = { ...info.params, ...params };

		info.access_control = accessControl;
		info.meta.capabilities = capabilities;

		// Persist reasoning config. A manual override stores an explicit
		// `supported_efforts` set; Automatic stores nothing and lets live
		// discovery (or the low/medium/high default) drive the chat selector.
		if (!reasoningModelEnabled) {
			info.meta.reasoning = { enabled: false };
		} else if (reasoningMode === 'manual') {
			const efforts = orderReasoningEfforts(Array.from(new Set(manualReasoningEfforts)));
			if (efforts.length > 0) {
				info.meta.reasoning = {
					enabled: true,
					supported_efforts: efforts,
					source: 'manual'
				};
			} else if (info.meta.reasoning) {
				// Manual but nothing selected → fall back to defaults.
				delete info.meta.reasoning;
			}
		} else {
			// Automatic: omit to preserve backward-compatible defaults + live discovery.
			if (info.meta.reasoning) {
				delete info.meta.reasoning;
			}
		}

		info.meta.cache_control_ephemeral = cacheControlEphemeralEnabled;

		// Persist service tier selector visibility + custom value list
		const parsedTierValues = serviceTierValues
			.split(',')
			.map((s) => s.trim())
			.filter(Boolean);
		const isDefaultTierSet =
			parsedTierValues.length === 3 &&
			parsedTierValues[0] === 'default' &&
			parsedTierValues[1] === 'flex' &&
			parsedTierValues[2] === 'priority';
		const _meta = info.meta as any;
		if (!serviceTierEnabled) {
			_meta.service_tier = { enabled: false };
		} else if (parsedTierValues.length > 0 && !isDefaultTierSet) {
			_meta.service_tier = { enabled: true, values: parsedTierValues };
		} else if (_meta.service_tier) {
			// Default state (enabled + OpenAI tiers): omit to avoid storing unnecessary data
			delete _meta.service_tier;
		}

		// Persist peak hours. Keep only valid, distinct windows; omit the whole
		// block when disabled or empty so default models stay clean.
		const validPeakBlocks = peakHoursBlocks
			.map((b) => ({ start: (b.start ?? '').trim(), end: (b.end ?? '').trim() }))
			.filter((b) => isValidBlock(b));
		if (peakHoursEnabled && validPeakBlocks.length > 0) {
			const peakHours: { enabled: boolean; blocks: PeakBlock[]; note?: string } = {
				enabled: true,
				blocks: validPeakBlocks
			};
			const trimmedNote = (peakHoursNote ?? '').trim();
			// Store the note only when it's a meaningful override of the default.
			if (trimmedNote && trimmedNote !== DEFAULT_PEAK_NOTE) {
				peakHours.note = trimmedNote;
			}
			_meta.peak_hours = peakHours;
		} else if (_meta.peak_hours) {
			delete _meta.peak_hours;
		}

		if (vision_preprocessor_model_id) {
			info.meta.vision_preprocessor_model_id = vision_preprocessor_model_id;
		} else {
			delete info.meta.vision_preprocessor_model_id;
		}
		if (vision_preprocessor_prompt?.trim()) {
			info.meta.vision_preprocessor_prompt = vision_preprocessor_prompt.trim();
		} else {
			delete info.meta.vision_preprocessor_prompt;
		}

		if (enableDescription) {
			info.meta.description = info.meta.description.trim() === '' ? null : info.meta.description;
		} else {
			info.meta.description = null;
		}

		if (toolIds.length > 0) {
			info.meta.toolIds = toolIds;
		} else if (info.meta.toolIds) {
			delete info.meta.toolIds;
		}

		if (filterIds.length > 0) {
			info.meta.filterIds = filterIds;
		} else if (info.meta.filterIds) {
			delete info.meta.filterIds;
		}

		if (defaultFilterIds.length > 0) {
			info.meta.defaultFilterIds = defaultFilterIds;
		} else if (info.meta.defaultFilterIds) {
			delete info.meta.defaultFilterIds;
		}

		if (actionIds.length > 0) {
			info.meta.actionIds = actionIds;
		} else if (info.meta.actionIds) {
			delete info.meta.actionIds;
		}

		if (defaultFeatureIds.length > 0) {
			info.meta.defaultFeatureIds = defaultFeatureIds;
		} else if (info.meta.defaultFeatureIds) {
			delete info.meta.defaultFeatureIds;
		}

		// Persist OpenRouter provider routing
		if (openrouterProviderOnly.length > 0) {
			info.params.custom_params = {
				...(info.params.custom_params ?? {}),
				provider: {
					only: openrouterProviderOnly,
					order: openrouterProviderOrder
				}
			};
		} else if (info.params.custom_params?.provider) {
			delete info.params.custom_params.provider;
			if (Object.keys(info.params.custom_params).length === 0) {
				delete info.params.custom_params;
			}
		}

		info.params.system = system.trim() === '' ? null : system;
		info.params.stop = params.stop ? params.stop.split(',').filter((s) => s.trim()) : null;
		Object.keys(info.params).forEach((key) => {
			if (info.params[key] === '' || info.params[key] === null) {
				delete info.params[key];
			}
		});

		await onSubmit(info);

		loading = false;
		success = false;
	};

	onMount(async () => {
		await tools.set(await getTools(localStorage.token));
		await functions.set(await getFunctions(localStorage.token));

		// Scroll to top 'workspace-container' element
		const workspaceContainer = document.getElementById('workspace-container');
		if (workspaceContainer) {
			workspaceContainer.scrollTop = 0;
		}

		if (model) {
			name = model.name;
			await tick();

			id = model.id;

			enableDescription = model?.meta?.description !== null;

			if (model.base_model_id) {
				const base_model = $models
					.filter((m) => !m?.preset && !(m?.arena ?? false))
					.find((m) => [model.base_model_id, `${model.base_model_id}:latest`].includes(m.id));

				if (base_model) {
					model.base_model_id = base_model.id;
					openrouterBaseModelId = base_model.id;
				} else {
					model.base_model_id = null;
					openrouterBaseModelId = model.base_model_id ?? model.id;
				}
			} else {
				// No base_model_id means this IS the base model — use its own ID
				openrouterBaseModelId = model.id;
			}

			system = model?.params?.system ?? '';

			params = { ...params, ...model?.params };
			params.stop = params?.stop
				? (typeof params.stop === 'string' ? params.stop.split(',') : (params?.stop ?? [])).join(
						','
					)
				: null;

			toolIds = model?.meta?.toolIds ?? [];
			filterIds = model?.meta?.filterIds ?? [];
			defaultFilterIds = model?.meta?.defaultFilterIds ?? [];
			actionIds = model?.meta?.actionIds ?? [];

			// Load reasoning config (default enabled if omitted). An explicit
			// `supported_efforts` (or legacy `extra_efforts`) means the admin
			// curated a manual set; otherwise the model is in Automatic mode.
			const _reasoning = model?.meta?.reasoning;
			reasoningModelEnabled = _reasoning?.enabled ?? true;
			if (Array.isArray(_reasoning?.supported_efforts) && _reasoning.supported_efforts.length > 0) {
				reasoningMode = 'manual';
				manualReasoningEfforts = orderReasoningEfforts(_reasoning.supported_efforts);
			} else if (Array.isArray(_reasoning?.extra_efforts) && _reasoning.extra_efforts.length > 0) {
				// Legacy config: represent base ∪ extras as an explicit manual set
				// so the next save upgrades it to `supported_efforts`.
				reasoningMode = 'manual';
				manualReasoningEfforts = orderReasoningEfforts([
					...BASE_REASONING_EFFORTS,
					..._reasoning.extra_efforts
				]);
			} else {
				reasoningMode = 'auto';
				manualReasoningEfforts = [];
			}
			cacheControlEphemeralEnabled = model?.meta?.cache_control_ephemeral ?? true;
			serviceTierEnabled = model?.meta?.service_tier?.enabled ?? true;
			serviceTierValues = (model?.meta?.service_tier?.values ?? []).join(', ');

			// Load peak hours (default disabled if omitted). Fall back to the default
			// note so the field is pre-filled when the admin opens an enabled model.
			peakHoursEnabled = model?.meta?.peak_hours?.enabled ?? false;
			peakHoursBlocks = (model?.meta?.peak_hours?.blocks ?? []).map((b) => ({
				start: b?.start ?? '',
				end: b?.end ?? ''
			}));
			peakHoursNote = model?.meta?.peak_hours?.note ?? DEFAULT_PEAK_NOTE;

			// Load OpenRouter provider routing config
			openrouterProviderOnly = model?.params?.custom_params?.provider?.only ?? [];
			openrouterProviderOrder = model?.params?.custom_params?.provider?.order ?? [];

			capabilities = { ...capabilities, ...(model?.meta?.capabilities ?? {}) };
			defaultFeatureIds = model?.meta?.defaultFeatureIds ?? [];
			vision_preprocessor_model_id = model?.meta?.vision_preprocessor_model_id ?? '';
			vision_preprocessor_prompt =
				model?.meta?.vision_preprocessor_prompt ??
				'Perform OCR on this image and describe its contents in the context of the user query: {query}';
			capabilities = capabilities;

			if ('access_control' in model) {
				accessControl = model.access_control;
			} else {
				accessControl = {};
			}

			console.log(model?.access_control);
			console.log(accessControl);

			info = {
				...info,
				...JSON.parse(
					JSON.stringify(
						model
							? model
							: {
									id: model.id,
									name: model.name
								}
					)
				)
			};

			console.log(model);
		}

		loaded = true;
	});
	$effect(() => {
		if (!edit) {
			if (name) {
				id = name
					.replace(/\s+/g, '-')
					.replace(/[^a-zA-Z0-9-]/g, '')
					.toLowerCase();
			}
		}
	});
	$effect(() => {
		visionPreprocessorModels = $models.filter((m) => m.info?.meta?.capabilities?.vision ?? true);
	});
	// The efforts the chat will actually offer while in Automatic mode: the
	// discovered set, or the low/medium/high default when the provider exposes
	// no effort granularity.
	let autoEffectiveEfforts = $derived(
		Array.isArray(discoveredReasoning?.supported_efforts) &&
			discoveredReasoning.supported_efforts.length > 0
			? orderReasoningEfforts(discoveredReasoning.supported_efforts)
			: [...BASE_REASONING_EFFORTS]
	);
	// Keep the discovery slug in sync with the selected base model, in BOTH the
	// create flow and the edit flow (the "Base Model (From)" select binds
	// info.base_model_id; onMount pre-normalizes it, so this only ever mirrors an
	// already-resolved id). When the model IS a base (no base_model_id) this
	// doesn't fire and the onMount-set openrouterBaseModelId (= model.id) stands.
	$effect(() => {
		if (info?.base_model_id) {
			openrouterBaseModelId = info.base_model_id;
		}
	});
	// Fetch discovery whenever the resolved base slug changes.
	$effect(() => {
		fetchDiscoveredReasoning(openrouterBaseModelId);
	});
</script>

{#if loaded}
	{#if onBack}
		<button
			class="flex space-x-1"
			onclick={() => {
				onBack();
			}}
		>
			<div class=" self-center">
				<svg
					xmlns="http://www.w3.org/2000/svg"
					viewBox="0 0 20 20"
					fill="currentColor"
					class="h-4 w-4"
				>
					<path
						fill-rule="evenodd"
						d="M17 10a.75.75 0 01-.75.75H5.612l4.158 3.96a.75.75 0 11-1.04 1.08l-5.5-5.25a.75.75 0 010-1.08l5.5-5.25a.75.75 0 111.04 1.08L5.612 9.25H16.25A.75.75 0 0117 10z"
						clip-rule="evenodd"
					/>
				</svg>
			</div>
			<div class=" self-center text-sm font-medium">{$i18n.t('Back')}</div>
		</button>
	{/if}

	<div class="w-full max-h-full flex justify-center">
		<input
			bind:this={filesInputElement}
			bind:files={inputFiles}
			type="file"
			hidden
			accept="image/*"
			onchange={() => {
				let reader = new FileReader();
				reader.onload = (event) => {
					let originalImageUrl = `${event.target.result}`;

					const img = new Image();
					img.src = originalImageUrl;

					img.onload = function () {
						const canvas = document.createElement('canvas');
						const ctx = canvas.getContext('2d');

						// Calculate the aspect ratio of the image
						const aspectRatio = img.width / img.height;

						// Calculate the new width and height to fit within 100x100
						let newWidth, newHeight;
						if (aspectRatio > 1) {
							newWidth = 250 * aspectRatio;
							newHeight = 250;
						} else {
							newWidth = 250;
							newHeight = 250 / aspectRatio;
						}

						// Set the canvas size
						canvas.width = 250;
						canvas.height = 250;

						// Calculate the position to center the image
						const offsetX = (250 - newWidth) / 2;
						const offsetY = (250 - newHeight) / 2;

						// Draw the image on the canvas
						ctx.drawImage(img, offsetX, offsetY, newWidth, newHeight);

						// Get the base64 representation of the compressed image
						const compressedSrc = canvas.toDataURL();

						// Display the compressed image
						info.meta.profile_image_url = compressedSrc;

						inputFiles = null;
						filesInputElement.value = '';
					};
				};

				if (
					inputFiles &&
					inputFiles.length > 0 &&
					['image/gif', 'image/webp', 'image/jpeg', 'image/png', 'image/svg+xml'].includes(
						inputFiles[0]['type']
					)
				) {
					reader.readAsDataURL(inputFiles[0]);
				} else {
					console.log(`Unsupported File Type '${inputFiles[0]['type']}'.`);
					inputFiles = null;
				}
			}}
		/>

		{#if !edit || (edit && model)}
			<form
				class="flex flex-col md:flex-row w-full gap-3 md:gap-6"
				onsubmit={preventDefault(() => {
					submitHandler();
				})}
			>
				<div class="self-center md:self-start flex justify-center my-2 shrink-0">
					<div class="self-center">
						<button
							class="rounded-xl flex shrink-0 items-center {info.meta.profile_image_url !==
							`${WEBUI_BASE_URL}/static/favicon.png`
								? 'bg-transparent'
								: 'bg-white'} shadow-xl group relative"
							type="button"
							onclick={() => {
								filesInputElement.click();
							}}
						>
							{#if info.meta.profile_image_url}
								<img
									src={info.meta.profile_image_url}
									alt="model profile"
									class="rounded-xl size-72 md:size-60 object-cover shrink-0"
								/>
							{:else}
								<img
									src="{WEBUI_BASE_URL}/static/favicon.png"
									alt="model profile"
									class=" rounded-xl size-72 md:size-60 object-cover shrink-0"
								/>
							{/if}

							<div class="absolute bottom-0 right-0 z-10">
								<div class="m-1.5">
									<div
										class="shadow-xl p-1 rounded-full border-2 border-white bg-gray-800 text-white group-hover:bg-gray-600 transition dark:border-black dark:bg-white dark:group-hover:bg-gray-200 dark:text-black"
									>
										<svg
											xmlns="http://www.w3.org/2000/svg"
											viewBox="0 0 16 16"
											fill="currentColor"
											class="size-5"
										>
											<path
												fill-rule="evenodd"
												d="M2 4a2 2 0 0 1 2-2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V4Zm10.5 5.707a.5.5 0 0 0-.146-.353l-1-1a.5.5 0 0 0-.708 0L9.354 9.646a.5.5 0 0 1-.708 0L6.354 7.354a.5.5 0 0 0-.708 0l-2 2a.5.5 0 0 0-.146.353V12a.5.5 0 0 0 .5.5h8a.5.5 0 0 0 .5-.5V9.707ZM12 5a1 1 0 1 1-2 0 1 1 0 0 1 2 0Z"
												clip-rule="evenodd"
											/>
										</svg>
									</div>
								</div>
							</div>

							<div
								class="absolute top-0 bottom-0 left-0 right-0 bg-white dark:bg-black rounded-lg opacity-0 group-hover:opacity-20 transition"
							></div>
						</button>

						<div class="flex w-full mt-1 justify-end">
							<button
								class="px-2 py-1 text-gray-500 rounded-lg text-xs"
								onclick={() => {
									info.meta.profile_image_url = `${WEBUI_BASE_URL}/static/favicon.png`;
								}}
								type="button"
							>
								{$i18n.t('Reset Image')}</button
							>
						</div>
					</div>
				</div>

				<div class="w-full">
					<div class="mt-2 my-2 flex flex-col">
						<div class="flex-1">
							<div>
								<input
									class="text-3xl font-semibold w-full bg-transparent outline-hidden font-primary"
									placeholder={$i18n.t('Model Name')}
									bind:value={name}
									required
								/>
							</div>
						</div>

						<div class="flex-1">
							<div>
								<input
									class="text-xs w-full bg-transparent text-gray-500 outline-hidden"
									placeholder={$i18n.t('Model ID')}
									bind:value={id}
									disabled={edit}
									required
								/>
							</div>
						</div>
					</div>

					{#if preset}
						<div class="my-1">
							<div class=" text-sm font-semibold mb-1">{$i18n.t('Base Model (From)')}</div>

							<div>
								<select
									class="text-sm w-full bg-transparent outline-hidden"
									placeholder={$i18n.t('Select a base model (e.g. llama3, gpt-4o)')}
									bind:value={info.base_model_id}
									onchange={(e) => {
										addUsage(e.target.value);
									}}
									required
								>
									<option value={null} class=" text-gray-900"
										>{$i18n.t('Select a base model')}</option
									>
									{#each $models.filter((m) => (model ? m.id !== model.id : true) && !m?.preset && m?.owned_by !== 'arena' && !(m?.direct ?? false)) as model}
										<option value={model.id} class=" text-gray-900">{model.name}</option>
									{/each}
								</select>
							</div>
						</div>
					{/if}

					<div class="my-1">
						<div class="mb-1 flex w-full justify-between items-center">
							<div class=" self-center text-sm font-semibold">{$i18n.t('Description')}</div>

							<button
								class="p-1 text-xs flex rounded-sm transition"
								type="button"
								aria-pressed={enableDescription ? 'true' : 'false'}
								aria-label={enableDescription
									? $i18n.t('Custom description enabled')
									: $i18n.t('Default description enabled')}
								onclick={() => {
									enableDescription = !enableDescription;
								}}
							>
								{#if !enableDescription}
									<span class="ml-2 self-center">{$i18n.t('Default')}</span>
								{:else}
									<span class="ml-2 self-center">{$i18n.t('Custom')}</span>
								{/if}
							</button>
						</div>

						{#if enableDescription}
							<Textarea
								className=" text-sm w-full bg-transparent outline-hidden resize-none overflow-y-hidden "
								placeholder={$i18n.t('Add a short description about what this model does')}
								bind:value={info.meta.description}
							/>
						{/if}
					</div>

					<div class=" mt-2 my-1">
						<div class="">
							<Tags
								tags={info?.meta?.tags ?? []}
								ondelete={(e) => {
									const tagName = e.detail;
									info.meta.tags = info.meta.tags.filter((tag) => tag.name !== tagName);
								}}
								onadd={(e) => {
									const tagName = e.detail;
									if (!(info?.meta?.tags ?? null)) {
										info.meta.tags = [{ name: tagName }];
									} else {
										info.meta.tags = [...info.meta.tags, { name: tagName }];
									}
								}}
							/>
						</div>
					</div>

					<div class="my-2">
						<div class="px-4 py-3 bg-gray-50 dark:bg-gray-950 rounded-2xl">
							<AccessControl
								bind:accessControl
								accessRoles={['read', 'write']}
								allowPublic={$user?.permissions?.sharing?.public_models || $user?.role === 'admin'}
							/>
						</div>
					</div>

					<hr class=" border-gray-100 dark:border-gray-850 my-1.5" />

					<div class="my-2">
						<div class="flex w-full justify-between">
							<div class=" self-center text-sm font-semibold">{$i18n.t('Model Params')}</div>
						</div>

						<div class="mt-2">
							<div class="my-1">
								<div class=" text-xs font-semibold mb-2">{$i18n.t('System Prompt')}</div>
								<div class="text-xs text-gray-400 dark:text-gray-500 mb-2">
									{$i18n.t('Note: Current date is automatically appended to the system prompt.')}
								</div>
								<div>
									<Textarea
										className=" text-sm w-full bg-transparent outline-hidden resize-none overflow-y-hidden "
										placeholder={$i18n.t(
											'Write your model system prompt content here\ne.g.) You are Mario from Super Mario Bros, acting as an assistant.'
										)}
										rows={4}
										bind:value={system}
									/>
								</div>
							</div>

							<div class="flex w-full justify-between">
								<div class=" self-center text-xs font-semibold">
									{$i18n.t('Advanced Params')}
								</div>

								<button
									class="p-1 px-3 text-xs flex rounded-sm transition"
									type="button"
									onclick={() => {
										showAdvanced = !showAdvanced;
									}}
								>
									{#if showAdvanced}
										<span class="ml-2 self-center">{$i18n.t('Hide')}</span>
									{:else}
										<span class="ml-2 self-center">{$i18n.t('Show')}</span>
									{/if}
								</button>
							</div>

							{#if showAdvanced}
								<div class="my-2">
									<AdvancedParams admin={true} custom={true} bind:params />
								</div>
							{/if}
						</div>
					</div>

					<hr class=" border-gray-100 dark:border-gray-850 my-1" />

					<div class="my-2">
						<div class="flex w-full justify-between items-center">
							<div class="flex w-full justify-between items-center">
								<div class=" self-center text-sm font-semibold">
									{$i18n.t('Prompt suggestions')}
								</div>

								<button
									class="p-1 text-xs flex rounded-sm transition"
									type="button"
									onclick={() => {
										if ((info?.meta?.suggestion_prompts ?? null) === null) {
											info.meta.suggestion_prompts = [{ content: '' }];
										} else {
											info.meta.suggestion_prompts = null;
										}
									}}
								>
									{#if (info?.meta?.suggestion_prompts ?? null) === null}
										<span class="ml-2 self-center">{$i18n.t('Default')}</span>
									{:else}
										<span class="ml-2 self-center">{$i18n.t('Custom')}</span>
									{/if}
								</button>
							</div>

							{#if (info?.meta?.suggestion_prompts ?? null) !== null}
								<button
									class="p-1 px-2 text-xs flex rounded-sm transition"
									type="button"
									onclick={() => {
										if (
											info.meta.suggestion_prompts.length === 0 ||
											info.meta.suggestion_prompts.at(-1).content !== ''
										) {
											info.meta.suggestion_prompts = [
												...info.meta.suggestion_prompts,
												{ content: '' }
											];
										}
									}}
								>
									<svg
										xmlns="http://www.w3.org/2000/svg"
										viewBox="0 0 20 20"
										fill="currentColor"
										class="w-4 h-4"
									>
										<path
											d="M10.75 4.75a.75.75 0 00-1.5 0v4.5h-4.5a.75.75 0 000 1.5h4.5v4.5a.75.75 0 001.5 0v-4.5h4.5a.75.75 0 000-1.5h-4.5v-4.5z"
										/>
									</svg>
								</button>
							{/if}
						</div>

						{#if info?.meta?.suggestion_prompts}
							<div class="flex flex-col space-y-1 mt-1 mb-3">
								{#if info.meta.suggestion_prompts.length > 0}
									{#each info.meta.suggestion_prompts as prompt, promptIdx}
										<div class=" flex rounded-lg">
											<input
												class=" text-sm w-full bg-transparent outline-hidden border-r border-gray-100 dark:border-gray-850"
												placeholder={$i18n.t('Write a prompt suggestion (e.g. Who are you?)')}
												bind:value={prompt.content}
											/>

											<button
												class="px-2"
												type="button"
												onclick={() => {
													info.meta.suggestion_prompts.splice(promptIdx, 1);
													info.meta.suggestion_prompts = info.meta.suggestion_prompts;
												}}
											>
												<XMark className={'size-4'} />
											</button>
										</div>
									{/each}
								{:else}
									<div class="text-xs text-center">{$i18n.t('No suggestion prompts')}</div>
								{/if}
							</div>
						{/if}
					</div>

					<hr class=" border-gray-100 dark:border-gray-850 my-1.5" />

					<div class="my-2">
						<FiltersSelector
							bind:selectedFilterIds={filterIds}
							filters={$functions.filter((func) => func.type === 'filter')}
						/>
					</div>

					{#if filterIds.length > 0}
						{@const toggleableFilters = $functions.filter(
							(func) =>
								func.type === 'filter' &&
								(filterIds.includes(func.id) || func?.is_global) &&
								func?.meta?.toggle
						)}

						{#if toggleableFilters.length > 0}
							<div class="my-2">
								<DefaultFiltersSelector
									bind:selectedFilterIds={defaultFilterIds}
									filters={toggleableFilters}
								/>
							</div>
						{/if}
					{/if}

					<div class="my-2">
						<ActionsSelector
							bind:selectedActionIds={actionIds}
							actions={$functions.filter((func) => func.type === 'action')}
						/>
					</div>

					<div class="my-2">
						<Capabilities bind:capabilities autoDetected={autoDetectedCapabilities} />
					</div>

					<div class="my-2">
						<div class="px-4 py-3 bg-gray-50 dark:bg-gray-950 rounded-2xl">
							<div class="flex w-full justify-between items-center">
								<div class="self-center text-sm font-semibold">{$i18n.t('Reasoning')}</div>
							</div>

							<div class="mt-2 flex justify-between items-center">
								<div class="text-xs font-semibold">{$i18n.t('Reasoning model')}</div>
								<div class="pr-2">
									<Switch bind:state={reasoningModelEnabled} />
								</div>
							</div>
							<div class="mt-1 text-xs text-gray-500 dark:text-gray-500">
								{$i18n.t(
									'Controls whether the chat UI should expose reasoning effort controls for this model.'
								)}
							</div>

							{#if reasoningModelEnabled}
								<!-- Discovered reasoning efforts (OpenRouter auto-discovery) -->
								<div
									class="mt-3 rounded-xl bg-white dark:bg-gray-900 px-3 py-2.5 border-hairline border-gray-100 dark:border-gray-800"
								>
									<div class="flex items-center justify-between">
										<div class="text-xs font-semibold">{$i18n.t('Discovered from provider')}</div>
										<button
											type="button"
											class="text-[11px] text-book-cloth dark:text-kraft hover:underline disabled:opacity-50 disabled:no-underline"
											disabled={discoveringReasoning || !openrouterBaseModelId}
											onclick={() => fetchDiscoveredReasoning(openrouterBaseModelId, true)}
										>
											{discoveringReasoning ? $i18n.t('Discovering…') : $i18n.t('Re-discover')}
										</button>
									</div>

									<div class="mt-1.5">
										{#if discoveringReasoning}
											<div class="text-xs text-gray-400 dark:text-gray-500 italic">
												{$i18n.t('Discovering reasoning support…')}
											</div>
										{:else if discoveredReasoning && (discoveredReasoning.supported_efforts?.length ?? 0) > 0}
											<div class="flex flex-wrap items-center gap-1.5">
												{#each orderReasoningEfforts(discoveredReasoning.supported_efforts) as effort}
													<span
														class="px-2 py-0.5 rounded-full text-[11px] font-medium bg-book-cloth/15 text-book-cloth dark:text-kraft border-hairline border-book-cloth/20"
													>
														{effort}
													</span>
												{/each}
												{#if discoveredReasoning.mandatory}
													<span
														class="px-2 py-0.5 rounded-full text-[11px] font-medium bg-warning/15 text-warning dark:text-warning-dark"
													>
														{$i18n.t('required')}
													</span>
												{/if}
											</div>
											{#if discoveredReasoning.default_effort}
												<div class="text-[11px] text-gray-500 dark:text-gray-400 mt-1">
													{$i18n.t('Default')}: {discoveredReasoning.default_effort}
												</div>
											{/if}
										{:else if discoveredReasoning}
											<div class="text-xs text-gray-500 dark:text-gray-400">
												{$i18n.t(
													'This model reasons but the provider exposes no selectable effort levels.'
												)}
											</div>
										{:else}
											<div class="text-xs text-gray-400 dark:text-gray-500 italic">
												{$i18n.t(
													'No provider reasoning data (not an OpenRouter model, or none advertised).'
												)}
											</div>
										{/if}
									</div>
								</div>

								<!-- Mode: Automatic (discovered) vs Manual override -->
								<div class="mt-3">
									<div
										class="grid grid-cols-2 gap-2 bg-gray-100 dark:bg-gray-800 p-1 rounded-lg text-xs"
									>
										<button
											type="button"
											class="py-1.5 rounded-md font-medium transition-all {reasoningMode === 'auto'
												? 'bg-white dark:bg-gray-850 text-book-cloth dark:text-kraft shadow-sm'
												: 'text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-300'}"
											onclick={() => (reasoningMode = 'auto')}
										>
											{$i18n.t('Automatic')}
										</button>
										<button
											type="button"
											class="py-1.5 rounded-md font-medium transition-all {reasoningMode ===
											'manual'
												? 'bg-white dark:bg-gray-850 text-book-cloth dark:text-kraft shadow-sm'
												: 'text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-300'}"
											onclick={() => {
												// Seed the manual set from whatever the model would use now.
												if (manualReasoningEfforts.length === 0) {
													manualReasoningEfforts = [...autoEffectiveEfforts];
												}
												reasoningMode = 'manual';
											}}
										>
											{$i18n.t('Manual override')}
										</button>
									</div>

									{#if reasoningMode === 'auto'}
										<div class="mt-2 text-xs text-gray-500 dark:text-gray-500">
											{$i18n.t(
												'Uses the efforts discovered from the provider and updates automatically. Falls back to low/medium/high when none are advertised.'
											)}
										</div>
										<div class="mt-2 flex flex-wrap gap-1.5">
											{#each autoEffectiveEfforts as effort}
												<span
													class="px-2 py-0.5 rounded-full text-[11px] font-medium bg-gray-100 dark:bg-gray-800 text-gray-600 dark:text-gray-300"
												>
													{effort}
												</span>
											{/each}
										</div>
									{:else}
										<div class="mt-2 text-xs text-gray-500 dark:text-gray-500 mb-2">
											{$i18n.t(
												'Choose exactly which reasoning efforts to offer in chat. Overrides discovery.'
											)}
										</div>
										<div class="flex flex-wrap gap-3 text-xs">
											{#each ALL_REASONING_EFFORTS as effort}
												<label class="flex items-center gap-2">
													<input
														type="checkbox"
														checked={manualReasoningEfforts.includes(effort)}
														onchange={() => toggleManualEffort(effort)}
													/>
													<span>{effort}</span>
												</label>
											{/each}
										</div>
										{#if manualReasoningEfforts.length === 0}
											<div class="text-[11px] text-warning dark:text-warning-dark mt-1.5">
												{$i18n.t('Nothing selected — the model will fall back to defaults.')}
											</div>
										{/if}
									{/if}
								</div>
							{/if}
						</div>
					</div>

					<div class="my-2">
						<div class="px-4 py-3 bg-gray-50 dark:bg-gray-950 rounded-2xl">
							<div class="flex w-full justify-between items-center">
								<div class="self-center text-sm font-semibold">{$i18n.t('Service Tier')}</div>
								<div class="pr-2">
									<Switch bind:state={serviceTierEnabled} />
								</div>
							</div>
							<div class="mt-1 text-xs text-gray-500 dark:text-gray-500">
								{$i18n.t(
									'Controls whether the service tier selector is shown for this model in the chat UI.'
								)}
							</div>

							<div class="mt-3">
								<div class="text-xs font-semibold mb-1">{$i18n.t('Allowed tier values')}</div>
								<div class="text-xs text-gray-500 dark:text-gray-500 mb-2">
									{$i18n.t(
										'Comma-separated. OpenAI uses "default, flex, priority"; Gemini uses "standard, flex, priority". Leave blank for the OpenAI default.'
									)}
								</div>
								<input
									type="text"
									disabled={!serviceTierEnabled}
									bind:value={serviceTierValues}
									placeholder="default, flex, priority"
									class="text-sm w-full bg-transparent outline-hidden border-hairline rounded-lg px-3 py-2"
								/>
							</div>
						</div>
					</div>

					<div class="my-2">
						<PeakHours
							bind:enabled={peakHoursEnabled}
							bind:blocks={peakHoursBlocks}
							bind:note={peakHoursNote}
						/>
					</div>

					<div class="my-2">
						<div class="px-4 py-3 bg-gray-50 dark:bg-gray-950 rounded-2xl">
							<div class="flex w-full justify-between items-center">
								<div class="self-center text-sm font-semibold">{$i18n.t('Cache Control')}</div>
								<div class="pr-2">
									<Switch bind:state={cacheControlEphemeralEnabled} />
								</div>
							</div>
							<div class="mt-1 text-xs text-gray-500 dark:text-gray-500">
								{$i18n.t(
									'Adds cache_control: { type: "ephemeral" } to the last message before forwarding the request upstream.'
								)}
							</div>
						</div>
					</div>

					<OpenRouterProviderSelector
						baseModelId={openrouterBaseModelId}
						bind:providerOnly={openrouterProviderOnly}
						bind:providerOrder={openrouterProviderOrder}
					/>

					{#if !capabilities.vision}
						<div class="my-2 p-4 border-hairline rounded-lg bg-gray-50 dark:bg-gray-950">
							<h3 class="text-sm font-semibold mb-3">Vision Preprocessor (for image inputs)</h3>
							<div class="mb-3">
								<label class="block text-xs font-semibold mb-1">Preprocessor Model</label>
								<select
									bind:value={vision_preprocessor_model_id}
									class="text-sm w-full bg-transparent outline-hidden border-hairline rounded-lg px-3 py-2"
								>
									<option value="">Select a vision model</option>
									{#each visionPreprocessorModels as m}
										<option value={m.id}>{m.name}</option>
									{/each}
								</select>
							</div>
							<div>
								<label class="block text-xs font-semibold mb-1">Custom Prompt Template</label>
								<Textarea
									className="text-sm w-full bg-transparent outline-hidden resize-none"
									placeholder={visionPlaceholder}
									bind:value={vision_preprocessor_prompt}
									rows={4}
								/>
							</div>
						</div>
					{/if}

					<hr class=" border-gray-100 dark:border-gray-850 my-1.5" />

					<div class="my-2">
						<DefaultToolsAndFeatures
							tools={$tools}
							bind:selectedToolIds={toolIds}
							{capabilities}
							bind:featureIds={defaultFeatureIds}
						/>
					</div>

					<div class="my-2 text-gray-300 dark:text-gray-700">
						<div class="flex w-full justify-between mb-2">
							<div class=" self-center text-sm font-semibold">{$i18n.t('JSON Preview')}</div>

							<button
								class="p-1 px-3 text-xs flex rounded-sm transition"
								type="button"
								onclick={() => {
									showPreview = !showPreview;
								}}
							>
								{#if showPreview}
									<span class="ml-2 self-center">{$i18n.t('Hide')}</span>
								{:else}
									<span class="ml-2 self-center">{$i18n.t('Show')}</span>
								{/if}
							</button>
						</div>

						{#if showPreview}
							<div>
								<textarea
									class="text-sm w-full bg-transparent outline-hidden resize-none"
									rows="10"
									value={JSON.stringify(info, null, 2)}
									disabled
									readonly></textarea>
							</div>
						{/if}
					</div>

					<div class="my-2 flex justify-end pb-20">
						<button
							class=" text-sm px-3 py-2 transition-colors duration-200 ease-paper rounded-full {loading
								? ' cursor-not-allowed bg-book-cloth hover:bg-kraft text-white dark:bg-book-cloth dark:hover:bg-kraft dark:text-white'
								: 'bg-book-cloth hover:bg-kraft text-white dark:bg-book-cloth dark:hover:bg-kraft dark:text-white'} flex w-full justify-center"
							type="submit"
							disabled={loading}
						>
							<div class=" self-center font-medium">
								{#if edit}
									{$i18n.t('Save & Update')}
								{:else}
									{$i18n.t('Save & Create')}
								{/if}
							</div>

							{#if loading}
								<div class="ml-1.5 self-center">
									<Spinner />
								</div>
							{/if}
						</button>
					</div>
				</div>
			</form>
		{/if}
	</div>
{/if}
