<script lang="ts">
	import dayjs from 'dayjs';
	import { onMount, tick, getContext } from 'svelte';

	import { mobile, models, settings } from '$lib/stores';

	import { generateMoACompletion } from '$lib/apis';
	import { createOpenAITextStream } from '$lib/apis/streaming';

	import ResponseMessage from './ResponseMessage.svelte';
	import Tooltip from '$lib/components/common/Tooltip.svelte';
	import Merge from '$lib/components/icons/Merge.svelte';

	import Markdown from './Markdown.svelte';
	import Name from './Name.svelte';
	import Skeleton from './Skeleton.svelte';
	import localizedFormat from 'dayjs/plugin/localizedFormat';
	import ProfileImage from './ProfileImage.svelte';
	import { WEBUI_BASE_URL } from '$lib/constants';
	import { shouldScrollIntoViewOnRender } from '$lib/utils/editScroll';
	import { getOrderedChildIds } from '$lib/utils/chatHistoryGraph';
	const i18n = getContext('i18n');
	dayjs.extend(localizedFormat);

	interface Props {
		chatId: any;
		history: any;
		messageId: any;
		activateMessageBranch: Function;
		selectedModels?: any;
		isLastMessage: any;
		readOnly?: boolean;
		editCodeBlock?: boolean;
		setInputText?: Function;
		updateChat: Function;
		editMessage: Function;
		saveMessage: Function;
		rateMessage: Function;
		actionMessage: Function;
		submitMessage: Function;
		deleteMessage: Function;
		continueResponse: Function;
		regenerateResponse: Function;
		rewindAndInsert?: Function;
		retryWithoutProviderRestrictions?: Function;
		markSkipRemainingRetries?: Function;
		regenerateWithModel?: Function;
		mergeResponses: Function;
		triggerScroll: Function;
		topPadding?: boolean;
	}

	let {
		chatId,
		history = $bindable(),
		messageId,
		activateMessageBranch,
		selectedModels = [],
		isLastMessage,
		readOnly = false,
		editCodeBlock = true,
		setInputText = () => {},
		updateChat,
		editMessage,
		saveMessage,
		rateMessage,
		actionMessage,
		submitMessage,
		deleteMessage,
		continueResponse,
		regenerateResponse,
		rewindAndInsert = () => {},
		retryWithoutProviderRestrictions = () => {},
		markSkipRemainingRetries = () => {},
		regenerateWithModel = () => {},
		mergeResponses,
		triggerScroll,
		topPadding = false
	}: Props = $props();
	let currentMessageId;
	let parentMessage = $state();
	let groupedMessageIds = $state({});
	let groupedMessageIdsIdx = $state({});

	// The merged response synthesises the per-model answers and has no .files of
	// its own, so sandbox: links in it must resolve against the union of the
	// currently-displayed contributing responses' generated files.
	let mergedSandboxFiles = $derived(
		Object.keys(groupedMessageIds ?? {})
			.flatMap((modelIdx) => {
				const ids = groupedMessageIds[modelIdx]?.messageIds ?? [];
				const mid = ids[groupedMessageIdsIdx[modelIdx] ?? ids.length - 1];
				return history?.messages?.[mid]?.files ?? [];
			})
			.filter((file) => file?.container_workspace)
	);

	let selectedModelIdx = $state(null);

	let message = $derived(history.messages[messageId]);

	const gotoMessage = async (modelIdx, messageIdx) => {
		// Clamp messageIdx to ensure it's within valid range
		groupedMessageIdsIdx[modelIdx] = Math.max(
			0,
			Math.min(messageIdx, groupedMessageIds[modelIdx].messageIds.length - 1)
		);

		const targetId = groupedMessageIds[modelIdx].messageIds[groupedMessageIdsIdx[modelIdx]];
		await activateMessageBranch(targetId);
	};

	const showPreviousMessage = async (modelIdx) => {
		groupedMessageIdsIdx[modelIdx] = Math.max(0, groupedMessageIdsIdx[modelIdx] - 1);

		const targetId = groupedMessageIds[modelIdx].messageIds[groupedMessageIdsIdx[modelIdx]];
		await activateMessageBranch(targetId);
	};

	const showNextMessage = async (modelIdx) => {
		groupedMessageIdsIdx[modelIdx] = Math.min(
			groupedMessageIds[modelIdx].messageIds.length - 1,
			groupedMessageIdsIdx[modelIdx] + 1
		);

		const targetId = groupedMessageIds[modelIdx].messageIds[groupedMessageIdsIdx[modelIdx]];
		await activateMessageBranch(targetId);
	};

	const initHandler = async () => {
		console.log('multiresponse:initHandler');
		await tick();

		currentMessageId = messageId;
		parentMessage = history.messages[messageId].parentId
			? history.messages[history.messages[messageId].parentId]
			: null;
		const parentChildIds = parentMessage?.id
			? getOrderedChildIds(history.messages ?? {}, parentMessage.id)
			: [];

		groupedMessageIds = parentMessage?.models.reduce((a, model, modelIdx) => {
			// Find all messages that are children of the parent message and have the same model
			let modelMessageIds = parentChildIds
				.map((id) => history.messages[id])
				.filter((m) => m?.modelIdx === modelIdx)
				.map((m) => m.id);

			// Legacy support for messages that don't have a modelIdx
			// Find all messages that are children of the parent message and have the same model
			if (modelMessageIds.length === 0) {
				let modelMessages = parentChildIds
					.map((id) => history.messages[id])
					.filter((m) => m?.model === model);

				modelMessages.forEach((m) => {
					m.modelIdx = modelIdx;
				});

				modelMessageIds = modelMessages.map((m) => m.id);
			}

			return {
				...a,
				[modelIdx]: { messageIds: modelMessageIds }
			};
		}, {});

		groupedMessageIdsIdx = parentMessage?.models.reduce((a, model, modelIdx) => {
			const idx = groupedMessageIds[modelIdx].messageIds.findIndex((id) => id === messageId);
			if (idx !== -1) {
				return {
					...a,
					[modelIdx]: idx
				};
			} else {
				return {
					...a,
					[modelIdx]: groupedMessageIds[modelIdx].messageIds.length - 1
				};
			}
		}, {});

		selectedModelIdx = history.messages[messageId]?.modelIdx;

		console.log(groupedMessageIds, groupedMessageIdsIdx);

		await tick();
	};

	const onGroupClick = async (_messageId, modelIdx) => {
		if (messageId != _messageId) {
			selectedModelIdx = modelIdx;
			await activateMessageBranch(_messageId);
		}
	};

	const mergeResponsesHandler = async () => {
		const responses = Object.keys(groupedMessageIds).map((modelIdx) => {
			const { messageIds } = groupedMessageIds[modelIdx];
			const messageId = messageIds[groupedMessageIdsIdx[modelIdx]];

			return history.messages[messageId].content;
		});
		mergeResponses(messageId, responses, chatId);
	};

	onMount(async () => {
		await initHandler();
		await tick();

		// Only pull the new/selected turn into view when the reader is already at
		// the bottom (a fresh submit, or branch-switching while tailing). If they
		// scrolled up to read/edit, this mount-time scroll would yank them away —
		// honor the 'preserve' intent the edit/branch paths already compute.
		if (($settings?.scrollOnBranchChange ?? true) && shouldScrollIntoViewOnRender()) {
			const messageElement = document.getElementById(`message-${messageId}`);
			if (messageElement) {
				messageElement.scrollIntoView({ block: 'start' });
			}
		}
	});
