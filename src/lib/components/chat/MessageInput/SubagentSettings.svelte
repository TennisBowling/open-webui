<script lang="ts">
	import { preventDefault } from '$lib/utils/eventModifiers';
	import { DropdownMenu } from 'bits-ui';
	import { getContext, tick } from 'svelte';

	import { flyAndScale } from '$lib/utils/transitions';
	import { isOnScreenKeyboardDevice } from '$lib/utils/device';
	import { models } from '$lib/stores';

	import Dropdown from '$lib/components/common/Dropdown.svelte';
	import Switch from '$lib/components/common/Switch.svelte';
	import Tooltip from '$lib/components/common/Tooltip.svelte';
	import Knobs from '$lib/components/icons/Knobs.svelte';

	const i18n: any = getContext('i18n');

	// Per-chat subagent model override. Empty = inherit the resolved default
	// (admin SUBAGENT_DEFAULT_MODEL, else the parent chat's model). Bound up to

	// Resolved by the parent (MessageInput) from the effective subagent
	// model's `info.meta.service_tiers`, falling back to the canonical set.
	// We don't recompute it here so the dropdown stays in sync with whatever

	interface Props {
		subagentReasoningEffort?: string;
		subagentServiceTier?: string;
		// Chat.svelte and persisted to chat.params.subagentModel.
		subagentModel?: string;
		subagentExternalToolsEnabled?: boolean;
		allowExternalTools?: boolean;
		selectedExternalToolCount?: number;
		containerWorkspaceActive?: boolean;
		// model the user has selected — even if that changes mid-session.
		allowedServiceTiers?: readonly string[];
		onSelectionTouched?: () => void;
	}

	let {
		subagentReasoningEffort = $bindable(''),
		subagentServiceTier = $bindable(''),
		subagentModel = $bindable(''),
		subagentExternalToolsEnabled = $bindable(true),
		allowExternalTools = false,
		selectedExternalToolCount = 0,
		containerWorkspaceActive = false,
		allowedServiceTiers = ['default', 'flex', 'priority'],
		onSelectionTouched = () => {}
	}: Props = $props();

	let show = $state(false);

	// Mirror InputMenu/IntegrationsMenu: when the popover closes, return focus to
	// the chat input so the mobile keyboard isn't left dismissed. The change-guard
	// (show !== prevShow) prevents this reactive from re-firing on unrelated
	// updates and avoids re-invalidating its own dependency. prevShow starts false
	// so it never fires on initial mount.
	let prevShow = $state(false);
	$effect(() => {
		if (show !== prevShow) {
			const wasOpen = prevShow;
			prevShow = show;
			if (wasOpen && !show && !isOnScreenKeyboardDevice()) {
				tick().then(() => document.getElementById('chat-input')?.focus());
			}
		}
	});

	// Picklist for the per-chat subagent model: the user's available models
	// minus admin-hidden ones, mirroring the navbar selector. If the chat is
	// already pinned to a model that is now hidden — or to a stale id no longer
	// in $models at all — we still surface it (prepended here / rendered as a
	// raw-id option in the template) so the native <select bind:value> can't
	// desync to a phantom default and silently drop the pinned model.
	let selectableSubagentModels = $derived(
		$models.filter((m) => m?.id && !((m?.info?.meta as any)?.hidden ?? false))
	);
	let subagentModelOptions = $derived(
		subagentModel && !selectableSubagentModels.some((m) => m.id === subagentModel)
			? (() => {
					const cur = $models.find((m) => m.id === subagentModel);
					return cur ? [cur, ...selectableSubagentModels] : selectableSubagentModels;
				})()
			: selectableSubagentModels
	);
	let subagentModelMissing = $derived(
		!!subagentModel && !$models.some((m) => m.id === subagentModel)
	);

	let hasOverride = $derived(
		!!(
			subagentModel ||
			subagentReasoningEffort ||
			subagentServiceTier ||
			(allowExternalTools && subagentExternalToolsEnabled)
		)
	);
	let externalToolsLabel = $derived(
		containerWorkspaceActive ? 'External tools and container' : 'External tools'
	);
	let externalToolsDescription = $derived(
		containerWorkspaceActive
			? 'Give subagents access to selected admin external tools, including this chat’s shared container workspace.'
			: 'Give subagents access to selected admin external tool servers for this chat.'
	);
</script>

