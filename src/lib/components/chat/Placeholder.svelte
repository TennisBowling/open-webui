<script lang="ts">
	import { dispatchComponentEvent } from '$lib/utils/componentEvents';
	import { toast } from '$lib/utils/toast';
	import { marked } from 'marked';

	import { onMount, getContext, tick } from 'svelte';
	import { blur, fade } from 'svelte/transition';

	const dispatch = (type: string, detail?: unknown) =>
		dispatchComponentEvent(eventProps, type, detail);

	import { updateFolderById } from '$lib/apis/folders';

	import {
		config,
		settings,
		user,
		modelsLoaded,
		models as _models,
		temporaryChatEnabled,
		selectedFolder
	} from '$lib/stores';
	import { sanitizeResponseContent, extractCurlyBraceWords } from '$lib/utils';
	import {
		formatSubscriptionLimitLabel,
		formatWindowLabel,
		formatUsedPercent,
		formatResetsIn
	} from '$lib/utils/subscriptionUsage';
	import { WEBUI_BASE_URL } from '$lib/constants';
	import { imageFallback } from '$lib/actions/imageFallback';

	import Suggestions from './Suggestions.svelte';
	import Tooltip from '$lib/components/common/Tooltip.svelte';
	import EyeSlash from '$lib/components/icons/EyeSlash.svelte';
	import MessageInput from './MessageInput.svelte';
	import FolderPlaceholder from './Placeholder/FolderPlaceholder.svelte';
	import FolderTitle from './Placeholder/FolderTitle.svelte';

	const i18n = getContext('i18n');

	interface Props {
		createMessagePair: Function;
		stopResponse: Function;
		autoScroll?: boolean;
		atSelectedModel: Model | undefined;
		selectedModels: [''];
		history: any;
		prompt?: string;
		files?: any;
		messageInput?: any;
		selectedToolIds?: any;
		selectedFilterIds?: any;
		onSelectionTouched?: () => void;
		onServiceTierTouched?: (tier: string) => void;
		showCommands?: boolean;
		imageGenerationEnabled?: boolean;
		webSearchEnabled?: boolean;
		studyModeEnabled?: boolean;
		dataVizEnabled?: boolean;
		automationsEnabled?: boolean;
		subagentsEnabled?: boolean;
		subagentReasoningEffort?: string;
		subagentServiceTier?: string;
		subagentModel?: string;
		subagentExternalToolsEnabled?: boolean;
		serviceTier?: string;
		onSelect?: any;
		onChange?: any;
		toolServers?: any;
		relevantGroups?: any;
		relevantSubscriptions?: any;
	}

	let {
		createMessagePair,
		stopResponse,
		autoScroll = $bindable(false),
		atSelectedModel = $bindable(),
		selectedModels = $bindable(),
		history,
		prompt = $bindable(''),
		files = $bindable([]),
		messageInput = $bindable(null),
		selectedToolIds = $bindable([]),
		selectedFilterIds = $bindable([]),
		onSelectionTouched = () => {},
		onServiceTierTouched = () => {},
		showCommands = $bindable(false),
		imageGenerationEnabled = $bindable(false),
		webSearchEnabled = $bindable(false),
		studyModeEnabled = $bindable(false),
		dataVizEnabled = $bindable(false),
		automationsEnabled = $bindable(false),
		subagentsEnabled = $bindable(false),
		subagentReasoningEffort = $bindable(''),
		subagentServiceTier = $bindable(''),
		subagentModel = $bindable(''),
		subagentExternalToolsEnabled = $bindable(true),
		serviceTier = $bindable('default'),
		onSelect = (e) => {},
		onChange = (e) => {},
		toolServers = [],
		relevantGroups = [],
		relevantSubscriptions = [],
		...eventProps
	}: Props & Record<string, unknown> = $props();

	let models = $state([]);
	let selectedModelIdx = $state(0);

	$effect(() => {
		models = selectedModels.map((id) => $_models.find((m) => m.id === id));
	});

	// Clamp only when the current index is out of range (e.g. a model was
	// removed) — do NOT force-jump to the last model on every array change,
	// which used to stomp the user's explicit avatar click and disagreed with
	// the picker (which shows the FIRST selected model).
	$effect(() => {
		if (selectedModelIdx > models.length - 1) {
			selectedModelIdx = Math.max(0, models.length - 1);
		}
	});
</script>

<!-- m-auto centres on the flex cross axis (it beats the parent's items-start, and
     auto margins collapse to 0 when the block is taller than the viewport, so a
     small screen scrolls instead of clipping). Mobile used to opt out of the
     centring with my-0 so that opening the iOS keyboard couldn't pan the model
     selector off-screen — the --app-offset-top glue in keyboardViewport.ts owns
     that now, and the opt-out left the landing screen hugging the navbar with
     two-thirds of a phone blank underneath. Typing mode still pins to the top,
     via .chat-placeholder in app.css. -->
