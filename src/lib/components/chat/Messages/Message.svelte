<script lang="ts">
	import { toast } from '$lib/utils/toast';

	import { tick, getContext, onMount } from 'svelte';
	const i18n = getContext('i18n');

	import { settings } from '$lib/stores';
	import { copyToClipboard } from '$lib/utils';

	import MultiResponseMessages from './MultiResponseMessages.svelte';
	import ResponseMessage from './ResponseMessage.svelte';
	import UserMessage from './UserMessage.svelte';

	interface Props {
		chatId: any;
		selectedModels?: any;
		idx?: number;
		history: any;
		messageId: any;
		user: any;
		setInputText?: Function;
		gotoMessage: any;
		activateMessageBranch: any;
		showPreviousMessage: any;
		showNextMessage: any;
		updateChat: any;
		editMessage: any;
		saveMessage: any;
		deleteMessage: any;
		rateMessage: any;
		actionMessage: any;
		submitMessage: any;
		regenerateResponse: any;
		retryWithoutProviderRestrictions?: Function;
		markSkipRemainingRetries?: Function;
		regenerateWithModel?: Function;
		continueResponse: any;
		rewindAndInsert?: any;
		mergeResponses: any;
		triggerScroll: any;
		readOnly?: boolean;
		editCodeBlock?: boolean;
		topPadding?: boolean;
		widescreen?: boolean | null;
	}

	let {
		chatId,
		selectedModels = [],
		idx = 0,
		history = $bindable(),
		messageId,
		user,
		setInputText = () => {},
		gotoMessage,
		activateMessageBranch,
		showPreviousMessage,
		showNextMessage,
		updateChat,
		editMessage,
		saveMessage,
		deleteMessage,
		rateMessage,
		actionMessage,
		submitMessage,
		regenerateResponse,
		retryWithoutProviderRestrictions = () => {},
		markSkipRemainingRetries = () => {},
		regenerateWithModel = () => {},
		continueResponse,
		rewindAndInsert = () => {},
		mergeResponses,
		triggerScroll,
		readOnly = false,
		editCodeBlock = true,
		topPadding = false,
		widescreen = null
	}: Props = $props();
</script>

<!-- Phones get a wider text column (px-4) and more air between turns (mb-4):
     at 390px the desktop px-5/mb-3 rhythm reads cramped.
     data-cv-wrap: hook for the height sweeper (messageHeights.ts), which
     replaces the 150px contain-intrinsic-size GUESS with each turn's measured
     height so content-visibility realization while scrolling up causes zero
     layout shift, and for the scroll-anchoring engine's anchor picking. -->
<div
	data-cv-wrap
	class="flex flex-col justify-between px-4 md:px-5 mb-4 md:mb-3 w-full {(widescreen ??
	$settings?.widescreenMode ??
	null)
		? 'max-w-full'
		: 'max-w-5xl'} mx-auto rounded-lg group"
	style="content-visibility: auto; contain-intrinsic-size: auto 150px;"
>
	{#if history.messages[messageId]}
		{#if history.messages[messageId].role === 'user'}
			<UserMessage
				{user}
				{chatId}
				{history}
				{messageId}
				isFirstMessage={idx === 0}
				siblings={history.messages[messageId].parentId !== null
					? (history.messages[history.messages[messageId].parentId]?.childrenIds ?? [])
					: (Object.values(history.messages)
							.filter((message) => message.parentId === null)
							.map((message) => message.id) ?? [])}
				{gotoMessage}
				{showPreviousMessage}
				{showNextMessage}
				{editMessage}
				{deleteMessage}
				{readOnly}
				{editCodeBlock}
				{topPadding}
			/>
		{:else if (history.messages[history.messages[messageId].parentId]?.models?.length ?? 1) === 1}
			<ResponseMessage
				{chatId}
				{history}
				{messageId}
				{selectedModels}
				isLastMessage={messageId === history.currentId}
				siblings={history.messages[history.messages[messageId].parentId]?.childrenIds ?? []}
				{setInputText}
				{gotoMessage}
				{showPreviousMessage}
				{showNextMessage}
				{updateChat}
				{editMessage}
				{saveMessage}
				{rateMessage}
				{actionMessage}
				{submitMessage}
				{deleteMessage}
				{continueResponse}
				{regenerateResponse}
				{rewindAndInsert}
				{retryWithoutProviderRestrictions}
				{markSkipRemainingRetries}
				{regenerateWithModel}
				{readOnly}
				{editCodeBlock}
				{topPadding}
			/>
		{:else}
			<MultiResponseMessages
				bind:history
				{chatId}
				{messageId}
				{activateMessageBranch}
				{selectedModels}
				isLastMessage={messageId === history?.currentId}
				{setInputText}
				{updateChat}
				{editMessage}
				{saveMessage}
				{rateMessage}
				{actionMessage}
				{submitMessage}
				{deleteMessage}
				{continueResponse}
				{regenerateResponse}
				{rewindAndInsert}
				{retryWithoutProviderRestrictions}
				{markSkipRemainingRetries}
				{regenerateWithModel}
				{mergeResponses}
				{triggerScroll}
				{readOnly}
				{editCodeBlock}
				{topPadding}
			/>
		{/if}
	{/if}
</div>
