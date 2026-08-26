<script lang="ts" module>
	import { dispatchComponentEvent } from '$lib/utils/componentEvents';
	// Shared id->model index. The `model` reactive below re-runs on every
	// streaming frame (the revision store reassigns `message`), and a linear
	// $models.find per frame per visible response message adds up on large
	// model lists. The Map rebuilds only when the models array identity changes.
	let _modelsIndexRef: any[] | null = null;
	let _modelsIndex: Map<string, any> | null = null;
	const lookupModelById = (modelList: any[], id: string) => {
		if (_modelsIndexRef !== modelList) {
			_modelsIndexRef = modelList;
			_modelsIndex = new Map((modelList ?? []).map((m) => [m.id, m]));
		}
		return _modelsIndex?.get(id);
	};
</script>

<script lang="ts">
	import { toast } from '$lib/utils/toast';
	import dayjs from 'dayjs';

	import { onMount, onDestroy, tick, getContext } from 'svelte';
	import type { Writable } from 'svelte/store';
	import type { i18n as i18nType, t } from 'i18next';

	const i18n = getContext<Writable<i18nType>>('i18n');

	const dispatch = (type: string, detail?: unknown) =>
		dispatchComponentEvent(eventProps, type, detail);

	import { createNewFeedback, getFeedbackById, updateFeedbackById } from '$lib/apis/evaluations';
	import { getChatById } from '$lib/apis/chats';
	import { generateTags } from '$lib/apis';

	import {
		config,
		models,
		settings,
		temporaryChatEnabled,
		TTSWorker,
		user,
		getMessageRevisionStore,
		messageEditingIds
	} from '$lib/stores';
	import { synthesizeOpenAISpeech } from '$lib/apis/audio';
	import { imageGenerations } from '$lib/apis/images';
	import {
		copyToClipboard as _copyToClipboard,
		approximateToHumanReadable,
		getMessageContentParts,
		sanitizeResponseContent,
		createMessagesList,
		formatDate,
		removeDetails,
		removeAllDetails
	} from '$lib/utils';
	import {
		autoGrowEditTextarea,
		captureEditEntryAnchor,
		placeEditBoxForKeyboard
	} from '$lib/utils/editScroll';
	import { WEBUI_BASE_URL } from '$lib/constants';
	import { resolveRetryModelId } from '$lib/utils/chatTurn';
	import { getOrderedChildIds } from '$lib/utils/chatHistoryGraph';

	import Name from './Name.svelte';
	import ProfileImage from './ProfileImage.svelte';
	import Skeleton from './Skeleton.svelte';
	import Tooltip from '$lib/components/common/Tooltip.svelte';
	import RateComment from './RateComment.svelte';
	import Spinner from '$lib/components/common/Spinner.svelte';
	import WebSearchResults from './ResponseMessage/WebSearchResults.svelte';
	import Sparkles from '$lib/components/icons/Sparkles.svelte';

	import DeleteConfirmDialog from '$lib/components/common/ConfirmDialog.svelte';

	import Error from './Error.svelte';
	import ContentRenderer from './ContentRenderer.svelte';
	import { KokoroWorker } from '$lib/workers/KokoroWorker';
	import FileItem from '$lib/components/common/FileItem.svelte';
	import OutputFileItem from '$lib/components/common/OutputFileItem.svelte';
	import FollowUps from './ResponseMessage/FollowUps.svelte';
	import { fade } from 'svelte/transition';
	import { flyAndScale } from '$lib/utils/transitions';
	import RegenerateMenu from './ResponseMessage/RegenerateMenu.svelte';
	import ModelSwitcher from './ResponseMessage/ModelSwitcher.svelte';
	import StatusHistory from './ResponseMessage/StatusHistory.svelte';
	import FullHeightIframe from '$lib/components/common/FullHeightIframe.svelte';

	interface MessageType {
		id: string;
		model: string;
		modelIdx?: number;
		content: string;
		content_blocks?: any[];
		files?: { type: string; url: string }[];
		timestamp: number;
		role: string;
		statusHistory?: {
			done: boolean;
			action: string;
			description: string;
			urls?: string[];
			query?: string;
			vision_prompt?: string;
			vision_response?: string;
			error?: string;
		}[];
		status?: {
			done: boolean;
			action: string;
			description: string;
			urls?: string[];
			query?: string;
		};
		done: boolean;
		error?: boolean | { content: string };
		sources?: string[];
		info?: {
			openai?: boolean;
			prompt_tokens?: number;
			completion_tokens?: number;
			total_tokens?: number;
			eval_count?: number;
			eval_duration?: number;
			prompt_eval_count?: number;
			prompt_eval_duration?: number;
			total_duration?: number;
			load_duration?: number;
			usage?: unknown;
		};
		annotation?: { type: string; rating: number };
		followUps?: string[];
		userStopped?: boolean;
	}

	let skipRetryClicked = $state(false);

	// True when a "done" message actually has something useful to act on. We use
	// this to hide the post-completion action buttons (edit/copy/speak/continue/
	// regenerate) when the message has only an error and no content — that state
	// previously rendered as a fully-completed response, which was misleading.
	const messageHasContent = (m: any) => {
		if (!m) return false;
		if (Array.isArray(m.content_blocks) && m.content_blocks.length > 0) return true;
		if (Array.isArray(m.tool_calls) && m.tool_calls.length > 0) return true;
		const c = m.content;
		if (typeof c === 'string') return c.trim().length > 0;
		if (Array.isArray(c)) return c.length > 0;
		return c != null;
	};

	interface Props {
		chatId?: string;
		history: any;
		messageId: any;
		selectedModels?: any;
		siblings: any;
		setInputText?: Function;
		gotoMessage?: Function;
		showPreviousMessage: Function;
		showNextMessage: Function;
		updateChat: Function;
		editMessage: Function;
		saveMessage: Function;
		rateMessage: Function;
		actionMessage: Function;
		deleteMessage: Function;
		submitMessage: Function;
		continueResponse: Function;
		regenerateResponse: Function;
		rewindAndInsert?: Function;
		retryWithoutProviderRestrictions?: Function;
		markSkipRemainingRetries?: Function;
		regenerateWithModel?: Function;
		isLastMessage?: boolean;
		readOnly?: boolean;
		editCodeBlock?: boolean;
		topPadding?: boolean;
	}

	let {
		chatId = '',
		history = $bindable(),
		messageId,
		selectedModels = [],
		siblings,
		setInputText = () => {},
		gotoMessage = () => {},
		showPreviousMessage,
		showNextMessage,
		updateChat,
		editMessage,
		saveMessage,
		rateMessage,
		actionMessage,
		deleteMessage,
		submitMessage,
		continueResponse,
		regenerateResponse,
		rewindAndInsert = () => {},
		retryWithoutProviderRestrictions = () => {},
		markSkipRemainingRetries = () => {},
		regenerateWithModel = () => {},
		isLastMessage = true,
		readOnly = false,
		editCodeBlock = true,
		topPadding = false,
		...eventProps
	}: Props & Record<string, unknown> = $props();

	let messageRevisionStore = $derived(getMessageRevisionStore(messageId));
	let message: MessageType = $derived.by(() => {
		$messageRevisionStore;
		return history.messages[messageId];
	});

	let buttonsContainerElement: HTMLDivElement = $state();
	let showDeleteConfirm = $state(false);

	let model = $state(null);

	let edit = $state(false);
	let editedContent = $state('');
	let editTextAreaElement: HTMLTextAreaElement = $state();

	// Mirror the local edit flag into the shared store so chat-level chrome
	// (mobile composer/token panels, sidebar edge strip) can stand down while
	// this message is being edited. Tracks the exact id it registered so a
	// messageId prop swap mid-edit can't strand a stale entry (which would
	// keep the composer hidden); onDestroy covers branch/chat teardown.
	let registeredEditId: string | null = null;
	const syncEditRegistration = (editing: boolean, id: string) => {
		const target = editing ? id : null;
		if (target === registeredEditId) return;
		messageEditingIds.update((ids) => {
			const next = new Set(ids);
			if (registeredEditId !== null) next.delete(registeredEditId);
			if (target !== null) next.add(target);
			return next;
		});
		registeredEditId = target;
	};

	let messageIndexEdit = $state(false);

	let audioParts: Record<number, HTMLAudioElement | null> = {};

	// Blob object URLs pin their decoded audio until revoked; release the parts
	// of a previous playback whenever a new one starts and on unmount.
	const releaseAudioParts = () => {
		for (const audio of Object.values(audioParts)) {
			if (audio?.src?.startsWith('blob:')) {
				URL.revokeObjectURL(audio.src);
			}
		}
		audioParts = {};
	};

	onDestroy(() => {
		releaseAudioParts();
		syncEditRegistration(false, messageId);
	});
	let speaking = $state(false);
	let speakingIdx: number | undefined;

	let loadingSpeech = $state(false);
	let generatingImage = $state(false);

	let showRateComment = $state(false);

	// --- Streaming bottom-anchor reserve --------------------------------------
	// Follow-up prompts are produced by a background task that completes a beat
	// AFTER the reply finishes streaming, then render in a row beneath the reply.
	// Without reservation the live reply rests ~150px lower than its settled
	// height, so completion visibly "jumps up" the moment the follow-up row pushes
	// it. We reserve that space from the start of the active turn: the streaming
	// reply already sits at its final height, and the follow-ups fill the reserved
	// box instead of shoving the reply. It is min-height (not a fixed height) so an
	// unusually tall row can still grow — but the reply never drops. See
	// everStreamed below for exactly when it is active. Tune FOLLOW_UP_RESERVE_PX
	// to match the typical follow-up row height /
	// preferred streaming reading height.
	const FOLLOW_UP_RESERVE_PX = 150;
	// Sticky "this reply streamed live in THIS session" latch. A fresh reply is
	// born message.done === false, so this flips true the moment it starts
	// streaming and STAYS true through done / stop / error / follow-up arrival. A
	// message rehydrated on chat load is born done === true and never flips it, so
	// it stays false. Gating the reserve on this is what keeps the three states
	// correct at once:
	//   • streaming reply gets the reserve, so it already sits at its settled
	//     height (no jump when the follow-up row later fills the reserve);
	//   • a stopped/errored LIVE reply keeps the reserve instead of collapsing it
	//     (collapsing would drop the reply down onto the input);
	//   • reloaded/old chats and scrolled-back history get NO phantom 150px gap —
	//     including mid-conversation multi-model groups (which hardcode
	//     isLastMessage), since their rehydrated columns never streamed here.
	let everStreamed = $state(false);

	const copyToClipboard = async (text) => {
		text = removeAllDetails(text);

		if (($config?.ui?.response_watermark ?? '').trim() !== '') {
			text = `${text}\n\n${$config?.ui?.response_watermark}`;
		}

		const res = await _copyToClipboard(text, null, $settings?.copyFormatted ?? false);
		if (res) {
			toast.success($i18n.t('Copying to clipboard was successful!'));
		}
	};

	const hasRenderableToolCalls = (m: any, content = '') => {
		if (Array.isArray(m?.content_blocks)) {
			return m.content_blocks.some((block: any) => block?.type === 'tool_calls');
		}
		return typeof content === 'string' && content.includes('<details type="tool_calls"');
	};

	const getMessageTextContent = (content) => {
		if (typeof content === 'string') {
			return content;
		}

		if (Array.isArray(content)) {
			return content
				.map((part) => {
					if (typeof part === 'string') {
						return part;
					}

					if (part?.type === 'text' && typeof part.text === 'string') {
						return part.text;
					}

					return '';
				})
				.join('\n');
		}

		return '';
	};

	const getStructuredTextContent = (blocks: any[] = []) =>
		blocks
			.filter((block) => block?.type === 'text')
			.map((block) => (block?.content ?? '').toString())
			.join('\n')
			.trim();

	const getLatestStructuredTextContent = (blocks: any[] = []) => {
		for (let i = blocks.length - 1; i >= 0; i -= 1) {
			const block = blocks[i];
			if (block?.type === 'text') return (block?.content ?? '').toString();
		}
		return '';
	};

	const playAudio = (idx: number) => {
		return new Promise<void>((res) => {
			speakingIdx = idx;
			const audio = audioParts[idx];

			if (!audio) {
				return res();
			}

			audio.play();
			audio.onended = async () => {
				await new Promise((r) => setTimeout(r, 300));

				if (Object.keys(audioParts).length - 1 === idx) {
					speaking = false;
				}

				res();
			};
		});
	};

	const toggleSpeakMessage = async () => {
		if (speaking) {
			try {
				speechSynthesis.cancel();

				if (speakingIdx !== undefined && audioParts[speakingIdx]) {
					audioParts[speakingIdx]!.pause();
					audioParts[speakingIdx]!.currentTime = 0;
				}
			} catch {}

			speaking = false;
			speakingIdx = undefined;
			return;
		}

		if (!messageTextContent.trim().length) {
			toast.info($i18n.t('No content to speak'));
			return;
		}

		speaking = true;

		const content = removeAllDetails(messageTextContent);

		if ($config.audio.tts.engine === '') {
			let voices = [];
			// iOS PWAs frequently never populate getVoices() (no voiceschanged in a
			// standalone context) — without a cap this 100ms interval spun forever.
			// After ~3s give up waiting and speak with the default voice.
			let voicesAttempts = 0;
			const getVoicesLoop = setInterval(() => {
				voices = speechSynthesis.getVoices();
				voicesAttempts += 1;
				if (voices.length > 0 || voicesAttempts >= 30) {
					clearInterval(getVoicesLoop);

					const voice =
						voices
							?.filter(
								(v) => v.voiceURI === ($settings?.audio?.tts?.voice ?? $config?.audio?.tts?.voice)
							)
							?.at(0) ?? undefined;

					console.log(voice);

					const speak = new SpeechSynthesisUtterance(content);
					speak.rate = $settings.audio?.tts?.playbackRate ?? 1;

					console.log(speak);

					speak.onend = () => {
						speaking = false;
						if ($settings.conversationMode) {
							document.getElementById('voice-input-button')?.click();
						}
					};

					if (voice) {
						speak.voice = voice;
					}

					speechSynthesis.speak(speak);
				}
			}, 100);
		} else {
			loadingSpeech = true;

			const messageContentParts: string[] = getMessageContentParts(
				content,
				$config?.audio?.tts?.split_on ?? 'punctuation'
			);

			if (!messageContentParts.length) {
				console.log('No content to speak');
				toast.info($i18n.t('No content to speak'));

				speaking = false;
				loadingSpeech = false;
				return;
			}

			console.debug('Prepared message content for TTS', messageContentParts);

			releaseAudioParts();
			audioParts = messageContentParts.reduce(
				(acc, _sentence, idx) => {
					acc[idx] = null;
					return acc;
				},
				{} as typeof audioParts
			);

			let lastPlayedAudioPromise = Promise.resolve(); // Initialize a promise that resolves immediately

			if ($settings.audio?.tts?.engine === 'browser-kokoro') {
				if (!$TTSWorker) {
					await TTSWorker.set(
						new KokoroWorker({
							dtype: $settings.audio?.tts?.engineConfig?.dtype ?? 'fp32'
						})
					);

					await $TTSWorker.init();
				}

				for (const [idx, sentence] of messageContentParts.entries()) {
					const blob = await $TTSWorker
						.generate({
							text: sentence,
							voice: $settings?.audio?.tts?.voice ?? $config?.audio?.tts?.voice
						})
						.catch((error) => {
							console.error(error);
							toast.error(`${error}`);

							speaking = false;
							loadingSpeech = false;
						});

					if (blob) {
						const audio = new Audio(blob);
						audio.playbackRate = $settings.audio?.tts?.playbackRate ?? 1;

						audioParts[idx] = audio;
						loadingSpeech = false;
						lastPlayedAudioPromise = lastPlayedAudioPromise.then(() => playAudio(idx));
					}
				}
			} else {
				for (const [idx, sentence] of messageContentParts.entries()) {
					const res = await synthesizeOpenAISpeech(
						localStorage.token,
						$settings?.audio?.tts?.defaultVoice === $config.audio.tts.voice
							? ($settings?.audio?.tts?.voice ?? $config?.audio?.tts?.voice)
							: $config?.audio?.tts?.voice,
						sentence
					).catch((error) => {
						console.error(error);
						toast.error(`${error}`);

						speaking = false;
						loadingSpeech = false;
					});

					if (res) {
						const blob = await res.blob();
						const blobUrl = URL.createObjectURL(blob);
						const audio = new Audio(blobUrl);
						audio.playbackRate = $settings.audio?.tts?.playbackRate ?? 1;

						audioParts[idx] = audio;
						loadingSpeech = false;
						lastPlayedAudioPromise = lastPlayedAudioPromise.then(() => playAudio(idx));
					}
				}
			}
		}
	};

	let preprocessedDetailsCache = [];

	function preprocessForEditing(content: string): string {
		// Replace <details>...</details> with unique ID placeholder
		const detailsBlocks = [];
		let i = 0;

		content = content.replace(/<details[\s\S]*?<\/details>/gi, (match) => {
			detailsBlocks.push(match);
			return `<details id="__DETAIL_${i++}__"/>`;
		});

		// Store original blocks in the editedContent or globally (see merging later)
		preprocessedDetailsCache = detailsBlocks;

		return content;
	}

	function postprocessAfterEditing(content: string): string {
		const restoredContent = content.replace(
			/<details id="__DETAIL_(\d+)__"\/>/g,
			(_, index) => preprocessedDetailsCache[parseInt(index)] || ''
		);

		return restoredContent;
	}

	const editMessageHandler = async () => {
		edit = true;

		editedContent = preprocessForEditing(messageTextContent);

		// Hold the message at its current on-screen position across the
		// markdown -> textarea swap so edit-entry doesn't shove the viewport.
		const restoreAnchor = captureEditEntryAnchor(message.id);

		await tick();

		editTextAreaElement.style.height = '';
		editTextAreaElement.style.height = `${editTextAreaElement.scrollHeight}px`;

		// preventScroll: do NOT let the browser scroll-into-view the freshly
		// focused (often tall) textarea. Matches UserMessage — on mobile the
		// composer hides for the edit, so the edit box must become the active
		// input immediately rather than leaving the user keyboard-less.
		editTextAreaElement?.focus({ preventScroll: true });

		await tick();
		restoreAnchor();

		// The focus above summons the iOS keyboard AFTER this handler returns.
		// When it arrives (or if it is already up from the composer), the edit
		// box gets top-aligned in the keyboard-shrunk viewport for maximum
		// editing room; on desktop this expires and the anchor result stands.
		placeEditBoxForKeyboard(message.id);
	};

	const editMessageConfirmHandler = async () => {
		const messageContent = postprocessAfterEditing(editedContent ? editedContent : '');
		const accepted = await editMessage(message.id, { content: messageContent }, false);
		if (accepted === false) return;

		edit = false;
		editedContent = '';

		await tick();
	};

	const cancelEditMessage = async () => {
		edit = false;
		editedContent = '';
		await tick();
	};

	const generateImage = async (message: MessageType) => {
		generatingImage = true;
		const res = await imageGenerations(localStorage.token, messageTextContent).catch((error) => {
			toast.error(`${error}`);
		});
		console.log(res);

		if (res) {
			const files = res.map((image) => ({
				type: 'image',
				url: `${image.url}`
			}));

			saveMessage(message.id, {
				...message,
				files: files
			});
		}

		generatingImage = false;
	};

	let feedbackLoading = $state(false);

	const feedbackHandler = async (rating: number | null = null, details: object | null = null) => {
		feedbackLoading = true;
		console.log('Feedback', rating, details);

		const updatedMessage = {
			...message,
			annotation: {
				...(message?.annotation ?? {}),
				...(rating !== null ? { rating: rating } : {}),
				...(details ? details : {})
			}
		};

		const chat = await getChatById(localStorage.token, chatId).catch((error) => {
			toast.error(`${error}`);
		});
		if (!chat) {
			return;
		}

		const messages = createMessagesList(history, message.id);
		const siblingIds = message.parentId
			? getOrderedChildIds(history.messages ?? {}, message.parentId)
			: [];

		let feedbackItem = {
			type: 'rating',
			data: {
				...(updatedMessage?.annotation ? updatedMessage.annotation : {}),
				model_id: message?.selectedModelId ?? message.model,
				...(siblingIds.length > 1
					? {
							sibling_model_ids: siblingIds
								.filter((id) => id !== message.id)
								.map((id) => history.messages[id]?.selectedModelId ?? history.messages[id].model)
						}
					: {})
			},
			meta: {
				arena: message ? message.arena : false,
				model_id: message.model,
				message_id: message.id,
				message_index: messages.length,
				chat_id: chatId
			},
			snapshot: {
				chat: chat
			}
		};

		const baseModels = [
			feedbackItem.data.model_id,
			...(feedbackItem.data.sibling_model_ids ?? [])
		].reduce((acc, modelId) => {
			const model = $models.find((m) => m.id === modelId);
			if (model) {
				acc[model.id] = model?.info?.base_model_id ?? null;
			} else {
				// Log or handle cases where corresponding model is not found
				console.warn(`Model with ID ${modelId} not found`);
			}
			return acc;
		}, {});
		feedbackItem.meta.base_models = baseModels;

		let feedback = null;
		if (message?.feedbackId) {
			feedback = await updateFeedbackById(
				localStorage.token,
				message.feedbackId,
				feedbackItem
			).catch((error) => {
				toast.error(`${error}`);
			});
		} else {
			feedback = await createNewFeedback(localStorage.token, feedbackItem).catch((error) => {
				toast.error(`${error}`);
			});

			if (feedback) {
				updatedMessage.feedbackId = feedback.id;
			}
		}

		console.log(updatedMessage);
		saveMessage(message.id, updatedMessage);

		await tick();

		if (!details) {
			showRateComment = true;

			if (!updatedMessage.annotation?.tags && (message?.content ?? '') !== '') {
				// attempt to generate tags
				const tags = await generateTags(localStorage.token, message.model, messages, chatId).catch(
					(error) => {
						console.error(error);
						return [];
					}
				);
				console.log(tags);

				if (tags) {
					updatedMessage.annotation.tags = tags;
					feedbackItem.data.tags = tags;

					saveMessage(message.id, updatedMessage);
					await updateFeedbackById(
						localStorage.token,
						updatedMessage.feedbackId,
						feedbackItem
					).catch((error) => {
						toast.error(`${error}`);
					});
				}
			}
		}

		feedbackLoading = false;
	};

	const deleteMessageHandler = async () => {
		deleteMessage(message.id);
	};

	onMount(async () => {
		// console.log('ResponseMessage mounted');

		await tick();
		if (buttonsContainerElement) {
			buttonsContainerElement.addEventListener('wheel', function (event) {
				if (buttonsContainerElement.scrollWidth <= buttonsContainerElement.clientWidth) {
					// If the container is not scrollable, horizontal scroll
					return;
				} else {
					event.preventDefault();

					if (event.deltaY !== 0) {
						// Adjust horizontal scroll position based on vertical scroll
						buttonsContainerElement.scrollLeft += event.deltaY;
					}
				}
			});
		}
	});
	$effect(() => {
		if (!message?.retrying) skipRetryClicked = false;
	});
	$effect(() => {
		model = lookupModelById($models, message.model);
	});
	$effect(() => {
		syncEditRegistration(edit, messageId);
	});
	$effect(() => {
		if (message?.done === false) everStreamed = true;
	});
	let followUpReserveActive = $derived(
		isLastMessage &&
			!readOnly &&
			everStreamed &&
			($settings?.autoFollowUps ?? true) &&
			// Only reserve when a follow-up row will ACTUALLY arrive. autoFollowUps is
			// just the per-user request toggle; the server still has to have follow-up
			// generation enabled (ENABLE_FOLLOW_UP_GENERATION / override — surfaced as
			// this feature flag). Without this, instances with follow-ups disabled
			// (the out-of-the-box default) would hold the reserve open and never fill
			// it, leaving a permanent empty gap above the input.
			($config?.features?.enable_follow_up_generation ?? false) &&
			// Hold the reserve while streaming, and after completion while follow-ups
			// are still PENDING (message.followUps stays undefined until the socket
			// event lands) or a non-empty row has ARRIVED to fill it. Collapse only
			// when the result is known-empty (followUps === [] — the generation
			// produced none) so such a reply settles to its natural height instead of
			// leaving a permanent empty gap. The backend always emits the event (even
			// []), so this "pending → resolved" transition is reliable.
			(!message?.done || message?.followUps === undefined || (message?.followUps?.length ?? 0) > 0)
	);
	let hasStructuredContent = $derived(
		Array.isArray(message?.content_blocks) && message.content_blocks.length > 0
	);
	// For structured messages, ContentRenderer renders content_blocks directly.
	// Keep this as plain assistant text for copy/TTS/edit/image prompts; do not
	// stringify tool results here or heavy research turns become O(huge) per tick.
	let messageTextContent = $derived(
		hasStructuredContent
			? (message?.done ?? false) || message?.error
				? getStructuredTextContent(message.content_blocks)
				: getLatestStructuredTextContent(message.content_blocks)
			: getMessageTextContent(message?.content)
	);
	$effect(() => {
		if (!edit) {
			(async () => {
				await tick();
			})();
		}
	});
