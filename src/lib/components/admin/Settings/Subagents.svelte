<script lang="ts">
	import { preventDefault } from '$lib/utils/eventModifiers';

	import { onMount, getContext } from 'svelte';
	import { toast } from '$lib/utils/toast';

	import { getSubagentsConfig, updateSubagentsConfig } from '$lib/apis/subagents';
	import { models } from '$lib/stores';
	import Switch from '$lib/components/common/Switch.svelte';

	const i18n: any = getContext('i18n');

	interface Props {
		saveHandler: Function;
	}

	let { saveHandler }: Props = $props();

	let enableSubagents = $state(false);
	let defaultModel = $state('');
	let contextFallbackModel = $state('');
	let parentPrompt = $state('');
	let systemPromptAppend = $state('');
	let defaultReasoningEffort = $state('');
	let defaultServiceTier = $state('');
	let allowExternalTools = $state(false);
	let externalToolsPrompt = $state('');

	// Cached default so the "Reset" button works without a round-trip.
	let initialParentPrompt = $state('');
	let initialSystemPromptAppend = $state('');
	let initialExternalToolsPrompt = $state('');

	// Common reasoning_effort values. The backend just passes the string
	// through to the provider, so a custom value works too — admin can edit
	// the value in env / config.json directly if they need something exotic.
	const REASONING_EFFORT_OPTIONS = [
		{ value: '', label: '— Model default —' },
		{ value: 'minimal', label: 'minimal' },
		{ value: 'low', label: 'low' },
		{ value: 'medium', label: 'medium' },
		{ value: 'high', label: 'high' },
		{ value: 'xhigh', label: 'xhigh' }
	];

	// Canonical OpenAI/OpenRouter service tiers. Same list MessageInput uses
	// for the parent chat (`DEFAULT_SERVICE_TIERS`). The backend passes the
	// string through unchanged, so an admin can drop in a provider-specific
	// value via env / config.json if they need one not in this list.
	const SERVICE_TIER_OPTIONS = [
		{ value: '', label: '— Don’t send —' },
		{ value: 'default', label: 'default' },
		{ value: 'flex', label: 'flex' },
		{ value: 'priority', label: 'priority' }
	];

	const submitHandler = async () => {
		try {
			const res = await updateSubagentsConfig(localStorage.token, {
				ENABLE_SUBAGENTS: enableSubagents,
				SUBAGENT_DEFAULT_MODEL: defaultModel,
				SUBAGENT_CONTEXT_FALLBACK_MODEL: contextFallbackModel,
				SUBAGENT_SYSTEM_PROMPT_APPEND: systemPromptAppend,
				SUBAGENT_PARENT_PROMPT: parentPrompt,
				SUBAGENT_DEFAULT_REASONING_EFFORT: defaultReasoningEffort,
				SUBAGENT_DEFAULT_SERVICE_TIER: defaultServiceTier,
				SUBAGENT_ALLOW_EXTERNAL_TOOLS: allowExternalTools,
				SUBAGENT_EXTERNAL_TOOLS_PROMPT: externalToolsPrompt
				// Note: the subagent's INNER system prompt is intentionally the
				// model's own admin-set system prompt verbatim (no preamble
				// from this admin page is injected). The `SUBAGENT_SYSTEM_PROMPT`
				// config still exists on the backend for backward compat but
				// the runner ignores it.
			});
			if (res) {
				toast.success($i18n.t('Settings saved successfully'));
			}
		} catch (err) {
			console.error(err);
			toast.error(`${err}`);
		}
	};

	onMount(async () => {
		try {
			const res = await getSubagentsConfig(localStorage.token);
			if (res) {
				enableSubagents = !!res.ENABLE_SUBAGENTS;
				defaultModel = res.SUBAGENT_DEFAULT_MODEL ?? '';
				contextFallbackModel = res.SUBAGENT_CONTEXT_FALLBACK_MODEL ?? '';
				parentPrompt = res.SUBAGENT_PARENT_PROMPT ?? '';
				initialParentPrompt = parentPrompt;
				systemPromptAppend = res.SUBAGENT_SYSTEM_PROMPT_APPEND ?? '';
				initialSystemPromptAppend = systemPromptAppend;
				defaultReasoningEffort = res.SUBAGENT_DEFAULT_REASONING_EFFORT ?? '';
				defaultServiceTier = res.SUBAGENT_DEFAULT_SERVICE_TIER ?? '';
				allowExternalTools = !!res.SUBAGENT_ALLOW_EXTERNAL_TOOLS;
				externalToolsPrompt = res.SUBAGENT_EXTERNAL_TOOLS_PROMPT ?? '';
				initialExternalToolsPrompt = externalToolsPrompt;
			}
		} catch (err) {
			console.error(err);
		}
	});