<Dropdown bind:show closeOnOutsideClick={true}>
	<Tooltip content={$i18n.t('Subagent settings')} placement="top">
		<button
			type="button"
			onmousedown={preventDefault()}
			class="relative p-2 max-md:p-2.5 flex items-center text-sm rounded-full transition-colors duration-300 focus:outline-hidden bg-transparent text-gray-600 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-800 border-hairline border-transparent hover:border-gray-200 dark:hover:border-gray-700"
			aria-label={$i18n.t('Subagent settings')}
		>
			<Knobs className="size-4" />
			{#if hasOverride}
				<span
					class="absolute top-1 right-1 w-1.5 h-1.5 rounded-full bg-book-cloth dark:bg-book-cloth"
					aria-hidden="true"
				></span>
			{/if}
		</button>
	</Tooltip>

	{#snippet content()}
		<div>
			<DropdownMenu.Content
				class="w-72 rounded-2xl px-3 py-3 border-hairline border-gray-100 dark:border-gray-800 z-50 bg-white dark:bg-gray-850 dark:text-white shadow-lg"
				sideOffset={8}
				side="top"
				align="start"
				transition={flyAndScale}
			>
				<div class="text-xs font-semibold text-gray-700 dark:text-gray-200 px-1 mb-2">
					{$i18n.t('Subagent settings')}
				</div>

				<div class="mb-3">
					<label
						for="subagent-settings-model"
						class="block text-xs font-medium text-gray-700 dark:text-gray-300 mb-1 px-1"
					>
						{$i18n.t('Subagent model')}
					</label>
					<select
						id="subagent-settings-model"
						bind:value={subagentModel}
						class="w-full rounded-lg py-1.5 px-3 text-sm bg-gray-50 dark:bg-gray-900 dark:text-gray-100 outline-hidden border-hairline border-gray-200 dark:border-gray-700"
					>
						<option value="">{$i18n.t('default (admin)')}</option>
						{#if subagentModelMissing}
							<option value={subagentModel}>{subagentModel}</option>
						{/if}
						{#each subagentModelOptions as model (model.id)}
							<option value={model.id}>{model.name}</option>
						{/each}
					</select>
					<p class="text-[10px] italic text-gray-500 dark:text-gray-400 mt-1 px-1">
						{$i18n.t('Empty inherits the resolved default model.')}
					</p>
				</div>

				{#if allowExternalTools}
					<div class="mb-3 px-1">
						<div class="flex items-start justify-between gap-3">
							<div class="min-w-0">
								<div class="text-xs font-medium text-gray-700 dark:text-gray-300">
									{$i18n.t(externalToolsLabel)}
								</div>
								<p class="text-[10px] italic text-gray-500 dark:text-gray-400 mt-1">
									{$i18n.t(externalToolsDescription)}
									{#if selectedExternalToolCount === 0}
										{$i18n.t(' No external tools are selected yet.')}
									{/if}
								</p>
							</div>
							<div class="shrink-0 pt-0.5">
								<Switch bind:state={subagentExternalToolsEnabled} onchange={onSelectionTouched} />
							</div>
						</div>
					</div>
				{/if}

				<div class="mb-3">
					<label
						for="subagent-settings-reasoning"
						class="block text-xs font-medium text-gray-700 dark:text-gray-300 mb-1 px-1"
					>
						{$i18n.t('Reasoning effort')}
					</label>
					<select
						id="subagent-settings-reasoning"
						bind:value={subagentReasoningEffort}
						class="w-full rounded-lg py-1.5 px-3 text-sm bg-gray-50 dark:bg-gray-900 dark:text-gray-100 outline-hidden border-hairline border-gray-200 dark:border-gray-700"
					>
						<option value="">{$i18n.t('default (admin)')}</option>
						<option value="minimal">minimal</option>
						<option value="low">low</option>
						<option value="medium">medium</option>
						<option value="high">high</option>
						<option value="xhigh">xhigh</option>
						<option value="max">max</option>
					</select>
					<p class="text-[10px] italic text-gray-500 dark:text-gray-400 mt-1 px-1">
						{$i18n.t('Empty inherits the admin default.')}
					</p>
				</div>

				<div>
					<label
						for="subagent-settings-tier"
						class="block text-xs font-medium text-gray-700 dark:text-gray-300 mb-1 px-1"
					>
						{$i18n.t('Service tier')}
					</label>
					<select
						id="subagent-settings-tier"
						bind:value={subagentServiceTier}
						class="w-full rounded-lg py-1.5 px-3 text-sm bg-gray-50 dark:bg-gray-900 dark:text-gray-100 outline-hidden border-hairline border-gray-200 dark:border-gray-700"
					>
						<option value="">{$i18n.t('default (admin)')}</option>
						{#each allowedServiceTiers as tier}
							<option value={tier}>{tier}</option>
						{/each}
					</select>
					<p class="text-[10px] italic text-gray-500 dark:text-gray-400 mt-1 px-1">
						{$i18n.t('Restricted to the subagent model’s supported tiers.')}
					</p>
				</div>
			</DropdownMenu.Content>
		</div>
	{/snippet}
</Dropdown>
