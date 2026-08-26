<script lang="ts">
	import { models, showSettings, settings, user, mobile, config } from '$lib/stores';
	import { onMount, tick, getContext } from 'svelte';
	import { toast } from '$lib/utils/toast';
	import Selector from './ModelSelector/Selector.svelte';
	import Tooltip from '../common/Tooltip.svelte';
	import PeakHoursNotice from './PeakHoursNotice.svelte';

	import { updateUserSettings } from '$lib/apis/users';
	const i18n = getContext('i18n');

	interface Props {
		selectedModels?: string[];
		disabled?: boolean;
		showSetDefault?: boolean;
		onModelsChange?: (modelIds: string[]) => void;
	}

	let {
		selectedModels = [''],
		disabled = false,
		showSetDefault = true,
		onModelsChange = () => {}
	}: Props = $props();
	let savingDefault = $state(false);

	const updateModels = (modelIds: string[]) => {
		onModelsChange([...modelIds]);
	};

	const saveDefaultModel = async () => {
		if (selectedModels.some((modelId) => modelId === '')) {
			toast.error($i18n.t('Choose a model before saving...'));
			return;
		}

		savingDefault = true;
		try {
			const nextUi = { ...$settings, models: [...selectedModels] };
			const savedSettings = await updateUserSettings(localStorage.token, { ui: nextUi });
			if (!savedSettings?.ui) {
				throw new Error($i18n.t('The server did not confirm the settings update.'));
			}

			settings.set(savedSettings.ui);
			try {
				// The settings store is seeded from this cache before the fresh settings
				// request completes on reload. Keep it in sync so a new chat does not
				// briefly initialize from (and retain) the previous default model.
				localStorage.setItem('settings', JSON.stringify(savedSettings));
			} catch (error) {
				console.warn('Failed to cache updated user settings', error);
			}

			toast.success($i18n.t('Default model updated'));
		} catch (error) {
			const detail = error?.detail ?? error?.message ?? `${error}`;
			toast.error(
				$i18n.t('Failed to save settings: {{error}}', {
					error: detail
				})
			);
		} finally {
			savingDefault = false;
		}
	};

	const pinModelHandler = async (modelId: string) => {
		let pinnedModels: string[] = $settings?.pinnedModels ?? [];

		if (pinnedModels.includes(modelId)) {
			pinnedModels = pinnedModels.filter((id) => id !== modelId);
		} else {
			pinnedModels = [...new Set([...pinnedModels, modelId])];
		}

		settings.set({ ...$settings, pinnedModels: pinnedModels });
		await updateUserSettings(localStorage.token, { ui: $settings });
	};

	// NOTE: ModelSelector never rewrites `selectedModels` reactively. Chat.svelte
	// owns the single reconciler that maps selectedModels against $models — a second automatic
	// writer here previously raced it in the same reactive flush (this block
	// would wipe an unrecognized/stale id to '' while Chat's reconciler refilled
	// it), desyncing the two-level bind chain into the Selector so the picker
	// got stuck showing "Select a model" even though the placeholder above the
	// composer correctly showed the refilled model.
</script>

<div class="flex flex-col w-full items-start">
	{#each selectedModels as selectedModel, selectedModelIdx (selectedModelIdx)}
		<div class="flex w-full max-w-fit">
			<div class="overflow-hidden w-full">
				<div class="max-w-full mr-1">
					<Selector
						id={`${selectedModelIdx}`}
						placeholder={$i18n.t('Select a model')}
						items={$models.map((model) => ({
							value: model.id,
							label: model.name,
							model: model
						}))}
						{pinModelHandler}
						value={selectedModel}
						onSelect={(modelId) => {
							const next = [...selectedModels];
							next[selectedModelIdx] = modelId;
							updateModels(next);
						}}
					/>
				</div>
			</div>

			{#if $user?.role === 'admin' || ($user?.permissions?.chat?.multiple_models ?? true)}
				{#if selectedModelIdx === 0}
					<div class="self-center mx-1 disabled:text-gray-600 disabled:hover:text-gray-600">
						<Tooltip content={$i18n.t('Add Model')}>
							<button
								class="inline-flex items-center justify-center max-md:p-2.5 max-md:min-w-10 max-md:min-h-10"
								{disabled}
								onclick={() => {
									updateModels([...selectedModels, '']);
								}}
								aria-label="Add Model"
							>
								<svg
									xmlns="http://www.w3.org/2000/svg"
									fill="none"
									viewBox="0 0 24 24"
									stroke-width="2"
									stroke="currentColor"
									class="size-3.5"
								>
									<path stroke-linecap="round" stroke-linejoin="round" d="M12 6v12m6-6H6" />
								</svg>
							</button>
						</Tooltip>
					</div>
				{:else}
					<div class="self-center mx-1 disabled:text-gray-600 disabled:hover:text-gray-600">
						<Tooltip content={$i18n.t('Remove Model')}>
							<button
								class="inline-flex items-center justify-center max-md:p-2.5 max-md:min-w-10 max-md:min-h-10"
								{disabled}
								onclick={() => {
									updateModels(selectedModels.filter((_, idx) => idx !== selectedModelIdx));
								}}
								aria-label="Remove Model"
							>
								<svg
									xmlns="http://www.w3.org/2000/svg"
									fill="none"
									viewBox="0 0 24 24"
									stroke-width="2"
									stroke="currentColor"
									class="size-3"
								>
									<path stroke-linecap="round" stroke-linejoin="round" d="M19.5 12h-15" />
								</svg>
							</button>
						</Tooltip>
					</div>
				{/if}
			{/if}
		</div>

		<PeakHoursNotice
			model={$models.find((m) => m.id === selectedModel)}
			className="mt-1 px-0.5 mb-0.5"
		/>
	{/each}
</div>

{#if showSetDefault}
	<div
		class="relative text-left mt-[1px] px-0.5 text-xs text-gray-600 dark:text-gray-400 font-primary"
	>
		<button
			type="button"
			class="hover:text-gray-900 dark:hover:text-gray-200 transition disabled:opacity-50"
			onclick={saveDefaultModel}
			disabled={savingDefault}
		>
			{$i18n.t('Set as default')}</button
		>
	</div>
{/if}
