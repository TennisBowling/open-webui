<script lang="ts">
	import { dispatchComponentEvent } from '$lib/utils/componentEvents';
	import { preventDefault } from '$lib/utils/eventModifiers';
	import DOMPurify from 'dompurify';
	import { marked } from 'marked';

	import { toast } from '$lib/utils/toast';

	import { v4 as uuidv4 } from 'uuid';
	import { createPicker, getAuthToken } from '$lib/utils/google-drive-picker';
	import { pickAndDownloadFile } from '$lib/utils/onedrive-file-picker';

	import { onMount, tick, getContext, onDestroy, untrack } from 'svelte';
	const dispatch = (type: string, detail?: unknown) =>
		dispatchComponentEvent(eventProps, type, detail);

	import {
		type Model,
		mobile,
		settings,
		models,
		config,
		showCallOverlay,
		chatId,
		socket,
		tools,
		toolServers,
		toolServersLoaded,
		user as _user,
		showControls,
		TTSWorker,
		temporaryChatEnabled
	} from '$lib/stores';

	import {
		createMessagesList,
		downscaleImageForUpload,
		extractContentFromFile,
		extractCurlyBraceWords,
		extractInputVariables,
		getAge,
		getCurrentDateTime,
		getFormattedDate,
		getFormattedTime,
		getUserPosition,
		getUserTimezone,
		getWeekday
	} from '$lib/utils';
	import { hasEnabledToolServers, loadToolServers } from '$lib/utils/toolServers';
	import { isOnScreenKeyboardDevice } from '$lib/utils/device';
	import { getPeakHoursConfig, getPeakStatus, peakClock } from '$lib/utils/peakHours';
	import PeakHoursNotice from './PeakHoursNotice.svelte';
	import { uploadFile, getFileById, primeFileObjectUrlById } from '$lib/apis/files';
	import {
		cancelVideoJob,
		createVideoJob,
		getActiveVideoJobs,
		getVideoJobsByIds,
		type VideoJob
	} from '$lib/apis/videos';
	import VideoAttachModal from './MessageInput/VideoAttachModal.svelte';
	import VideoAttachment from './MessageInput/VideoAttachment.svelte';
	import type { ReasoningEffort } from '$lib/apis';
	import { generateAutoCompletion } from '$lib/apis';
	import { deleteFileById } from '$lib/apis/files';
	import { getSessionUser } from '$lib/apis/auths';
	import { getTools } from '$lib/apis/tools';

	import { WEBUI_BASE_URL, WEBUI_API_BASE_URL, PASTED_TEXT_CHARACTER_LIMIT } from '$lib/constants';
	import { imageFallback } from '$lib/actions/imageFallback';

	import {
		BASE_REASONING_EFFORTS,
		getEffectiveReasoning,
		clampEffortToEffective
	} from '$lib/constants/reasoning';

	import InputMenu from './MessageInput/InputMenu.svelte';
	import VoiceRecording from './MessageInput/VoiceRecording.svelte';
	import FilesOverlay from './MessageInput/FilesOverlay.svelte';
	import QueuedMessages from './MessageInput/QueuedMessages.svelte';
	import ImageQualityBadge from './MessageInput/ImageQualityBadge.svelte';
	import ToolServersModal from './ToolServersModal.svelte';
	import RichTextInputFallback from './MessageInput/RichTextInputFallback.svelte';
	import ToolbarToggle from './MessageInput/ToolbarToggle.svelte';

	import Tooltip from '../common/Tooltip.svelte';
	import FileItem from '../common/FileItem.svelte';
	import Image from '../common/Image.svelte';

	import XMark from '../icons/XMark.svelte';
	import Headphone from '../icons/Headphone.svelte';
	import GlobeAlt from '../icons/GlobeAlt.svelte';
	import Photo from '../icons/Photo.svelte';
	import UserGroup from '../icons/UserGroup.svelte';
	import CommandLine from '../icons/CommandLine.svelte';

	import InputVariablesModal from './MessageInput/InputVariablesModal.svelte';
	import Voice from '../icons/Voice.svelte';
	import Terminal from '../icons/Terminal.svelte';
	import IntegrationsMenu from './MessageInput/IntegrationsMenu.svelte';
	import SubagentSettings from './MessageInput/SubagentSettings.svelte';
	import Component from '../icons/Component.svelte';
	import PlusAlt from '../icons/PlusAlt.svelte';

	import { KokoroWorker } from '$lib/workers/KokoroWorker';

	import { getSuggestionRenderer } from '../common/RichTextInput/suggestions';
	import CommandSuggestionList from './MessageInput/CommandSuggestionList.svelte';
	import Knobs from '../icons/Knobs.svelte';
	import ValvesModal from '../workspace/common/ValvesModal.svelte';

	// RichTextInput pulls in tiptap/prosemirror/lowlight/hljs (~140KB brotli) which
	// is a chat-input feature, not needed for first paint. Kick off the dynamic
	// import immediately (in parallel with initial render, not gated behind first
	// interaction) and fall back to a plain <textarea> until it resolves — see
	// `editorReady`/`fallbackFocused` below for the swap logic, including the
	// iOS-keyboard-safe focus-aware deferral. The template renders whichever of
	// RichTextInputFallback/RichTextInput is appropriate from a single `{#if}`
	// (not from inside `{#await}`) so the fallback instance is never
	// destroyed-and-recreated while the import is still pending.
	const richTextInputLoader = import('../common/RichTextInput.svelte');

	const i18n = getContext('i18n');

	let selectedModelIds: string[] = $state([]);

	// Queue of messages submitted while a response was streaming. The chip
	// strip above the input shows these and lets the user edit / unqueue
	// before they auto-send. Queue mutation handlers are owned by Chat.svelte

	// True for the brief gap on a server-drain chat between the prior turn finishing
	// and the next queued turn's generation attaching — keeps the input bar in its
	// working state so it doesn't flick to the idle "Send a Message" affordance and

	const canceledImageUploads = new Set<string>();
	const imageUploadAbortControllers = new Map<string, AbortController>();

	const enableContainerTool = () => {
		if (!containerWorkspaceConfigured || !containerToolId) {
			toast.error($i18n.t('Container workspace is not configured.'));
			return;
		}
		if (!(selectedToolIds ?? []).includes(containerToolId)) {
			selectedToolIds = [...(selectedToolIds ?? []), containerToolId];
		}
		onSelectionTouched();
		files = files.map((file) => ({ ...file, container_mode: true }));
		toast.success($i18n.t('Container enabled for this message'));
	};

	// Fired whenever the USER explicitly toggles a tool/feature here, so the chat
	// can mark its selection as user-curated (and stop a model switch from

	// Fired with the newly-picked tier when the user picks a service tier by
	// hand. Chat.svelte uses this to (a) permanently stand down the off-peak /
	// threshold auto-flip for this chat, so a deliberate choice is never
	// overwritten back to `flex`, and (b) persist the choice as that model's

	// Empty string = inherit the admin-set SUBAGENT_DEFAULT_REASONING_EFFORT.
	// Otherwise: minimal / low / medium / high / xhigh (free string; provider

	// Empty string = inherit the admin-set SUBAGENT_DEFAULT_SERVICE_TIER (which
	// itself may be empty, in which case no service_tier is sent and the
	// provider picks its own default). Otherwise: any string the provider

	// Empty string = inherit the resolved default subagent model (admin
	// SUBAGENT_DEFAULT_MODEL, else the parent chat's model). Otherwise: an
	// explicit model id the subagent should run as for this chat. Persisted to
	// `chat.params.subagentModel`, which the backend reads in

	// Reasoning effort functionality
	let reasoningEffort = $state('medium'); // default to medium
	let reasoningEffortByModel = $state({});
	let currentTrackedModel = $state('');
	let userModifiedEffortForCurrentModel = $state(false);
	let preferencesLoaded = $state(false);

	// Service tier functionality. Tier values are provider-specific (OpenAI uses
	// `default/flex/priority`, Gemini uses `standard/flex/priority`, etc.) so the
	// allowed list is computed per-model from `meta.service_tier.values`, falling
	// back to the OpenAI vocabulary when the model didn't customize it.
	//
	// Persisting/restoring the per-model tier is owned by Chat.svelte, NOT here.
	// This component is torn down and remounted when the chat transitions from
	// the empty-chat Placeholder composer to the docked composer right after the
	// first send, and a fresh mount must never be mistaken for the user
	// switching models — Chat.svelte stays alive across that transition and is
	// the only component that can tell the two apart. This component only
	// clamps `serviceTier` (bound from Chat.svelte) to whatever the current
	// model actually allows.
	const DEFAULT_SERVICE_TIERS = ['default', 'flex', 'priority'] as const;
	type ServiceTier = string;

	const getServiceTiersForModel = (modelId: string): readonly string[] => {
		const m = $models.find((mm) => mm.id === modelId);
		const vals = (m?.info?.meta as any)?.service_tier?.values;
		return Array.isArray(vals) && vals.length > 0 ? vals : DEFAULT_SERVICE_TIERS;
	};

	const getEffectiveForModel = (modelId: string) =>
		getEffectiveReasoning($models.find((m) => m.id === modelId));

	const getAllowedEffortsForModel = (modelId: string) =>
		getEffectiveForModel(modelId).allowedEfforts;

	const clampEffortToAllowed = (effort: string, allowedEfforts: string[]) => {
		if (!allowedEfforts || allowedEfforts.length === 0) {
			return null;
		}

		if (allowedEfforts.includes(effort)) {
			return effort;
		}

		// Prefer medium if available; otherwise first allowed
		return allowedEfforts.includes('medium') ? 'medium' : allowedEfforts[0];
	};

	let currentSelectedModel = $derived(
		selectedModelIds.length === 1 ? $models.find((m) => m.id === selectedModelIds[0]) : null
	);
	let supportsServiceTier = $derived(
		!!currentSelectedModel &&
			currentSelectedModel.owned_by !== 'ollama' &&
			(currentSelectedModel.info?.meta as any)?.service_tier?.enabled !== false
	);
	let showServiceTierSelector = $derived(supportsServiceTier);
	let allowedServiceTiers: readonly string[] = $derived(
		supportsServiceTier ? getServiceTiersForModel(selectedModelIds[0]) : DEFAULT_SERVICE_TIERS
	);
	let allowedReasoningEffortsForCurrentModel: string[] = $derived(
		selectedModelIds.length === 1 ? getAllowedEffortsForModel(selectedModelIds[0]) : []
	);
	let reasoningEnabledForCurrentModel = $derived(allowedReasoningEffortsForCurrentModel.length > 0);
	let showReasoningEffortSelector = $derived(reasoningEnabledForCurrentModel);

	// Load reasoning effort preferences from localStorage
	const loadReasoningEffortPreferences = () => {
		try {
			const stored = localStorage.getItem('reasoningEffortByModel');
			if (stored) {
				reasoningEffortByModel = JSON.parse(stored);
			}
		} catch (e) {
			console.error('Error loading reasoning effort preferences:', e);
		}
		preferencesLoaded = true;
	};

	// Save reasoning effort preferences to localStorage
	const saveReasoningEffortPreferences = () => {
		try {
			localStorage.setItem('reasoningEffortByModel', JSON.stringify(reasoningEffortByModel));
		} catch (e) {
			console.error('Error saving reasoning effort preferences:', e);
		}
	};

	// Handle user changes to reasoning effort
	const handleReasoningEffortChange = (event) => {
		const requestedEffort = event.target.value;

		const modelToUse =
			currentTrackedModel || (selectedModelIds.length > 0 ? selectedModelIds[0] : null);
		const clamped = modelToUse
			? (clampEffortToEffective(requestedEffort, getEffectiveForModel(modelToUse)) ?? 'medium')
			: (clampEffortToAllowed(requestedEffort, BASE_REASONING_EFFORTS) ?? 'medium');

		reasoningEffort = clamped;
		userModifiedEffortForCurrentModel = true;

		if (modelToUse) {
			reasoningEffortByModel[modelToUse] = clamped;
			saveReasoningEffortPreferences();
		}

		// Desktop nicety only — on touch, refocusing here pops the keyboard
		// right after an effort tap.
		if (!hasOnScreenKeyboard) {
			tick().then(() => document.getElementById('chat-input')?.focus({ preventScroll: true }));
		}
	};

	let showInputVariablesModal = $state(false);
	let inputVariablesModalCallback = $state((variableValues) => {});
	let inputVariables = $state({});
	let inputVariableValues = {};

	let showValvesModal = $state(false);
	let selectedValvesType = $state('tool'); // 'tool' or 'function'
	let selectedValvesItemId = $state(null);
	let integrationsMenuCloseOnOutsideClick = $state(true);

	const inputVariableHandler = async (text: string): Promise<string> => {
		inputVariables = extractInputVariables(text);

		// No variables? return the original text immediately.
		if (Object.keys(inputVariables).length === 0) {
			return text;
		}

		// Show modal and wait for the user's input.
		showInputVariablesModal = true;
		return await new Promise<string>((resolve) => {
			inputVariablesModalCallback = (variableValues) => {
				inputVariableValues = { ...inputVariableValues, ...variableValues };
				replaceVariables(inputVariableValues);
				showInputVariablesModal = false;
				resolve(text);
			};
		});
	};

	const textVariableHandler = async (text: string) => {
		if (text.includes('{{CLIPBOARD}}')) {
			const clipboardText = await navigator.clipboard.readText().catch((err) => {
				toast.error($i18n.t('Failed to read clipboard contents'));
				return '{{CLIPBOARD}}';
			});

			const clipboardItems = await navigator.clipboard.read();

			let imageUrl = null;
			for (const item of clipboardItems) {
				// Check for known image types
				for (const type of item.types) {
					if (type.startsWith('image/')) {
						const blob = await item.getType(type);
						imageUrl = URL.createObjectURL(blob);
					}
				}
			}

			if (imageUrl) {
				files = [
					...files,
					{
						type: 'image',
						url: imageUrl,
						fullQuality: false
					}
				];
			}

			text = text.replaceAll('{{CLIPBOARD}}', clipboardText);
		}

		if (text.includes('{{USER_LOCATION}}')) {
			let location;
			try {
				location = await getUserPosition();
			} catch (error) {
				toast.error($i18n.t('Location access not allowed'));
				location = 'LOCATION_UNKNOWN';
			}
			text = text.replaceAll('{{USER_LOCATION}}', String(location));
		}

		const sessionUser = await getSessionUser(localStorage.token);

		if (text.includes('{{USER_NAME}}')) {
			const name = sessionUser?.name || 'User';
			text = text.replaceAll('{{USER_NAME}}', name);
		}

		if (text.includes('{{USER_BIO}}')) {
			const bio = sessionUser?.bio || '';

			if (bio) {
				text = text.replaceAll('{{USER_BIO}}', bio);
			}
		}

		if (text.includes('{{USER_GENDER}}')) {
			const gender = sessionUser?.gender || '';

			if (gender) {
				text = text.replaceAll('{{USER_GENDER}}', gender);
			}
		}

		if (text.includes('{{USER_BIRTH_DATE}}')) {
			const birthDate = sessionUser?.date_of_birth || '';

			if (birthDate) {
				text = text.replaceAll('{{USER_BIRTH_DATE}}', birthDate);
			}
		}

		if (text.includes('{{USER_AGE}}')) {
			const birthDate = sessionUser?.date_of_birth || '';

			if (birthDate) {
				// calculate age using date
				const age = getAge(birthDate);
				text = text.replaceAll('{{USER_AGE}}', age);
			}
		}

		if (text.includes('{{USER_LANGUAGE}}')) {
			const language = localStorage.getItem('locale') || 'en-US';
			text = text.replaceAll('{{USER_LANGUAGE}}', language);
		}

		if (text.includes('{{CURRENT_DATE}}')) {
			const date = getFormattedDate();
			text = text.replaceAll('{{CURRENT_DATE}}', date);
		}

		if (text.includes('{{CURRENT_TIME}}')) {
			const time = getFormattedTime();
			text = text.replaceAll('{{CURRENT_TIME}}', time);
		}

		if (text.includes('{{CURRENT_DATETIME}}')) {
			const dateTime = getCurrentDateTime();
			text = text.replaceAll('{{CURRENT_DATETIME}}', dateTime);
		}

		if (text.includes('{{CURRENT_TIMEZONE}}')) {
			const timezone = getUserTimezone();
			text = text.replaceAll('{{CURRENT_TIMEZONE}}', timezone);
		}

		if (text.includes('{{CURRENT_WEEKDAY}}')) {
			const weekday = getWeekday();
			text = text.replaceAll('{{CURRENT_WEEKDAY}}', weekday);
		}

		return text;
	};

	const replaceVariables = (variables: Record<string, any>) => {
		console.log('Replacing variables:', variables);

		const chatInput = document.getElementById('chat-input');

		if (chatInput) {
			if (chatInputElement) {
				chatInputElement.replaceVariables(variables);
				chatInputElement.focus({ preventScroll: true });
			} else {
				// Real editor not mounted yet (lazy import still loading, or the
				// fallback textarea currently has focus) — buffer and replay once
				// it mounts (see replayEditorQueue).
				pendingReplaceVariables = variables;
				pendingFocus = true;
			}
		}
	};

	export const setText = async (text?: string, cb?: (text: string) => void) => {
		const chatInput = document.getElementById('chat-input');

		if (chatInput) {
			if (text !== '') {
				text = await textVariableHandler(text || '');
			}

			if (chatInputElement) {
				chatInputElement.setText(text);
				chatInputElement.focus({ preventScroll: true });
			} else {
				// Real editor not mounted yet — drive the fallback textarea
				// directly via `prompt` (it's two-way bound) and queue a focus +
				// replay for when the real editor mounts.
				prompt = text ?? '';
				pendingFocus = true;
			}

			if (text !== '') {
				text = await inputVariableHandler(text);
			}

			await tick();
			if (cb) await cb(text);
		}
	};

	const getCommand = () => {
		const chatInput = document.getElementById('chat-input');
		let word = '';

		if (chatInput) {
			word = chatInputElement?.getWordAtDocPos();
		}

		return word;
	};

	const replaceCommandWithText = (text) => {
		const chatInput = document.getElementById('chat-input');
		if (!chatInput) return;

		chatInputElement?.replaceCommandWithText(text);
	};

	const insertTextAtCursor = async (text: string) => {
		const chatInput = document.getElementById('chat-input');
		if (!chatInput) return;

		text = await textVariableHandler(text);

		if (command) {
			replaceCommandWithText(text);
		} else {
			chatInputElement?.insertContent(text);
		}

		await tick();
		text = await inputVariableHandler(text);
		await tick();

		const chatInputContainer = document.getElementById('chat-input-container');
		if (chatInputContainer) {
			chatInputContainer.scrollTop = chatInputContainer.scrollHeight;
		}

		await tick();
		if (chatInput) {
			chatInput.focus({ preventScroll: true });
			chatInput.dispatchEvent(new Event('input'));

			const words = extractCurlyBraceWords(prompt);

			if (words.length > 0) {
				const word = words.at(0);
				await tick();
			} else {
				chatInput.scrollTop = chatInput.scrollHeight;
			}
		}
	};

	let command = $state('');
	let suggestions = $state(null);

	let showTools = $state(false);
	let toolSelectionReady = $state(false);

	let activeServerToolIds: string[] = $state([]);

	let loaded = $state(false);
	let recording = $state(false);

	let isComposing = $state(false);
	// Safari has a bug where compositionend is not triggered correctly #16615
	// when using the virtual keyboard on iOS.
	let compositionEndedAt = $state(-2e8);
	const isSafari = /^((?!chrome|android).)*safari/i.test(navigator.userAgent);
	function inOrNearComposition(event: Event) {
		if (isComposing) {
			return true;
		}
		// See https://www.stum.de/2016/06/24/handling-ime-events-in-javascript/.
		// On Japanese input method editors (IMEs), the Enter key is used to confirm character
		// selection. On Safari, when Enter is pressed, compositionend and keydown events are
		// emitted. The keydown event triggers newline insertion, which we don't want.
		// This method returns true if the keydown event should be ignored.
		// We only ignore it once, as pressing Enter a second time *should* insert a newline.
		// Furthermore, the keydown event timestamp must be close to the compositionEndedAt timestamp.
		// This guards against the case where compositionend is triggered without the keyboard
		// (e.g. character confirmation may be done with the mouse), and keydown is triggered
		// afterwards- we wouldn't want to ignore the keydown event in this case.
		if (isSafari && Math.abs(event.timeStamp - compositionEndedAt) < 500) {
			compositionEndedAt = -2e8;
			return true;
		}
		return false;
	}

	let chatInputContainerElement;
	let chatInputElement = $state.raw();

	// --- Lazy RichTextInput swap state (see richTextInputLoader above) ---
	// Whether the plain-<textarea> fallback currently has focus. While true, the
	// swap to the real rich editor is deferred even after the dynamic import
	// resolves — programmatically re-focusing a freshly-mounted element doesn't
	// reliably reopen the iOS keyboard, which would drop mid-sentence typing on
	// exactly the flaky-cellular-cold-boot case this lazy-load is meant to help.
	let fallbackFocused = $state(false);
	// Becomes true once the dynamic import resolves; stays false forever if it
	// rejects (offline before first use, chunk-load failure, etc.), in which
	// case the fallback textarea remains the permanent composer.
	let editorReady = $state(false);
	let RichTextInputComponent: any = $state(null);
	richTextInputLoader
		.then((m) => {
			RichTextInputComponent = m.default;
			editorReady = true;
		})
		.catch((error) => {
			console.error('Failed to load RichTextInput, staying on fallback textarea', error);
		});
	// Buffered calls made against `chatInputElement` while the real editor isn't
	// mounted yet (still loading, or swap deferred by fallbackFocused). Replayed
	// once the real editor actually mounts.
	let pendingReplaceVariables: Record<string, any> | null = null;
	let pendingFocus = $state(false);
	// Tracks which chatInputElement instance we've already replayed the pending
	// queue + current `prompt` into, so remounts (e.g. the {#key} blocks below
	// toggling on settings changes) each get replayed exactly once.
	let lastReplayedEditor: any = null;

	// The focus-aware deferral only matters where programmatic focus can't
	// reopen an on-screen keyboard. With a hardware keyboard it's actively
	// harmful: onMount autofocuses the fallback within one macrotask — before
	// the dynamic import resolves — so the swap would be deferred for the whole
	// first message and markdown would never render while typing (new chats
	// were stuck on the plain textarea). Swap immediately instead, carrying
	// focus + draft over via the replay queue.
	const hasOnScreenKeyboard = isOnScreenKeyboardDevice();

	async function replayEditorQueue(editor = chatInputElement) {
		if (!editor || editor === lastReplayedEditor) return;
		lastReplayedEditor = editor;

		// Capture the draft BEFORE awaiting: the freshly-mounted editor can emit
		// a spurious empty onChange during that tick (its mount-time value=''
		// reactive), clobbering `prompt` before we read it — seed from the value
		// captured at swap time.
		const seedPrompt = prompt;
		await tick();
		if (chatInputElement !== editor) return;

		// The real editor is uncontrolled (no `value` prop wired up), so seed it
		// with whatever `prompt` held at swap time — this covers both explicit
		// setText() calls made while unmounted and plain typing into the
		// fallback textarea before the swap.
		if (seedPrompt !== '') {
			editor.setText?.(seedPrompt);
		}

		if (pendingReplaceVariables) {
			editor.replaceVariables?.(pendingReplaceVariables);
			pendingReplaceVariables = null;
		}

		if (pendingFocus) {
			editor.focus?.();
			pendingFocus = false;
		}
	}

	// Shared Enter/Shift+Enter (submit/steer/queue), Escape, Ctrl+Shift+Enter,
	// and ArrowUp-to-edit-last-message behavior for the chat composer. Used by
	// both RichTextInput (which wraps the native event in `e.detail.event`) and
	// RichTextInputFallback's plain <textarea> (which forwards the native event
	// directly) — both call sites normalize down to a native KeyboardEvent
	// before calling this. Slash/`@`/`#` command-menu triggering isn't included
	// here since that relies on RichTextInput's ProseMirror suggestion
	// infrastructure, which the plain-textarea fallback doesn't have.
	async function handleComposerKeydown(e: KeyboardEvent) {
		const isCtrlPressed = e.ctrlKey || e.metaKey; // metaKey is for Cmd key on Mac
		const suggestionsContainerElement = document.getElementById('suggestions-container');

		if (e.key === 'Escape' && turnLive) {
			// This keydown also bubbles to the window-level Escape handler below.
			// Handle a live response once here, then stop propagation so one key
			// press cannot dispatch multiple concurrent stop requests.
			e.preventDefault();
			e.stopPropagation();
			stopResponse();
			return;
		}

		// Command/Ctrl + Shift + Enter to submit a message pair
		if (isCtrlPressed && e.key === 'Enter' && e.shiftKey) {
			e.preventDefault();
			createMessagePair(prompt);
			// Exclusively a message-pair action — without this return the
			// keystroke also fell through to the enterPressed branch below
			// and double-dispatched a steer/queue/submit (C26).
			return;
		}

		if (prompt === '' && e.key == 'ArrowUp') {
			e.preventDefault();

			const userMessageElement = [...document.getElementsByClassName('user-message')]?.at(-1);

			if (userMessageElement) {
				userMessageElement.scrollIntoView({ block: 'center' });
				const editButton = [...document.getElementsByClassName('edit-user-message-button')]?.at(-1);

				editButton?.click();
			}
		}

		if (!suggestionsContainerElement) {
			if (
				!$mobile ||
				!(
					'ontouchstart' in window ||
					navigator.maxTouchPoints > 0 ||
					navigator.msMaxTouchPoints > 0
				)
			) {
				if (inOrNearComposition(e)) {
					return;
				}

				// Uses keyCode '13' for Enter key for chinese/japanese keyboards.
				//
				// Depending on the user's settings, it will send the message
				// either when Enter is pressed or when Ctrl+Enter is pressed.
				const enterPressed =
					($settings?.ctrlEnterToSend ?? false)
						? (e.key === 'Enter' || e.keyCode === 13) && isCtrlPressed && !e.shiftKey
						: (e.key === 'Enter' || e.keyCode === 13) && !e.shiftKey;

				if (enterPressed) {
					e.preventDefault();
					if (prompt !== '' || files.length > 0) {
						// While the model is WORKING, Enter/Alt+Enter don't start a
						// new send — they route into the queue:
						//   • bare Enter → STEER: inject at the next tool-call
						//     boundary (mid-task course-correct)
						//   • Alt+Enter  → QUEUE: deliver after the whole response
						//     finishes (today's queue behavior)
						// When idle, Enter submits as normal. (Shift+Enter still
						// inserts a newline — it never reaches here because
						// enterPressed requires !shiftKey.)
						const working = turnLive;
						if (working && !hasInFlightFiles) {
							dispatch(e.altKey ? 'queueAfterFinal' : 'steer', prompt);
						} else {
							// A submit with uploads in flight is deferred by
							// Chat.svelte and automatically retried when every
							// attachment is ready.
							dispatch('submit', prompt);
						}
					}
				}
			}
		}
	}

	// Shared image/file/large-text paste handling, see handleComposerKeydown's
	// doc comment for why this is factored out and how the two call sites
	// (RichTextInput vs. RichTextInputFallback) differ in event shape.
	async function handleComposerPaste(e: ClipboardEvent) {
		console.log(e);

		const clipboardData = e.clipboardData || (window as any).clipboardData;

		if (clipboardData && clipboardData.items) {
			for (const item of clipboardData.items) {
				if (item.type.indexOf('image') !== -1) {
					const file = item.getAsFile();
					if (file) {
						await inputFilesHandler([file]);
						e.preventDefault();
					}
				} else if (item?.kind === 'file') {
					const file = item.getAsFile();
					if (file) {
						const _files = [file];
						await inputFilesHandler(_files);
						e.preventDefault();
					}
				} else if (item.type === 'text/plain') {
					if (($settings?.largeTextAsFile ?? false) && !shiftKey) {
						const text = clipboardData.getData('text/plain');

						if (text.length > PASTED_TEXT_CHARACTER_LIMIT) {
							e.preventDefault();
							const blob = new Blob([text], { type: 'text/plain' });
							const file = new File([blob], `Pasted_Text_${Date.now()}.txt`, {
								type: 'text/plain'
							});

							await uploadFileHandler(file, true);
						}
					}
				}
			}
		}
	}

	let filesInputElement = $state();
	let commandsElement;

	// ---- Toolbar rail overflow affordance -----------------------------------
	// The controls are one scrollable line, and on a phone they are wider than
	// the row (attach + toggles + integrations + effort and service-tier pills
	// against ~250px). Fade whichever edge the content continues past — a row
	// that just stops mid-pill behind the mic reads as broken, and there is
	// otherwise nothing to tell you the rest can be reached by swiping.
	let toolbarRailElement = $state<HTMLDivElement | undefined>(undefined);
	let toolbarFade = $state<'none' | 'start' | 'end' | 'both'>('none');

	const updateToolbarFade = () => {
		const el = toolbarRailElement;
		if (!el) return;
		const overflow = el.scrollWidth - el.clientWidth;
		if (overflow <= 1) {
			toolbarFade = 'none';
			return;
		}
		const atStart = el.scrollLeft <= 1;
		const atEnd = el.scrollLeft >= overflow - 1;
		toolbarFade = atStart ? 'end' : atEnd ? 'start' : 'both';
	};

	// The row's own box changes with the viewport (keyboard, rotation), and what
	// is IN it changes with the model (a model with no reasoning efforts drops
	// the pill entirely) and with every toggle that grows a label. A resize
	// observer sees the first, a mutation observer the second; both coalesce
	// into one measurement per frame.
	let toolbarFadeFrame: number | null = null;
	const scheduleToolbarFade = () => {
		if (toolbarFadeFrame !== null) return;
		toolbarFadeFrame = requestAnimationFrame(() => {
			toolbarFadeFrame = null;
			updateToolbarFade();
		});
	};

	$effect(() => {
		const el = toolbarRailElement;
		if (!el) return;
		const resizeObserver = new ResizeObserver(scheduleToolbarFade);
		resizeObserver.observe(el);
		const mutationObserver = new MutationObserver(scheduleToolbarFade);
		mutationObserver.observe(el, { childList: true, subtree: true, characterData: true });
		scheduleToolbarFade();
		return () => {
			resizeObserver.disconnect();
			mutationObserver.disconnect();
			if (toolbarFadeFrame !== null) {
				cancelAnimationFrame(toolbarFadeFrame);
				toolbarFadeFrame = null;
			}
		};
	});

	let inputFiles = $state();

	let dragged = $state(false);
	let shiftKey = $state(false);

	let user = null;
	interface Props {
		onChange?: Function;
		createMessagePair: Function;
		stopResponse: Function;
		autoScroll?: boolean;
		// Single definition lives in Chat.svelte (see its `turnLive` derived).
		turnLive?: boolean;
		atSelectedModel?: Model | undefined;
		selectedModels?: string[];
		history: any;
		prompt?: string;
		files?: any;
		// because that's where persistence lives.
		queue?: any[];
		editQueuedMessage?: (id: string, text: string) => void;
		removeQueuedMessage?: (id: string) => void;
		sendQueuedNow?: () => void;
		userInitiatedStop?: boolean;
		selectedToolIds?: string[];
		selectedFilterIds?: any;
		imageGenerationEnabled?: boolean;
		webSearchEnabled?: boolean;
		studyModeEnabled?: boolean;
		dataVizEnabled?: boolean;
		automationsEnabled?: boolean;
		subagentsEnabled?: boolean;
		// resetting it). No-op default keeps this optional for other callers.
		onSelectionTouched?: () => void;
		// preferred tier for future chats.
		onServiceTierTouched?: (tier: string) => void;
		// decides what's actually valid).
		subagentReasoningEffort?: string;
		// accepts (typically `default` / `flex` / `priority`).
		subagentServiceTier?: string;
		// `_resolve_subagent_model_id`.
		subagentModel?: string;
		subagentExternalToolsEnabled?: boolean;
		serviceTier?: ServiceTier;
		showCommands?: boolean;
		placeholder?: string;
	}

	let {
		onChange = () => {},
		createMessagePair,
		stopResponse,
		autoScroll = $bindable(false),
		turnLive = false,
		atSelectedModel = $bindable(undefined),
		selectedModels = $bindable(['']),
		history,
		prompt = $bindable(''),
		files = $bindable([]),
		queue = [],
		editQueuedMessage = () => {},
		removeQueuedMessage = () => {},
		sendQueuedNow = () => {},
		userInitiatedStop = false,
		selectedToolIds = $bindable([]),
		selectedFilterIds = $bindable([]),
		imageGenerationEnabled = $bindable(false),
		webSearchEnabled = $bindable(false),
		studyModeEnabled = $bindable(false),
		dataVizEnabled = $bindable(false),
		automationsEnabled = $bindable(false),
		subagentsEnabled = $bindable(false),
		onSelectionTouched = () => {},
		onServiceTierTouched = () => {},
		subagentReasoningEffort = $bindable(''),
		subagentServiceTier = $bindable(''),
		subagentModel = $bindable(''),
		subagentExternalToolsEnabled = $bindable(true),
		serviceTier = $bindable('default'),
		showCommands = $bindable(false),
		placeholder = '',
		...eventProps
	}: Props & Record<string, unknown> = $props();

	let capabilityModelIds = $derived(atSelectedModel?.id ? [atSelectedModel.id] : selectedModels);
	let visionCapableModels = $derived(
		capabilityModelIds.filter(
			(model) => $models.find((m) => m.id === model)?.info?.meta?.capabilities?.vision ?? true
		)
	);
	let effectiveVisionCapableModels = $derived(
		capabilityModelIds.filter((modelId) => {
			const model = $models.find((m) => m.id === modelId);
			return (
				(model?.info?.meta?.capabilities?.vision ?? true) ||
				model?.info?.meta?.vision_preprocessor_model_id
			);
		})
	);
	let fileUploadCapableModels = $derived(
		capabilityModelIds.filter(
			(model) => $models.find((m) => m.id === model)?.info?.meta?.capabilities?.file_upload ?? true
		)
	);
	let webSearchCapableModels = $derived(
		capabilityModelIds.filter(
			(model) => $models.find((m) => m.id === model)?.info?.meta?.capabilities?.web_search ?? true
		)
	);
	// Mirrors backend `model_supports_video_input`: an explicit capability wins,
	// otherwise fall back to the provider's declared input modalities. Absent
	// means no — video payloads are far too large to attach speculatively.
	let videoCapableModels = $derived(
		capabilityModelIds.filter((modelId) => {
			const model = $models.find((m) => m.id === modelId);
			if (!model) return false;
			const explicit = model?.info?.meta?.capabilities?.video;
			if (explicit !== undefined && explicit !== null) return !!explicit;
			// `input_modalities` is the flattened field the backend fills from the
			// OpenRouter catalog; `architecture` only exists when the connection
			// fetched a real provider list (i.e. no model_ids allowlist).
			const modalities =
				model?.input_modalities ??
				model?.architecture?.input_modalities ??
				model?.openai?.architecture?.input_modalities ??
				[];
			return Array.isArray(modalities) && modalities.includes('video');
		})
	);
	let videoSupported = $derived(
		capabilityModelIds.length > 0 && videoCapableModels.length === capabilityModelIds.length
	);
	let imageGenerationCapableModels = $derived(
		capabilityModelIds.filter(
			(model) =>
				$models.find((m) => m.id === model)?.info?.meta?.capabilities?.image_generation ?? true
		)
	);
	let toggleFilters = $derived.by(() => {
		const filterLists = capabilityModelIds
			.filter(Boolean)
			.map((id) => ($models.find((model) => model.id === id) || {})?.filters ?? []);
		return filterLists.length > 0
			? filterLists.reduce((acc, filters) =>
					acc.filter((f1) => filters.some((f2) => f2.id === f1.id))
				)
			: [];
	});

	let directToolServersConfigured = $derived(hasEnabledToolServers($settings?.toolServers ?? []));
	let showToolsButton = $derived(
		($tools ?? []).length > 0 ||
			($toolServers ?? []).length > 0 ||
			(directToolServersConfigured && !$toolServersLoaded)
	);

	// $config and $_user start as undefined and populate asynchronously (cache
	// hits resolve fast, but on first visits and slow mobile networks they
	// don't). Treating undefined as "feature disabled" / "no permission" caused
	// buttons to silently vanish until the user reloaded — particularly the
	// web-search button on mobile. Treat undefined/null as "not loaded yet,
	// assume yes". The backend re-validates the feature flag and permissions
	// on actual use, so an optimistic button is safe; the alternative (UI
	// disappearing for the first paint) is worse.

	let showWebSearchButton = $derived(
		capabilityModelIds.length === webSearchCapableModels.length &&
			($config == null || !!$config?.features?.enable_web_search) &&
			($_user == null ||
				$_user?.role === 'admin' ||
				$_user?.permissions?.features?.web_search !== false)
	);
	let showSubagentsButton = $derived(
		($config == null || !!$config?.features?.enable_subagents) &&
			($_user == null ||
				$_user?.role === 'admin' ||
				$_user?.permissions?.features?.subagents !== false)
	);
	let showStudyModeButton = $derived($config == null || !!$config?.features?.enable_study_mode);
	let showDataVizButton = $derived($config == null || !!$config?.features?.enable_data_viz);
	// Automations are model-agnostic (the scheduled run picks its own model), so
	// this only checks the instance flag — same shape as the subagents button.
	let showAutomationsButton = $derived($config == null || !!$config?.features?.enable_automations);
	let showImageGenerationButton = $derived(
		capabilityModelIds.length === imageGenerationCapableModels.length &&
			($config == null || !!$config?.features?.enable_image_generation) &&
			($_user == null ||
				$_user?.role === 'admin' ||
				$_user?.permissions?.features?.image_generation !== false)
	);

	const screenCaptureHandler = async () => {
		try {
			// Request screen media
			const mediaStream = await navigator.mediaDevices.getDisplayMedia({
				video: { cursor: 'never' },
				audio: false
			});
			// Once the user selects a screen, temporarily create a video element
			const video = document.createElement('video');
			video.srcObject = mediaStream;
			// Ensure the video loads without affecting user experience or tab switching
			await video.play();
			// Set up the canvas to match the video dimensions
			const canvas = document.createElement('canvas');
			canvas.width = video.videoWidth;
			canvas.height = video.videoHeight;
			// Grab a single frame from the video stream using the canvas
			const context = canvas.getContext('2d');
			context.drawImage(video, 0, 0, canvas.width, canvas.height);
			// Stop all video tracks (stop screen sharing) after capturing the image
			mediaStream.getTracks().forEach((track) => track.stop());

			// bring back focus to this current tab, so that the user can see the screen capture
			window.focus();

			// Convert the canvas to a Base64 image URL
			const imageUrl = canvas.toDataURL('image/png');
			// Add the captured image to the files array to render it
			files = [...files, { type: 'image', url: imageUrl, fullQuality: false }];
			// Clean memory: Clear video srcObject
			video.srcObject = null;
		} catch (error) {
			// Handle any errors (e.g., user cancels screen sharing)
			console.error('Error capturing screen:', error);
		}
	};

	// Poll the backend file row to track text-extraction progress. The chip's
	// status flips ready/failed when the backend lands data.status === 'completed'
	// or 'failed'. Bounded to ~5 minutes so a stuck extraction doesn't hold a
	// deferred send forever; after that we rely on the backend's lazy fallback
	// at chat-completion time.
	const updatePendingFile = (itemId: string, updates: Record<string, any>): boolean => {
		let found = false;
		files = files.map((item) => {
			if (item?.itemId !== itemId) return item;
			found = true;
			return { ...item, ...updates };
		});
		return found;
	};

	const pollFileExtractionStatus = async (fileId: string, itemId: string) => {
		const maxAttempts = 150;
		const delayMs = 2000;
		for (let attempt = 0; attempt < maxAttempts; attempt++) {
			await new Promise((r) => setTimeout(r, delayMs));
			if (!files.some((f) => f?.itemId === itemId && f?.id === fileId)) return;
			try {
				const file = await getFileById(localStorage.token, fileId);
				if (!file) continue;
				const status = file?.data?.status;
				if (status === 'completed') {
					updatePendingFile(itemId, { file, status: 'uploaded' });
					return;
				}
				if (status === 'failed') {
					updatePendingFile(itemId, { file, status: 'failed' });
					return;
				}
				updatePendingFile(itemId, { file });
			} catch (e) {
				console.error('pollFileExtractionStatus:', e);
			}
		}
		// Polling exhausted — mark the attachment ready. Chat-completion's lazy
		// fallback will handle the actual extraction.
		updatePendingFile(itemId, { status: 'uploaded' });
	};

	// ---------------------------------------------------------------------
	// Video attachments
	//
	// The pipeline runs entirely server-side and its state lives in a DB row,
	// so the composer only mirrors it. Socket events keep the chip live; on
	// mount we re-read from the server instead of trusting anything local,
	// which is what makes a reload / closed tab / different device resume
	// cleanly rather than stranding a spinner.
	// ---------------------------------------------------------------------

	let showVideoModal = $state(false);

	// Job ids are remembered per-chat so a reload can recover jobs that finished
	// while the tab was gone (`/jobs/active` only reports still-running work).
	const videoJobStorageKey = () => `video-jobs:${$chatId || 'new'}`;

	const rememberVideoJob = (jobId: string) => {
		try {
			const key = videoJobStorageKey();
			const ids: string[] = JSON.parse(localStorage.getItem(key) || '[]');
			if (!ids.includes(jobId)) {
				localStorage.setItem(key, JSON.stringify([...ids, jobId].slice(-20)));
			}
		} catch (e) {
			// Private-mode / quota failures must not break attaching a video.
		}
	};

	const forgetVideoJob = (jobId: string) => {
		try {
			const key = videoJobStorageKey();
			const ids: string[] = JSON.parse(localStorage.getItem(key) || '[]');
			localStorage.setItem(key, JSON.stringify(ids.filter((id) => id !== jobId)));
		} catch (e) {
			// ignore
		}
	};

	/** Map a job document onto the composer attachment it drives. */
	const videoItemFromJob = (job: VideoJob) => {
		const done = job.status === 'completed';
		const failed = job.status === 'failed' || job.status === 'canceled';
		return {
			type: 'video',
			jobId: job.id,
			itemId: `video:${job.id}`,
			name:
				job.result?.filename ||
				job.title ||
				(job.source_url ? job.source_url.replace(/^https?:\/\//, '').slice(0, 60) : 'Video'),
			status: done ? 'uploaded' : failed ? 'failed' : 'processing',
			stage: job.stage,
			stageLabel: job.progress?.label || job.stage_label,
			stageDetail: job.progress?.detail ?? '',
			percent: job.progress?.percent ?? null,
			error: job.error ?? '',
			fallbackUsed: !!job.result?.fallback_used,
			sourceKind: job.result?.source ?? '',
			meta: job.result ?? {},
			...(done && job.result?.file_id
				? {
						id: job.result.file_id,
						url: `${WEBUI_API_BASE_URL}/files/${job.result.file_id}`,
						size: job.result.size
					}
				: {})
		};
	};

	const applyVideoJob = (job: VideoJob) => {
		const next = videoItemFromJob(job);
		const idx = files.findIndex((f) => f?.jobId === job.id);
		if (idx === -1) return false;
		files = files.map((f, i) => (i === idx ? { ...f, ...next } : f));
		if (job.status === 'completed' || job.status === 'failed' || job.status === 'canceled') {
			forgetVideoJob(job.id);
		}
		return true;
	};

	const addVideoJobItem = (job: VideoJob) => {
		if (files.some((f) => f?.jobId === job.id)) return;
		files = [...files, videoItemFromJob(job)];
		rememberVideoJob(job.id);
	};

	const videoSubmitHandler = async (spec) => {
		try {
			let fileId: string | undefined;

			if (spec.sourceType === 'upload' && spec.file) {
				// The raw upload is just the pipeline's input; the attachment the
				// user sees is the processed clip the job produces.
				const uploaded = await uploadFile(localStorage.token, spec.file, null, {
					process: false
				});
				if (!uploaded?.id) {
					toast.error($i18n.t('Could not upload that video.'));
					return;
				}
				fileId = uploaded.id;
			}

			const job = await createVideoJob(localStorage.token, {
				source_type: spec.sourceType,
				url: spec.url,
				file_id: fileId,
				chat_id: $chatId || null,
				fps: spec.fps,
				quality: spec.quality,
				start: spec.start,
				end: spec.end,
				audio: spec.audio
			});
			addVideoJobItem(job);
		} catch (e) {
			toast.error(`${e}`);
		}
	};

	const removeVideoJob = async (jobId: string) => {
		forgetVideoJob(jobId);
		try {
			await cancelVideoJob(localStorage.token, jobId);
		} catch (e) {
			// Already finished or already gone — removal from the composer is
			// the user-visible outcome either way.
		}
	};

	/** Re-attach server-side jobs after a reload, tab close, or device switch. */
	const resyncVideoJobs = async () => {
		try {
			let remembered: string[] = [];
			try {
				remembered = JSON.parse(localStorage.getItem(videoJobStorageKey()) || '[]');
			} catch (e) {
				remembered = [];
			}

			const seen = new Set<string>();
			const jobs: VideoJob[] = [];

			// Running work, including anything started on another device.
			const active = await getActiveVideoJobs(localStorage.token).catch(() => ({ jobs: [] }));
			for (const job of active?.jobs ?? []) {
				if (job.chat_id && $chatId && job.chat_id !== $chatId) continue;
				if (!job.chat_id && $chatId) continue;
				seen.add(job.id);
				jobs.push(job);
			}

			// Jobs this tab was tracking that have since reached a terminal state.
			const missing = remembered.filter((id) => !seen.has(id));
			if (missing.length) {
				const byIds = await getVideoJobsByIds(localStorage.token, missing).catch(() => ({
					jobs: []
				}));
				jobs.push(...(byIds?.jobs ?? []));
			}

			for (const job of jobs) {
				if (job.status === 'canceled') {
					forgetVideoJob(job.id);
					continue;
				}
				if (files.some((f) => f?.jobId === job.id)) {
					applyVideoJob(job);
				} else {
					addVideoJobItem(job);
				}
			}
		} catch (e) {
			console.warn('resyncVideoJobs failed', e);
		}
	};

	const videoJobEventHandler = (event) => {
		const payload = event?.data;
		if (payload?.type !== 'video:job') return;
		const job = payload?.data as VideoJob;
		if (!job?.id) return;
		applyVideoJob(job);
	};

	// Reads only `$socket`; the handlers touch `files` when they fire, not
	// during setup, so this never retriggers itself.
	$effect(() => {
		const s = $socket;
		if (!s) return;

		// A disconnect can swallow the final "completed" event, which would leave
		// the chip stuck mid-progress forever. Re-reading on reconnect closes that
		// gap without needing the events to be replayable.
		const onReconnect = () => void resyncVideoJobs();

		s.on('events', videoJobEventHandler);
		s.on('connect', onReconnect);

		return () => {
			s.off('events', videoJobEventHandler);
			s.off('connect', onReconnect);
		};
	});

	const uploadFileHandler = async (file, fullContext: boolean = false) => {
		if ($_user?.role !== 'admin' && !($_user?.permissions?.chat?.file_upload ?? true)) {
			toast.error($i18n.t('You do not have permission to upload files.'));
			return null;
		}

		if (fileUploadCapableModels.length !== selectedModels.length) {
			toast.error($i18n.t('Model(s) do not support file upload'));
			return null;
		}

		const tempItemId = uuidv4();
		const fileItem = {
			type: 'file',
			file: '',
			id: null,
			url: '',
			name: file.name,
			status: 'uploading',
			size: file.size,
			error: '',
			itemId: tempItemId,
			...(containerWorkspaceActive ? { container_mode: true } : {}),
			...(fullContext ? { context: 'full' } : {})
		};

		if (fileItem.size == 0) {
			toast.error($i18n.t('You cannot upload an empty file.'));
			return null;
		}

		files = [...files, fileItem];

		if (!$temporaryChatEnabled) {
			try {
				// If the file is an audio file, provide the language for STT.
				let metadata = null;
				if (
					(file.type.startsWith('audio/') || file.type.startsWith('video/')) &&
					$settings?.audio?.stt?.language
				) {
					metadata = {
						language: $settings?.audio?.stt?.language
					};
				}

				// Upload as a chat attachment. When container workspace is active,
				// extraction/PDF preprocessing is unnecessary; the model reads the
				// original file from /workspace/inputs.
				const uploadedFile = await uploadFile(localStorage.token, file, metadata, {
					process: !containerWorkspaceActive
				});

				if (uploadedFile) {
					if (uploadedFile.error) {
						console.warn('File upload warning:', uploadedFile.error);
						toast.warning(uploadedFile.error);
					}

					// Replace the item in the bound array instead of mutating the
					// pre-$state object captured before the upload await. Mutating
					// that stale raw object could leave the proxied UI item stuck at
					// "uploading" even though the file row was already completed.
					const initialBackendStatus = uploadedFile?.data?.status;
					const nextStatus =
						initialBackendStatus === 'pending' || initialBackendStatus === 'processing'
							? 'processing'
							: initialBackendStatus === 'failed'
								? 'failed'
								: 'uploaded';
					const attachmentStillPresent = updatePendingFile(tempItemId, {
						file: uploadedFile,
						id: uploadedFile.id,
						url: `${WEBUI_API_BASE_URL}/files/${uploadedFile.id}`,
						status: nextStatus
					});

					console.log('File upload completed:', {
						id: uploadedFile.id,
						name: fileItem.name,
						status: nextStatus
					});

					if (attachmentStillPresent && nextStatus === 'processing') {
						void pollFileExtractionStatus(uploadedFile.id, tempItemId);
					}
				} else {
					files = files.filter((item) => item?.itemId !== tempItemId);
				}
			} catch (e) {
				toast.error(`${e}`);
				files = files.filter((item) => item?.itemId !== tempItemId);
			}
		} else {
			// Temporary PDFs follow the normal chat PDF path without creating a
			// durable server-side File row: keep the original bytes as a local data
			// URL so Chat.svelte emits the same native `type: "file"` model input.
			const isPdf = file.type === 'application/pdf' || file.name?.toLowerCase().endsWith('.pdf');
			if (isPdf) {
				try {
					const pdfBlob =
						file.type === 'application/pdf' ? file : new Blob([file], { type: 'application/pdf' });
					const dataUrl = await new Promise<string>((resolve, reject) => {
						const reader = new FileReader();
						reader.onload = () => resolve(String(reader.result ?? ''));
						reader.onerror = () => reject(reader.error ?? new Error('Failed to read PDF'));
						reader.readAsDataURL(pdfBlob);
					});

					updatePendingFile(tempItemId, {
						status: 'uploaded',
						type: 'file',
						url: dataUrl,
						id: `local:${uuidv4()}`,
						temporary: true,
						file: {
							filename: file.name,
							meta: { content_type: 'application/pdf', size: file.size }
						}
					});
					return;
				} catch (error) {
					toast.error($i18n.t('Failed to read the PDF: {{error}}', { error }));
					files = files.filter((item: any) => item?.itemId !== tempItemId);
					return null;
				}
			}

			// Other temporary-chat files continue to use lightweight local text
			// extraction; binary files remain unsupported without an upload.

			const content = await extractContentFromFile(file).catch((error) => {
				toast.error(
					$i18n.t('Failed to extract content from the file: {{error}}', { error: error })
				);
				return null;
			});

			if (content === null) {
				toast.error($i18n.t('Failed to extract content from the file.'));
				files = files.filter((item) => item?.itemId !== tempItemId);
				return null;
			} else {
				console.log('Extracted content from file:', {
					name: file.name,
					size: file.size,
					content: content
				});

				updatePendingFile(tempItemId, {
					status: 'uploaded',
					type: 'text',
					content,
					id: uuidv4() // Temporary ID for the file
				});
			}
		}
	};

	const getFileExtension = (filename: string) => {
		const dotIndex = filename.lastIndexOf('.');
		return dotIndex === -1 ? '' : filename.slice(dotIndex).toLowerCase();
	};

	const normalizeImageMimeType = (type: string) => {
		const normalized = (type || '').toLowerCase();
		if (normalized === 'image/jpg') return 'image/jpeg';
		if (normalized === 'image/pjpeg') return 'image/jpeg';
		if (normalized === 'image/x-png') return 'image/png';
		return normalized;
	};

	const SAFE_IMAGE_MIME_TYPES = new Set(['image/png', 'image/jpeg', 'image/webp', 'image/gif']);
	const HEIC_LIKE_IMAGE_MIME_TYPES = new Set([
		'image/heic',
		'image/heif',
		'image/heic-sequence',
		'image/heif-sequence',
		'image/x-heic',
		'image/x-heif'
	]);
	const HEIC_LIKE_IMAGE_EXTENSIONS = new Set([
		'.heic',
		'.heif',
		'.heics',
		'.heifs',
		'.hif',
		'.hifs'
	]);
	const HEIF_FILE_BRANDS = new Set([
		'heic',
		'heix',
		'hevc',
		'hevx',
		'heim',
		'heis',
		'hevm',
		'hevs',
		'mif1',
		'msf1'
	]);
	const SAFE_IMAGE_EXTENSIONS = new Set(['.png', '.jpg', '.jpeg', '.gif', '.webp']);

	const inferImageMimeTypeFromExtension = (filename: string) => {
		const ext = getFileExtension(filename || '');
		switch (ext) {
			case '.png':
				return 'image/png';
			case '.jpg':
			case '.jpeg':
				return 'image/jpeg';
			case '.gif':
				return 'image/gif';
			case '.webp':
				return 'image/webp';
			default:
				return null;
		}
	};

	const isHeicLikeImage = (file: File) => {
		const type = normalizeImageMimeType(file.type || '');
		const ext = getFileExtension(file.name || '');
		return HEIC_LIKE_IMAGE_MIME_TYPES.has(type) || HEIC_LIKE_IMAGE_EXTENSIONS.has(ext);
	};

	const hasHeifSignature = async (file: File) => {
		try {
			const bytes = new Uint8Array(await file.slice(0, 64).arrayBuffer());
			if (bytes.length < 12) return false;

			const decoder = new TextDecoder('ascii');
			if (decoder.decode(bytes.slice(4, 8)) !== 'ftyp') return false;

			for (let offset = 8; offset + 4 <= bytes.length; offset += 4) {
				if (HEIF_FILE_BRANDS.has(decoder.decode(bytes.slice(offset, offset + 4)))) {
					return true;
				}
			}
		} catch (e) {
			console.debug('HEIF signature sniff failed', e);
		}
		return false;
	};

	const isImageLikeFile = (file: File) => {
		const type = normalizeImageMimeType(file.type || '');
		if (SAFE_IMAGE_MIME_TYPES.has(type) || HEIC_LIKE_IMAGE_MIME_TYPES.has(type)) {
			return true;
		}

		const ext = getFileExtension(file.name || '');
		return SAFE_IMAGE_EXTENSIONS.has(ext) || HEIC_LIKE_IMAGE_EXTENSIONS.has(ext);
	};

	const inputFilesHandler = async (inputFiles: File[] | FileList) => {
		const inputFilesArray = Array.from(inputFiles ?? []);
		console.log('Input files handler called with:', inputFilesArray);

		if (
			($config?.file?.max_count ?? null) !== null &&
			files.length + inputFilesArray.length > ($config?.file?.max_count ?? 0)
		) {
			toast.error(
				$i18n.t(`You can only chat with a maximum of {{maxCount}} file(s) at a time.`, {
					maxCount: $config?.file?.max_count
				})
			);
			return;
		}

		const maxSizeMb = $config?.file?.max_size ?? null;
		const maxSizeBytes = maxSizeMb !== null ? maxSizeMb * 1024 * 1024 : null;

		const getErrorText = (err: any) => {
			const truncate = (text: string, maxLength = 500) =>
				text.length > maxLength ? `${text.slice(0, maxLength - 3)}...` : text;

			if (!err) return null;

			let text: string | null = null;

			if (typeof err === 'string') {
				text = err;
			} else if (typeof err?.detail === 'string') {
				text = err.detail;
			} else if (typeof err?.message === 'string') {
				text = err.message;
			} else {
				const detail = err?.detail;
				if (detail && typeof detail === 'object') {
					if (typeof detail.message === 'string') text = detail.message;
					else if (typeof detail.error === 'string') text = detail.error;
					else {
						try {
							text = JSON.stringify(detail);
						} catch {
							// ignore
						}
					}
				}

				if (text === null) {
					try {
						text = JSON.stringify(err);
					} catch {
						// ignore
					}
				}

				if (text === null) {
					text = String(err);
				}
			}

			return text ? truncate(text) : null;
		};

		const handleInputFile = async (file: File) => {
			console.log('Processing file:', {
				name: file.name,
				type: file.type,
				size: file.size,
				extension: file.name.split('.').at(-1)
			});

			const heicLikeBySignature = await hasHeifSignature(file);
			const isImageLike = isImageLikeFile(file) || heicLikeBySignature;
			if (!isImageLike) {
				if (maxSizeBytes !== null && file.size > maxSizeBytes) {
					console.log('File exceeds max size limit:', {
						fileSize: file.size,
						maxSize: maxSizeBytes
					});
					toast.error(
						$i18n.t(`File size should not exceed {{maxSize}} MB.`, {
							maxSize: maxSizeMb
						})
					);
					return;
				}

				await uploadFileHandler(file);
				return;
			}

			if (effectiveVisionCapableModels.length === 0) {
				toast.error($i18n.t('Selected model(s) do not support image inputs'));
				return;
			}

			const tempImageId = `temp-${Date.now()}-${Math.random().toString(36).substr(2, 9)}`;
			const abortController = new AbortController();
			imageUploadAbortControllers.set(tempImageId, abortController);

			let fileToUpload: File = file;
			let previewUrl = '';
			const heicLike = isHeicLikeImage(file) || heicLikeBySignature;

			try {
				if (!heicLike) {
					const normalizedType = normalizeImageMimeType(file.type || '');
					const inferredType = SAFE_IMAGE_MIME_TYPES.has(normalizedType)
						? normalizedType
						: inferImageMimeTypeFromExtension(file.name || '');

					if (inferredType) {
						const currentType = normalizeImageMimeType(fileToUpload.type || '');
						const fileName = fileToUpload.name || `image-${Date.now()}`;
						fileToUpload =
							currentType === inferredType
								? fileToUpload
								: new File([fileToUpload], fileName, {
										type: inferredType,
										lastModified: fileToUpload.lastModified
									});
					}

					previewUrl = URL.createObjectURL(fileToUpload);
				}

				files = [
					...files,
					{
						type: 'image',
						url: previewUrl,
						name: file.name,
						status: 'uploading',
						progress: 0,
						serverProcessing: heicLike,
						itemId: tempImageId
					}
				];

				const updateProgress = (progress: number) => {
					const fileIndex = files.findIndex((f) => f.itemId === tempImageId);
					if (fileIndex !== -1) {
						// Upload progress can reach 100% before the server responds (e.g. remote storage),
						// so cap at 99% until we actually mark the file as uploaded.
						files[fileIndex].progress = Math.max(0, Math.min(99, progress));
						files[fileIndex].serverProcessing = heicLike && files[fileIndex].progress >= 99;
						files = files;
					}
				};

				// Downscale/re-encode the raster image before upload to cut upload
				// bytes on metered/slow links — unless the user pinned full quality
				// (never set on a fresh placeholder, checked defensively). Returns
				// null to keep the original (tiny/GIF/SVG/decode-fail/not-smaller),
				// including HEIC the browser can't decode (the server converts it).
				const placeholderPinned = files.find((f) => f.itemId === tempImageId)?.fullQuality === true;
				if (!placeholderPinned) {
					try {
						const optimized = await downscaleImageForUpload(fileToUpload);
						if (optimized) fileToUpload = optimized;
					} catch (err) {
						console.error('Image downscale skipped:', err);
					}
				}

				const uploadedFile = await uploadFile(localStorage.token, fileToUpload, null, {
					onProgress: updateProgress,
					signal: abortController.signal,
					process: !containerWorkspaceActive
				});

				if (!uploadedFile) {
					throw new Error('Upload failed: server returned an empty response');
				}

				if (previewUrl) URL.revokeObjectURL(previewUrl);

				const fileIndex = files.findIndex((f) => f.itemId === tempImageId);
				if (fileIndex === -1 || canceledImageUploads.has(tempImageId)) {
					try {
						await deleteFileById(localStorage.token, uploadedFile.id);
					} catch (err) {
						console.error(err);
					}
					return;
				}

				// The server URL is the durable/cross-device source of truth, but
				// this browser already has the exact bytes it uploaded. Prime the
				// same width-keyed object-URL cache Image.svelte will consult so the
				// post-upload transition does not immediately download a thumbnail.
				// An unchanged HEIC/HEIF remains excluded because the server converts
				// it to JPEG. If the browser successfully preprocessed it into a safe
				// WebP/JPEG, however, `fileToUpload` is exactly what the server stored
				// and is safe to reuse here.
				if (SAFE_IMAGE_MIME_TYPES.has(normalizeImageMimeType(fileToUpload.type))) {
					const inlineWidth =
						typeof window !== 'undefined' && (window.devicePixelRatio ?? 1) > 2 ? 1024 : 768;
					primeFileObjectUrlById(uploadedFile.id, fileToUpload, inlineWidth);
				}

				files[fileIndex] = {
					type: 'image',
					url: `${WEBUI_API_BASE_URL}/files/${uploadedFile.id}/content`,
					name: uploadedFile?.meta?.name ?? file.name,
					status: 'uploaded',
					progress: 100,
					serverProcessing: false,
					itemId: tempImageId,
					file: uploadedFile,
					id: uploadedFile.id,
					// Preserve any quality choice already made on the placeholder so
					// a full rebuild here can never silently drop a user's pin.
					fullQuality: files[fileIndex]?.fullQuality === true,
					...(containerWorkspaceActive ? { container_mode: true } : {})
				};
				files = files;
			} catch (err: any) {
				if (previewUrl) URL.revokeObjectURL(previewUrl);

				const isAbortError = err?.name === 'AbortError';
				if (isAbortError || canceledImageUploads.has(tempImageId)) {
					files = files.filter((f) => f.itemId !== tempImageId);
					return;
				}

				console.error(err);
				files = files.filter((f) => f.itemId !== tempImageId);
				const errorText = getErrorText(err);
				const name = file?.name || 'image';
				toast.error(
					errorText ? `${name}: ${errorText}` : `${name}: ${$i18n.t('Failed to upload image')}`
				);
			} finally {
				canceledImageUploads.delete(tempImageId);
				imageUploadAbortControllers.delete(tempImageId);
			}
		};

		const maxConcurrentUploads = $mobile ? 2 : 4;
		const queue = [...inputFilesArray];
		const workers = Array.from(
			{ length: Math.min(maxConcurrentUploads, queue.length) },
			async () => {
				while (queue.length > 0) {
					const nextFile = queue.shift();
					if (nextFile) {
						await handleInputFile(nextFile);
					}
				}
			}
		);

		void Promise.all(workers);
	};

	const onDragOver = (e) => {
		e.preventDefault();

		// Check if a file is being dragged.
		if (e.dataTransfer?.types?.includes('Files')) {
			dragged = true;
		} else {
			dragged = false;
		}
	};

	const onDragLeave = () => {
		dragged = false;
	};

	const onDrop = async (e) => {
		e.preventDefault();
		console.log(e);

		if (e.dataTransfer?.files) {
			const inputFiles = Array.from(e.dataTransfer?.files);
			if (inputFiles && inputFiles.length > 0) {
				console.log(inputFiles);
				inputFilesHandler(inputFiles);
			}
		}

		dragged = false;
	};

	const onKeyDown = (e) => {
		if (e.key === 'Shift') {
			shiftKey = true;
		}

		if (e.key === 'Escape') {
			console.log('Escape');
			dragged = false;

			// Stop response if generating (allows Escape to work even when input is not focused)
			if (turnLive) {
				stopResponse();
			}
		}
	};

	const onKeyUp = (e) => {
		if (e.key === 'Shift') {
			shiftKey = false;
		}
	};

	const onFocus = () => {};

	const onBlur = () => {
		shiftKey = false;
	};

	onMount(async () => {
		suggestions = [
			{
				char: '@',
				render: getSuggestionRenderer(CommandSuggestionList, {
					i18n,
					onSelect: (e) => {
						const { type, data } = e;

						if (type === 'model') {
							atSelectedModel = data;
						}

						document.getElementById('chat-input')?.focus({ preventScroll: true });
					},

					insertTextHandler: insertTextAtCursor,
					onUpload: (e) => {
						const { type, data } = e;

						if (type === 'file') {
							if (files.find((f) => f.id === data.id)) {
								return;
							}
							files = [
								...files,
								{
									...data,
									status: 'processed'
								}
							];
						} else {
							dispatch('upload', e);
						}
					}
				})
			},
			{
				char: '/',
				render: getSuggestionRenderer(CommandSuggestionList, {
					i18n,
					onSelect: (e) => {
						const { type, data } = e;

						if (type === 'model') {
							atSelectedModel = data;
						}

						document.getElementById('chat-input')?.focus({ preventScroll: true });
					},

					insertTextHandler: insertTextAtCursor,
					onUpload: (e) => {
						const { type, data } = e;

						if (type === 'file') {
							if (files.find((f) => f.id === data.id)) {
								return;
							}
							files = [
								...files,
								{
									...data,
									status: 'processed'
								}
							];
						} else {
							dispatch('upload', e);
						}
					}
				})
			},
			{
				char: '#',
				render: getSuggestionRenderer(CommandSuggestionList, {
					i18n,
					onSelect: (e) => {
						const { type, data } = e;

						if (type === 'model') {
							atSelectedModel = data;
						}

						document.getElementById('chat-input')?.focus({ preventScroll: true });
					},

					insertTextHandler: insertTextAtCursor,
					onUpload: (e) => {
						const { type, data } = e;

						if (type === 'file') {
							if (files.find((f) => f.id === data.id)) {
								return;
							}
							files = [
								...files,
								{
									...data,
									status: 'processed'
								}
							];
						} else {
							dispatch('upload', e);
						}
					}
				})
			}
		];
		loaded = true;

		// Autofocus the composer on mount — hardware-keyboard devices only; on
		// touch it would summon the keyboard on every chat open / app boot.
		if (!hasOnScreenKeyboard) {
			window.setTimeout(() => {
				const chatInput = document.getElementById('chat-input');
				chatInput?.focus({ preventScroll: true });
			}, 0);
		}

		window.addEventListener('keydown', onKeyDown);
		window.addEventListener('keyup', onKeyUp);

		window.addEventListener('focus', onFocus);
		window.addEventListener('blur', onBlur);

		// Re-attach any server-side video work before the first paint settles, so
		// a reloaded tab shows a live progress chip instead of an empty composer.
		void resyncVideoJobs();

		await tick();

		const dropzoneElement = document.getElementById('chat-container');

		dropzoneElement?.addEventListener('dragover', onDragOver);
		dropzoneElement?.addEventListener('drop', onDrop);
		dropzoneElement?.addEventListener('dragleave', onDragLeave);

		// Load tool list then sanitize selectedToolIds before rendering any "active tool" UI.
		// This prevents a brief "1 available tool" flicker when a previously-selected
		// tool server/tool has been deleted in admin settings.
		const fetchedTools = $tools ?? (await getTools(localStorage.token));
		if ($tools === null) {
			await tools.set(fetchedTools);
		}

		if ((selectedToolIds ?? []).some((id) => id.startsWith('direct_server:'))) {
			await loadToolServers().catch((e) => {
				console.error('Failed to load selected direct tool servers', e);
			});
		}

		const fetchedToolIdSet = new Set((fetchedTools ?? []).map((t) => t?.id).filter(Boolean));
		const previousSelectedToolIds = selectedToolIds ?? [];
		selectedToolIds = previousSelectedToolIds.filter((id) => {
			if (!id) return false;
			if (fetchedToolIdSet.has(id)) return true;

			// Direct tool servers are generated client-side from $toolServers.
			if (id.startsWith('direct_server:')) {
				const idx = Number(id.split(':').at(-1));
				return Number.isFinite(idx) && Boolean(($toolServers ?? [])[idx]?.info);
			}

			return false;
		});

		// Surface dropped MCP / OpenAPI server selections instead of silently
		// stripping them. Previously a missing /tools/ entry just disappeared
		// from selectedToolIds, so the user thought a tool was on while the
		// outbound request had no tool_ids — a major "MCP doesn't work" footgun.
		const droppedServerIds = previousSelectedToolIds.filter(
			(id) =>
				id &&
				!selectedToolIds.includes(id) &&
				(id.startsWith('server:mcp:') || id.startsWith('user:mcp:') || id.startsWith('server:'))
		);
		if (droppedServerIds.length > 0) {
			toast.warning(
				droppedServerIds.length === 1
					? $i18n.t('Tool server "{{id}}" is unavailable and has been deselected.', {
							id: droppedServerIds[0].replace(/^server:(mcp:)?/, '')
						})
					: $i18n.t('{{count}} tool servers are unavailable and have been deselected.', {
							count: droppedServerIds.length
						})
			);
		}
		toolSelectionReady = true;

		// Load reasoning effort preferences
		loadReasoningEffortPreferences();
	});

	onDestroy(() => {
		console.log('destroy');
		window.removeEventListener('keydown', onKeyDown);
		window.removeEventListener('keyup', onKeyUp);

		window.removeEventListener('focus', onFocus);
		window.removeEventListener('blur', onBlur);

		const dropzoneElement = document.getElementById('chat-container');

		if (dropzoneElement) {
			dropzoneElement?.removeEventListener('dragover', onDragOver);
			dropzoneElement?.removeEventListener('drop', onDrop);
			dropzoneElement?.removeEventListener('dragleave', onDragLeave);
		}
	});
	$effect(() => {
		selectedModelIds = atSelectedModel !== undefined ? [atSelectedModel.id] : selectedModels;
	});
	// Resolve the selected model(s) that are currently in (or about to enter) a
	// peak window, so we only render the notice block when there's something to
	// say. `$peakClock` ticks so this re-evaluates as time passes.
	let peakNoticeModels = $derived(
		(selectedModelIds ?? [])
			.filter((id) => id && id !== '')
			.filter((id, index, arr) => arr.indexOf(id) === index)
			.map((id) => $models.find((m) => m.id === id))
			.filter(
				(m): m is NonNullable<typeof m> =>
					!!m && getPeakStatus(getPeakHoursConfig(m), $peakClock).state !== 'none'
			)
	);
	// "Is a turn live" is computed ONCE, in Chat.svelte, and passed down — see
	// the `turnLive` prop. It used to be recomputed here from four separate
	// props while Chat.svelte kept its own copy for the programmatic-submit
	// guard, and the two had already drifted (Chat's omitted queueDrainPending),
	// which is how bare Enter could flip between send and steer inconsistently.
	// A queue is "paused" only after a real user Stop (not merely the absence of a
	// live turn) — that keeps the amber "Paused — Send now" banner from flashing
	// during a normal server-side auto-drain. userInitiatedStop catches the
	// immediate, this-tab case; leafStopped derives it from the DURABLE userStopped
	// flag on the current leaf so a RELOADED tab (or any other tab/device viewing
	// the same stopped-then-pending chat) still surfaces the resume affordance —
	// the per-tab latch alone hid it everywhere but the tab that pressed Stop.
	let leafStopped = $derived(
		!!history?.currentId && history.messages?.[history.currentId]?.userStopped === true
	);
	let queuePaused = $derived(queue.length > 0 && !turnLive && (userInitiatedStop || leafStopped));
	let containerFeatures = $derived(($config as any)?.features ?? {});
	let containerToolId = $derived(
		containerFeatures?.container_mcp_server_id
			? `server:mcp:${containerFeatures.container_mcp_server_id}`
			: ''
	);
	let containerWorkspaceConfigured = $derived(
		Boolean(containerFeatures?.enable_container_workspace_sync && containerToolId)
	);
	let containerWorkspaceActive = $derived(
		Boolean(containerWorkspaceConfigured && (selectedToolIds ?? []).includes(containerToolId))
	);
	let showContainerButton = $derived(containerWorkspaceConfigured);
	// Keep the send affordance active during uploads. Chat.svelte turns an
	// attempted send into a deferred send and fires it as soon as every file is
	// ready. When container workspace is active, backend extraction is not
	// required.
	let hasInFlightFiles = $derived(
		files.some(
			(f) =>
				f?.status === 'uploading' ||
				// A video's file row does not exist until its job finishes, so the
				// container-workspace exemption below must not apply to it —
				// sending early would attach nothing at all.
				(f?.type === 'video' && f?.status === 'processing') ||
				(f?.status === 'processing' && !containerWorkspaceActive)
		)
	);
	let sendDisabled = $derived(prompt === '' && files.length === 0);
	let subagentExternalToolsAllowed = $derived(!!$config?.features?.subagent_allow_external_tools);
	let selectedSubagentExternalToolIds = $derived(
		(selectedToolIds ?? []).filter((id) => String(id).startsWith('server:'))
	);
	// Keep `allowedServiceTiers` current for the selected model and clamp
	// `serviceTier` into range when it isn't. Force 'default' when the model
	// has service_tier disabled in its settings — otherwise a tier that was
	// valid for the previous model leaks through and gets sent in the payload
	// even though the selector UI is hidden. This never RESTORES a persisted
	// per-model preference — that's Chat.svelte's job (see the comment above
	// `serviceTier`'s declaration), so a remount of this component can never
	// clobber the tier that's currently in effect.
	$effect(() => {
		if (selectedModelIds.length === 1 && preferencesLoaded) {
			if (supportsServiceTier) {
				if (!allowedServiceTiers.includes(serviceTier)) {
					serviceTier = allowedServiceTiers[0] ?? 'default';
				}
			} else {
				serviceTier = 'default';
			}
		}
	});
	// Update reasoning effort when selected model changes
	$effect(() => {
		if (selectedModelIds.length > 0 && preferencesLoaded) {
			const newModelId = selectedModelIds[0];
			if (newModelId !== currentTrackedModel) {
				const isFirstTimeSettingModel = !currentTrackedModel;
				const shouldLoadStoredPreference =
					!isFirstTimeSettingModel || !userModifiedEffortForCurrentModel;

				currentTrackedModel = newModelId;
				userModifiedEffortForCurrentModel = false;

				const effective = getEffectiveForModel(newModelId);

				if (shouldLoadStoredPreference) {
					const newEffort = reasoningEffortByModel[newModelId] || 'medium';
					reasoningEffort = clampEffortToEffective(newEffort, effective) ?? 'medium';
				} else {
					const clamped = clampEffortToEffective(reasoningEffort, effective) ?? 'medium';
					reasoningEffort = clamped;
					reasoningEffortByModel[newModelId] = clamped;
					saveReasoningEffortPreferences();
				}
			}
		}
	});
	// If model capabilities change (admin updates), ensure current selection is still valid.
	$effect(() => {
		if (selectedModelIds.length === 1 && preferencesLoaded) {
			const modelId = selectedModelIds[0];
			const effective = getEffectiveForModel(modelId);
			const clamped = clampEffortToEffective(reasoningEffort, effective);
			if (clamped && clamped !== reasoningEffort) {
				reasoningEffort = clamped;
				reasoningEffortByModel[modelId] = clamped;
				saveReasoningEffortPreferences();
			}
		}
	});
	$effect(() => {
		if (!showValvesModal) {
			integrationsMenuCloseOnOutsideClick = true;
		}
	});
	// Resolves the subagent's effective model id the same way the backend
	// (`utils/subagent._resolve_subagent_model_id`) does — per-chat override
	// (`subagentModel`) → admin default (SUBAGENT_DEFAULT_MODEL) → parent's
	// currently-selected model. Powers the dynamic service-tier dropdown inside
	// the SubagentSettings popover so the allowed tiers always match whichever
	// model the subagent will actually run as.
	let resolvedSubagentModelId = $derived(
		subagentModel ||
			'' ||
			($config?.features?.subagent_default_model ?? '') ||
			(atSelectedModel?.id ?? selectedModelIds?.[0]) ||
			''
	);
	let allowedSubagentServiceTiers = $derived(getServiceTiersForModel(resolvedSubagentModelId));
	// Changing the subagent model can shrink the set of service tiers it
	// supports. If a previously-chosen *concrete* tier is no longer valid, fall
	// back to '' (admin default) so the <select bind:value> can't desync to a
	// phantom option and silently keep sending an unsupported tier. '' is always
	// a valid option, so this only fires on a real mismatch (no reactive loop).
	$effect(() => {
		if (subagentServiceTier && !allowedSubagentServiceTiers.includes(subagentServiceTier)) {
			subagentServiceTier = '';
		}
	});
	$effect(() => {
		onChange({
			prompt,
			files: files
				.filter((file) => file.type !== 'image')
				.map((file) => {
					return {
						...file,
						user: undefined,
						access_control: undefined
					};
				}),
			selectedToolIds,
			selectedFilterIds,
			imageGenerationEnabled,
			webSearchEnabled,
			studyModeEnabled,
			dataVizEnabled,
			automationsEnabled,
			subagentsEnabled,
			subagentReasoningEffort,
			subagentServiceTier,
			subagentExternalToolsEnabled,
			// Only include reasoning when the selected model is configured as a reasoning model.
			...(showReasoningEffortSelector ? { reasoning: { effort: reasoningEffort } } : {})
		});
	});
	$effect(() => {
		showCommands = ['/', '#', '@'].includes(command?.charAt(0)) || '\\#' === command?.slice(0, 2);
	});
	$effect(() => {
		activeServerToolIds = toolSelectionReady
			? (selectedToolIds ?? []).filter(
					(id) =>
						id !== containerToolId &&
						(id.startsWith('server:mcp:') ||
							id.startsWith('user:mcp:') ||
							id.startsWith('server:') ||
							id.startsWith('direct_server:'))
				)
			: [];
	});
	$effect(() => {
		if (editorReady && fallbackFocused && !hasOnScreenKeyboard) {
			pendingFocus = true;
			fallbackFocused = false;
		}
	});
	// Single source of truth for which composer is shown. The real editor only
	// mounts once it's loaded AND the fallback isn't currently focused; the
	// fallback is shown in every other case (still loading, loaded-but-focused,
	// or load failed) — and because this is a plain `{#if}/{:else}` at the top
	// level of the template (not nested inside `{#await}`), the SAME
	// RichTextInputFallback instance persists across the pending→resolved
	// transition instead of being destroyed and recreated, which is what
	// actually preserves the iOS keyboard.
	let showRichTextInput = $derived(editorReady && !fallbackFocused);
	$effect(() => {
		const editor = chatInputElement;
		if (editor) {
			untrack(() => {
				void replayEditorQueue(editor);
			});
		}
	});
	// Subagents are model-agnostic (the inner subagent picks its own model
	// from chat.params.subagentModel / config.SUBAGENT_DEFAULT_MODEL), so the
	// derived button state above only checks the global feature flag and user
	// permission rather than the parent model's capabilities.
	// Everything the Integrations menu manages (server tools, toggle filters,
	// data viz, image generation, study mode) is summarized as a single count
	// badge on the Integrations button itself, since those toggles no longer
	// render their own pills in the toolbar.
	let activeIntegrationCount = $derived(
		(toolSelectionReady ? activeServerToolIds.length : 0) +
			(selectedFilterIds?.length ?? 0) +
			(dataVizEnabled ? 1 : 0) +
			(automationsEnabled ? 1 : 0) +
			(imageGenerationEnabled ? 1 : 0) +
			(studyModeEnabled ? 1 : 0)
	);
</script>

<FilesOverlay show={dragged} />
<ToolServersModal bind:show={showTools} {selectedToolIds} />

<VideoAttachModal bind:show={showVideoModal} onSubmit={videoSubmitHandler} />

<InputVariablesModal
	bind:show={showInputVariablesModal}
	variables={inputVariables}
	onSave={inputVariablesModalCallback}
/>

<ValvesModal
	bind:show={showValvesModal}
	userValves={true}
	type={selectedValvesType}
	id={selectedValvesItemId ?? null}
	onsave={async () => {
		await tick();
	}}
	onclose={() => {
		integrationsMenuCloseOnOutsideClick = true;
	}}
/>

{#if loaded}
	<div class="w-full font-primary">
		<div class=" mx-auto inset-x-0 bg-transparent flex justify-center">
			<div
				class="flex flex-col px-3 {($settings?.widescreenMode ?? null)
					? 'max-w-full'
					: 'max-w-6xl'} w-full"
			>
				<div class="relative">
					<!-- The old always-on jump-to-bottom arrow here was REMOVED: it was a
					     duplicate of Chat.svelte's jump pill with worse behavior — shown at
					     ANY distance while not following (the pill waits 320px so near-bottom
					     re-engagement happens by scrolling), and it teleported with a raw
					     scrollTop write instead of the retargeting glide. One affordance,
					     one code path. -->
				</div>
			</div>
		</div>

		<div class="bg-transparent">
			<div
				class="{($settings?.widescreenMode ?? null)
					? 'max-w-full'
					: 'max-w-6xl'} px-2.5 mx-auto inset-x-0"
			>
				<div class="">
					<input
						bind:this={filesInputElement}
						bind:files={inputFiles}
						type="file"
						hidden
						multiple
						onchange={async () => {
							if (inputFiles && inputFiles.length > 0) {
								const _inputFiles = Array.from(inputFiles);
								inputFilesHandler(_inputFiles);
							} else {
								toast.error($i18n.t(`File not found.`));
							}

							filesInputElement.value = '';
						}}
					/>

					{#if recording}
						<VoiceRecording
							bind:recording
							onCancel={async () => {
								recording = false;

								await tick();
								if (!hasOnScreenKeyboard) {
									document.getElementById('chat-input')?.focus({ preventScroll: true });
								}
							}}
							onConfirm={async (data) => {
								const { text, filename } = data;

								recording = false;

								await tick();
								insertTextAtCursor(text);

								await tick();
								// After dictation the likely next act is Send, not typing —
								// don't summon the keyboard on touch.
								if (!hasOnScreenKeyboard) {
									document.getElementById('chat-input')?.focus({ preventScroll: true });
								}

								if ($settings?.speechAutoSend ?? false) {
									dispatch('submit', prompt);
								}
							}}
						/>
					{:else}
						<QueuedMessages
							{queue}
							{editQueuedMessage}
							{removeQueuedMessage}
							{sendQueuedNow}
							paused={queuePaused}
						/>

						{#if peakNoticeModels.length > 0}
							<div class="kb-hide flex flex-col items-start gap-1 px-1 mb-1.5">
								{#each peakNoticeModels as peakModel (peakModel.id)}
									<PeakHoursNotice model={peakModel} />
								{/each}
							</div>
						{/if}

						<form
							class="w-full flex flex-col gap-1.5"
							onsubmit={preventDefault(() => {
								// The send button doubles as a "Steer" button while the model is
								// working (see #steer-message-button): clicking it injects the
								// message at the next tool-call boundary, same as bare Enter.
								// Queue-after-final stays available via Alt+Enter. Otherwise it
								// submits normally.
								const working = turnLive;
								dispatch(working && !hasInFlightFiles ? 'steer' : 'submit', prompt);
							})}
						>
							<div
								id="message-input-container"
								class="flex-1 flex flex-col relative w-full shadow-lg rounded-2xl {$temporaryChatEnabled
									? 'border-hairline border-dashed border-gray-100 dark:border-gray-800 hover:border-gray-200 focus-within:border-gray-200 hover:dark:border-gray-700 focus-within:dark:border-gray-700'
									: serviceTier === 'priority'
										? 'border border-success/70 dark:border-success-dark/70 hover:border-success focus-within:border-success hover:dark:border-success-dark focus-within:dark:border-success-dark'
										: serviceTier === 'flex'
											? 'border border-book-cloth/70 dark:border-kraft/70 hover:border-book-cloth focus-within:border-book-cloth hover:dark:border-kraft focus-within:dark:border-kraft'
											: 'border-hairline border-gray-100 dark:border-gray-850 hover:border-gray-200 focus-within:border-gray-100 hover:dark:border-gray-800 focus-within:dark:border-gray-800'}  transition px-1 bg-white/5 dark:bg-gray-500/5 backdrop-blur-sm dark:text-gray-100"
								dir={$settings?.chatDirection ?? 'auto'}
							>
								{#if atSelectedModel !== undefined}
									<div class="px-3 pt-3 text-left w-full flex flex-col z-10">
										<div class="flex items-center justify-between w-full">
											<div class="pl-[1px] flex items-center gap-2 text-sm dark:text-gray-500">
												<img
													use:imageFallback
													alt="model profile"
													class="size-3.5 max-w-[28px] object-cover rounded-full"
													src={$models.find((model) => model.id === atSelectedModel.id)?.info?.meta
														?.profile_image_url ??
														($i18n.language === 'dg-DG'
															? `${WEBUI_BASE_URL}/doge.png`
															: `${WEBUI_BASE_URL}/static/favicon.png`)}
													decoding="async"
												/>
												<div>
													<span class="">{atSelectedModel.name}</span>
												</div>
											</div>
											<div>
												<button
													class="flex items-center dark:text-gray-500"
													onmousedown={preventDefault()}
													onclick={() => {
														atSelectedModel = undefined;
													}}
												>
													<XMark />
												</button>
											</div>
										</div>
									</div>
								{/if}

								{#if files.length > 0}
									<div class="mx-2 mt-2.5 pb-1.5 flex items-center flex-wrap gap-2">
										{#each files as file, fileIdx}
											{#if file.type === 'image'}
												<div class="relative group">
													<div class="relative">
														{#if file.url}
															<Image
																src={file.url}
																alt=""
																imageClassName="max-h-48 max-w-64 rounded-xl object-cover"
															/>
														{:else}
															<div
																class="h-32 w-48 max-h-48 max-w-64 rounded-xl bg-gray-100 dark:bg-gray-800 border-hairline border-gray-200 dark:border-gray-700 flex flex-col items-center justify-center text-gray-500 dark:text-gray-400 text-xs"
															>
																<Photo className="size-7" strokeWidth="1.75" />
																<div class="mt-2 max-w-36 truncate">
																	{file.name ?? $i18n.t('Image')}
																</div>
															</div>
														{/if}
														{#if file.status === 'uploading'}
															<div
																class="absolute inset-0 rounded-xl bg-black/40 flex flex-col gap-1 items-center justify-center text-white"
															>
																<!-- Circular progress ring -->
																<svg class="size-12" viewBox="0 0 36 36">
																	<!-- Background circle -->
																	<circle
																		cx="18"
																		cy="18"
																		r="14"
																		fill="none"
																		stroke="rgba(255,255,255,0.3)"
																		stroke-width="3"
																	/>
																	<!-- Progress circle -->
																	<circle
																		cx="18"
																		cy="18"
																		r="14"
																		fill="none"
																		stroke="white"
																		stroke-width="3"
																		stroke-linecap="round"
																		stroke-dasharray={2 * Math.PI * 14}
																		stroke-dashoffset={2 *
																			Math.PI *
																			14 *
																			(1 - (file.progress || 0) / 100)}
																		transform="rotate(-90 18 18)"
																		class="transition-all duration-150"
																	/>
																</svg>
																{#if file.serverProcessing}
																	<div class="text-[11px] font-medium px-2 text-center">
																		{(file.progress ?? 0) >= 99
																			? $i18n.t('Converting image…')
																			: $i18n.t('Uploading image…')}
																	</div>
																{/if}
															</div>
														{/if}
														{#if atSelectedModel ? effectiveVisionCapableModels.length === 0 : selectedModels.length !== effectiveVisionCapableModels.length}
															<Tooltip
																className="absolute top-2 left-2"
																content={$i18n.t(
																	'Models without native vision (using preprocessor): {{ models }}',
																	{
																		models: [
																			...(atSelectedModel ? [atSelectedModel] : selectedModels)
																		]
																			.filter((id) => !effectiveVisionCapableModels.includes(id))
																			.join(', ')
																	}
																)}
															>
																<svg
																	xmlns="http://www.w3.org/2000/svg"
																	viewBox="0 0 24 24"
																	fill="currentColor"
																	aria-hidden="true"
																	class="size-5 fill-warning dark:fill-warning-dark"
																>
																	<path
																		fill-rule="evenodd"
																		d="M9.401 3.003c1.155-2 4.043-2 5.197 0l7.355 12.748c1.154 2-.29 4.5-2.599 4.5H4.645c-2.309 0-3.752-2.5-2.598-4.5L9.4 3.003ZM12 8.25a.75.75 0 0 1 .75.75v3.75a.75.75 0 0 1-1.5 0V9a.75.75 0 0 1 .75-.75Zm0 8.25a.75.75 0 1 0 0-1.5.75.75 0 0 0 0 1.5Z"
																		clip-rule="evenodd"
																	/>
																</svg>
															</Tooltip>
														{/if}
													</div>
													<!-- Image quality / compression badge (bottom left) -->
													{#if file.status !== 'uploading'}
														<div class="absolute bottom-1.5 left-1.5">
															<ImageQualityBadge
																fullQuality={file.fullQuality === true}
																size={file?.file?.meta?.size ?? null}
																ontoggle={(e) => {
																	const target = file.itemId
																		? files.find((f) => f.itemId === file.itemId)
																		: files[fileIdx];
																	if (target) {
																		target.fullQuality = e.detail.fullQuality;
																		files = files;
																	}
																}}
															/>
														</div>
													{/if}
													<!-- Action buttons (top right) -->
													<div class="absolute top-1.5 right-1.5 flex gap-1">
														<button
															class="p-1.5 max-md:p-2.5 bg-white/90 hover:bg-white text-gray-700 rounded-full shadow-sm {($settings?.highContrastMode ??
																false) ||
															$mobile ||
															file.status === 'uploading'
																? ''
																: 'group-hover:opacity-100 opacity-0 transition-opacity'}"
															type="button"
															aria-label={$i18n.t('Remove file')}
															onclick={() => {
																const fileToRemove = files[fileIdx];
																if (
																	fileToRemove?.type === 'image' &&
																	fileToRemove?.status === 'uploading' &&
																	fileToRemove?.itemId
																) {
																	canceledImageUploads.add(fileToRemove.itemId);
																	imageUploadAbortControllers.get(fileToRemove.itemId)?.abort();
																	imageUploadAbortControllers.delete(fileToRemove.itemId);
																}

																const url = fileToRemove?.url;
																if (typeof url === 'string' && url.startsWith('blob:')) {
																	URL.revokeObjectURL(url);
																}

																files.splice(fileIdx, 1);
																files = files;
															}}
														>
															<svg
																xmlns="http://www.w3.org/2000/svg"
																viewBox="0 0 20 20"
																fill="currentColor"
																aria-hidden="true"
																class="size-4"
															>
																<path
																	d="M6.28 5.22a.75.75 0 00-1.06 1.06L8.94 10l-3.72 3.72a.75.75 0 101.06 1.06L10 11.06l3.72 3.72a.75.75 0 101.06-1.06L11.06 10l3.72-3.72a.75.75 0 00-1.06-1.06L10 8.94 6.28 5.22z"
																/>
															</svg>
														</button>
													</div>
												</div>
											{:else if file.type === 'video'}
												<VideoAttachment
													item={file}
													ondismiss={async () => {
														const jobId = file?.jobId;
														files.splice(fileIdx, 1);
														files = files;
														if (jobId) {
															await removeVideoJob(jobId);
														}
													}}
												/>
											{:else}
												<FileItem
													item={file}
													name={file.name}
													type={file.type}
													size={file?.size}
													loading={file.status === 'uploading'}
													dismissible={true}
													edit={true}
													small={true}
													containerMode={containerWorkspaceActive}
													allowContainer={containerWorkspaceConfigured && !containerWorkspaceActive}
													modal={['file', 'collection'].includes(file?.type)}
													ondismiss={async () => {
														// Remove from UI state
														files.splice(fileIdx, 1);
														files = files;
													}}
													onmodeChange={(e) => {
														if (e.detail?.mode === 'container') {
															enableContainerTool();
														}
														// Mode toggle on the chip mutates `file.processing_mode`
														// in place; bump the array reference so reactivity fires.
														files = files;
													}}
													onclick={() => {
														console.log(file);
													}}
												/>
											{/if}
										{/each}
									</div>
								{/if}

								<div class="px-2.5">
									<div
										class="scrollbar-hidden rtl:text-right ltr:text-left bg-transparent dark:text-gray-100 outline-hidden w-full px-1 resize-none h-fit max-h-96 overflow-auto {files.length ===
										0
											? atSelectedModel !== undefined
												? 'pt-1.5'
												: 'pt-2.5'
											: 'pt-1'}"
										id="chat-input-container"
									>
										{#if suggestions}
											{#key $settings?.richTextInput ?? true}
												{#key $settings?.showFormattingToolbar ?? false}
													{#if !showRichTextInput}
														<!-- Shown whenever the real editor isn't loaded yet, load
														     failed, OR the fallback is currently focused (deferring
														     the swap avoids dropping the iOS keyboard mid-sentence —
														     programmatic re-focus doesn't reliably reopen it). This is
														     a single `{#if}` at the top level (not nested inside
														     `{#await}`), so this is the SAME RichTextInputFallback
														     instance across the pending→resolved transition; it's
														     never destroyed and recreated, and swaps to RichTextInput
														     silently on blur via onFocusChange. -->
														<RichTextInputFallback
															id="chat-input"
															bind:value={prompt}
															placeholder={placeholder
																? placeholder
																: turnLive
																	? $i18n.t('Enter to steer · Alt+Enter to queue')
																	: $i18n.t('Send a Message')}
															onFocusChange={(focused) => (fallbackFocused = focused)}
															onkeydown={(e) => handleComposerKeydown(e)}
															onpaste={(e) => handleComposerPaste(e)}
														/>
													{:else}
														<RichTextInputComponent
															bind:this={chatInputElement}
															id="chat-input"
															onChange={(e) => {
																prompt = e.md;
																command = getCommand();
															}}
															json={true}
															richText={$settings?.richTextInput ?? true}
															messageInput={true}
															showFormattingToolbar={$settings?.showFormattingToolbar ?? false}
															floatingMenuPlacement={'top-start'}
															insertPromptAsRichText={$settings?.insertPromptAsRichText ?? false}
															shiftEnter={!($settings?.ctrlEnterToSend ?? false) &&
																!$mobile &&
																!(
																	'ontouchstart' in window ||
																	navigator.maxTouchPoints > 0 ||
																	navigator.msMaxTouchPoints > 0
																)}
															placeholder={placeholder
																? placeholder
																: turnLive
																	? $i18n.t('Enter to steer · Alt+Enter to queue')
																	: $i18n.t('Send a Message')}
															largeTextAsFile={($settings?.largeTextAsFile ?? false) && !shiftKey}
															autocomplete={$config?.features?.enable_autocomplete_generation &&
																($settings?.promptAutocomplete ?? false)}
															generateAutoCompletion={async (text) => {
																if (selectedModelIds.length === 0 || !selectedModelIds.at(0)) {
																	toast.error($i18n.t('Please select a model first.'));
																}

																const res = await generateAutoCompletion(
																	localStorage.token,
																	selectedModelIds.at(0),
																	text,
																	history?.currentId
																		? createMessagesList(history, history.currentId)
																		: null
																).catch((error) => {
																	console.log(error);

																	return null;
																});

																console.log(res);
																return res;
															}}
															{suggestions}
															oncompositionstart={() => (isComposing = true)}
															oncompositionend={(e) => {
																compositionEndedAt = e.timeStamp;
																isComposing = false;
															}}
															onkeydown={async (e) => {
																await handleComposerKeydown(e.detail.event);
															}}
															onpaste={async (e) => {
																await handleComposerPaste(e.detail.event);
															}}
														/>
													{/if}
												{/key}
											{/key}
										{/if}
									</div>
								</div>

						<!-- kb-row: tighter margins while the keyboard is up. The controls
							     themselves stay — attach/web-search/effort/dictate are things
							     people genuinely reach for mid-typing. -->
								<div class="kb-row flex justify-between mb-2.5 mx-0.5 max-w-full" dir="ltr">
									<!-- One line, always, and it scrolls: with the model's effort +
									     service-tier pills in play the controls run ~370px wide in a ~250px
									     row on a phone. What made the hidden third UNREACHABLE was not the
									     row being one line, it was that (a) a horizontal thumb-drag could be
									     cancelled outright by the typing-mode drag containment misreading
									     its first 1-2px sample as vertical (fixed in keyboardViewport.ts)
									     and (b) nothing said there was anything to scroll TO — the pills
									     just stopped mid-word behind the mic. Hence data-fade: the edge the
									     content continues past is masked to transparent, so the row reads as
									     scrollable and its clipping reads as deliberate.
									     No touch-action here: overflow-y-hidden already keeps the .tap-target
									     ::after overhangs from making the row scrollable vertically, and an
									     axis lock is one more thing that can decide a horizontal drag is not
									     for this element. -->
									<div
										bind:this={toolbarRailElement}
										onscroll={updateToolbarFade}
										data-fade={toolbarFade}
										class="toolbar-rail ml-1 self-end flex items-center flex-1 max-w-[80%] flex-nowrap overflow-x-auto overflow-y-hidden scrollbar-none"
									>
										<InputMenu
											bind:files
											selectedModels={atSelectedModel ? [atSelectedModel.id] : selectedModels}
											{fileUploadCapableModels}
											{screenCaptureHandler}
											{inputFilesHandler}
											{videoSupported}
											addVideoHandler={() => {
												showVideoModal = true;
											}}
											uploadFilesHandler={() => {
												filesInputElement.click();
											}}
											uploadGoogleDriveHandler={async () => {
												try {
													const fileData = await createPicker();
													if (fileData) {
														const file = new File([fileData.blob], fileData.name, {
															type: fileData.blob.type
														});
														await uploadFileHandler(file);
													} else {
														console.log('No file was selected from Google Drive');
													}
												} catch (error) {
													console.error('Google Drive Error:', error);
													toast.error(
														$i18n.t('Error accessing Google Drive: {{error}}', {
															error: error.message
														})
													);
												}
											}}
											uploadOneDriveHandler={async (authorityType) => {
												try {
													const fileData = await pickAndDownloadFile(authorityType);
													if (fileData) {
														const file = new File([fileData.blob], fileData.name, {
															type: fileData.blob.type || 'application/octet-stream'
														});
														await uploadFileHandler(file);
													} else {
														console.log('No file was selected from OneDrive');
													}
												} catch (error) {
													console.error('OneDrive Error:', error);
												}
											}}
											onUpload={async (e) => {
												dispatch('upload', e);
											}}
											onClose={async () => {
												if (hasOnScreenKeyboard) return;
												await tick();

												const chatInput = document.getElementById('chat-input');
												chatInput?.focus({ preventScroll: true });
											}}
										>
											<div
												id="input-menu-button"
												class="tap-target bg-transparent hover:bg-gray-100 text-gray-700 dark:text-white dark:hover:bg-gray-800 rounded-full size-9 flex justify-center items-center outline-hidden focus:outline-hidden"
											>
												<PlusAlt className="size-6" />
											</div>
										</InputMenu>

										<div
											class="flex self-center w-[1px] h-5 mx-1 bg-gray-200/50 dark:bg-gray-800/50"
										></div>

										{#if showWebSearchButton}
											<ToolbarToggle
												active={webSearchEnabled || ($settings?.webSearch ?? false) === 'always'}
												tooltip={$i18n.t('Web Search')}
												onClick={() => {
													webSearchEnabled = !webSearchEnabled;
													onSelectionTouched();
												}}
											>
												<GlobeAlt className="size-5" strokeWidth="1.75" />
											</ToolbarToggle>
										{/if}

										{#if showContainerButton}
											<ToolbarToggle
												active={containerWorkspaceActive}
												tooltip={$i18n.t('Container')}
												ariaLabel={containerWorkspaceActive
													? $i18n.t('Disable Container')
													: $i18n.t('Enable Container')}
												onClick={() => {
													if (containerWorkspaceActive) {
														selectedToolIds = selectedToolIds.filter(
															(id) => id !== containerToolId
														);
													} else {
														selectedToolIds = [...(selectedToolIds ?? []), containerToolId];
													}
													onSelectionTouched();
												}}
											>
												<Terminal className="size-5" strokeWidth="1.75" />
											</ToolbarToggle>
										{/if}

										{#if showSubagentsButton}
											<ToolbarToggle
												active={subagentsEnabled}
												tooltip={$i18n.t(
													'Subagents — let the model spawn research workers in isolated contexts'
												)}
												ariaLabel={subagentsEnabled
													? $i18n.t('Disable Subagents')
													: $i18n.t('Enable Subagents')}
												onClick={() => {
													subagentsEnabled = !subagentsEnabled;
													onSelectionTouched();
												}}
											>
												<UserGroup className="size-5" strokeWidth="1.75" />
											</ToolbarToggle>

											{#if subagentsEnabled}
												<!-- Per-chat subagent overrides (model + reasoning
													effort + service tier + external tools) live inside
													a single popover next to the Subagents toggle, so the
													input bar stays uncluttered as more knobs get added. -->
												<SubagentSettings
													bind:subagentModel
													bind:subagentReasoningEffort
													bind:subagentServiceTier
													bind:subagentExternalToolsEnabled
													allowedServiceTiers={allowedSubagentServiceTiers}
													allowExternalTools={subagentExternalToolsAllowed}
													selectedExternalToolCount={selectedSubagentExternalToolIds.length}
													{containerWorkspaceActive}
													{onSelectionTouched}
												/>
											{/if}
										{/if}

										{#if showStudyModeButton || showDataVizButton || showAutomationsButton || showImageGenerationButton || showToolsButton || (toggleFilters && toggleFilters.length > 0)}
											<IntegrationsMenu
												{toggleFilters}
												{showStudyModeButton}
												{showDataVizButton}
												{showAutomationsButton}
												{showImageGenerationButton}
												{onSelectionTouched}
												bind:selectedToolIds
												bind:selectedFilterIds
												bind:studyModeEnabled
												bind:dataVizEnabled
												bind:automationsEnabled
												bind:imageGenerationEnabled
												closeOnOutsideClick={integrationsMenuCloseOnOutsideClick}
												onShowToolDetails={() => {
													showTools = true;
												}}
												onShowValves={(e) => {
													const { type, id } = e;
													selectedValvesType = type;
													selectedValvesItemId = id;
													showValvesModal = true;
													integrationsMenuCloseOnOutsideClick = false;
												}}
												onClose={async () => {
													if (hasOnScreenKeyboard) return;
													await tick();

													const chatInput = document.getElementById('chat-input');
													chatInput?.focus({ preventScroll: true });
												}}
											>
												<div
													id="integration-menu-button"
													class="tap-target flex justify-center items-center outline-hidden focus:outline-hidden transition-colors duration-300 {activeIntegrationCount >
													0
														? 'h-9 px-2.5 gap-1.5 rounded-full text-gray-900 dark:text-gray-100 bg-manilla/60 hover:bg-manilla/80 dark:bg-manilla-dark dark:hover:bg-manilla-dark/80 border-hairline border-book-cloth/30 dark:border-book-cloth/40'
														: 'size-9 rounded-full bg-transparent hover:bg-gray-100 text-gray-700 dark:text-white dark:hover:bg-gray-800'}"
												>
													<Component className="size-5" strokeWidth="1.75" />
													{#if activeIntegrationCount > 0}
														<span class="text-xs font-medium">{activeIntegrationCount}</span>
													{/if}
												</div>
											</IntegrationsMenu>
										{/if}

										{#if selectedModelIds.length === 1 && $models.find((m) => m.id === selectedModelIds[0])?.has_user_valves}
											<div class="ml-1 flex gap-1.5 shrink-0">
												<Tooltip content={$i18n.t('Valves')} placement="top">
													<button
														id="model-valves-button"
														class="tap-target bg-transparent hover:bg-gray-100 text-gray-700 dark:text-white dark:hover:bg-gray-800 rounded-full size-9 flex justify-center items-center outline-hidden focus:outline-hidden"
														onclick={() => {
															selectedValvesType = 'function';
															selectedValvesItemId = selectedModelIds[0]?.split('.')[0];
															showValvesModal = true;
														}}
													>
														<Knobs className="size-5" strokeWidth="1.75" />
													</button>
												</Tooltip>
											</div>
										{/if}

										<div class="ml-1 flex gap-1.5 shrink-0">
											{#if showReasoningEffortSelector}
												<!-- Reasoning Effort Selector -->
												<Tooltip content={'Reasoning Effort'} placement="top">
													<div class="relative flex items-center">
														<div
															class="group shrink-0 whitespace-nowrap p-2 max-md:p-2.5 flex gap-1.5 items-center text-sm rounded-full transition-colors duration-300 focus:outline-hidden max-w-full overflow-hidden bg-transparent text-gray-600 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-800 border-hairline border-transparent hover:border-gray-200 dark:hover:border-gray-700"
														>
															<svg
																xmlns="http://www.w3.org/2000/svg"
																viewBox="0 0 24 24"
																fill="none"
																stroke="currentColor"
																stroke-width="1.75"
																stroke-linecap="round"
																stroke-linejoin="round"
																class="size-5"
															>
																<path
																	d="M9.663 17h4.673M12 3v1m6.364 1.636l-.707.707M21 12h-1M4 12H3m3.343-5.657l-.707-.707m2.828 9.9a5 5 0 117.072 0l-.548.547A3.374 3.374 0 0014 18.469V19a2 2 0 11-4 0v-.531c0-.895-.356-1.754-.988-2.386l-.548-.547z"
																/>
															</svg>
															<span class="text-xs font-medium whitespace-nowrap"
																>{reasoningEffort}</span
															>

															<select
																bind:value={reasoningEffort}
																onchange={handleReasoningEffortChange}
																class="absolute inset-0 w-full h-full opacity-0 cursor-pointer"
															>
																{#each allowedReasoningEffortsForCurrentModel as effort}
																	<option value={effort}>{effort}</option>
																{/each}
															</select>
														</div>
													</div>
												</Tooltip>
											{/if}

											{#if showServiceTierSelector}
												<!-- Service Tier Selector -->
												<Tooltip content={'Service Tier'} placement="top">
													<div class="relative flex items-center">
														<div
															class="group shrink-0 whitespace-nowrap p-2 max-md:p-2.5 flex gap-1.5 items-center text-sm rounded-full transition-colors duration-300 focus:outline-hidden max-w-full overflow-hidden bg-transparent text-gray-600 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-800 border-hairline border-transparent hover:border-gray-200 dark:hover:border-gray-700"
														>
															<svg
																xmlns="http://www.w3.org/2000/svg"
																viewBox="0 0 24 24"
																fill="none"
																stroke="currentColor"
																stroke-width="1.75"
																stroke-linecap="round"
																stroke-linejoin="round"
																class="size-5"
															>
																<path d="m12 14 4-4" />
																<path d="M3.34 19a10 10 0 1 1 17.32 0" />
															</svg>
															<!-- Tier colors: flex = book-cloth/kraft (terracotta orange),
															     priority = success green. Priority used to be `warning`
															     (#A8783E), nearly the same warm amber as flex's #CC785C —
															     indistinguishable at pill size. These same hues drive the
															     composer border tint below. -->
															<span
																class="text-xs font-medium whitespace-nowrap {serviceTier ===
																'priority'
																	? 'text-success dark:text-success-dark'
																	: serviceTier === 'flex'
																		? 'text-book-cloth dark:text-kraft'
																		: ''}">{serviceTier}</span
															>

															<select
																value={serviceTier}
																onchange={(e) => {
																	const tier = e.target.value;
																	serviceTier = tier;
																	onServiceTierTouched(tier);
																	if (!hasOnScreenKeyboard) {
																		tick().then(() =>
																			document
																				.getElementById('chat-input')
																				?.focus({ preventScroll: true })
																		);
																	}
																}}
																class="absolute inset-0 w-full h-full opacity-0 cursor-pointer"
															>
																{#each allowedServiceTiers as tier}
																	<option value={tier}>{tier}</option>
																{/each}
															</select>
														</div>
													</div>
												</Tooltip>
											{/if}
										</div>
									</div>

									<div class="self-end flex space-x-1 mr-1 shrink-0">
										{#if (!history?.currentId || history.messages[history.currentId]?.done == true) && ($_user?.role === 'admin' || ($_user?.permissions?.chat?.stt ?? true))}
											<!-- {$i18n.t('Record voice')} -->
											<Tooltip content={$i18n.t('Dictate')}>
												<button
													id="voice-input-button"
													class=" text-gray-600 dark:text-gray-300 hover:text-gray-700 dark:hover:text-gray-200 transition rounded-full p-1.5 max-md:p-2.5 mr-0.5 self-center"
													type="button"
													onclick={async () => {
														try {
															let stream = await navigator.mediaDevices
																.getUserMedia({ audio: true })
																.catch(function (err) {
																	toast.error(
																		$i18n.t(
																			`Permission denied when accessing microphone: {{error}}`,
																			{
																				error: err
																			}
																		)
																	);
																	return null;
																});

															if (stream) {
																recording = true;
																const tracks = stream.getTracks();
																tracks.forEach((track) => track.stop());
															}
															stream = null;
														} catch {
															toast.error($i18n.t('Permission denied when accessing microphone'));
														}
													}}
													aria-label="Voice Input"
												>
													<svg
														xmlns="http://www.w3.org/2000/svg"
														viewBox="0 0 20 20"
														fill="currentColor"
														class="size-5"
													>
														<path d="M7 4a3 3 0 016 0v6a3 3 0 11-6 0V4z" />
														<path
															d="M5.5 9.643a.75.75 0 00-1.5 0V10c0 3.06 2.29 5.585 5.25 5.954V17.5h-1.5a.75.75 0 000 1.5h4.5a.75.75 0 000-1.5h-1.5v-1.546A6.001 6.001 0 0016 10v-.357a.75.75 0 00-1.5 0V10a4.5 4.5 0 01-9 0v-.357z"
														/>
													</svg>
												</button>
											</Tooltip>
										{/if}

										{#if turnLive}
											<div class=" flex items-center gap-1">
												{#if !sendDisabled}
													<!-- Steer button: while a response is streaming, pressing
													     Enter or clicking here injects the typed message at the
													     next tool-call boundary (same as bare Enter). Alt+Enter
													     still queues for after the response finishes. Only
													     visible when the user has something to send. -->
													<Tooltip
														content={hasInFlightFiles
															? $i18n.t('Send when uploads finish')
															: $i18n.t('Steer (Alt+Enter queues instead)')}
													>
														<button
															id="steer-message-button"
															class="bg-book-cloth hover:bg-kraft text-white transition-colors duration-200 ease-paper rounded-full p-1.5 max-md:p-2.5"
															type="submit"
															aria-label={hasInFlightFiles
																? $i18n.t('Send when uploads finish')
																: $i18n.t('Steer message')}
														>
															<svg
																xmlns="http://www.w3.org/2000/svg"
																viewBox="0 0 20 20"
																fill="currentColor"
																class="size-5"
																aria-hidden="true"
															>
																<path
																	fill-rule="evenodd"
																	d="M11.983 1.907a.75.75 0 00-1.292-.657l-8.5 9.5A.75.75 0 002.75 12h4.116l-.99 6.093a.75.75 0 001.292.657l8.5-9.5A.75.75 0 0015.25 8h-4.116l.849-6.093z"
																	clip-rule="evenodd"
																/>
															</svg>
														</button>
													</Tooltip>
												{/if}
												<Tooltip content={$i18n.t('Stop')}>
													<button
														id="stop-response-button"
														aria-label={$i18n.t('Stop')}
														class="bg-white hover:bg-gray-100 text-gray-800 dark:bg-gray-700 dark:text-white dark:hover:bg-gray-800 transition-colors duration-200 ease-paper rounded-full p-1.5 max-md:p-2.5 border-hairline border-gray-300 dark:border-gray-700"
														type="button"
														onclick={() => {
															stopResponse();
														}}
													>
														<svg
															xmlns="http://www.w3.org/2000/svg"
															viewBox="0 0 24 24"
															fill="currentColor"
															class="size-5"
														>
															<path
																fill-rule="evenodd"
																d="M2.25 12c0-5.385 4.365-9.75 9.75-9.75s9.75 4.365 9.75 9.75-4.365 9.75-9.75 9.75S2.25 17.385 2.25 12zm6-2.438c0-.724.588-1.312 1.313-1.312h4.874c.725 0 1.313.588 1.313 1.313v4.874c0 .725-.588 1.313-1.313 1.313H9.564a1.312 1.312 0 01-1.313-1.313V9.564z"
																clip-rule="evenodd"
															/>
														</svg>
													</button>
												</Tooltip>
											</div>
										{:else if prompt === '' && files.length === 0 && ($_user?.role === 'admin' || ($_user?.permissions?.chat?.call ?? true))}
											<div class=" flex items-center">
												<!-- {$i18n.t('Call')} -->
												<Tooltip content={$i18n.t('Voice mode')}>
													<button
														class=" bg-gray-900 text-white hover:bg-gray-800 dark:bg-white dark:text-gray-900 dark:hover:bg-gray-100 transition-colors duration-200 ease-paper rounded-full p-1.5 max-md:p-2.5 self-center"
														type="button"
														onclick={async () => {
															if (selectedModels.length > 1) {
																toast.error($i18n.t('Select only one model to call'));

																return;
															}

															if ($config?.audio?.stt?.engine === 'web') {
																toast.error(
																	$i18n.t('Call feature is not supported when using Web STT engine')
																);

																return;
															}
															// check if user has access to getUserMedia
															try {
																let stream = await navigator.mediaDevices.getUserMedia({
																	audio: true
																});
																// If the user grants the permission, proceed to show the call overlay

																if (stream) {
																	const tracks = stream.getTracks();
																	tracks.forEach((track) => track.stop());
																}

																stream = null;

																if ($settings.audio?.tts?.engine === 'browser-kokoro') {
																	// If the user has not initialized the TTS worker, initialize it
																	if (!$TTSWorker) {
																		await TTSWorker.set(
																			new KokoroWorker({
																				dtype: $settings.audio?.tts?.engineConfig?.dtype ?? 'fp32'
																			})
																		);

																		await $TTSWorker.init();
																	}
																}

																showCallOverlay.set(true);
																showControls.set(true);
															} catch (err) {
																// If the user denies the permission or an error occurs, show an error message
																toast.error(
																	$i18n.t('Permission denied when accessing media devices')
																);
															}
														}}
														aria-label={$i18n.t('Voice mode')}
													>
														<Voice className="size-5" strokeWidth="2.5" />
													</button>
												</Tooltip>
											</div>
										{:else}
											<div class=" flex items-center">
												<Tooltip content={$i18n.t('Send message')}>
													<button
														id="send-message-button"
														class="{!sendDisabled
															? 'bg-book-cloth text-white hover:bg-kraft'
															: 'text-white/70 bg-gray-200 dark:bg-gray-800 dark:text-gray-500 disabled'} transition-colors duration-200 ease-paper rounded-full p-1.5 max-md:p-2.5 self-center"
														type="submit"
														disabled={sendDisabled}
													>
														<svg
															xmlns="http://www.w3.org/2000/svg"
															viewBox="0 0 16 16"
															fill="currentColor"
															class="size-5"
														>
															<path
																fill-rule="evenodd"
																d="M8 14a.75.75 0 0 1-.75-.75V4.56L4.03 7.78a.75.75 0 0 1-1.06-1.06l4.5-4.5a.75.75 0 0 1 1.06 0l4.5 4.5a.75.75 0 0 1-1.06 1.06L8.75 4.56v8.69A.75.75 0 0 1 8 14Z"
																clip-rule="evenodd"
															/>
														</svg>
													</button>
												</Tooltip>
											</div>
										{/if}
									</div>
								</div>
							</div>

							<!-- No {:else} spacer here. The empty <div class="mb-1"> that stood
							     in for a missing license footer was not free: as a second form
							     child it also activated the form's gap-1.5, so instances with no
							     footer (the default) carried 10px of dead band under the composer
							     in EVERY state — including typing mode, where .pb-composer is
							     deliberately zeroed so the box can sit flush on the keyboard.
							     With a single child the gap collapses and the bubble's border is
							     the form's bottom edge. -->
							{#if $config?.license_metadata?.input_footer}
								<div class=" text-xs text-gray-500 text-center line-clamp-1 marked">
									{@html DOMPurify.sanitize(marked($config?.license_metadata?.input_footer))}
								</div>
							{/if}
						</form>
					{/if}
				</div>
			</div>
		</div>
	</div>
{/if}