<div
	class="chat-placeholder m-auto w-full max-w-6xl px-2 @2xl:px-20 translate-y-6 max-md:translate-y-0 py-24 max-md:pt-14 max-md:pb-6 text-center"
>
	{#if $temporaryChatEnabled}
		<Tooltip
			content={$i18n.t("This chat won't appear in history and your messages will not be saved.")}
			className="w-full flex justify-center mb-0.5"
			placement="top"
		>
			<div class="flex items-center gap-2 text-gray-500 text-base my-2 w-fit">
				<EyeSlash strokeWidth="2.5" className="size-4" />{$i18n.t('Temporary Chat')}
			</div>
		</Tooltip>
	{/if}

	<div
		class="w-full text-3xl text-gray-800 dark:text-gray-100 text-center flex items-center gap-4 font-primary"
	>
		<div class="w-full flex flex-col justify-center items-center">
			{#if $selectedFolder}
				<FolderTitle
					folder={$selectedFolder}
					onUpdate={async (folder) => {
						// sidebarSync handles folder:updated broadcasts; no local refetch needed.
					}}
					onDelete={async () => {
						// sidebarSync handles folder:deleted (cascades to chats refetch); only clear selection here.
						selectedFolder.set(null);
					}}
				/>
			{:else}
				<div class="flex flex-row justify-center gap-3 @sm:gap-3.5 w-fit px-5 max-w-xl">
					<div class="flex shrink-0 justify-center">
						<div class="flex -space-x-4 mb-0.5" in:fade={{ duration: 100 }}>
							{#each models as model, modelIdx}
								<Tooltip
									content={(models[modelIdx]?.info?.meta?.tags ?? [])
										.map((tag) => tag.name.toUpperCase())
										.join(', ')}
									placement="top"
								>
									<button
										aria-hidden={models.length <= 1}
										aria-label={$i18n.t('Get information on {{name}} in the UI', {
											name: models[modelIdx]?.name
										})}
										onclick={() => {
											selectedModelIdx = modelIdx;
										}}
									>
										<img
											use:imageFallback
											src={model?.info?.meta?.profile_image_url ??
												($i18n.language === 'dg-DG'
													? `${WEBUI_BASE_URL}/doge.png`
													: `${WEBUI_BASE_URL}/static/favicon.png`)}
											class=" size-9 @sm:size-10 rounded-full border-hairline border-gray-100 dark:border-none"
											aria-hidden="true"
											draggable="false"
											decoding="async"
										/>
									</button>
								</Tooltip>
							{/each}
						</div>
					</div>

					<div
						class=" text-3xl @sm:text-3xl line-clamp-1 flex items-center"
						in:fade={{ duration: 100 }}
					>
						{#if models[selectedModelIdx]?.name}
							<Tooltip
								content={models[selectedModelIdx]?.name}
								placement="top"
								className=" flex items-center "
							>
								<span class="line-clamp-1">
									{models[selectedModelIdx]?.name}
								</span>
							</Tooltip>
						{:else if !$modelsLoaded}
							{$i18n.t('Loading...')}
						{:else}
							{$i18n.t('Hello, {{name}}', { name: $user?.name })}
						{/if}
					</div>
				</div>

				<div class="flex mt-1 mb-2">
					<div in:fade={{ duration: 100, delay: 50 }}>
						{#if models[selectedModelIdx]?.info?.meta?.description ?? null}
							<Tooltip
								className=" w-fit"
								content={marked.parse(
									sanitizeResponseContent(
										models[selectedModelIdx]?.info?.meta?.description ?? ''
									).replaceAll('\n', '<br>')
								)}
								placement="top"
							>
								<div
									class="mt-0.5 px-2 text-sm font-normal text-gray-500 dark:text-gray-400 line-clamp-2 max-w-xl markdown"
								>
									{@html marked.parse(
										sanitizeResponseContent(
											models[selectedModelIdx]?.info?.meta?.description ?? ''
										).replaceAll('\n', '<br>')
									)}
								</div>
							</Tooltip>

							{#if models[selectedModelIdx]?.info?.meta?.user}
								<div class="mt-0.5 text-sm font-normal text-gray-400 dark:text-gray-500">
									By
									{#if models[selectedModelIdx]?.info?.meta?.user.community}
										<a
											href="https://openwebui.com/m/{models[selectedModelIdx]?.info?.meta?.user
												.username}"
											>{models[selectedModelIdx]?.info?.meta?.user.name
												? models[selectedModelIdx]?.info?.meta?.user.name
												: `@${models[selectedModelIdx]?.info?.meta?.user.username}`}</a
										>
									{:else}
										{models[selectedModelIdx]?.info?.meta?.user.name}
									{/if}
								</div>
							{/if}
						{/if}
					</div>
				</div>
			{/if}

			{#if relevantGroups.length > 0 || relevantSubscriptions.length > 0}
				<div class="@md:max-w-3xl w-full pb-1">
					<div class="bg-gray-50 dark:bg-gray-850 rounded-lg p-3 text-xs">
						{#each relevantGroups as [groupName, groupData]}
							{@const effectiveUsage = groupData.effectiveUsage}
							{@const isOverLimit = groupData.limit && effectiveUsage.total > groupData.limit}
							<div class="flex items-center justify-between mb-1 last:mb-0">
								<span
									class="font-medium {isOverLimit
										? 'text-error-brick dark:text-error-brick-dark'
										: 'text-gray-700 dark:text-gray-300'}">{groupName}</span
								>
								<div
									class="flex flex-wrap items-center space-x-2 {isOverLimit
										? 'text-error-brick dark:text-error-brick-dark'
										: 'text-gray-600 dark:text-gray-400'}"
								>
									<span>{effectiveUsage.in.toLocaleString()} IN</span>
									<span>·</span>
									<span>{effectiveUsage.out.toLocaleString()} OUT</span>
									<span>·</span>
									<span>{effectiveUsage.total.toLocaleString()} TOTAL</span>
									{#if groupData.limit}
										<span>/ {groupData.limit.toLocaleString()}</span>
									{/if}
								</div>
							</div>
						{/each}
						{#each relevantSubscriptions as sub}
							{#each sub.windows ?? [] as w (w.id)}
								{@const ratio = (w.used_percent ?? 0) / 100}
								<div class="flex items-center justify-between gap-3 mb-1 last:mb-0">
									<span
										class="font-medium shrink-0 {ratio >= 1
											? 'text-error-brick dark:text-error-brick-dark'
											: 'text-gray-700 dark:text-gray-300'}"
										>{formatSubscriptionLimitLabel(sub.name, w)} · {formatWindowLabel(w)}</span
									>
									<div
										class="flex items-center gap-2 min-w-0 {ratio >= 1
											? 'text-error-brick dark:text-error-brick-dark'
											: 'text-gray-600 dark:text-gray-400'}"
									>
										{#if w.resets_at}
											<span class="truncate text-gray-400 dark:text-gray-500"
												>{formatResetsIn(w.resets_at, Date.now())}</span
											>
										{/if}
										<div
											class="w-24 sm:w-32 h-[3px] rounded-full bg-gray-200 dark:bg-gray-800 overflow-hidden shrink-0"
										>
											<div
												class="h-full rounded-full {ratio >= 1
													? 'bg-error-brick dark:bg-error-brick-dark'
													: ratio >= 0.8
														? 'bg-warning dark:bg-warning-dark'
														: 'bg-gray-400 dark:bg-gray-600'}"
												style="width: {Math.min(100, Math.round(ratio * 100))}%"
											></div>
										</div>
										<span class="tabular-nums shrink-0">{formatUsedPercent(w)}</span>
									</div>
								</div>
							{/each}
						{/each}
					</div>
				</div>
			{/if}

			<div class="text-base font-normal @md:max-w-3xl w-full py-3 {atSelectedModel ? 'mt-2' : ''}">
				<MessageInput
					bind:this={messageInput}
					{history}
					bind:selectedModels
					{onSelectionTouched}
					{onServiceTierTouched}
					bind:files
					bind:prompt
					bind:autoScroll
					bind:selectedToolIds
					bind:selectedFilterIds
					bind:imageGenerationEnabled
					bind:webSearchEnabled
					bind:studyModeEnabled
					bind:dataVizEnabled
					bind:automationsEnabled
					bind:subagentsEnabled
					bind:subagentReasoningEffort
					bind:subagentServiceTier
					bind:subagentExternalToolsEnabled
					bind:subagentModel
					bind:serviceTier
					bind:atSelectedModel
					bind:showCommands
					{toolServers}
					{stopResponse}
					{createMessagePair}
					placeholder={$i18n.t('How can I help you today?')}
					{onChange}
					onupload={(e) => {
						dispatch('upload', e.detail);
					}}
					onsubmit={(e) => {
						dispatch('submit', e.detail);
					}}
				/>
			</div>
		</div>
	</div>

	{#if $selectedFolder}
		<div
			class="mx-auto px-4 md:max-w-3xl md:px-6 font-primary min-h-62"
			in:fade={{ duration: 200, delay: 200 }}
		>
			<FolderPlaceholder folder={$selectedFolder} />
		</div>
	{:else}
		<div class="mx-auto max-w-2xl font-primary mt-2" in:fade={{ duration: 200, delay: 200 }}>
			<div class="mx-5">
				<Suggestions
					suggestionPrompts={atSelectedModel?.info?.meta?.suggestion_prompts ??
						models[selectedModelIdx]?.info?.meta?.suggestion_prompts ??
						$config?.default_prompt_suggestions ??
						[]}
					inputValue={prompt}
					{onSelect}
				/>
			</div>
		</div>
	{/if}
</div>