</script>

<form
	class="flex flex-col h-full justify-between space-y-3 text-sm"
	onsubmit={preventDefault(async () => {
		await submitHandler();
		saveHandler();
	})}
>
	<div class=" space-y-3 overflow-y-scroll scrollbar-hidden h-full">
		<div>
			<div class="mb-3">
				<div class=" mb-2.5 text-base font-medium">{$i18n.t('Subagents')}</div>
				<div class=" text-xs text-gray-500 dark:text-gray-400 mb-2">
					{$i18n.t(
						'Subagents let the parent chat model spawn isolated research workers via the subagent_launch / subagent_continue tools. Each subagent runs in its own context with web search + fetch and returns its synthesized answer.'
					)}
				</div>

				<hr class=" border-gray-100 dark:border-gray-850 my-2" />

				<div class="mb-2.5 flex w-full justify-between">
					<div class=" self-center text-xs font-medium">
						{$i18n.t('Enable Subagents')}
					</div>
					<div class="flex items-center relative">
						<Switch bind:state={enableSubagents} />
					</div>
				</div>

				{#if enableSubagents}
					<div class="mb-2.5 flex w-full justify-between gap-4">
						<div class="min-w-0">
							<div class="self-center text-xs font-medium">
								{$i18n.t('Allow external tools in subagents')}
							</div>
							<div class="text-xs text-gray-500 dark:text-gray-400 mt-1">
								{$i18n.t(
									'When enabled, chats can opt in to give subagents access to selected admin-added external tool servers, including the configured container MCP server. Subagents still cannot spawn subagents.'
								)}
							</div>
						</div>
						<div class="flex items-center relative shrink-0">
							<Switch bind:state={allowExternalTools} />
						</div>
					</div>

					{#if allowExternalTools}
						<div class="mb-2.5 flex w-full flex-col">
							<div class="flex w-full items-center mb-1">
								<div class="self-center text-xs font-medium">
									{$i18n.t('External tools prompt append')}
								</div>
								<button
									type="button"
									class="ml-auto text-xs text-gray-500 hover:text-gray-700 dark:text-gray-400 dark:hover:text-gray-200"
									onclick={preventDefault(() => {
										externalToolsPrompt = initialExternalToolsPrompt;
									})}
								>
									{$i18n.t('Reset')}
								</button>
							</div>
							<div class="text-xs text-gray-500 dark:text-gray-400 mb-1">
								{$i18n.t(
									'Appended only when a subagent receives selected external tools. Use this to explain tool access, shared container workspace rules, and coordination with the main chat or other subagents.'
								)}
							</div>
							<textarea
								class="w-full rounded-lg py-2 px-4 text-sm bg-gray-50 dark:text-gray-300 dark:bg-gray-850 outline-hidden resize-y"
								rows="6"
								placeholder={$i18n.t('Append when subagents have external tools…')}
								bind:value={externalToolsPrompt}></textarea>
						</div>
					{/if}

					<div class="mb-2.5 flex w-full flex-col">
						<div class=" self-center text-xs font-medium mb-1 mr-auto">
							{$i18n.t('Default subagent model')}
						</div>
						<div class=" text-xs text-gray-500 dark:text-gray-400 mb-1">
							{$i18n.t(
								"Empty = fall back to the parent chat's model. A per-chat override (chat.params.subagentModel) still wins when set. The subagent uses this model's own admin-set system prompt verbatim — no additional preamble is injected."
							)}
						</div>
						<select
							class="w-full rounded-lg py-2 px-4 text-sm bg-gray-50 dark:text-gray-300 dark:bg-gray-850 outline-hidden"
							bind:value={defaultModel}
						>
							<option value="">{$i18n.t('— Use parent model —')}</option>
							{#each $models ?? [] as m}
								<option value={m.id}>{m.name ?? m.id}</option>
							{/each}
						</select>
					</div>

					<div class="mb-2.5 flex w-full flex-col">
						<div class=" self-center text-xs font-medium mb-1 mr-auto">
							{$i18n.t('Long-context fallback model')}
						</div>
						<div class=" text-xs text-gray-500 dark:text-gray-400 mb-1">
							{$i18n.t(
								'When the active subagent model runs out of input context, retry only that failed turn with this model and keep using it for the rest of the subagent conversation. Empty disables automatic fallback. Output-token limits and unrelated errors do not trigger a switch.'
							)}
						</div>
						<select
							class="w-full rounded-lg py-2 px-4 text-sm bg-gray-50 dark:text-gray-300 dark:bg-gray-850 outline-hidden"
							bind:value={contextFallbackModel}
						>
							<option value="">{$i18n.t('— Disabled —')}</option>
							{#each $models ?? [] as m}
								<option value={m.id}>{m.name ?? m.id}</option>
							{/each}
						</select>
					</div>

					<div class="mb-2.5 flex w-full flex-col">
						<div class=" self-center text-xs font-medium mb-1 mr-auto">
							{$i18n.t('Default reasoning effort')}
						</div>
						<div class=" text-xs text-gray-500 dark:text-gray-400 mb-1">
							{$i18n.t(
								'Reasoning effort the subagent uses when its model supports it. Per-chat override (chat.params.subagentReasoningEffort) wins when set. Higher effort = deeper thinking but more tokens. Default high is appropriate for research subagents.'
							)}
						</div>
						<select
							class="w-full rounded-lg py-2 px-4 text-sm bg-gray-50 dark:text-gray-300 dark:bg-gray-850 outline-hidden"
							bind:value={defaultReasoningEffort}
						>
							{#each REASONING_EFFORT_OPTIONS as opt}
								<option value={opt.value}>{opt.label}</option>
							{/each}
						</select>
					</div>

					<div class="mb-2.5 flex w-full flex-col">
						<div class=" self-center text-xs font-medium mb-1 mr-auto">
							{$i18n.t('Default service tier')}
						</div>
						<div class=" text-xs text-gray-500 dark:text-gray-400 mb-1">
							{$i18n.t(
								"Service tier the subagent's request rides on. Per-chat override (chat.params.subagentServiceTier) wins when set. Leave as “Don’t send” to let the provider use its own default — the subagent's tier is kept separate from the parent chat's per-request tier so research workers can run on a cheaper / faster lane."
							)}
						</div>
						<select
							class="w-full rounded-lg py-2 px-4 text-sm bg-gray-50 dark:text-gray-300 dark:bg-gray-850 outline-hidden"
							bind:value={defaultServiceTier}
						>
							{#each SERVICE_TIER_OPTIONS as opt}
								<option value={opt.value}>{opt.label}</option>
							{/each}
						</select>
					</div>

					<div class="mb-2.5 flex w-full flex-col">
						<div class="flex w-full items-center mb-1">
							<div class=" self-center text-xs font-medium">
								{$i18n.t('Subagent system prompt append')}
							</div>
							<button
								type="button"
								class="ml-auto text-xs text-gray-500 hover:text-gray-700 dark:text-gray-400 dark:hover:text-gray-200"
								onclick={preventDefault(() => {
									systemPromptAppend = initialSystemPromptAppend;
								})}
							>
								{$i18n.t('Reset')}
							</button>
						</div>
						<div class=" text-xs text-gray-500 dark:text-gray-400 mb-1">
							{$i18n.t(
								"Appended to every subagent's system prompt after the model's own prompt. Use this for global instructions like citation style, output format, or domain rules you want ALL subagents to follow. Leave empty to skip."
							)}
						</div>
						<textarea
							class="w-full rounded-lg py-2 px-4 text-sm bg-gray-50 dark:text-gray-300 dark:bg-gray-850 outline-hidden resize-y"
							rows="6"
							placeholder={$i18n.t('Append to subagent system prompt…')}
							bind:value={systemPromptAppend}></textarea>
					</div>

					<div class="mb-2.5 flex w-full flex-col">
						<div class="flex w-full items-center mb-1">
							<div class=" self-center text-xs font-medium">
								{$i18n.t('Parent chat instructions')}
							</div>
							<button
								type="button"
								class="ml-auto text-xs text-gray-500 hover:text-gray-700 dark:text-gray-400 dark:hover:text-gray-200"
								onclick={preventDefault(() => {
									parentPrompt = initialParentPrompt;
								})}
							>
								{$i18n.t('Reset')}
							</button>
						</div>
						<div class=" text-xs text-gray-500 dark:text-gray-400 mb-1">
							{$i18n.t(
								'Appended to the parent chat system prompt when subagents are enabled. Teaches the parent model when and how to call subagent_launch / subagent_continue.'
							)}
						</div>
						<textarea
							class="w-full rounded-lg py-2 px-4 text-sm bg-gray-50 dark:text-gray-300 dark:bg-gray-850 outline-hidden resize-y"
							rows="8"
							placeholder={$i18n.t('Parent chat instructions…')}
							bind:value={parentPrompt}></textarea>
					</div>
				{/if}
			</div>
		</div>
	</div>
	<div class="flex justify-end pt-3 text-sm font-medium">
		<button
			class="px-3.5 py-1.5 text-sm font-medium bg-book-cloth hover:bg-kraft text-white dark:bg-book-cloth dark:text-white dark:hover:bg-kraft transition-colors duration-200 ease-paper rounded-full"
			type="submit"
		>
			{$i18n.t('Save')}
		</button>
	</div>
</form>