</script>

<DeleteConfirmDialog
	bind:show={showDeleteConfirm}
	title={$i18n.t('Delete message?')}
	onconfirm={() => {
		deleteMessageHandler();
	}}
/>

{#key message.id}
	<div
		class=" flex w-full message-{message.id} group"
		id="message-{message.id}"
		dir={$settings.chatDirection}
	>
		<div class={`shrink-0 ltr:mr-3 rtl:ml-3 hidden @lg:flex mt-1 `}>
			<ProfileImage
				src={model?.info?.meta?.profile_image_url ??
					($i18n.language === 'dg-DG'
						? `${WEBUI_BASE_URL}/doge.png`
						: `${WEBUI_BASE_URL}/favicon.png`)}
				className={'size-8 assistant-message-profile-image'}
			/>
		</div>

		<div class="flex-auto w-0 pl-1 relative">
			<Name>
				<Tooltip content={model?.name ?? message.model} placement="top-start">
					<span class="line-clamp-1 text-gray-900 dark:text-gray-100">
						{model?.name ?? message.model}
					</span>
				</Tooltip>

				{#if message.timestamp}
					<div
						class="self-center text-xs font-medium first-letter:capitalize ml-0.5 translate-y-[1px] {($settings?.highContrastMode ??
						false)
							? 'dark:text-gray-100 text-gray-900'
							: 'invisible group-hover:visible transition text-gray-400'}"
					>
						<Tooltip content={dayjs(message.timestamp * 1000).format('LLLL')}>
							<span class="line-clamp-1"
								>{$i18n.t(formatDate(message.timestamp * 1000), {
									LOCALIZED_TIME: dayjs(message.timestamp * 1000).format('LT'),
									LOCALIZED_DATE: dayjs(message.timestamp * 1000).format('L')
								})}</span
							>
						</Tooltip>
					</div>
				{/if}
			</Name>

			<div>
				<div class="chat-{message.role} w-full min-w-full markdown-prose">
					<div>
						{#if model?.info?.meta?.capabilities?.status_updates ?? true}
							<StatusHistory
								statusHistory={message?.statusHistory}
								expand={message?.content === ''}
							/>
						{/if}

						{#if message?.embeds && message.embeds.length > 0}
							<div class="my-1 w-full flex overflow-x-auto gap-2 flex-wrap">
								{#each message.embeds as embed, idx}
									<div class="my-2 w-full" id={`${message.id}-embeds-${idx}`}>
										<FullHeightIframe
											src={embed}
											allowScripts={true}
											allowForms={true}
											allowSameOrigin={true}
											allowPopups={true}
										/>
									</div>
								{/each}
							</div>
						{/if}

						{#if edit === true}
							<div
								class="message-edit-box w-full bg-gray-50 dark:bg-gray-800 rounded-2xl px-5 py-3 my-2"
								data-kb-keep
							>
								<textarea
									id="message-edit-{message.id}"
									bind:this={editTextAreaElement}
									class="message-edit-scroller block bg-transparent outline-hidden w-full resize-none"
									bind:value={editedContent}
									oninput={(e) => {
										autoGrowEditTextarea(e.currentTarget);
									}}
									onkeydown={(e) => {
										if (e.key === 'Escape') {
											document.getElementById('close-edit-message-button')?.click();
										}

										const isCmdOrCtrlPressed = e.metaKey || e.ctrlKey;
										const isEnterPressed = e.key === 'Enter';

										if (isCmdOrCtrlPressed && isEnterPressed) {
											document.getElementById('confirm-edit-message-button')?.click();
										}
									}}></textarea>

								<div class="mt-2 flex justify-between text-sm font-medium">
									<div class="self-center text-xs font-normal text-gray-500 dark:text-gray-400">
										{$i18n.t('Saved as a new version')}
									</div>

									<div class="flex space-x-1.5">
										<button
											id="close-edit-message-button"
											class="px-3.5 py-1.5 bg-gray-100 hover:bg-gray-200 dark:bg-gray-800 dark:hover:bg-gray-700 text-gray-800 dark:text-white transition rounded-lg"
											onclick={() => {
												cancelEditMessage();
											}}
										>
											{$i18n.t('Cancel')}
										</button>

										<button
											id="confirm-edit-message-button"
											class="px-3.5 py-1.5 bg-book-cloth hover:bg-kraft text-white transition-colors duration-200 ease-paper rounded-lg"
											onclick={() => {
												editMessageConfirmHandler();
											}}
										>
											{$i18n.t('Save')}
										</button>
									</div>
								</div>
							</div>
						{:else}
							<div class="w-full flex flex-col relative" id="response-content-container">
								{#if !hasStructuredContent && messageTextContent === '' && !message.error && message.done !== true && ((model?.info?.meta?.capabilities?.status_updates ?? true) ? (message?.statusHistory ?? [...(message?.status ? [message?.status] : [])]).length === 0 || (message?.statusHistory?.at(-1)?.hidden ?? false) : true)}
									<Skeleton />
								{:else if (messageTextContent || hasStructuredContent) && message.error !== true}
									<!-- always show message contents even if there's an error -->
									<!-- unless message.error === true which is legacy error handling, where the error message is stored in message.content -->
									<ContentRenderer
										id={`${chatId}-${message.id}`}
										messageId={message.id}
										{chatId}
										content={messageTextContent}
										content_blocks={Array.isArray(message?.content_blocks)
											? message.content_blocks
											: null}
										sources={message.sources}
										sandboxFiles={message.files ?? []}
										dataVizOverrides={message?.dataVizOverrides ?? {}}
										save={!readOnly}
										preview={!readOnly}
										{editCodeBlock}
										{topPadding}
										parseImmediately={message?.done ?? false}
										messageDone={message?.done ?? false}
										messageStopped={message?.userStopped === true}
										messageErrored={!!message?.error}
										done={($settings?.chatFadeStreamingText ?? true)
											? (message?.done ?? false)
											: true}
										{model}
										onTaskClick={async (e) => {
											console.log(e);
										}}
										onSave={({ raw, oldContent, newContent }) => {
											history.messages[message.id].content = history.messages[
												message.id
											].content.replace(raw, raw.replace(oldContent, newContent));

											updateChat();
										}}
										onRewind={!readOnly &&
										message?.done === true &&
										Array.isArray(message?.content_blocks)
											? (cutIndex, text) => rewindAndInsert(message, cutIndex, text ?? '')
											: null}
									/>
								{/if}

								{#if message?.retrying && !message?.done}
									<div
										class="flex flex-col gap-2 py-3 px-4 bg-warning/10 rounded-xl border-hairline border-warning/25"
									>
										<div
											class="flex items-center justify-between gap-2 text-sm text-warning dark:text-warning-dark"
										>
											<div class="flex items-center gap-2">
												<svg
													class="size-4 animate-spin"
													xmlns="http://www.w3.org/2000/svg"
													fill="none"
													viewBox="0 0 24 24"
												>
													<circle
														class="opacity-25"
														cx="12"
														cy="12"
														r="10"
														stroke="currentColor"
														stroke-width="4"
													></circle>
													<path
														class="opacity-75"
														fill="currentColor"
														d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"
													></path>
												</svg>
												<span>
													{#if message.retrying.reason === 'network'}
														Connection lost — will retry when it returns...
													{:else}
														Attempt {message.retrying.attempt} of {message.retrying.maxAttempts} failed.
														Retrying in {message.retrying.countdown}s...
													{/if}
												</span>
											</div>
											{#if skipRetryClicked}
												<span class="text-xs italic shrink-0"
													>Will not retry if next request fails</span
												>
											{:else}
												<button
													type="button"
													class="text-xs underline hover:text-warning dark:hover:text-warning-dark shrink-0"
													onclick={() => {
														markSkipRemainingRetries(message.id);
														skipRetryClicked = true;
													}}
												>
													Do not retry if next request fails
												</button>
											{/if}
										</div>
										<div class="w-full bg-warning/20 rounded-full h-1">
											<div
												class="bg-warning dark:bg-warning-dark h-1 rounded-full transition-all duration-1000"
												style="width: {((message.retrying.attempt * 2 -
													message.retrying.countdown) /
													(message.retrying.attempt * 2)) *
													100}%"
											></div>
										</div>
									</div>
								{/if}

								{#if message?.error}
									<!-- Canonical error shape is {content: str}; legacy error === true keeps
										the message text in message.content. Any other shape (old persisted
										rows, raw strings) is handed to Error verbatim — it renders string and
										object payloads — so a shape mismatch never shows an empty box. -->
									<Error
										content={message?.error === true
											? messageTextContent
											: message?.error?.content || message?.error}
										onRetryWithoutProvider={message?.providerFailed
											? () => retryWithoutProviderRestrictions(message)
											: null}
									/>
								{/if}
							</div>
						{/if}
					</div>
				</div>

				{#if !edit && message?.files && message.files.length > 0}
					<div class="mt-3 mb-1 flex flex-wrap gap-2">
						{#each message.files as file (file?.id ?? file?.url ?? file)}
							<OutputFileItem item={file} sandboxFiles={message.files} />
						{/each}
					</div>
				{/if}

				{#if !edit}
					<!-- max-md:-mx-2.5 cancels the buttons' own 10px touch padding so the
					     icon column lines up with the message text above it — and, because
					     the row is then as wide as the message, buys back the ~20px that
					     used to push the last action (usually Delete) onto a second line
					     on a 390px phone. flex-wrap stays as the graceful fallback for
					     setups with even more actions (ratings, image generation). -->
					<div
						bind:this={buttonsContainerElement}
						class="flex justify-start overflow-x-auto buttons text-gray-600 dark:text-gray-500 mt-0.5 max-md:-mx-2.5 max-md:flex-wrap max-md:gap-y-1"
					>
						{#if !message.done && !readOnly && isLastMessage}
							<!-- Model switcher shown during generation -->
							<ModelSwitcher
								{chatId}
								messageId={message.id}
								taskId={null}
								currentModelId={message.model}
								onSwitch={(modelId) => {
									// Update message model for UI feedback
									message.pendingSwitchModel = modelId;
								}}
							/>
						{/if}
						{#if (message.done && !(message?.error && !messageHasContent(message))) || siblings.length > 1}
							{#if siblings.length > 1}
								<div class="flex self-center min-w-fit" dir="ltr">
									<button
										aria-label={$i18n.t('Previous message')}
										class="self-center p-1.5 max-md:p-2.5 hover:bg-black/5 dark:hover:bg-white/5 dark:hover:text-white hover:text-black rounded-lg transition"
										onclick={() => {
											showPreviousMessage(message);
										}}
									>
										<svg
											aria-hidden="true"
											xmlns="http://www.w3.org/2000/svg"
											fill="none"
											viewBox="0 0 24 24"
											stroke="currentColor"
											stroke-width="2.5"
											class="size-4"
										>
											<path
												stroke-linecap="round"
												stroke-linejoin="round"
												d="M15.75 19.5 8.25 12l7.5-7.5"
											/>
										</svg>
									</button>

									{#if messageIndexEdit}
										<div
											class="text-sm flex justify-center font-semibold self-center dark:text-gray-100 min-w-fit max-md:px-1"
										>
											<input
												id="message-index-input-{message.id}"
												type="number"
												value={siblings.indexOf(message.id) + 1}
												min="1"
												max={siblings.length}
												onfocus={(e) => {
													e.target.select();
												}}
												onblur={(e) => {
													gotoMessage(message, e.target.value - 1);
													messageIndexEdit = false;
												}}
												onkeydown={(e) => {
													if (e.key === 'Enter') {
														gotoMessage(message, e.target.value - 1);
														messageIndexEdit = false;
													}
												}}
												class="bg-transparent font-semibold self-center dark:text-gray-100 min-w-fit outline-hidden"
											/>/{siblings.length}
										</div>
									{:else}
										<!-- svelte-ignore a11y_no_static_element_interactions -->
										<div
											class="text-sm tracking-widest font-semibold self-center dark:text-gray-100 min-w-fit max-md:px-1"
											ondblclick={async () => {
												messageIndexEdit = true;

												await tick();
												const input = document.getElementById(`message-index-input-${message.id}`);
												if (input) {
													input.focus({ preventScroll: true });
													input.select();
												}
											}}
										>
											{siblings.indexOf(message.id) + 1}/{siblings.length}
										</div>
									{/if}

									<button
										class="self-center p-1.5 max-md:p-2.5 hover:bg-black/5 dark:hover:bg-white/5 dark:hover:text-white hover:text-black rounded-lg transition"
										onclick={() => {
											showNextMessage(message);
										}}
										aria-label={$i18n.t('Next message')}
									>
										<svg
											xmlns="http://www.w3.org/2000/svg"
											fill="none"
											aria-hidden="true"
											viewBox="0 0 24 24"
											stroke="currentColor"
											stroke-width="2.5"
											class="size-4"
										>
											<path
												stroke-linecap="round"
												stroke-linejoin="round"
												d="m8.25 4.5 7.5 7.5-7.5 7.5"
											/>
										</svg>
									</button>
								</div>
							{/if}

							{#if message.done}
								{#if !readOnly}
									{#if $user?.role === 'user' ? ($user?.permissions?.chat?.edit ?? true) : true}
										<Tooltip content={$i18n.t('Edit')} placement="bottom">
											<button
												aria-label={$i18n.t('Edit')}
												class="{isLastMessage || ($settings?.highContrastMode ?? false)
													? 'visible'
													: 'invisible group-hover:visible'} p-1.5 max-md:p-2.5 hover:bg-black/5 dark:hover:bg-white/5 rounded-lg dark:hover:text-white hover:text-black transition"
												onclick={() => {
													editMessageHandler();
												}}
											>
												<svg
													xmlns="http://www.w3.org/2000/svg"
													fill="none"
													viewBox="0 0 24 24"
													stroke-width="2.3"
													aria-hidden="true"
													stroke="currentColor"
													class="w-4 h-4"
												>
													<path
														stroke-linecap="round"
														stroke-linejoin="round"
														d="M16.862 4.487l1.687-1.688a1.875 1.875 0 112.652 2.652L6.832 19.82a4.5 4.5 0 01-1.897 1.13l-2.685.8.8-2.685a4.5 4.5 0 011.13-1.897L16.863 4.487zm0 0L19.5 7.125"
													/>
												</svg>
											</button>
										</Tooltip>
									{/if}
								{/if}

								<Tooltip content={$i18n.t('Copy')} placement="bottom">
									<button
										aria-label={$i18n.t('Copy')}
										class="{isLastMessage || ($settings?.highContrastMode ?? false)
											? 'visible'
											: 'invisible group-hover:visible'} p-1.5 max-md:p-2.5 hover:bg-black/5 dark:hover:bg-white/5 rounded-lg dark:hover:text-white hover:text-black transition copy-response-button"
										onclick={() => {
											copyToClipboard(messageTextContent);
										}}
									>
										<svg
											xmlns="http://www.w3.org/2000/svg"
											fill="none"
											aria-hidden="true"
											viewBox="0 0 24 24"
											stroke-width="2.3"
											stroke="currentColor"
											class="w-4 h-4"
										>
											<path
												stroke-linecap="round"
												stroke-linejoin="round"
												d="M15.666 3.888A2.25 2.25 0 0013.5 2.25h-3c-1.03 0-1.9.693-2.166 1.638m7.332 0c.055.194.084.4.084.612v0a.75.75 0 01-.75.75H9a.75.75 0 01-.75-.75v0c0-.212.03-.418.084-.612m7.332 0c.646.049 1.288.11 1.927.184 1.1.128 1.907 1.077 1.907 2.185V19.5a2.25 2.25 0 01-2.25 2.25H6.75A2.25 2.25 0 014.5 19.5V6.257c0-1.108.806-2.057 1.907-2.185a48.208 48.208 0 011.927-.184"
											/>
										</svg>
									</button>
								</Tooltip>

								{#if $user?.role === 'admin' || ($user?.permissions?.chat?.tts ?? true)}
									<Tooltip content={$i18n.t('Read Aloud')} placement="bottom">
										<button
											aria-label={$i18n.t('Read Aloud')}
											id="speak-button-{message.id}"
											class="{isLastMessage || ($settings?.highContrastMode ?? false)
												? 'visible'
												: 'invisible group-hover:visible'} p-1.5 max-md:p-2.5 hover:bg-black/5 dark:hover:bg-white/5 rounded-lg dark:hover:text-white hover:text-black transition"
											onclick={() => {
												if (!loadingSpeech) {
													toggleSpeakMessage();
												}
											}}
										>
											{#if loadingSpeech}
												<svg
													class=" w-4 h-4"
													fill="currentColor"
													viewBox="0 0 24 24"
													aria-hidden="true"
													xmlns="http://www.w3.org/2000/svg"
												>
													<style>
														.spinner_S1WN {
															animation: spinner_MGfb 0.8s linear infinite;
															animation-delay: -0.8s;
														}

														.spinner_Km9P {
															animation-delay: -0.65s;
														}

														.spinner_JApP {
															animation-delay: -0.5s;
														}

														@keyframes spinner_MGfb {
															93.75%,
															100% {
																opacity: 0.2;
															}
														}
													</style>
													<circle class="spinner_S1WN" cx="4" cy="12" r="3" />
													<circle class="spinner_S1WN spinner_Km9P" cx="12" cy="12" r="3" />
													<circle class="spinner_S1WN spinner_JApP" cx="20" cy="12" r="3" />
												</svg>
											{:else if speaking}
												<svg
													xmlns="http://www.w3.org/2000/svg"
													fill="none"
													viewBox="0 0 24 24"
													aria-hidden="true"
													stroke-width="2.3"
													stroke="currentColor"
													class="w-4 h-4"
												>
													<path
														stroke-linecap="round"
														stroke-linejoin="round"
														d="M17.25 9.75 19.5 12m0 0 2.25 2.25M19.5 12l2.25-2.25M19.5 12l-2.25 2.25m-10.5-6 4.72-4.72a.75.75 0 0 1 1.28.53v15.88a.75.75 0 0 1-1.28.53l-4.72-4.72H4.51c-.88 0-1.704-.507-1.938-1.354A9.009 9.009 0 0 1 2.25 12c0-.83.112-1.633.322-2.396C2.806 8.756 3.63 8.25 4.51 8.25H6.75Z"
													/>
												</svg>
											{:else}
												<svg
													xmlns="http://www.w3.org/2000/svg"
													fill="none"
													viewBox="0 0 24 24"
													aria-hidden="true"
													stroke-width="2.3"
													stroke="currentColor"
													class="w-4 h-4"
												>
													<path
														stroke-linecap="round"
														stroke-linejoin="round"
														d="M19.114 5.636a9 9 0 010 12.728M16.463 8.288a5.25 5.25 0 010 7.424M6.75 8.25l4.72-4.72a.75.75 0 011.28.53v15.88a.75.75 0 01-1.28.53l-4.72-4.72H4.51c-.88 0-1.704-.507-1.938-1.354A9.01 9.01 0 012.25 12c0-.83.112-1.633.322-2.396C2.806 8.756 3.63 8.25 4.51 8.25H6.75z"
													/>
												</svg>
											{/if}
										</button>
									</Tooltip>
								{/if}

								{#if $config?.features.enable_image_generation && ($user?.role === 'admin' || $user?.permissions?.features?.image_generation !== false) && !readOnly}
									<Tooltip content={$i18n.t('Generate Image')} placement="bottom">
										<button
											aria-label={$i18n.t('Generate Image')}
											class="{isLastMessage || ($settings?.highContrastMode ?? false)
												? 'visible'
												: 'invisible group-hover:visible'}  p-1.5 max-md:p-2.5 hover:bg-black/5 dark:hover:bg-white/5 rounded-lg dark:hover:text-white hover:text-black transition"
											onclick={() => {
												if (!generatingImage) {
													generateImage(message);
												}
											}}
										>
											{#if generatingImage}
												<svg
													aria-hidden="true"
													class=" w-4 h-4"
													fill="currentColor"
													viewBox="0 0 24 24"
													xmlns="http://www.w3.org/2000/svg"
												>
													<style>
														.spinner_S1WN {
															animation: spinner_MGfb 0.8s linear infinite;
															animation-delay: -0.8s;
														}

														.spinner_Km9P {
															animation-delay: -0.65s;
														}

														.spinner_JApP {
															animation-delay: -0.5s;
														}

														@keyframes spinner_MGfb {
															93.75%,
															100% {
																opacity: 0.2;
															}
														}
													</style>
													<circle class="spinner_S1WN" cx="4" cy="12" r="3" />
													<circle class="spinner_S1WN spinner_Km9P" cx="12" cy="12" r="3" />
													<circle class="spinner_S1WN spinner_JApP" cx="20" cy="12" r="3" />
												</svg>
											{:else}
												<svg
													xmlns="http://www.w3.org/2000/svg"
													fill="none"
													aria-hidden="true"
													viewBox="0 0 24 24"
													stroke-width="2.3"
													stroke="currentColor"
													class="w-4 h-4"
												>
													<path
														stroke-linecap="round"
														stroke-linejoin="round"
														d="m2.25 15.75 5.159-5.159a2.25 2.25 0 0 1 3.182 0l5.159 5.159m-1.5-1.5 1.409-1.409a2.25 2.25 0 0 1 3.182 0l2.909 2.909m-18 3.75h16.5a1.5 1.5 0 0 0 1.5-1.5V6a1.5 1.5 0 0 0-1.5-1.5H3.75A1.5 1.5 0 0 0 2.25 6v12a1.5 1.5 0 0 0 1.5 1.5Zm10.5-11.25h.008v.008h-.008V8.25Zm.375 0a.375.375 0 1 1-.75 0 .375.375 0 0 1 .75 0Z"
													/>
												</svg>
											{/if}
										</button>
									</Tooltip>
								{/if}

								{#if message.usage}
									<Tooltip
										content={message.usage
											? `<pre>${sanitizeResponseContent(
													JSON.stringify(message.usage, null, 2)
														.replace(/"([^(")"]+)":/g, '$1:')
														.slice(1, -1)
														.split('\n')
														.map((line) => line.slice(2))
														.map((line) => (line.endsWith(',') ? line.slice(0, -1) : line))
														.join('\n')
												)}</pre>`
											: ''}
										placement="bottom"
										clickToStick={true}
										tippyOptions={{ delay: [0, 100], appendTo: () => document.body }}
									>
										<button
											aria-hidden="true"
											class=" {isLastMessage || ($settings?.highContrastMode ?? false)
												? 'visible'
												: 'invisible group-hover:visible'} p-1.5 max-md:p-2.5 hover:bg-black/5 dark:hover:bg-white/5 rounded-lg dark:hover:text-white hover:text-black transition whitespace-pre-wrap"
											id="info-{message.id}"
										>
											<svg
												aria-hidden="true"
												xmlns="http://www.w3.org/2000/svg"
												fill="none"
												viewBox="0 0 24 24"
												stroke-width="2.3"
												stroke="currentColor"
												class="w-4 h-4"
											>
												<path
													stroke-linecap="round"
													stroke-linejoin="round"
													d="M11.25 11.25l.041-.02a.75.75 0 011.063.852l-.708 2.836a.75.75 0 001.063.853l.041-.021M21 12a9 9 0 11-18 0 9 9 0 0118 0zm-9-3.75h.008v.008H12V8.25z"
												/>
											</svg>
										</button>
									</Tooltip>
								{/if}

								{#if !readOnly}
									{#if !$temporaryChatEnabled && ($config?.features.enable_message_rating ?? true) && ($user?.role === 'admin' || ($user?.permissions?.chat?.rate_response ?? true))}
										<Tooltip content={$i18n.t('Good Response')} placement="bottom">
											<button
												aria-label={$i18n.t('Good Response')}
												class="{isLastMessage || ($settings?.highContrastMode ?? false)
													? 'visible'
													: 'invisible group-hover:visible'} p-1.5 max-md:p-2.5 hover:bg-black/5 dark:hover:bg-white/5 rounded-lg {(
													message?.annotation?.rating ?? ''
												).toString() === '1'
													? 'bg-gray-100 dark:bg-gray-800'
													: ''} dark:hover:text-white hover:text-black transition disabled:cursor-progress disabled:hover:bg-transparent"
												disabled={feedbackLoading}
												onclick={async () => {
													await feedbackHandler(1);
													window.setTimeout(() => {
														document
															.getElementById(`message-feedback-${message.id}`)
															?.scrollIntoView({ block: 'nearest' });
													}, 0);
												}}
											>
												<svg
													aria-hidden="true"
													stroke="currentColor"
													fill="none"
													stroke-width="2.3"
													viewBox="0 0 24 24"
													stroke-linecap="round"
													stroke-linejoin="round"
													class="w-4 h-4"
													xmlns="http://www.w3.org/2000/svg"
												>
													<path
														d="M14 9V5a3 3 0 0 0-3-3l-4 9v11h11.28a2 2 0 0 0 2-1.7l1.38-9a2 2 0 0 0-2-2.3zM7 22H4a2 2 0 0 1-2-2v-7a2 2 0 0 1 2-2h3"
													/>
												</svg>
											</button>
										</Tooltip>

										<Tooltip content={$i18n.t('Bad Response')} placement="bottom">
											<button
												aria-label={$i18n.t('Bad Response')}
												class="{isLastMessage || ($settings?.highContrastMode ?? false)
													? 'visible'
													: 'invisible group-hover:visible'} p-1.5 max-md:p-2.5 hover:bg-black/5 dark:hover:bg-white/5 rounded-lg {(
													message?.annotation?.rating ?? ''
												).toString() === '-1'
													? 'bg-gray-100 dark:bg-gray-800'
													: ''} dark:hover:text-white hover:text-black transition disabled:cursor-progress disabled:hover:bg-transparent"
												disabled={feedbackLoading}
												onclick={async () => {
													await feedbackHandler(-1);
													window.setTimeout(() => {
														document
															.getElementById(`message-feedback-${message.id}`)
															?.scrollIntoView({ block: 'nearest' });
													}, 0);
												}}
											>
												<svg
													aria-hidden="true"
													stroke="currentColor"
													fill="none"
													stroke-width="2.3"
													viewBox="0 0 24 24"
													stroke-linecap="round"
													stroke-linejoin="round"
													class="w-4 h-4"
													xmlns="http://www.w3.org/2000/svg"
												>
													<path
														d="M10 15v4a3 3 0 0 0 3 3l4-9V2H5.72a2 2 0 0 0-2 1.7l-1.38 9a2 2 0 0 0 2 2.3zm7-13h2.67A2.31 2.31 0 0 1 22 4v7a2.31 2.31 0 0 1-2.33 2H17"
													/>
												</svg>
											</button>
										</Tooltip>
									{/if}

									{#if isLastMessage && ($user?.role === 'admin' || ($user?.permissions?.chat?.continue_response ?? true))}
										<Tooltip content={$i18n.t('Continue Response')} placement="bottom">
											<button
												aria-label={$i18n.t('Continue Response')}
												type="button"
												id="continue-response-button"
												class="{isLastMessage || ($settings?.highContrastMode ?? false)
													? 'visible'
													: 'invisible group-hover:visible'} p-1.5 max-md:p-2.5 hover:bg-black/5 dark:hover:bg-white/5 rounded-lg dark:hover:text-white hover:text-black transition"
												onclick={() => {
													continueResponse();
												}}
											>
												<svg
													aria-hidden="true"
													xmlns="http://www.w3.org/2000/svg"
													fill="none"
													viewBox="0 0 24 24"
													stroke-width="2.3"
													stroke="currentColor"
													class="w-4 h-4"
												>
													<path
														stroke-linecap="round"
														stroke-linejoin="round"
														d="M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z"
													/>
													<path
														stroke-linecap="round"
														stroke-linejoin="round"
														d="M15.91 11.672a.375.375 0 0 1 0 .656l-5.603 3.113a.375.375 0 0 1-.557-.328V8.887c0-.286.307-.466.557-.327l5.603 3.112Z"
													/>
												</svg>
											</button>
										</Tooltip>
									{/if}

									{#if $user?.role === 'admin' || ($user?.permissions?.chat?.regenerate_response ?? true)}
										{#if $settings?.regenerateMenu ?? true}
											<button
												type="button"
												class="hidden regenerate-response-button"
												onclick={() => {
													showRateComment = false;
													regenerateResponse(message);

													(model?.actions ?? []).forEach((action) => {
														dispatch('action', {
															id: action.id,
															event: {
																id: 'regenerate-response',
																data: {
																	messageId: message.id
																}
															}
														});
													});
												}}
											></button>

											<RegenerateMenu
												onRegenerate={(prompt = null) => {
													showRateComment = false;
													regenerateResponse(message, prompt);

													(model?.actions ?? []).forEach((action) => {
														dispatch('action', {
															id: action.id,
															event: {
																id: 'regenerate-response',
																data: {
																	messageId: message.id
																}
															}
														});
													});
												}}
												onRegenerateWithModel={(modelId, preserveToolContext = false) => {
													showRateComment = false;
													regenerateWithModel(message, modelId, preserveToolContext);
												}}
												currentModelId={message.model}
												retryModelId={resolveRetryModelId({
													selectedModelIds: selectedModels,
													modelIdx: message.modelIdx,
													fallbackModelId: message.model
												}) ?? message.model}
												hasToolCalls={hasRenderableToolCalls(message, messageTextContent)}
											>
												<Tooltip content={$i18n.t('Regenerate')} placement="bottom">
													<div
														aria-label={$i18n.t('Regenerate')}
														class="{isLastMessage
															? 'visible'
															: 'invisible group-hover:visible'} p-1.5 max-md:p-2.5 hover:bg-black/5 dark:hover:bg-white/5 rounded-lg dark:hover:text-white hover:text-black transition"
													>
														<svg
															xmlns="http://www.w3.org/2000/svg"
															fill="none"
															viewBox="0 0 24 24"
															stroke-width="2.3"
															aria-hidden="true"
															stroke="currentColor"
															class="w-4 h-4"
														>
															<path
																stroke-linecap="round"
																stroke-linejoin="round"
																d="M16.023 9.348h4.992v-.001M2.985 19.644v-4.992m0 0h4.992m-4.993 0l3.181 3.183a8.25 8.25 0 0013.803-3.7M4.031 9.865a8.25 8.25 0 0113.803-3.7l3.181 3.182m0-4.991v4.99"
															/>
														</svg>
													</div>
												</Tooltip>
											</RegenerateMenu>
										{:else}
											<Tooltip content={$i18n.t('Regenerate')} placement="bottom">
												<button
													type="button"
													aria-label={$i18n.t('Regenerate')}
													class="{isLastMessage
														? 'visible'
														: 'invisible group-hover:visible'} p-1.5 max-md:p-2.5 hover:bg-black/5 dark:hover:bg-white/5 rounded-lg dark:hover:text-white hover:text-black transition regenerate-response-button"
													onclick={() => {
														showRateComment = false;
														regenerateResponse(message);

														(model?.actions ?? []).forEach((action) => {
															dispatch('action', {
																id: action.id,
																event: {
																	id: 'regenerate-response',
																	data: {
																		messageId: message.id
																	}
																}
															});
														});
													}}
												>
													<svg
														xmlns="http://www.w3.org/2000/svg"
														fill="none"
														viewBox="0 0 24 24"
														stroke-width="2.3"
														aria-hidden="true"
														stroke="currentColor"
														class="w-4 h-4"
													>
														<path
															stroke-linecap="round"
															stroke-linejoin="round"
															d="M16.023 9.348h4.992v-.001M2.985 19.644v-4.992m0 0h4.992m-4.993 0l3.181 3.183a8.25 8.25 0 0013.803-3.7M4.031 9.865a8.25 8.25 0 0113.803-3.7l3.181 3.182m0-4.991v4.99"
														/>
													</svg>
												</button>
											</Tooltip>
										{/if}
									{/if}

									{#if $user?.role === 'admin' || ($user?.permissions?.chat?.delete_message ?? true)}
										{#if siblings.length > 1}
											<Tooltip content={$i18n.t('Delete')} placement="bottom">
												<button
													type="button"
													aria-label={$i18n.t('Delete')}
													id="delete-response-button"
													class="{isLastMessage || ($settings?.highContrastMode ?? false)
														? 'visible'
														: 'invisible group-hover:visible'} p-1.5 max-md:p-2.5 hover:bg-black/5 dark:hover:bg-white/5 rounded-lg dark:hover:text-white hover:text-black transition"
													onclick={() => {
														showDeleteConfirm = true;
													}}
												>
													<svg
														xmlns="http://www.w3.org/2000/svg"
														fill="none"
														viewBox="0 0 24 24"
														stroke-width="2"
														stroke="currentColor"
														aria-hidden="true"
														class="w-4 h-4"
													>
														<path
															stroke-linecap="round"
															stroke-linejoin="round"
															d="m14.74 9-.346 9m-4.788 0L9.26 9m9.968-3.21c.342.052.682.107 1.022.166m-1.022-.165L18.16 19.673a2.25 2.25 0 0 1-2.244 2.077H8.084a2.25 2.25 0 0 1-2.244-2.077L4.772 5.79m14.456 0a48.108 48.108 0 0 0-3.478-.397m-12 .562c.34-.059.68-.114 1.022-.165m0 0a48.11 48.11 0 0 1 3.478-.397m7.5 0v-.916c0-1.18-.91-2.164-2.09-2.201a51.964 51.964 0 0 0-3.32 0c-1.18.037-2.09 1.022-2.09 2.201v.916m7.5 0a48.667 48.667 0 0 0-7.5 0"
														/>
													</svg>
												</button>
											</Tooltip>
										{/if}
									{/if}

									{#if isLastMessage}
										{#each model?.actions ?? [] as action}
											<Tooltip content={action.name} placement="bottom">
												<button
													type="button"
													aria-label={action.name}
													class="{isLastMessage || ($settings?.highContrastMode ?? false)
														? 'visible'
														: 'invisible group-hover:visible'} p-1.5 max-md:p-2.5 hover:bg-black/5 dark:hover:bg-white/5 rounded-lg dark:hover:text-white hover:text-black transition"
													onclick={() => {
														actionMessage(action.id, message);
													}}
												>
													{#if action?.icon}
														<div class="size-4">
															<img
																src={action.icon}
																class="w-4 h-4 {action.icon.includes('svg')
																	? 'dark:invert-[80%]'
																	: ''}"
																style="fill: currentColor;"
																alt={action.name}
															/>
														</div>
													{:else}
														<Sparkles strokeWidth="2.1" className="size-4" />
													{/if}
												</button>
											</Tooltip>
										{/each}
									{/if}
								{/if}
							{/if}
						{/if}
					</div>

					<!-- Reserve box: holds the post-reply UI (rate comment + follow-up row).
					     min-height keeps the streaming reply at its settled height so the
					     follow-ups, which arrive after completion, fill this space instead
					     of jumping the reply upward. Inert (no min-height) when the reply
					     isn't the live one expecting follow-ups. -->
					<div style={followUpReserveActive ? `min-height: ${FOLLOW_UP_RESERVE_PX}px` : ''}>
						{#if message.done && showRateComment}
							<RateComment
								bind:message
								bind:show={showRateComment}
								onsave={async (e) => {
									await feedbackHandler(null, {
										...e.detail
									});
								}}
							/>
						{/if}

						{#if (isLastMessage || ($settings?.keepFollowUpPrompts ?? false)) && message.done && !readOnly && (message?.followUps ?? []).length > 0}
							<div class="mt-2.5" in:fade={{ duration: 100 }}>
								<FollowUps
									followUps={message?.followUps}
									onClick={(prompt) => {
										if ($settings?.insertFollowUpPrompt ?? false) {
											// Insert the follow-up prompt into the input box
											setInputText(prompt);
										} else {
											// Submit the follow-up prompt directly
											submitMessage(message?.id, prompt);
										}
									}}
								/>
							</div>
						{/if}
					</div>
				{/if}
			</div>
		</div>
	</div>
{/key}

<style>
	.buttons::-webkit-scrollbar {
		display: none; /* for Chrome, Safari and Opera */
	}

	.buttons {
		-ms-overflow-style: none; /* IE and Edge */
		scrollbar-width: none; /* Firefox */
	}
</style>