</script>

{#if parentMessage}
	<div>
		<div
			class="flex snap-x snap-mandatory overflow-x-auto scrollbar-hidden"
			id="responses-container-{chatId}-{parentMessage.id}"
		>
			{#if $settings?.displayMultiModelResponsesInTabs ?? false}
				<div class="w-full">
					<div class=" flex w-full mb-4.5 border-b-hairline border-gray-200 dark:border-gray-850">
						<div
							class="flex gap-2 scrollbar-none overflow-x-auto w-fit text-center font-medium bg-transparent pt-1 text-sm"
						>
							{#each Object.keys(groupedMessageIds) as modelIdx}
								{#if groupedMessageIdsIdx[modelIdx] !== undefined && groupedMessageIds[modelIdx].messageIds.length > 0}
									<!-- svelte-ignore a11y_no_static_element_interactions -->
									<!-- svelte-ignore a11y_click_events_have_key_events -->

									{@const _messageId =
										groupedMessageIds[modelIdx].messageIds[groupedMessageIdsIdx[modelIdx]]}

									{@const model = $models.find((m) => m.id === history.messages[_messageId]?.model)}

									<button
										class="min-w-fit {selectedModelIdx == modelIdx
											? ' dark:border-gray-300 '
											: ' opacity-35 border-transparent'} pb-1.5 px-2.5 transition border-b-2"
										onclick={async () => {
											if (selectedModelIdx != modelIdx) {
												selectedModelIdx = modelIdx;
											}

											onGroupClick(_messageId, modelIdx);
										}}
									>
										<div class="flex items-center gap-1.5">
											<!-- <ProfileImage
												src={model?.info?.meta?.profile_image_url ??
													($i18n.language === 'dg-DG'
														? `${WEBUI_BASE_URL}/doge.png`
														: `${WEBUI_BASE_URL}/favicon.png`)}
												className={'size-5 assistant-message-profile-image'}
											/> -->

											<div class="-translate-y-[1px]">
												{model ? `${model.name}` : history.messages[_messageId]?.model}
											</div>
										</div>
									</button>
								{/if}
							{/each}
						</div>
					</div>

					{#if selectedModelIdx !== null}
						{@const _messageId =
							groupedMessageIds[selectedModelIdx].messageIds[
								groupedMessageIdsIdx[selectedModelIdx]
							]}
						{#key history.currentId}
							{#if message}
								<ResponseMessage
									{chatId}
									{history}
									messageId={_messageId}
									{selectedModels}
									isLastMessage={true}
									siblings={groupedMessageIds[selectedModelIdx].messageIds}
									gotoMessage={(message, messageIdx) => gotoMessage(selectedModelIdx, messageIdx)}
									showPreviousMessage={() => showPreviousMessage(selectedModelIdx)}
									showNextMessage={() => showNextMessage(selectedModelIdx)}
									{setInputText}
									{updateChat}
									{editMessage}
									{saveMessage}
									{rateMessage}
									{deleteMessage}
									{actionMessage}
									{submitMessage}
									{continueResponse}
									{rewindAndInsert}
									regenerateResponse={async (message, prompt = null) => {
										regenerateResponse(message, prompt);
										await tick();
										groupedMessageIdsIdx[selectedModelIdx] =
											groupedMessageIds[selectedModelIdx].messageIds.length - 1;
									}}
									{retryWithoutProviderRestrictions}
									{markSkipRemainingRetries}
									regenerateWithModel={async (message, modelId, preserveToolContext = false) => {
										regenerateWithModel(message, modelId, preserveToolContext);
										await tick();
										groupedMessageIdsIdx[selectedModelIdx] =
											groupedMessageIds[selectedModelIdx].messageIds.length - 1;
									}}
									{readOnly}
									{topPadding}
								/>
							{/if}
						{/key}
					{/if}
				</div>
			{:else}
				{#each Object.keys(groupedMessageIds) as modelIdx}
					{#if groupedMessageIdsIdx[modelIdx] !== undefined && groupedMessageIds[modelIdx].messageIds.length > 0}
						<!-- svelte-ignore a11y_no_static_element_interactions -->
						<!-- svelte-ignore a11y_click_events_have_key_events -->
						{@const _messageId =
							groupedMessageIds[modelIdx].messageIds[groupedMessageIdsIdx[modelIdx]]}

						<div
							class=" snap-center w-full max-w-full m-1 border-hairline {history.messages[messageId]
								?.modelIdx == modelIdx
								? `bg-gray-50 dark:bg-gray-850 border-book-cloth/40 ${
										$mobile ? 'min-w-full' : 'min-w-80'
									}`
								: `border-gray-100 dark:border-gray-850 border-dashed ${
										$mobile ? 'min-w-full' : 'min-w-80'
									}`} transition-all p-5 rounded-2xl"
							onclick={async () => {
								onGroupClick(_messageId, modelIdx);
							}}
						>
							{#key history.currentId}
								{#if message}
									<ResponseMessage
										{chatId}
										{history}
										messageId={_messageId}
										{selectedModels}
										isLastMessage={true}
										siblings={groupedMessageIds[modelIdx].messageIds}
										gotoMessage={(message, messageIdx) => gotoMessage(modelIdx, messageIdx)}
										showPreviousMessage={() => showPreviousMessage(modelIdx)}
										showNextMessage={() => showNextMessage(modelIdx)}
										{setInputText}
										{updateChat}
										{editMessage}
										{saveMessage}
										{rateMessage}
										{deleteMessage}
										{actionMessage}
										{submitMessage}
										{continueResponse}
										{rewindAndInsert}
										regenerateResponse={async (message, prompt = null) => {
											regenerateResponse(message, prompt);
											await tick();
											groupedMessageIdsIdx[modelIdx] =
												groupedMessageIds[modelIdx].messageIds.length - 1;
										}}
										{retryWithoutProviderRestrictions}
										{markSkipRemainingRetries}
										regenerateWithModel={async (message, modelId, preserveToolContext = false) => {
											regenerateWithModel(message, modelId, preserveToolContext);
											await tick();
											groupedMessageIdsIdx[modelIdx] =
												groupedMessageIds[modelIdx].messageIds.length - 1;
										}}
										{readOnly}
										{editCodeBlock}
										{topPadding}
									/>
								{/if}
							{/key}
						</div>
					{/if}
				{/each}
			{/if}
		</div>

		{#if !readOnly}
			{#if !Object.keys(groupedMessageIds).find((modelIdx) => {
				const { messageIds } = groupedMessageIds[modelIdx];
				const _messageId = messageIds[groupedMessageIdsIdx[modelIdx]];
				return !history.messages[_messageId]?.done ?? false;
			})}
				<div class="flex justify-end">
					<div class="w-full">
						{#if history.messages[messageId]?.merged?.status}
							{@const message = history.messages[messageId]?.merged}

							<div class="w-full rounded-xl pl-5 pr-2 py-2 mt-2">
								<Name>
									{$i18n.t('Merged Response')}

									{#if message.timestamp}
										<span
											class=" self-center invisible group-hover:visible text-gray-400 text-xs font-medium uppercase ml-0.5 -mt-0.5"
										>
											{dayjs(message.timestamp * 1000).format('LT')}
										</span>
									{/if}
								</Name>

								<div class="mt-1 markdown-prose w-full min-w-full">
									{#if (message?.content ?? '') === ''}
										<Skeleton />
									{:else}
										<Markdown
											id={`merged`}
											content={message.content ?? ''}
											sandboxFiles={mergedSandboxFiles}
										/>
									{/if}
								</div>
							</div>
						{/if}
					</div>

					{#if isLastMessage}
						<div class=" shrink-0 text-gray-600 dark:text-gray-500 mt-1">
							<Tooltip content={$i18n.t('Merge Responses')} placement="bottom">
								<button
									type="button"
									id="merge-response-button"
									class="{true
										? 'visible'
										: 'invisible group-hover:visible'} p-1 max-md:p-2.5 hover:bg-black/5 dark:hover:bg-white/5 rounded-lg dark:hover:text-white hover:text-black transition"
									onclick={() => {
										mergeResponsesHandler();
									}}
								>
									<Merge className=" size-5 " />
								</button>
							</Tooltip>
						</div>
					{/if}
				</div>
			{/if}
		{/if}
	</div>
{/if}
