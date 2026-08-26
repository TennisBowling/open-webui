<script lang="ts">
	import { passive } from '$lib/utils/eventModifiers';

	import { v4 as uuidv4 } from 'uuid';
	import dayjs from 'dayjs';
	import { toast } from '$lib/utils/toast';
	import { PaneGroup, Pane, PaneResizer } from 'paneforge';

	import { getContext, onDestroy, onMount, tick, untrack } from 'svelte';
	const i18n: Writable<i18nType> = getContext('i18n');
	const cloneState = <T,>(value: T): T => $state.snapshot(value) as T;

	import { goto } from '$app/navigation';
	import { page } from '$app/stores';

	import { get, type Unsubscriber, type Writable } from 'svelte/store';
	import type { i18n as i18nType } from 'i18next';
	import { WEBUI_BASE_URL, WEBUI_API_BASE_URL } from '$lib/constants';

	import {
		chatId,
		chats,
		config,
		type Model,
		models,
		tags as allTags,
		settings,
		showSidebar,
		WEBUI_NAME,
		banners,
		user,
		socket,
		online,
		showControls,
		showCallOverlay,
		showFilePreview,
		previewFile,
		openFilePreview,
		temporaryChatEnabled,
		mobile,
		showOverview,
		chatTitle,
		chatFreshness,
		showArtifacts,
		tools,
		toolServers,
		functions,
		selectedFolder,
		pinnedChats,
		showEmbeds,
		chatTokenStats,
		chatTokenStatsRefreshTrigger,
		subagentLiveStates,
		questionStates,
		reasoningBlockOpenState,
		messageEditingIds,
		browserLiveStates,
		showBrowserPanel,
		browserPanelDismissed,
		isLastActiveTab,
		folderChatListInvalidation,
		tokenUsageGroups as tokenUsageGroupsStore,
		subscriptionUsage as subscriptionUsageStore,
		bumpMessageRevision,
		clearMessageRevisionStores
	} from '$lib/stores';
	import {
		formatSubscriptionLimitLabel,
		formatWindowLabel,
		formatUsedPercent,
		formatResetsIn
	} from '$lib/utils/subscriptionUsage';
	import {
		convertMessagesToHistory,
		copyToClipboard,
		getMessageContentParts,
		createMessagesList,
		getPromptVariables,
		processDetails,
		removeAllDetails,
		getTimeRange
	} from '$lib/utils';
	import { loadToolServers } from '$lib/utils/toolServers';
	import { readAsyncTaskResponse } from '$lib/utils/asyncTaskResponse';
	import {
		closeOpenAgenticBlocks,
		getChatGenerationErrorCode,
		inactiveAssistantTerminalPatch,
		isNonRetryableChatGenerationError,
		resolveLoadedModelIds,
		snapshotTurnModelIds,
		wasGenerationStartStopped
	} from '$lib/utils/chatTurn';
	import { pendingLazyBodyCount } from '$lib/utils/lazyBlockBodies';
	import {
		ChatGenerationLifecycleRegistry,
		type ServerGenerationOperation
	} from '$lib/utils/chatGenerationLifecycle';
	import {
		getStructuredRetryLastRequestContext,
		getRewindContext,
		getSubagentToolCallCutIndex,
		canBatchSubagentToolCallCuts,
		getRetryableToolContext,
		countCompletedToolCalls,
		countCompletedStructuredToolCalls,
		shouldContinueFromLastToolRequest,
		getStringMessageContent,
		hasMessageContent,
		expandMessagesForToolResumption
	} from '$lib/utils/retryLastRequest';
	import {
		hydrateToolResultsInBlocks,
		mergeToolResultEntries,
		normalizeToolResultEntry
	} from '$lib/utils/toolResults';
	import {
		applyDeltaOp,
		decideSnapshotAdoption,
		mergeReasoningDetail,
		normalizeStreamingContentBlocks,
		bumpStreamingBlockRevision,
		compactStreamOps,
		decodeCompactStreamPayload
	} from '$lib/utils/stream-protocol';
	import { streamPerfCount, streamPerfEnd, streamPerfStart } from '$lib/utils/streamPerf';
	import { createMessageHeightSweeper } from '$lib/utils/messageHeights';
	import {
		activeSubagentRerunEntryKeys,
		activeSubagentStreamMessageIds,
		compareSubagentRerunGeneration,
		findSubagentRunEntry,
		hasActiveDetachedSubagentRerun,
		isDetachedSubagentRerun,
		isFreshRerunResult,
		seedPersistedSubagentRuns,
		setSubagentRunAliases,
		shouldApplyIncomingSubagentGeneration,
		shouldParentFinalizeSubagentRun,
		shouldApplyRerunOptimisticState,
		subagentRunHasActiveRerunKey,
		subagentScopedStateKey
	} from '$lib/utils/subagentState';

	import {
		createNewChat,
		getAllTags,
		getChatByIdTail,
		getChatMessageToolResult,
		getChatMessagesBranch,
		getChatMessagesSiblings,
		getTagsById,
		updateChatFolderIdById,
		patchChat,
		drainChatQueue,
		getChatMeta,
		compactChat,
		browserLiveFrame,
		type PatchChatOp
	} from '$lib/apis/chats';
	import { findDeepestBranchLeaf } from '$lib/utils/chatHistoryGraph';
	import { decorate, upsertSorted } from '$lib/utils/sidebarSync';
	import { getChat as getOfflineChat } from '$lib/offline/chatStore';
	import { isOnScreenKeyboardDevice } from '$lib/utils/device';
	import {
		saveOfflineChatSnapshot,
		handleLocalChatSaved,
		removeOfflineChat
	} from '$lib/offline/manager';
	import { chatCompletion } from '$lib/apis/openai';
	import { processWeb, processWebSearch, processYoutubeVideo } from '$lib/apis/retrieval';
	import { getAndUpdateUserLocation } from '$lib/apis/users';
	import {
		generateQueries,
		chatAction,
		generateMoACompletion,
		stopChatGenerations,
		getChatWorkState,
		getActiveStreamsByChatId,
		getStreamSnapshot,
		getStreamDeltas,
		getBrowserFrame
	} from '$lib/apis';
	import type { ReasoningEffort } from '$lib/apis';
	import {
		REASONING_EFFORT_ORDER,
		getEffectiveReasoning,
		clampEffortToEffective
	} from '$lib/constants/reasoning';
	import { getTools } from '$lib/apis/tools';
	import { uploadFile, getFileContentById } from '$lib/apis/files';
	import { createOpenAITextStream } from '$lib/apis/streaming';
	import {
		rewindAdoptSubagentResults,
		rewindSubagentsForRerun,
		rerunSubagent
	} from '$lib/apis/subagents';

	import { fade } from 'svelte/transition';

	import MessageInput from '$lib/components/chat/MessageInput.svelte';
	import Messages from '$lib/components/chat/Messages.svelte';
	import Navbar from '$lib/components/chat/Navbar.svelte';
	import ChatControls from './ChatControls.svelte';
	import EventConfirmDialog from '../common/ConfirmDialog.svelte';
	import Placeholder from './Placeholder.svelte';
	import Sidebar from '../icons/Sidebar.svelte';
	import ChevronDown from '../icons/ChevronDown.svelte';
	import { getFunctions } from '$lib/apis/functions';
	import Image from '../common/Image.svelte';
	import { updateFolderById } from '$lib/apis/folders';
	import { dispatchWidgetRender } from '$lib/utils/dataVizRegistry';

	// Local-first open bundle from the route loader: SEPARATE promises for the
	// local IDB copy (paints immediately) and the network body (revalidates it).
	// Task/stream state arrives inside the body response (__active) or is

	interface Props {
		chatIdProp?: string;
		preloadedData?: any;
		// proven absent by a 304.
		preloaded?: {
			chatId: string;
			localEntryPromise: Promise<any> | null;
			chatPromise: Promise<any>;
		} | null;
	}

	let {
		chatIdProp = '',
		preloadedData = $bindable(null),
		preloaded = $bindable(null)
	}: Props = $props();

	let loading = $state(true);
	// True while a provisional (local-copy) open is awaiting its network
	// revalidation. Gates the writers that must not act on possibly-stale state
	// (send, model persistence) — reads/render are free to use the stale view.
	let chatRevalidating = false;
	let chatRevalidationPromise: Promise<void> | null = null;
	// Is the RENDERED view confirmed current? This drives the navbar sync mark
	// and is deliberately anchored to DATA truth points, not process lifetimes
	// (the first cut keyed the mark to "reconcile function still running",
	// which made it lag: content popped in at history-commit while trailing
	// bookkeeping held "Syncing…" open, and on phone-wake nothing showed until
	// the socket happened to reconnect).
	//   → unverified: provisional (local-copy) paint, socket DISCONNECT (the
	//     staleness window starts when the link dies, not when it returns),
	//     reconnect reconcile start.
	//   → verified: a network-sourced body COMMITS to history (the exact
	//     moment new content appears), a revalidation/reconcile confirms
	//     "unchanged", or the reconnect reconcile settles without a reload.
	let chatViewUnverified = $state(false);

	let initialScrollSettled = $state(false);
	// Gates the reveal of the messages content on navigation. Defaults to true so
	// new/temporary chats (where navigateHandler never runs) are always visible;
	// navigateHandler flips it false only while it pins an existing chat to the
	// bottom during initial load, then reveals it already-at-bottom.
	let messagesReady = $state(true);
	let navigateGeneration = 0; // Incremented on each navigation; stale loadChat calls abort before touching state
	// True once navigateHandler has completed its FIRST run on this component
	// instance (cold load / deep link / hard reload); subsequent runs are soft
	// in-app navigations. The local-first tier serves on BOTH — the flag now
	// only informs per-nav resets that care about the distinction.
	let hasCompletedFirstNavigate = false;

	const eventTarget = new EventTarget();
	let controlPane = $state();
	let controlPaneComponent = $state();

	let messageInput = $state.raw(null);

	let autoScroll = $state(true);
	let processing = '';
	let messagesContainerElement: HTMLDivElement = $state();
	let messagesContentElement: HTMLDivElement = $state();

	let navbarElement = $state();

	let showEventConfirmation = $state(false);
	let eventConfirmationTitle = $state('');
	let eventConfirmationMessage = $state('');
	let eventConfirmationInput = $state(false);
	let eventConfirmationInputPlaceholder = $state('');
	let eventConfirmationInputValue = $state('');
	let eventCallback = $state(null);

	let chatIdUnsubscriber: Unsubscriber | undefined;

	let selectedModels = $state(['']);
	// Incremented only by an explicit picker action. Programmatic chat loads do
	// not touch it, which lets local-first revalidation distinguish newer user
	// intent from the provisional/server model state it is reconciling.
	let modelSelectionRevision = $state(0);
	let atSelectedModel: Model | undefined = $state();
	let selectedModelIds = $state([]);

	let selectedToolIds = $state([]);
	// Becomes true once the user has explicitly touched ANY tool/feature toggle
	// in this chat (or a saved chat is loaded with a non-default selection). While
	// dirty, switching models keeps the user's selection instead of clobbering it
	// with the new model's defaults.
	let toolSelectionDirty = $state(false);
	let selectedFilterIds = $state([]);
	let imageGenerationEnabled = $state(false);
	let webSearchEnabled = $state(false);
	let studyModeEnabled = $state(false);
	let dataVizEnabled = $state(false);
	let automationsEnabled = $state(false);
	let subagentsEnabled = $state(false);
	// Per-chat override of admin global SUBAGENT_DEFAULT_REASONING_EFFORT.
	// Empty string = inherit the admin default. Otherwise: minimal / low /
	// medium / high / xhigh (or any provider-specific value).
	let subagentReasoningEffort: string = $state('');
	// Empty string = inherit the admin SUBAGENT_DEFAULT_SERVICE_TIER (which
	// itself may be empty, in which case no service_tier field is sent).
	// Otherwise: any string the provider accepts (`default` / `flex` /
	// `priority` for OpenAI; provider-specific values otherwise).
	let subagentServiceTier: string = $state('');
	// Per-chat override of the resolved subagent model (admin
	// SUBAGENT_DEFAULT_MODEL, else the parent chat's model). Empty = inherit.
	// Persisted to chat.params.subagentModel; backend reads it in
	// `_resolve_subagent_model_id`.
	let subagentModel: string = $state('');
	// Per-chat opt-in for inheriting selected admin external tool servers
	// (including the configured container MCP server) into subagent runs.
	let subagentExternalToolsEnabled = $state(true);

	const sameStringArray = (a = [], b = []) =>
		Array.isArray(a) &&
		Array.isArray(b) &&
		a.length === b.length &&
		a.every((value, idx) => value === b[idx]);

	// Comparators / value normalizers for the per-chat param toggles below.
	const scalarParamsEqual = (a: any, b: any) => a === b;
	const identityParamClone = (v: any) => v;
	const cloneStringArrayParam = (v: any) => [...(v ?? [])];

	// ─── Per-chat toolbar state ──────────────────────────────────────────────
	//
	// Every composer toggle (tools, web search, study mode, data viz, subagents
	// and its overrides) is chat-scoped state with the same three duties:
	// restore it when the chat loads, mirror it into `params`, and PATCH it
	// durably. Those duties used to be nine hand-written copies each, spread
	// over four places — which is how a toggle could be silently thrown away:
	// `loadChat()` unconditionally re-applied the server's copy of `params`, so
	// any toggle whose PATCH hadn't confirmed yet (a second or two on a weak
	// link, and there are a dozen things that trigger a reload — a stopped
	// generation, a completion, a reconnect, a queue drain) was reverted before
	// it ever reached the server. Enabling a tool mid-chat and watching it turn
	// itself back off was exactly this.
	//
	// One table now owns all three duties, plus the write-tracking that makes
	// the restore safe: a key with an unconfirmed local write is NEVER
	// overwritten by a reload, and a write that fails is retried instead of
	// being lost.
	type ChatParamBinding = {
		key: string;
		read: () => any;
		write: (value: any) => void;
		/**
		 * What an ABSENT `params[key]` means, for the in-memory mirror's diff.
		 * (Restoring an absent key is a no-op — see restoreChatParams.)
		 */
		fallback: any;
		equals: (a: any, b: any) => boolean;
		clone: (value: any) => any;
		/**
		 * Optional veto on RESTORE only: a saved value the currently selected
		 * model can't honour (e.g. web search on a model without the capability)
		 * is dropped rather than restored.
		 */
		canRestore?: (value: any) => boolean;
	};

	const chatParamBindings: ChatParamBinding[] = [
		{
			key: 'selectedToolIds',
			read: () => selectedToolIds,
			write: (v) => (selectedToolIds = v),
			fallback: [],
			equals: sameStringArray,
			clone: cloneStringArrayParam
		},
		{
			key: 'webSearchEnabled',
			read: () => webSearchEnabled,
			write: (v) => (webSearchEnabled = v),
			fallback: false,
			equals: scalarParamsEqual,
			clone: identityParamClone,
			canRestore: () =>
				(atSelectedModel ?? $models.find((m) => m.id === selectedModels[0]))?.info?.meta
					?.capabilities?.web_search ?? true
		},
		{
			key: 'studyModeEnabled',
			read: () => studyModeEnabled,
			write: (v) => (studyModeEnabled = v),
			fallback: false,
			equals: scalarParamsEqual,
			clone: identityParamClone
		},
		{
			key: 'dataVizEnabled',
			read: () => dataVizEnabled,
			write: (v) => (dataVizEnabled = v),
			fallback: false,
			equals: scalarParamsEqual,
			clone: identityParamClone
		},
		{
			key: 'automationsEnabled',
			read: () => automationsEnabled,
			write: (v) => (automationsEnabled = v),
			fallback: false,
			equals: scalarParamsEqual,
			clone: identityParamClone
		},
		{
			key: 'subagentsEnabled',
			read: () => subagentsEnabled,
			write: (v) => (subagentsEnabled = v),
			fallback: false,
			equals: scalarParamsEqual,
			clone: identityParamClone
		},
		{
			key: 'subagentReasoningEffort',
			read: () => subagentReasoningEffort,
			write: (v) => (subagentReasoningEffort = v ?? ''),
			fallback: '',
			equals: scalarParamsEqual,
			clone: identityParamClone
		},
		{
			key: 'subagentServiceTier',
			read: () => subagentServiceTier,
			write: (v) => (subagentServiceTier = v ?? ''),
			fallback: '',
			equals: scalarParamsEqual,
			clone: identityParamClone
		},
		{
			key: 'subagentModel',
			read: () => subagentModel,
			write: (v) => (subagentModel = v ?? ''),
			fallback: '',
			equals: scalarParamsEqual,
			clone: identityParamClone
		},
		{
			key: 'subagentExternalToolsEnabled',
			read: () => subagentExternalToolsEnabled,
			write: (v) => (subagentExternalToolsEnabled = !!v),
			fallback: true,
			equals: scalarParamsEqual,
			clone: identityParamClone
		}
	];

	// Last value the SERVER confirmed for each key in the current chat. `undefined`
	// means "not loaded yet" and gates the first persist, so opening a chat can
	// never PATCH the defaults back over the saved selection.
	let chatParamPersisted: Record<string, any> = $state({});
	// Keys whose local value has not been confirmed by the server: a PATCH is in
	// flight, or one failed and hasn't been retried yet. A reload must leave these
	// alone — the local value is newer than anything the server can return.
	let chatParamUnconfirmed = $state(new Set<string>());
	// Bumped after a failed PATCH so the sync effect re-runs and retries.
	let chatParamRetryTick = $state(0);
	const chatParamRetryTimers = new Map<string, ReturnType<typeof setTimeout>>();
	const chatParamRetryCounts = new Map<string, number>();
	const CHAT_PARAM_MAX_RETRIES = 4;

	const markChatParamUnconfirmed = (key: string, unconfirmed: boolean) => {
		const next = new Set(chatParamUnconfirmed);
		if (unconfirmed) next.add(key);
		else next.delete(key);
		chatParamUnconfirmed = next;
	};

	/** Forget every chat-scoped tracking record — called when the visible chat changes. */
	const resetChatParamTracking = () => {
		for (const timer of chatParamRetryTimers.values()) clearTimeout(timer);
		chatParamRetryTimers.clear();
		chatParamRetryCounts.clear();
		chatParamPersisted = {};
		chatParamUnconfirmed = new Set<string>();
	};

	/**
	 * Seed the persisted baseline from the `params` blob a freshly CREATED chat
	 * was stored with. Read from `params` (what the server actually received),
	 * not from the live toggles: if a toggle changed after that snapshot was
	 * taken, the difference is real and the sync effect should PATCH it.
	 */
	const markChatParamsPersisted = () => {
		const next: Record<string, any> = {};
		for (const binding of chatParamBindings) {
			next[binding.key] = binding.clone(params?.[binding.key] ?? binding.fallback);
		}
		chatParamPersisted = next;
	};

	/**
	 * Apply a loaded chat's saved `params` to the toolbar.
	 *
	 * A key the chat has NO saved entry for keeps whatever the live value is —
	 * that's the model's "Default Tools & Features" selection, applied by
	 * setDefaults(), and a chat saved before the key existed must not erase it.
	 * A key with an unconfirmed local write is skipped entirely, so a reload
	 * racing the user's own toggle can't revert it. Either way the resulting
	 * LIVE value becomes the persisted baseline, so restoring never triggers a
	 * write-back of what we just read.
	 */
	const restoreChatParams = (savedParams: any) => {
		const nextPersisted: Record<string, any> = { ...chatParamPersisted };
		for (const binding of chatParamBindings) {
			if (chatParamUnconfirmed.has(binding.key)) continue;
			const saved = savedParams?.[binding.key];
			if (saved !== undefined && (!binding.canRestore || binding.canRestore(saved))) {
				binding.write(binding.clone(saved));
			}
			nextPersisted[binding.key] = binding.clone(binding.read());
		}
		chatParamPersisted = nextPersisted;
	};

	const persistChatParam = async (binding: ChatParamBinding, chatIdToPersist: string) => {
		const key = binding.key;
		const value = binding.clone(binding.read());
		clearTimeout(chatParamRetryTimers.get(key));
		chatParamRetryTimers.delete(key);
		markChatParamUnconfirmed(key, true);
		try {
			await saveChatHandler(chatIdToPersist, history, { ...params, [key]: value }, [
				{ op: 'set_param', key, value }
			]);
			// Only a confirmed write updates the shadow. Recording it optimistically
			// is what turned a dropped PATCH into permanent data loss: the local and
			// "persisted" values agreed, so nothing ever retried, and the next reload
			// served the stale server copy.
			chatParamPersisted = { ...chatParamPersisted, [key]: value };
			chatParamRetryCounts.delete(key);
			markChatParamUnconfirmed(key, false);
		} catch (error) {
			console.error(`Failed to persist chat param ${key}`, error);
			const attempts = (chatParamRetryCounts.get(key) ?? 0) + 1;
			chatParamRetryCounts.set(key, attempts);
			if (attempts <= CHAT_PARAM_MAX_RETRIES) {
				chatParamRetryTimers.set(
					key,
					setTimeout(
						() => {
							chatParamRetryTimers.delete(key);
							// Re-arm the sync effect; it re-diffs and retries this key.
							markChatParamUnconfirmed(key, false);
							chatParamRetryTick += 1;
						},
						Math.min(2000 * 2 ** (attempts - 1), 30000)
					)
				);
			}
			// Stay unconfirmed either way, so a reload keeps the user's value.
		}
	};

	let showCommands = $state(false);

	const generationLifecycles = new ChatGenerationLifecycleRegistry();
	// Number of send/retry loops currently driving a turn CLIENT-SIDE (between
	// attempts no backend task exists yet). The reconnect handler's "zero
	// active tasks ⇒ the task finished while we were away ⇒ teardown+reload"
	// deduction is WRONG while one of these is live: it used to flip
	// `generating` false (killing the retry countdown) and loadChat() then
	// replaced history with a server copy that never saw the turn — the
	// "empty finished response" / vanished-send failure. While this is > 0
	// the retry loop owns convergence; reconnect must stand down.
	let activeSendRetryLoops = $state(0);

	// ─── "Is this chat generating?" ──────────────────────────────────────────
	//
	// DERIVED, never assigned. This used to be a plain boolean written from
	// twenty-two places — every send path, every socket terminal handler, the
	// reconnect handler, the resume poller, both retry loops, Stop, chat
	// switches. Any one path that failed to run left it latched, and a latched
	// `true` is what kept the composer showing Stop long after the answer had
	// finished rendering. There is now exactly one definition of the truth:
	//
	//   the lifecycle registry holds an unsettled record for this chat,
	//   or a client-side retry loop is between attempts.
	//
	// Every path that starts or ends work does so by mutating the registry
	// (begin / observe / markAccepted / retry / stop / terminal) or by
	// reconciling it against the server's work state — and the UI follows for
	// free. `generationRevision` is the registry's change signal; without it a
	// plain method call on a non-reactive class would never re-run this.
	let generationRevision = $state(0);
	generationLifecycles.subscribe(() => {
		generationRevision += 1;
	});
	let generating = $derived.by(() => {
		void generationRevision;
		const visibleChatId = activeChatId;
		if (visibleChatId && generationLifecycles.activeForChat(visibleChatId).length > 0) return true;
		// A retry loop between attempts owns the turn even though no record is in
		// flight for the instant of the countdown. Stop always wins over it.
		return activeSendRetryLoops > 0 && !userInitiatedStop;
	});
	let suppressErrorToast = false;
	// ─── THIS TAB's Stop intent ──────────────────────────────────────────────
	//
	// One value, because it answers two questions that must never disagree:
	//   · latched  — the user pressed Stop here and no new generation has started
	//                since. Gates the queue drain, tells the stream-side aborted
	//                handler that this was a user Stop and not a stray abort, and
	//                suppresses the retry-loop term of `generating`.
	//   · recent   — the Stop landed within STOP_RACE_WINDOW_MS, so work that
	//                shows up NOW was almost certainly already in flight when the
	//                user hit it, and should be halted too.
	// These were two variables (`userInitiatedStop` + `lastLocalStopAt`), and
	// only ONE of the two reset sites reset both — so a Stop could keep
	// suppressing drains for 3s after this tab had legitimately started a new
	// generation.
	const STOP_RACE_WINDOW_MS = 3000;
	let localStop: { at: number } | null = $state(null);
	let userInitiatedStop = $derived(localStop !== null);
	const stoppedHereRecently = () => !!localStop && Date.now() - localStop.at < STOP_RACE_WINDOW_MS;
	const stopResponsesInProgress = new Map<string, Promise<void>>();
	let branchReplacementPromise: Promise<boolean> | null = null;
	let lastPersistedSelectedModelsKey = '';

	const selectedModelsPersistKey = (
		chatId: string | null | undefined,
		modelIds = selectedModels
	) => (chatId ? `${chatId}:${JSON.stringify(modelIds ?? [])}` : '');

	const rememberPersistedSelectedModels = (
		chatId: string | null | undefined,
		modelIds = selectedModels
	) => {
		const key = selectedModelsPersistKey(chatId, modelIds);
		if (key) lastPersistedSelectedModelsKey = key;
	};

	/**
	 * Publish the terminal "the user stopped this turn" state for one assistant
	 * message. THE single definition of what stopped means, because it means
	 * three things at once and every bug in this area came from a call site
	 * expressing some of them and not the others:
	 *
	 *   1. the LIFECYCLE record is latched — aborts the in-flight controller,
	 *      refuses a retry re-arm, and drops the chat out of `generating` /
	 *      `taskIds`. Created on the spot if this tab never owned the turn.
	 *   2. the MESSAGE is finished and flagged — durable, so a reload, another
	 *      tab or another device sees the stop and the "Paused — Send now"
	 *      affordance survives.
	 *   3. any pending streamed-content flush is cancelled, so nothing lands
	 *      after the cancel.
	 *   4. every reasoning/tool_calls block left OPEN is closed, so the message
	 *      does not keep spinning "Thinking…" under a finished turn. This has to
	 *      happen here rather than being left to the backend's push: (2) sets
	 *      `userStopped`, and every inbound content handler drops events for a
	 *      user-stopped message — that guard is what stops late tokens landing
	 *      after a cancel, and it would swallow the close too.
	 *
	 * `maps` is EVERY message map that has to agree. The retry loops drive a
	 * detached `_history` clone alongside the live `history`; writing only one of
	 * them is what let a countdown tick republish `done:false` over the cancel
	 * and strand the message spinning forever.
	 */
	const markTurnStopped = (
		messageId: string | null | undefined,
		{ maps, chatId }: { maps?: Array<Record<string, any> | undefined | null>; chatId?: string } = {}
	) => {
		if (!messageId) return;
		const targetMaps = maps ?? [history?.messages];
		const row = targetMaps.map((map) => map?.[messageId]).find(Boolean);
		generationLifecycles.latchStopped({
			chatId: chatId || getVisibleChatId(),
			messageId,
			// Only a fallback — an existing record keeps its own identity.
			generationId: String(row?.generation_id ?? ''),
			turnId: String(row?.turn_id ?? ''),
			navigationGeneration: navigateGeneration
		});
		cancelStreamingMessageFlush(messageId);
		for (const map of targetMaps) {
			const target = map?.[messageId];
			if (!target) continue;
			target.done = true;
			target.userStopped = true;
			// `error` is deliberately preserved: if the last attempt really did
			// fail, the user is entitled to see why they were left with a partial
			// turn. The countdown already cleared it for the stop-mid-wait case.
			target.retrying = null;
			// Stop the dangling clock — see (4) above. Replacing the array is what
			// makes ContentRenderer re-project: its per-block signature cache is
			// keyed on `duration`, but the `$effect` that rebuilds it reads
			// `content_blocks` by reference.
			if (closeOpenAgenticBlocks(target)) {
				target.content_blocks = [...target.content_blocks];
			}
		}
	};

	/**
	 * Was this turn stopped by the user? Two terms, one per scope: the lifecycle
	 * latch is THIS tab's live answer (and survives `history` being replaced),
	 * the durable flag is everyone else's — another tab, another device, or this
	 * one after a reload.
	 */
	const isUserStoppedMessageId = (
		messageId: string | null | undefined,
		messages = history?.messages
	) =>
		!!messageId &&
		(generationLifecycles.isStopped(messageId) || messages?.[messageId]?.userStopped === true);

	const prepareGenerationLifecycle = (
		chatId: string,
		message: any,
		identity: { generationId?: string; turnId?: string } = {}
	) => {
		const generationId = identity.generationId ?? uuidv4();
		const turnId = identity.turnId ?? uuidv4();
		message.generation_id = generationId;
		message.turn_id = turnId;
		const { fresh } = generationLifecycles.begin({
			chatId,
			messageId: message.id,
			generationId,
			turnId,
			navigationGeneration: navigateGeneration
		});
		if (fresh) {
			// A genuinely NEW generation clears this tab's Stop intent and un-stops
			// the message. `begin()` has already replaced any stopped lifecycle
			// record for this id (a re-run of the SAME generation id deliberately
			// keeps its latch — see the registry), so only the durable flag is left
			// to clear, on both the caller's message object and the live row.
			localStop = null;
			let cleared = false;
			for (const target of [message, history?.messages?.[message.id]]) {
				if (target?.userStopped === true) {
					target.userStopped = false;
					cleared = true;
				}
			}
			if (cleared) history = { ...history };
		}
		return { generationId, turnId, fresh };
	};

	const attachGenerationController = (
		messageId: string,
		generationId: string,
		controller: AbortController
	) => generationLifecycles.attachController(messageId, generationId, controller);

	const ownsGeneration = (messageId: string | null | undefined) => {
		const record = generationLifecycles.get(messageId);
		return !!record && record.phase !== 'stopped' && record.phase !== 'terminal';
	};

	// Settle one message's generation and report whether the whole TURN is now
	// over (no other local generation for this chat, no sibling model response
	// still pending). Callers use the return value to decide turn-scoped
	// teardown; `generating` and `taskIds` both derive from the registry, so
	// marking the record terminal is all this needs to do for them.
	const settleGenerationLifecycle = (messageId: string | null | undefined) => {
		if (!messageId) return false;
		generationLifecycles.terminal(messageId);
		const visibleChatId = getVisibleChatId();
		const activeLocal = generationLifecycles.activeForChat(visibleChatId);
		const message = history.messages[messageId];
		const siblingIds =
			message?.parentId && history.messages[message.parentId]?.childrenIds
				? history.messages[message.parentId].childrenIds
				: [];
		const hasPendingSibling = siblingIds.some((id) => {
			if (id === messageId) return false;
			const sibling = history.messages[id];
			return (
				sibling?.role === 'assistant' &&
				sibling.done !== true &&
				!sibling.error &&
				!isUserStoppedMessageId(id)
			);
		});
		return activeLocal.length === 0 && !hasPendingSibling;
	};

	// Gated debug logger. Flip on in DevTools with `localStorage.chatStreamDebug = '1'`
	// to surface every controller-state mutation and handleOpenAIError invocation —
	// useful for tracking down stray "operation was aborted" errors without leaving
	// noise in production.
	const chatStreamDebug = (...args: unknown[]) => {
		try {
			if (typeof localStorage !== 'undefined' && localStorage.chatStreamDebug === '1') {
				console.debug(...args);
			}
		} catch {
			// localStorage access can throw in some contexts (e.g. disabled cookies)
		}
	};

	type StreamFlushState = {
		animationFrame: number | null;
		runTTS: boolean;
		ownerId?: string | null;
	};
	const streamFlushes = new Map<string, StreamFlushState>();
	const streamTTSPartCounts = new Map<string, number>();

	// Stream protocol v2.1: per-message mirror of the backend's
	// `STREAM_VERSION[message_id]` + `content_blocks` + `TOOL_RESULTS` state.
	// Used by chatDeltaHandler to apply ops in-order and recover from gaps
	// via the snapshot endpoint (Phase 0 wire contracts #1 + #2 in the plan).
	type StreamDelta = {
		op: string;
		version: number;
		run?: number;
		payload: any;
	};
	type StreamMirror = {
		content_blocks: any[];
		version: number;
		// Stream RUN id (epoch) this mirror's version space belongs to. A
		// retry / "Continue Response" reuses the SAME message id but restarts
		// versions at 0 — the run id (minted server-side per generation run and
		// stamped on every stream event) is what makes that reset explicit.
		// 0 = unknown (legacy event/cache without run info).
		run: number;
		tool_results: Map<string, any>;
		pending_deltas: StreamDelta[];
		snapshotting: boolean;
		snapshotPromise: Promise<void> | null;
		// STRUCTURAL INCOHERENCE latch. Set when applyDeltaOp reports a
		// structural gap (e.g. an append had to FABRICATE a block whose
		// block_open this mirror never saw — the reasoning-as-answer-text
		// corruption) and cleared only when an authoritative server snapshot is
		// ADOPTED. While set: (a) the sessionStorage stream cache is cleared and
		// never rewritten, so this tab can never re-poison itself through a
		// reload; (b) every subsequent delta re-arms a debounced heal snapshot,
		// so a heal fetch that FAILED (offline at that instant) is retried until
		// one lands instead of leaving the corruption in place forever; (c) a
		// Stop never persists this mirror's content over the backend's.
		needsHeal: boolean;
		lastHealRequestAt: number;
	};
	const streamMirrors = new Map<string, StreamMirror>();
	// One-shot guard for the terminal convergence backstop in chatDoneHandler:
	// a message that arrived at chat:done EMPTY while the server reports real
	// content triggers a single authoritative loadChat, never a reload loop.
	const _emptyDoneReloadedIds = new Set<string>();

	const getOrCreateStreamMirror = (messageId: string): StreamMirror => {
		let mirror = streamMirrors.get(messageId);
		if (!mirror) {
			const existing = history?.messages?.[messageId];
			mirror = {
				content_blocks: Array.isArray(existing?.content_blocks) ? existing.content_blocks : [],
				version: 0,
				run: 0,
				tool_results: new Map(),
				pending_deltas: [],
				snapshotting: false,
				snapshotPromise: null,
				needsHeal: false,
				lastHealRequestAt: 0
			};
			streamMirrors.set(messageId, mirror);
		}
		return mirror;
	};

	// Reconcile an incoming stream event's run id against the mirror's.
	//  - 'stale': the event belongs to an OLDER run — drop it (it would splice
	//    superseded content/ops into the current run).
	//  - 'reset': the event belongs to a NEWER run — the mirror was cleared
	//    (version space restarted at 0) and adopted the new run; the caller
	//    should proceed treating the event as the new run's.
	//  - 'ok': same run, or no run info on either side (legacy) — proceed with
	//    plain version arithmetic.
	const reconcileMirrorRun = (
		mirror: StreamMirror,
		run: number | null | undefined
	): 'stale' | 'reset' | 'ok' => {
		const eventRun = typeof run === 'number' && run > 0 ? run : 0;
		if (!eventRun) return 'ok';
		if (!mirror.run) {
			mirror.run = eventRun;
			return 'ok';
		}
		if (eventRun < mirror.run) return 'stale';
		if (eventRun > mirror.run) {
			mirror.run = eventRun;
			mirror.version = 0;
			mirror.content_blocks = [];
			mirror.tool_results = new Map();
			mirror.pending_deltas = [];
			// A fresh run starts from a coherent empty mirror — any structural
			// incoherence belonged to the dead run.
			mirror.needsHeal = false;
			return 'reset';
		}
		return 'ok';
	};

	const emitPendingTTSParts = (message: any, { done = false }: { done?: boolean } = {}) => {
		if (!message?.content) {
			return;
		}

		const messageContentParts = getMessageContentParts(
			removeAllDetails(message.content),
			($config as any)?.audio?.tts?.split_on ?? 'punctuation'
		);
		if (!done) {
			messageContentParts.pop();
		}

		let dispatchedCount = streamTTSPartCounts.get(message.id) ?? 0;
		if (dispatchedCount > messageContentParts.length) {
			dispatchedCount = 0;
		}

		for (const content of messageContentParts.slice(dispatchedCount)) {
			if (!content || content === message.lastSentence) {
				continue;
			}

			message.lastSentence = content;
			eventTarget.dispatchEvent(
				new CustomEvent('chat', {
					detail: {
						id: message.id,
						content
					}
				})
			);
		}

		streamTTSPartCounts.set(message.id, messageContentParts.length);
	};

	const cancelStreamingMessageFlush = (messageId: string) => {
		const state = streamFlushes.get(messageId);
		if (!state) {
			return;
		}

		if (state.animationFrame !== null) {
			cancelAnimationFrame(state.animationFrame);
		}
		streamFlushes.delete(messageId);
		streamTTSPartCounts.delete(messageId);
	};

	// Release the per-message stream mirror (content_blocks snapshot +
	// tool_results Map + buffered deltas) on ANY terminal state. Previously the
	// mirror was deleted only on chat:done, so a stopped or errored generation
	// leaked its mirror — including a potentially large tool_results Map — for
	// the lifetime of the Chat component. Safe to call repeatedly.
	const releaseStreamMirror = (messageId: string) => {
		if (!messageId) return;
		streamMirrors.delete(messageId);
		clearStreamCache(messageId);
	};

	const flushStreamingMessage = (messageId: string, force = false) => {
		const perf = streamPerfStart();
		const state = streamFlushes.get(messageId);
		if (!state) {
			streamPerfEnd('chat.flush_missing', perf);
			return;
		}

		if (state.animationFrame !== null) {
			cancelAnimationFrame(state.animationFrame);
			state.animationFrame = null;
		}

		const message = (history.messages as Record<string, any>)?.[messageId];
		if (!message) {
			streamFlushes.delete(messageId);
			streamTTSPartCounts.delete(messageId);
			streamPerfEnd('chat.flush_no_message', perf);
			return;
		}

		if (
			!force &&
			state.ownerId &&
			['stopped', 'terminal'].includes(generationLifecycles.get(state.ownerId)?.phase ?? '')
		) {
			streamFlushes.delete(messageId);
			streamTTSPartCounts.delete(messageId);
			streamPerfEnd('chat.flush_stale_owner', perf);
			return;
		}

		if (state.runTTS) {
			emitPendingTTSParts(message);
		}

		(history.messages as Record<string, any>)[messageId] = message;
		// Wake only the affected ResponseMessage. This avoids reassigning the whole
		// history object on every streaming frame, so the message list and sibling
		// responses do not re-evaluate for each token/tool delta.
		bumpMessageRevision(messageId);
		streamFlushes.delete(messageId);

		if (autoScroll) {
			scrollToBottom();
		}

		streamPerfEnd('chat.flushStreamingMessage', perf);
	};

	const scheduleStreamingMessageFlush = (
		messageId: string,
		{ runTTS = false, ownerId = null }: { runTTS?: boolean; ownerId?: string | null } = {}
	) => {
		let state = streamFlushes.get(messageId);
		if (!state) {
			state = {
				animationFrame: null,
				runTTS: false,
				ownerId
			};
			streamFlushes.set(messageId, state);
		}

		state.runTTS = state.runTTS || runTTS;
		state.ownerId = ownerId ?? state.ownerId;

		if (state.animationFrame !== null) {
			return;
		}

		state.animationFrame = requestAnimationFrame(() => {
			flushStreamingMessage(messageId);
		});
	};

	const skipRemainingRetriesSet = new Set();
	const markSkipRemainingRetries = (messageId) => {
		if (messageId) skipRemainingRetriesSet.add(messageId);
	};

	// Network-level fetch failure (offline / connection reset / network switched
	// mid-request), as opposed to an error the SERVER returned. These are
	// treated as connectivity events, not model failures: they don't burn the
	// retry loops' no-progress budget, their retry countdown holds while the
	// socket is down (retry fires when connectivity returns, not on a blind
	// timer), and — because the backend dedupes duplicate sends by assistant
	// message id — re-POSTing after a blip is safe even when the original
	// request actually made it through and is still generating.
	const isNetworkFetchError = (err: any): boolean => {
		if (!err) return false;
		if (typeof err === 'object' && (err as any).network === true) return true;
		const msg = String((err as any)?.message ?? err ?? '').toLowerCase();
		return (
			err instanceof TypeError ||
			msg.includes('failed to fetch') ||
			msg.includes('networkerror') ||
			msg.includes('load failed') ||
			msg.includes('network changed') ||
			msg.includes('err_network') ||
			msg.includes('err_internet_disconnected') ||
			msg.includes('err_connection')
		);
	};

	type UsagePayload = {
		prompt_tokens?: number | string | null;
		completion_tokens?: number | string | null;
		total_tokens?: number | string | null;
		prompt_tokens_details?: { cached_tokens?: number | string | null } | null;
	};

	type AppliedUsage = {
		promptTokens: number;
		completionTokens: number;
		totalTokens: number;
		cacheReadTokens: number;
	};

	// Per-message record of the last cumulative usage we've added to the navbar
	// totals. Provider usage payloads (OpenRouter, OpenAI, …) are cumulative for
	// the response and the same response surfaces here through multiple paths
	// (direct-stream chunk, socket delta op, snapshot reconciliation, chat:done
	// finalize). Storing the prior cumulative lets us add only the delta on each
	// re-emission, so totals stay idempotent regardless of order/count of paths.
	const lastAppliedUsageByMessage = new Map<string, AppliedUsage>();

	const getUsageTokenCounts = (usage: UsagePayload = {}) => {
		const toNumber = (value: unknown) => {
			const n = Number(value ?? 0);
			return Number.isFinite(n) ? n : 0;
		};

		const promptTokens = toNumber(usage?.prompt_tokens);
		const completionTokens = toNumber(usage?.completion_tokens);
		const totalTokens = toNumber(usage?.total_tokens) || promptTokens + completionTokens;
		const cacheReadTokens = toNumber(usage?.prompt_tokens_details?.cached_tokens);

		return {
			promptTokens,
			completionTokens,
			totalTokens,
			cacheReadTokens
		};
	};

	const applyUsageToChatTokenStats = (
		_chatId: string | null | undefined,
		messageId: string | null | undefined,
		usage: UsagePayload | null | undefined
	) => {
		if (!usage || !_chatId || _chatId.startsWith('local:')) return;

		const { promptTokens, completionTokens, totalTokens, cacheReadTokens } =
			getUsageTokenCounts(usage);
		if (!promptTokens && !completionTokens && !totalTokens && !cacheReadTokens) return;

		const prior = messageId ? lastAppliedUsageByMessage.get(messageId) : undefined;
		const isFirstSighting = !prior;

		// First-sighting-but-already-authoritative guard: a first sighting normally
		// means "this round's usage has never been added, add it in full." But if
		// `lastAppliedUsageByMessage` lost its entry for this message — a page
		// reload, or the 1000-entry LRU eviction above — while the chat's
		// `chatTokenStats` store ALREADY carries an authoritative (message_count > 0)
		// baseline that already covers (or is about to cover, via the next
		// chat:token-usage push) this round, adding the FULL payload here double-
		// counts it on top of the authoritative totals. That double-count is exactly
		// what pinned the token pill at an inflated value in production — and
		// because the store was inflated, EVERY subsequent authoritative DB read
		// looked "stale" by the old total-based staleResponse guard and got
		// rejected, so it never self-healed without a full page reload.
		//
		// Detect it by comparing this round's absolute (not delta) usage numbers
		// against the store's current totals: if the store already has a real
		// baseline for this chat AND its totals are already >= this payload's
		// totals, treat this as already-covered — seed the map so future deltas
		// are computed correctly, but contribute nothing here.
		const storeBaseline = get(chatTokenStats);
		const hasAuthoritativeBaseline =
			!!storeBaseline &&
			storeBaseline.chat_id === _chatId &&
			(storeBaseline.message_count ?? 0) > 0;
		const alreadyCoveredByAuthoritative =
			isFirstSighting &&
			hasAuthoritativeBaseline &&
			(storeBaseline.total_input_tokens ?? 0) >= promptTokens &&
			(storeBaseline.total_output_tokens ?? 0) >= completionTokens &&
			(storeBaseline.total_tokens ?? 0) >= totalTokens &&
			(storeBaseline.total_cache_read_tokens ?? 0) >= cacheReadTokens;

		// Clamp at 0: if a provider ever reports a smaller value on a later
		// emission, don't subtract — backend analytics is authoritative and
		// reconciles on reload. A first sighting already covered by an
		// authoritative baseline (see above) contributes a zero delta instead of
		// the normal full first-sighting add.
		const deltaPrompt = alreadyCoveredByAuthoritative
			? 0
			: Math.max(0, promptTokens - (prior?.promptTokens ?? 0));
		const deltaCompletion = alreadyCoveredByAuthoritative
			? 0
			: Math.max(0, completionTokens - (prior?.completionTokens ?? 0));
		const deltaTotal = alreadyCoveredByAuthoritative
			? 0
			: Math.max(0, totalTokens - (prior?.totalTokens ?? 0));
		const deltaCacheRead = alreadyCoveredByAuthoritative
			? 0
			: Math.max(0, cacheReadTokens - (prior?.cacheReadTokens ?? 0));

		if (messageId) {
			lastAppliedUsageByMessage.set(messageId, {
				promptTokens: Math.max(promptTokens, prior?.promptTokens ?? 0),
				completionTokens: Math.max(completionTokens, prior?.completionTokens ?? 0),
				totalTokens: Math.max(totalTokens, prior?.totalTokens ?? 0),
				cacheReadTokens: Math.max(cacheReadTokens, prior?.cacheReadTokens ?? 0)
			});
			if (lastAppliedUsageByMessage.size > 1000) {
				const oldest = lastAppliedUsageByMessage.keys().next().value;
				if (oldest) lastAppliedUsageByMessage.delete(oldest);
			}
		}

		chatTokenStats.update((current) => {
			const base = current?.chat_id === _chatId ? current : null;
			return {
				chat_id: _chatId,
				total_input_tokens: (base?.total_input_tokens ?? 0) + deltaPrompt,
				total_output_tokens: (base?.total_output_tokens ?? 0) + deltaCompletion,
				total_tokens: (base?.total_tokens ?? 0) + deltaTotal,
				total_cache_read_tokens: (base?.total_cache_read_tokens ?? 0) + deltaCacheRead,
				last_input_tokens: promptTokens,
				last_output_tokens: completionTokens,
				last_cache_read_tokens: cacheReadTokens,
				// Don't advance message_count either when this first sighting was
				// already covered by an authoritative baseline — that baseline's own
				// message_count already accounts for this round (or the next push will).
				message_count:
					(base?.message_count ?? 0) + (isFirstSighting && !alreadyCoveredByAuthoritative ? 1 : 0),
				cost: base?.cost ?? 0,
				loading: false
			};
		});
	};

	// Authoritative per-chat token totals pushed by the backend (chat:token-usage)
	// after each conversation_token_usage write — the SAME cumulative numbers a
	// chat reload reads from the DB. This is what makes the pill reflect ongoing
	// usage live: it is the ONLY live path that surfaces subagent token roll-up on
	// the parent pill (a subagent's own usage events never reach applyUsageToChatTokenStats),
	// and because it carries true cumulative totals it also corrects the optimistic
	// per-round delta path, which undercounts multi-round agentic turns. No HTTP
	// fetch involved — it rides the already-open socket, so it is bandwidth-cheap.
	const applyAuthoritativeChatTokenStats = (_chatId: string | null | undefined, stats: any) => {
		if (!_chatId || _chatId.startsWith('local:') || !stats || typeof stats !== 'object') return;
		// Belt-and-suspenders: the event is already visibility-gated to this chat,
		// but never let a payload stamped for another chat write this chat's row.
		if (stats.chat_id && stats.chat_id !== _chatId) return;

		const incomingCount = Number(stats.message_count ?? 0);

		chatTokenStats.update((current) => {
			const base = current?.chat_id === _chatId ? current : null;
			// Cumulative totals only ever grow, so Math.max makes the apply
			// reorder-proof: an older push delivered after a newer one (concurrent
			// subagent rounds racing through emit) can never regress the visible
			// numbers. The "last request" snapshot fields are NOT cumulative, so we
			// gate them on the monotonic message_count (DB event count) and keep the
			// prior snapshot when an out-of-order/older push arrives.
			const isFresh = !base || incomingCount >= (base.message_count ?? 0);
			return {
				chat_id: _chatId,
				total_input_tokens: Math.max(
					base?.total_input_tokens ?? 0,
					Number(stats.total_input_tokens ?? 0)
				),
				total_output_tokens: Math.max(
					base?.total_output_tokens ?? 0,
					Number(stats.total_output_tokens ?? 0)
				),
				total_tokens: Math.max(base?.total_tokens ?? 0, Number(stats.total_tokens ?? 0)),
				total_cache_read_tokens: Math.max(
					base?.total_cache_read_tokens ?? 0,
					Number(stats.total_cache_read_tokens ?? 0)
				),
				last_input_tokens: isFresh
					? Number(stats.last_input_tokens ?? 0)
					: (base?.last_input_tokens ?? 0),
				last_output_tokens: isFresh
					? Number(stats.last_output_tokens ?? 0)
					: (base?.last_output_tokens ?? 0),
				last_cache_read_tokens: isFresh
					? Number(stats.last_cache_read_tokens ?? 0)
					: (base?.last_cache_read_tokens ?? 0),
				message_count: Math.max(base?.message_count ?? 0, incomingCount),
				// Cost now rides the push (authoritative read-time value). It is
				// monotonic non-decreasing within a session, so Math.max is
				// reorder-proof (an older push delivered late can't regress it) and
				// lets the $ segment update live / appear on a new chat's first turn
				// instead of only after a reload. A downward correction (message
				// delete / pricing edit) surfaces on the next reload/chat-switch,
				// when the store resets. Older builds / a throttle-dropped final
				// push may omit cost — keep the prior value then rather than zeroing.
				cost:
					stats.cost == null
						? (base?.cost ?? 0)
						: Math.max(base?.cost ?? 0, Number(stats.cost) || 0),
				loading: false
			};
		});
	};

	let chat = null;
	let tags = [];

	let history = $state({
		messages: {},
		currentId: null
	});

	// Structure revision: bumped ONLY when the message graph changes shape
	// (a message is added/removed, the current branch pointer changes, or the
	// history is rebuilt on load/reattach) — never on a per-frame streaming
	// content flush. Messages.svelte rebuilds its rendered chain only when this
	// (or currentId / pagination state) changes, so streaming deltas no longer
	// force an O(chat-length) chain walk + full re-render every animation frame.
	let messageStructureRevision = $state(0);
	const bumpMessageStructure = () => {
		messageStructureRevision += 1;
	};

	// Backend task ids for the visible chat's live work. DERIVED, never assigned:
	// the lifecycle registry already records each generation's task ids (via
	// markAccepted and the server work-state reconcile), so a second hand-kept
	// copy could only ever drift from it — and it did, across 15 separate
	// assignments that each had to remember to clear it. Same treatment, and same
	// reason, as `generating` above.
	let taskIds: string[] | null = $derived.by(() => {
		void generationRevision;
		const ids = generationLifecycles.taskIdsForChat(activeChatId);
		return ids.length > 0 ? ids : null;
	});
	let resumeTaskPollInterval: ReturnType<typeof setTimeout> | null = null;
	let resumeTaskPollInFlight = false;
	// Bumped by both stop and start so an in-flight tick from a superseded poller
	// can never reschedule itself alongside a fresh one.
	let resumeTaskPollGeneration = 0;
	const RESUME_POLL_BASE_MS = 2000;
	const RESUME_POLL_MAX_MS = 10000;

	// `force` is for TEARDOWN only — unmount, chat switch, Stop. Everything else
	// is a "this turn is over, the safety net has done its job" stop, and those
	// all fire at exactly the moment a queue-drain bridge gets armed. The poll is
	// the ONLY thing that can retire that bridge (see `markQueueDrainPending`), so
	// letting a turn-end stop it is what stranded the composer on Stop with the
	// answer fully rendered. Enforcing the invariant here, once, is what keeps
	// every terminal handler from having to remember it.
	const stopResumeTaskPolling = ({ force = false }: { force?: boolean } = {}) => {
		if (queueDrainPending && !force) return;
		resumeTaskPollGeneration += 1;
		if (resumeTaskPollInterval) {
			clearTimeout(resumeTaskPollInterval);
			resumeTaskPollInterval = null;
		}
		resumeTaskPollInFlight = false;
	};

	const startResumeTaskPolling = (chatIdToWatch: string) => {
		if (!chatIdToWatch || $temporaryChatEnabled) return;
		if (resumeTaskPollInterval) return;
		const myGeneration = ++resumeTaskPollGeneration;
		// Backoff: this poll is a missed-terminal safety net, not the primary
		// signal (chat:done arrives over the socket, and every terminal handler
		// reconciles instantly). A fixed 2s cadence for a multi-minute agentic
		// turn was a sustained request trickle on cellular.
		let delay = RESUME_POLL_BASE_MS;
		const tick = async () => {
			if (resumeTaskPollInFlight) return;
			resumeTaskPollInFlight = true;
			try {
				// Hidden tab: the server suppresses live deltas anyway and refocus
				// runs a full snapshot reconcile — skip the network, keep the timer.
				if (document.hidden) {
					delay = RESUME_POLL_BASE_MS;
					return;
				}
				const res = await getChatWorkState(localStorage.token, chatIdToWatch).catch(() => null);
				// The await above is a network RTT during which the user may have
				// navigated to a DIFFERENT chat. taskIds / the lifecycle registry /
				// the subagent store are shared with the now-visible chat, and
				// resumeTaskPollInterval may have been replaced by navigateHandler's new
				// poller — so once we are no longer the visible chat, touch NOTHING.
				if (getVisibleChatId() !== chatIdToWatch) return;
				if (!res) {
					// The task-status probe FAILED (e.g. a network blip). Do NOT treat a
					// failed probe as "turn finished" — that would prematurely settle live
					// records and fire a spurious loadChat mid-generation. Leave the state
					// as-is and retry on the next tick.
					return;
				}
				// Passing the chat id makes this answer AUTHORITATIVE: records the
				// server no longer lists are settled here, which is what releases a
				// turn whose terminal event this tab never received. Records still in
				// a local pre-POST phase are exempt (see the registry).
				// The reconcile above is the ONLY thing this tick has to do to the
				// generation state: `generating` and `taskIds` both derive from the
				// registry it just updated.
				generationLifecycles.reconcileServerOperations(
					res?.generations,
					navigateGeneration,
					chatIdToWatch
				);
				// Same answer, applied to the queue-drain handoff: this is the probe
				// that retires the bridge when the backend turns out not to be
				// draining after all. It runs BEFORE the teardown test below, so a
				// bridge still standing there means the server confirmed a drain.
				reconcileQueueDrain(res);
				const activeRerunTaskIds = res?.rerun_task_ids ?? [];
				const hasGenerationWork =
					generationLifecycles.activeForChat(chatIdToWatch).length > 0 || activeSendRetryLoops > 0;
				if (!hasGenerationWork && activeRerunTaskIds.length === 0 && !queueDrainPending) {
					stopResumeTaskPolling();
					await loadChat();
				} else {
					if (hasGenerationWork && ($config as any)?.features?.stream_protocol_version === 'v2.1') {
						await snapshotActiveStreamsForChat(chatIdToWatch).catch(() => []);
					}
					delay = Math.min(delay * 1.5, RESUME_POLL_MAX_MS);
				}
			} finally {
				resumeTaskPollInFlight = false;
				// Reschedule only if this poller is still the current one (stop — even
				// from inside the tick body via loadChat — or a restart bumps the
				// generation, and stop also nulls the handle).
				if (resumeTaskPollGeneration === myGeneration && resumeTaskPollInterval !== null) {
					resumeTaskPollInterval = setTimeout(tick, delay);
				}
			}
		};
		resumeTaskPollInterval = setTimeout(tick, delay);
	};

	// Chat Input
	let prompt = $state('');
	let chatFiles = $state([]);
	let files = $state([]);
	let params = $state({});
	let deferredUploadSubmit: { token: number } | null = null;
	let deferredUploadSubmitToken = 0;

	// Queue of follow-up messages submitted while a response was streaming.
	// Each item is SELF-CONTAINED: it snapshots everything the backend needs to
	// drive the send autonomously (server-driven drain), because the drain may
	// run with zero browser tabs open. The backend pops the head on clean
	// completion and starts the next generation via start_generation().
	//
	// Time-sensitive context (current date/time prompt variables) is NOT
	// snapshotted — the backend recomputes those from `timezone` at drain time so
	// a message queued at 2pm and drained at 3pm gets the right "current time".
	type QueuedMessage = {
		id: string;
		// Display fields (shown in the queued-message chip strip).
		prompt: string;
		files: any[];
		atSelectedModelId?: string | null;
		createdAt: number;
		// Delivery mode:
		//   'after_final' → deliver as a fresh turn after the whole response
		//                   finishes (the server-driven drain; default).
		//   'steer'       → inject at the next tool-call boundary mid-task (the
		//                   backend agentic loop pops these via
		//                   pop_steer_items_by_id; never goes through the drain).
		mode?: 'after_final' | 'steer';
		// Self-contained send spec consumed by the backend drain. Snapshotted at
		// enqueue time from the live toolbar/settings state.
		sendSpec?: {
			model: string;
			models: string[];
			content: string;
			files: any[];
			params?: any;
			tool_ids?: string[];
			tool_servers?: any[];
			filter_ids?: string[];
			features?: any;
			variables?: any;
			reasoning?: any;
			service_tier?: string;
			background_tasks?: any;
			model_item?: any;
			stream_options?: any;
			timezone?: string;
		};
	};
	let queue: QueuedMessage[] = $state([]);
	// Ids of items THIS tab added to `queue` optimistically whose append_queue_item
	// PATCH hasn't confirmed yet. Used to preserve them when a concurrent chat:queue:
	// updated broadcast (from another tab's queue op, whose server snapshot predates
	// our append) would otherwise blind-replace the queue and drop our own chip.
	// Also holds ids of optimistic EDITs (prefer our local version over the snapshot's
	// stale copy). Cleared when the item's PATCH settles.
	let pendingQueueItemIds = new Set<string>();
	// Ids this tab optimistically REMOVED whose remove PATCH hasn't confirmed — kept
	// excluded from a concurrent broadcast's snapshot so a just-removed chip doesn't
	// reappear before our remove commits.
	let pendingRemovedQueueItemIds = new Set<string>();
	// Reconcile an authoritative server queue snapshot with THIS tab's own not-yet-
	// committed optimistic mutations: drop our pending removes, prefer our local copy
	// for pending adds/edits, and re-append pending adds the snapshot doesn't have yet.
	// Used for BOTH the live chat:queue:updated broadcast and the reconnect reconcile so
	// a snapshot that predates our in-flight PATCH never drops/reverts/resurrects a chip.
	const reconcileServerQueue = (serverQueue: QueuedMessage[]): QueuedMessage[] => {
		if (!Array.isArray(serverQueue)) return queue;
		if (pendingQueueItemIds.size === 0 && pendingRemovedQueueItemIds.size === 0) {
			return serverQueue;
		}
		const localById = new Map(queue.map((q) => [q?.id, q]));
		const merged: QueuedMessage[] = serverQueue
			.filter((q) => q && !pendingRemovedQueueItemIds.has(q.id))
			.map((q) =>
				pendingQueueItemIds.has(q.id) && localById.has(q.id)
					? (localById.get(q.id) as QueuedMessage)
					: q
			);
		const mergedIds = new Set(merged.map((q) => q?.id));
		for (const l of queue) {
			if (l && pendingQueueItemIds.has(l.id) && !mergedIds.has(l.id)) merged.push(l);
		}
		return merged;
	};
	let queueSending = $state(false);
	let queueSavePromise: Promise<void> = Promise.resolve();
	// Falling-edge tracker for auto-send-on-complete. Kept in sync by the single
	// watcher at the bottom of this file; the only OTHER writes are the explicit
	// `false` resets on a chat switch, which are load-bearing — leaving a chat
	// mid-generation drops the derived `generating` to false for the new chat,
	// and without the reset that reads as "a turn just finished here" and drains
	// the new chat's queue.
	let _wasGenerating = $state(false);

	// ─── The queue-drain handoff ─────────────────────────────────────────────
	//
	// On a SERVER-DRAIN chat, the moment the prior turn finishes cleanly with
	// items still queued, the backend decides whether to pop the head and start
	// the next generation. That decision takes a beat, and until it lands this
	// tab knows nothing: the finished turn's lifecycle record is settled and the
	// next one does not exist yet. Without a bridge the input bar flicks
	// working -> idle "Send a Message" -> working across the gap.
	//
	// This used to be a bare 20-SECOND TIMER, and it was the ONE term of
	// `turnLive` that was a GUESS rather than something observed — which is
	// exactly why the composer could sit on Stop, alone, long after the answer
	// had fully rendered as finished with its action row. Every way the backend
	// can decline to drain (lock contention, a superseded completion, a Stop on
	// the finishing turn, or a steer already consumed mid-turn whose queue
	// broadcast this tab never received) left the guess standing, and the socket
	// events that clear it — chat:user-message / chat:queue:drained — are
	// precisely the ones a weak mobile link drops. Worse, the same turn-end that
	// armed the guess also STOPPED the work-state poll, so for those 20s nothing
	// was left that could discover the truth.
	//
	// It is now VERIFIED rather than timed. Arming it also arms the poll that
	// retires it, and the first authoritative work-state answer showing neither a
	// live generation nor a server-side `draining` marker clears it (see
	// `reconcileQueueDrain`). So the bridge can outlive the truth by at most one
	// poll interval, and when the drain never happens at all it no longer
	// outlives it by a fixed twenty seconds — it ends as soon as we can ask.
	let queueDrainPending = $state(false);
	// Set when handleRemoteUserMessage defers a drained user-message insert to
	// loadChat (unknown parent). Used to suppress the paired chip-clear shrink so a
	// behind tab never shows a "chip gone, bubble absent" gap during the loadChat RTT.
	let remoteUserDeferredLoadAt = 0;
	const clearQueueDrainPending = () => {
		queueDrainPending = false;
	};
	const markQueueDrainPending = () => {
		const chatIdToVerify = getVisibleChatId();
		if (!chatIdToVerify || chatIdToVerify.startsWith('local:')) return;
		queueDrainPending = true;
		// The bridge and the channel that retires it are armed TOGETHER. A pending
		// drain with nothing polling is the latch this replaced: the terminal
		// handlers stop the resume poll on turn settle (correct when nothing is
		// queued), so without this the bar would wait on socket events alone.
		// Idempotent — startResumeTaskPolling early-returns if one is running.
		startResumeTaskPolling(chatIdToVerify);
	};

	/**
	 * Retire the drain bridge against the server's authoritative work state.
	 * Called from EVERY site that reconciles that state (resume poll, reconnect,
	 * chat open) so "is a drain still coming?" has one answer everywhere.
	 *
	 * A null/!ok `workState` means the probe FAILED — unknown, never empty.
	 * Clearing on a failed probe would drop the composer to idle in the middle of
	 * a drain the server is really performing, which is the same class of lie in
	 * the other direction.
	 */
	const reconcileQueueDrain = (workState: any) => {
		if (!queueDrainPending) return;
		if (!workState || typeof workState !== 'object') return;
		// `draining` is the backend's durable marker, written in the same locked
		// transaction that pops the queue head. While it stands the drain is
		// committed and its generation is registering right now.
		if (workState.draining) return;
		if ((workState.generations?.length ?? 0) > 0) return;
		clearQueueDrainPending();
	};

	// THE definition of "a turn is live" — a generation or backend task is in
	// flight (a sibling branch still streaming, a resumed/headless drain, a
	// subagent run), the visible leaf hasn't finished, or a server-side queue
	// drain is about to take over. Everything that has to make the send-vs-steer
	// / Stop-vs-Send / queue-vs-submit decision reads THIS, including
	// MessageInput (via its `turnLive` prop). It was previously computed twice —
	// here without `queueDrainPending`, there with it — and the disagreement is
	// what made bare Enter flip between send and steer inconsistently.
	let turnLive = $derived(
		generating ||
			(taskIds?.length ?? 0) > 0 ||
			(!!history?.currentId && history.messages[history.currentId]?.done != true) ||
			queueDrainPending
	);

	const persistQueue = async () => {
		const _chatId = getVisibleChatId();
		if (!_chatId) return;

		const queueSnapshot = cloneState(queue ?? []);
		const save = queueSavePromise
			.catch(() => undefined)
			.then(async () => {
				await saveChatHandler(_chatId, history, params, [
					{ op: 'set_queue', queue: queueSnapshot }
				]);
			});

		queueSavePromise = save.catch(() => undefined);

		await save;
	};

	// Token usage tracking — backend pushes `token-usage:update` socket events;
	// the local mirror reads the store so existing references continue to work.
	let tokenUsageGroups: Record<string, any> = $state({});
	let resetTimeouts: Map<string, ReturnType<typeof setTimeout>> = new Map(); // Per-group reset timeouts
	let resetTrigger = $state(0); // Increment to force Svelte reactivity when reset times pass

	/**
	 * Compute effective usage for a group, considering if reset time has passed client-side.
	 * This provides instant UI feedback when reset occurs, even before server confirms.
	 */
	const getEffectiveUsage = (groupData: any): { in: number; out: number; total: number } => {
		const now = Math.floor(Date.now() / 1000);
		const nextReset = groupData?.next_reset_at;

		// If we're past the reset time, show 0 optimistically while waiting for server confirmation
		if (nextReset && now >= nextReset) {
			return { in: 0, out: 0, total: 0 };
		}

		return groupData?.usage || { in: 0, out: 0, total: 0 };
	};

	// Reasoning effort tracking
	let reasoning = $state({ effort: 'medium' });

	// Service tier tracking. Value is provider-specific (OpenAI: default/flex/
	// priority; Gemini: standard/flex/priority; etc.) so we keep it as `string`
	// rather than a fixed union — the per-model allowed list lives on
	// `meta.service_tier.values`. MessageInput.svelte only clamps this to what
	// the current model allows; restoring/persisting the per-model preference
	// lives here (see `restoreServiceTierForModel` below), because this
	// component — unlike MessageInput — stays alive across the transition from
	// the empty-chat Placeholder composer to the docked composer.
	let serviceTier: string = $state('default');

	// Per-model service tier persistence, single source of truth. Deliberately
	// NOT owned by MessageInput.svelte: that component is destroyed and
	// recreated the moment `history.currentId` goes from null to set (the
	// Placeholder → docked-composer swap right after the first send), and a
	// fresh mount looks identical to a genuine model switch from the inside of
	// a child component. Keeping this state here — where "did the model
	// actually change" is already tracked via `oldSelectedModelIds` — means a
	// composer remount can never be mistaken for a switch and silently
	// overwrite the tier that was just used to send.
	let serviceTierByModel: Record<string, string> = {};
	try {
		const _storedServiceTierByModel = localStorage.getItem('serviceTierByModel');
		if (_storedServiceTierByModel) serviceTierByModel = JSON.parse(_storedServiceTierByModel);
	} catch {}

	const getAllowedServiceTiersForModel = (modelId: string): string[] => {
		const m = $models.find((mm) => mm.id === modelId);
		const values = (m?.info?.meta as any)?.service_tier?.values;
		return Array.isArray(values) && values.length > 0 ? values : ['default', 'flex', 'priority'];
	};

	const modelSupportsServiceTier = (modelId: string | undefined): boolean => {
		if (!modelId) return false;
		const m = $models.find((mm) => mm.id === modelId);
		if (!m) return false;
		return m.owned_by !== 'ollama' && (m?.info?.meta as any)?.service_tier?.enabled !== false;
	};

	// Apply modelId's persisted tier preference (falling back to a valid
	// current value, or the model's first allowed tier). Call this ONLY on a
	// genuine model switch or fresh chat load — never on a composer remount.
	const restoreServiceTierForModel = (modelId: string | undefined) => {
		if (!modelId) return;
		if (!modelSupportsServiceTier(modelId)) {
			serviceTier = 'default';
			return;
		}
		const allowed = getAllowedServiceTiersForModel(modelId);
		const stored = serviceTierByModel[modelId];
		if (stored && allowed.includes(stored)) {
			serviceTier = stored;
		} else if (!allowed.includes(serviceTier)) {
			serviceTier = allowed[0] ?? 'default';
		}
	};

	const saveServiceTierForModel = (modelId: string | undefined, tier: string) => {
		if (!modelId) return;
		serviceTierByModel = { ...serviceTierByModel, [modelId]: tier };
		try {
			localStorage.setItem('serviceTierByModel', JSON.stringify(serviceTierByModel));
		} catch {}
	};

	// Fired by MessageInput (whichever instance is currently mounted) when the
	// user picks a tier by hand: stand down the off-peak/threshold auto-flip
	// for the rest of this chat, and remember the choice for this model.
	const handleServiceTierTouched = (tier: string) => {
		serviceTierUserTouched = true;
		if (selectedModelIds.length === 1) {
			saveServiceTierForModel(selectedModelIds[0], tier);
		}
	};

	// Baseline so we only push a service-tier change to active task(s) when the
	// user actually flips it mid-run (and not when it's first set at task start).
	// Reset to null when no active task is running.
	let _serviceTierBaseline: typeof serviceTier | null = $state(null);

	// Auto-flip to `flex` service tier when (a) wall-clock falls inside the
	// admin-configured off-peak window where flex's latency penalty is
	// effectively zero, or (b) any token group covering this chat's model is
	// at/above the admin-configured threshold ratio. Policy values live on
	// `$config.features.flex_auto_flip_*` (see `/api/config`). Fires once per
	// chat with an "Undo" toast; resets on chat navigation.
	//
	// Fallback defaults match what the feature shipped with so behavior is
	// stable if the config payload is delayed or missing the keys (e.g. older
	// backend). The enabled-by-default fallback is `false` so we never
	// auto-flip in the absence of explicit admin policy.
	const isOffPeakHour = (now: Date, startHour: number, endHour: number, tz: string): boolean => {
		let hour: number;
		try {
			hour = Number(
				new Intl.DateTimeFormat('en-US', {
					timeZone: tz,
					hour: 'numeric',
					hour12: false
				}).format(now)
			);
		} catch {
			// Bad timezone string — skip rather than crash the chat.
			return false;
		}
		if (startHour === endHour) return false;
		// Window wraps midnight when start > end.
		return startHour > endHour
			? hour >= startHour || hour < endHour
			: hour >= startHour && hour < endHour;
	};

	const isApproachingAnyLimit = (groups: [string, any][], ratio: number): boolean => {
		return groups.some(([, g]) => {
			if (!g?.limit) return false;
			const used = g?.effectiveUsage?.total ?? 0;
			return used / g.limit >= ratio;
		});
	};

	// The auto-flip must not be a one-shot latch: a chat can sit open long
	// enough to cross the off-peak boundary, or usage can climb past the
	// threshold mid-conversation, so the rule is "keep the tier at flex until
	// the user says otherwise" — re-evaluate whenever `serviceTier` or
	// `_nowTick` changes and stand down permanently once the user picks a
	// tier by hand in this chat (`serviceTierUserTouched`).
	let serviceTierUserTouched = $state(false);
	let _nowTick = $state(Date.now());
	let _nowTickInterval: ReturnType<typeof setInterval> | null = null;

	// A model that has service tiers disabled (or doesn't list `flex`) must never
	// be flipped: MessageInput force-resets those back to `default` on every
	// `serviceTier` change, which would ping-pong forever now that the flip
	// re-evaluates.
	const modelSupportsFlexTier = (modelId: string | undefined): boolean => {
		if (!modelId) return false;
		const m = $models.find((mm) => mm.id === modelId);
		if (!m || m?.owned_by === 'ollama') return false;
		const st = (m?.info?.meta as any)?.service_tier;
		if (st?.enabled === false) return false;
		const values =
			Array.isArray(st?.values) && st.values.length > 0
				? st.values
				: ['default', 'flex', 'priority'];
		return values.includes('flex');
	};

	const getAllowedReasoningEffortsForModel = (modelId: string) =>
		getEffectiveReasoning($models.find((m) => m.id === modelId)).allowedEfforts;

	const getEffectiveReasoningForModel = (modelId: string, desired: { effort: string } | null) => {
		const effective = getEffectiveReasoning($models.find((m) => m.id === modelId));
		if (!effective.enabled) return null;
		const desiredEffort = desired?.effort;
		if (!desiredEffort) return null;
		const clamped = clampEffortToEffective(desiredEffort, effective);
		return clamped ? { effort: clamped } : null;
	};

	/**
	 * Schedule per-group timeouts that fire exactly when each group's reset time arrives.
	 * When a reset time is reached:
	 * 1. Immediately increment resetTrigger to force UI update (shows 0 via getEffectiveUsage)
	 * 2. Fetch fresh data from server to get the new next_reset_at
	 */
	const scheduleGroupResetChecks = () => {
		const now = Math.floor(Date.now() / 1000);

		for (const [groupName, groupData] of Object.entries(tokenUsageGroups)) {
			const nextReset = (groupData as any).next_reset_at;

			// Clear any existing timeout for this group
			const existingTimeout = resetTimeouts.get(groupName);
			if (existingTimeout) {
				clearTimeout(existingTimeout);
				resetTimeouts.delete(groupName);
			}

			if (nextReset && nextReset > now) {
				// Calculate ms until reset (no buffer - we want instant UI update)
				const msUntilReset = (nextReset - now) * 1000;

				// Don't schedule if more than 24 hours away (will be recalculated on next poll)
				if (msUntilReset > 0 && msUntilReset < 24 * 60 * 60 * 1000) {
					const timeout = setTimeout(async () => {
						console.log(
							`Token usage: reset time reached for "${groupName}", triggering UI update...`
						);

						// Increment trigger to force Svelte reactivity - UI will immediately show 0
						// because getEffectiveUsage() checks if now >= next_reset_at
						resetTrigger++;

						// Clean up this timeout from the map
						resetTimeouts.delete(groupName);

						// Fetch fresh data from server (includes new next_reset_at for rolling windows)
						// Small delay to ensure server has processed the reset
						setTimeout(async () => {
							await fetchTokenUsage();
						}, 500);
					}, msUntilReset);

					resetTimeouts.set(groupName, timeout);
				}
			}
		}
	};

	/**
	 * Clear all scheduled reset timeouts
	 */
	const clearAllResetTimeouts = () => {
		for (const [groupName, timeout] of resetTimeouts) {
			clearTimeout(timeout);
		}
		resetTimeouts.clear();
	};

	const fetchTokenUsage = async () => {
		try {
			const response = await fetch(`${WEBUI_BASE_URL}/api/usage/groups`, {
				method: 'GET',
				headers: {
					Authorization: `Bearer ${localStorage.token}`,
					'Content-Type': 'application/json'
				}
			});

			if (response.ok) {
				const data = await response.json();
				tokenUsageGroupsStore.set(data.groups || {});
				// Subscription-provider usage rides the same response. Guarded so
				// a backend that predates the field can't clobber bootstrap-seeded
				// state with an empty object.
				if (data.subscriptions && typeof data.subscriptions === 'object') {
					subscriptionUsageStore.set(data.subscriptions);
				}
				// Per-group reset timeouts are (re)scheduled by the reactive on
				// tokenUsageGroups below — one path for both this fetch and the
				// token-usage:update socket pushes.
			}
		} catch (error) {
			console.error('Error fetching token usage:', error);
		}
	};

	const formatTokensCompact = (n: number) => {
		if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(n >= 10_000_000 ? 0 : 1)}M`;
		if (n >= 1_000) return `${(n / 1_000).toFixed(n >= 10_000 ? 0 : 1)}k`;
		return `${n}`;
	};

	// While an earlier message is being edited on mobile AND the on-screen
	// keyboard is up, the bottom composer and token panels stand down: two
	// competing inputs starve the keyboard-shrunken viewport, and the edit
	// box's Save/Cancel row is the active "composer" for the duration.
	// Deliberately keyed on the keyboard, not just the edit: hiding the
	// composer while pinned at the absolute bottom SHRINKS max scrollTop, so
	// the browser clamps the position and the whole conversation visibly
	// settles downward with no scroll room for the edit anchor to restore
	// from. With the keyboard up the container is far shorter than the
	// content, so the geometry change is absorbed by the keyboard edit
	// placement (placeEditBoxForKeyboard) instead. Restored the moment either
	// condition ends.
	let keyboardShown = $state(false);
	// Set when the on-screen keyboard closes — the RO's composer-shrink
	// compensation stands down briefly so keyboard-close growth keeps its
	// deliberate "re-glue the tail to the freed screen bottom" behavior.
	let keyboardClosedAt = 0;

	// Entering edit mode is an explicit act on a specific message — stop
	// following the stream so the bottom-pin can't rearrange the view under the
	// editor. (The edit-entry anchors in editScroll.ts used to defer to the pin
	// when near the bottom, but editing the LAST message while sitting at the
	// bottom is the most common edit of all — the pin visibly yanked it.) Uses
	// the same disengage primitive as a scroll-up gesture; submit/regenerate
	// re-engage explicitly, and a reader who scrolls back to the bottom
	// re-engages via onScroll as always.
	let anyMessageEditing = $state(false);

	// TEMPORARY debug probe: the "bar snaps on clearing the input" bug is a
	// scrollTop change with no RO/pin/correction — i.e. an out-of-engine write.
	// Patch scrollIntoView + the container's scrollTop/scrollTo setters so the
	// culprit lands in __engineDebug WITH a stack trace. Install once; pushes
	// only happen while window.__engineDebug is set.
	const installScrollProbes = () => {
		const proto = Element.prototype as any;
		if (proto.__scrollProbesInstalled) return;
		proto.__scrollProbesInstalled = true;
		const push = (entry: any) => {
			const dbg = (window as any).__engineDebug;
			if (dbg) dbg.push({ t: performance.now(), ...entry });
		};
		const isMessagesContainer = (el: any) =>
			el === document.getElementById('messages-container');
		const containsMC = (el: any) => {
			const mc = document.getElementById('messages-container');
			return mc && (mc === el || mc.contains(el));
		};
		const origSIV = proto.scrollIntoView;
		proto.scrollIntoView = function (...args: any[]) {
			if (containsMC(this)) {
				push({ ev: 'siv', args: JSON.stringify(args[0] ?? null), stack: new Error().stack });
			}
			return origSIV.apply(this, args);
		};
		const origScrollTo = proto.scrollTo;
		for (const name of ['scrollTo', 'scroll', 'scrollBy', 'scrollIntoViewIfNeeded']) {
			const orig = proto[name];
			if (!orig) continue;
			proto[name] = function (...args: any[]) {
				if (isMessagesContainer(this)) {
					push({ ev: name, args: JSON.stringify(args[0] ?? args), stack: new Error().stack });
				}
				return orig.apply(this, args);
			};
		}
		// Focus tracking: a native focus-reveal scroll (element inside the
		// container getting focused) has no JS-visible call — the only trace
		// is the focus events themselves.
		const describe = (el: any) =>
			el
				? `${el.tagName}#${el.id || ''}.${String(el.className ?? '').slice(0, 60)}`
				: String(el);
		window.addEventListener('focusin', (e) => push({ ev: 'focusin', target: describe(e.target) }), true);
		window.addEventListener('focusout', (e) => push({ ev: 'focusout', target: describe(e.target) }), true);
		// Input-event correlation: if a wheel/touch/keyboard event precedes the
		// mystery scroll by a frame, that's the cause; if the scroll follows a
		// backspace with NOTHING in between, it's a native editing behavior.
		window.addEventListener(
			'keydown',
			(e) => push({ ev: 'kd', key: (e as KeyboardEvent).key, target: describe(e.target) }),
			true
		);
		window.addEventListener(
			'wheel',
			(e) =>
				push({
					ev: 'wheel',
					deltaY: Math.round((e as WheelEvent).deltaY),
					target: describe(e.target)
				}),
			{ capture: true, passive: true }
		);
		window.addEventListener(
			'pointerdown',
			(e) => push({ ev: 'ptrdown', target: describe(e.target) }),
			true
		);
		// Selection-API tracking: a native caret/selection reveal (Chrome
		// scrolling a freshly-set DOM selection into view) never touches
		// scrollTop/scrollTo/scrollIntoView in JS — but it DOES go through
		// these. The stack identifies the caller (ProseMirror, browser, app).
		const selProto = (window as any).Selection?.prototype;
		if (selProto && !selProto.__selectionProbesInstalled) {
			selProto.__selectionProbesInstalled = true;
			const safeArgs = (args: any[]) =>
				args.map((a: any) =>
					a instanceof Node ? `${a.nodeName}#${(a as Element).id ?? ''}` : a
				);
			for (const name of ['collapse', 'addRange', 'extend', 'modify']) {
				const orig = selProto[name];
				if (!orig) continue;
				selProto[name] = function (...args: any[]) {
					if (this === window.getSelection()) {
						push({
							ev: `sel-${name}`,
							args: JSON.stringify(safeArgs(args).slice(0, 2)),
							stack: new Error().stack
						});
					}
					return orig.apply(this, args);
				};
			}
		}
		const focusOrig = (HTMLElement.prototype as any).focus;
		if (focusOrig && !(HTMLElement.prototype as any).__focusProbed) {
			(HTMLElement.prototype as any).__focusProbed = true;
			(HTMLElement.prototype as any).focus = function (options?: any) {
				push({ ev: 'focus()', target: describe(this), opts: JSON.stringify(options ?? null), stack: new Error().stack });
				return focusOrig.call(this, options);
			};
		}
		const desc = Object.getOwnPropertyDescriptor(proto, 'scrollTop');
		if (desc?.set) {
			Object.defineProperty(proto, 'scrollTop', {
				...desc,
				set(v: number) {
					if (isMessagesContainer(this)) {
						push({ ev: 'st-write', v, stack: new Error().stack });
					}
					return desc.set!.call(this, v);
				}
			});
		}
		// Capture-phase scroll listener: catches scrolls on ANY element (the
		// flex column above the container, document, window) — the container's
		// own onscroll only covers itself. Rect snapshots show WHAT moved.
		const logRects = (ev: string, target: any) => {
			const dbg = (window as any).__engineDebug;
			if (!dbg) return;
			const mc = document.getElementById('messages-container');
			const msgs = document.querySelectorAll('[id^="message-"]');
			const last = msgs.length ? msgs[msgs.length - 1] : null;
			dbg.push({
				t: performance.now(),
				ev,
				target: target?.id || target?.tagName || String(target?.nodeName),
				containerTop: mc?.getBoundingClientRect().top,
				containerBottom: mc?.getBoundingClientRect().bottom,
				containerScrollTop: mc?.scrollTop,
				composerTop: composerElement?.getBoundingClientRect().top,
				lastMsgBottom: last?.getBoundingClientRect().bottom,
				vvOffsetTop: window.visualViewport?.offsetTop,
				vvHeight: window.visualViewport?.height,
				winScrollY: window.scrollY
			});
		};
		window.addEventListener(
			'scroll',
			(e) => logRects('any-scroll', e.target),
			{ capture: true, passive: true }
		);
		window.visualViewport?.addEventListener('scroll', () =>
			logRects('vv-scroll', 'visualViewport')
		);
		window.visualViewport?.addEventListener('resize', () =>
			logRects('vv-resize', 'visualViewport')
		);
		// Rect sampler: even a movement with NO scroll event, NO resize and NO
		// scroll write (pure layout/paint-level shift) shows up here as a
		// before/after rect delta. Runs only while __engineDebug is set.
		let lastRectSnapshot = '';
		setInterval(() => {
			if (!(window as any).__engineDebug) return;
			const mc = document.getElementById('messages-container');
			// Real message ROWS have id="message-<uuid>"; exclude composer/edit ids.
			const rows = [...document.querySelectorAll('[id^="message-"]')].filter(
				(el) => !/^(message-input|message-edit|message-index)/.test(el.id)
			);
			const last: any = rows.at(-1);
			const buttons = last?.querySelector?.('.buttons');
			const column = mc?.parentElement;
			const navbar = document.getElementById('chat-navbar');
			const snap = JSON.stringify({
				mcTop: Math.round(mc?.getBoundingClientRect().top ?? -1),
				mcBottom: Math.round(mc?.getBoundingClientRect().bottom ?? -1),
				mcST: Math.round(mc?.scrollTop ?? -1),
				colST: Math.round(column?.scrollTop ?? -1),
				colTop: Math.round(column?.getBoundingClientRect().top ?? -1),
				navBottom: Math.round(navbar?.getBoundingClientRect().bottom ?? -1),
				compTop: Math.round(composerElement?.getBoundingClientRect().top ?? -1),
				rowId: last?.id,
				rowTop: Math.round(last?.getBoundingClientRect().top ?? -1),
				rowBottom: Math.round(last?.getBoundingClientRect().bottom ?? -1),
				barTop: Math.round(buttons?.getBoundingClientRect().top ?? -1),
				barBottom: Math.round(buttons?.getBoundingClientRect().bottom ?? -1)
			});
			if (snap !== lastRectSnapshot) {
				lastRectSnapshot = snap;
				push({ ev: 'rect', ...JSON.parse(snap) });
			}
		}, 100);
	};

	onMount(() => {
		installScrollProbes();
		// Initial fetch only; backend pushes `token-usage:update` socket
		// events for subsequent changes. Deferred to idle time (not on the
		// critical boot path) and skipped entirely while offline — there's
		// nothing to fetch and it'd just be a wasted network attempt.
		if (typeof navigator === 'undefined' || navigator.onLine) {
			const runFetchTokenUsage = () => fetchTokenUsage();
			if (typeof requestIdleCallback === 'function') {
				requestIdleCallback(runFetchTokenUsage);
			} else {
				setTimeout(runFetchTokenUsage, 1);
			}
		}
		// Tick once a minute so the off-peak window check re-evaluates as the
		// hour boundary crosses (1pm/5am PST).
		_nowTickInterval = setInterval(() => {
			_nowTick = Date.now();
		}, 60_000);
	});

	onDestroy(() => {
		clearAllResetTimeouts();
		stopResumeTaskPolling({ force: true });
		if (_nowTickInterval) {
			clearInterval(_nowTickInterval);
			_nowTickInterval = null;
		}
	});

	const navigateHandler = async () => {
		const myGeneration = ++navigateGeneration;
		// Determine BEFORE flipping the flag: is this the component's first ever
		// navigateHandler run (cold load / deep link / hard reload — chatIdProp's
		// reactive statement also fires once on component construction) or a
		// subsequent run on an already-mounted instance (soft in-app navigation,
		// e.g. a sidebar click)? Only the latter is eligible for the zero-network
		// cache-tier race in loadChat.
		const isSoftNav = hasCompletedFirstNavigate;
		hasCompletedFirstNavigate = true;
		loading = true;
		initialScrollSettled = false;
		stopSubagentUpdateBatching();
		// Chat switch: the outgoing chat's drain bridge is not this view's problem
		// (the `queue = []` below drops it anyway), so this stop is unconditional.
		clearQueueDrainPending();
		stopResumeTaskPolling({ force: true });
		// No generation state to reset here: `generating` and `taskIds` are both
		// derived per chat id from the lifecycle registry, so switching chats
		// re-evaluates them for the new target. (Clearing them by hand is what a
		// completed chat used to need in order not to inherit the previous chat's
		// live input mode.)
		clearMessageRevisionStores();
		resetChatParamTracking();

		prompt = '';
		messageInput?.setText('');

		files = [];
		selectedToolIds = [];
		selectedFilterIds = [];
		webSearchEnabled = false;
		subagentsEnabled = false;
		subagentReasoningEffort = '';
		subagentServiceTier = '';
		subagentModel = '';
		subagentExternalToolsEnabled = true;
		imageGenerationEnabled = false;

		// Clear the queue from the previous chat. loadChat will re-populate from
		// chatContent.queue if there's a persisted queue on the chat we navigate to.
		queue = [];
		_wasGenerating = false;

		// Reset auto-flip state so the new chat re-evaluates the off-peak /
		// threshold triggers and can re-show the toast.
		serviceTierUserTouched = false;

		const storageChatInput = sessionStorage.getItem(
			`chat-input${chatIdProp ? `-${chatIdProp}` : ''}`
		);

		if (chatIdProp && (await loadChat(myGeneration, true, isSoftNav))) {
			await tick();
			loading = false;
			// Restore this model's persisted service tier now that loadChat has
			// settled `selectedModels` — see `restoreServiceTierForModel`.
			if (selectedModelIds.length === 1) {
				restoreServiceTierForModel(selectedModelIds[0]);
			}
			// Keep the messages content invisible while it lays out and we pin it
			// to the bottom; gate the pagination loader until settled.
			messagesReady = false;
			initialScrollSettled = false;
			cancelGlide(); // a stale glide must never steer the incoming chat
			clearExpansionHold(); // ...nor a hold armed in the chat we just left

			// Let #messages-container + messagesContentElement mount, then run a
			// layout-driven settle loop that re-pins to the true bottom as each
			// band of content-visibility:auto messages realizes its real height.
			await tick();
			settleInterrupted = false;
			// Stamp every rendered turn's REAL height now, while the content is
			// still hidden. Without this the async sweeper (whose scroll-quiet
			// gate treats the settle's own pin writes as scrolling) always ran
			// AFTER the reveal, and each of its 300ms chunks grew the 150px
			// placeholders visibly — the "chat jumps a few times then settles"
			// stutter on open. With final heights in place the settle below
			// converges immediately and nothing moves post-reveal.
			messageHeightSweeper.sweepAllNow();
			await settleAtBottom(myGeneration);
			if (myGeneration !== navigateGeneration) return;

			messagesReady = true; // reveal — already at the bottom, no visible motion
			initialScrollSettled = true; // allow the pagination loader to grow the window
			// Pin to the bottom for streaming / late additions — unless the user
			// already pulled away during the reveal, in which case honor that.
			if (!settleInterrupted) autoScroll = true;
			// A reader who interrupted the settle owns the position from frame
			// one — arm the engine now (their disengage ran while messagesReady
			// was false, so its own capture was a no-op).
			if (!autoScroll) captureScrollCorrectionAnchor();

			await tick();

			if (storageChatInput) {
				try {
					const input = JSON.parse(storageChatInput);

					if (!$temporaryChatEnabled) {
						messageInput?.setText(input.prompt);
						files = input.files;
						// Tool/feature state from a draft is only restored when the USER
						// curated it (toolSelectionDirty was saved with the draft).
						// Drafts also capture programmatic state — restoring that
						// unconditionally made a once-enabled tool (e.g. the container
						// server) sticky forever, overriding the per-model admin defaults.
						if (input.toolSelectionDirty) {
							if (input.selectedToolIds && input.selectedToolIds.length > 0) {
								selectedToolIds = input.selectedToolIds;
							}
							selectedFilterIds = input.selectedFilterIds;
							imageGenerationEnabled = input.imageGenerationEnabled;
							studyModeEnabled = input.studyModeEnabled ?? false;
							dataVizEnabled = input.dataVizEnabled ?? false;
							automationsEnabled = input.automationsEnabled ?? false;
							toolSelectionDirty = true;
						}
					}
				} catch (e) {}
			}

			if (!isOnScreenKeyboardDevice()) {
				const chatInput = document.getElementById('chat-input');
				chatInput?.focus({ preventScroll: true });
			}
		} else {
			await goto('/');
		}
	};

	const onSelect = async (e) => {
		const { type, data } = e;

		if (type === 'prompt') {
			// Handle prompt selection
			messageInput?.setText(data, async () => {
				if (!($settings?.insertSuggestionPrompt ?? false)) {
					await tick();
					submitPrompt(prompt);
				}
			});
		}
	};

	const arraysEqual = (a: string[], b: string[]) =>
		a.length === b.length && a.every((v, i) => v === b[i]);

	// ModelSelector is controlled by this component: a picker click reaches this
	// function directly instead of travelling through an indexed, multi-level
	// two-way binding chain. The assignment and intent revision therefore become
	// one synchronous state transition.
	const handleSelectedModelsChange = (modelIds: string[]) => {
		selectedModels = [...modelIds];
		modelSelectionRevision += 1;
	};

	const saveSessionSelectedModels = () => {
		const selectedModelsString = JSON.stringify(selectedModels);
		if (
			selectedModels.length === 0 ||
			(selectedModels.length === 1 && selectedModels[0] === '') ||
			sessionStorage.selectedModels === selectedModelsString
		) {
			return;
		}
		sessionStorage.selectedModels = selectedModelsString;
		console.log('saveSessionSelectedModels', selectedModels, sessionStorage.selectedModels);
	};

	// Serialize picker-only writes. Two rapid selections must reach the server in
	// the same order as the user's clicks; otherwise an older, slower PATCH can
	// finish last and silently restore the previous model on reload.
	let selectedModelsPersistTail: Promise<void> = Promise.resolve();
	const queuedSelectedModelsPersistKeys = new Set<string>();

	const persistSelectedModelsForChat = () => {
		const visibleChatId = getVisibleChatId();
		if (
			loading ||
			// A provisional (local-copy) open may carry stale models until the
			// network revalidation lands — persisting from that snapshot would
			// PATCH the chat back to an old model set.
			chatRevalidating ||
			$temporaryChatEnabled ||
			!visibleChatId ||
			visibleChatId.startsWith('local:') ||
			selectedModels.length === 0 ||
			selectedModels.some((id) => !id)
		) {
			return;
		}

		const key = selectedModelsPersistKey(visibleChatId);
		if (
			!key ||
			key === lastPersistedSelectedModelsKey ||
			queuedSelectedModelsPersistKeys.has(key)
		) {
			return;
		}

		const modelIds = cloneState(selectedModels ?? []);
		queuedSelectedModelsPersistKeys.add(key);
		selectedModelsPersistTail = selectedModelsPersistTail
			.catch(() => undefined)
			.then(async () => {
				if (key === lastPersistedSelectedModelsKey) return;
				await saveChatHandler(visibleChatId, history, params, [
					{ op: 'set_models', models: modelIds }
				]);
				if (getVisibleChatId() === visibleChatId) {
					lastPersistedSelectedModelsKey = key;
				}
			})
			.catch((error) => {
				console.error('Failed to persist selected models', error);
			})
			.finally(() => {
				queuedSelectedModelsPersistKeys.delete(key);
			});
	};

	let oldSelectedModelIds = $state(['']);

	// Called from the message-input toggles whenever the USER explicitly turns a
	// tool/feature on or off. This is the dependable signal that the selection is
	// intentional (vs. inferring from state diffs, which can't tell a user toggle
	// from a programmatic default-apply).
	let liveToolSelectionRevision = $state(0);
	let liveToolSelectionOperationVersion = $state(Date.now() * 1000);
	let liveToolSelectionPending = $state(false);
	const liveToolSelectionSentByTask = new Map<string, string>();

	const markToolSelectionDirty = () => {
		toolSelectionDirty = true;
		liveToolSelectionPending = true;
		liveToolSelectionOperationVersion = Math.max(
			liveToolSelectionOperationVersion + 1,
			Date.now() * 1000
		);
		liveToolSelectionRevision += 1;
	};

	const liveToolFeatureFlags = () => {
		const features = getFeatures() as Record<string, any>;
		return {
			web_search: !!features.web_search,
			study_mode: !!features.study_mode,
			data_viz: !!features.data_viz,
			subagents: !!features.subagents
		};
	};

	const liveToolSelectionKey = () =>
		JSON.stringify({
			tool_ids: [...(selectedToolIds ?? [])].sort(),
			features: liveToolFeatureFlags(),
			subagentExternalToolsEnabled: !!subagentExternalToolsEnabled
		});

	const toolSelectionLabel = (toolId: string) => {
		if (toolId.startsWith('direct_server:')) {
			const serverIndex = Number(toolId.slice('direct_server:'.length));
			const server = Number.isInteger(serverIndex) ? ($toolServers ?? [])[serverIndex] : null;
			return server?.info?.title ?? server?.name ?? server?.url ?? toolId;
		}
		const tool = ($tools ?? []).find((item) => item?.id === toolId);
		return tool?.name ?? tool?.meta?.name ?? toolId;
	};

	const buildToolSelectionEnvelope = (
		selectionIds: string[],
		resolvedToolServers: any[],
		features: Record<string, any> = liveToolFeatureFlags(),
		externalToolsEnabled = subagentExternalToolsEnabled,
		revision = liveToolSelectionOperationVersion
	) => {
		const normalizedFeatures = {
			web_search: !!features.web_search,
			study_mode: !!features.study_mode,
			data_viz: !!features.data_viz,
			subagents: !!features.subagents
		};
		const toolIds = selectionIds.filter((id) => !id.startsWith('direct_server:'));
		const enabledFeatureIds = Object.entries(normalizedFeatures)
			.filter(([, enabled]) => enabled)
			.map(([feature]) => `feature:${feature}`);
		const labels: Record<string, string> = {};
		for (const toolId of selectionIds) labels[toolId] = toolSelectionLabel(toolId);
		labels['feature:web_search'] = $i18n.t('Web Search');
		labels['feature:study_mode'] = $i18n.t('Study Mode');
		labels['feature:data_viz'] = $i18n.t('Data Visualization');
		labels['feature:subagents'] = $i18n.t('Subagents');
		return {
			operation_id: uuidv4(),
			revision,
			selection_ids: [...selectionIds, ...enabledFeatureIds],
			tool_ids: toolIds,
			tool_servers: cloneState(resolvedToolServers ?? []),
			features: normalizedFeatures,
			labels,
			params: {
				subagentExternalToolsEnabled: !!externalToolsEnabled
			}
		};
	};

	const resolveSelectedDirectToolServers = async (
		selectionIds: string[] = selectedToolIds as string[]
	) => {
		const directIds = selectionIds
			.filter((id) => id.startsWith('direct_server:'))
			.map((id) => id.slice('direct_server:'.length))
			.map((id) => (!Number.isNaN(Number(id)) ? Number(id) : id));
		if (directIds.length === 0) return [];
		await loadToolServers();
		const resolved = ($toolServers ?? []).filter(
			(server, index) => directIds.includes(index) || directIds.includes(server?.id)
		);
		if (resolved.length < directIds.length) {
			throw new Error($i18n.t('Failed to load selected tool servers.'));
		}
		return resolved;
	};

	const syncLiveToolSelection = async (expectedKey: string) => {
		const chatIdToUpdate = getVisibleChatId();
		if (!chatIdToUpdate || !$socket?.connected) return;
		const operationVersion = liveToolSelectionOperationVersion;
		const selectionIds = [...(selectedToolIds ?? [])];
		const features = liveToolFeatureFlags();
		const externalToolsEnabled = subagentExternalToolsEnabled;
		const records = generationLifecycles.activeForChat(chatIdToUpdate);
		const targets = records.flatMap((record) =>
			[...record.taskIds].map((taskId) => ({
				taskId,
				messageId: record.messageId
			}))
		);
		const unsent = targets.filter(
			({ taskId }) => liveToolSelectionSentByTask.get(taskId) !== expectedKey
		);
		if (unsent.length === 0) {
			liveToolSelectionPending = false;
			return;
		}

		let selectedServers: any[];
		try {
			selectedServers = await resolveSelectedDirectToolServers(selectionIds);
		} catch (error) {
			toast.error(`${error}`);
			return;
		}
		// Direct-server discovery is asynchronous. If the user changed the
		// selection while it was loading, the newer effect owns the operation;
		// never mark or send this now-stale snapshot.
		if (
			liveToolSelectionKey() !== expectedKey ||
			liveToolSelectionOperationVersion !== operationVersion
		) {
			return;
		}
		const selection = buildToolSelectionEnvelope(
			selectionIds,
			selectedServers,
			features,
			externalToolsEnabled,
			operationVersion
		);

		await Promise.all(
			unsent.map(async ({ taskId, messageId }) => {
				// Mark before the await: a second toggle gets a different key and
				// immediately queues a replacement operation instead of waiting
				// behind this ack.
				liveToolSelectionSentByTask.set(taskId, expectedKey);
				const result: any = await emitSocketAck(
					'tool-selection:update',
					{
						chat_id: chatIdToUpdate,
						message_id: messageId,
						task_id: taskId,
						selection
					},
					5000
				);
				if (!result?.status && liveToolSelectionSentByTask.get(taskId) === expectedKey) {
					liveToolSelectionSentByTask.delete(taskId);
					liveToolSelectionRevision += 1;
				}
			})
		);

		const latestRecords = generationLifecycles.activeForChat(chatIdToUpdate);
		const everyActiveTaskUpdated =
			latestRecords.length > 0 &&
			latestRecords.every(
				(record) =>
					record.taskIds.size > 0 &&
					[...record.taskIds].every(
						(taskId) => liveToolSelectionSentByTask.get(taskId) === liveToolSelectionKey()
					)
			);
		if (everyActiveTaskUpdated) liveToolSelectionPending = false;
	};

	const onSelectedModelIdsChange = () => {
		// While a chat is loading/navigating, `selectedModels` is being set from
		// the persisted chat and the toolSelectionDirty flag + selection are
		// restored separately. Re-applying model defaults here would race that
		// restore (and run against a stale dirty flag carried over from the
		// previous chat), so only react to genuine user-driven model switches in
		// an already-loaded chat.
		if (loading) {
			oldSelectedModelIds = selectedModelIds;
			return;
		}
		if (oldSelectedModelIds.filter((id) => id).length > 0) {
			// A genuine user-driven switch (not a composer remount, guarded above
			// by `loading`) — restore this model's persisted tier preference.
			if (selectedModelIds.length === 1) {
				restoreServiceTierForModel(selectedModelIds[0]);
			}
			if (toolSelectionDirty) {
				// The user has curated their tools/features for this chat. Keep
				// their selection across the model switch — only turn OFF the
				// capability-gated features the new model can't do, so we never
				// send a feature the model doesn't support.
				const model = atSelectedModel ?? $models.find((m) => m.id === selectedModels[0]);
				const caps = model?.info?.meta?.capabilities ?? {};
				if (webSearchEnabled && (caps.web_search ?? true) === false) {
					webSearchEnabled = false;
				}
				if (imageGenerationEnabled && (caps.image_generation ?? true) === false) {
					imageGenerationEnabled = false;
				}
			} else {
				// No manual curation yet — apply the newly selected model's
				// defaults (preserving the prior web-search behavior).
				const _webSearchEnabled = webSearchEnabled;
				resetInput();

				if (_webSearchEnabled) {
					const model = atSelectedModel ?? $models.find((m) => m.id === selectedModels[0]);
					if (model?.info?.meta?.capabilities?.web_search ?? true) {
						webSearchEnabled = true;
					}
				}
			}
		}
		oldSelectedModelIds = selectedModelIds;
	};

	const resetInput = () => {
		selectedToolIds = [];
		selectedFilterIds = [];
		toolSelectionDirty = false;
		webSearchEnabled = false;
		studyModeEnabled = false;
		dataVizEnabled = false;
		automationsEnabled = false;
		subagentsEnabled = false;
		subagentReasoningEffort = '';
		subagentServiceTier = '';
		subagentModel = '';
		subagentExternalToolsEnabled = true;
		subagentLiveStates.set({});
		questionStates.set({});
		reasoningBlockOpenState.set({});
		messageEditingIds.set(new Set());
		messageHeightSweeper.reset();
		browserLiveStates.set({});
		showBrowserPanel.set(false);
		browserPanelDismissed.set(false);
		clearMessageRevisionStores();
		imageGenerationEnabled = false;

		serviceTierUserTouched = false;

		setDefaults();
	};

	const setDefaults = async () => {
		if (!$tools) {
			tools.set(await getTools(localStorage.token));
		}
		if (!$functions) {
			functions.set(await getFunctions(localStorage.token));
		}
		if (selectedModels.length !== 1 && !atSelectedModel) {
			return;
		}

		// Defaults never beat explicit user curation. This also covers the
		// first-load race where the getTools await above resolves AFTER a URL
		// param / draft restore has already populated a curated selection.
		if (toolSelectionDirty) {
			return;
		}

		const model = atSelectedModel ?? $models.find((m) => m.id === selectedModels[0]);
		if (model) {
			// Per-model defaults (admin "Default Tools & Features" panel) are the
			// single source of truth for what starts enabled in a new chat with
			// this model. Applied when the user hasn't curated the selection —
			// and an EMPTY admin selection means nothing starts on, so unchecking
			// a tool (e.g. the container server) actually turns it off instead of
			// leaving a stale selection behind.
			selectedToolIds = [
				...new Set(
					[...(model?.info?.meta?.toolIds ?? [])].filter((id) => $tools.find((t) => t.id === id))
				)
			];

			// Set Default Filters (Toggleable only)
			if (model?.info?.meta?.defaultFilterIds) {
				selectedFilterIds = model.info.meta.defaultFilterIds.filter((id) =>
					model?.filters?.find((f) => f.id === id)
				);
			}

			// Builtin feature defaults. Capability-gated features additionally
			// require the model capability; every feature requires its instance-
			// level admin flag (all are re-checked server-side at send time too).
			const defaultFeatureIds = model?.info?.meta?.defaultFeatureIds ?? [];
			const instanceFeatures = $config?.features ?? {};

			imageGenerationEnabled =
				(model.info?.meta?.capabilities?.['image_generation'] ?? false) &&
				(instanceFeatures.enable_image_generation ?? false) &&
				defaultFeatureIds.includes('image_generation');

			webSearchEnabled =
				(model.info?.meta?.capabilities?.['web_search'] ?? false) &&
				(instanceFeatures.enable_web_search ?? false) &&
				defaultFeatureIds.includes('web_search');

			subagentsEnabled =
				(instanceFeatures.enable_subagents ?? false) &&
				($user?.role === 'admin' || $user?.permissions?.features?.subagents !== false) &&
				defaultFeatureIds.includes('subagents');

			dataVizEnabled =
				(instanceFeatures.enable_data_viz ?? false) && defaultFeatureIds.includes('data_viz');

			automationsEnabled =
				(instanceFeatures.enable_automations ?? false) && defaultFeatureIds.includes('automations');

			studyModeEnabled =
				(instanceFeatures.enable_study_mode ?? false) && defaultFeatureIds.includes('study_mode');
		}
	};

	const parseChatIdFromPath = (pathname = '') => {
		const match = pathname.match(/^\/c\/([^/?#]+)/);
		return match?.[1] ? decodeURIComponent(match[1]) : '';
	};

	const resolveRouteChatId = () => {
		const fromPage = parseChatIdFromPath($page.url.pathname);
		if (fromPage) return fromPage;
		const browserPathname = typeof window !== 'undefined' ? window.location.pathname : '';
		return parseChatIdFromPath(browserPathname) || chatIdProp || '';
	};

	const isPersistentChatView = () => {
		const browserPathname = typeof window !== 'undefined' ? window.location.pathname : '';
		return browserPathname.includes('/c/') || $page.url.pathname.includes('/c/');
	};

	let routeChatId = $state('');
	let activeChatId = $state('');

	const getVisibleChatId = () => {
		const currentRouteChatId = resolveRouteChatId();
		if (currentRouteChatId) {
			return currentRouteChatId;
		}

		const currentChatId = $chatId ?? '';
		return $temporaryChatEnabled || currentChatId.startsWith('local:') || isPersistentChatView()
			? currentChatId
			: '';
	};

	const getDraftChatId = () => getVisibleChatId() || null;

	const isVisibleChatEvent = (eventChatId) => {
		if (!eventChatId) {
			return false;
		}

		return getVisibleChatId() === eventChatId;
	};

	// --- verification handoff re-open guard ------------------------------------
	// A human-verification handoff is BLOCKING: while any browser session has
	// requiresHuman set, the panel is the only way to solve (or dismiss) it.
	// If the user closes the panel mid-handoff, nothing else would ever reopen
	// it — once the model pauses there are no more live frames, and the
	// auto-open path honors the dismissal flag. So while a handoff is armed and
	// the panel is hidden, poll the daemon's live state (host-file read, cheap)
	// and reopen the panel until the challenge is resolved or dismissed.
	// Closing the panel is a visual preference, not a way to abandon the agent
	// mid-challenge; the "No challenge here" button is the abandonment hatch.
	$effect(() => {
		const visibleChatId = getVisibleChatId();
		const anyRequired = Object.values($browserLiveStates).some((s) => s?.requiresHuman === true);
		if (!visibleChatId || !anyRequired || $showBrowserPanel) return;
		const watch = setInterval(async () => {
			const res = await browserLiveFrame(localStorage.token, visibleChatId).catch(() => null);
			if (!res) return;
			const entries = res?.sessions ?? (res?.session ? [res] : []);
			if (entries.some((s) => s?.requiresHuman === true)) {
				browserPanelDismissed.set(false);
				showBrowserPanel.set(true);
				showControls.set(true);
			}
		}, 2500);
		return () => clearInterval(watch);
	});

	const getPendingAssistantMessageIds = () => {
		return Object.entries(history.messages)
			.filter(([, message]) => message.role === 'assistant' && message.done !== true)
			.map(([messageId]) => messageId);
	};

	const resolveChatEventMessageId = (eventMessageId) => {
		if (eventMessageId && history.messages[eventMessageId]) {
			return eventMessageId;
		}

		// If the event explicitly named a message id that we don't have, it's a
		// stale event from a previous request — DON'T retarget it to the current
		// pending message. That retargeting was the source of multiple latent
		// bugs where a delayed cancel/error from the prior turn would stomp on
		// the in-flight follow-up.
		if (eventMessageId) {
			chatStreamDebug('[chat-stream] dropping stale event with unmatched message_id', {
				eventMessageId
			});
			return null;
		}

		// Event has no message_id (legacy/back-compat behavior); fall back to
		// the only pending assistant message if there's exactly one.
		const pendingAssistantMessageIds = getPendingAssistantMessageIds();

		return pendingAssistantMessageIds.length === 1 ? pendingAssistantMessageIds[0] : null;
	};

	// Human-friendly label for a browser session tab. The parent agent's session
	// is "main"; a subagent's session is its subagent_id, so look it up in
	// subagentLiveStates to show the subagent's name. Returns '' when we can't
	// resolve a nicer label (the panel then falls back to the raw session id).
	const browserSessionLabel = (session: string | undefined): string => {
		if (!session) return '';
		if (session === 'main') return $i18n.t('Main');
		const run = findSubagentRunEntry(get(subagentLiveStates), '', [session])?.[1];
		if (run?.name) {
			return run.num ? `${run.name} (#${run.num})` : run.name;
		}
		return '';
	};

	const markPendingAssistantMessagesDone = () => {
		const pendingAssistantMessageIds = getPendingAssistantMessageIds();

		for (const messageId of pendingAssistantMessageIds) {
			history.messages[messageId] = {
				...history.messages[messageId],
				done: true
			};
		}

		if (pendingAssistantMessageIds.length > 0) {
			history = { ...history };
		}
	};

	const showMessage = async (
		message,
		ignoreSettings = false,
		opts: { suppressScroll?: boolean } = {}
	) => {
		await tick();

		const _chatId = getVisibleChatId();
		const requestedMessageId = message?.id ?? null;
		const rootIds = Object.keys(history.messages ?? {}).filter(
			(id) => history.messages[id]?.parentId == null
		);
		const branchStartId = requestedMessageId ?? rootIds.at(-1) ?? null;
		let _messageId = findDeepestBranchLeaf(history.messages ?? {}, branchStartId);

		if (!_messageId) return;

		// Overview and sibling arrows can select a lean stub from the all-branch
		// manifest. Hydrate the selected leaf before flipping currentId so the
		// branch never renders as an empty conversation while pagination catches
		// up. Preserve every sibling stub already present in the map.
		if (
			_chatId &&
			!_chatId.startsWith('local:') &&
			(history.messages[_messageId]?._stub ||
				(branchStartId ? history.messages[branchStartId]?._stub : false))
		) {
			const page = await getChatMessagesBranch(localStorage.token, _chatId, {
				leaf: _messageId,
				limit: 25
			}).catch(() => null);
			const incoming = Array.isArray(page)
				? page
				: Array.isArray(page?.messages)
					? page.messages
					: [];
			for (const hydrated of incoming) {
				if (!hydrated?.id) continue;
				history.messages[hydrated.id] = {
					...(history.messages[hydrated.id] ?? {}),
					...hydrated,
					_stub: false
				};
			}
		}

		history.currentId = _messageId;

		await tick();
		await tick();
		await tick();

		// `suppressScroll` lets a caller (e.g. deleteMessage) own the viewport
		// itself — it does the currentId navigation + persistence here but skips
		// BOTH the scrollIntoView yank and the position-based autoScroll write, so
		// the caller can preserve the user's prior scroll anchor instead.
		if (!opts.suppressScroll && (($settings?.scrollOnBranchChange ?? true) || ignoreSettings)) {
			const messageElement = document.getElementById(`message-${message.id}`);
			if (messageElement) {
				// Retargeting glide, NOT native scrollIntoView({smooth}): the
				// native animation dies to any other scrollTop write (engine
				// corrections, pins) and aims at a stale target when sibling
				// content realizes mid-flight.
				glideToMessage(message.id);
			}
			// Navigating to a branch message that isn't the conversation leaf means
			// the user wants to read here, not tail the bottom — stop following so a
			// concurrent stream / late content can't yank them off it. (The old
			// position-based onScroll used to disengage here as a side effect.)
			if (message.id !== _messageId) autoScroll = false;
		}

		await tick();
		saveChatHandler(
			_chatId,
			history,
			params,
			history?.currentId ? [{ op: 'set_history_current_id', current_id: history.currentId }] : []
		);
	};

	const extractSubagentFinalText = (contentBlocks: any[] | undefined, content = '') => {
		if (typeof content === 'string' && content.trim()) return content.trim();
		if (!Array.isArray(contentBlocks)) return '';
		const textBlocks: string[] = [];
		for (const block of contentBlocks) {
			if (block?.type === 'text' && typeof block?.content === 'string' && block.content.trim()) {
				textBlocks.push(block.content.trim());
			}
		}
		return textBlocks.join('\n\n').trim();
	};

	const patchParentSubagentRuns = (runs: any[] = []) => {
		if (!Array.isArray(runs) || runs.length === 0 || !history.messages) return;

		let changed = false;
		const nextMessages: Record<string, any> = { ...(history.messages ?? {}) };

		for (const run of runs) {
			const parentMessageId = run?.parent_message_id as string | undefined;
			const entryKey = (run?.entry_key || run?.subagent_id || run?.chat_id || run?.tool_call_id) as
				| string
				| undefined;
			if (!parentMessageId || !entryKey || !nextMessages[parentMessageId]) continue;

			const parentMessage = nextMessages[parentMessageId];
			const existingRuns =
				parentMessage.subagent_runs && typeof parentMessage.subagent_runs === 'object'
					? parentMessage.subagent_runs
					: {};
			const prior =
				existingRuns[entryKey] && typeof existingRuns[entryKey] === 'object'
					? existingRuns[entryKey]
					: {};

			// `live` is a session-only marker (see SubagentRun.live). Strip it
			// from the copy that lands in history.messages so it never persists
			// to the DB — otherwise a reload would treat a stale `running` row as
			// authoritative and never fall back to terminal placeholder evidence.
			const { live: _live, ...runForHistory } = run ?? {};

			nextMessages[parentMessageId] = {
				...parentMessage,
				subagent_runs: {
					...existingRuns,
					[entryKey]: {
						...prior,
						...runForHistory,
						entry_key: entryKey
					}
				}
			};
			changed = true;
		}

		if (changed) {
			history = { ...history, messages: nextMessages };
		}
	};

	const patchParentSubagentRun = (run: any) => {
		patchParentSubagentRuns([run]);
	};

	const SUBAGENT_UI_FLUSH_MS = 500;
	const SUBAGENT_STATUS_HISTORY_LIMIT = 20;
	const SUBAGENT_PARENT_PATCH_INTERVAL_MS = 5000;

	type PendingSubagentUpdate = {
		keys: string[];
		sd: any;
		latestCompletion?: any;
		terminalEvent?: any;
		statuses: any[];
		hasTerminal: boolean;
		deltas?: any[];
		toolResults?: any[];
		doneEvent?: any;
	};

	let pendingSubagentUpdates = new Map<string, PendingSubagentUpdate>();
	let subagentUpdateFlushTimer: ReturnType<typeof setTimeout> | null = null;
	let lastSubagentParentPatchAt = 0;

	const getSubagentKeys = (sd: any) =>
		[sd?.tool_call_id, sd?.subagent_id, sd?.chat_id, sd?.entry_key].filter(Boolean) as string[];

	const getSubagentBatchKey = (sd: any, keys = getSubagentKeys(sd)) =>
		subagentScopedStateKey(
			sd?.parent_message_id || '',
			`${
				(sd?.entry_key ||
					sd?.tool_call_id ||
					sd?.subagent_id ||
					sd?.chat_id ||
					keys[0] ||
					'') as string
			}${sd?.rerun_id ? `\u001e${sd.rerun_id}` : ''}`
		);

	const isTerminalSubagentInnerEvent = (innerEvent: any) => {
		const innerType = innerEvent?.type;
		const innerData = innerEvent?.data ?? {};
		return (
			(innerType === 'chat:completion' && innerData?.done === true) ||
			innerType === 'chat:done' ||
			innerType === 'chat:message:error' ||
			innerType === 'chat:tasks:cancel'
		);
	};

	// Stream v2.1: apply one chat:delta op into a subagent's content_blocks
	// mirror. Mirrors the parent-message applyDeltaOp logic. The v2.1 wire format
	// deliberately strips heavy tool result bodies out of content_blocks and
	// sends them via tool_call:result, so slim `{ tool_call_id }` placeholders
	// must never overwrite a full result that the mirror already has.
	const applySubagentDeltaOp = (mirror: { content_blocks: any[] }, op: string, payload: any) => {
		if (!payload) payload = {};
		// `changedBlocks` is a per-op reactivity scratch list used by the PARENT
		// applyDeltaOp (declared in ITS closure). This subagent variant pushed to it
		// too but never declared it here, so block_open / tool_call_args_append threw
		// a ReferenceError that bubbled out of mergeSubagentPendingIntoRun and DROPPED
		// the entire subagent delta batch — the live card never streamed its
		// reasoning/tool-calls. The subagent path rebuilds cur.content_blocks
		// wholesale (a fresh object drives reactivity), so this list is write-only
		// here; declare it locally so the ops apply instead of throwing.
		const changedBlocks: any[] = [];
		if (op === 'text_append') {
			const idx = payload.block_idx;
			const block = mirror.content_blocks[idx];
			const text = payload.text || '';
			if (block && (block.type === 'text' || block.type === 'reasoning')) {
				const current = block.content || '';
				block.content =
					text.includes(current) && text.length > current.length ? text : current + text;
			} else if (idx === mirror.content_blocks.length) {
				const prev = mirror.content_blocks[idx - 1];
				if (
					prev &&
					(prev.type === 'text' || prev.type === 'reasoning') &&
					text.startsWith(prev.content || '')
				) {
					prev.content = text;
				} else {
					mirror.content_blocks.push({ type: 'text', content: text });
				}
			}
		} else if (op === 'block_open') {
			const block: any = { type: payload.type, content: '' };
			if (payload.attrs && typeof payload.attrs === 'object') {
				Object.assign(block, payload.attrs);
			}
			if (payload.type === 'tool_calls' && !Array.isArray(block.content)) {
				block.content = [];
			}
			if (typeof payload.block_idx === 'number') {
				// Never create array holes (undefined serializes as JSON null and
				// poisons any consumer of a cloned block list) — pad missed
				// indices with inert placeholders so the array stays dense. The
				// placeholder type is NOT text/reasoning so the adjacent-block
				// normalizer can't merge them and shift later server indices.
				while (mirror.content_blocks.length < payload.block_idx) {
					mirror.content_blocks.push({ type: 'placeholder', content: '' });
				}
				const existing = mirror.content_blocks[payload.block_idx];
				if (
					existing?.type === payload.type &&
					(payload.type === 'text' || payload.type === 'reasoning') &&
					typeof existing.content === 'string' &&
					existing.content
				) {
					block.content = existing.content;
				}
				mirror.content_blocks[payload.block_idx] = block;
				changedBlocks.push(block);
			} else {
				mirror.content_blocks.push(block);
				changedBlocks.push(block);
			}
		} else if (op === 'block_close') {
			const block = mirror.content_blocks[payload.block_idx];
			if (block) {
				if (payload.duration != null) block.duration = payload.duration;
				if (payload.output != null) block.output = payload.output;
				if (payload.ended != null) block.ended = payload.ended;
				if (payload.ended_at != null) block.ended_at = payload.ended_at;
				if (Array.isArray(payload.results)) {
					block.results = mergeToolResultEntries(
						payload.results,
						undefined,
						Array.isArray(block.results) ? block.results : []
					);
				}
			}
		} else if (op === 'tool_call_add') {
			const block = mirror.content_blocks[payload.block_idx];
			if (block && block.type === 'tool_calls') {
				if (!Array.isArray(block.content)) block.content = [];
				block.content.push(payload.tool_call);
			}
		} else if (op === 'tool_call_args_append') {
			// Reverse scan, stop at first match — see the parent applyDeltaOp
			// copy for rationale (O(1) common case vs O(blocks × calls)).
			const blocks = mirror.content_blocks;
			let found = false;
			for (let bi = blocks.length - 1; bi >= 0 && !found; bi--) {
				const block = blocks[bi];
				if (block?.type !== 'tool_calls' || !Array.isArray(block.content)) continue;
				for (let ci = block.content.length - 1; ci >= 0; ci--) {
					const tc = block.content[ci];
					if (tc?.id === payload.tool_call_id || tc?.tool_call_id === payload.tool_call_id) {
						const fn = tc.function || (tc.function = {});
						fn.arguments = (fn.arguments || '') + (payload.args_delta || '');
						changedBlocks.push(block);
						found = true;
						break;
					}
				}
			}
		} else if (op === 'replace') {
			if (Array.isArray(payload.content_blocks)) {
				if (typeof payload.block_idx === 'number' && payload.block_idx > 0) {
					const replacementBlocks = hydrateToolResultsInBlocks(
						payload.content_blocks,
						undefined,
						mirror.content_blocks.slice(
							payload.block_idx,
							payload.block_idx + payload.content_blocks.length
						)
					);
					mirror.content_blocks.splice(
						payload.block_idx,
						payload.content_blocks.length,
						...replacementBlocks
					);
				} else {
					mirror.content_blocks = hydrateToolResultsInBlocks(
						payload.content_blocks.slice(),
						undefined,
						mirror.content_blocks
					);
				}
			}
		} else if (op === 'sources' || op === 'selected_model_id' || op === 'usage') {
			// Handled by caller (sets fields on the run object).
		}
		if (op !== 'text_append' && op !== 'tool_call_args_append') {
			normalizeStreamingContentBlocks(mirror.content_blocks);
		}
	};

	// Flip every still-'running' subagent card belonging to `parentMessageId` to a
	// terminal status (done if it has a final answer, else cancelled), mirroring the
	// backend finalizer sweep. Used on parent CANCEL and on NORMAL completion so a
	// card whose own terminal event was missed never keeps spinning a runaway clock
	// until reload. Gated by parent_message_id so a concurrent independent redo's
	// cards are not touched.
	const flipRunningSubagentsTerminal = (parentMessageId: string | null) => {
		subagentLiveStates.update((s) => {
			let mutated = false;
			const out = { ...s };
			const nowSec = Math.floor(Date.now() / 1000);
			for (const [k, r] of Object.entries(out) as [string, any][]) {
				if (!r || r.status !== 'running') continue;
				if (parentMessageId) {
					// A specific parent generation just finalized (completed / cancelled /
					// done). Its INLINE subagents ran inside that generation, so they are
					// provably no longer running — flip them terminal even if `live`,
					// because a `live` card whose own chat:subagent:update terminal was
					// suppressed (backgrounded tab) or dropped (reconnect black-hole) would
					// otherwise spin 'Researching…' forever. A detached redo never runs
					// concurrently with its parent's inline finalize (redos start only
					// AFTER the turn completed), so this can't kill an active redo of THIS
					// message; cards of a DIFFERENT parent are skipped just below.
					if (!shouldParentFinalizeSubagentRun(r, parentMessageId)) continue;
				} else {
					// No parent attribution (a completion/done event without a message id):
					// only heal NOT-live stale cards. Never touch a `live` card we can't
					// attribute — it may be an active detached redo whose own terminal is
					// still in flight (this is the case the blanket `if (r.live) continue`
					// existed to protect).
					if (r.live) continue;
				}
				const finished = typeof r.final_text === 'string' && r.final_text.trim().length > 0;
				out[k] = {
					...r,
					status: finished ? 'done' : 'cancelled',
					ended_at: typeof r.ended_at === 'number' ? r.ended_at : nowSec,
					live: false
				};
				mutated = true;
			}
			return mutated ? out : s;
		});
	};

	/**
	 * Terminalize the chat's DETACHED subagent redos.
	 *
	 * The inline sweep above deliberately skips these (a redo owns its own
	 * terminal write and can overlap a parent action in another tab), so a
	 * chat-wide Stop — which now cancels redo tasks too — needs its own flip.
	 * Call only once the server has confirmed it cancelled those tasks.
	 */
	const flipRunningSubagentRerunsTerminal = () => {
		subagentLiveStates.update((s) => {
			let mutated = false;
			const out = { ...s };
			const nowSec = Math.floor(Date.now() / 1000);
			for (const [k, r] of Object.entries(out) as [string, any][]) {
				if (!r || r.status !== 'running' || !isDetachedSubagentRerun(r)) continue;
				out[k] = {
					...r,
					status: 'cancelled',
					live: false,
					final_text: r.final_text || r.previous_final_text,
					ended_at: typeof r.ended_at === 'number' ? r.ended_at : nowSec
				};
				mutated = true;
			}
			return mutated ? out : s;
		});
	};

	const mergeSubagentPendingIntoRun = (existing: any, pending: PendingSubagentUpdate) => {
		const sd = pending.sd ?? {};
		const rerunGenerationOrder = compareSubagentRerunGeneration(existing, sd);
		if (
			existing?.rerun_id &&
			sd?.rerun_id &&
			existing.rerun_id !== sd.rerun_id &&
			!shouldApplyIncomingSubagentGeneration(existing, sd)
		) {
			// A delayed event from an older detached rerun must not terminate or
			// append output into the newer generation currently shown for this
			// card. rerun_attempt orders same-second attempts; started_at remains
			// only a compatibility fallback for older servers.
			return existing;
		}
		const resetForNewRerun =
			existing?.rerun_id &&
			sd?.rerun_id &&
			existing.rerun_id !== sd.rerun_id &&
			rerunGenerationOrder === 1;
		const cur: any = {
			...(!resetForNewRerun && existing
				? existing
				: {
						subagent_id: sd.subagent_id,
						entry_key: sd.entry_key ?? sd.subagent_id,
						parent_message_id: sd.parent_message_id,
						tool_call_id: sd.tool_call_id,
						num: sd.num,
						name: sd.name,
						chat_id: sd.chat_id ?? sd.subagent_id,
						status: 'running',
						// Seed timing from the event if an update raced ahead of the
						// `chat:subagent:start` (otherwise a done-before-start leaves the
						// run with ended_at but no started_at → bare "Done" not a timer).
						started_at: sd.started_at
					})
		};
		if (cur.started_at == null && sd.started_at != null) cur.started_at = sd.started_at;

		cur.subagent_id = cur.subagent_id ?? sd.subagent_id;
		cur.entry_key = cur.entry_key ?? sd.entry_key ?? sd.subagent_id;
		cur.parent_message_id = cur.parent_message_id ?? sd.parent_message_id;
		cur.tool_call_id = cur.tool_call_id ?? sd.tool_call_id;
		cur.num = cur.num ?? sd.num;
		cur.name = cur.name ?? sd.name;
		cur.chat_id = cur.chat_id ?? sd.chat_id ?? sd.subagent_id;
		cur.rerun_id = sd.rerun_id ?? cur.rerun_id;
		cur.rerun_attempt = sd.rerun_attempt ?? cur.rerun_attempt;

		if (pending.statuses.length > 0) {
			const sh = Array.isArray(cur.statusHistory) ? cur.statusHistory : [];
			cur.statusHistory = [...sh, ...pending.statuses].slice(-SUBAGENT_STATUS_HISTORY_LIMIT);
		}

		// Stream-v2.1 inner events: apply deltas + tool results to the per-run
		// mirror so SubagentBlock keeps rendering off `content_blocks`.
		if (Array.isArray(pending.deltas) && pending.deltas.length > 0) {
			let blocks = Array.isArray(cur.content_blocks) ? cur.content_blocks.slice() : [];
			// Replace shared references so reactive consumers see a new array.
			const mirror = { content_blocks: blocks };
			for (const d of pending.deltas) {
				applySubagentDeltaOp(mirror, d?.op, d?.payload);
				if (d?.op === 'sources' && Array.isArray(d?.payload?.sources)) {
					cur.sources = d.payload.sources;
				} else if (d?.op === 'selected_model_id' && d?.payload?.model_id) {
					cur.selectedModelId = d.payload.model_id;
				}
			}
			cur.content_blocks = mirror.content_blocks;
		}
		if (Array.isArray(pending.toolResults) && pending.toolResults.length > 0) {
			const blocks = Array.isArray(cur.content_blocks) ? cur.content_blocks.slice() : [];
			for (const tr of pending.toolResults) {
				if (!tr?.tool_call_id) continue;
				const resultEntry = normalizeToolResultEntry(tr.tool_call_id, {
					tool_call_id: tr.tool_call_id,
					content: tr.result ?? '',
					...(Array.isArray(tr.files) && tr.files.length > 0 ? { files: tr.files } : {}),
					...(Array.isArray(tr.embeds) && tr.embeds.length > 0 ? { embeds: tr.embeds } : {}),
					...(tr.subagent_id ? { subagent_id: tr.subagent_id } : {}),
					...(tr.error ? { error: true } : {}),
					...(tr.error_reason ? { error_reason: tr.error_reason } : {}),
					...(tr.notice ? { notice: tr.notice } : {})
				});
				for (const block of blocks) {
					if (block?.type !== 'tool_calls' || !Array.isArray(block.content)) continue;
					block.results = Array.isArray(block.results) ? block.results : [];
					const mergedResult =
						mergeToolResultEntries([resultEntry], undefined, block.results)[0] ?? resultEntry;
					const existingIdx = block.results.findIndex(
						(r: any) => r?.tool_call_id === tr.tool_call_id
					);
					if (existingIdx >= 0) block.results[existingIdx] = mergedResult;
					else block.results.push(mergedResult);
					for (const tc of block.content) {
						if (tc?.id === tr.tool_call_id || tc?.tool_call_id === tr.tool_call_id) {
							tc.result = tr.result;
						}
					}
				}
			}
			cur.content_blocks = blocks;
		}

		if (pending.latestCompletion) {
			const innerData = pending.latestCompletion?.data ?? {};
			if (Array.isArray(innerData.content_blocks)) {
				cur.content_blocks = hydrateToolResultsInBlocks(
					innerData.content_blocks,
					undefined,
					Array.isArray(cur.content_blocks) ? cur.content_blocks : []
				);
			}
			if (typeof innerData.content === 'string') {
				cur.content = innerData.content;
			}
			if (innerData.done === true) {
				cur.status = 'done';
				cur.ended_at = Math.floor(Date.now() / 1000);
				cur.live = false;
				cur.final_text =
					cur.final_text || extractSubagentFinalText(cur.content_blocks, cur.content);
			}
		}

		const terminalType = pending.terminalEvent?.type;
		const terminalData = pending.terminalEvent?.data ?? {};
		if (terminalType === 'chat:message:error') {
			cur.status = 'error';
			cur.error = terminalData?.error ?? terminalData;
			cur.ended_at = Math.floor(Date.now() / 1000);
			cur.live = false;
			cur.final_text = cur.final_text || cur.previous_final_text;
		} else if (terminalType === 'chat:tasks:cancel') {
			cur.status = 'cancelled';
			cur.ended_at = Math.floor(Date.now() / 1000);
			cur.live = false;
			cur.final_text = cur.final_text || cur.previous_final_text;
		}

		if (pending.doneEvent) {
			// v2.1 terminal: chat:done finalizes the subagent's mirror.
			const doneData = pending.doneEvent?.data ?? {};
			cur.status = 'done';
			cur.ended_at = Math.floor(Date.now() / 1000);
			cur.live = false;
			if (doneData.usage) cur.usage = doneData.usage;
			// Prefer any answer already mirrored locally; else the authoritative
			// final_text the finalize broadcast carries (for a card that missed every
			// content update); else extract from whatever blocks we do have.
			cur.final_text =
				cur.final_text ||
				(typeof doneData.final_text === 'string' && doneData.final_text.trim()
					? doneData.final_text
					: '') ||
				extractSubagentFinalText(cur.content_blocks, cur.content);
		}

		// B3: a live, NON-terminal delta/status for this run means this session is
		// actively driving it now — re-promote a card that a reload seeded as
		// terminal (a stale 'done'/'cancelled' from persisted evidence) back to
		// 'running' + live so its timer and transcript resume. Tightly gated:
		// only on a real live signal, NEVER for a run that genuinely finished
		// (has final_text), and NEVER when the parent message is HARD-stopped
		// (errored / user-stopped — those subagents must stay terminal). A merely
		// `done` parent must NOT block re-promotion: a redo runs a fresh subagent
		// against an already-done parent turn, and that live rerun must animate.
		if (!pending.hasTerminal && (cur.status === 'done' || cur.status === 'cancelled')) {
			const sawLiveSignal =
				(Array.isArray(pending.deltas) && pending.deltas.length > 0) ||
				pending.statuses.length > 0 ||
				(pending.latestCompletion && pending.latestCompletion?.data?.done !== true);
			const genuinelyFinished =
				typeof cur.final_text === 'string' && cur.final_text.trim().length > 0;
			const parentMsg = cur.parent_message_id
				? (history.messages ?? {})[cur.parent_message_id]
				: null;
			const parentHardStopped = !!parentMsg?.error || parentMsg?.userStopped === true;
			if (sawLiveSignal && !genuinelyFinished && !parentHardStopped) {
				cur.status = 'running';
				cur.live = true;
				cur.ended_at = null;
			}
		}

		return cur;
	};

	const flushPendingSubagentUpdates = (forcePatchAll = false) => {
		if (subagentUpdateFlushTimer) {
			clearTimeout(subagentUpdateFlushTimer);
			subagentUpdateFlushTimer = null;
		}
		if (pendingSubagentUpdates.size === 0) return;

		const batch = Array.from(pendingSubagentUpdates.values());
		pendingSubagentUpdates.clear();

		const persistedRuns: { run: any; terminal: boolean }[] = [];
		subagentLiveStates.update((s) => {
			const out = { ...s };
			for (const pending of batch) {
				const existing = findSubagentRunEntry(
					out,
					pending.sd?.parent_message_id || '',
					pending.keys
				)?.[1];
				const next = mergeSubagentPendingIntoRun(existing, pending);
				persistedRuns.push({ run: next, terminal: pending.hasTerminal });
				setSubagentRunAliases(
					out,
					next,
					pending.keys,
					next?.parent_message_id || pending.sd?.parent_message_id || ''
				);
			}
			return out;
		});

		const now = Date.now();
		const shouldPatchPeriodic =
			now - lastSubagentParentPatchAt >= SUBAGENT_PARENT_PATCH_INTERVAL_MS;
		const runsToPatch = persistedRuns
			.filter(({ terminal }) => forcePatchAll || terminal || shouldPatchPeriodic)
			.map(({ run }) => run);
		if (runsToPatch.length > 0) {
			patchParentSubagentRuns(runsToPatch);
			if (shouldPatchPeriodic) lastSubagentParentPatchAt = now;
		}
	};

	const scheduleSubagentUpdateFlush = () => {
		if (subagentUpdateFlushTimer) return;
		subagentUpdateFlushTimer = setTimeout(() => {
			subagentUpdateFlushTimer = null;
			flushPendingSubagentUpdates();
		}, SUBAGENT_UI_FLUSH_MS);
	};

	const queueSubagentUpdate = (sd: any, innerEvent: any) => {
		const keys = getSubagentKeys(sd);
		if (keys.length === 0) return;
		const key = getSubagentBatchKey(sd, keys);
		if (!key) return;
		const pending = pendingSubagentUpdates.get(key) ?? {
			keys: [],
			sd,
			statuses: [],
			hasTerminal: false
		};

		pending.keys = Array.from(new Set([...pending.keys, ...keys]));
		pending.sd = { ...pending.sd, ...sd };
		const innerType = innerEvent?.type;
		const innerData = innerEvent?.data ?? {};
		if (
			innerType !== 'chat:completion' &&
			innerType !== 'status' &&
			innerType !== 'chat:message:error' &&
			innerType !== 'chat:tasks:cancel' &&
			innerType !== 'chat:delta' &&
			innerType !== 'tool_call:result' &&
			innerType !== 'chat:done'
		) {
			return;
		}

		if (innerType === 'chat:completion') {
			pending.latestCompletion = innerEvent;
		} else if (innerType === 'status') {
			pending.statuses = [...pending.statuses, innerData].slice(-SUBAGENT_STATUS_HISTORY_LIMIT);
		} else if (innerType === 'chat:message:error' || innerType === 'chat:tasks:cancel') {
			pending.terminalEvent = innerEvent;
		} else if (innerType === 'chat:delta') {
			pending.deltas = pending.deltas ?? [];
			pending.deltas.push(innerData);
		} else if (innerType === 'tool_call:result') {
			pending.toolResults = pending.toolResults ?? [];
			pending.toolResults.push(innerData);
		} else if (innerType === 'chat:done') {
			pending.doneEvent = innerEvent;
		}

		pending.hasTerminal = pending.hasTerminal || isTerminalSubagentInnerEvent(innerEvent);
		pendingSubagentUpdates.set(key, pending);

		if (pending.hasTerminal) {
			flushPendingSubagentUpdates();
		} else {
			scheduleSubagentUpdateFlush();
		}
	};

	const stopSubagentUpdateBatching = () => {
		flushPendingSubagentUpdates(true);
		if (subagentUpdateFlushTimer) {
			clearTimeout(subagentUpdateFlushTimer);
			subagentUpdateFlushTimer = null;
		}
		pendingSubagentUpdates.clear();
	};

	const applyBatchedStreamEvent = (event: any) => {
		const type = event?.data?.type ?? null;
		const data = event?.data?.data ?? null;

		if (type === 'chat:subagent:update') {
			const sd = data ?? {};
			queueSubagentUpdate(sd, sd.inner_event ?? {});
			return true;
		}

		if (type !== 'chat:delta' && type !== 'tool_call:result') {
			return false;
		}

		const resolvedMessageId = resolveChatEventMessageId(event.message_id);
		const message = resolvedMessageId ? history.messages[resolvedMessageId] : null;
		if (!message) {
			const messageId = event.message_id ?? data?.message_id;
			if (messageId && type === 'chat:delta') {
				const mirror = getOrCreateStreamMirror(messageId);
				mirror.pending_deltas.push({
					op: data?.op || '',
					version: typeof data?.version === 'number' ? data.version : 0,
					run: typeof data?.run === 'number' ? data.run : 0,
					payload: data?.payload
				});
				void requestStreamSnapshot(messageId, event.chat_id);
				return true;
			}
			if (messageId && type === 'tool_call:result' && data?.tool_call_id) {
				const mirror = getOrCreateStreamMirror(messageId);
				mirror.tool_results.set(
					data.tool_call_id,
					normalizeToolResultEntry(data.tool_call_id, data)
				);
				void requestStreamSnapshot(messageId, event.chat_id);
				return true;
			}
			return true;
		}
		if (message.retrying) return true;

		if (type === 'chat:delta') {
			chatDeltaHandler(data, message, event.chat_id);
		} else {
			toolCallResultHandler(data, message);
		}

		return true;
	};

	// Cross-device prompt sync: normalize a remote user message's files so images
	// and documents render on THIS device. The server already rewrites blob:/local
	// urls to portable `/api/v1/files/{id}/content`; this is a defensive backstop —
	// rebuild a missing/non-resolvable url from the file id, mirroring
	// getFileContentUrl.
	const normalizeRemoteFiles = (files: any[]) => {
		if (!Array.isArray(files)) return [];
		return files.map((f) => {
			if (!f || typeof f !== 'object') return f;
			const url = f.url;
			const portable =
				typeof url === 'string' &&
				(url.startsWith('data:') || url.startsWith('http') || /\/files\/[^/]+\/content/.test(url));
			if (!portable && f.id) {
				return { ...f, url: `${WEBUI_API_BASE_URL}/files/${f.id}/content` };
			}
			return f;
		});
	};

	// Cross-device prompt sync: a `chat:user-message` event means a prompt was
	// submitted on this chat (from another device, or this one). Surgically insert
	// the user bubble and link it into the tree so it appears alongside the
	// assistant stream that already reaches us via the chat's stream room — WITHOUT
	// a full reload. Idempotent (keyed by message id): the origin device receiving
	// its own event, a prior loadChat, a queue-drain reload, or a socket
	// redelivery are all no-op merges. loadChat() stays the authoritative backstop
	// and agrees with what we insert (the server emits the exact persisted row).
	const handleRemoteUserMessage = (event: any) => {
		if ($temporaryChatEnabled) return;
		// The origin device already created this row optimistically (submitPrompt),
		// and is in its own chat's stream room, so it receives its own event. Skip
		// it: re-applying would be a no-op merge but still costs a structure bump
		// and could yank the origin's branch view if the user navigated mid-stream.
		if (event?.session_id && $socket?.id && event.session_id === $socket.id) return;
		const payload = event?.data ?? {};
		const um = payload.user_message;
		if (!um || !um.id) return;

		const userId = um.id;
		const parentId = um.parentId ?? null;
		const assistantId = payload.assistant_message_id ?? null;

		// Parent named but unknown locally: we can't safely attach (would orphan
		// the node / build a divergent branch). Defer to the full-history rebuild.
		// Record the deferral so the chip-clear that the server emits right after
		// this bubble (chat:queue:updated) doesn't shrink the chip while loadChat is
		// still in flight — otherwise a behind tab shows neither the queued chip nor
		// the bubble for one loadChat round-trip. loadChat reconciles the queue.
		if (parentId && !history.messages[parentId]) {
			remoteUserDeferredLoadAt = Date.now();
			void loadChat();
			return;
		}

		const existing = history.messages[userId];
		if (existing) {
			// Idempotent merge: keep the local row's (possibly richer) links, but
			// prefer the server's portable files when the local row has none.
			history.messages[userId] = {
				...existing,
				content: existing.content ?? um.content ?? '',
				files:
					Array.isArray(existing.files) && existing.files.length
						? existing.files
						: normalizeRemoteFiles(um.files ?? [])
			};
		} else {
			history.messages[userId] = {
				id: userId,
				parentId,
				childrenIds: Array.isArray(um.childrenIds) ? [...um.childrenIds] : [],
				role: 'user',
				content: um.content ?? '',
				files: normalizeRemoteFiles(um.files ?? []),
				models: Array.isArray(um.models) ? um.models : [],
				timestamp: um.timestamp ?? Math.floor(Date.now() / 1000)
			};
		}

		// Link into the parent's children (dedup).
		if (parentId) {
			const parent = history.messages[parentId];
			if (parent) {
				if (!Array.isArray(parent.childrenIds)) parent.childrenIds = [];
				if (!parent.childrenIds.includes(userId)) parent.childrenIds.push(userId);
			}
		}

		// Ensure the assistant turn renders alongside the user bubble — the same
		// "user message + assistant + live cursor" shape as a normal send. The
		// server already knows the assistant id (it rides on this event as
		// assistant_message_id), but on a headless queue-drain the placeholder row is
		// created server-side and is not yet in THIS tab's history, and on a
		// cross-device send it streams in slightly later. If we only inserted the user
		// bubble and pointed currentId at it, the tab would show "just my message" (no
		// assistant container, no typewriter cursor) until the later
		// chat:queue:drained -> loadChat / first chat:delta lands — which, for a
		// non-streaming or slow-to-first-token model, is the WHOLE turn. So MATERIALIZE
		// a minimal assistant placeholder (done:false, empty) parented to the user
		// message and advance currentId to it: the empty done:false row drives the
		// start Skeleton cursor and flips turnLive, exactly like a fresh send. loadChat
		// (wholesale replace) and requestStreamSnapshot reconcile to the authoritative
		// server row by the SAME id — no duplicate, no wrong parent.
		if (assistantId) {
			let assistant = history.messages[assistantId];
			if (!assistant) {
				assistant = {
					id: assistantId,
					parentId: userId,
					childrenIds: [],
					role: 'assistant',
					content: '',
					content_blocks: [],
					model:
						Array.isArray(um.models) && um.models.length
							? um.models[0]
							: (selectedModels?.[0] ?? ''),
					done: false,
					timestamp: Math.floor(Date.now() / 1000)
				};
				history.messages[assistantId] = assistant;
			}
			// Re-home under the new user message if it materialized under a stale
			// node (a chat:delta/snapshot beat this event) or was just created above.
			if (assistant.parentId !== userId) {
				const oldParent = assistant.parentId ? history.messages[assistant.parentId] : null;
				if (oldParent && Array.isArray(oldParent.childrenIds)) {
					oldParent.childrenIds = oldParent.childrenIds.filter((c) => c !== assistantId);
				}
				assistant.parentId = userId;
			}
			const userRow = history.messages[userId];
			if (!Array.isArray(userRow.childrenIds)) userRow.childrenIds = [];
			if (!userRow.childrenIds.includes(assistantId)) userRow.childrenIds.push(assistantId);
		}

		// Follow the new turn (matches single-device + queue-drain UX): advance to
		// the assistant when an assistant id was carried (now always materialized
		// above), else to the user message.
		//
		// EXCEPTION (concurrent send): if THIS client has its own in-flight generation
		// (it owns a local generation lifecycle), a peer's simultaneous send
		// must NOT steal the view. We still recorded the peer's user+assistant rows in
		// history above (reachable via branch nav), but we keep currentId on our OWN
		// streaming branch and do not override our generating/poll state. Without this,
		// two devices sending at once would each get yanked onto the other's sibling
		// branch and lose sight of their own streaming answer.
		const hasOwnInflightTurn = generationLifecycles.activeForChat(getVisibleChatId()).length > 0;
		if (!hasOwnInflightTurn) {
			history.currentId = assistantId && history.messages[assistantId] ? assistantId : userId;
		}

		// The drained/remote turn is now visible with its assistant container, so the
		// "drain pending" bridge that kept the input bar in its working state across
		// the prior-turn -> drain gap has done its job (turnLive now holds via
		// currentId pointing at the not-done assistant). Clear it so it can't latch.
		clearQueueDrainPending();

		history = { ...history };
		bumpMessageStructure();

		// G5 (multi-client): a remote turn just started on THIS chat from another
		// device/tab. Register it as OBSERVED work so the composer reflects it
		// exactly like a local turn, and start the resume-task poll: it attaches
		// the live taskIds (so Stop works cross-device), reconciles via snapshot,
		// and — because that poll is authoritative — settles the observed record
		// the moment the backend task finishes, even if this tab never sees the
		// terminal chat:done. Idempotent: startResumeTaskPolling early-returns if
		// a poll is already running; the origin tab never reaches here (skipped
		// above). Skipped when we have our OWN in-flight turn (concurrent send).
		//
		// The poll starts even when NO assistant id came with the event. That case
		// leaves `currentId` on the bare user message, and a user row has no `done`
		// flag — so `turnLive`'s "the visible leaf hasn't finished" term reads true
		// and, with nothing observing and nothing polling, stays true for the rest
		// of the session if the assistant row never arrives. Same disease as the
		// drain bridge it just cleared: a working state with no channel that can
		// retire it. The poll is that channel — it either attaches the real
		// generation or, finding none, reloads and settles the leaf.
		const assistantAlreadyFinished = !!assistantId && history.messages[assistantId]?.done === true;
		if (!hasOwnInflightTurn && !assistantAlreadyFinished) {
			const _remoteChatId = getVisibleChatId();
			if (_remoteChatId && !_remoteChatId.startsWith('local:')) {
				// No assistant id ⇒ nothing to key an observed record on; the poll is
				// the whole convergence mechanism in that case.
				if (assistantId) {
					generationLifecycles.observe(_remoteChatId, assistantId, navigateGeneration);
				}
				startResumeTaskPolling(_remoteChatId);
			}
		}
	};

	const chatEventHandler = async (event, cb, options: { skipTick?: boolean } = {}) => {
		const perf = streamPerfStart();

		// data_viz:render must be handled REGARDLESS of which chat this tab is
		// currently viewing. It renders in a hidden, detached iframe
		// (dispatchWidgetRender) and does not depend on the visible DataVizWidget
		// being mounted. The backend's show_widget tool blocks on this ack, so
		// dropping it via the visibility guard below — because the user switched
		// the originating tab to a different chat — would stall the model for the
		// full 30s render timeout.
		if (event?.data?.type === 'data_viz:render') {
			const result = await dispatchWidgetRender(event.message_id, event?.data?.data ?? null);
			if (cb) cb(result);
			streamPerfEnd('chat.event.data_viz_render', perf);
			return;
		}

		// Any server event for a chat means its content advanced — drop its LRU snapshot
		// (item 2) so a later user-initiated switch back refetches. Done before the
		// visibility guard so background chats streaming on another device are covered
		// too. Over-invalidation only ever costs a cache miss, never staleness.
		invalidateChatOpenCache(event.chat_id);

		if (!isVisibleChatEvent(event.chat_id)) {
			streamPerfEnd('chat.event_ignored_not_visible', perf);
			return;
		}

		if (!options.skipTick) {
			await tick();
		}

		const visibleChatId = getVisibleChatId();
		const type = event?.data?.type ?? null;
		const data = event?.data?.data ?? null;
		streamPerfCount(`chat.event.${type ?? 'unknown'}`);

		// Stream-v2.1 batching: socket.main may coalesce consecutive chat:delta /
		// tool_call:result envelopes into one chat:delta:batch. Chat.svelte owns
		// the live message mirror, so it must unpack the batch itself; the global
		// layout handler cannot mutate this component's history.
		if (type === 'chat:delta:batch2') {
			const groups = Array.isArray(event?.data?.groups) ? event.data.groups : [];
			let innerCount = 0;
			for (const group of groups) {
				const messageId = group?.message_id ?? event.message_id;
				const baseVersion = typeof group?.base_version === 'number' ? group.base_version : 0;
				const offsetVersions = group?.version_mode === 'offset';
				// Compact frames drop the per-delta run stamp; the envelope carries
				// it once per message group — restore it on each synthetic event so
				// run-gating works identically to un-batched delivery.
				const groupRun = typeof group?.run === 'number' && group.run > 0 ? group.run : 0;
				for (const delta of Array.isArray(group?.deltas) ? group.deltas : []) {
					if (!Array.isArray(delta) || delta.length < 2) continue;
					const [encodedVersion, opCode] = delta;
					if (typeof encodedVersion !== 'number') continue;
					const version = offsetVersions ? baseVersion + encodedVersion : encodedVersion;
					const op = compactStreamOps[opCode] ?? opCode;
					const payload = decodeCompactStreamPayload(op, delta);
					const innerEvent = {
						chat_id: event.chat_id,
						message_id: messageId,
						data: {
							type: 'chat:delta',
							data: {
								message_id: messageId,
								version,
								...(groupRun ? { run: groupRun } : {}),
								op,
								payload
							}
						}
					};
					innerCount += 1;
					if (applyBatchedStreamEvent(innerEvent)) continue;
					await chatEventHandler(innerEvent, cb, { skipTick: true });
				}
				for (const result of Array.isArray(group?.tool_results) ? group.tool_results : []) {
					const innerEvent = {
						chat_id: event.chat_id,
						message_id: messageId,
						data: { type: 'tool_call:result', data: { ...result, message_id: messageId } }
					};
					innerCount += 1;
					if (applyBatchedStreamEvent(innerEvent)) continue;
					await chatEventHandler(innerEvent, cb, { skipTick: true });
				}
			}
			streamPerfEnd('chat.event_handler', perf, innerCount || 1);
			return;
		}

		if (type === 'chat:delta:batch') {
			const batch = Array.isArray(event?.data?.batch) ? event.data.batch : [];
			streamPerfCount('chat.event.batch_inner', batch.length);
			for (const inner of batch) {
				if (!inner || typeof inner !== 'object') continue;
				const innerEvent = {
					chat_id: inner.chat_id ?? event.chat_id,
					message_id: inner.message_id ?? event.message_id,
					data: inner.data
				};
				if (applyBatchedStreamEvent(innerEvent)) continue;
				await chatEventHandler(innerEvent, cb, { skipTick: true });
			}
			streamPerfEnd('chat.event_handler', perf, batch.length || 1);
			return;
		}

		if (type === 'chat:token-usage') {
			// Authoritative per-chat token totals (cumulative). Already
			// visibility-gated above to event.chat_id; apply straight to the pill —
			// no fetch. This is the live driver for subagent roll-up + multi-round
			// correction. See applyAuthoritativeChatTokenStats.
			applyAuthoritativeChatTokenStats(event.chat_id, data);
			streamPerfEnd('chat.event.token_usage', perf);
			return;
		}

		if (type === 'chat:title') {
			const title = typeof data === 'string' ? data : (data?.title ?? '');
			if (title) {
				chatTitle.set(title);
			}
			if (data?.id && title) {
				const updatedAt =
					typeof data?.updated_at === 'number' ? data.updated_at : Math.floor(Date.now() / 1000);
				const row = decorate({
					id: data.id,
					title,
					updated_at: updatedAt,
					created_at: updatedAt,
					pinned: data.pinned ?? false,
					archived: data.archived ?? false,
					folder_id: data.folder_id ?? null
				});
				chats.update((arr) => upsertSorted(arr, row));
			}
			return;
		}

		if (type === 'browser:frame') {
			// Live browser progress frame (fire-and-forget). Handled EARLY and
			// independently of history.messages: the state lives in a side store
			// keyed by per-agent browser SESSION (so parallel-browsing agents each
			// get their own tab, and per-frame churn never re-renders the message
			// list), and it must NOT be gated behind the in-flight `message` lookup
			// / retry guard below — a frame can arrive before the assistant message
			// row materializes (headless drain / reattach).
			//
			// Key precedence: the per-agent `session` id (the parent is "main";
			// each subagent is its subagent_id) so concurrent browsers don't
			// overwrite each other. Legacy frames without a session fall back to the
			// message id (single-tab behavior, unchanged).
			const bid = data?.session || resolveChatEventMessageId(event.message_id) || event.message_id;
			if (bid) {
				// Derive startedAt from the daemon's elapsedMs so the timer (a)
				// resets per browser call within a turn and (b) is immune to
				// container/client clock skew. Falls back to now when absent.
				const elapsed = Number(data?.elapsedMs ?? 0) || 0;
				const startedAt = Date.now() - elapsed;
				const isDone = data?.done === true;
				const label = browserSessionLabel(data?.session);
				browserLiveStates.update((s) => ({
					...s,
					[bid]: {
						...(s[bid] ?? {}),
						...data,
						startedAt,
						done: isDone,
						...(label ? { label } : {})
					}
				}));
				// Auto-open when nothing else owns the side pane and the user hasn't
				// dismissed the panel this turn. Never steal focus from an open
				// Artifact/Embed/FilePreview/Overview/Call.
				//
				// Open on ANY frame while the turn is live — not only non-done ones.
				// The backend poller exists only while a browser_* tool call is in
				// flight, so a frame arriving here means the agent is browsing RIGHT
				// NOW even when the frame itself says done (fast actions — a sub-500ms
				// navigate/snapshot — complete inside one poll tick, so the only
				// frames they ever emit are done:true; requiring !isDone made typical
				// quick browsing never open the panel at all). A terminal done frame
				// after the turn ends (generating false) still doesn't pop it open.
				//
				// EXCEPTION (root fix for "closed panel bricks the captcha"): a
				// frame that says a human is REQUIRED opens the panel regardless
				// of the dismissal flag — a handoff is blocking, and the panel is
				// the only way to solve or dismiss it.
				if (
					!get(showBrowserPanel) &&
					!get(showCallOverlay) &&
					!get(showArtifacts) &&
					!get(showEmbeds) &&
					!get(showFilePreview) &&
					!get(showOverview) &&
					(data?.requiresHuman === true ||
						((!isDone || generating) && !get(browserPanelDismissed)))
				) {
					showBrowserPanel.set(true);
					showControls.set(true);
				}
			}
			return;
		}

		if (type === 'chat:stream:sync_required') {
			const messageId = data?.message_id ?? event.message_id;
			if (messageId) {
				const replayed = await requestStreamReplay(messageId, event.chat_id).catch(() => false);
				if (!replayed) {
					// The server explicitly told us we're out of sync — its
					// snapshot is authoritative here (heal adopts even if this
					// mirror's version looks ahead).
					await requestStreamSnapshot(messageId, event.chat_id, { force: true, heal: true });
				}
			}
			return;
		}

		// Server-driven queue reflection. The backend owns draining for DB chats;
		// these events let every tab (and a later-opened tab) mirror the queue
		// state and attach to a generation it didn't start.
		if (type === 'chat:queue:updated') {
			// Plain queue mutation (enqueue/remove/edit from another tab, or the
			// head popped). Mirror the authoritative server queue into local state.
			if (Array.isArray(data?.queue)) {
				// If we JUST deferred a drained user-message insert to loadChat (an
				// unknown-parent behind tab), skip a chip-SHRINK here: it is the chip-
				// clear paired with that bubble, and applying it now would clear the
				// chip while the bubble is still a loadChat round-trip away (a visible
				// "chip gone, bubble absent" gap). loadChat reconciles the queue from
				// the blob when it lands. Only suppress an actual shrink, and only
				// briefly, so normal queue updates are never dropped.
				const deferringRemoteLoad =
					Date.now() - remoteUserDeferredLoadAt < 4000 && data.queue.length < queue.length;
				if (!deferringRemoteLoad) {
					// Merge, not blind-replace: reconcile the authoritative server snapshot
					// with THIS tab's own not-yet-committed queue mutations (a concurrent
					// queue op from another tab broadcasts a snapshot that predates our
					// uncommitted add/edit/remove). See reconcileServerQueue.
					queue = reconcileServerQueue(data.queue as QueuedMessage[]);
				}
			}
			return;
		}

		if (type === 'chat:queue:drained') {
			// A queued item just started generating server-side. Sync the queue
			// and reload so the new user message + assistant placeholder (created
			// by the backend, with ids this tab never saw) show up and the stream
			// gets subscribed via the normal active-stream/snapshot path.
			if (Array.isArray(data?.queue)) {
				queue = reconcileServerQueue(data.queue as QueuedMessage[]);
			}
			// C09: if a drain fired in the same instant THIS tab pressed Stop
			// (before the Stop signal landed), the user asked to halt — stop the
			// just-spawned generation rather than attaching to it. Gate on the
			// RECENCY of this tab's Stop, not merely on the latch: the latch stays
			// set until this tab's next submit, so gating on it alone would make
			// this tab kill a generation ANOTHER tab legitimately started (e.g. via
			// "Send now") long after. Still fall through to loadChat so a terminal
			// stopped row (e.g. a headless-drain cancel) renders.
			if (stoppedHereRecently() && visibleChatId && !visibleChatId.startsWith('local:')) {
				const responseMessageId = String(data?.response_message_id ?? '');
				const generationId = String(data?.generation_id ?? '');
				const turnId = String(data?.turn_id ?? '');
				if (responseMessageId && generationId && turnId) {
					await stopChatGenerations(localStorage.token, visibleChatId, {
						generations: [
							{
								generation_id: generationId,
								message_id: responseMessageId,
								turn_id: turnId
							}
						]
					}).catch(() => null);
				}
			}
			if (visibleChatId && !$temporaryChatEnabled) {
				// loadChat re-fetches history + active streams, flips `generating`,
				// and starts resume polling — i.e. it attaches this tab to the
				// headless generation exactly like a reload would.
				await loadChat();
			}
			return;
		}

		if (type === 'chat:tags') {
			const [chatTags, _allTags] = await Promise.all([
				visibleChatId ? getTagsById(localStorage.token, visibleChatId).catch(() => []) : null,
				getAllTags(localStorage.token)
			]);
			if (visibleChatId) {
				tags = chatTags ?? [];
			}
			allTags.set(_allTags);
			return;
		}

		if (type === 'notification') {
			const toastType = data?.type ?? 'info';
			const toastContent = data?.content ?? '';

			if (toastType === 'success') {
				toast.success(toastContent);
			} else if (toastType === 'error') {
				toast.error(toastContent);
			} else if (toastType === 'warning') {
				toast.warning(toastContent);
			} else {
				toast.info(toastContent);
			}

			return;
		}

		if (type === 'confirmation') {
			eventCallback = cb;

			eventConfirmationInput = false;
			showEventConfirmation = true;

			eventConfirmationTitle = data.title;
			eventConfirmationMessage = data.message;
			return;
		}

		if (type === 'execute') {
			eventCallback = cb;

			try {
				// Use Function constructor to evaluate code in a safer way
				const asyncFunction = new Function(`return (async () => { ${data.code} })()`);
				const result = await asyncFunction();

				if (cb) {
					cb(result);
				}
			} catch (error) {
				console.error('Error executing code:', error);
			}

			return;
		}

		if (type === 'input') {
			eventCallback = cb;

			eventConfirmationInput = true;
			showEventConfirmation = true;

			eventConfirmationTitle = data.title;
			eventConfirmationMessage = data.message;
			eventConfirmationInputPlaceholder = data.placeholder;
			eventConfirmationInputValue = data?.value ?? '';
			return;
		}

		// Subagent lifecycle events. The runner in `utils/subagent.py` emits
		// `chat:subagent:start` once when a subagent run begins, and
		// `chat:subagent:update` wrapping each forwarded inner-pipeline event
		// (chat:completion, status, errors) so the parent UI's collapsible
		// block under the parent assistant message can render live progress.
		//
		// Both events are keyed on `tool_call_id` (the parent's tool_call_id
		// that triggered this subagent) — that's the same key
		// `serialize_content_blocks` stamps onto the `<details
		// type="subagent_launch">` placeholder, so the SubagentBlock
		// component can look up state without any prop drilling.
		if (type === 'chat:subagent:start') {
			const sd = data ?? {};
			const keys = [sd.tool_call_id, sd.subagent_id, sd.chat_id, sd.entry_key].filter(Boolean);
			if (keys.length > 0) {
				const now = Math.floor(Date.now() / 1000);
				let persistedRun: any = null;
				subagentLiveStates.update((s) => {
					const existing: any =
						findSubagentRunEntry(s, sd.parent_message_id || '', keys)?.[1] ?? {};
					if (
						sd.rerun_id &&
						existing.rerun_id &&
						sd.rerun_id !== existing.rerun_id &&
						!shouldApplyIncomingSubagentGeneration(existing, sd)
					) {
						// A delayed or unorderable start from another attempt must
						// not replace the generation already displayed.
						persistedRun = null;
						return s;
					}
					if (
						sd.rerun_id &&
						existing.rerun_id === sd.rerun_id &&
						existing.live === false &&
						['done', 'error', 'cancelled'].includes(existing.status)
					) {
						// The rerun completed before its delayed start event was
						// delivered. Terminal state for the same generation wins.
						persistedRun = null;
						return s;
					}
					const next: any = {
						...existing,
						subagent_id: sd.subagent_id,
						entry_key: sd.entry_key ?? existing.entry_key ?? sd.subagent_id,
						parent_message_id: sd.parent_message_id,
						tool_call_id: sd.tool_call_id,
						num: sd.num,
						name: sd.name,
						chat_id: sd.chat_id ?? sd.subagent_id,
						prompt: sd.prompt ?? existing.prompt,
						background: sd.background ?? existing.background,
						continuation: sd.continuation === true,
						rerun: sd.rerun === true || existing.rerun === true,
						rerun_id: sd.rerun_id ?? existing.rerun_id,
						rerun_attempt: sd.rerun_attempt ?? existing.rerun_attempt,
						status: 'running',
						// This session now owns the stream — make the store status
						// authoritative over the parent message's persisted
						// `<details done="true">` placeholder (not rewritten on redo).
						live: true,
						// Prefer the event's start time (the backend stamps it on every
						// start, including reruns) so a redo times from the REDO, not the
						// original launch — and every tab agrees. Falls back to a prior
						// value, then the local clock.
						started_at: sd.started_at ?? existing.started_at ?? now,
						// A (re)start resets the terminal state so a redo observed in any
						// tab clears the prior answer/timer instead of showing stale data.
						ended_at: undefined,
						previous_final_text: existing.final_text || existing.previous_final_text,
						final_text: undefined,
						error: undefined,
						stale: false,
						content_blocks: [],
						content: ''
					};
					persistedRun = next;
					const out = { ...s };
					setSubagentRunAliases(out, next, keys, sd.parent_message_id || '');
					return out;
				});
				patchParentSubagentRun(persistedRun);
			}
			return;
		}

		if (type === 'chat:subagent:update') {
			const sd = data ?? {};
			const innerEvent = sd.inner_event ?? {};
			queueSubagentUpdate(sd, innerEvent);
			return;
		}

		// Cross-device prompt sync: insert/merge the user bubble for a prompt
		// submitted on this chat. Handled BEFORE resolveChatEventMessageId because
		// the event's message_id (the assistant id) is by definition not yet in
		// local history, so the gate below would drop it.
		if (type === 'chat:user-message') {
			handleRemoteUserMessage(event);
			return;
		}

		const resolvedMessageId = resolveChatEventMessageId(event.message_id);
		let message = resolvedMessageId ? history.messages[resolvedMessageId] : null;

		if (!message) {
			if ((type === 'chat:delta' || type === 'tool_call:result') && event.message_id) {
				applyBatchedStreamEvent(event);
				return;
			}
			// CRITICAL: with the tightened resolveChatEventMessageId, an event
			// reaches this "no message" branch only when it (a) had no
			// message_id, or (b) named a message id we don't have. Case (b) is
			// almost always a stale event from a previous turn — reconciling
			// history here would kill the in-flight follow-up. So: act only when
			// nothing is currently in flight for this chat. A client-side send
			// retry (countdown between attempts) has no controller but IS an
			// in-flight turn — same protection.
			//
			// Nothing here touches `generating`/`taskIds`: both derive from the
			// lifecycle registry, and `hasInFlight` is that same predicate, so if
			// it's false they already read as idle.
			const hasInFlight =
				generationLifecycles.activeForChat(getVisibleChatId()).length > 0 ||
				activeSendRetryLoops > 0;
			const canReloadVisible = !!visibleChatId && !$temporaryChatEnabled;
			chatStreamDebug('[chat-stream] no-message event', {
				type,
				eventMessageId: event.message_id,
				eventChatId: event.chat_id,
				hasInFlight
			});

			if (type === 'chat:tasks:cancel') {
				if (!hasInFlight) markPendingAssistantMessagesDone();
				// Reconcile the token pill: a cancel can drop the final throttled
				// chat:token-usage push and otherwise triggers no reconcile.
				if (canReloadVisible) chatTokenStatsRefreshTrigger.update((n) => n + 1);
				return;
			}

			// Terminal events for a turn we don't hold locally: the durable row is
			// the only source of truth left, so reload to adopt it. (message:error
			// deliberately falls through when it can't reload, so the trailing
			// stop-by-error guard below still runs.)
			const isTerminalForUnknownTurn =
				type === 'chat:done' || (type === 'chat:completion' && data?.done);
			if (isTerminalForUnknownTurn || type === 'chat:message:error') {
				if (!hasInFlight && canReloadVisible) {
					await loadChat();
					return;
				}
				if (isTerminalForUnknownTurn) return;
			}

			console.warn('Unable to resolve live chat message for current chat event', event);
			return;
		}

		// Terminal and v1 completion events carry the backend generation id.
		// A retry/continue may reuse this same message id, so message identity
		// alone is insufficient: a delayed event from the prior run must not
		// settle or mutate the newer lifecycle.
		const eventGenerationId = typeof data?.generation_id === 'string' ? data.generation_id : '';
		const eventLifecycle = generationLifecycles.get(resolvedMessageId);
		if (
			eventGenerationId &&
			eventLifecycle &&
			eventLifecycle.generationId !== eventGenerationId &&
			(type === 'chat:completion' ||
				type === 'chat:done' ||
				type === 'chat:message:error' ||
				type === 'chat:tasks:cancel')
		) {
			chatStreamDebug('[chat-stream] dropping stale-generation terminal event', {
				type,
				resolvedMessageId,
				eventGenerationId,
				activeGenerationId: eventLifecycle.generationId
			});
			return;
		}

		// Stale-event guard: while we're in a retry countdown, any state
		// mutation from the just-failed attempt (chat:completion done=true,
		// chat:message:error, chat:tasks:cancel, content deltas, etc.) would
		// corrupt the retry — flipping done=true, clearing generating, or
		// stuffing in old content. Drop everything except the trailing
		// stop-by-error guard below.
		if (message.retrying) {
			return;
		}

		if (type === 'status') {
			if (message?.statusHistory) {
				message.statusHistory.push(data);
			} else {
				message.statusHistory = [data];
			}
		} else if (type === 'chat:completion') {
			await chatCompletionEventHandler(data, message, event.chat_id);
			// chatCompletionEventHandler manages history.messages updates internally via
			// spread objects. Do NOT fall through to the store-back at the end of this
			// function — it holds a reference to the pre-spread message object which
			// would overwrite the done=true state set inside chatCompletionEventHandler.
			return;
		} else if (type === 'chat:delta') {
			// Stream protocol v2.1: backend emits per-op deltas instead of resending
			// the full content_blocks every tick. See plan Phase 0 wire contract #1.
			chatDeltaHandler(data, message, event.chat_id);
			return;
		} else if (type === 'tool_call:result') {
			// Stream protocol v2.1: tool results are emitted once, by id; subsequent
			// deltas reference the tool_call_id without resending the body.
			toolCallResultHandler(data, message);
			return;
		} else if (type === 'chat:done') {
			// Stream protocol v2.1 terminal event. Mirrors the v1 `chat:completion`
			// `done:true` finalize path.
			await chatDoneHandler(data, message, event.chat_id);
			return;
		} else if (type === 'chat:tasks:cancel') {
			// A manual retry/continue can intentionally reuse the same assistant
			// message id. Never let the delayed terminal event from the old run
			// settle that newer lifecycle. The generic generation gate above
			// protects the originating tab; run protects observers/reconnects
			// that do not own a lifecycle.
			const cancelRun = typeof data?.run === 'number' ? data.run : 0;
			const cancelMirror = streamMirrors.get(resolvedMessageId);
			if (cancelRun && cancelMirror?.run && cancelRun < cancelMirror.run) {
				chatStreamDebug('[chat-stream] dropping stale-run chat:tasks:cancel', {
					resolvedMessageId,
					cancelRun,
					mirrorRun: cancelMirror.run
				});
				return;
			}
			const cancelOwnedByThisTab = ownsGeneration(resolvedMessageId);
			chatStreamDebug('[chat-stream] resolved chat:tasks:cancel — clearing controller', {
				resolvedMessageId,
				ownedByThisMessage: cancelOwnedByThisTab
			});
			const turnSettled = settleGenerationLifecycle(resolvedMessageId);
			if (turnSettled) stopResumeTaskPolling();

			// Cancellation is message-scoped on the wire. A user Stop emits one
			// terminal event per sibling (and already latches all siblings
			// locally); an isolated provider/task cancellation must not mark the
			// other model responses done while they are still streaming.
			message.done = true;

			// B2: the parent generation was cancelled — flip its still-'running'
			// subagent cards to terminal immediately so they stop spinning without
			// waiting for a reload (the backend finalizer sweep persists the same).
			flipRunningSubagentsTerminal(resolvedMessageId);

			history = { ...history };

			// Multi-client reconcile: a stop we did NOT initiate cancels a stream whose
			// live tokens we may have had suppressed (hidden tab) or been lagging behind.
			// Unlike the clean chat:done path, the cancel handler doesn't pull content —
			// so without this a behind tab would freeze on stale/truncated partial text.
			// Fetch the backend-finalized partial (the snapshot endpoint falls back to
			// the persisted row post-cancel). Skipped for the tab that authored the turn
			// (its view is authoritative) and the tab that clicked Stop (userStopped guard);
			// the resume poll is the 2s backstop for those.
			if (
				resolvedMessageId &&
				event.chat_id &&
				!cancelOwnedByThisTab &&
				!isUserStoppedMessageId(resolvedMessageId)
			) {
				void requestStreamSnapshot(resolvedMessageId, event.chat_id, { force: true });
			}
		} else if (type === 'chat:message:delta' || type === 'message') {
			message.content += data.content;
			history.messages[resolvedMessageId] = message;
			history = { ...history };
		} else if (type === 'chat:message' || type === 'replace') {
			message.content = data.content;
			history.messages[resolvedMessageId] = message;
			history = { ...history };
		} else if (type === 'chat:message:compacted') {
			// A `/compact` that ran with no generation attached (the idle command
			// from another tab, or a queued one the drain executed). Mid-turn
			// compactions arrive on `chat:completion` instead, with the rest of
			// the block list.
			applyCompactionBlocks(resolvedMessageId, data?.content_blocks);
			history = { ...history };
		} else if (type === 'chat:message:files') {
			message.files = data.files;
		} else if (type === 'files') {
			message.files = mergeMessageFiles(message.files ?? [], data.files ?? []);
		} else if (type === 'chat:message:embeds' || type === 'embeds') {
			message.embeds = data.embeds;
		} else if (type === 'data_viz:override') {
			// Backend's show_widget tool just persisted a corrected widget_code.
			// Merge into the in-memory message so the soon-to-mount DataVizWidget
			// (or any already-mounted one) picks it up without waiting for reload.
			if (data?.key && typeof data.widget_code === 'string') {
				const existing =
					message.dataVizOverrides && typeof message.dataVizOverrides === 'object'
						? message.dataVizOverrides
						: {};
				message.dataVizOverrides = { ...existing, [data.key]: data.widget_code };
				history.messages[resolvedMessageId] = message;
				history = { ...history };
			}
		} else if (type === 'chat:message:error') {
			// Run gate: an error from a SUPERSEDED run (a late terminal racing the
			// retry that already restarted this message id) must not paint an
			// error banner over — or mark done — the LIVE run.
			const errRun = typeof data?.run === 'number' ? data.run : 0;
			const errMirror = streamMirrors.get(resolvedMessageId);
			if (errRun && errMirror?.run && errRun < errMirror.run) {
				chatStreamDebug('[chat-stream] dropping stale-run chat:message:error', {
					resolvedMessageId,
					errRun,
					mirrorRun: errMirror.run
				});
				return;
			}
			const errorOwnedByThisTab = ownsGeneration(resolvedMessageId);
			chatStreamDebug('[chat-stream] resolved chat:message:error — clearing controller', {
				resolvedMessageId,
				error: data?.error,
				ownedByThisMessage: errorOwnedByThisTab
			});
			// A Stop that raced the provider's own failure still lands here. The
			// user's cancel is the authoritative outcome for that run — settle the
			// message without painting a red banner for a turn they killed.
			if (!isUserStoppedMessageId(resolvedMessageId)) {
				message.error = data.error;
			}
			message.done = true;
			const turnSettled = settleGenerationLifecycle(resolvedMessageId);
			releaseStreamMirror(resolvedMessageId);
			// Terminal received — stop the resume poll (the snapshot reconcile below is
			// authoritative), otherwise its next tick fires a redundant loadChat.
			if (turnSettled) stopResumeTaskPolling();
			// Symmetric to the chat:tasks:cancel reconcile: a behind/hidden observer had
			// its live token deltas suppressed, so it holds only the last catch-up
			// snapshot. The backend error path persists the FULL partial content_blocks;
			// pull them so the errored message shows the authoritative partial instead of
			// stale/truncated text. Skipped for the authoring tab (its view is current).
			if (resolvedMessageId && event.chat_id && !errorOwnedByThisTab) {
				void requestStreamSnapshot(resolvedMessageId, event.chat_id, { force: true });
			}
		} else if (type === 'chat:message:follow_ups') {
			message.followUps = data.follow_ups;

			if (autoScroll) {
				// The follow-up row fills the reserve box ResponseMessage already
				// holds open during the turn, so the reply doesn't move — no smooth
				// re-pin (that animated slide WAS the "jump up"). A plain anchored pin
				// settles any sub-pixel residual without animation.
				scrollToBottom();
			}
		} else if (type === 'model-switch:pending') {
			// Model switch has been queued
			message.pendingSwitchModel = data.model_id;
			toast.info(
				$i18n.t('Model switch to {{model}} queued for next iteration', { model: data.model_id })
			);
		} else if (type === 'model-switch:applied') {
			// Model switch was applied
			message.model = data.new_model_id;
			message.modelName =
				$models.find((m) => m.id === data.new_model_id)?.name ?? data.new_model_id;
			message.pendingSwitchModel = null;
			toast.success($i18n.t('Switched to model: {{model}}', { model: message.modelName }));
		} else if (type === 'tool-selection:applied') {
			// The durable/right-aligned history marker arrives through the
			// adjacent content_blocks update. This event is the operation-level
			// acknowledgement and intentionally adds no duplicate toast or row.
		} else if (type === 'tool-selection:error') {
			toast.error(
				$i18n.t('Could not update tools for the running response: {{message}}', {
					message: data?.message ?? $i18n.t('Unknown error')
				})
			);
		} else if (type === 'source' || type === 'citation') {
			// Regular source.
			if (message?.sources) {
				message.sources.push(data);
			} else {
				message.sources = [data];
			}
		} else {
			console.log('Unknown message type', data);
		}

		history.messages[resolvedMessageId] = message;
	};

	const onMessageHandler = async (event: {
		origin: string;
		data: { type: string; text: string };
	}) => {
		if (event.origin !== window.origin) {
			return;
		}

		if (event.data.type === 'action:submit') {
			console.debug(event.data.text);

			if (prompt !== '') {
				await tick();
				submitPrompt(prompt);
			}
		}

		// Replace with your iframe's origin
		if (event.data.type === 'input:prompt') {
			console.debug(event.data.text);

			const inputElement = document.getElementById('chat-input');

			if (inputElement) {
				messageInput?.setText(event.data.text);
				inputElement.focus({ preventScroll: true });
			}
		}

		if (event.data.type === 'input:prompt:submit') {
			console.debug(event.data.text);

			if (event.data.text !== '') {
				await tick();
				submitPrompt(event.data.text);
			}
		}
	};

	const savedModelIds = async () => {
		if (
			$selectedFolder &&
			selectedModels.filter((modelId) => modelId !== '').length > 0 &&
			!arraysEqual($selectedFolder?.data?.model_ids ?? [], selectedModels)
		) {
			const res = await updateFolderById(localStorage.token, $selectedFolder.id, {
				data: {
					model_ids: selectedModels
				}
			});
		}
	};

	let pageSubscribe = null;
	let showControlsSubscribe = null;
	let selectedFolderSubscribe = null;
	let socketSubscribe = null;
	// Current 'connect' handler registered on the app socket (see onMount's
	// socket.subscribe); tracked so it can be removed on re-registration/destroy.
	let socketConnectHandler = null;
	let socketDisconnectHandler = null;
	// True once the socket has dropped during this component's lifetime and a
	// reconnect hasn't yet reconciled. Distinguishes a genuine RE-connect (live
	// pushes were missed — show the sync mark) from the boot-time first connect
	// (nothing was missed — showing "Syncing" there is just load noise).
	let socketDroppedSinceConnect = false;
	let subscribedStreamChatId: string | null = null;
	const streamCapabilities = {
		compact_batch: true,
		replay: true,
		ack: true,
		visibility: true
	};
	const lastAckedStreamVersionByMessage = new Map<string, number>();
	const pendingAckByMessage = new Map<string, number>();
	let ackFlushTimer: ReturnType<typeof setTimeout> | null = null;
	let streamAckIntervalMs = 250;
	const streamCacheTimers = new Map<string, ReturnType<typeof setTimeout>>();
	const streamVisible = () =>
		typeof document === 'undefined' || document.visibilityState === 'visible';
	const streamCacheKey = (chatId: string | null, messageId: string) =>
		chatId && messageId ? `owui:stream-cache:${chatId}:${messageId}` : '';

	const clearStreamCache = (messageId: string) => {
		if (!messageId) return;
		const suffix = `:${messageId}`;
		for (const [key, timer] of streamCacheTimers.entries()) {
			if (!key.endsWith(suffix)) continue;
			clearTimeout(timer);
			streamCacheTimers.delete(key);
		}
		if (typeof sessionStorage === 'undefined') return;
		try {
			for (let i = sessionStorage.length - 1; i >= 0; i--) {
				const key = sessionStorage.key(i);
				if (key?.startsWith('owui:stream-cache:') && key.endsWith(suffix)) {
					sessionStorage.removeItem(key);
				}
			}
		} catch {
			// Best-effort cleanup; storage access can throw in hardened browser contexts.
		}
	};

	const emitSocketAck = <T = any,>(
		event: string,
		payload: any,
		timeoutMs = 3000
	): Promise<T | null> =>
		new Promise((resolve) => {
			if (!$socket) {
				resolve(null);
				return;
			}
			let settled = false;
			const timer = setTimeout(() => {
				if (settled) return;
				settled = true;
				resolve(null);
			}, timeoutMs);
			$socket.emit(event, payload, (response: T) => {
				if (settled) return;
				settled = true;
				clearTimeout(timer);
				resolve(response ?? null);
			});
		});

	const flushStreamAcks = () => {
		ackFlushTimer = null;
		if (!$socket || pendingAckByMessage.size === 0) return;
		const visibleChatId = getVisibleChatId();
		if (!visibleChatId) return;
		for (const [messageId, version] of pendingAckByMessage.entries()) {
			pendingAckByMessage.delete(messageId);
			const lastAcked = lastAckedStreamVersionByMessage.get(messageId) ?? 0;
			if (version <= lastAcked) continue;
			lastAckedStreamVersionByMessage.set(messageId, version);
			$socket.emit('stream:ack', {
				chat_id: visibleChatId,
				message_id: messageId,
				version
			});
		}
	};

	const scheduleStreamAck = (messageId: string, version: number) => {
		if (!messageId || !Number.isFinite(version) || version <= 0) return;
		pendingAckByMessage.set(messageId, Math.max(pendingAckByMessage.get(messageId) ?? 0, version));
		if (ackFlushTimer) return;
		ackFlushTimer = setTimeout(flushStreamAcks, Math.max(50, streamAckIntervalMs || 250));
	};

	const applyStreamRuntimeConfig = (runtime: any) => {
		const ackInterval = Number(runtime?.ack_interval_ms);
		if (Number.isFinite(ackInterval)) {
			streamAckIntervalMs = Math.max(50, Math.min(2000, ackInterval));
		}
	};

	const writeStreamCache = (messageId: string, chatId: string | null) => {
		if (typeof sessionStorage === 'undefined') return;
		const key = streamCacheKey(chatId, messageId);
		if (!key) return;
		const mirror = streamMirrors.get(messageId);
		if (!mirror || mirror.version <= 0 || !Array.isArray(mirror.content_blocks)) return;
		// Never cache a structurally-incoherent mirror (a fabricated block whose
		// type was guessed): a reload would hydrate exactly this corruption at a
		// high version + valid run, replay would come back clean, and no heal
		// would ever fire — the "reload doesn't fix it" loop.
		if (mirror.needsHeal) return;
		try {
			sessionStorage.setItem(
				key,
				JSON.stringify({
					version: mirror.version,
					run: mirror.run,
					content_blocks: mirror.content_blocks,
					ts: Date.now()
				})
			);
		} catch {
			// Best-effort cache; quota failures should never affect streaming.
		}
	};

	const scheduleStreamCacheWrite = (messageId: string, chatId: string | null) => {
		const key = streamCacheKey(chatId, messageId);
		if (!key || streamCacheTimers.has(key)) return;
		streamCacheTimers.set(
			key,
			setTimeout(() => {
				streamCacheTimers.delete(key);
				writeStreamCache(messageId, chatId);
			}, 1000)
		);
	};

	const hydrateStreamFromCache = (messageId: string, chatId: string | null) => {
		if (typeof sessionStorage === 'undefined') return false;
		const key = streamCacheKey(chatId, messageId);
		if (!key) return false;
		try {
			const raw = sessionStorage.getItem(key);
			if (!raw) return false;
			const cached = JSON.parse(raw);
			if (!cached || typeof cached.version !== 'number' || !Array.isArray(cached.content_blocks)) {
				return false;
			}
			const mirror = getOrCreateStreamMirror(messageId);
			// Seed VIRGIN (version-0) mirrors only — the reload/reattach case.
			// A mirror that has already applied deltas, or was authoritatively
			// REWOUND by a heal snapshot (version > 0), must never be overwritten
			// by this tab's own past state: after a heal, the cache could hold
			// exactly the corrupted higher-version blocks the heal replaced (the
			// adopt path rewrites the cache, but keep this direction safe too).
			if (mirror.version !== 0) return false;
			// Run-validate the cache: a cached copy from a SUPERSEDED run (the
			// message was retried/continued since it was written) must never
			// seed the mirror — its version space is dead and its blocks are the
			// failed run's. A cache from a NEWER run resets the mirror first
			// (reconcileMirrorRun), then adopts below.
			if (reconcileMirrorRun(mirror, cached.run) === 'stale') return false;
			if (mirror.version >= cached.version) return false;
			mirror.version = cached.version;
			mirror.content_blocks = cached.content_blocks;
			const message = history.messages?.[messageId];
			if (message) {
				writeMirrorToMessage(mirror, message);
				history.messages[messageId] = message;
				scheduleStreamingMessageFlush(messageId, { runTTS: false, ownerId: messageId });
			}
			scheduleStreamAck(messageId, mirror.version);
			return true;
		} catch {
			return false;
		}
	};

	const unsubscribeStreamChat = (chatIdToUnsubscribe: string | null = subscribedStreamChatId) => {
		if (!chatIdToUnsubscribe || !$socket) return;
		$socket.emit('stream:unsubscribe', { chat_id: chatIdToUnsubscribe });
		if (subscribedStreamChatId === chatIdToUnsubscribe) {
			subscribedStreamChatId = null;
		}
	};

	const subscribeStreamChat = async (chatIdToSubscribe: string | null) => {
		if (!chatIdToSubscribe || $temporaryChatEnabled) return null;
		if (subscribedStreamChatId === chatIdToSubscribe) return null;
		// No point waiting out the 3s ack timeout when the socket isn't even
		// connected — this is what previously stalled offline chat opens for a
		// full 3 seconds before falling through to the also-failing network
		// fetch. All callers already tolerate a null ack (it can already
		// resolve null on a timeout), so this is a safe fast-path.
		if (!$socket?.connected) return null;
		if (subscribedStreamChatId) {
			unsubscribeStreamChat(subscribedStreamChatId);
		}
		const response = await emitSocketAck<{ status?: boolean; streams?: any[]; runtime?: any }>(
			'stream:subscribe',
			{
				chat_id: chatIdToSubscribe,
				visible: streamVisible(),
				capabilities: streamCapabilities
			}
		);
		if (response?.status) {
			applyStreamRuntimeConfig(response.runtime);
			subscribedStreamChatId = chatIdToSubscribe;
		}
		return response;
	};

	const sendStreamVisibility = () => {
		const visibleChatId = getVisibleChatId();
		if (!$socket || !visibleChatId || $temporaryChatEnabled) return;
		$socket.emit('stream:visibility', {
			chat_id: visibleChatId,
			visible: streamVisible()
		});
		if (streamVisible()) {
			void snapshotActiveStreamsForChat(visibleChatId);
		}
	};

	// Cmd/Ctrl+S inside a temporary chat saves it instead of triggering the
	// browser's "Save Page" dialog. Skip when an editable element is focused so
	// it doesn't intercept normal text-input shortcuts.
	const onSaveChatShortcut = (event: KeyboardEvent) => {
		const isSaveCombo = (event.metaKey || event.ctrlKey) && event.key?.toLowerCase() === 's';
		if (!isSaveCombo) return;
		if (!$temporaryChatEnabled) return;

		const target = event.target as HTMLElement | null;
		const tag = target?.tagName?.toLowerCase();
		if (tag === 'input' || tag === 'textarea' || target?.isContentEditable) return;

		if (!history?.currentId || !Object.keys(history.messages ?? {}).length) return;

		event.preventDefault();
		saveTempChatHandler();
	};

	onMount(async () => {
		loading = true;

		window.addEventListener('message', onMessageHandler);
		window.addEventListener('keydown', onSaveChatShortcut);
		window.addEventListener('keyboard-viewport', onKeyboardViewport);
		document.addEventListener('visibilitychange', sendStreamVisibility);
		// A scrollbar drag can end anywhere (including outside the window), so the
		// release has to be observed globally — see onPointerDown.
		window.addEventListener('pointerup', endPointerScroll);
		window.addEventListener('pointercancel', endPointerScroll);
		window.addEventListener('blur', endPointerScroll);

		// Register socket event handler reactively
		socketSubscribe = socket.subscribe((_socket) => {
			if (_socket) {
				// Remove old listener if any
				_socket.off('events', chatEventHandler);
				// Register new listener
				_socket.on('events', chatEventHandler);
				const visibleChatId = getVisibleChatId();
				if (visibleChatId && !$temporaryChatEnabled) {
					// New socket connection/store instance: re-join the visible chat's
					// stream room even if the logical chat id did not change.
					subscribedStreamChatId = null;
					void subscribeStreamChat(visibleChatId);
				}

				// Reload chat if we reconnect while generating to catch missed completion events
				const connectHandler = async () => {
					// A (re)connect means we may have missed deltas while offline — bump the
					// epoch so every LRU snapshot cached under the prior connection is no
					// longer trusted (item 2).
					chatOpenCacheSocketEpoch++;
					const visibleChatId = getVisibleChatId();
					if (!visibleChatId || $temporaryChatEnabled || visibleChatId.startsWith('local:')) {
						return;
					}
					// The stream-room subscription is per-socket and is dropped on
					// disconnect. On a transport reconnect the socket OBJECT is reused,
					// so the store doesn't re-emit and subscribedStreamChatId still
					// equals visibleChatId — which would make subscribeStreamChat
					// early-return and NEVER re-join the room (no live deltas until a
					// manual reload). Reset it first so the re-join actually happens.
					subscribedStreamChatId = null;
					await subscribeStreamChat(visibleChatId);

					// If this view was served from the offline (IDB) copy, a reconnect
					// is the moment to swap in live data — otherwise the stale body and
					// the "Offline copy · saved X ago" banner persist while online, and
					// a message sent now would append onto stale history. The reload
					// re-fetches and drops the __offlineCopy marker.
					if ((chat as any)?.__offlineCopy) {
						console.log('Reconnected with an offline-served chat visible. Reloading...');
						await loadChat();
						return;
					}

					if (generating || taskIds) {
						console.log('Socket reconnected while generating. Checking task status...');
						let taskRes;
						try {
							taskRes = await getChatWorkState(localStorage.token, visibleChatId);
						} catch (e) {
							// The task-status probe threw on reconnect. Don't silently stay on
							// possibly-stale state — fall back to a full reload, matching the
							// resilience of the idle branch below.
							console.error('Failed to check task status on reconnect; reloading', e);
							await loadChat();
							return;
						}
						// Same authoritative reconcile the resume poller uses (chat id
						// passed): records the server no longer lists finished while we
						// were away and are settled here. The two used to disagree —
						// reconnect cleared, the poller kept a stale local record alive —
						// which is how a chat could stay "generating" indefinitely.
						const reconnectGenerations = generationLifecycles.reconcileServerOperations(
							taskRes?.generations,
							navigateGeneration,
							activeSendRetryLoops > 0 ? null : visibleChatId
						);
						reconcileQueueDrain(taskRes);
						if (reconnectGenerations.length === 0) {
							// "No active task" is ambiguous: the task may have FINISHED while
							// we were away — or it may NEVER HAVE STARTED because this tab's
							// send/retry loop is still trying to deliver the POST. In the
							// latter case tearing down + reloading here killed the retry
							// countdown (generating=false) and loadChat() replaced history
							// with a server copy that never saw the turn — the send just
							// vanished (or stranded as an empty done assistant). The retry
							// loop owns convergence; stand down until it exits.
							if (activeSendRetryLoops > 0) {
								// Don't tear down — but DO converge: the retried POST may have
								// already run to COMPLETION while this tab was still
								// reconnecting (fast generation + slow socket backoff), in
								// which case there are no active streams to attach to and no
								// terminal events coming. One forced snapshot of the pending
								// leaf reconciles it from the persisted row (terminal
								// adoption sets done + content), which also lets the retry
								// loop's wait-poll observe completion and exit.
								chatStreamDebug(
									'[chat-stream] reconnect: no active task but a send retry is in flight — reconciling leaf only'
								);
								const pendingLeaf = history?.currentId ? history.messages[history.currentId] : null;
								if (
									pendingLeaf?.role === 'assistant' &&
									pendingLeaf.done !== true &&
									!pendingLeaf.error
								) {
									void requestStreamSnapshot(pendingLeaf.id, visibleChatId, {
										force: true,
										heal: true
									});
								}
								return;
							}
							console.log('Task finished while disconnected. Reloading chat...');
							chatStreamDebug('[chat-stream] reconnect: task finished — settled by reconcile');
							await loadChat();
						} else {
							console.log('Task is still running on the backend. Resuming stream...');
							// v2.1: the RAM stream store is authoritative while a
							// generation is active. Ask the backend for active stream
							// message ids and snapshot those directly instead of
							// guessing from DB `done` flags.
							let activeIds: string[] = [];
							if (($config as any)?.features?.stream_protocol_version === 'v2.1') {
								activeIds = (await snapshotActiveStreamsForChat(visibleChatId)) ?? [];
							}
							// Re-arm the resume-task poll as a missed-terminal backstop. The
							// origin tab (submitPrompt) never started one, so if it reconnects
							// mid-turn and then misses the terminal chat:done (another blip), it
							// would strand without this. Idempotent: early-returns if already armed.
							if (visibleChatId && !visibleChatId.startsWith('local:')) {
								startResumeTaskPolling(visibleChatId);
							}
							// A generation is running, but while disconnected we may have
							// missed (a) queue mutations (enqueue/edit/remove) and (b) a
							// SIBLING turn's terminal — e.g. our own turn M finished and the
							// queued M2 is what's now active. snapshotActiveStreamsForChat
							// only touches in-progress streams, never the queue or a
							// just-completed leaf, so reconcile both: re-hydrate the queue
							// chip, and if the server's current leaf is a message we never
							// received, reload to catch up.
							const meta = await getChatMeta(localStorage.token, visibleChatId).catch(() => null);
							let reconciled = false;
							if (meta) {
								if (Array.isArray(meta.queue)) queue = reconcileServerQueue(meta.queue);
								const serverLeaf = meta?.history?.currentId ?? null;
								if (serverLeaf && !history?.messages?.[serverLeaf]) {
									await loadChat();
									reconciled = true;
								}
							}
							// A PRIOR turn (M1) may have FINISHED during the disconnect while a
							// LATER turn (M2, the one still active) took over — and M2's snapshot
							// above materialized M2 as the current leaf, so the serverLeaf check
							// no longer fires. M1's terminal chat:done is NOT replayable, so it is
							// stranded not-done with a perpetual cursor + truncated text. Detect
							// it: any not-done assistant on the current path that is NOT an active
							// stream is such a straggler — loadChat's sibling-finalize sweep fixes it.
							if (!reconciled && activeSendRetryLoops === 0) {
								// (Guarded on no client-side send retry in flight: this tab's
								// own pending turn is not-done and not an active stream by
								// definition — reloading would wipe it mid-retry.)
								const activeSet = new Set(activeIds);
								const path = history?.currentId
									? createMessagesList(history, history.currentId)
									: [];
								const stranded = path.some(
									(m: any) =>
										m &&
										m.role === 'assistant' &&
										m.done !== true &&
										!m.error &&
										m.userStopped !== true &&
										!activeSet.has(m.id)
								);
								if (stranded) {
									await loadChat();
								}
							}
						}
					} else {
						// C18: an IDLE tab (it finished its own turn) may be viewing a chat
						// whose queue is draining the NEXT item server-side. A dropped
						// chat:queue:drained / chat:queue:updated during the disconnect
						// would otherwise leave the chip strip stale and this tab never
						// attached to the live headless generation — until a manual reload.
						// The old handler skipped this entirely (guarded on generating ||
						// taskIds, both false here). Re-hydrate the queue and attach if a
						// generation is now running.
						try {
							const taskRes = await getChatWorkState(localStorage.token, visibleChatId).catch(
								() => null
							);
							// This is the branch a drain-pending tab lands in (it is idle by
							// definition: its own turn settled), so it is the reconnect that
							// most often owns retiring the bridge.
							reconcileQueueDrain(taskRes);
							if (
								generationLifecycles.reconcileServerOperations(
									taskRes?.generations,
									navigateGeneration
								).length > 0
							) {
								// A generation we're not attached to (a headless drain) — attach.
								await loadChat();
							} else {
								// No live generation to attach to. But a passive viewer may be
								// sitting on an UNFINISHED assistant leaf — e.g. one eagerly
								// materialized from a remote chat:user-message (queue drain /
								// cross-device), or from a delta — whose terminal chat:done fired
								// while we were disconnected. chat:done is NOT replayable, so
								// without a reconcile that leaf would render a perpetual typewriter
								// cursor and keep the input bar in its working state forever. If the
								// current leaf is a not-done assistant, loadChat() to pull its
								// authoritative (now terminal) state — loadChat re-hydrates the
								// queue too, so it subsumes the chip refresh. Otherwise just refresh
								// the queue chip strip in case a chat:queue:updated was missed.
								const leaf = history?.currentId ? history.messages[history.currentId] : null;
								if (leaf && leaf.role === 'assistant' && leaf.done !== true) {
									await loadChat();
								} else {
									const meta = await getChatMeta(localStorage.token, visibleChatId).catch(
										() => null
									);
									// G2 (multi-client): while we were disconnected another device may
									// have sent AND fully completed a new turn in this chat. That turn's
									// terminal chat:done is NOT replayable, so we'd silently keep showing
									// the stale prior leaf forever. If the server's authoritative current
									// leaf is a message we never received, reload to catch up. Gated on
									// "we don't have it locally" so a user deliberately viewing an older
									// branch (whose leaf we DO hold) is never yanked to the latest.
									const serverLeaf = meta?.history?.currentId ?? null;
									// Also catch an in-place change that KEEPS the leaf id — a Continue
									// Response reuses the same assistant id, so `!history.messages[leaf]`
									// is false, but the server's updated_at advanced past what we last
									// knew. Reloading is safe (same-chat reconcile, no scroll yank).
									const serverUpdatedAt = meta?.updated_at;
									const changedWhileGone =
										typeof serverUpdatedAt === 'number' &&
										typeof chat?.updated_at === 'number' &&
										serverUpdatedAt > chat.updated_at;
									if (!meta) {
										// The reconcile probe FAILED on reconnect. Don't silently assume
										// "nothing changed" (the generating branch reloads on a failed probe
										// for the same reason) — a turn may have completed while we were
										// gone. Reload to reconcile against server truth.
										await loadChat();
									} else if ((serverLeaf && !history?.messages?.[serverLeaf]) || changedWhileGone) {
										await loadChat();
									} else if (Array.isArray(meta.queue)) {
										queue = reconcileServerQueue(meta.queue);
									}
								}
							}
						} catch (e) {
							console.error('Failed queue/stream resync on reconnect', e);
						}
					}
				};
				// Deregister the PREVIOUS handler (off with the fresh closure removes
				// nothing) and remember the current one so onDestroy can remove it —
				// otherwise every chat-route leave/return leaked a live handler that
				// kept running loadChat() into a destroyed component on reconnects.
				if (socketConnectHandler) {
					_socket.off('connect', socketConnectHandler);
				}
				// Freshness tracking around the reconnect reconcile. Only a genuine
				// RE-connect (socketDroppedSinceConnect) tracks — the boot-time
				// first 'connect' missed nothing and must not flash the mark. The
				// mark is released EARLY at history-commit (inside loadChat) when
				// the reconcile swaps in a fresh body — the finally here only
				// covers the "checked, nothing changed" outcome.
				const trackedConnectHandler = async () => {
					const isReconnect = socketDroppedSinceConnect;
					socketDroppedSinceConnect = false;
					const vis = getVisibleChatId();
					const track = isReconnect && !!vis && !$temporaryChatEnabled && !vis.startsWith('local:');
					if (track) chatViewUnverified = true;
					try {
						await connectHandler();
					} finally {
						// Release on ANY reconnect (not just tracked ones): the
						// reconcile closes the staleness window globally, and a latch
						// left over from a drop on a since-navigated-away chat must
						// not flash "Syncing" on the next unrelated open.
						if (isReconnect) chatViewUnverified = false;
					}
				};
				socketConnectHandler = trackedConnectHandler;
				_socket.on('connect', trackedConnectHandler);
				// The staleness window OPENS when the link dies (phone lock, tunnel,
				// backgrounding) — not when it comes back. Without this, a wake
				// shows nothing while the reconnect backoff runs, exactly the
				// moment the user is reading possibly-stale data.
				if (socketDisconnectHandler) {
					_socket.off('disconnect', socketDisconnectHandler);
				}
				const trackedDisconnectHandler = () => {
					socketDroppedSinceConnect = true;
					const vis = getVisibleChatId();
					if (vis && !$temporaryChatEnabled && !vis.startsWith('local:')) {
						chatViewUnverified = true;
					}
				};
				socketDisconnectHandler = trackedDisconnectHandler;
				_socket.on('disconnect', trackedDisconnectHandler);
			}
		});

		pageSubscribe = page.subscribe(async (p) => {
			if (p.url.pathname === '/') {
				await tick();
				initNewChat();
			}
		});

		const storageChatInput = sessionStorage.getItem(
			`chat-input${chatIdProp ? `-${chatIdProp}` : ''}`
		);

		if (!chatIdProp) {
			loading = false;
			await tick();
		}

		if (storageChatInput) {
			prompt = '';
			messageInput?.setText('');

			files = [];
			selectedToolIds = [];
			selectedFilterIds = [];
			webSearchEnabled = false;
			studyModeEnabled = false;
			dataVizEnabled = false;
			automationsEnabled = false;
			imageGenerationEnabled = false;

			try {
				const input = JSON.parse(storageChatInput);

				if (!$temporaryChatEnabled) {
					messageInput?.setText(input.prompt);
					files = input.files;
					// Tool/feature state from a draft is only restored when the USER
					// curated it (toolSelectionDirty was saved with the draft) — see
					// the matching guard in the loadChat draft restore.
					if (input.toolSelectionDirty) {
						if (input.selectedToolIds && input.selectedToolIds.length > 0) {
							selectedToolIds = input.selectedToolIds;
						}
						selectedFilterIds = input.selectedFilterIds;
						if (!chatIdProp) {
							webSearchEnabled = input.webSearchEnabled;
						}
						imageGenerationEnabled = input.imageGenerationEnabled;
						studyModeEnabled = input.studyModeEnabled ?? false;
						dataVizEnabled = input.dataVizEnabled ?? false;
						automationsEnabled = input.automationsEnabled ?? false;
						toolSelectionDirty = true;
					}
				}
			} catch (e) {}
		}

		showControlsSubscribe = showControls.subscribe(async (value) => {
			if (controlPane && !$mobile) {
				try {
					if (value) {
						controlPaneComponent.openPane();
					} else {
						controlPane.collapse();
					}
				} catch (e) {
					// ignore
				}
			}

			if (!value) {
				showCallOverlay.set(false);
				showOverview.set(false);
				showArtifacts.set(false);
				showEmbeds.set(false);
			}
		});

		selectedFolderSubscribe = selectedFolder.subscribe(async (folder) => {
			if (folder?.data?.model_ids && !arraysEqual(selectedModels, folder.data.model_ids)) {
				selectedModels = folder.data.model_ids;

				console.log('Set selectedModels from folder data:', selectedModels);
			}
		});

		if (!isOnScreenKeyboardDevice()) {
			const chatInput = document.getElementById('chat-input');
			chatInput?.focus({ preventScroll: true });
		}
	});

	onDestroy(() => {
		try {
			deferredUploadSubmitToken += 1;
			deferredUploadSubmit = null;
			stopSubagentUpdateBatching();
			for (const messageId of streamFlushes.keys()) {
				cancelStreamingMessageFlush(messageId);
			}
			// Release any remaining stream mirrors (content_blocks + tool_results
			// Maps) so they don't outlive the component on unmount/navigation.
			streamMirrors.clear();
			pageSubscribe();
			showControlsSubscribe();
			selectedFolderSubscribe();
			unsubscribeStreamChat();
			socketSubscribe?.();
			chatIdUnsubscriber?.();
			window.removeEventListener('message', onMessageHandler);
			window.removeEventListener('keydown', onSaveChatShortcut);
			window.removeEventListener('keyboard-viewport', onKeyboardViewport);
			document.removeEventListener('visibilitychange', sendStreamVisibility);
			window.removeEventListener('pointerup', endPointerScroll);
			window.removeEventListener('pointercancel', endPointerScroll);
			window.removeEventListener('blur', endPointerScroll);
			if (ackFlushTimer) {
				clearTimeout(ackFlushTimer);
				ackFlushTimer = null;
			}
			for (const timer of streamCacheTimers.values()) {
				clearTimeout(timer);
			}
			streamCacheTimers.clear();
			$socket?.off('events', chatEventHandler);
			if (socketConnectHandler) {
				$socket?.off('connect', socketConnectHandler);
				socketConnectHandler = null;
			}
			if (socketDisconnectHandler) {
				$socket?.off('disconnect', socketDisconnectHandler);
				socketDisconnectHandler = null;
			}
		} catch (e) {
			console.error(e);
		}
	});

	// File upload functions

	const uploadGoogleDriveFile = async (fileData) => {
		console.log('Starting uploadGoogleDriveFile with:', {
			id: fileData.id,
			name: fileData.name,
			url: fileData.url,
			headers: {
				Authorization: `Bearer ${token}`
			}
		});

		// Validate input
		if (!fileData?.id || !fileData?.name || !fileData?.url || !fileData?.headers?.Authorization) {
			throw new Error('Invalid file data provided');
		}

		const tempItemId = uuidv4();
		const fileItem = {
			type: 'file',
			file: '',
			id: null,
			url: fileData.url,
			name: fileData.name,
			status: 'uploading',
			error: '',
			itemId: tempItemId,
			size: 0
		};

		try {
			files = [...files, fileItem];
			console.log('Processing web file with URL:', fileData.url);

			// Configure fetch options with proper headers
			const fetchOptions = {
				headers: {
					Authorization: fileData.headers.Authorization,
					Accept: '*/*'
				},
				method: 'GET'
			};

			// Attempt to fetch the file
			console.log('Fetching file content from Google Drive...');
			const fileResponse = await fetch(fileData.url, fetchOptions);

			if (!fileResponse.ok) {
				const errorText = await fileResponse.text();
				throw new Error(`Failed to fetch file (${fileResponse.status}): ${errorText}`);
			}

			// Get content type from response
			const contentType = fileResponse.headers.get('content-type') || 'application/octet-stream';
			console.log('Response received with content-type:', contentType);

			// Convert response to blob
			console.log('Converting response to blob...');
			const fileBlob = await fileResponse.blob();

			if (fileBlob.size === 0) {
				throw new Error('Retrieved file is empty');
			}

			console.log('Blob created:', {
				size: fileBlob.size,
				type: fileBlob.type || contentType
			});

			// Create File object with proper MIME type
			const file = new File([fileBlob], fileData.name, {
				type: fileBlob.type || contentType
			});

			console.log('File object created:', {
				name: file.name,
				size: file.size,
				type: file.type
			});

			if (file.size === 0) {
				throw new Error('Created file is empty');
			}

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

			// Upload file to server
			console.log('Uploading file to server...');
			const uploadedFile = await uploadFile(localStorage.token, file, metadata);

			if (!uploadedFile) {
				throw new Error('Server returned null response for file upload');
			}

			console.log('File uploaded successfully:', uploadedFile);

			// Update file item with upload results
			fileItem.status = 'uploaded';
			fileItem.file = uploadedFile;
			fileItem.id = uploadedFile.id;
			fileItem.size = file.size;
			fileItem.url = `${WEBUI_API_BASE_URL}/files/${uploadedFile.id}`;

			files = files;
			toast.success($i18n.t('File uploaded successfully'));
		} catch (e) {
			console.error('Error uploading file:', e);
			files = files.filter((f) => f.itemId !== tempItemId);
			toast.error(
				$i18n.t('Error uploading file: {{error}}', {
					error: e.message || 'Unknown error'
				})
			);
		}
	};

	const uploadWeb = async (url) => {
		console.log(url);

		const fileItem = {
			type: 'text',
			name: url,
			status: 'uploading',
			url: url,
			error: ''
		};

		try {
			files = [...files, fileItem];
			const res = await processWeb(localStorage.token, '', url);

			if (res) {
				fileItem.status = 'uploaded';
				fileItem.file = {
					...res.file,
					...fileItem.file
				};

				files = files;
			}
		} catch (e) {
			// Remove the failed doc from the files array
			files = files.filter((f) => f.name !== url);
			toast.error(JSON.stringify(e));
		}
	};

	const uploadYoutubeTranscription = async (url) => {
		console.log(url);

		const fileItem = {
			type: 'text',
			name: url,
			status: 'uploading',
			context: 'full',
			url: url,
			error: ''
		};

		try {
			files = [...files, fileItem];
			const res = await processYoutubeVideo(localStorage.token, url);

			if (res) {
				fileItem.status = 'uploaded';
				fileItem.file = {
					...res.file,
					...fileItem.file
				};
				files = files;
			}
		} catch (e) {
			// Remove the failed doc from the files array
			files = files.filter((f) => f.name !== url);
			toast.error(`${e}`);
		}
	};

	//////////////////////////
	// Web functions
	//////////////////////////

	const initNewChat = async () => {
		console.log('initNewChat');
		if ($user?.role !== 'admin' && $user?.permissions?.chat?.temporary_enforced) {
			await temporaryChatEnabled.set(true);
		}

		if ($settings?.temporaryChatByDefault ?? false) {
			if ($temporaryChatEnabled === false) {
				await temporaryChatEnabled.set(true);
			} else if ($temporaryChatEnabled === null) {
				// if set to null set to false; refer to temp chat toggle click handler
				await temporaryChatEnabled.set(false);
			}
		}

		const availableModels = $models
			.filter((m) => !(m?.info?.meta?.hidden ?? false))
			.map((m) => m.id);

		if ($page.url.searchParams.get('models') || $page.url.searchParams.get('model')) {
			const urlModels = (
				$page.url.searchParams.get('models') ||
				$page.url.searchParams.get('model') ||
				''
			)?.split(',');

			if (urlModels.length === 1) {
				const m = $models.find((m) => m.id === urlModels[0]);
				if (!m) {
					const modelSelectorButton = document.getElementById('model-selector-0-button');
					if (modelSelectorButton) {
						modelSelectorButton.click();
						await tick();

						const modelSelectorInput = document.getElementById('model-search-input');
						if (modelSelectorInput) {
							modelSelectorInput.focus();
							modelSelectorInput.value = urlModels[0];
							modelSelectorInput.dispatchEvent(new Event('input'));
						}
					}
				} else {
					selectedModels = urlModels;
				}
			} else {
				selectedModels = urlModels;
			}

			selectedModels = selectedModels.filter((modelId) =>
				$models.map((m) => m.id).includes(modelId)
			);
		} else {
			if ($selectedFolder?.data?.model_ids) {
				selectedModels = $selectedFolder?.data?.model_ids;
			} else {
				if (sessionStorage.selectedModels) {
					selectedModels = JSON.parse(sessionStorage.selectedModels);
					sessionStorage.removeItem('selectedModels');
				} else {
					if ($settings?.models) {
						selectedModels = $settings?.models;
					} else if ($config?.default_models) {
						console.log($config?.default_models.split(',') ?? '');
						selectedModels = $config?.default_models.split(',');
					}
				}
			}

			if (availableModels.length > 0) {
				selectedModels = selectedModels.filter((modelId) => availableModels.includes(modelId));
			}
		}

		if (
			availableModels.length > 0 &&
			(selectedModels.length === 0 || (selectedModels.length === 1 && selectedModels[0] === ''))
		) {
			selectedModels = [availableModels?.at(0) ?? ''];
		}

		// Restore this model's persisted service tier now that selection has
		// settled, so a fresh chat starts with the same tier the docked
		// composer will show after the first send — not a value a remounted
		// composer instance might restore differently.
		if (selectedModels.length === 1 && selectedModels[0]) {
			restoreServiceTierForModel(selectedModels[0]);
		}

		await showControls.set(false);
		await showCallOverlay.set(false);
		await showOverview.set(false);
		await showArtifacts.set(false);

		if ($page.url.pathname.includes('/c/')) {
			window.history.replaceState(history.state, '', `/`);
		}

		autoScroll = true;

		// Check if we should preserve state (from temp chat toggle)
		const preserveState = sessionStorage.getItem('tempChatPreserveState');
		const preservedWebSearch = preserveState ? webSearchEnabled : false;
		if (preserveState) {
			sessionStorage.removeItem('tempChatPreserveState');
		}

		resetInput();
		await chatId.set('');
		await chatTitle.set('');

		history = {
			messages: {},
			currentId: null
		};
		subagentLiveStates.set({});
		questionStates.set({});
		reasoningBlockOpenState.set({});
		messageEditingIds.set(new Set());
		messageHeightSweeper.reset();

		chatFiles = [];
		params = {};
		queue = [];
		_wasGenerating = false;

		if ($page.url.searchParams.get('youtube')) {
			uploadYoutubeTranscription(
				`https://www.youtube.com/watch?v=${$page.url.searchParams.get('youtube')}`
			);
		}

		if ($page.url.searchParams.get('load-url')) {
			await uploadWeb($page.url.searchParams.get('load-url'));
		}

		if ($page.url.searchParams.get('web-search') === 'true') {
			webSearchEnabled = true;
		}

		if ($page.url.searchParams.get('image-generation') === 'true') {
			imageGenerationEnabled = true;
		}

		if ($page.url.searchParams.get('reasoning')) {
			const reasoningParam = $page.url.searchParams.get('reasoning')?.toLowerCase();
			if ((REASONING_EFFORT_ORDER as string[]).includes(reasoningParam)) {
				reasoning.effort = reasoningParam;
			}
		}

		if ($page.url.searchParams.get('tools')) {
			selectedToolIds = $page.url.searchParams
				.get('tools')
				?.split(',')
				.map((id) => id.trim())
				.filter((id) => id);
		} else if ($page.url.searchParams.get('tool-ids')) {
			selectedToolIds = $page.url.searchParams
				.get('tool-ids')
				?.split(',')
				.map((id) => id.trim())
				.filter((id) => id);
		}

		if ($page.url.searchParams.get('call') === 'true') {
			showCallOverlay.set(true);
			showControls.set(true);
		}

		if ($page.url.searchParams.get('q')) {
			const q = $page.url.searchParams.get('q') ?? '';
			messageInput?.setText(q);

			if (q) {
				if (($page.url.searchParams.get('submit') ?? 'true') === 'true') {
					await tick();
					submitPrompt(q);
				}
			}
		}

		if ($models.length > 0) {
			selectedModels = selectedModels.map((modelId) =>
				$models.map((m) => m.id).includes(modelId) ? modelId : ''
			);
		}

		// Restore preserved state from temp chat toggle
		if (preserveState && preservedWebSearch) {
			const model = atSelectedModel ?? $models.find((m) => m.id === selectedModels[0]);
			if (model?.info?.meta?.capabilities?.web_search ?? true) {
				webSearchEnabled = true;
			}
		}

		// A new chat is "user-curated" (and therefore must survive a model switch
		// without being reset to model defaults) when an explicit source — URL
		// params or a preserved temp-chat toggle — populated the selection. A bare
		// new chat stays non-dirty so switching models still applies the newly
		// chosen model's defaults.
		toolSelectionDirty =
			Boolean($page.url.searchParams.get('tools')) ||
			Boolean($page.url.searchParams.get('tool-ids')) ||
			$page.url.searchParams.get('web-search') === 'true' ||
			$page.url.searchParams.get('image-generation') === 'true' ||
			Boolean(preserveState && preservedWebSearch);

		// Touch predicate, not the width-based $mobile store: a wide iPad isn't
		// "$mobile" but focusing here still summons its on-screen keyboard.
		if (!isOnScreenKeyboardDevice()) {
			const chatInput = document.getElementById('chat-input');
			setTimeout(() => chatInput?.focus({ preventScroll: true }), 0);
		}
	};

	// Recent-chat LRU (item 2): the stitched getChatByIdTail result keyed by chatId.
	// Serving from here turns a user-initiated switch back to a recently-viewed chat
	// into ZERO history bytes over the wire — critical on metered links. Entries are
	// only ever served when we can PROVE they're still current (see loadChat), and are
	// invalidated on any event/local-write touching the chat, so a stale snapshot can
	// never render.
	const CHAT_OPEN_CACHE_MAX = 8;
	const chatOpenCache = new Map<
		string,
		{ updatedAt: number; data: any; tags: any[]; epoch: number }
	>();
	// Bumped on every socket (re)connect. A cache entry is only trusted while this
	// epoch is unchanged AND the socket is currently connected — so any disconnect
	// (with or without a reconnect) since the entry was cached forces a fresh fetch,
	// because deltas may have been missed while we were offline.
	let chatOpenCacheSocketEpoch = 0;
	// Chat ids with PATCHes in flight (refcounted — saves can overlap): the
	// sidebar row still carries the pre-save updated_at during the round-trip,
	// so the kept-stale offline IDB entry would pass the equality gate and
	// race-serve pre-edit content. The IDB race tier skips these ids.
	const offlineRaceDirtyChatIds = new Map<string, number>();
	const markOfflineRaceDirty = (id: string) => {
		offlineRaceDirtyChatIds.set(id, (offlineRaceDirtyChatIds.get(id) ?? 0) + 1);
	};
	const unmarkOfflineRaceDirty = (id: string) => {
		const n = (offlineRaceDirtyChatIds.get(id) ?? 1) - 1;
		if (n <= 0) {
			offlineRaceDirtyChatIds.delete(id);
		} else {
			offlineRaceDirtyChatIds.set(id, n);
		}
	};

	const invalidateChatOpenCache = (id: string | null | undefined) => {
		if (id) chatOpenCache.delete(id);
	};

	const chatOpenCachePut = (
		id: string,
		updatedAt: number,
		data: any,
		tags: any[],
		epoch: number
	) => {
		chatOpenCache.delete(id);
		chatOpenCache.set(id, { updatedAt, data, tags, epoch });
		while (chatOpenCache.size > CHAT_OPEN_CACHE_MAX) {
			const oldest = chatOpenCache.keys().next().value;
			if (oldest === undefined) break;
			chatOpenCache.delete(oldest);
		}
	};

	// Post-paint stream/task reconcile — the single owner of done-flag forcing,
	// active-stream flips, mirror run seeding, snapshot replay, browser-frame
	// seeding and the generating/polling flip. loadChat schedules it UNAWAITED
	// right before returning, so first paint never waits on the subscribe ack
	// (3s timeout on a zombie socket) or the task-ids/active-streams RTTs; a
	// chat with live work repaints into its working state the moment these
	// land (sub-second), exactly like any reattach window.
	const applyStreamTaskState = async (
		generation: number,
		cid: string,
		taskResIn: any = null,
		subscribePromise: Promise<any> | null = null,
		activeIn: {
			generations?: ServerGenerationOperation[];
			rerun_task_ids?: any[];
			subagent_rerun_entry_keys?: string[];
			streams?: any[];
		} | null = null
	) => {
		try {
			let _taskRes: any = null;
			let _activeStreamsRes: any = null;
			if (activeIn && typeof activeIn === 'object') {
				// The open response bundled the authoritative task/stream state
				// (or a 304 proved there is none — see getChatByIdTail). Zero
				// follow-up round-trips.
				_taskRes = {
					...activeIn,
					generations: Array.isArray(activeIn.generations) ? activeIn.generations : [],
					rerun_task_ids: Array.isArray(activeIn.rerun_task_ids) ? activeIn.rerun_task_ids : [],
					subagent_rerun_entry_keys: Array.isArray(activeIn.subagent_rerun_entry_keys)
						? activeIn.subagent_rerun_entry_keys
						: []
				};
				_activeStreamsRes = { streams: Array.isArray(activeIn.streams) ? activeIn.streams : [] };
			} else {
				const _subscribeRes = subscribePromise ? await subscribePromise : null;
				if (generation !== navigateGeneration || get(chatId) !== cid) return;

				// The subscribe ack is authoritative only for stream state. A
				// generation operation exists before its stream/task is attached,
				// so even an empty stream ack cannot prove that the chat has no
				// work. Fetch the compact work state whenever the chat-open
				// response did not already bundle it.
				const _ackStreams = Array.isArray(_subscribeRes?.streams) ? _subscribeRes.streams : null;
				const _ackPresent = _ackStreams !== null;
				const _ackHasActivity = _ackPresent && _ackStreams.length > 0;
				const _needStreamsHttp = !_ackPresent || _ackHasActivity;

				try {
					_taskRes = taskResIn ? await taskResIn : null;
				} catch {
					_taskRes = null;
				}
				const [_taskResFinal, _streamsFinal] = await Promise.all([
					_taskRes !== null
						? Promise.resolve(_taskRes)
						: getChatWorkState(localStorage.token, cid).catch(() => null),
					_needStreamsHttp
						? getActiveStreamsByChatId(localStorage.token, cid).catch(() => null)
						: Promise.resolve(_subscribeRes)
				]);
				_taskRes = _taskResFinal;
				_activeStreamsRes = _streamsFinal;
			}
			if (generation !== navigateGeneration || get(chatId) !== cid) return;
			const messages = history?.messages as Record<string, any> | undefined;
			if (!messages) return;

			const taskStateKnown = _taskRes !== null && typeof _taskRes === 'object';
			const rerunTaskIdSet = new Set(_taskRes?.rerun_task_ids ?? []);
			// Authoritative ONLY when the work-state probe actually answered: a
			// failed probe means "unknown", and settling records on it would falsely
			// finish a live turn. This is the same authoritative reconcile the resume
			// poller and the reconnect handler use — all three used to answer "is
			// this chat still generating" slightly differently.
			const liveGenerations = generationLifecycles.reconcileServerOperations(
				_taskRes?.generations,
				navigateGeneration,
				taskStateKnown ? cid : null
			);
			// A chat opened (or reloaded) mid-handoff gets the same authoritative
			// answer, so the composer never inherits a bridge the server can't
			// account for — and, conversely, an open that lands while `draining`
			// still stands keeps the bar working instead of flashing idle.
			reconcileQueueDrain(_taskRes);
			const activeParentTaskIds = liveGenerations
				.map((operation) => operation.task_id)
				.filter(Boolean);
			const pendingMessageIds = new Set(
				liveGenerations
					.filter((operation) => !operation.task_id)
					.map((operation) => operation.message_id)
			);
			const hasActiveTasksOnLoad = liveGenerations.length > 0;

			if (taskStateKnown && history.currentId) {
				for (const message of Object.values(messages)) {
					if (message?.role === 'assistant') {
						const localGeneration = generationLifecycles.get(message.id);
						const hasLiveLocalTransport =
							!!localGeneration?.controller &&
							!localGeneration.controller.signal.aborted &&
							!['stopped', 'terminal'].includes(localGeneration.phase);
						if (
							message.done === false &&
							!message.error &&
							(hasLiveLocalTransport ||
								(hasActiveTasksOnLoad &&
									(pendingMessageIds.size === 0 || pendingMessageIds.has(message.id))))
						) {
							continue;
						}
						Object.assign(message, inactiveAssistantTerminalPatch(message));
					}
				}
			}

			const activeStreamMessageIds = Array.isArray(_activeStreamsRes?.streams)
				? _activeStreamsRes.streams
						.map((stream: any) => stream?.message_id)
						.filter(
							(id: unknown): id is string =>
								typeof id === 'string' && id.length > 0 && !isUserStoppedMessageId(id, messages)
						)
				: [];
			for (const mid of activeStreamMessageIds) {
				const message = messages?.[mid];
				if (message && message.role === 'assistant') {
					message.done = false;
				}
			}
			// A hidden tab intentionally ignores Chat A's terminal socket event
			// while Chat B is visible. On return, the durable done/error/stop flag
			// is authoritative. Retire only handed-off records (controller=null);
			// a controller still present is an actual local preflight/direct
			// stream that task-state cannot see yet.
			for (const record of generationLifecycles.activeForChat(cid)) {
				const message = messages[record.messageId];
				if (
					!record.controller &&
					(message?.done === true ||
						!!message?.error ||
						isUserStoppedMessageId(record.messageId, messages))
				) {
					generationLifecycles.terminal(record.messageId, record.generationId);
				}
			}
			// Seed each active stream's mirror with the server's CURRENT run id so
			// snapshot/replay reconciliation starts from the right run.
			for (const stream of Array.isArray(_activeStreamsRes?.streams)
				? _activeStreamsRes.streams
				: []) {
				const mid = stream?.message_id;
				if (
					typeof mid === 'string' &&
					mid.length > 0 &&
					!isUserStoppedMessageId(mid, messages) &&
					typeof stream?.run === 'number' &&
					stream.run > 0
				) {
					reconcileMirrorRun(getOrCreateStreamMirror(mid), stream.run);
				}
			}

			history = history; // commit done-flag flips

			if (activeStreamMessageIds.length > 0) {
				await Promise.all(activeStreamMessageIds.map((mid) => requestStreamSnapshot(mid, cid)));

				// Live browser frames are fire-and-forget (not in the stream
				// snapshot) — seed the panel from the host workspace, best-effort.
				void Promise.all(
					activeStreamMessageIds.map(async (mid) => {
						try {
							const f = await getBrowserFrame(localStorage.token, mid, cid);
							if (!f) return;
							const sessions = Array.isArray(f.sessions) && f.sessions.length ? f.sessions : null;
							const entries = sessions ?? (f.frame ? [{ ...f, session: undefined }] : []);
							let anyLive = false;
							for (const entry of entries) {
								if (!entry?.frame) continue;
								const key = entry.session || mid;
								const label = browserSessionLabel(entry.session);
								browserLiveStates.update((s) => ({
									...s,
									[key]: {
										...(s[key] ?? {}),
										...entry,
										startedAt: s[key]?.startedAt ?? entry?.startedAt ?? Date.now(),
										...(label ? { label } : {})
									}
								}));
								if (!entry.done) anyLive = true;
							}
							if (anyLive && !get(showBrowserPanel)) {
								showBrowserPanel.set(true);
								showControls.set(true);
							}
						} catch (e) {
							// ignore — panel simply has no seed frame
						}
					})
				);
			}
			if (generation !== navigateGeneration || get(chatId) !== cid) return;

			// If work is still running on the backend (we reloaded mid-stream),
			// register it and poll task status so a missed terminal event can't
			// strand the turn. An active STREAM with no matching lifecycle record
			// is observed work — record it so the derived composer state sees it.
			// (The reconcile above already carried the live task ids onto their
			// records, which is where `taskIds` reads them from.)
			stopResumeTaskPolling();
			if (
				activeParentTaskIds.length > 0 ||
				liveGenerations.length > 0 ||
				activeStreamMessageIds.length > 0 ||
				generationLifecycles.activeForChat(cid).length > 0
			) {
				for (const mid of activeStreamMessageIds) {
					generationLifecycles.observe(cid, mid, navigateGeneration);
				}
				startResumeTaskPolling(cid);
			} else if (taskStateKnown) {
				// Detached reruns are independent of the main composer, but the
				// poller still needs to reload their durable terminal result if
				// this tab misses the socket event. A drain bridge that SURVIVED the
				// reconcile above (the server still reports `draining`) needs the
				// poll for the same reason — it is what will retire it.
				if (rerunTaskIdSet.size > 0 || queueDrainPending) {
					startResumeTaskPolling(cid);
				}
			} else {
				// A failed task-registry probe means "unknown", never "empty".
				// Preserve the DB/local generating flags and retry; otherwise a
				// transient Redis outage can falsely finish a live parent/rerun.
				startResumeTaskPolling(cid);
			}
		} catch (e) {
			console.error('stream/task reconcile failed', e);
		}
	};

	const loadChat = async (
		generation: number = navigateGeneration,
		userInitiated: boolean = false,
		isSoftNav: boolean = false,
		revalidationStartedAtModelRevision: number | null = null
	) => {
		const currentChatId = chatIdProp || getVisibleChatId();

		if (!currentChatId) {
			return false;
		}

		// Is this a reconcile reload of the chat we're ALREADY viewing (queue drain,
		// completion/error/done backstop, reconnect attach, resume-poll, remote-user
		// orphan)? Those must not steal follow-intent from a reader who scrolled up.
		// A genuine navigation to a different chat (currentId differs) still pins.
		const isSameChatReload = get(chatId) === currentChatId;

		chatId.set(currentChatId);

		if ($temporaryChatEnabled) {
			temporaryChatEnabled.set(false);
		}

		// Subscribe kickoff — deliberately NOT awaited. Its ack only feeds the
		// stream/task reconcile (which now runs AFTER first paint), and its 3s
		// timeout on a connected-but-zombie socket used to block the entire open.
		// Deltas that arrive before the reconcile are buffered/replayed by the
		// v2.1 mirror machinery exactly as during any reattach window.
		const _subscribePromise = subscribeStreamChat(currentChatId);

		let _chat;
		// Task ids for the reconcile: a VALUE (legacy preloadedData), a PROMISE
		// (route-loader bundle), or null (fetch if the subscribe ack demands it).
		let _taskResIn: any = null;
		let _revalidationStartedAtModelRevision = revalidationStartedAtModelRevision;

		// Offline/network-failure fallback: getChatByIdTail (via apis/chats)
		// throws a STRUCTURED error ({isNetworkError, status?, detail?}). Only a
		// genuine network failure (fetch never reached the server, or we know
		// we're offline) is eligible to fall back to the local offline copy — a
		// real HTTP status (e.g. 401 for a deleted/access-revoked chat) must
		// ALWAYS keep today's goto('/') behavior and must NEVER consult the
		// offline store, otherwise a deleted/revoked chat could resurrect from
		// stale local cache while online.
		const loadPaginatedChat = async () => {
			// Ride the conditional/incremental ladder instead of refetching the
			// full tail: reconnect reconciles and fallback loads land here, and
			// they used to re-download every body even when the local copy only
			// missed a message or two. IDB entry preferred (carries the etag for
			// a true 304); the in-memory LRU still enables the manifest delta.
			let _condEntry = null;
			if (!currentChatId.startsWith('local:')) {
				try {
					_condEntry = $user?.id ? await getOfflineChat($user.id, currentChatId) : null;
				} catch {
					_condEntry = null;
				}
				if (!_condEntry?.data) {
					const lru = chatOpenCache.get(currentChatId);
					if (lru?.data) {
						_condEntry = { data: lru.data, tags: lru.tags, updatedAt: lru.updatedAt };
					}
				}
			}
			const chat = await getChatByIdTail(localStorage.token, currentChatId, 25, {
				etagEntry: _condEntry
			}).catch(async (error: any) => {
				if (error?.isNetworkError) {
					const userId = $user?.id;
					const offlineEntry = userId ? await getOfflineChat(userId, currentChatId) : null;
					if (offlineEntry) {
						const offlineChat = cloneState(offlineEntry.data);
						if (Array.isArray(offlineEntry.tags)) {
							Object.defineProperty(offlineChat, '__tailTags', {
								value: cloneState(offlineEntry.tags),
								enumerable: false,
								configurable: true
							});
						}
						// Non-enumerable marker so the UI (offline banner, built in
						// parallel) can tell this render came from local storage
						// rather than a live fetch, without it leaking into any
						// JSON.stringify/structuredClone of the chat payload.
						Object.defineProperty(offlineChat, '__offlineCopy', {
							value: { storedAt: offlineEntry.storedAt },
							enumerable: false,
							configurable: true
						});
						return offlineChat;
					}
					// Genuinely offline with no local copy — say so instead of
					// silently bouncing to the home screen.
					toast.error($i18n.t('This chat is not available offline.'));
				} else if (error?.status === 401 || error?.status === 404) {
					// Deleted / access revoked — drop the local copy so it can't
					// serve offline later (same rule as the prefetch sweep).
					const _missUserId = $user?.id;
					if (_missUserId) {
						void removeOfflineChat(_missUserId, currentChatId);
					}
				}
				await goto('/');
				return null;
			});

			return chat;
		};

		// ---------------------------------------------------------------------
		// Local-first tier: ANY local copy — in-memory LRU first, then the IDB
		// store — paints IMMEDIATELY, even stale, even on a cold load. This
		// replaces the old "zero-network race tier": that tier only served a
		// copy provably identical to the sidebar row (exact updated_at match,
		// empty subscribe ack, clean sidebar), so a stale copy always waited a
		// full network round-trip while perfectly renderable content sat in
		// storage. Correctness now comes from the revalidation continuation
		// below — the network fetch +page.ts already fired (304 / manifest
		// delta / full tail) reconciles the view the moment it lands, through
		// the SAME non-user-initiated reload path every other reconcile uses.
		// Writers that must not act on possibly-stale state (send, model
		// persistence) gate on `chatRevalidating` for the sub-second window.
		let _servedFromCache = false;
		const _preloaded = preloaded && preloaded.chatId === currentChatId ? preloaded : null;
		if (_preloaded) preloaded = null;

		// The tier requires the route-loader bundle: its chatPromise is what
		// revalidates the stale view. Without it (programmatic reloads, non-route
		// callers) a provisional serve would never reconcile — so don't serve.
		if (userInitiated && _preloaded && !currentChatId.startsWith('local:')) {
			const lru = chatOpenCache.get(currentChatId);
			let localData: any = null;
			let localTags: any = null;
			if (lru?.data) {
				localData = lru.data;
				localTags = lru.tags;
			} else if (!offlineRaceDirtyChatIds.has(currentChatId)) {
				// IDB read is local milliseconds; the route loader shares the same
				// read on soft navs via localEntryPromise.
				const entry = _preloaded?.localEntryPromise
					? await _preloaded.localEntryPromise.catch(() => null)
					: $user?.id
						? await getOfflineChat($user.id, currentChatId).catch(() => null)
						: null;
				if (entry?.data) {
					localData = entry.data;
					localTags = entry.tags;
				}
			}
			if (localData) {
				try {
					_chat = cloneState(localData);
					if (Array.isArray(localTags)) {
						Object.defineProperty(_chat, '__tailTags', {
							value: cloneState(localTags),
							enumerable: false,
							configurable: true
						});
					}
					_servedFromCache = true;
					// Deliberately NO chatViewUnverified latch here: an ordinary chat
					// open (sidebar click) revalidating its local copy is expected
					// behavior, not a staleness event. The sync mark is reserved for
					// connection stories — socket drop / reconnect catch-up — where
					// the user genuinely can't assume the view is current.
				} catch {
					_chat = undefined; // non-cloneable copy — fall through to network
					_servedFromCache = false;
				}
			}
		}

		if (_servedFromCache && _preloaded) {
			// Revalidation continuation: reconcile against the in-flight network
			// fetch. A true 304 substitutes the same IDB body and carries an
			// explicit non-enumerable marker; that is the ONLY proof the cached
			// body is unchanged. Every 200 is applied, even when updated_at and
			// currentId match, because message-row revisions can change within
			// the same integer second. A changed body is stashed on
			// `preloadedData` and loadChat re-runs non-user-initiated (no refetch).
			const _revalGeneration = generation;
			const _revalChatId = currentChatId;
			const _revalModelSelectionRevision = modelSelectionRevision;
			chatRevalidating = true;
			const _reval = (async () => {
				let freshChat: any = null;
				try {
					freshChat = await _preloaded.chatPromise;
				} catch {
					freshChat = null;
				}
				if (_revalGeneration !== navigateGeneration || get(chatId) !== _revalChatId) return;
				if (!freshChat) {
					// Network failure or 401/404 — the full reload path owns those
					// (offline banner / local-copy eviction / goto('/')).
					await loadChat(navigateGeneration, false, false, _revalModelSelectionRevision);
					return;
				}
				if ((freshChat as any)?.__notModified === true) {
					// Stale copy turned out to be current — the view is verified NOW
					// (the trailing task/stream reconcile must not hold the mark).
					chatViewUnverified = false;
					// Task/stream state still needs reconciling: a 304 carries a
					// proven-idle __active stamp, a fresh 200 carries the bundled
					// state; an old server carries neither and falls back to the
					// ack/HTTP path inside.
					void applyStreamTaskState(
						_revalGeneration,
						_revalChatId,
						null,
						null,
						((freshChat as any)?.__active as any) ?? null
					);
					return;
				}
				preloadedData = {
					chatId: _revalChatId,
					chat: freshChat,
					taskRes: null,
					modelSelectionRevisionAtRevalidationStart: _revalModelSelectionRevision
				};
				await loadChat(navigateGeneration, false);
			})();
			let _tracked: Promise<void>;
			_tracked = _reval.finally(() => {
				// Only clear if a newer navigation hasn't installed its own guard.
				if (chatRevalidationPromise === _tracked) {
					chatRevalidationPromise = null;
					chatRevalidating = false;
				}
			});
			chatRevalidationPromise = _tracked;
			// Swallow the background rejection (awaiters like submitPrompt attach
			// their own handlers) so a reval failure never logs as unhandled.
			void _tracked.catch(() => {});
		}

		if (_servedFromCache) {
			// painted from the local copy; revalidation runs in the background
		} else if (_preloaded) {
			_chat = await _preloaded.chatPromise.catch(() => null);
			if (!_chat) {
				// Route-loader fetch failed — the full path owns error classes
				// (offline fallback, 401/404 eviction, goto('/')).
				_chat = await loadPaginatedChat();
			}
		} else if (preloadedData && preloadedData.chatId === currentChatId && preloadedData.chat) {
			_chat = preloadedData.chat;
			_taskResIn = preloadedData.taskRes ?? null;
			_revalidationStartedAtModelRevision =
				preloadedData.modelSelectionRevisionAtRevalidationStart ?? null;
			preloadedData = null;
		} else {
			_chat = await loadPaginatedChat();
		}

		// Abort if a newer navigation started while we were fetching
		if (generation !== navigateGeneration) {
			return false;
		}

		chat = _chat;

		if (!chat) {
			return null;
		}

		// Populate the LRU with this fresh snapshot (item 2) so a later user-initiated
		// switch back can skip the history fetch entirely. Store a pre-mutation clone
		// keyed to the sidebar's current updated_at (the freshness token we compare on
		// serve). Skip temp/local chats and cache hits (already fresh in the map).
		if (
			!_servedFromCache &&
			!currentChatId.startsWith('local:') &&
			!(_chat as any)?.__offlineCopy
		) {
			const sidebarUpdatedAt = get(chats)?.find((c: any) => c?.id === currentChatId)?.updated_at;
			if (typeof sidebarUpdatedAt === 'number') {
				const _cacheTags = Array.isArray((_chat as any)?.__tailTags)
					? (_chat as any).__tailTags
					: null;
				try {
					chatOpenCachePut(
						currentChatId,
						sidebarUpdatedAt,
						cloneState(_chat),
						_cacheTags,
						chatOpenCacheSocketEpoch
					);
				} catch {
					// Non-cloneable snapshot (shouldn't happen for plain JSON chats) —
					// just skip caching rather than risk a shared-reference bug.
				}
			}
		}

		// Local-copy write-through — UNCONDITIONAL, for every user. This store is
		// a CACHE, not an offline feature: it is what makes the next open of this
		// chat instant (local-first paint + 304/manifest revalidation). The
		// `offlineChatStorage` setting still governs the bandwidth-consuming
		// offline AFFORDANCES (background prefetch sweeps, pinning UI, sidebar
		// dimming) — but a cache that only worked for users who found a toggle
		// meant everyone else re-downloaded every chat on every open. Correctness
		// never depends on freshness here (etag + per-row _rev revalidate), and
		// the per-user entry cap bounds growth. Persists every successfully
		// NETWORK-fetched chat (a local-copy serve is already the stored entry).
		if (
			!_servedFromCache &&
			!currentChatId.startsWith('local:') &&
			!(_chat as any)?.__offlineCopy
		) {
			const userId = $user?.id;
			const chatUpdatedAt = (_chat as any)?.updated_at;
			if (userId && typeof chatUpdatedAt === 'number') {
				const scheduleIdleWrite = (fn: () => void) => {
					if (typeof requestIdleCallback === 'function') {
						requestIdleCallback(() => fn());
					} else {
						setTimeout(fn, 200);
					}
				};
				// Tags + updatedAt captured NOW (fetch-time truth, cheap); the heavy
				// body clone happens once, inside the manager, at idle — off the
				// chat-open critical path.
				const _cacheTags = Array.isArray((_chat as any)?.__tailTags)
					? (_chat as any).__tailTags
					: [];
				scheduleIdleWrite(() => {
					void saveOfflineChatSnapshot({
						userId,
						chatId: currentChatId,
						chat: _chat,
						tags: _cacheTags,
						updatedAt: chatUpdatedAt
					});
				});
			}
		}

		// Tags: the single-request tail open (Contract 2) already carries them, so
		// use those and skip a round-trip. Old server / two-request fallback loads
		// them asynchronously — they're cosmetic and shouldn't block rendering.
		const _tailTags = (_chat as any)?.__tailTags;
		if (Array.isArray(_tailTags)) {
			tags = _tailTags;
		} else {
			getTagsById(localStorage.token, currentChatId)
				.then((_tags) => {
					tags = _tags;
				})
				.catch(() => {
					tags = [];
				});
		}

		const chatContent = chat.chat;

		if (!chatContent) {
			return null;
		}

		// Build history before model selection so corrupted/legacy chats with
		// `chat.models: []` can still infer a usable model from their messages.
		const loadedHistory =
			(chatContent?.history ?? undefined) !== undefined
				? chatContent.history
				: convertMessagesToHistory(chatContent.messages);

		const normalizeModelIds = (value: unknown): string[] => {
			const values = Array.isArray(value) ? value : value ? [value] : [];
			return values.filter((id): id is string => typeof id === 'string' && id.length > 0);
		};

		// Repair rows created by the broken new-chat stream-v2.1 path: the final
		// stream upsert could create an assistant row before the placeholder row
		// was appended, leaving it with role="" and no parentId. That made reloads
		// render a blank conversation even though content_blocks were saved.
		{
			const messages = loadedHistory?.messages as Record<string, any> | undefined;
			if (messages && typeof messages === 'object') {
				const ordered = Object.values(messages) as any[];
				for (const message of ordered) {
					if (!Array.isArray(message.childrenIds)) message.childrenIds = [];
					if (
						(!message.role || message.role === '') &&
						(message.model || message.selectedModelId || Array.isArray(message.content_blocks))
					) {
						message.role = 'assistant';
					}
				}
				for (const message of ordered) {
					if (message.role !== 'assistant' || message.parentId) continue;
					const messageIdx = ordered.indexOf(message);
					const messageTs = typeof message.timestamp === 'number' ? message.timestamp : null;
					const parent =
						ordered
							.slice(0, messageIdx >= 0 ? messageIdx : ordered.length)
							.reverse()
							.find(
								(m) =>
									m?.role === 'user' &&
									(messageTs === null ||
										typeof m.timestamp !== 'number' ||
										m.timestamp <= messageTs)
							) ??
						ordered
							.slice()
							.reverse()
							.find((m) => m?.role === 'user');
					if (parent?.id) {
						message.parentId = parent.id;
						parent.childrenIds = Array.isArray(parent.childrenIds) ? parent.childrenIds : [];
						if (!parent.childrenIds.includes(message.id)) parent.childrenIds.push(message.id);
					}
				}
			}
		}

		let loadedModels = normalizeModelIds(chatContent?.models);
		if (loadedModels.length === 0) {
			const historyMessages = Object.values(loadedHistory?.messages ?? {}) as any[];
			const currentMessage = loadedHistory?.currentId
				? (loadedHistory.messages as Record<string, any>)?.[loadedHistory.currentId]
				: null;
			const candidates = [currentMessage, ...historyMessages.slice().reverse()].filter(Boolean);
			for (const message of candidates) {
				loadedModels = normalizeModelIds(message?.models);
				if (loadedModels.length === 0) {
					loadedModels = normalizeModelIds(message?.selectedModelId ?? message?.model);
				}
				if (loadedModels.length > 0) break;
			}
		}
		let persistedModelIds = loadedModels.length > 0 ? loadedModels : [''];
		const canSelectMultiple =
			$user?.role === 'admin' || ($user?.permissions?.chat?.multiple_models ?? true);
		if (!canSelectMultiple) {
			persistedModelIds = persistedModelIds.length > 0 ? [persistedModelIds[0]] : [''];
		}

		const loadedModelSelection = resolveLoadedModelIds({
			persistedModelIds,
			currentModelIds: selectedModels,
			revalidationStartedAtRevision: _revalidationStartedAtModelRevision,
			currentRevision: modelSelectionRevision
		});
		selectedModels =
			loadedModelSelection.modelIds.length > 0 ? loadedModelSelection.modelIds : [''];
		if (!canSelectMultiple) {
			selectedModels = selectedModels.length > 0 ? [selectedModels[0]] : [''];
		}

		// Remember what the server actually carried, not a newer user selection
		// preserved across local-first revalidation. Once chatRevalidating clears,
		// the model writer sees that difference and persists the user's choice.
		rememberPersistedSelectedModels(currentChatId, persistedModelIds);
		oldSelectedModelIds = selectedModels;

		// Done-flag forcing moved to applyStreamTaskState (post-paint): it needs
		// the task-ids answer, and stored flags are already correct for finished
		// turns — only stranded (crashed mid-stream) rows briefly render as
		// "working" until the reconcile lands.

		// A stopped turn is by definition finished. Older rows can carry the flag
		// without `done`, which would render them as still working. No per-tab
		// bookkeeping is needed here any more: `isUserStoppedMessageId` reads this
		// durable flag directly, so a loaded row is self-describing.
		for (const message of Object.values(loadedHistory.messages ?? {}) as any[]) {
			if (message?.userStopped === true) message.done = true;
		}

		// Drop every stale stream mirror before committing the fresh history.
		// Mirrors were previously cleared only on component destroy, so an
		// in-app navigation back to a chat (or a reload-in-place) reused a
		// mirror whose version/run/blocks belonged to a dead view — its high
		// version silently swallowed live deltas and its blocks could splice
		// into the reloaded message. Active streams re-seed below via the
		// run-stamped /active list + replay/snapshot; the sessionStorage cache
		// is left intact (it is run-validated on hydrate).
		streamMirrors.clear();

		history = loadedHistory;
		// Full history rebuild on chat load — force the rendered chain to
		// recompute even if the id-count / currentId happen to coincide with a
		// prior chat (e.g. reloading the same chat).
		bumpMessageStructure();

		// A network-sourced body just COMMITTED — this is the exact moment new
		// content becomes visible, so the view is verified NOW. Releasing here
		// (instead of when the surrounding reconcile/revalidation returns) is
		// what keeps the sync mark honest: "Up to date" lands together with the
		// content, not seconds after it.
		if (!_servedFromCache) {
			chatViewUnverified = false;
		}

		// Active-stream flips + mirror run seeding moved to applyStreamTaskState
		// (post-paint) — they need the subscribe ack / active-streams answer.

		chatTitle.set(chatContent.title);

		params = chatContent?.params ?? {};
		chatFiles = chatContent?.files ?? [];
		// Hydrate the queued-message strip from the persisted chat blob. Guard
		// against malformed legacy data — older chats never had this field, so
		// `undefined` is normal and just means an empty queue.
		queue = reconcileServerQueue(
			Array.isArray(chatContent?.queue) ? (chatContent.queue as QueuedMessage[]) : []
		);
		_wasGenerating = false;

		// Seed ask_user question state (drafts + submitted answers) so an inline
		// question card restores partial selections / its locked answer across a
		// reload. Replace, don't merge — like subagentLiveStates, stale entries
		// from a previously-viewed chat must not leak in.
		questionStates.set(
			chatContent?.question_states && typeof chatContent.question_states === 'object'
				? chatContent.question_states
				: {}
		);

		// Restore every per-chat toolbar toggle from the saved params in one pass
		// (see chatParamBindings). Keys the user has just changed but whose PATCH
		// hasn't confirmed are deliberately skipped — a reload racing the user's
		// own toggle must never revert it. Feature flags are re-checked at submit
		// time, so restoring a `true` here is safe even if a global flag flipped.
		restoreChatParams(params);

		// A saved chat that already carries a non-empty tool/feature selection is
		// treated as user-curated: switching models in it must NOT wipe that
		// selection back to the model's defaults.
		toolSelectionDirty =
			(Array.isArray(selectedToolIds) && selectedToolIds.length > 0) ||
			webSearchEnabled ||
			imageGenerationEnabled ||
			studyModeEnabled ||
			dataVizEnabled ||
			automationsEnabled ||
			subagentsEnabled ||
			(Array.isArray(selectedFilterIds) && selectedFilterIds.length > 0);

		// Hydrate the subagent live-state store with anything persisted on
		// this chat's messages. This must be self-contained: after a full tab
		// reload there are no live socket events left, and the parent message's
		// content/content_blocks/subagent_runs can be from slightly different
		// write moments. Seed from every source and key every run by every stable
		// identifier (tool_call_id, subagent_id/chat_id, entry_key) so the
		// SubagentBlock can always find it regardless of which HTML attribute the
		// markdown projection preserved.
		const seeded = seedPersistedSubagentRuns(history.messages as Record<string, any>);
		try {
			const decodeHtmlAttr = (value: unknown) => {
				if (typeof value !== 'string') return value ?? '';
				if (typeof document === 'undefined') return value;
				const textarea = document.createElement('textarea');
				textarea.innerHTML = value;
				return textarea.value;
			};

			const parseToolArgs = (raw: unknown): Record<string, any> => {
				try {
					let value: any = decodeHtmlAttr(raw);
					if (typeof value === 'string') value = JSON.parse(value);
					if (typeof value === 'string') value = JSON.parse(value);
					return value && typeof value === 'object' ? value : {};
				} catch {
					return {};
				}
			};

			const seedRun = (run: any, parentMsg: any, explicitEntryKey?: string) => {
				if (!run || typeof run !== 'object') return;
				const entryKey = run.entry_key || explicitEntryKey || run.subagent_id || run.chat_id;
				const subagentId = run.subagent_id || run.chat_id || '';
				const normalized = {
					...run,
					entry_key: entryKey,
					subagent_id: subagentId,
					chat_id: run.chat_id || subagentId,
					// A rewound sibling branch carries copied subagent_runs from the
					// original message. The run's embedded parent_message_id may still
					// point at that older moved-on sibling, but the card the user clicked
					// belongs to parentMsg. Prefer the containing message so redo targets
					// the visible branch and can find its tool-call block.
					parent_message_id: parentMsg?.id || run.parent_message_id
				};
				const keys = [run.tool_call_id, subagentId, normalized.chat_id, entryKey].filter(Boolean);
				const existing = findSubagentRunEntry(seeded, normalized.parent_message_id || '', keys, {
					scan: false
				})?.[1];
				const merged = { ...(existing ?? {}), ...normalized };
				setSubagentRunAliases(seeded, merged, keys, normalized.parent_message_id || '');
			};

			// A parent message is still LIVE when THIS specific message is in the
			// active-stream set (subagents run inline in its generation), OR when an
			// in-flight subagent rerun (a "redo") targets one of ITS runs. A redo
			// registers under `subagent-rerun:{chat}:{entry}` — those entry keys are
			// surfaced by /api/tasks/chat as `subagent_rerun_entry_keys`, so we can
			// resolve exactly which parent message owns the live rerun and keep ONLY
			// that one alive (instead of the old chat-coarse `hasActiveTasksOnLoad`
			// fallback, which wrongly kept a crash orphan on message A "running" just
			// because a rerun ran against message B). Under a live parent a `running`
			// run with no `ended_at` is genuinely still going; otherwise it is a crash
			// orphan / lost terminal write — freeze it (no ended_at => no bogus
			// duration) instead of ticking.
			const loadActiveState = ((_chat as any)?.__active as any) ?? null;
			const activeStreamMessageIdSet = activeSubagentStreamMessageIds(loadActiveState);
			// Resolve which parent messages own a run that an active rerun targets:
			// scan every message's subagent_runs for an entry whose entry_key /
			// subagent_id / tool_call_id is in the active rerun set.
			// This comes from the consolidated chat-open response. The old code
			// referenced `_taskRes`, a function-local variable owned by the later
			// post-paint reconciler; that ReferenceError cleared the entire
			// subagent store on every reload.
			const reRunParentIds = new Set<string>();
			const rerunEntryKeys = activeSubagentRerunEntryKeys(loadActiveState);
			if (rerunEntryKeys.size > 0) {
				for (const msg of Object.values(history.messages ?? {})) {
					const m = msg as any;
					const runs = m?.subagent_runs;
					if (!runs || typeof runs !== 'object' || !m?.id) continue;
					for (const [entryKey, run] of Object.entries(runs)) {
						const r = run as any;
						if (subagentRunHasActiveRerunKey(rerunEntryKeys, entryKey, r)) {
							reRunParentIds.add(m.id);
							break;
						}
					}
				}
			}
			const isParentLive = (parentMsg: any) =>
				(parentMsg?.id && activeStreamMessageIdSet.has(parentMsg.id)) ||
				(parentMsg?.id && reRunParentIds.has(parentMsg.id));

			for (const msg of Object.values(history.messages ?? {})) {
				const m = msg as any;
				const runs = m?.subagent_runs;
				if (runs && typeof runs === 'object') {
					const parentLive = isParentLive(m);
					for (const [entryKey, run] of Object.entries(runs)) {
						const r = run as any;
						// Keep a running run live ONLY when its OWN key matches an active
						// rerun task (the redo task is keyed by the clicked entry; a
						// from_launch relaunch is keyed by its launch_key = subagent_id /
						// tool_call_id, all checked below). Do NOT keep every running run
						// alive just because SOME unrelated rerun is active in the chat —
						// that would let a genuine crash-orphan on a dead message tick
						// 'running' forever. An edge miss self-heals on the next poller
						// reload when the rerun finishes.
						const entryHasRerunTask = subagentRunHasActiveRerunKey(rerunEntryKeys, entryKey, r);
						const seedRunValue =
							!parentLive && !entryHasRerunTask && r?.status === 'running' && r?.ended_at == null
								? { ...r, status: 'cancelled' }
								: r;
						seedRun(seedRunValue, m, entryKey);
					}
				}

				for (const block of Array.isArray(m?.content_blocks) ? m.content_blocks : []) {
					if (block?.type !== 'tool_calls') continue;
					const calls = Array.isArray(block?.content) ? block.content : [];
					const results = Array.isArray(block?.results) ? block.results : [];
					for (const call of calls) {
						const callId = call?.id || '';
						const toolName = call?.function?.name || '';
						if (toolName !== 'subagent_launch' && toolName !== 'subagent_continue') continue;
						const args = parseToolArgs(call?.function?.arguments ?? '');
						const result = results.find((r: any) => r?.tool_call_id === callId);
						const subagentId = result?.subagent_id || '';
						// A result row only proves the subagent finished with a real
						// answer when its content is non-empty. The launch placeholder
						// for a cancelled / no-final-text run persists an EMPTY result
						// that must NOT be read as success — otherwise the header shows
						// a green "done" for a subagent that never produced anything.
						const resultContent = typeof result?.content === 'string' ? result.content : '';
						const resultHasAnswer = resultContent.trim().length > 0;
						// An error result is a non-empty STRING too (the subagent error
						// sentinel / a structured error flag) — it must NOT be read as a
						// real answer and forged into 'done'/final_text.
						const resultErrored =
							result?.error === true ||
							/^Subagent\s+\d+\s+\(.*\)\s+ERROR\b/.test(resultContent.trim());
						const existing =
							findSubagentRunEntry(
								seeded,
								m?.id || '',
								[callId, ...(toolName === 'subagent_launch' ? [subagentId] : [])],
								{ scan: false }
							)?.[1] ?? {};
						const inferredEntryKey =
							existing.entry_key ||
							(toolName === 'subagent_continue' && subagentId && callId
								? `${subagentId}#${callId}`
								: subagentId || callId);
						seedRun(
							{
								...existing,
								subagent_id: existing.subagent_id || subagentId,
								chat_id: existing.chat_id || subagentId,
								tool_call_id: existing.tool_call_id || callId,
								name: existing.name || args?.name || args?.name_or_id || '',
								prompt: existing.prompt || args?.prompt || '',
								background: existing.background || args?.background || '',
								continuation: existing.continuation || toolName === 'subagent_continue',
								status: resultHasAnswer
									? existing.status === 'error' || resultErrored
										? 'error'
										: 'done'
									: // No real answer in the result. Trust the run entry's
										// own terminal status; absent that, only seed 'running'
										// under a LIVE parent — a lost-write entry on a dead/
										// finished parent must freeze ('cancelled'), not tick.
										existing.status || (isParentLive(m) ? 'running' : 'cancelled'),
								final_text:
									existing.final_text ||
									(resultHasAnswer && !resultErrored ? resultContent : undefined)
							},
							m,
							inferredEntryKey
						);
					}
				}
			}

			// Replace, don't merge. Merging keeps stale subagent rows from a prior
			// chat/navigation alive and makes reload behavior depend on browsing
			// history instead of the current chat's persisted state.
			subagentLiveStates.set(seeded);
		} catch (e) {
			console.warn('Failed to hydrate subagentLiveStates:', e);
			// The persisted baseline is intentionally built outside this richer
			// enrichment block. A future parser/task-hydration regression may lose
			// live decoration, but it cannot make durable done/error cards vanish.
			subagentLiveStates.set(seeded);
		}

		// Re-arm follow-to-bottom only on a genuine navigation/open. On a same-chat
		// reconcile reload, preserve whatever the user chose — a scrolled-up reader
		// stays put; one already tailing keeps tailing. (Re-engage otherwise stays
		// owned by submit/regen via engageAndScrollToBottom + onScroll near-bottom.)
		if (!isSameChatReload) autoScroll = true;

		// Browser panel frames: replace, don't accumulate. The seed below merges
		// per-key, so without this reset every visited chat's last base64 frame
		// (~50-300KB each) stayed resident for the whole session. Same-chat
		// reconciles keep the live frames (socket frames may be fresher than the
		// host snapshot fetched below).
		if (!isSameChatReload) browserLiveStates.set({});

		// Stream/task state (done-flag forcing, active-stream flips, mirror
		// seeding, snapshot replay, generating/polling flip) reconciles AFTER
		// first paint — see applyStreamTaskState. The open response bundles the
		// state (`__active`), so this normally costs ZERO extra round-trips; a
		// provisional (local-copy) serve defers to its revalidation continuation
		// instead, whose 304/fresh-body outcome carries the state. Stop any
		// previous chat's resume-polling synchronously so its callbacks can't
		// fire against this view in the gap.
		stopResumeTaskPolling();
		if (!_servedFromCache) {
			void applyStreamTaskState(
				generation,
				currentChatId,
				_taskResIn,
				_subscribePromise,
				((_chat as any)?.__active as any) ?? null
			);
		}

		await tick();

		return true;
	};

	// Re-engage stick-to-bottom once the user brings themselves back within this
	// many px of the bottom. Disengaging is gesture-driven (wheel / touch), so this
	// threshold governs only re-engagement. The asymmetry — trivial to leave,
	// deliberate to rejoin — is what makes a fast stream feel right instead of
	// "impossible to escape".
	const AUTO_SCROLL_REENGAGE_PX = 64;
	// After the user pulls away (or a programmatic nudge), briefly suppress
	// position-based re-engagement so trackpad / touch momentum settling can't snap
	// the view back to the bottom against their intent.
	const REENGAGE_COOLDOWN_MS = 250;
	const WHEEL_UP_DEADZONE = 0.5; // filter sub-pixel jitter, still catch a line/page tick up
	const TOUCH_UP_DEADZONE = 6; // px of finger travel before it counts as a drag-up
	// Show the floating jump-to-bottom pill only once the reader is meaningfully
	// away from the bottom — well past the re-engage band so the two affordances
	// never overlap (near the bottom, scrolling down re-engages by itself).
	const JUMP_TO_BOTTOM_SHOW_PX = 320;

	let scrollToBottomFrame: number | null = null;
	let scrollStateFrame: number | null = null;
	let observedMessagesContentElement: HTMLDivElement | null = null;
	let observedMessagesContainerElement: HTMLDivElement | null = null;
	let observedMessagesListElement: HTMLElement | null = null;
	// Bound bottom chrome: the composer (pb-composer div). The RO uses the
	// composer's height to tell composer-driven viewport changes (typing /
	// clearing a draft) apart from other chrome (token panel, keyboard). The
	// compensation spacer preserves a scrolled-up reader's position through the
	// former; bottom-following keeps the tail pinned through either. The spacer
	// lives at the bottom of the scroll content in Messages.svelte
	// (#composer-compensation-spacer) — resolved per tick, it remounts.
	let composerElement: HTMLDivElement | null = null;
	// Temporary room used only while a scrolled-up reader owns the viewport and
	// the composer shrinks underneath it. This state must outlive an individual
	// ResizeObserver callback so every path that resumes bottom-following can
	// retire the room before calculating the real bottom. Leaving the spacer in
	// place makes it part of scrollHeight and strands the final action row high
	// above the composer until a reload remounts it at zero height.
	let composerCompensation = 0;

	const setComposerCompensation = (px: number) => {
		composerCompensation = Math.max(0, px);
		const spacer = document.getElementById('composer-compensation-spacer');
		if (spacer) spacer.style.height = `${composerCompensation}px`;
	};

	const clearComposerCompensation = () => {
		if (composerCompensation === 0) return;
		setComposerCompensation(0);
	};

	// Replaces the 150px content-visibility placeholder guess with each turn's
	// measured height during idle moments, so scrolling up realizes to
	// identical heights (zero layout shift). See messageHeights.ts.
	const messageHeightSweeper = createMessageHeightSweeper();
	let messagesResizeObserver: ResizeObserver | null = null;
	let reengageCooldownUntil = 0;
	let settleInterrupted = false;
	let touchStartY = 0;
	let showJumpToBottom = $state(false);

	const getBottomDistance = (element: HTMLElement) =>
		Math.max(0, element.scrollHeight - element.scrollTop - element.clientHeight);

	const getBottomScrollTop = (element: HTMLElement) =>
		Math.max(0, element.scrollHeight - element.clientHeight);

	const isNearBottom = (element: HTMLElement) =>
		getBottomDistance(element) <= AUTO_SCROLL_REENGAGE_PX;

	// ---- Glide controller (the single ANIMATED-scroll owner) ----------------
	// Native behavior:'smooth' is never used on #messages-container. A native
	// animation aims at a target computed once and is cancelled by any other
	// scrollTop write — so the jump-to-bottom pill's smooth glide visibly
	// teleported the moment a ResizeObserver tick ran the instant pin (content
	// realizes/streams DURING the glide), and branch-nav scrollIntoView died to
	// engine corrections the same way. The glide instead RE-AIMS every frame
	// (the target function is re-evaluated, so realization below, streaming
	// growth, keyboard resizes all just bend the path), always lands exactly,
	// and has clear ownership: while a glide is active the instant pin and the
	// anchoring engine stand down, and ANY user gesture (touch down, wheel)
	// cancels it — matching the native "touch stops the fling" convention.
	let glideFrame: number | null = null;
	let glideTargetFn: (() => number | null) | null = null;

	const glideActive = () => glideFrame !== null;

	const cancelGlide = () => {
		if (glideFrame !== null) cancelAnimationFrame(glideFrame);
		glideFrame = null;
		glideTargetFn = null;
	};

	const startGlide = (getTarget: () => number | null) => {
		cancelGlide();
		glideTargetFn = getTarget;
		const step = () => {
			glideFrame = null;
			const container = messagesContainerElement;
			const get = glideTargetFn;
			if (!container || !get) return;
			const target = get();
			if (target === null) {
				cancelGlide();
				return;
			}
			const remaining = target - container.scrollTop;
			if (Math.abs(remaining) <= 1) {
				container.scrollTop = target;
				cancelGlide(); // arrived — instant-pin / engine ownership resumes
				return;
			}
			// Fast start, gentle landing: cover ~22% of the remaining distance
			// per frame (95% closed in ~10 frames) with a floor so the tail
			// never crawls. Long distances resolve in ~a dozen frames, which is
			// what a "jump to bottom" should feel like.
			const stepPx = Math.sign(remaining) * Math.max(24, Math.abs(remaining) * 0.22);
			container.scrollTop += Math.abs(stepPx) >= Math.abs(remaining) ? remaining : stepPx;
			glideFrame = requestAnimationFrame(step);
		};
		glideFrame = requestAnimationFrame(step);
	};

	const glideToBottom = () => {
		clearComposerCompensation();
		startGlide(() => {
			// Follow intent revoked (gesture) → stop mid-flight.
			if (!autoScroll || !messagesContainerElement) return null;
			return getBottomScrollTop(messagesContainerElement);
		});
	};

	const glideToMessage = (messageId: string) =>
		startGlide(() => {
			const container = messagesContainerElement;
			const el = document.getElementById(`message-${messageId}`);
			if (!container || !el || !el.isConnected) return null;
			// Message top a touch below the container top (scrollIntoView
			// block:'start' with breathing room), clamped to the scroll range.
			const top =
				el.getBoundingClientRect().top -
				container.getBoundingClientRect().top +
				container.scrollTop;
			return Math.max(0, Math.min(top - 12, getBottomScrollTop(container)));
		});

	// Low-level pin. Deliberately does NOT touch `autoScroll`: follow intent is
	// owned by the gesture handlers and the re-engage logic, never re-asserted as a
	// side effect of a programmatic scroll. (The old "impossible to escape" bug was
	// exactly this — every pin force-wrote autoScroll = true, so the next token
	// re-armed following the instant after the user pulled away.)
	const scrollToBottomNow = () => {
		if (!messagesContainerElement) return;
		if (glideActive()) return; // the glide is the pin while it runs, and it re-aims
		// Compensation preserves a scrolled-up reader's position across composer
		// shrinkage. Once following owns the viewport again it is stale by
		// definition; exclude it from the bottom target before measuring.
		clearComposerCompensation();
		const target = getBottomScrollTop(messagesContainerElement);
		if (Math.abs(messagesContainerElement.scrollTop - target) <= 1) return;

		{
			const dbg = (window as any).__engineDebug;
			if (dbg)
				dbg.push({
					t: performance.now(),
					ev: 'pin',
					from: messagesContainerElement.scrollTop,
					to: target
				});
		}
		messagesContainerElement.scrollTo({ top: target, behavior: 'auto' });
	};

	const scrollToBottom = (behavior: ScrollBehavior = 'auto') => {
		// One-shot smooth requests become a retargeting glide; the streaming hot
		// path stays instant (smooth animations rubber-band under token bursts).
		if (behavior === 'smooth') {
			glideToBottom();
			return;
		}

		if (scrollToBottomFrame !== null) return;
		scrollToBottomFrame = requestAnimationFrame(async () => {
			scrollToBottomFrame = null;
			await tick();
			// The user may have grabbed the scroll between this frame being queued
			// and it running — never yank them back after they've taken over.
			if (!autoScroll) return;
			scrollToBottomNow();
		});
	};

	// The ONLY programmatic path that turns following back on: explicit user
	// actions (submit, regenerate, jump-to-bottom). Near-bottom re-engagement in
	// onScroll is the other (user-driven) path.
	const engageAndScrollToBottom = (behavior: ScrollBehavior = 'auto') => {
		autoScroll = true;
		reengageCooldownUntil = 0;
		clearExpansionHold(); // an explicit "take me to the bottom" outranks a hold
		scrollToBottom(behavior);
	};

	// On-screen keyboard opened/closed (keyboardViewport.ts): the messages
	// container height changes with no content change, so the content
	// ResizeObserver never fires. If the user is following the bottom, keep them
	// pinned through the keyboard's show/hide animation (the height keeps
	// changing for several frames after the transition event). Gated on
	// autoScroll every frame so a gesture disengage mid-animation wins.
	const onKeyboardViewport = (event: Event) => {
		const open = Boolean((event as CustomEvent)?.detail?.open);
		keyboardShown = open;
		// Compensation must NOT kick in for keyboard-CLOSE growth: on mobile the
		// deliberate behavior is re-gluing the tail to the freed screen bottom.
		if (!open) keyboardClosedAt = performance.now();
		const start = performance.now();
		const step = () => {
			if (!autoScroll || !messagesContainerElement) return;
			scrollToBottomNow();
			if (performance.now() - start < 450) requestAnimationFrame(step);
		};
		requestAnimationFrame(step);
	};

	// Halt any in-flight CSS smooth-scroll animation so it can't keep dragging the
	// viewport toward the bottom after the user has started pulling away.
	const haltSmoothScroll = () => {
		if (!messagesContainerElement) return;
		messagesContainerElement.scrollTo({
			top: messagesContainerElement.scrollTop,
			behavior: 'auto'
		});
	};

	// Single source of truth for "user pulled away from the bottom". Runs
	// synchronously from the wheel / touch handlers so that by the time any
	// ResizeObserver tick or queued pin fires, it already reads autoScroll === false
	// and stands down. Refreshes the cooldown on every call so a continuous upward
	// gesture is never re-engaged mid-flight.
	// NOTE: this does NOT clear an expansion hold. Scroll GESTURES clear it
	// (they call clearExpansionHold themselves), but disengaging as a
	// consequence of an expansion — a subagent card announcing that the user
	// opened it — must leave the hold that is keeping that very card still.
	const disengageAutoScroll = () => {
		cancelGlide(); // a gesture cancels ANY animated glide, follow-state aside
		reengageCooldownUntil = performance.now() + REENGAGE_COOLDOWN_MS;
		if (!autoScroll) return; // already free; just refreshed the cooldown
		autoScroll = false;
		if (scrollToBottomFrame !== null) {
			cancelAnimationFrame(scrollToBottomFrame);
			scrollToBottomFrame = null;
		}
		settleInterrupted = true; // abort an initial-load settle loop if one is running
		haltSmoothScroll();
		// Ownership of the viewport just passed to the reader — arm the
		// scroll-anchoring engine immediately. Waiting for the next scroll
		// event left a null-anchor window where shifts (sweeper measurements,
		// late images) painted uncorrected, with native overflow-anchor also
		// off. No-op while messagesReady is false (the reveal step arms then).
		captureScrollCorrectionAnchor();
	};

	const onWheel = (event: WheelEvent) => {
		if (event.ctrlKey) return; // pinch-zoom gesture, not a scroll
		cancelGlide(); // any real wheel input takes the animation's ownership
		clearExpansionHold();
		if (event.deltaY < -WHEEL_UP_DEADZONE) disengageAutoScroll();
	};

	// ---- Non-wheel scroll input ---------------------------------------------
	// Disengage is deliberately gesture-driven (see disengageAutoScroll), but
	// "gesture" was only ever wheel + touch. A reader who drags the SCROLLBAR,
	// drag-selects text upward, or presses PageUp kept `autoScroll === true`,
	// so the next ResizeObserver tick (a token, a late image, a realizing
	// placeholder) teleported them back to the bottom mid-read. These two
	// handlers close that hole without reintroducing position-based disengage:
	// both are positive evidence of user input, never inferred from position.
	let pointerScrollActive = false;

	const onPointerDown = (event: PointerEvent) => {
		// Touch has its own (drag-direction aware) handling in onTouchMove.
		if (event.pointerType === 'touch') return;
		pointerScrollActive = true;
	};

	const endPointerScroll = () => {
		pointerScrollActive = false;
	};

	const SCROLL_UP_KEYS = new Set(['PageUp', 'ArrowUp', 'Home']);

	const onContainerKeyDown = (event: KeyboardEvent) => {
		if (event.defaultPrevented || event.ctrlKey || event.metaKey || event.altKey) return;
		const target = event.target as HTMLElement | null;
		const tag = target?.tagName;
		// Typing in an edit box / textarea is not scrolling.
		if (target?.isContentEditable || tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') {
			return;
		}
		if (SCROLL_UP_KEYS.has(event.key) || (event.key === ' ' && event.shiftKey)) {
			clearExpansionHold();
			disengageAutoScroll();
		}
	};

	// ---- Expansion hold ("a click never moves what you clicked") ------------
	// Expanding a tool call / reasoning block / subagent card grows the page
	// under a bottom-pin that then drags the header the user just clicked off
	// the top of the screen — the classic "it jumped away as I opened it".
	// Any element tagged `data-anchor-on-click` inside the message list records
	// its viewport position at click-CAPTURE time (before its own handler runs
	// and mutates the DOM); every resize for the next HOLD_MS puts it back
	// exactly where it was, pre-paint. Ownership is explicit: while a hold is
	// live it outranks both the bottom pin and the anchoring engine, and any
	// scroll gesture cancels it.
	const EXPANSION_HOLD_MS = 700;
	// Absolute ceiling for a hold extended by an in-flight lazy body (below). A
	// request that hangs must eventually hand the viewport back rather than
	// freeze it against a layout the user has moved on from.
	const EXPANSION_HOLD_MAX_MS = 5000;
	let expansionHold: {
		el: HTMLElement;
		top: number;
		until: number;
		expiresAt: number;
	} | null = null;

	const clearExpansionHold = () => {
		expansionHold = null;
	};

	const armExpansionHold = (event: Event) => {
		const container = messagesContainerElement;
		if (!container) return;
		const target = event.target as Element | null;
		const anchor = target?.closest?.('[data-anchor-on-click]') as HTMLElement | null;
		if (!anchor || !container.contains(anchor)) return;
		expansionHold = {
			el: anchor,
			// Measured against the CONTAINER, not the viewport: the scroll box
			// itself moves (keyboard, composer auto-grow) and that must not read
			// as content shifting under the click.
			top: anchor.getBoundingClientRect().top - container.getBoundingClientRect().top,
			until: performance.now() + EXPANSION_HOLD_MS,
			expiresAt: performance.now() + EXPANSION_HOLD_MAX_MS
		};
	};

	/** Returns true when the hold owns the viewport for this resize. */
	const applyExpansionHold = (): boolean => {
		const hold = expansionHold;
		const container = messagesContainerElement;
		if (!hold || !container) return false;
		if (!hold.el.isConnected || glideActive()) {
			// Gone, or an animated scroll (an explicit destination) took over.
			expansionHold = null;
			return false;
		}
		const delta =
			hold.el.getBoundingClientRect().top - container.getBoundingClientRect().top - hold.top;
		if (Math.abs(delta) > 0.5) {
			container.scrollTop += delta;
		}
		// Holding the clicked row still may have carried us away from the bottom
		// (expanding a long tool result while following a stream). Following is
		// then over — the user asked to read this, not to be dragged onward.
		if (autoScroll && getBottomDistance(container) > AUTO_SCROLL_REENGAGE_PX) {
			autoScroll = false;
			reengageCooldownUntil = performance.now() + REENGAGE_COOLDOWN_MS;
			// No anchor while the hold owns the viewport — capturing mid-hold would
			// baseline against a layout the hold is still moving. The handover
			// below arms the engine once, on the delivery that ends the hold.
			scrollCorrectionAnchor = null;
		}
		// Expiry is evaluated AFTER correcting: the delivery that runs out the
		// clock still gets held (a lazily fetched tool body can land right on the
		// boundary), and the anchoring engine is armed against the settled layout
		// so the NEXT shift is corrected rather than absorbed as a new baseline.
		//
		// A body still in flight KEEPS the hold past its window, up to a hard
		// ceiling. 700ms was a bet on how long a fetch takes; on a slow connection
		// it lost, the hold lapsed, and the body then landed and shoved the reader
		// — the precise thing the hold exists to prevent. `expiresAt` bounds it so
		// a hung request cannot own the viewport indefinitely.
		const now = performance.now();
		const heldForLazyBody = pendingLazyBodyCount() > 0 && now <= hold.expiresAt && now > hold.until;
		if (now > hold.until && !heldForLazyBody) {
			expansionHold = null;
			if (!autoScroll) captureScrollCorrectionAnchor();
		}
		return true;
	};

	const onTouchStart = (event: TouchEvent) => {
		// Finger down stops an in-flight glide — the native "touch arrests the
		// fling" convention. (Follow intent is only revoked by an actual
		// upward drag, in onTouchMove.)
		cancelGlide();
		touchDragging = true;
		if (event.touches?.length === 1) touchStartY = event.touches[0].clientY;
	};

	const onTouchMove = (event: TouchEvent) => {
		if (event.touches?.length !== 1) return;
		// Finger dragging down (clientY increasing) reveals earlier content — the
		// user is scrolling up and wants out of the stream.
		if (Math.abs(event.touches[0].clientY - touchStartY) > TOUCH_UP_DEADZONE) {
			clearExpansionHold(); // a real drag, not a tap that happens to wobble
		}
		if (event.touches[0].clientY - touchStartY > TOUCH_UP_DEADZONE) disengageAutoScroll();
	};

	const onTouchEnd = () => {
		touchDragging = false;
	};

	let touchDragging = false;

	// Tracks the last position we saw so a pointer-driven UPWARD move (scrollbar
	// drag, drag-select autoscroll) can be told apart from the pin's own
	// downward writes. Content growth never changes scrollTop — only input and
	// our own writes do — so this stays free of the false positives that made
	// position-based disengage unworkable.
	// lastObservedScrollTop is captured SYNCHRONOUSLY on every scroll event:
	// the RO's clamp-restore reads it as the pre-clamp position, and a browser
	// clamp's own scroll event only fires after the RO callback.
	let lastObservedScrollTop = 0;
	let lastHandledScrollTop = 0;

	const onScroll = () => {
		if (messagesContainerElement) {
			lastObservedScrollTop = messagesContainerElement.scrollTop;
		}
		// STRAY-SCROLL CORRECTION: while following the bottom, every user
		// gesture that leaves it disengages SYNCHRONOUSLY (wheel-up, touch-up,
		// scrollbar-up drag via onPointerDown, scroll-up keys) — before the
		// scroll event. So a scroll event arriving with autoScroll still true
		// (and no drag in progress) means the view drifted WITHOUT user intent:
		// the browser's own caret/selection reveal on editing (the "snap" when
		// clearing the input — Chrome scrolls the messages container natively,
		// no JS channel), a stray clamp, or an anchor artifact. Re-pin
		// immediately: scroll events are dispatched before the rAF step in the
		// same frame, so the correction lands pre-paint and is invisible.
		if (autoScroll && !pointerScrollActive && !touchDragging && !glideActive()) {
			const c = messagesContainerElement;
			if (c && getBottomDistance(c) > 1) {
				scrollToBottomNow();
			}
		}
		if (scrollStateFrame !== null) return;
		scrollStateFrame = requestAnimationFrame(() => {
			scrollStateFrame = null;
			if (!messagesContainerElement) return;
			const scrollTop = messagesContainerElement.scrollTop;
			const movedUp = scrollTop < lastHandledScrollTop - 2;
			lastHandledScrollTop = scrollTop;
			if (autoScroll && movedUp && pointerScrollActive && !glideActive()) {
				clearExpansionHold();
				disengageAutoScroll();
			}
			if (!autoScroll) {
				// Disengaging is gesture-driven; here we only re-arm following once
				// the user has brought themselves back to the bottom.
				if (performance.now() >= reengageCooldownUntil && isNearBottom(messagesContainerElement)) {
					autoScroll = true;
				}
			}
			showJumpToBottom =
				!autoScroll && getBottomDistance(messagesContainerElement) > JUMP_TO_BOTTOM_SHOW_PX;
			maintainScrollCorrectionAnchor();
		});
	};

	// ---- Scroll-anchoring engine (manual overflow-anchor) -------------------
	// While a reader is scrolled up, content ABOVE their viewport keeps changing
	// height with no user input: content-visibility placeholders realize their
	// true heights, images/KaTeX/code-highlight land late, pagination prepends
	// older turns. iOS Safari has NO scroll anchoring at all, so every one of
	// those reads as the view teleporting mid-read. This engine is the missing
	// anchoring, and it is the SOLE owner (the container sets
	// overflow-anchor:none so Chrome's native anchoring can't double-correct):
	// an anchor message near the viewport top is tracked by its offset within
	// the scroll CONTENT — a metric invariant both to user scrolling (so a
	// correction can never undo a frame of an active fling) and to every other
	// corrector's scrollTop writes (edit anchors, delete/sibling restores) —
	// and when the content resizes, the ResizeObserver below (which fires after
	// layout but BEFORE paint) corrects the residual so the shift is never
	// painted. The anchor's baseline is deliberately NOT refreshed per scroll
	// frame — only re-picked when it leaves the viewport vicinity — because a
	// same-frame refresh would absorb a not-yet-corrected shift. Only active
	// while the reader owns the position (!autoScroll); the bottom-pin owns it
	// otherwise. Height changes at/below the anchor's top edge don't move it in
	// content coordinates, which is exactly why the surviving at-mutation
	// restores (delete, sibling swap, edit entry — all anchored to a visible
	// message at/below this engine's anchor) compose with it cleanly.
	//
	// The anchor is REFINED below the message row: a row can be far taller
	// than the viewport (an assistant turn with subagent cards runs to many
	// screens), so a row-top anchor can't see growth that happens INSIDE the
	// row but above the viewport top — a subagent card's async content landing,
	// KaTeX/images/code-highlight realizing late. That growth leaves the row's
	// top edge unmoved (zero delta, no correction) while visibly teleporting
	// the reader. We therefore descend from the row to the deepest in-flow
	// element straddling the viewport-top line; anything above the reader then
	// moves the anchor. The row is kept alongside as the stable fallback for
	// when a markdown re-render remounts the refined node.
	let scrollCorrectionAnchor: {
		wrap: Element;
		el: Element;
		wrapTop: number;
		elTop: number;
	} | null = null;
	// One-shot: absorb the next observed shift as the new baseline instead of
	// correcting it (set on edit entry — see the messageEditingIds reactive).
	let rebaselineOnNextScrollCorrection = $state(false);

	const anchorContentTop = (el: Element, container: HTMLElement): number =>
		el.getBoundingClientRect().top - container.getBoundingClientRect().top + container.scrollTop;

	// Descend from the message row toward the viewport-top line: the deepest
	// in-flow child whose box straddles the line (or the first child below it,
	// when the row starts inside the viewport). Growth inside a tall message
	// but ABOVE the line moves this inner anchor even though the row's top edge
	// didn't budge — the exact shift a row-level anchor can't see. Overlay
	// overlay chrome such as sticky headers is skipped: it doesn't
	// track content flow, so anchoring to it would measure chrome, not content.
	const refineScrollAnchor = (wrap: Element, container: HTMLElement): Element => {
		const lineY = container.getBoundingClientRect().top + 1;
		let current = wrap;
		// The depth budget has to clear the deepest content nesting, not a typical
		// one: row → message → prose → block group → collapsible → markdown →
		// token wrapper is already ~10 levels before you reach the element that
		// actually holds a late-loading image. Stopping short leaves the anchor
		// above the growth it is supposed to measure, which reads as an
		// uncorrected jump. Each level is one cheap children scan.
		for (let depth = 0; depth < 24; depth++) {
			let next: Element | null = null;
			const kids = current.children;
			for (let i = 0; i < kids.length; i++) {
				const r = kids[i].getBoundingClientRect();
				if (r.height <= 0) continue; // display:contents / hidden nodes have no box
				if (r.bottom > lineY) {
					next = kids[i];
					break;
				}
			}
			if (!next) break;
			const position = getComputedStyle(next).position;
			if (position === 'absolute' || position === 'fixed' || position === 'sticky') break;
			current = next;
		}
		return current;
	};

	const captureScrollCorrectionAnchor = () => {
		scrollCorrectionAnchor = null;
		const container = messagesContainerElement;
		if (!container || autoScroll || !messagesReady) return;
		const containerTop = container.getBoundingClientRect().top;
		const wraps = container.querySelectorAll('[data-cv-wrap]');
		if (wraps.length === 0) return;
		// Binary search the first message whose bottom clears the viewport top
		// (document order == visual order).
		let lo = 0;
		let hi = wraps.length - 1;
		let found = -1;
		while (lo <= hi) {
			const mid = (lo + hi) >> 1;
			if (wraps[mid].getBoundingClientRect().bottom > containerTop + 1) {
				found = mid;
				hi = mid - 1;
			} else {
				lo = mid + 1;
			}
		}
		if (found >= 0) {
			const wrap = wraps[found];
			const el = refineScrollAnchor(wrap, container);
			scrollCorrectionAnchor = {
				wrap,
				el,
				wrapTop: anchorContentTop(wrap, container),
				elTop: anchorContentTop(el, container)
			};
			const dbg = (window as any).__engineDebug;
			if (dbg)
				dbg.push({
					t: performance.now(),
					ev: 'capture',
					anchorId: (wrap.closest('[id^="message-"]') as HTMLElement | null)?.id,
					contentTop: scrollCorrectionAnchor.elTop,
					scrollTop: container.scrollTop
				});
		}
	};

	// Called per scroll frame: drop the anchor when following resumes, re-pick
	// when it drifted out of the viewport vicinity, otherwise LEAVE THE
	// BASELINE ALONE (see engine comment).
	const maintainScrollCorrectionAnchor = () => {
		const container = messagesContainerElement;
		{
			const dbg = (window as any).__engineDebug;
			if (dbg)
				dbg.push({
					t: performance.now(),
					ev: 'maintain',
					autoScroll,
					messagesReady,
					hasContainer: Boolean(container),
					scrollTop: container?.scrollTop,
					maxScrollTop: container ? container.scrollHeight - container.clientHeight : 0
				});
		}
		if (!container || autoScroll || !messagesReady) {
			scrollCorrectionAnchor = null;
			return;
		}
		const a = scrollCorrectionAnchor;
		// Probe with the refined node when it survives, else the row (a markdown
		// re-render can remount the inner node while the keyed row persists).
		const probe = a ? (a.el.isConnected ? a.el : a.wrap.isConnected ? a.wrap : null) : null;
		if (probe) {
			const containerTop = container.getBoundingClientRect().top;
			const r = probe.getBoundingClientRect();
			if (r.bottom > containerTop - 200 && r.top < containerTop + container.clientHeight) {
				return;
			}
		}
		captureScrollCorrectionAnchor();
	};

	const applyScrollCorrection = () => {
		const container = messagesContainerElement;
		const a = scrollCorrectionAnchor;
		const dbg = (window as any).__engineDebug;
		// A glide owns the viewport and re-aims through layout shifts itself.
		if (glideActive()) return;
		if (!container || autoScroll || !messagesReady) {
			if (dbg)
				dbg.push({
					t: performance.now(),
					ev: 'skip',
					hasAnchor: Boolean(a),
					autoScroll,
					messagesReady
				});
			return;
		}
		if (!a) {
			// Self-arm: a resize arrived while the reader owns the position but
			// no anchor exists (transient empty list, anchor lost mid-switch).
			// This delivery's shift is absorbed, every later one is corrected —
			// realization/prepend tails span many deliveries.
			captureScrollCorrectionAnchor();
			if (dbg) dbg.push({ t: performance.now(), ev: 'self-arm' });
			return;
		}
		if (!a.wrap.isConnected) {
			if (dbg) dbg.push({ t: performance.now(), ev: 'anchor-lost' });
			captureScrollCorrectionAnchor();
			return;
		}
		if (rebaselineOnNextScrollCorrection) {
			rebaselineOnNextScrollCorrection = false;
			a.wrapTop = anchorContentTop(a.wrap, container);
			if (!a.el.isConnected) a.el = refineScrollAnchor(a.wrap, container);
			a.elTop = anchorContentTop(a.el, container);
			if (dbg) dbg.push({ t: performance.now(), ev: 'rebaseline' });
			return;
		}
		// Prefer the refined node: it sees growth INSIDE a tall row above the
		// viewport. If a re-render remounted it, the row still bounds the
		// inter-row part of the shift — correct that much (no worse than a
		// row-only anchor), then re-refine below.
		const delta = a.el.isConnected
			? anchorContentTop(a.el, container) - a.elTop
			: anchorContentTop(a.wrap, container) - a.wrapTop;
		if (dbg)
			dbg.push({
				t: performance.now(),
				ev: 'apply',
				delta,
				anchorId: (a.wrap.closest('[id^="message-"]') as HTMLElement | null)?.id,
				scrollTop: container.scrollTop
			});
		if (Math.abs(delta) > 0.5) {
			container.scrollTop += delta;
			// The layout shift is now compensated; the anchor's new content
			// offset is the go-forward baseline. (Sub-threshold drift is left to
			// accumulate rather than re-baselined away.)
			a.wrapTop = anchorContentTop(a.wrap, container);
			a.elTop = a.el.isConnected ? anchorContentTop(a.el, container) : a.elTop;
		}
		if (!a.el.isConnected) {
			// Re-refine after the layout settled so the next delivery is
			// measured against the fresh inner node again.
			a.el = refineScrollAnchor(a.wrap, container);
			a.elTop = anchorContentTop(a.el, container);
		}
	};

	const observeMessagesContent = (element?: HTMLDivElement, containerElement?: HTMLDivElement) => {
		// The message LIST (<ul id="messages-list"> in Messages.svelte) is the
		// element whose box actually tracks content height. The bound content
		// wrapper is h-full (fixed-height flex item): content overflows it, so
		// its box NEVER changes when messages grow — observing only it silently
		// disabled every content-growth reaction (the box does still track
		// container-height changes like keyboard/composer, which is why those
		// worked). The list is re-resolved on every call because it remounts
		// with the chat (the reactive below keys on activeChatId for that).
		const listElement = document.getElementById('messages-list') as HTMLElement | null;
		{
			const dbg = (window as any).__engineDebug;
			if (dbg)
				dbg.push({
					t: performance.now(),
					ev: 'observe-call',
					hasContent: Boolean(element),
					hasContainer: Boolean(containerElement),
					hasList: Boolean(listElement),
					unchanged:
						observedMessagesContentElement === element &&
						observedMessagesContainerElement === containerElement &&
						observedMessagesListElement === listElement
				});
		}
		if (
			observedMessagesContentElement === element &&
			observedMessagesContainerElement === containerElement &&
			observedMessagesListElement === listElement
		) {
			return;
		}
		messagesResizeObserver?.disconnect();
		messagesResizeObserver = null;
		observedMessagesContentElement = element ?? null;
		observedMessagesContainerElement = containerElement ?? null;
		observedMessagesListElement = listElement ?? null;

		if (!element || typeof ResizeObserver === 'undefined') return;
		// Reset any compensation left over from the chat we're leaving, then
		// baseline all three heights so the first RO tick doesn't misread a
		// mount-time layout as a composer-driven viewport change.
		setComposerCompensation(0);
		let lastContainerHeight = containerElement?.clientHeight ?? 0;
		let lastComposerHeight = composerElement?.clientHeight ?? 0;
		let lastListHeight = listElement?.offsetHeight ?? 0;
		messagesResizeObserver = new ResizeObserver((entries) => {
			// The message LIST overflows the scroll box (its box is what tracks
			// content height), so a change in the container's own height means
			// EXTERNAL chrome resized: the composer auto-grew/shrunk, the token
			// panel appeared at completion, keyboard-open --app-height, edit-mode
			// chrome hiding. Composer-driven changes are told apart by the
			// composer's own height delta cancelling the container's.
			const container = messagesContainerElement;
			const containerHeight = container?.clientHeight ?? lastContainerHeight;
			const composerHeight = composerElement?.clientHeight ?? lastComposerHeight;
			const listHeight = observedMessagesListElement?.offsetHeight ?? lastListHeight;
			const containerDelta = containerHeight - lastContainerHeight;
			const composerDelta = composerHeight - lastComposerHeight;
			const contentDelta = listHeight - lastListHeight;
			lastContainerHeight = containerHeight;
			lastComposerHeight = composerHeight;
			lastListHeight = listHeight;
			{
				const dbg = (window as any).__engineDebug;
				if (dbg)
					dbg.push({
						t: performance.now(),
						ev: 'ro',
						autoScroll,
						messagesReady,
						containerDelta,
						composerDelta,
						contentDelta,
						compensation: composerCompensation,
						scrollTop: container?.scrollTop,
						lastObserved: lastObservedScrollTop
					});
			}

			if (messagesReady && container && containerDelta !== 0) {
				if (
					containerDelta > 0 &&
					!autoScroll &&
					performance.now() - keyboardClosedAt > 500
				) {
					// Container GREW (composer shrank / chrome hid). max scrollTop
					// dropped by the same delta and the browser already clamped any
					// scrollTop beyond the new max AT LAYOUT (before this callback)
					// — that clamp is the one-frame slide of the whole conversation
					// down toward the input. Grow the compensation spacer by the
					// delta (scroll range restored ⇒ the clamp's target range never
					// actually shrank) and undo the clamp itself. All pre-paint.
					const maxScrollTopPreCompensation = container.scrollHeight - containerHeight;
					setComposerCompensation(composerCompensation + containerDelta);
					// lastObservedScrollTop is still the PRE-clamp position here:
					// the clamp's own scroll event only fires after this callback.
					if (lastObservedScrollTop > maxScrollTopPreCompensation) {
						{
							const dbg = (window as any).__engineDebug;
							if (dbg)
								dbg.push({
									t: performance.now(),
									ev: 'clamp-restore',
									from: container.scrollTop,
									to: lastObservedScrollTop
								});
						}
						container.scrollTop = lastObservedScrollTop;
					}
				} else if (containerDelta < 0 && composerCompensation > 0) {
					// Container SHRANK while compensation is outstanding (composer
					// growing back): the spacer's room is exactly what the shrink
					// consumes — hand it back so the scroll range never moves.
					setComposerCompensation(
						composerCompensation - Math.min(composerCompensation, -containerDelta)
					);
				}
			}

			// During the initial settle phase the content is hidden and the settle
			// loop owns scroll position — don't fight it. After reveal, this keeps
			// the view pinned to the bottom while streaming / late content grows.
			if (messagesReady && applyExpansionHold()) {
				// A just-clicked expander owns the viewport (see applyExpansionHold):
				// hold it still while its body slides open, whatever else grows.
				showJumpToBottom =
					!autoScroll &&
					Boolean(messagesContainerElement) &&
					getBottomDistance(messagesContainerElement!) > JUMP_TO_BOTTOM_SHOW_PX;
			} else if (messagesReady && autoScroll) {
				// SYNCHRONOUS pin: this observer fires after layout but BEFORE
				// paint, so correcting here means a height change above/at the
				// viewport is never painted un-pinned. The deferred
				// scrollToBottom() (rAF + tick = 1–2 frames later) let each
				// content-visibility realization wave paint displaced first and
				// then snap — the visible "jumps around then settles" on chat
				// open (realization waves keep landing well past the settle
				// budget on chats without stamped heights).
				//
				// The content-side signal is the LIST's box (the only observed
				// element whose height tracks message content).
				const contentChanged = entries.some(
					(e) => e.target === observedMessagesListElement
				);
				// A viewport change fully explained by the composer (heights
				// cancel: typing or clearing a draft, edit-mode chrome hiding)
				// needs no extra pin here: a growing composer covers the bottom of
				// the scrollback, while the browser's natural clamp keeps the tail
				// attached when it shrinks. Non-composer shrinkage (token panel,
				// keyboard) still pins so the tail is never buried.
				const composerDriven =
					containerDelta !== 0 && Math.abs(containerDelta + composerDelta) < 2;
				if (contentChanged) {
					// Absorb outstanding compensation into genuine content growth
					// so the dead band under the conversation drains with the
					// stream instead of lingering into the next turn.
					if (composerCompensation > 0) {
						const consume = Math.min(composerCompensation, Math.max(0, contentDelta));
						if (consume > 0) {
							setComposerCompensation(composerCompensation - consume);
						}
					}
					scrollToBottomNow();
				} else if (!composerDriven && containerDelta < 0) {
					scrollToBottomNow();
				}
			} else if (messagesReady && messagesContainerElement) {
				// Scrolled-up reader: hold their reading position through the
				// resize (manual scroll anchoring — see the engine above). Runs
				// pre-paint, so realization/prepend/late-image shifts are never
				// visible.
				applyScrollCorrection();
				// Content growth fires no scroll event, so a scrolled-up reader's
				// distance-to-bottom can cross the pill threshold silently while a
				// response streams below — refresh the affordance here too.
				showJumpToBottom =
					!autoScroll && getBottomDistance(messagesContainerElement) > JUMP_TO_BOTTOM_SHOW_PX;
			}
			// Any resize can mean new/changed turn heights (pagination prepends,
			// width changes, streamed content) — keep the placeholder
			// measurements fresh. Throttled + scroll-quiet-gated internally.
			messageHeightSweeper.schedule();
		});
		messagesResizeObserver.observe(element);
		// Also watch the scroll BOX itself: chrome outside it (composer auto-grow,
		// the token panel appearing at completion, keyboard-open --app-height,
		// edit-mode chrome hiding) resizes the container via flex with NO
		// content-side resize. Growth is neutralized by the compensation spacer
		// (the browser would otherwise clamp scrollTop and slide the conversation
		// down); only non-composer shrinkage re-pins, so a pinned reader never
		// loses the last lines under new chrome.
		if (containerElement) {
			messagesResizeObserver.observe(containerElement);
		}
		// And the composer, whose delta tells composer-driven viewport changes
		// (typing / clearing a draft) apart from the rest.
		if (composerElement) {
			messagesResizeObserver.observe(composerElement);
		}
		// And the list itself — the ONLY content-growth signal (see above).
		if (listElement) {
			messagesResizeObserver.observe(listElement);
		}
	};

	// Initial-load bottom anchoring. The messages content is revealed (opacity)
	// only once this resolves, so the user never sees the intermediate scroll
	// positions caused by content-visibility:auto messages realizing their real
	// heights, async markdown/code highlight, katex, etc. We re-pin to the bottom
	// every frame until the scroll height is stable for 2 consecutive frames or a
	// time budget elapses, then do a final pin. A newer navigation aborts via the
	// generation guard so stale settles can't move the new chat.
	const settleAtBottom = (generation: number, maxMs = 350) =>
		new Promise<void>((resolve) => {
			const el = messagesContainerElement;
			if (!el) return resolve();
			const start = performance.now();
			let stableFrames = 0;
			let lastHeight = -1;
			const step = () => {
				if (generation !== navigateGeneration) return resolve();
				// A wheel / touch gesture during the initial reveal means the user
				// wants to read up — abort the settle instead of fighting them.
				if (settleInterrupted) return resolve();
				el.scrollTop = el.scrollHeight;
				const h = el.scrollHeight;
				stableFrames = h === lastHeight ? stableFrames + 1 : 0;
				lastHeight = h;
				if (stableFrames >= 2 || performance.now() - start >= maxMs) {
					el.scrollTop = el.scrollHeight;
					return resolve();
				}
				requestAnimationFrame(step);
			};
			requestAnimationFrame(step);
		});

	onDestroy(() => {
		if (scrollToBottomFrame !== null) cancelAnimationFrame(scrollToBottomFrame);
		if (scrollStateFrame !== null) cancelAnimationFrame(scrollStateFrame);
		cancelGlide();
		messagesResizeObserver?.disconnect();
		messageHeightSweeper.destroy();
	});
	let _completedMessageIds = new Set<string>();
	// Dedup only needs a recency window (socket + direct-stream races happen
	// within a turn) — without a cap the set grew for the whole session. Sets
	// iterate in insertion order, so trimming drops the oldest ids first.
	const COMPLETED_MESSAGE_IDS_MAX = 2000;

	const chatCompletedHandler = async (chatId, modelId, responseMessageId, messages) => {
		// Terminal bookkeeping is IDEMPOTENT and must run on EVERY completion
		// signal, including a duplicate one. It used to sit behind the dedup guard
		// below, so whenever a message id saw two completions — the socket +
		// direct-stream race this guard was written for, or a continue/retry that
		// reuses the same assistant id — the second, real completion returned
		// early and never settled the generation. The message rendered as
		// finished while its lifecycle record stayed live, which is precisely the
		// "answer is done but the composer still shows Stop" state.
		const owned = ownsGeneration(responseMessageId);
		chatStreamDebug('[chat-stream] chatCompletedHandler — settling generation', {
			responseMessageId,
			ownedByThisMessage: owned
		});
		if (history.messages[responseMessageId]) {
			history.messages[responseMessageId].done = true;
		}
		const turnSettled = settleGenerationLifecycle(responseMessageId);
		// Stop the observer/mid-join/reconnect resume poll now that this turn has a
		// terminal (we received chat:done, so the poll's safety-net job is done).
		// Prevents one redundant work-state poll + full loadChat ~2s after every turn,
		// which was an extra round-trip and could clobber a user mid branch-nav/edit.
		// A subsequent queue-drain re-arms it via chat:queue:drained -> loadChat.
		if (turnSettled) stopResumeTaskPolling();
		history = { ...history };

		// The rest of the handler is the once-per-message work.
		if (_completedMessageIds.has(responseMessageId)) {
			return;
		}
		_completedMessageIds.add(responseMessageId);
		if (_completedMessageIds.size > COMPLETED_MESSAGE_IDS_MAX) {
			for (const oldId of _completedMessageIds) {
				_completedMessageIds.delete(oldId);
				if (_completedMessageIds.size <= COMPLETED_MESSAGE_IDS_MAX) break;
			}
		}

		await tick();

		if (isVisibleChatEvent(chatId)) {
			if (!$temporaryChatEnabled) {
				// Backend already persisted the final message state via realtime
				// chat save during streaming, and the chat:updated socket event
				// (Wire Contract #7) bumps the sidebar in all tabs. No further
				// client-side work needed.
			}
		}
	};

	const chatActionHandler = async (chatId, actionId, modelId, responseMessageId, event = null) => {
		try {
			const messages = createMessagesList(history, responseMessageId);

			const res = await chatAction(localStorage.token, actionId, {
				model: modelId,
				messages: messages.map((m) => ({
					id: m.id,
					role: m.role,
					content: m.content,
					info: m.info ? m.info : undefined,
					timestamp: m.timestamp,
					...(m.sources ? { sources: m.sources } : {}),
					...(m.reasoning_details ? { reasoning_details: m.reasoning_details } : {}),
					...(m.reasoning_details_per_round
						? { reasoning_details_per_round: m.reasoning_details_per_round }
						: {})
				})),
				...(event ? { event: event } : {}),
				model_item: $models.find((m) => m.id === modelId),
				chat_id: chatId,
				session_id: $socket?.id,
				id: responseMessageId
			}).catch((error) => {
				toast.error(`${error}`);
				messages.at(-1).error = { content: error };
				return null;
			});

			if (res !== null && res.messages) {
				// Update chat history with the new messages
				for (const message of res.messages) {
					history.messages[message.id] = {
						...history.messages[message.id],
						...(history.messages[message.id].content !== message.content
							? { originalContent: history.messages[message.id].content }
							: {}),
						...message
					};
				}
			}

			if (isVisibleChatEvent(chatId)) {
				if (!$temporaryChatEnabled) {
					const actionOps: PatchChatOp[] = [];
					for (const m of res?.messages ?? []) {
						if (!m?.id) continue;
						actionOps.push({
							op: 'update_message_content',
							message_id: m.id,
							content: m.content,
							...(m.files !== undefined ? { files: m.files } : {}),
							...(m.annotation !== undefined ? { annotation: m.annotation } : {})
						});
					}
					if (actionOps.length > 0) {
						await patchChat(localStorage.token, chatId, actionOps);
					}
				}
			}
		} finally {
			// Chat actions never own a generation lifecycle record, so there is no
			// generation state for them to clean up — `generating` and `taskIds`
			// derive from the registry this never touched.
			chatStreamDebug('[chat-stream] chatActionHandler finally', { responseMessageId });
		}
	};

	const createMessagePair = async (userPrompt) => {
		messageInput?.setText('');
		if (selectedModels.length === 0) {
			toast.error($i18n.t('Model not selected'));
		} else {
			const modelId = selectedModels[0];
			const model = $models.filter((m) => m.id === modelId).at(0);

			const messages = createMessagesList(history, history.currentId);
			const parentMessage = messages.length !== 0 ? messages.at(-1) : null;

			const userMessageId = uuidv4();
			const responseMessageId = uuidv4();

			const userMessage = {
				id: userMessageId,
				parentId: parentMessage ? parentMessage.id : null,
				childrenIds: [responseMessageId],
				role: 'user',
				content: userPrompt ? userPrompt : `[PROMPT] ${userMessageId}`,
				timestamp: Math.floor(Date.now() / 1000)
			};

			const responseMessage = {
				id: responseMessageId,
				parentId: userMessageId,
				childrenIds: [],
				role: 'assistant',
				content: `[RESPONSE] ${responseMessageId}`,
				done: true,

				model: modelId,
				modelName: model.name ?? model.id,
				modelIdx: 0,
				timestamp: Math.floor(Date.now() / 1000)
			};

			if (parentMessage) {
				parentMessage.childrenIds.push(userMessageId);
				history.messages[parentMessage.id] = parentMessage;
			}
			history.messages[userMessageId] = userMessage;
			history.messages[responseMessageId] = responseMessage;

			history.currentId = responseMessageId;

			await tick();

			if (autoScroll) {
				scrollToBottom();
			}

			if (messages.length === 0) {
				await initChatHandler(history);
			} else {
				await saveChatHandler(getVisibleChatId(), history, params, [
					{
						op: 'append_message',
						message_id: userMessage.id,
						parent_id: userMessage.parentId,
						role: 'user',
						content: userMessage.content,
						timestamp: userMessage.timestamp
					},
					{
						op: 'append_message',
						message_id: responseMessage.id,
						parent_id: responseMessage.parentId,
						role: 'assistant',
						content: responseMessage.content,
						model: responseMessage.model,
						modelName: responseMessage.modelName,
						modelIdx: responseMessage.modelIdx,
						timestamp: responseMessage.timestamp
					},
					{ op: 'set_history_current_id', current_id: history.currentId }
				]);
			}
		}
	};

	// Content identity for a container output descriptor (mirrors the backend
	// _file_content_key): two descriptors with the same (workspace_path, sha256)
	// are the same logical file even when their randomly-minted id differs (which
	// happens when the same output is imported twice — e.g. a concurrent fanout
	// rerun). Non-container files (no container_workspace) fall back to id-only.
	const fileContentKey = (file: any): string | null => {
		const cw = file?.container_workspace;
		if (cw && cw.workspace_path && cw.sha256) {
			return `cw\u0000${cw.workspace_path}\u0000${cw.sha256}`;
		}
		return null;
	};

	// Merge incoming file descriptors into an existing list, deduping by id AND by
	// content identity, keeping existing entries first (mirrors the backend
	// _merge_files so the LIVE list matches the persisted/reloaded list).
	const mergeMessageFiles = (existing: any[], incoming: any[]): any[] => {
		const merged = Array.isArray(existing) ? [...existing] : [];
		const seenIds = new Set<string>();
		const seenContent = new Set<string>();
		for (const f of merged) {
			const id = f?.id ?? f?.url ?? f?.content ?? JSON.stringify(f);
			if (id) seenIds.add(id);
			const ck = fileContentKey(f);
			if (ck) seenContent.add(ck);
		}
		for (const f of incoming ?? []) {
			const id = f?.id ?? f?.url ?? f?.content ?? JSON.stringify(f);
			const ck = fileContentKey(f);
			if (id && seenIds.has(id)) continue;
			if (ck && seenContent.has(ck)) continue;
			if (id) seenIds.add(id);
			if (ck) seenContent.add(ck);
			merged.push(f);
		}
		return merged;
	};

	// Extensions FilePreview.svelte can actually render inline: images/audio/pdf
	// directly, text/code as a <pre>/Markdown (the backend stores data.content for
	// these — mirror of container_workspace.py _TEXT_PREVIEW_EXTS), office docs only
	// via their converted PDF (preview_file_id). Anything else falls through to the
	// "No inline preview is available" placeholder — never auto-open those.
	const INLINE_PREVIEWABLE_EXTS = new Set([
		// documents rendered directly
		'pdf',
		// images
		'png',
		'jpg',
		'jpeg',
		'gif',
		'webp',
		'avif',
		'bmp',
		'ico',
		'svg',
		// audio
		'mp3',
		'wav',
		'ogg',
		'oga',
		'm4a',
		'flac',
		'aac',
		'opus',
		// text/code (backend writes data.content for these)
		'txt',
		'md',
		'markdown',
		'rst',
		'csv',
		'tsv',
		'json',
		'jsonl',
		'ndjson',
		'yaml',
		'yml',
		'toml',
		'ini',
		'cfg',
		'conf',
		'env',
		'log',
		'xml',
		'py',
		'pyi',
		'ipynb',
		'js',
		'mjs',
		'cjs',
		'ts',
		'tsx',
		'jsx',
		'vue',
		'svelte',
		'java',
		'kt',
		'kts',
		'scala',
		'groovy',
		'c',
		'cc',
		'cpp',
		'cxx',
		'h',
		'hpp',
		'hxx',
		'rs',
		'go',
		'rb',
		'php',
		'pl',
		'pm',
		'lua',
		'r',
		'jl',
		'dart',
		'swift',
		'm',
		'mm',
		'cs',
		'fs',
		'fsx',
		'ex',
		'exs',
		'erl',
		'hs',
		'ml',
		'mli',
		'clj',
		'cljs',
		'sh',
		'bash',
		'zsh',
		'fish',
		'ps1',
		'bat',
		'cmd',
		'sql',
		'graphql',
		'gql',
		'proto',
		'css',
		'scss',
		'sass',
		'less',
		'tex',
		'bib',
		'srt',
		'vtt',
		'patch',
		'diff',
		'gitignore',
		'dockerignore',
		'editorconfig'
	]);

	const fileHasInlinePreview = (file: any): boolean => {
		// Office docs are previewable iff their LibreOffice→PDF conversion exists.
		if (file?.preview_file_id || file?.container_workspace?.preview_file_id) return true;
		const name = String(file?.name ?? file?.filename ?? '').toLowerCase();
		const dot = name.lastIndexOf('.');
		const ext = dot >= 0 ? name.slice(dot + 1) : '';
		return INLINE_PREVIEWABLE_EXTS.has(ext);
	};

	const openGeneratedFilePreview = (files: any[] = [], siblings: any[] = files) => {
		// Auto-open the first file we can actually render; if nothing in the batch
		// has an inline preview, don't open the panel at all — popping open a
		// "No inline preview is available for this file type." placeholder helps
		// nobody. The files are still listed on the message for manual open.
		const file = files.find(
			(item) => item?.type === 'file' && item?.id && fileHasInlinePreview(item)
		);
		if (!file) return;
		openFilePreview(file, siblings);
	};

	const chatCompletionEventHandler = async (data, message, chatId) => {
		const {
			id,
			done,
			choices,
			content,
			content_blocks,
			sources,
			selected_model_id,
			error,
			usage,
			files: event_files,
			reasoning_details,
			reasoning_details_per_round
		} = data;
		let shouldRunTTS = false;
		let shouldFlushStreamingUpdate = false;

		if (isUserStoppedMessageId(message.id)) {
			cancelStreamingMessageFlush(message.id);
			message.done = true;
			releaseStreamMirror(message.id);
			history.messages[message.id] = message;
			history = { ...history };
			return;
		}

		if (error) {
			await handleOpenAIError(error, message, 'chatCompletionEventHandler:data.error');
			// Error takes priority — do NOT fall through to the `done`
			// handler which would finalize the message as completed and
			// prevent the automatic retry mechanism from triggering.
			return;
		}

		if (sources && !message?.sources) {
			message.sources = sources;
			shouldFlushStreamingUpdate = true;
		}

		if (choices) {
			if (choices[0]?.message?.content) {
				// Non-stream response
				message.content += choices[0]?.message?.content;
				shouldFlushStreamingUpdate = true;
				shouldRunTTS = true;

				if (choices[0]?.message?.reasoning_details) {
					message.reasoning_details = choices[0].message.reasoning_details;
					shouldFlushStreamingUpdate = true;
				}
			} else {
				// Stream response
				if (choices[0]?.delta?.reasoning_details) {
					if (!Array.isArray(message.reasoning_details)) {
						message.reasoning_details = [];
					}
					for (const detail of choices[0].delta.reasoning_details) {
						mergeReasoningDetail(message.reasoning_details, detail);
					}
					shouldFlushStreamingUpdate = true;
				}

				let value = choices[0]?.delta?.content ?? '';
				if (!(message.content == '' && value == '\n')) {
					message.content += value;
					shouldFlushStreamingUpdate = true;
					shouldRunTTS = true;

					if (navigator.vibrate && ($settings?.hapticFeedback ?? false)) {
						navigator.vibrate(5);
					}
				}
			}
		}

		// Some backends may only attach final `reasoning_details` on the done event (no `choices` deltas).
		if (Array.isArray(reasoning_details)) {
			if (reasoning_details.length > 0) {
				message.reasoning_details = reasoning_details;
				shouldFlushStreamingUpdate = true;
			}
		} else if (reasoning_details) {
			message.reasoning_details = reasoning_details;
			shouldFlushStreamingUpdate = true;
		}

		// Per-round reasoning_details (one array per stream round / tool-call
		// round) lets the chat replay attach the correct round's reasoning to
		// each tool_calls assistant message in multi-turn follow-ups. Without
		// this, only the last round's reasoning survives in `reasoning_details`.
		if (Array.isArray(reasoning_details_per_round) && reasoning_details_per_round.length > 0) {
			message.reasoning_details_per_round = reasoning_details_per_round;
			shouldFlushStreamingUpdate = true;
		}

		if (Array.isArray(content_blocks)) {
			// Structured content blocks are the canonical replay form. They travel
			// alongside the legacy `content` HTML projection for backwards compat
			// and keep the API replay byte-stable with the live tool-call loop.
			message.content_blocks = content_blocks;
			shouldFlushStreamingUpdate = true;
		}

		if (content) {
			// REALTIME_CHAT_SAVE is disabled
			message.content = content;
			shouldFlushStreamingUpdate = true;
			shouldRunTTS = true;

			if (navigator.vibrate && ($settings?.hapticFeedback ?? false)) {
				navigator.vibrate(5);
			}
		}

		if (selected_model_id) {
			message.selectedModelId = selected_model_id;
			message.arena = true;
			shouldFlushStreamingUpdate = true;
		}

		if (usage) {
			message.usage = usage;
			applyUsageToChatTokenStats(chatId, message.id, usage);
			chatTokenStatsRefreshTrigger.update((n) => n + 1);
			shouldFlushStreamingUpdate = true;
		}

		if (Array.isArray(event_files) && event_files.length > 0) {
			message.files = mergeMessageFiles(message.files ?? [], event_files);
			shouldFlushStreamingUpdate = true;
		}

		if (done) {
			if (Array.isArray(event_files) && event_files.length > 0) {
				// Open the just-generated file, but carry the FULL accumulated
				// message.files as siblings so sandbox: links inside it (which may
				// point at files from an earlier event in this turn) resolve.
				openGeneratedFilePreview(event_files, message.files ?? event_files);
			}

			message = { ...message };
			emitPendingTTSParts(message, { done: true });
			cancelStreamingMessageFlush(message.id);
			message.done = true;
			releaseStreamMirror(message.id);
			// Backstop: flip any subagent card still 'running' on normal completion
			// (its own terminal event was missed) so it stops a runaway clock.
			flipRunningSubagentsTerminal(message?.id ?? null);

			if ($settings.responseAutoCopy) {
				copyToClipboard(message.content);
			}

			if ($settings.responseAutoPlayback && !$showCallOverlay) {
				await tick();
				document.getElementById(`speak-button-${message.id}`)?.click();
			}

			eventTarget.dispatchEvent(
				new CustomEvent('chat:finish', {
					detail: {
						id: message.id,
						content: message.content
					}
				})
			);

			history.messages[message.id] = message;
			history = { ...history };

			await tick();
			if (autoScroll) {
				scrollToBottom();
			}

			await chatCompletedHandler(
				chatId,
				message.model,
				message.id,
				createMessagesList(history, message.id)
			);

			// Trigger token stats refresh if this message had usage data
			if (message.usage) {
				chatTokenStatsRefreshTrigger.update((n) => n + 1);
			}
		} else if (shouldFlushStreamingUpdate) {
			history.messages[message.id] = message;
			scheduleStreamingMessageFlush(message.id, { runTTS: shouldRunTTS, ownerId: message.id });
		}
	};

	const writeMirrorToMessage = (mirror: StreamMirror, message: any) => {
		// Hand the live array to the renderer; downstream code already treats
		// content_blocks as the canonical replay form (ResponseMessage.svelte).
		message.content_blocks = mirror.content_blocks;
	};

	const requestStreamSnapshot = async (
		messageId: string,
		chatId: string | null,
		{ force = false, heal = false }: { force?: boolean; heal?: boolean } = {}
	) => {
		// Capture navigation identity: the snapshot fetch below is a network RTT,
		// during which the user may navigate to a different chat. `history` is then
		// the OTHER chat's, so applying this snapshot (or materializing a row) would
		// corrupt it / spawn a phantom message. Bail after the await if we've moved.
		const snapGeneration = navigateGeneration;
		const snapVisibleChat = getVisibleChatId();
		if (isUserStoppedMessageId(messageId)) {
			const message = history.messages[messageId];
			if (message) {
				cancelStreamingMessageFlush(messageId);
				message.done = true;
				history.messages[messageId] = message;
				history = { ...history };
			}
			return;
		}

		const mirror = getOrCreateStreamMirror(messageId);
		if (heal) {
			// Stamp BEFORE any await so the delta-driven re-arm debounce in
			// chatDeltaHandler measures from the most recent attempt, even one
			// that is still in flight or about to fail.
			mirror.lastHealRequestAt = Date.now();
		}
		if (mirror.snapshotPromise) {
			if (!force) return mirror.snapshotPromise;
			await mirror.snapshotPromise.catch(() => undefined);
		}

		mirror.snapshotting = true;
		// Set when the buffered-delta replay below hits a structural gap (an op
		// targeting a block the reconciled mirror doesn't have) — the .finally
		// then schedules ONE follow-up reconcile to heal it.
		let needsFollowUpSnapshot = false;
		// Set when adoption REWOUND the mirror below the wire (heal/terminal
		// authority at a lower snapshot version) — the .finally replays the
		// tail from the adopted version so the mirror catches back up.
		let rewoundBelowWire = false;
		mirror.snapshotPromise = (async () => {
			let snap: any = null;
			try {
				snap = await getStreamSnapshot(localStorage.token, messageId, chatId);
			} catch (err) {
				console.error('[chat:delta] snapshot fetch failed', messageId, err);
				return;
			}

			if (!snap) {
				return;
			}

			// Navigated to a different chat during the fetch — do NOT touch the
			// now-foreign `history` (would clobber it or materialize a phantom row).
			if (snapGeneration !== navigateGeneration) {
				return;
			}
			// Identity guard (C8): when navigation happened BEFORE this call (e.g. the
			// resume poller's getActiveStreamsByChatId await already bumped the
			// generation, captured here post-bump), the generation check above passes
			// even though we've moved. Reject by IDENTITY: this snapshot targets
			// `chatId`, but `history` is whatever chat is visible now — applying it,
			// especially materializing a row, grafts a phantom 'researching' bubble
			// into the freshly-navigated chat (and a save can persist that alien node).
			const _visibleNow = getVisibleChatId();
			if (
				(chatId && _visibleNow && chatId !== _visibleNow) ||
				(snapVisibleChat && _visibleNow && snapVisibleChat !== _visibleNow)
			) {
				return;
			}

			let message = history.messages[messageId];
			if (!message) {
				// The server has an in-flight (or just-finished) stream for a
				// message this tab never created — e.g. a queued follow-up drained
				// server-side, and loadChat() either raced the persistence or this
				// tab attached late. Materialize a minimal assistant row from the
				// snapshot so the response renders live, instead of bailing (which
				// left the user staring at an empty date divider until the terminal
				// reload). Only do this for a still-active stream; a terminal
				// snapshot for an unknown row is handled by the resume-poll/reload.
				if (snap.status === 'done' || snap.status === 'cancelled' || snap.status === 'error') {
					return;
				}
				// If the snapshot's parent (the user message) isn't in our history, this
				// tab is missing intervening context — e.g. the cross-device
				// chat:user-message was skipped as an oversized inline data: image, so we
				// never got the prompt bubble. Grafting the assistant onto our stale
				// currentId would mis-parent it AND leave the user bubble absent for the
				// whole turn (chat:done then resolves the materialized row, so no repair
				// ever fires). Reload to fetch the authoritative tree (user + assistant)
				// instead; the .finally below resets the mirror so the reload's stream
				// attaches cleanly.
				if (snap.parentId && !history.messages[snap.parentId]) {
					void loadChat();
					return;
				}
				const parentId =
					snap.parentId && history.messages[snap.parentId]
						? snap.parentId
						: (history.currentId ?? null);
				message = {
					id: messageId,
					parentId,
					childrenIds: [],
					role: 'assistant',
					content: '',
					content_blocks: [],
					model: snap.model ?? snap.selected_model_id ?? selectedModels?.[0] ?? '',
					modelName: undefined,
					done: false,
					timestamp: Math.floor(Date.now() / 1000)
				};
				history.messages[messageId] = message;
				const parent = parentId ? history.messages[parentId] : null;
				if (parent) {
					if (!Array.isArray(parent.childrenIds)) parent.childrenIds = [];
					if (!parent.childrenIds.includes(messageId)) parent.childrenIds.push(messageId);
				}
				history.currentId = messageId;
				history = { ...history };
				// Structure changed (new node + currentId); bump so Messages.svelte
				// re-walks the chain and the row actually paints.
				bumpMessageStructure();

				// OBS-2 (multi-client): we attached to a remote turn purely from a
				// delta/snapshot — no chat:user-message ran (e.g. the prompt carried an
				// oversized image, so the cross-device bubble was deferred). Mirror the
				// chat:user-message observer path (G5): register the observed work and
				// start the resume-task poll so a missed terminal chat:done can't strand
				// this tab in the working state, and cross-device Stop works. Skipped
				// when we have our OWN in-flight turn (concurrent-send guard).
				if (
					generationLifecycles.activeForChat(chatId).length === 0 &&
					chatId &&
					!chatId.startsWith('local:')
				) {
					generationLifecycles.observe(chatId, messageId, navigateGeneration);
					startResumeTaskPolling(chatId);
				}
			}

			if (isUserStoppedMessageId(messageId)) {
				cancelStreamingMessageFlush(messageId);
				message.done = true;
				history.messages[messageId] = message;
				history = { ...history };
				return;
			}

			const prevMirrorVersion = mirror.version;
			const snapVersion = typeof snap.version === 'number' ? snap.version : 0;
			const snapRun = typeof snap.run === 'number' && snap.run > 0 ? snap.run : 0;
			const snapTerminal =
				snap.status === 'done' || snap.status === 'cancelled' || snap.status === 'error';

			const snapToolResults = new Map();
			if (snap.tool_results && typeof snap.tool_results === 'object') {
				for (const [k, v] of Object.entries(snap.tool_results)) {
					snapToolResults.set(k, normalizeToolResultEntry(k, v));
				}
			}
			const snapshotContentBlocks = Array.isArray(snap.content_blocks)
				? hydrateToolResultsInBlocks(snap.content_blocks.slice(), snapToolResults)
				: [];
			const liveContentBlocks = Array.isArray(message.content_blocks)
				? message.content_blocks
				: mirror.content_blocks;

			// Pure, unit-tested decision — see decideSnapshotAdoption in
			// stream-protocol.ts for the rationale (stale-run / run-advance /
			// version authority / heal-with-rewind / never-wipe-content-with-
			// empty). `heal` marks authoritative reconciles: structural gap,
			// server-sent op:snapshot (an oversized op was DROPPED from the
			// wire), or terminal chat:done — the server state must win even at
			// a lower version, with the tail replayed forward afterwards.
			const adoption = decideSnapshotAdoption({
				snapRun,
				snapVersion,
				snapTerminal,
				snapHasContent: snapshotContentBlocks.length > 0,
				mirrorRun: mirror.run,
				mirrorVersion: prevMirrorVersion,
				liveHasContent: liveContentBlocks.length > 0 || mirror.content_blocks.length > 0,
				heal
			});
			if (adoption === 'ignore') {
				chatStreamDebug('[chat-stream] ignoring stale-run snapshot', {
					messageId,
					snapRun,
					mirrorRun: mirror.run
				});
				return;
			}

			mirror.tool_results = snapToolResults;
			if (adoption === 'adopt') {
				mirror.content_blocks = snapshotContentBlocks;
				mirror.version = snapVersion;
				if (snapRun) mirror.run = snapRun;
				// Server truth replaced the blocks wholesale — any structural
				// incoherence latched on this mirror is healed.
				mirror.needsHeal = false;
				if (snapVersion < prevMirrorVersion) {
					// Authoritative rewind (heal/terminal): the wire is ahead of
					// this snapshot — .finally replays the tail from snapVersion
					// so the healed mirror catches back up instead of stalling.
					rewoundBelowWire = true;
				}
				// Refresh the session cache NOW: the cached copy may hold exactly
				// the corrupted/stale blocks this adoption replaced, and a later
				// virgin-mirror hydrate would reinstall them.
				if (mirror.version > 0) {
					writeStreamCache(messageId, chatId);
				} else {
					clearStreamCache(messageId);
				}
			} else {
				chatStreamDebug('[chat-stream] snapshot not adopted — keeping live blocks', {
					messageId,
					snapshotStatus: snap.status,
					snapshotVersion: snapVersion,
					snapshotRun: snapRun,
					keptVersion: prevMirrorVersion,
					keptRun: mirror.run
				});
			}
			if (mirror.version > 0) {
				scheduleStreamAck(messageId, mirror.version);
			}

			const buffered = mirror.pending_deltas;
			mirror.pending_deltas = [];

			writeMirrorToMessage(mirror, message);
			if (snap.usage) {
				message.usage = snap.usage;
				applyUsageToChatTokenStats(chatId, message.id, snap.usage);
				chatTokenStatsRefreshTrigger.update((n) => n + 1);
			}
			if (Array.isArray(snap.sources)) {
				message.sources = snap.sources;
			}
			if (snap.selected_model_id) {
				message.selectedModelId = snap.selected_model_id;
				message.arena = true;
			}
			if (snap.status === 'error' && snap.error) message.error = snap.error;
			if (snap.status === 'done' || snap.status === 'cancelled' || snap.status === 'error') {
				message.done = true;
			}

			for (const d of buffered) {
				// Run filters: a buffered delta from an OLDER run than the adopted
				// snapshot is superseded noise; one from a NEWER run means a retry
				// started while this snapshot was in flight — re-buffer it so the
				// follow-up snapshot (scheduled in .finally) reconciles the new run.
				if (d.run && mirror.run && d.run < mirror.run) continue;
				if (d.run && mirror.run && d.run > mirror.run) {
					mirror.pending_deltas.push(d);
					continue;
				}
				if (d.version <= mirror.version) continue;
				if (d.version > mirror.version + 1) {
					// Still gapped after snapshot — re-buffer and refetch.
					mirror.pending_deltas.push(d);
					continue;
				}
				if (applyDeltaOp(mirror, d.op, d.payload)) {
					needsFollowUpSnapshot = true;
					// Same latch as the live path: incoherent until a snapshot
					// actually adopts (the follow-up below may fail on a blip).
					mirror.needsHeal = true;
					clearStreamCache(messageId);
				}
				// op=usage carries no mirror content, so applyDeltaOp is a no-op for
				// it — apply it to the live counter here too, else a usage delta that
				// was buffered behind a snapshot/version-gap never reaches the pill on
				// the optimistic path. (The authoritative chat:token-usage push also
				// corrects this within ~0.5s, but keep the optimistic path coherent.)
				if (d.op === 'usage' && d.payload?.usage) {
					message.usage = d.payload.usage;
					applyUsageToChatTokenStats(chatId, message.id, d.payload.usage);
					chatTokenStatsRefreshTrigger.update((n) => n + 1);
				}
				mirror.version = d.version;
			}
			if (mirror.version > 0) {
				scheduleStreamAck(messageId, mirror.version);
				flushStreamAcks();
			}

			writeMirrorToMessage(mirror, message);
			history.messages[messageId] = message;
			scheduleStreamingMessageFlush(messageId, { runTTS: false, ownerId: messageId });
			scheduleStreamCacheWrite(messageId, chatId);
		})().finally(() => {
			mirror.snapshotting = false;
			mirror.snapshotPromise = null;
			// Converge NOW instead of waiting for the next live delta (which may
			// never come if the gap sits at the very end of the stream):
			//  - rewoundBelowWire: adoption took an authoritative older snapshot
			//    (heal/terminal) — replay the tail from the adopted version.
			//  - pending_deltas: still gapped / a newer run started mid-snapshot.
			//  - needsFollowUpSnapshot: buffered replay hit a structural gap.
			// Replay-first (cheap delta catch-up), snapshot only if that leaves
			// gaps. NOTE: must run AFTER snapshotPromise clears — the old
			// in-body re-request deduped against the still-pending promise
			// (itself) and silently never fired. Deferred + identity-guarded so
			// a navigated-away chat or a released mirror can't loop.
			if (mirror.pending_deltas.length > 0 || needsFollowUpSnapshot || rewoundBelowWire) {
				setTimeout(() => {
					if (
						streamMirrors.get(messageId) !== mirror ||
						(chatId && getVisibleChatId() !== chatId)
					) {
						return;
					}
					void (async () => {
						const replayed = await requestStreamReplay(messageId, chatId).catch(() => false);
						// Replay delivered these same versions through the live
						// handler — anything buffered at/below the mirror is now a
						// duplicate, not a gap.
						mirror.pending_deltas = mirror.pending_deltas.filter((d) => d.version > mirror.version);
						if (!replayed || mirror.pending_deltas.length > 0 || needsFollowUpSnapshot) {
							void requestStreamSnapshot(messageId, chatId, {
								heal: needsFollowUpSnapshot
							});
						}
					})();
				}, 250);
			}
		});

		return mirror.snapshotPromise;
	};

	const requestStreamReplay = async (messageId: string, chatId: string | null) => {
		if (!messageId || $temporaryChatEnabled) return false;
		hydrateStreamFromCache(messageId, chatId);
		const mirror = getOrCreateStreamMirror(messageId);
		// Send the run id alongside after_version: the server refuses to replay
		// across a run boundary (snapshot_required) — replaying a NEW run's ops
		// onto an OLD run's mirror (or vice versa) is exactly how reasoning got
		// spliced into answer text. Also guards the reverse freeze: without the
		// run, an old-run after_version above the new run's counter used to get
		// back "ok, no events" and the client would sit caught-up-but-frozen.
		const replay = await getStreamDeltas(
			localStorage.token,
			messageId,
			chatId,
			mirror.version,
			mirror.run || null
		).catch(() => null);
		if (!replay || replay.status !== 'ok' || !Array.isArray(replay.events)) {
			return false;
		}
		for (const replayEvent of replay.events) {
			const event = {
				chat_id: chatId,
				message_id: messageId,
				data: replayEvent
			};
			if (applyBatchedStreamEvent(event)) continue;
			await chatEventHandler(event, () => {}, { skipTick: true });
		}
		if (mirror.version > 0) {
			scheduleStreamAck(messageId, mirror.version);
			flushStreamAcks();
		}
		return true;
	};

	const snapshotActiveStreamsForChat = async (chatIdToSnapshot: string | null) => {
		if (!chatIdToSnapshot || $temporaryChatEnabled) return [];
		// Capture navigation identity BEFORE the network await. If the user navigates
		// during the getActiveStreamsByChatId RTT, `history` becomes another chat's —
		// mutating message.done below or materializing a snapshot row would corrupt it
		// or spawn a phantom 'researching' bubble in the freshly-navigated chat. Bail
		// on either a generation bump or a visible-chat identity change.
		const snapGen = navigateGeneration;
		const active = await getActiveStreamsByChatId(localStorage.token, chatIdToSnapshot).catch(
			() => null
		);
		if (snapGen !== navigateGeneration || getVisibleChatId() !== chatIdToSnapshot) {
			return [];
		}
		const streams = Array.isArray(active?.streams) ? active.streams : [];
		const messageIds = streams
			.map((stream: any) => stream?.message_id)
			.filter(
				(id: unknown): id is string =>
					typeof id === 'string' && id.length > 0 && !isUserStoppedMessageId(id)
			);

		// Reconcile each mirror's RUN against the server's active-stream registry
		// BEFORE replay/snapshot below. If the message was retried/continued while
		// this tab was away, the mirror's version space is the DEAD run's —
		// resetting here lets requestStreamReplay ask for the new run from
		// version 0 (a cheap delta replay) instead of sending a stale
		// after_version and thrashing through snapshot_required.
		for (const stream of streams) {
			const mid = stream?.message_id;
			if (typeof mid !== 'string' || !mid || isUserStoppedMessageId(mid)) continue;
			if (typeof stream?.run === 'number' && stream.run > 0) {
				reconcileMirrorRun(getOrCreateStreamMirror(mid), stream.run);
			}
		}

		for (const mid of messageIds) {
			const message = history?.messages?.[mid];
			if (
				message &&
				message.role === 'assistant' &&
				message.done !== true &&
				message.userStopped !== true &&
				!message.error
			) {
				message.done = false;
			}
		}

		await Promise.all(
			messageIds.map(async (mid) => {
				// An incoherence-latched mirror must NOT replay-and-continue: a
				// replay from its (corrupted-but-contiguous) version comes back
				// clean and leaves the fabricated block in place. Only an
				// authoritative snapshot adoption heals it.
				if (streamMirrors.get(mid)?.needsHeal) {
					await requestStreamSnapshot(mid, chatIdToSnapshot, { force: true, heal: true });
					return;
				}
				const replayed = await requestStreamReplay(mid, chatIdToSnapshot).catch(() => false);
				if (!replayed) {
					await requestStreamSnapshot(mid, chatIdToSnapshot);
				}
			})
		);
		return messageIds;
	};

	const chatDeltaHandler = (
		delta: { message_id?: string; version?: number; run?: number; op?: string; payload?: any },
		message: any,
		chatId: string | null
	) => {
		if (isUserStoppedMessageId(message.id)) {
			cancelStreamingMessageFlush(message.id);
			message.done = true;
			releaseStreamMirror(message.id);
			history.messages[message.id] = message;
			history = { ...history };
			return;
		}

		const perf = streamPerfStart();
		const op = delta.op || '';
		const version = typeof delta.version === 'number' ? delta.version : 0;
		const run = typeof delta.run === 'number' ? delta.run : 0;
		const mirror = getOrCreateStreamMirror(message.id);

		// Run gate FIRST: a delta from an OLDER run is a late emit that raced a
		// retry/continue — never buffer or apply it (it would splice superseded
		// ops into the current run's content). A NEWER run resets the mirror:
		// the server restarted this message's version space at 0, so everything
		// the mirror holds (blocks, buffered deltas, version) belongs to a dead
		// run. Without this, the `version <= mirror.version` staleness gate
		// below silently swallowed EVERY delta of a retry (frozen/empty
		// response until a manual reload).
		const runState = reconcileMirrorRun(mirror, run);
		if (runState === 'stale') {
			streamPerfEnd('chat.delta.stale_run', perf);
			return;
		}
		if (runState === 'reset') {
			chatStreamDebug('[chat-stream] delta run advanced — mirror reset', {
				messageId: message.id,
				run,
				version
			});
		}

		if (mirror.snapshotting) {
			mirror.pending_deltas.push({ op, version, run, payload: delta.payload });
			return;
		}

		if (version > mirror.version + 1) {
			mirror.pending_deltas.push({ op, version, run, payload: delta.payload });
			requestStreamSnapshot(message.id, chatId);
			return;
		}

		if (version !== 0 && version <= mirror.version) {
			// Stale/duplicate replay (e.g. snapshot already covered it).
			return;
		}

		// Version 1 is by definition the FIRST op of a run — the server's mirror
		// is empty at that instant. A fresh (version-0) mirror seeded from
		// persisted blocks (getOrCreateStreamMirror copies message.content_blocks,
		// which for a retry is the FAILED run's partial) must start from scratch
		// here: otherwise block_open's content-preserving defense grafts the old
		// run's text into the new run's blocks (mixed/duplicated content).
		if (version === 1 && mirror.version === 0 && mirror.content_blocks.length > 0) {
			mirror.content_blocks = [];
			mirror.tool_results = new Map();
		}

		// Continue/regenerate resume (multi-client): a FRESH delta for a message this
		// tab holds as done=true means the turn was reactivated on another device (e.g.
		// "Continue Response", which reuses the SAME assistant id and emits no
		// chat:user-message — so the handleRemoteUserMessage/G5 observer attach never
		// ran here). Flip it back to streaming and, unless this is our OWN in-flight turn,
		// register the observed work + arm the resume-poll backstop, so the observer
		// shows the working/Stop state (not idle) and can't strand on a missed terminal.
		if (message.done === true) {
			message.done = false;
			// A retryable generation error emits chat:message:error to ALL tabs, then the
			// AUTHOR silently retries the SAME message id. Observers set message.error +
			// done and would otherwise stay stuck showing a red banner even as the retry
			// streams — and it never self-heals (snapshot only SETS error). Clear it here
			// so the reactivated stream renders cleanly; the retry's chat:done finalizes it.
			if (message.error) message.error = undefined;
			history.messages[message.id] = message;
			// This id already completed once (turn 1), so it's in _completedMessageIds;
			// clear it so the continuation's terminal chat:done runs chatCompletedHandler
			// in full (it early-returns on a duplicate id) — otherwise generating/taskIds
			// would never clear at the continuation's done and the input would stay stuck
			// in the Stop state until the 2s resume poll.
			_completedMessageIds.delete(message.id);
			if (
				generationLifecycles.activeForChat(chatId).length === 0 &&
				chatId &&
				!chatId.startsWith('local:')
			) {
				generationLifecycles.observe(chatId, message.id, navigateGeneration);
				startResumeTaskPolling(chatId);
			}
		}

		if (op === 'snapshot') {
			// The server DROPPED an oversized op from the wire and sent this
			// marker instead — the mirror is now missing that op's effect and
			// only the snapshot can restore coherence. Do NOT advance
			// mirror.version here: bumping it without the op's content made the
			// mirror permanently incoherent whenever the snapshot fetch failed
			// (flaky link) — subsequent contiguous deltas then applied onto
			// stale blocks (the reasoning-as-text fabrication, no disconnect
			// needed). Left un-bumped, the next delta trips the normal version-
			// gap machinery (buffer + snapshot) until coherence is restored.
			void requestStreamSnapshot(message.id, chatId, { force: true, heal: true });
			return;
		}

		const structuralGap = applyDeltaOp(mirror, op, delta.payload);
		if (version !== 0) mirror.version = version;
		if (mirror.version > 0) scheduleStreamAck(message.id, mirror.version);
		if (structuralGap) {
			// The op targeted a block this mirror doesn't have (or has with the
			// wrong type) — e.g. an append whose block_open never applied here, so
			// its type had to be GUESSED as 'text'. Latch incoherence: the cache
			// is purged and stays unwritten, and every delta below re-arms a heal
			// until an authoritative snapshot actually ADOPTS.
			mirror.needsHeal = true;
			clearStreamCache(message.id);
		}
		if (mirror.needsHeal && !mirror.snapshotting && Date.now() - mirror.lastHealRequestAt > 1000) {
			// Heal from the authoritative snapshot: heal-mode ADOPTS even when
			// this (corrupted) mirror is AHEAD of the server's snapshot cadence —
			// adopt-only-if-newer used to refuse exactly the snapshot that carried
			// the fix — and then replays the tail forward from the adopted
			// version. Debounced re-arm: a heal fetch that FAILED (offline blip at
			// that instant) used to leave the fabricated block in place for the
			// rest of the stream; now the next delta simply tries again.
			chatStreamDebug('[chat-stream] mirror incoherent — healing from snapshot', {
				messageId: message.id,
				op,
				version
			});
			void requestStreamSnapshot(message.id, chatId, { force: true, heal: true });
		}

		const payload = delta.payload || {};
		if (op === 'sources' && Array.isArray(payload.sources)) {
			message.sources = payload.sources;
		} else if (op === 'selected_model_id' && payload.model_id) {
			message.selectedModelId = payload.model_id;
			message.arena = true;
		} else if (op === 'usage' && payload.usage) {
			message.usage = payload.usage;
			applyUsageToChatTokenStats(chatId, message.id, payload.usage);
			chatTokenStatsRefreshTrigger.update((n) => n + 1);
		}

		writeMirrorToMessage(mirror, message);
		history.messages[message.id] = message;
		scheduleStreamingMessageFlush(message.id, { runTTS: false, ownerId: message.id });
		scheduleStreamCacheWrite(message.id, chatId);
		streamPerfEnd(`chat.delta.${op || 'unknown'}`, perf);
	};

	const toolCallResultHandler = (
		data: {
			message_id?: string;
			tool_call_id?: string;
			result?: any;
			result_ref?: string;
			result_lazy?: boolean;
			size?: number;
			sha256?: string;
			summary?: any;
			files?: any[];
			embeds?: any[];
			subagent_id?: string;
			error?: boolean;
			error_reason?: string;
			notice?: string;
		},
		message: any
	) => {
		if (isUserStoppedMessageId(message.id)) {
			cancelStreamingMessageFlush(message.id);
			message.done = true;
			releaseStreamMirror(message.id);
			history.messages[message.id] = message;
			history = { ...history };
			return;
		}

		const perf = streamPerfStart();
		if (!data?.tool_call_id) return;
		const mirror = getOrCreateStreamMirror(message.id);
		// Run gate (same as chatDeltaHandler): a tool result from a SUPERSEDED
		// run — a late emit racing a retry/continue that already restarted this
		// message id — must not graft its body onto the new run's blocks. A
		// NEWER run resets the mirror and proceeds (the result belongs to it).
		const trRun = typeof (data as any)?.run === 'number' ? (data as any).run : 0;
		if (reconcileMirrorRun(mirror, trRun) === 'stale') {
			streamPerfEnd('chat.tool_result.stale_run', perf);
			return;
		}
		const resultEntry = normalizeToolResultEntry(data.tool_call_id, {
			tool_call_id: data.tool_call_id,
			content: data.result ?? '',
			...(data.result_ref ? { result_ref: data.result_ref } : {}),
			...(data.result_lazy ? { result_lazy: true } : {}),
			...(typeof data.size === 'number' ? { size: data.size } : {}),
			...(data.sha256 ? { sha256: data.sha256 } : {}),
			...(data.summary ? { summary: data.summary } : {}),
			...(Array.isArray(data.files) && data.files.length > 0 ? { files: data.files } : {}),
			...(Array.isArray(data.embeds) && data.embeds.length > 0 ? { embeds: data.embeds } : {}),
			...(data.subagent_id ? { subagent_id: data.subagent_id } : {}),
			...(data.error ? { error: true } : {}),
			...(data.error_reason ? { error_reason: data.error_reason } : {}),
			...(data.notice ? { notice: data.notice } : {})
		});
		mirror.tool_results.set(data.tool_call_id, resultEntry);
		if (Array.isArray(data.files) && data.files.length > 0) {
			message.files = mergeMessageFiles(message.files ?? [], data.files);
		}
		if (Array.isArray(data.embeds) && data.embeds.length > 0) {
			const seen = new Set(message.embeds ?? []);
			message.embeds = [
				...(message.embeds ?? []),
				...data.embeds.filter((embed) => !seen.has(embed))
			];
		}
		// Inline the tool result into the matching tool_calls block so the
		// renderer keeps working unchanged. blocksToDisplayMarkdown reads
		// block.results[], while some live components also look at tc.result.
		// Only the block that actually CONTAINS this tool_call_id should get the
		// result — scan from the end (newest first) and stop at the first match.
		// The previous code looped every block and pushed the result into each
		// tool_calls block, which both leaked the result into unrelated blocks
		// and was O(blocks) per result.
		const blocks = mirror.content_blocks;
		for (let bi = blocks.length - 1; bi >= 0; bi--) {
			const block = blocks[bi];
			if (block?.type !== 'tool_calls' || !Array.isArray(block.content)) continue;
			const ownsCall = block.content.some(
				(tc: any) => tc?.id === data.tool_call_id || tc?.tool_call_id === data.tool_call_id
			);
			if (!ownsCall) continue;
			const nextResults = Array.isArray(block.results) ? block.results.slice() : [];
			const mergedResult =
				mergeToolResultEntries([resultEntry], mirror.tool_results, nextResults)[0] ?? resultEntry;
			const existingIdx = nextResults.findIndex((r: any) => r?.tool_call_id === data.tool_call_id);
			if (existingIdx >= 0) nextResults[existingIdx] = mergedResult;
			else nextResults.push(mergedResult);
			block.results = nextResults;
			for (const tc of block.content) {
				if (tc?.id === data.tool_call_id || tc?.tool_call_id === data.tool_call_id) {
					tc.result = data.result;
				}
			}
			bumpStreamingBlockRevision(block);
			break;
		}
		writeMirrorToMessage(mirror, message);
		history.messages[message.id] = message;
		scheduleStreamingMessageFlush(message.id, { runTTS: false, ownerId: message.id });
		streamPerfEnd('chat.tool_result', perf);
	};

	const chatDoneHandler = async (
		data: {
			message_id?: string;
			version?: number;
			run?: number;
			usage?: any;
			updated_at?: number;
		},
		message: any,
		chatId: string | null
	) => {
		// Stop the live browser panel's timer (leave the last frame visible). The
		// poller already emits a terminal done frame per session when each browser
		// call ends, but on chat completion freeze the parent's "main" tab (and any
		// legacy message-id-keyed entry) as a belt-and-suspenders so the timer never
		// keeps ticking after the turn ends. Subagent tabs are frozen by their own
		// terminal frames.
		const _freezeKeys = [message?.id, 'main'].filter(Boolean) as string[];
		browserLiveStates.update((s) => {
			let changed = false;
			const next = { ...s };
			for (const k of _freezeKeys) {
				if (next[k] && !next[k].done) {
					next[k] = { ...next[k], done: true };
					changed = true;
				}
			}
			return changed ? next : s;
		});
		if (isUserStoppedMessageId(message.id)) {
			cancelStreamingMessageFlush(message.id);
			message.done = true;
			releaseStreamMirror(message.id);
			history.messages[message.id] = message;
			history = { ...history };
			return;
		}

		const mirror = getOrCreateStreamMirror(message.id);
		// A chat:done from a SUPERSEDED run (it raced a retry/continue that
		// already restarted this message id) must not finalize the LIVE run —
		// it would mark the streaming message done and tear down its mirror
		// mid-flight. A done from a NEWER run resets the mirror (reconcile)
		// and proceeds; the terminal snapshot below then adopts the final state.
		const doneRun = typeof data?.run === 'number' ? data.run : 0;
		if (reconcileMirrorRun(mirror, doneRun) === 'stale') {
			chatStreamDebug('[chat-stream] dropping stale-run chat:done', {
				messageId: message.id,
				doneRun,
				mirrorRun: mirror.run
			});
			return;
		}
		const shouldFetchTerminalSnapshot =
			!!chatId || (typeof data?.version === 'number' && data.version > mirror.version + 1);

		if (shouldFetchTerminalSnapshot) {
			// heal: the terminal snapshot is SERVER TRUTH — it must be adopted
			// even when this mirror's version is ahead of the snapshot stamp
			// (any lingering mid-stream corruption dies at the finish line).
			// The empty-over-content guard inside adoption still protects a
			// finished answer from an empty DB-fallback body.
			await requestStreamSnapshot(message.id, chatId, { force: true, heal: true });
		}
		if (typeof data?.version === 'number' && data.version > 0) {
			scheduleStreamAck(message.id, data.version);
			flushStreamAcks();
		}
		// Never let an EMPTY mirror wipe real rendered content at terminal — the
		// mirror can be legitimately empty here if a run-advance reset it and the
		// terminal snapshot fetch then failed (offline blip at the finish line).
		// The persisted row is authoritative in that case; the resume-poll /
		// reload paths reconcile it.
		if (
			mirror.content_blocks.length > 0 ||
			!Array.isArray(message.content_blocks) ||
			message.content_blocks.length === 0
		) {
			writeMirrorToMessage(mirror, message);
		}
		writeStreamCache(message.id, chatId);
		// Terminal convergence backstop: the server says this turn produced
		// content (final_content_hash != empty-sha / completion tokens > 0) but
		// this tab ended up with NONE — every reconcile above failed (e.g. the
		// terminal snapshot raced state cleanup on a flaky link). Reload once
		// from the authoritative row instead of stranding an empty bubble with
		// action buttons (the reported "answer disappeared at done" state).
		const EMPTY_SHA256 = 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855';
		const finalHash =
			typeof (data as any)?.final_content_hash === 'string' ? (data as any).final_content_hash : '';
		const completionTokens = Number(
			data?.usage?.completion_tokens ?? message?.usage?.completion_tokens ?? 0
		);
		const messageHasBody =
			(Array.isArray(message.content_blocks) &&
				message.content_blocks.some(
					(b: any) =>
						b &&
						(typeof b.content === 'string'
							? b.content.trim().length > 0
							: Array.isArray(b.content)
								? b.content.length > 0
								: b.type !== 'text' && b.type !== 'reasoning')
				)) ||
			(typeof message.content === 'string' && message.content.trim().length > 0);
		if (
			!messageHasBody &&
			((finalHash && finalHash !== EMPTY_SHA256) || completionTokens > 0) &&
			chatId &&
			!_emptyDoneReloadedIds.has(message.id)
		) {
			_emptyDoneReloadedIds.add(message.id);
			chatStreamDebug(
				'[chat-stream] empty message at terminal but server has content — reloading',
				{
					messageId: message.id,
					finalHash,
					completionTokens
				}
			);
			// The reload converges once — but the same blip that ate the terminal
			// events can eat the reload too, and chat:done never re-fires, so a
			// failed one-shot used to strand the empty bubble until a manual
			// reload. Retry up to 3 ACTUAL attempts; while provably offline the
			// timer just re-arms (zero network cost) and the attempt spends only
			// when connectivity is back. Aborts as soon as the message has a body
			// (another path converged) or the user navigated away.
			const _emptyDoneRetry = (attempt: number) => {
				void loadChat()
					.then((res) => {
						if (res) return;
						_emptyDoneReloadedIds.delete(message.id);
						_scheduleEmptyDoneRetry(attempt);
					})
					.catch(() => {
						_emptyDoneReloadedIds.delete(message.id);
						_scheduleEmptyDoneRetry(attempt);
					});
			};
			const _scheduleEmptyDoneRetry = (attempt: number) => {
				if (attempt >= 3) return;
				setTimeout(() => {
					if (getVisibleChatId() !== chatId) return;
					const m = history.messages[message.id];
					const converged =
						m &&
						((Array.isArray(m.content_blocks) && m.content_blocks.length > 0) ||
							(typeof m.content === 'string' && m.content.trim().length > 0));
					if (converged) return;
					if (!$online && !$socket?.connected) {
						// Still in the tunnel — hold this attempt for when it can succeed.
						_scheduleEmptyDoneRetry(attempt);
						return;
					}
					_emptyDoneReloadedIds.add(message.id);
					_emptyDoneRetry(attempt + 1);
				}, 3000);
			};
			_emptyDoneRetry(0);
		}
		const generatedFiles = (message.files ?? []).filter((file: any) => file?.container_workspace);
		if (generatedFiles.length > 0) {
			openGeneratedFilePreview(generatedFiles);
		}
		if (data?.usage) {
			message.usage = data.usage;
			applyUsageToChatTokenStats(chatId, message.id, data.usage);
		}
		message = { ...message };
		emitPendingTTSParts(message, { done: true });
		cancelStreamingMessageFlush(message.id);
		message.done = true;
		streamMirrors.delete(message.id);
		// Backstop: flip any subagent card still 'running' on normal completion
		// (its own terminal event was missed) so it stops a runaway clock.
		flipRunningSubagentsTerminal(message?.id ?? null);

		if ($settings?.responseAutoCopy) {
			copyToClipboard(message.content || '');
		}
		if ($settings?.responseAutoPlayback && !$showCallOverlay) {
			await tick();
			document.getElementById(`speak-button-${message.id}`)?.click();
		}

		eventTarget.dispatchEvent(
			new CustomEvent('chat:finish', {
				detail: { id: message.id, content: message.content }
			})
		);

		history.messages[message.id] = message;
		history = { ...history };

		await tick();
		if (autoScroll) scrollToBottom();

		if (message.usage) {
			chatTokenStatsRefreshTrigger.update((n) => n + 1);
		}
		if (chatId) {
			// `chat:updated` socket broadcasts skip the originating tab, so we
			// patch the sidebar locally using the `updated_at` shipped with the
			// chat:done payload. Falls back to a wall-clock approximation if
			// the backend didn't include it (legacy / pre-fix servers).
			const ts =
				typeof data?.updated_at === 'number' ? data.updated_at : Math.floor(Date.now() / 1000);
			patchSidebarUpdatedAt(chatId, ts);
			// Track the chat's authoritative updated_at so a reconnect can tell whether
			// the chat changed while we were disconnected — including an in-place Continue
			// Response that reuses the SAME assistant id (so the leaf-id reconcile can't
			// see it). Only advance it (never regress) for the currently-viewed chat.
			if (chat && getVisibleChatId() === chatId && ts > (chat.updated_at ?? 0)) {
				chat.updated_at = ts;
			}
			invalidateFolderChatLists([data?.folder_id, chat?.folder_id], 'chat:done:origin');

			await chatCompletedHandler(
				chatId,
				message.model,
				message.id,
				createMessagesList(history, message.id)
			);
		}
	};

	//////////////////////////
	// Chat functions
	//////////////////////////

	let offlineDraftToastShown = $state(false);

	const blockParentGenerationDuringSubagentRerun = (): boolean => {
		if (!hasActiveDetachedSubagentRerun(get(subagentLiveStates))) return false;
		toast.error(
			$i18n.t('Wait for the active subagent redo to finish before continuing the main chat.')
		);
		return true;
	};

	type DeferredUploadFile = {
		itemId?: string;
		id?: string;
		ref: any;
	};

	const fileIsInFlight = (file: any) =>
		file?.status === 'uploading' || file?.status === 'processing';

	const sameDeferredUploadFile = (tracked: DeferredUploadFile, file: any) => {
		if (tracked.itemId) return file?.itemId === tracked.itemId;
		if (tracked.id) return file?.id === tracked.id;
		return file === tracked.ref;
	};

	const waitForAttachedFiles = async (token: number): Promise<'ready' | 'failed' | 'cancelled'> => {
		const tracked: DeferredUploadFile[] = [];

		while (token === deferredUploadSubmitToken) {
			for (const file of files.filter(fileIsInFlight)) {
				if (tracked.some((candidate) => sameDeferredUploadFile(candidate, file))) continue;
				tracked.push({
					itemId: file?.itemId,
					id: file?.id,
					ref: file
				});
			}

			for (const candidate of tracked) {
				const current = files.find((file) => sameDeferredUploadFile(candidate, file));
				// Upload failures and explicit removal both remove the placeholder
				// from `files`. Never auto-send the remaining attachments as though
				// the missing file had succeeded.
				if (!current || current?.status === 'failed') return 'failed';
			}

			if (!files.some(fileIsInFlight)) return 'ready';
			await new Promise((resolve) => setTimeout(resolve, 150));
		}

		return 'cancelled';
	};

	const submitPrompt = async (userPrompt, { _raw = false } = {}) => {
		console.log('submitPrompt', userPrompt, getVisibleChatId());

		// Offline gating: both conditions must hold — an OS false-negative on
		// navigator.onLine while the socket is actually still connected must
		// NOT block sending. Leave the input untouched (don't clear prompt/files
		// below, just bail out early) so the text survives as a draft exactly
		// like any other unsent input (chat-input localStorage persistence is
		// unaffected since we never touch it here).
		if (!$online && !$socket?.connected) {
			if (!offlineDraftToastShown) {
				offlineDraftToastShown = true;
				toast.info($i18n.t("You're offline — message kept as draft."));
			}
			return;
		}

		// Local-first open: a provisional (local-copy) view may still be
		// revalidating against the network. A send must branch from the REAL
		// current leaf — wait out the (sub-second) revalidation so the parent id
		// can't point at a stale currentId. The prompt text is untouched while
		// waiting; navigation away aborts via the generation checks downstream.
		if (chatRevalidationPromise) {
			try {
				await chatRevalidationPromise;
			} catch {
				// revalidation failures fall back to a full reload elsewhere
			}
		}
		if (blockParentGenerationDuringSubagentRerun()) return;

		// `/compact` is a command, not a prompt. This is the idle path (the
		// composer dispatches `steer` while a turn is working, and the backend
		// recognizes the command at the steer boundary / queue drain), so run the
		// cut now instead of sending anything.
		if (isCompactCommand(userPrompt)) {
			await runCompactCommand();
			return;
		}

		// A new turn may start a fresh browser session; allow the panel to auto-open
		// again even if the user dismissed it on a previous turn.
		browserPanelDismissed.set(false);

		const _selectedModels = selectedModels.map((modelId) =>
			$models.map((m) => m.id).includes(modelId) ? modelId : ''
		);

		if (!arraysEqual(selectedModels, _selectedModels)) {
			selectedModels = _selectedModels;
		}

		if (userPrompt === '' && files.length === 0) {
			toast.error($i18n.t('Please enter a prompt'));
			return;
		}
		if (selectedModels.includes('')) {
			toast.error($i18n.t('Model not selected'));
			return;
		}
		const submittedModelIds = snapshotTurnModelIds({
			mentionedModelId: atSelectedModel?.id,
			selectedModelIds: selectedModels
		});
		if (
			submittedModelIds.length === 0 ||
			submittedModelIds.some((modelId) => !$models.some((model) => model.id === modelId))
		) {
			toast.error($i18n.t('Model not selected'));
			return;
		}

		if (
			files.length > 0 &&
			files.filter((file) => file.status === 'uploading' || file.status === 'processing').length > 0
		) {
			const inFlightFiles = files.filter(
				(file) => file.status === 'uploading' || file.status === 'processing'
			);
			if (deferredUploadSubmit) {
				toast.info($i18n.t('Your message is already waiting for its files to finish.'));
				return;
			}

			const token = ++deferredUploadSubmitToken;
			const promptAtDeferral = prompt;
			deferredUploadSubmit = { token };
			toast.info(
				$i18n.t('Your message will send automatically when {{count}} file(s) finish uploading.', {
					count: inFlightFiles.length
				})
			);

			const uploadResult = await waitForAttachedFiles(token);
			if (deferredUploadSubmit?.token === token) deferredUploadSubmit = null;
			if (uploadResult === 'cancelled') return;
			if (uploadResult === 'failed') {
				toast.error(
					$i18n.t(
						'A file failed to finish uploading or was removed. Your message is still in the composer.'
					)
				);
				return;
			}

			// Include edits made while the upload was finishing. Programmatic
			// submits (voice/call/etc.) may not mirror their text into `prompt`,
			// so retain the originally submitted value when the composer itself
			// did not change.
			const readyPrompt =
				prompt !== promptAtDeferral ? prompt.replaceAll('\n\n', '\n') : userPrompt;
			return await submitPrompt(readyPrompt, { _raw });
		}

		if (files.some((file) => file?.status === 'failed')) {
			toast.error($i18n.t('Remove or retry failed file attachments before sending your message.'));
			return;
		}

		if (
			($config?.file?.max_count ?? null) !== null &&
			files.length + chatFiles.length > $config?.file?.max_count
		) {
			toast.error(
				$i18n.t(`You can only chat with a maximum of {{maxCount}} file(s) at a time.`, {
					maxCount: $config?.file?.max_count
				})
			);
			return;
		}

		if (history?.currentId) {
			const lastMessage = history.messages[history.currentId];
			// PROGRAMMATIC submits (speech auto-send, suggestion click, postMessage,
			// call overlay) bypass MessageInput's keydown gate, so they need the same
			// liveness check here or they start a SECOND concurrent generation on the
			// same chat (C27). Route any submit during a live turn into the queue.
			if (turnLive) {
				// Response still streaming — instead of dropping the submit on
				// the floor, queue it. dequeueAndSend() will fire as soon as the
				// response naturally completes (falling-edge reactive below).
				await enqueueMessage(userPrompt);
				return;
			}

			if (lastMessage.error && !lastMessage.content) {
				// Error in response
				toast.error($i18n.t(`Oops! There was an error in the previous response.`));
				return;
			}
		}

		messageInput?.setText('');
		prompt = '';

		const messages = createMessagesList(history, history.currentId);
		const containerFeatures = ($config as any)?.features ?? {};
		const containerToolId = containerFeatures?.container_mcp_server_id
			? `server:mcp:${containerFeatures.container_mcp_server_id}`
			: '';
		const containerWorkspaceActive = Boolean(
			containerFeatures?.enable_container_workspace_sync &&
				containerToolId &&
				(selectedToolIds ?? []).includes(containerToolId)
		);
		const _files = cloneState(files).map((file) =>
			containerWorkspaceActive ? { ...file, container_mode: true } : file
		);

		chatFiles.push(
			..._files.filter((item) =>
				['doc', 'text', 'file', 'note', 'chat', 'folder', 'collection'].includes(item.type)
			)
		);
		chatFiles = chatFiles.filter(
			// Remove duplicates
			(item, index, array) =>
				array.findIndex((i) => JSON.stringify(i) === JSON.stringify(item)) === index
		);

		files = [];
		messageInput?.setText('');

		// Create user message
		let userMessageId = uuidv4();
		let userMessage = {
			id: userMessageId,
			parentId: messages.length !== 0 ? messages.at(-1).id : null,
			childrenIds: [],
			role: 'user',
			content: userPrompt,
			files: _files.length > 0 ? _files : undefined,
			timestamp: Math.floor(Date.now() / 1000), // Unix epoch
			models: submittedModelIds
		};

		// Add message to history and Set currentId to messageId
		history.messages[userMessageId] = userMessage;
		history.currentId = userMessageId;

		// Append messageId to childrenIds of parent message
		if (messages.length !== 0) {
			history.messages[messages.at(-1).id].childrenIds.push(userMessageId);
		}

		const chatInput = document.getElementById('chat-input');
		if ($mobile) {
			chatInput?.blur();
		} else {
			chatInput?.focus({ preventScroll: true });
		}

		saveSessionSelectedModels();

		// 'preserve': sending must never yank a reader who has scrolled up back
		// to the bottom. The bottom-pin only applies when the user is already
		// following the bottom (autoScroll true, e.g. initNewChat sets this for
		// a fresh chat) — 'preserve' honors that instead of forcing it.
		await sendMessage(history, userMessageId, {
			modelIds: submittedModelIds,
			newChat: true,
			scrollBehavior: 'preserve'
		});
	};

	const sendMessage = async (
		_history,
		parentId: string,
		{
			messages = null,
			modelId = null,
			modelIds = null,
			modelIdx = null,
			newChat = false,
			supersedeActiveTurn = false,
			// Default to 'preserve': a reader who has scrolled up must never be
			// moved without an explicit reason. Callers that intend a hard
			// force-to-bottom must pass scrollBehavior: 'engage' explicitly.
			scrollBehavior = 'preserve'
		}: {
			messages?: any[] | null;
			modelId?: string | null;
			modelIds?: string[] | null;
			modelIdx?: number | null;
			newChat?: boolean;
			supersedeActiveTurn?: boolean;
			scrollBehavior?: 'engage' | 'preserve';
		} = {}
	) => {
		if (blockParentGenerationDuringSubagentRerun()) return;
		if (autoScroll) {
			scrollToBottom();
		}

		let _chatId = getVisibleChatId();
		_history = cloneState(_history);

		const syncHistorySnapshot = () => {
			history = cloneState(_history);
		};
		syncHistorySnapshot();
		// Note: do NOT clear _completedMessageIds here. The set's purpose is to
		// dedupe completion-handler invocations per message id; uuids never
		// collide, so clearing it just creates a window where a delayed
		// completion event for a previous message can re-trigger
		// chatCompletedHandler and clobber the in-flight request's state.

		const mirrorHistoryMessage = (messageId) => {
			const nextMessage = _history.messages[messageId];
			if (!nextMessage) {
				return;
			}
			history.messages[messageId] = cloneState(nextMessage);
			history = { ...history };
		};

		const responseMessageIds: Record<PropertyKey, string> = {};
		// Resolve the turn's model identity exactly once. The returned array is a
		// copy, so a picker change or chat revalidation during placeholder
		// persistence cannot alter this turn after it has started.
		const selectedModelIds = snapshotTurnModelIds({
			explicitModelIds: modelIds,
			explicitModelId: modelId,
			mentionedModelId: atSelectedModel?.id,
			selectedModelIds: selectedModels
		});
		const chatModelIdsAtTurnStart = [...selectedModels];
		// One send attempt may fan out to several model siblings. They share this
		// cancellation/ownership turn id, while a later manual regenerate gets a
		// fresh one even when it reuses the same user parent message.
		const turnId = uuidv4();

		// Create response messages for each selected model
		for (const [_modelIdx, modelId] of selectedModelIds.entries()) {
			const model = $models.filter((m) => m.id === modelId).at(0);

			if (model) {
				let responseMessageId = uuidv4();
				const generationId = uuidv4();
				let responseMessage = {
					parentId: parentId,
					id: responseMessageId,
					childrenIds: [],
					role: 'assistant',
					content: '',
					generation_id: generationId,
					turn_id: turnId,
					model: model.id,
					modelName: model.name ?? model.id,
					modelIdx: modelIdx ? modelIdx : _modelIdx,
					timestamp: Math.floor(Date.now() / 1000) // Unix epoch
				};

				// Add message to history and Set currentId to messageId
				history.messages[responseMessageId] = responseMessage;
				history.currentId = responseMessageId;

				// Append messageId to childrenIds of parent message
				if (parentId !== null && history.messages[parentId]) {
					// Add null check before accessing childrenIds
					history.messages[parentId].childrenIds = [
						...history.messages[parentId].childrenIds,
						responseMessageId
					];
				}

				responseMessageIds[`${modelId}-${modelIdx ? modelIdx : _modelIdx}`] = responseMessageId;
			}
		}
		history = history;

		// Create new chat if newChat is true and first user message
		if (newChat && _history.messages[_history.currentId].parentId === null) {
			try {
				_chatId = await initChatHandler(_history);
			} catch (e) {
				// Chat creation is the one send step with no retry path: without a
				// persisted chat row the completion POST can only 404. It also runs
				// BEFORE the try/finally below, so an unhandled throw here used to
				// strand the tab (half-created turn, cleared composer). Unwind to a
				// clean draft instead — same UX as the submitPrompt offline gate. No
				// lifecycle record exists yet, so there is no generation to settle.
				console.error('initChatHandler failed — reverting send to draft', e);
				const draftText = _history.messages[parentId]?.content ?? '';
				for (const respId of Object.values(responseMessageIds)) {
					delete history.messages[respId];
				}
				if (history.messages[parentId]) {
					delete history.messages[parentId];
				}
				history.currentId = null;
				history = { ...history };
				bumpMessageStructure();
				if (draftText) {
					messageInput?.setText(draftText);
				}
				if (isNetworkFetchError(e)) {
					toast.info($i18n.t("You're offline — message kept as draft."));
				} else {
					toast.error(`${e}`);
				}
				return;
			}
		}

		// Cancellation belongs to the exact assistant generations that existed
		// when Stop was pressed. `localStop` deliberately remains latched for
		// retry/queue/stream teardown, but it is NOT authority over a later turn.
		// Using that chat-wide latch here made the common
		// Stop → edit user message → resend sequence cancel its brand-new
		// placeholders before prepareGenerationLifecycle could register them (and
		// clear the old latch). The abandoned placeholder was then persisted with
		// done unset and rendered as a pulsing cursor forever.
		//
		// There is no unaddressable window here: the placeholder ids are created
		// synchronously above, before this function's first await. A Stop that
		// lands during new-chat creation finds those exact rows in `history` and
		// markTurnStopped latches them by message/generation identity.
		const stoppedBeforePlaceholderSave = wasGenerationStartStopped(
			Object.values(responseMessageIds),
			(responseMessageId) => isUserStoppedMessageId(responseMessageId)
		);
		for (const respId of Object.values(responseMessageIds)) {
			const responseMessage = history.messages[respId];
			if (!responseMessage?.generation_id || !responseMessage?.turn_id) continue;
			if (stoppedBeforePlaceholderSave) {
				// Keep lifecycle and durable message state atomic. Calling the
				// registry directly here used to leave a legitimately immediate
				// Stop as an unfinished empty row even though its lifecycle was
				// already settled.
				markTurnStopped(respId, { chatId: _chatId });
			} else {
				prepareGenerationLifecycle(_chatId, responseMessage, {
					generationId: responseMessage.generation_id,
					turnId: responseMessage.turn_id
				});
			}
		}

		await tick();

		// Skip the structuredClone of `history` — saveChatHandler only reads it
		// to serialize a request body, and the LLM request is fired in parallel
		// below, so the save no longer gates the upstream call.
		_history = history;

		// Build append_message ops for the user message + each freshly-created
		// response message so the backend has a parented chain to attach stream
		// deltas to via realtime save. For a brand-new root chat, createNewChat
		// already persisted the user message, but it did NOT know about the
		// assistant placeholder created above; append the assistant rows here or
		// the first stream upsert will create orphan rows with role=""/no parent.
		const initialOps: PatchChatOp[] = [];
		// Persist the picker state in the same ordered mutation as the turn
		// skeleton. Reload can therefore never observe "new message, old chat
		// model" merely because the separate picker-effect PATCH lost a race.
		initialOps.push({
			op: 'set_models',
			models: cloneState(chatModelIdsAtTurnStart)
		});
		const isNewChatRootSend = newChat && _history.messages[parentId]?.parentId === null;
		if (!isNewChatRootSend) {
			const userMsg = _history.messages[parentId];
			if (userMsg && userMsg.role === 'user') {
				initialOps.push({
					op: 'append_message',
					message_id: userMsg.id,
					parent_id: userMsg.parentId ?? null,
					role: 'user',
					content: userMsg.content,
					...(userMsg.files !== undefined ? { files: userMsg.files } : {}),
					...(userMsg.models !== undefined ? { models: userMsg.models } : {}),
					timestamp: userMsg.timestamp
				});
			}
		}
		for (const respId of Object.values(responseMessageIds)) {
			const m = _history.messages[respId];
			if (!m) continue;
			initialOps.push({
				op: 'append_message',
				message_id: m.id,
				parent_id: m.parentId ?? null,
				role: 'assistant',
				content: m.content ?? '',
				model: m.model,
				modelName: m.modelName,
				modelIdx: m.modelIdx,
				generation_id: m.generation_id,
				turn_id: m.turn_id,
				...(m.done !== undefined ? { done: m.done } : {}),
				...(m.userStopped !== undefined ? { userStopped: m.userStopped } : {}),
				timestamp: m.timestamp
			});
		}
		if (_history.currentId) {
			initialOps.push({ op: 'set_history_current_id', current_id: _history.currentId });
		}

		const initialSavePromise = saveChatHandler(
			_chatId,
			_history,
			params,
			initialOps.length > 0 ? initialOps : null
		)
			.then(() => {
				rememberPersistedSelectedModels(_chatId, chatModelIdsAtTurnStart);
				// The picker remains usable while this ordered turn-skeleton PATCH
				// is in flight. If it changed, publish that newer intent after the
				// turn-start model write so completion order cannot restore the
				// earlier model.
				if (
					getVisibleChatId() === _chatId &&
					selectedModelsPersistKey(_chatId) !==
						selectedModelsPersistKey(_chatId, chatModelIdsAtTurnStart)
				) {
					persistSelectedModelsForChat();
				}
			})
			.catch((err) => {
				console.error('saveChatHandler failed:', err);
			});
		// Stream-v2.1 deltas are keyed to the assistant placeholder row. Make sure
		// that row exists (with parentId/role/model metadata) before the backend
		// starts realtime upserts, otherwise the stream can create an orphan row
		// and reloads lose the user/assistant branch relationship.
		if (!_chatId?.startsWith('local:') && !$temporaryChatEnabled) {
			await initialSavePromise;
		}

		try {
			// Stop pressed during the placeholder save: every response id for this
			// send is latched, so there is nothing left to request. Asking the
			// lifecycle registry directly (rather than reading a global flag) keeps
			// this scoped to THIS send — a Stop on some other branch can't abandon it.
			const sendIds = Object.values(responseMessageIds);
			if (
				sendIds.length > 0 &&
				sendIds.every(
					(respId) => generationLifecycles.isStopped(respId) || isUserStoppedMessageId(respId)
				)
			) {
				return;
			}

			await Promise.all(
				selectedModelIds.map(async (modelId, _modelIdx) => {
					console.log('modelId', modelId);
					const model = $models.filter((m) => m.id === modelId).at(0);

					if (model) {
						// If there are image files, check if model is vision capable
						const hasImages = createMessagesList(_history, parentId).some((message) =>
							message.files?.some((file) => file.type === 'image')
						);

						const hasNativeVision = model.info?.meta?.capabilities?.vision ?? true;
						const hasPreprocessor = !!model.info?.meta?.vision_preprocessor_model_id;

						if (hasImages && !hasNativeVision && !hasPreprocessor) {
							toast.error(
								$i18n.t('Model {{modelName}} is not vision capable', {
									modelName: model.name ?? model.id
								})
							);
						}

						let responseMessageId =
							responseMessageIds[`${modelId}-${modelIdx ? modelIdx : _modelIdx}`];

						// Vision/PDF preprocessing for non-vision models now runs
						// server-side in assemble_conversation_from_leaf, so it works
						// identically for normal sends and the zero-tab queue drain.

						// `engage` (a brand-new user submit) forces the view to the
						// bottom to watch the new turn. `preserve` (retry / regenerate /
						// rewind / continue / edit-resend of an EARLIER turn) respects the
						// reader's gesture intent — follow only if already at the bottom —
						// so acting on a message while scrolled up no longer yanks the view.
						if (scrollBehavior === 'preserve') {
							if (autoScroll) scrollToBottom();
						} else {
							engageAndScrollToBottom();
						}

						const MAX_NO_PROGRESS = 5;
						// Give up only after MAX_NO_PROGRESS *consecutive* failures that
						// made NO forward progress (no new completed tool call). Each time
						// the agent advances, the counter resets — a turn that keeps doing
						// real work is never cut off at an arbitrary total. ABSOLUTE_RETRY_CEILING
						// is a pure runaway guard so a pathological "one tool call then error,
						// forever" loop can't hang the tab; a real turn never approaches it.
						const ABSOLUTE_RETRY_CEILING = 100;
						let retryCancelled = false;
						let savedToolContent = null;
						let savedReasoningDetails = null;
						let savedReasoningDetailsPerRound = null;
						// v2.1 turns keep their tool history in content_blocks (content is a
						// text-only projection, so the legacy getRetryableToolContext parser
						// never fires for them). Once a failed attempt has delivered ANY
						// blocks, the server row holds the partial turn — latch that so the
						// retry pins the assembly leaf to this message and the backend
						// continues the turn in place instead of restarting it.
						let structuredDelivered = false;
						let consecutiveNoProgress = 0;
						let lastCompletedToolCalls = -1;
						// Minted with the placeholder and stable across every
						// transport/provider retry. Stop can therefore latch this
						// operation even while the initial placeholder save is in flight.
						const generationId = _history.messages[responseMessageId].generation_id;

						// Stop must end this loop wherever it is — including BETWEEN
						// attempts, where no backend task exists and the only evidence of
						// the user's intent is the local latch. The user-stopped set is
						// read alongside the lifecycle phase because a previous attempt's
						// terminal event can settle the record before the countdown even
						// starts.
						const retryStopped = () =>
							generationLifecycles.isStopped(responseMessageId, generationId) ||
							isUserStoppedMessageId(responseMessageId);
						// A NEWER generation took over this message id (continue / rewind /
						// regenerate reuse it). This loop must get out of the way, but must
						// NOT touch the message — that state now belongs to the new run.
						const retrySuperseded = () =>
							!generationLifecycles.isCurrent(responseMessageId, generationId);
						const retryShouldExit = () => retryStopped() || retrySuperseded();
						// Publish the terminal Stop state on the LIVE message AND on this
						// loop's own detached snapshot — writing only one of them is what
						// let a countdown tick (or the defensive cleanup below) re-publish
						// the pre-Stop `retrying`/`done:false` state over the cancel.
						const finalizeStoppedRetry = () => {
							if (retrySuperseded() || !retryStopped()) return;
							markTurnStopped(responseMessageId, {
								maps: [history.messages, _history.messages],
								chatId: _chatId
							});
							const live = history.messages[responseMessageId];
							if (live) {
								history.messages[responseMessageId] = { ...live };
								history = { ...history };
							}
						};

						activeSendRetryLoops++;
						try {
							for (let attempt = 1; attempt <= ABSOLUTE_RETRY_CEILING; attempt++) {
								let responseMessage = _history.messages[responseMessageId];

								if (attempt > 1) {
									// Re-arm the generation: the failed attempt settled its record
									// terminal, but this loop still owns the turn. (Stop wins —
									// `retry` refuses a stopped record.)
									generationLifecycles.retry(responseMessageId, generationId);

									// Preserve tool context so retry continues from where it left off
									if (savedToolContent) {
										responseMessage.content = savedToolContent;
										responseMessage.preservedToolContext = true;
										if (savedReasoningDetails) {
											responseMessage.reasoning_details = savedReasoningDetails;
										}
										if (savedReasoningDetailsPerRound) {
											responseMessage.reasoning_details_per_round = savedReasoningDetailsPerRound;
										}
									} else if (!structuredDelivered) {
										// Structured turns keep their partial history in
										// content_blocks — wiping content here is pointless (it's a
										// projection) and blanks the visible partial turn.
										responseMessage.content = '';
									}

									responseMessage.error = null;
									responseMessage.done = false;
									responseMessage.retrying = null;
									_history.messages[responseMessageId] = responseMessage;
									mirrorHistoryMessage(responseMessageId);
								}

								// Suppress the error toast while another retry is still possible;
								// show it only when the next no-progress failure would exhaust
								// the consecutive cap.
								suppressErrorToast = consecutiveNoProgress + 1 < MAX_NO_PROGRESS;

								await sendMessageSocket(
									model,
									messages && messages.length > 0
										? messages
										: createMessagesList(_history, responseMessageId),
									_history,
									responseMessageId,
									_chatId,
									{
										scrollBehavior,
										generationId,
										turnId,
										supersedeActiveTurn,
										// A retry (attempt > 1) reuses this SAME assistant message id,
										// which may already carry accumulated content_blocks (partial
										// tool history from before the failure). sendMessageSocket's
										// `leafMessageId` defaults to responseMessage.parentId — correct
										// for attempt 1 (the message is brand new and empty), but WRONG
										// here: under the v2.1 body the backend reconstructs the entire
										// outbound history by walking chat_message rows from leaf_message_id
										// and does not look at the client's `messages` array at all, so
										// defaulting to parentId excludes this assistant message —
										// and everything in its content_blocks — from the resend. That
										// was the root cause of a poisoned/errored turn's retry collapsing
										// to just system+user (losing all prior tool-call history). Pin
										// the leaf to responseMessageId itself so the backend's walk
										// includes it — but ONLY when a prior attempt actually DELIVERED
										// content (legacy savedToolContent, or any streamed
										// content_blocks — the v2.1 write-through means delivered blocks
										// always have a server-side row): a network-failed first delivery
										// has no server-side assistant row at all, and pinning the leaf
										// to that nonexistent id made assembly manufacture a role-less
										// orphan row (broken tree, response invisible after reload).
										// With the leaf pinned, the backend seeds the generation from the
										// row's blocks and CONTINUES the turn — tool calls and reasoning
										// accumulated before the error are kept, not re-run.
										...(attempt > 1 && (savedToolContent || structuredDelivered)
											? { leafMessageId: responseMessageId }
											: {})
									}
								);
								if (getVisibleChatId() !== _chatId) {
									generationLifecycles.terminal(responseMessageId, generationId);
									retryCancelled = true;
									break;
								}

								// Wait for response to actually complete (handles socket-based delivery
								// where sendMessageSocket returns before the response arrives)
								{
									const msg = history.messages[responseMessageId];
									if (!msg?.done && !msg?.error) {
										let lastKnownActiveAt = Date.now();
										let lastPollAt = 0;
										while (true) {
											await new Promise((r) => setTimeout(r, 250));
											if (getVisibleChatId() !== _chatId) {
												generationLifecycles.terminal(responseMessageId, generationId);
												break;
											}
											// Stop is terminal and immediate: don't sit here waiting for a
											// done/error that the cancelled run will never deliver (that was
											// a 12s "still generating" stall after every Stop that landed
											// while this wait owned the turn).
											if (retryShouldExit()) {
												finalizeStoppedRetry();
												break;
											}
											const m = history.messages[responseMessageId];
											if (m?.done || m?.error) break;

											if (_chatId && Date.now() - lastPollAt > 3000) {
												lastPollAt = Date.now();
												const [taskRes, activeStreams] = await Promise.all([
													getChatWorkState(localStorage.token, _chatId).catch(() => null),
													getActiveStreamsByChatId(localStorage.token, _chatId).catch(() => null)
												]);
												// FAILED probes (offline / server unreachable) are
												// INCONCLUSIVE, not "nothing is running": during a network
												// blip both fetches reject, and 12s of that used to make
												// this tab declare the request dead and paint an error over
												// a generation that was streaming fine server-side. Only a
												// probe that actually ANSWERED "no active task, no active
												// stream" may advance the inactivity clock.
												if (taskRes === null && activeStreams === null) {
													lastKnownActiveAt = Date.now();
													continue;
												}
												const liveGenerations = generationLifecycles.reconcileServerOperations(
													taskRes?.generations,
													navigateGeneration
												);
												const hasActiveTask = liveGenerations.some((operation) =>
													Boolean(operation.task_id)
												);
												const hasPendingGeneration = liveGenerations.some(
													(operation) =>
														!operation.task_id && operation.message_id === responseMessageId
												);
												const hasActiveStream = (activeStreams?.streams ?? []).some(
													(stream) => stream?.message_id === responseMessageId
												);

												if (hasActiveTask || hasPendingGeneration || hasActiveStream) {
													lastKnownActiveAt = Date.now();
												} else if (Date.now() - lastKnownActiveAt > 12000) {
													// No active task/stream for 12s: the generation ended. It
													// may have COMPLETED with a lost terminal `chat:done` event
													// (reconnect / stale session_id / navigation), so reconcile
													// from the snapshot before erroring — done/cancelled resolve
													// to done, a real failure resolves to error. Erroring here
													// unconditionally was the primary "sent but no response" bug.
													await requestStreamSnapshot(responseMessageId, _chatId, {
														force: true
													}).catch(() => undefined);
													const reconciled = history.messages[responseMessageId];
													if (reconciled?.done || reconciled?.error) {
														break;
													}
													// A user Stop resolves to done, never to a retryable
													// error — even if the terminal write raced/failed
													// backend-side, "the user stopped this" is authoritative.
													if (isUserStoppedMessageId(responseMessageId)) {
														if (reconciled) {
															reconciled.done = true;
															history.messages[responseMessageId] = reconciled;
															history = { ...history };
														}
														break;
													}
													await handleOpenAIError(
														{ message: 'Chat request is not active on the backend.' },
														reconciled ?? m,
														'response-wait-no-active-task'
													);
													break;
												}
											}
										}
									}
								}
								if (getVisibleChatId() !== _chatId) {
									retryCancelled = true;
									break;
								}

								suppressErrorToast = false;

								// Sync from reactive history back to _history (socket handler writes to history, not _history)
								const completedMsg = history.messages[responseMessageId];
								if (completedMsg) {
									_history.messages[responseMessageId] = cloneState(completedMsg);
								}
								responseMessage = _history.messages[responseMessageId];

								if (!responseMessage.error) break;
								// A user Stop is terminal — never auto-retry it, even if a
								// late error event landed on the message after the cancel.
								if (retryShouldExit()) {
									finalizeStoppedRetry();
									break;
								}
								// Replaying an admission/identity failure with the same turn and
								// generation can never change the answer. In particular, five
								// automatic 409 retries only outlived the original turn and left
								// the replacement placeholder orphaned.
								if (isNonRetryableChatGenerationError(responseMessage.error)) break;

								// Save tool context from failed attempt for next retry (legacy v1
								// turns keep tool history as HTML markers in `content`).
								const failedToolContext = getRetryableToolContext(responseMessage.content);
								if (failedToolContext?.hasCompletedToolCall) {
									savedToolContent = failedToolContext.content;
									savedReasoningDetails = responseMessage.reasoning_details || null;
									savedReasoningDetailsPerRound =
										responseMessage.reasoning_details_per_round || null;
								}
								// v2.1 turns keep it in content_blocks instead — any delivered
								// blocks mean the server row carries the partial turn, so the
								// next attempt must pin the assembly leaf to this message.
								if (
									Array.isArray(responseMessage.content_blocks) &&
									responseMessage.content_blocks.length > 0
								) {
									structuredDelivered = true;
								}

								// A network-level failure (offline / connection dropped mid-POST)
								// is a connectivity event, not a model failure: it never burns
								// the no-progress budget (the ABSOLUTE_RETRY_CEILING still
								// bounds it) and its wait below holds while the socket is down,
								// so a train tunnel at send time RESUMES instead of hard-erroring
								// after a few blind attempts. Safe to re-send: the backend
								// dedupes by assistant message id, so if the lost POST actually
								// started a generation, the retry just re-attaches to it.
								const networkFailure = isNetworkFetchError(responseMessage.error);

								// Forward-progress check: if this failed attempt completed more
								// tool calls than the previous failure, the agent advanced — reset
								// the consecutive-failure counter so progress is never punished.
								// Structured blocks first (v2.1); legacy content parse otherwise.
								const completedNow =
									countCompletedStructuredToolCalls(responseMessage) ||
									countCompletedToolCalls(responseMessage.content);
								if (networkFailure) {
									// leave the budget untouched
								} else if (lastCompletedToolCalls >= 0 && completedNow > lastCompletedToolCalls) {
									consecutiveNoProgress = 0;
								} else {
									consecutiveNoProgress += 1;
								}
								lastCompletedToolCalls = completedNow;

								if (
									consecutiveNoProgress < MAX_NO_PROGRESS &&
									!skipRemainingRetriesSet.has(responseMessageId)
								) {
									const displayAttempt = Math.max(consecutiveNoProgress, 1);
									const waitSeconds = networkFailure ? 2 : Math.min(displayAttempt, 5) * 2;

									responseMessage.error = null;
									responseMessage.done = false;
									if (!structuredDelivered) {
										responseMessage.content = '';
									}
									responseMessage.retrying = {
										attempt: displayAttempt,
										maxAttempts: MAX_NO_PROGRESS,
										countdown: waitSeconds,
										...(networkFailure ? { reason: 'network' } : {})
									};
									_history.messages[responseMessageId] = responseMessage;
									mirrorHistoryMessage(responseMessageId);

									await new Promise((resolve) => {
										let remaining = waitSeconds;
										// Bound the connectivity hold so a wedged socket can't pin
										// the countdown forever — after this, attempt anyway (a
										// failed attempt just lands back here).
										let heldTicks = 0;
										const ticker = setInterval(() => {
											// Stop is checked FIRST, before the connectivity hold below.
											// The hold used to `return` ahead of every stop check, so a
											// Stop pressed during "Connection lost — will retry when it
											// returns…" was ignored for up to 300 ticks while the tick
											// kept re-publishing the not-done retrying state over it.
											if (retryShouldExit()) {
												clearInterval(ticker);
												resolve();
												return;
											}
											// Network-failure waits hold while we're provably
											// offline: retry the moment connectivity returns
											// instead of burning attempts into a dead link.
											const offline = !$online && !$socket?.connected;
											if (networkFailure && offline && heldTicks < 300) {
												heldTicks++;
												responseMessage.retrying = {
													attempt: displayAttempt,
													maxAttempts: MAX_NO_PROGRESS,
													countdown: remaining,
													reason: 'network'
												};
												_history.messages[responseMessageId] = responseMessage;
												mirrorHistoryMessage(responseMessageId);
												return;
											}
											remaining--;
											if (remaining <= 0) {
												clearInterval(ticker);
												resolve();
												return;
											}
											responseMessage.retrying = {
												attempt: displayAttempt,
												maxAttempts: MAX_NO_PROGRESS,
												countdown: remaining,
												...(networkFailure ? { reason: 'network' } : {})
											};
											_history.messages[responseMessageId] = responseMessage;
											mirrorHistoryMessage(responseMessageId);
										}, 1000);
									});

									if (retryShouldExit()) {
										finalizeStoppedRetry();
										retryCancelled = true;
										break;
									}
									continue;
								}

								// All retries exhausted — restore tool context so manual retry can use it
								if (savedToolContent) {
									responseMessage.content = savedToolContent;
									responseMessage.preservedToolContext = true;
									if (savedReasoningDetails) {
										responseMessage.reasoning_details = savedReasoningDetails;
									}
									if (savedReasoningDetailsPerRound) {
										responseMessage.reasoning_details_per_round = savedReasoningDetailsPerRound;
									}
								}

								// Check for provider restrictions
								const hasProviderRestrictions = !!(
									model?.info?.params?.custom_params?.provider?.only?.length ||
									model?.info?.params?.custom_params?.provider?.order?.length
								);
								if (hasProviderRestrictions) {
									responseMessage.providerFailed = true;
								}
								_history.messages[responseMessageId] = responseMessage;
								mirrorHistoryMessage(responseMessageId);
								break;
							}
						} finally {
							activeSendRetryLoops = Math.max(0, activeSendRetryLoops - 1);
						}
						skipRemainingRetriesSet.delete(responseMessageId);

						// Defensive cleanup: the "Retrying in Xs..." box must never outlive
						// the retry loop, even if state got corrupted by a stale socket
						// event mid-countdown. Patch the single field on the LIVE message
						// rather than mirroring this loop's snapshot — once a newer
						// generation owns the message id, republishing the whole snapshot
						// would stomp the live run's state.
						{
							const liveMsg = history.messages[responseMessageId];
							if (liveMsg?.retrying) {
								liveMsg.retrying = null;
								history.messages[responseMessageId] = { ...liveMsg };
								history = { ...history };
							}
							const snapshotMsg = _history.messages[responseMessageId];
							if (snapshotMsg?.retrying) snapshotMsg.retrying = null;
						}
					} else {
						toast.error($i18n.t(`Model {{modelId}} not found`, { modelId }));
					}
				})
			);
		} finally {
			chatStreamDebug('[chat-stream] sendMessage finally — settling response lifecycles');
			for (const responseMessageId of Object.values(responseMessageIds)) {
				const message = history.messages[responseMessageId];
				if (message?.done || message?.error || isUserStoppedMessageId(responseMessageId)) {
					settleGenerationLifecycle(responseMessageId);
				}
			}
		}
	};

	const getFeatures = (modelIds: string[] | null = null) => {
		let features = {};

		if ($config?.features)
			features = {
				image_generation:
					$config?.features?.enable_image_generation &&
					($user?.role === 'admin' || $user?.permissions?.features?.image_generation !== false)
						? imageGenerationEnabled
						: false,
				web_search:
					$config?.features?.enable_web_search &&
					($user?.role === 'admin' || $user?.permissions?.features?.web_search !== false)
						? webSearchEnabled
						: false,
				study_mode: $config?.features?.enable_study_mode ? studyModeEnabled : false,
				data_viz: $config?.features?.enable_data_viz ? dataVizEnabled : false,
				automations: $config?.features?.enable_automations ? automationsEnabled : false,
				subagents:
					$config?.features?.enable_subagents &&
					($user?.role === 'admin' || $user?.permissions?.features?.subagents !== false)
						? subagentsEnabled
						: false
			};

		const currentModels = modelIds ?? (atSelectedModel?.id ? [atSelectedModel.id] : selectedModels);
		if (
			currentModels.filter(
				(model) => $models.find((m) => m.id === model)?.info?.meta?.capabilities?.web_search ?? true
			).length === currentModels.length
		) {
			if ($config?.features?.enable_web_search && ($settings?.webSearch ?? false) === 'always') {
				features = { ...features, web_search: true };
			}
		}

		return features;
	};

	// The backend only reads `model_item` for DIRECT models (ids absent from
	// app.state.MODELS): main.py uses it both as the v2.1 assembly fallback model
	// and as the model itself when `model_item.direct` is set. For backend-managed
	// models the id is in MODELS and model_item is ignored entirely — so shipping
	// the whole model object every completion POST is pure waste on metered links.
	// Send it only for direct models, and drop the (often base64) avatar the backend
	// never needs.
	const buildModelItemUplink = (model: any) => {
		if (!model?.direct) return undefined;
		const meta = model?.info?.meta;
		if (meta && 'profile_image_url' in meta) {
			const { profile_image_url, ...metaRest } = meta;
			return { ...model, info: { ...model.info, meta: metaRest } };
		}
		return model;
	};

	// Snapshot the full send context for a queued message so the backend can
	// drive the send autonomously (zero tabs open). Mirrors the payload
	// construction in sendMessageSocket: resolves selectedToolIds into
	// tool_ids + tool_servers, captures features/params/reasoning/service_tier/
	// model. The chosen model is the @-mentioned one (if any) else the primary
	// selected model. Time-sensitive prompt variables are intentionally omitted
	// — the backend recomputes them from `timezone` at drain time.
	const captureQueueSendSpec = async (
		userPrompt: string,
		itemFiles: any[],
		atModelId: string | null,
		queuedParentMessageId: string | null
	) => {
		// Queue capture also awaits tool discovery. Freeze its source chat before
		// that await for the same reason as an immediate send.
		const queueSelectedModels = [...(selectedModels ?? [])];
		const queueSelectedToolIds = [...(selectedToolIds ?? [])];
		const queueSelectedFilterIds = [...(selectedFilterIds ?? [])];
		const queueSettings = cloneState($settings ?? {});
		const queueParams = cloneState(params ?? {});
		const queueFeatures = cloneState(getFeatures());
		const queueReasoning = cloneState(reasoning);
		const queueServiceTier = serviceTier;
		const queueUserName = $user?.name;
		const modelId = atModelId || queueSelectedModels[0];
		const model = $models.find((m) => m.id === modelId);
		if (!model) return null;

		const toolIds: string[] = [];
		const toolServerIds: any[] = [];
		for (const toolId of queueSelectedToolIds) {
			if (toolId.startsWith('direct_server:')) {
				const serverId = toolId.replace('direct_server:', '');
				toolServerIds.push(!isNaN(parseInt(serverId)) ? parseInt(serverId) : serverId);
			} else {
				toolIds.push(toolId);
			}
		}

		let selectedToolServers: any[] = [];
		if (toolServerIds.length > 0) {
			await loadToolServers().catch(() => undefined);
			selectedToolServers = ($toolServers ?? []).filter(
				(server, idx) => toolServerIds.includes(idx) || toolServerIds.includes(server?.id)
			);
		}

		const usesUsage = model.info?.meta?.capabilities?.usage ?? false;
		const serviceTierDisabled = (model?.info?.meta as any)?.service_tier?.enabled === false;

		return {
			model: model.id,
			models: atModelId ? [atModelId] : queueSelectedModels,
			queued_parent_message_id: queuedParentMessageId,
			content: userPrompt,
			files: cloneState(itemFiles ?? []),
			params: { ...queueSettings?.params, ...queueParams },
			tool_ids: toolIds.length > 0 ? toolIds : undefined,
			tool_servers: selectedToolServers,
			tool_selection: buildToolSelectionEnvelope(
				queueSelectedToolIds,
				selectedToolServers,
				queueFeatures,
				(queueParams as any)?.subagentExternalToolsEnabled ?? subagentExternalToolsEnabled
			),
			filter_ids: queueSelectedFilterIds.length > 0 ? queueSelectedFilterIds : undefined,
			features: queueFeatures,
			variables: {
				// Location is resolved per-send in sendMessageSocket and isn't in
				// scope here; the backend recomputes time-sensitive variables from
				// `timezone` at drain time, so snapshotting name-only is fine.
				...getPromptVariables(queueUserName, undefined)
			},
			reasoning: queueReasoning,
			...(serviceTierDisabled ? {} : { service_tier: queueServiceTier }),
			background_tasks: { follow_up_generation: queueSettings?.autoFollowUps ?? true },
			model_item: buildModelItemUplink(model),
			...(usesUsage ? { stream_options: { include_usage: true } } : {}),
			timezone: Intl?.DateTimeFormat?.()?.resolvedOptions?.()?.timeZone
		};
	};

	const getFileContentUrl = (file: any) => {
		if (file?.url) return file.url;
		return file?.id ? `${WEBUI_API_BASE_URL}/files/${file.id}/content` : '';
	};

	const sendMessageSocket = async (
		model,
		_messages,
		_history,
		responseMessageId,
		_chatId,
		opts = {}
	) => {
		if (blockParentGenerationDuringSubagentRerun()) return null;
		const responseMessage = _history.messages[responseMessageId];
		if (!responseMessage) return null;
		const { generationId, turnId } = prepareGenerationLifecycle(_chatId, responseMessage, {
			generationId: (opts as any)?.generationId,
			turnId: (opts as any)?.turnId
		});
		const requestNavigationGeneration = navigateGeneration;
		if (generationLifecycles.isStopped(responseMessageId, generationId)) {
			return null;
		}
		// Everything below can await (tick, geolocation, file reads, tool-server
		// discovery). Snapshot the chat-scoped send context now so navigating to
		// another chat cannot splice that chat's tools/settings/files into this
		// request. The per-message controller is also installed before the first
		// await; Stop therefore latches and aborts the whole preflight, and the
		// final ownership check prevents a POST from being resurrected afterward.
		const requestSettings = cloneState($settings ?? {});
		const requestParams = cloneState(params ?? {});
		const requestSelectedToolIds = [...(selectedToolIds ?? [])];
		const requestSelectedFilterIds = [...(selectedFilterIds ?? [])];
		const requestSelectedModels = [...(selectedModels ?? [])];
		const requestAtSelectedModelId = atSelectedModel?.id;
		// Capability gates belong to the model already stamped on this turn, not
		// whatever model the picker may show after the placeholder save.
		const requestFeatures = cloneState(getFeatures([model.id]));
		const requestReasoning = cloneState(reasoning);
		const requestServiceTier = serviceTier;
		const requestTemporaryChatEnabled = $temporaryChatEnabled;
		const requestConfigFeatures = cloneState(($config as any)?.features ?? {});
		const requestUserName = $user?.name;
		const requestChatFiles = cloneState(chatFiles ?? []);
		const requestModelItem = buildModelItemUplink($models.find((m) => m.id === model.id) ?? model);
		// Session identity is only for live delivery. Never fabricate or reuse a
		// disconnected socket id to force backend persistence: saved-chat
		// generations are durable independently, and a fake/stale id can make an
		// interactive event caller wait on a browser session that does not exist.
		const requestSessionId = $socket?.connected && $socket?.id ? $socket.id : undefined;
		const requestController = new AbortController();
		if (!attachGenerationController(responseMessageId, generationId, requestController)) {
			return null;
		}
		const requestStoppedBeforePost = () =>
			requestController.signal.aborted ||
			generationLifecycles.isStopped(responseMessageId, generationId) ||
			!generationLifecycles.isCurrent(responseMessageId, generationId) ||
			isUserStoppedMessageId(responseMessageId, _history.messages);

		// `engage` pins to the bottom; `preserve` (the default) only follows if
		// the reader is already at the bottom, so it never yanks a user who
		// scrolled up. A reader must never be moved without an explicit reason —
		// callers that intend a hard force-to-bottom must pass 'engage' explicitly.
		const scrollBehavior = (opts as any)?.scrollBehavior ?? 'preserve';
		const applyScrollIntent = () => {
			if (scrollBehavior === 'preserve') {
				if (autoScroll) scrollToBottom();
			} else {
				engageAndScrollToBottom();
			}
		};
		// Same assistant id can be reused by continue/retry flows; a new request
		// must be allowed to count a fresh usage payload even if numbers match.
		lastAppliedUsageByMessage.delete(responseMessageId);
		const userMessage = _history.messages[responseMessage.parentId];
		const leafMessageId = (opts as any)?.leafMessageId ?? responseMessage.parentId;
		const v2NewUserMessage =
			userMessage?.role === 'user'
				? {
						id: userMessage.id,
						parentId: userMessage.parentId ?? null,
						role: 'user',
						content: userMessage.content ?? '',
						files: userMessage.files ?? [],
						models: userMessage.models ?? [],
						timestamp: userMessage.timestamp
					}
				: null;

		const chatMessageFiles = _messages
			.filter((message) => message.files)
			.flatMap((message) => message.files);

		// Keep this request's file selection local. Mutating the component-level
		// chatFiles here used to let a stale Chat A continuation filter Chat B's
		// attachments after navigation.
		const relevantChatFiles = requestChatFiles.filter((item) => {
			const fileExists = chatMessageFiles.some((messageFile) => messageFile.id === item.id);
			return fileExists;
		});

		let files = cloneState(relevantChatFiles);
		files.push(
			...(userMessage?.files ?? []).filter((item) =>
				['doc', 'text', 'file', 'note', 'chat', 'collection'].includes(item.type)
			)
		);
		// Remove duplicates
		files = files.filter(
			(item, index, array) =>
				array.findIndex((i) => JSON.stringify(i) === JSON.stringify(item)) === index
		);

		applyScrollIntent();
		eventTarget.dispatchEvent(
			new CustomEvent('chat:start', {
				detail: {
					id: responseMessageId
				}
			})
		);
		await tick();
		if (requestStoppedBeforePost()) return null;

		let userLocation;
		if (requestSettings?.userLocation) {
			userLocation = await getAndUpdateUserLocation(localStorage.token).catch((err) => {
				console.error(err);
				return undefined;
			});
			if (requestStoppedBeforePost()) return null;
		}

		const stream =
			model?.info?.params?.stream_response ??
			requestSettings?.params?.stream_response ??
			requestParams?.stream_response ??
			true;

		const containerFeatures = requestConfigFeatures;
		const containerToolId = containerFeatures?.container_mcp_server_id
			? `server:mcp:${containerFeatures.container_mcp_server_id}`
			: '';
		const containerWorkspaceActive = Boolean(
			containerFeatures?.enable_container_workspace_sync &&
				containerToolId &&
				requestSelectedToolIds.includes(containerToolId)
		);

		// v2.1 body shape: backend assembles the conversation by walking
		// chat_message rows from leaf_message_id. Temporary chats aren't
		// persisted, so they keep the v1 messages-array body. The v1 build
		// below also stays as the fallback when the backend hasn't flipped
		// STREAM_PROTOCOL_VERSION to v2.1 yet.
		const isTempChat = requestTemporaryChatEnabled || _chatId?.startsWith('local:');
		const useV21Body = requestConfigFeatures?.stream_protocol_version === 'v2.1' && !isTempChat;

		let messages: any[] = [];
		if (!useV21Body) {
			messages = [
				requestParams?.system || requestSettings.system
					? {
							role: 'system',
							content: `${requestParams?.system ?? requestSettings?.system ?? ''}`
						}
					: undefined,
				...expandMessagesForToolResumption(_messages).map((message) => ({
					...message,
					content:
						typeof message.content === 'string' ? processDetails(message.content) : message.content
				}))
			].filter((message) => message);

			const TEXT_FILE_EXTS = new Set([
				'txt',
				'md',
				'markdown',
				'rst',
				'csv',
				'tsv',
				'json',
				'jsonl',
				'ndjson',
				'yaml',
				'yml',
				'toml',
				'ini',
				'cfg',
				'conf',
				'env',
				'log',
				'xml',
				'svg',
				'py',
				'pyi',
				'ipynb',
				'js',
				'mjs',
				'cjs',
				'ts',
				'tsx',
				'jsx',
				'vue',
				'svelte',
				'java',
				'kt',
				'kts',
				'scala',
				'groovy',
				'c',
				'cc',
				'cpp',
				'cxx',
				'h',
				'hpp',
				'hxx',
				'rs',
				'go',
				'rb',
				'php',
				'pl',
				'pm',
				'lua',
				'r',
				'jl',
				'dart',
				'swift',
				'm',
				'mm',
				'cs',
				'fs',
				'fsx',
				'ex',
				'exs',
				'erl',
				'hs',
				'ml',
				'mli',
				'clj',
				'cljs',
				'sh',
				'bash',
				'zsh',
				'fish',
				'ps1',
				'bat',
				'cmd',
				'sql',
				'graphql',
				'gql',
				'proto',
				'css',
				'scss',
				'sass',
				'less',
				'tex',
				'bib',
				'srt',
				'vtt',
				'patch',
				'diff',
				'gitignore',
				'dockerignore',
				'editorconfig'
			]);

			const isTextFile = (file) => {
				if (file?.type !== 'file') return false;
				const name = (file.name || file.file?.filename || '').toLowerCase();
				if (name.endsWith('.pdf')) return false;
				const dot = name.lastIndexOf('.');
				const ext = dot >= 0 ? name.slice(dot + 1) : name;
				if (ext && TEXT_FILE_EXTS.has(ext)) return true;
				const ct = (file.content_type || file.file?.meta?.content_type || '').toLowerCase();
				if (ct.startsWith('text/') && !ct.includes('html')) return true;
				return false;
			};

			// Files that don't read as plain text but the backend can extract from:
			// office formats, html, epub, etc. These travel as `type: "file"` content
			// parts with a `processing_mode` and get materialised on the server
			// (openai.py file-part loop: text mode → <document> text part;
			// pdf mode → LibreOffice → existing PDF + file-parser plugin path).
			const EXTRACTABLE_EXTS = new Set([
				'docx',
				'doc',
				'odt',
				'rtf',
				'pptx',
				'ppt',
				'xlsx',
				'xls',
				'html',
				'htm',
				'epub'
			]);

			const isExtractableFile = (file) => {
				if (file?.type !== 'file') return false;
				const name = (file.name || file.file?.filename || '').toLowerCase();
				if (name.endsWith('.pdf')) return false;
				const dot = name.lastIndexOf('.');
				const ext = dot >= 0 ? name.slice(dot + 1) : '';
				return EXTRACTABLE_EXTS.has(ext);
			};

			const fetchTextFileContent = async (file) => {
				if (typeof file._inlinedText === 'string') return file._inlinedText;
				try {
					const blob = await getFileContentById(file.id);
					const text = blob ? await blob.text() : '';
					file._inlinedText = text;
					return text;
				} catch (e) {
					console.error('Failed to read text file content:', e);
					file._inlinedText = '';
					return '';
				}
			};

			const escapeXmlAttr = (s) =>
				String(s)
					.replace(/&/g, '&amp;')
					.replace(/</g, '&lt;')
					.replace(/>/g, '&gt;')
					.replace(/"/g, '&quot;');

			const buildTextFileBlocks = async (files) => {
				const textFiles = (files ?? []).filter(isTextFile);
				if (!textFiles.length) return '';
				const blocks = await Promise.all(
					textFiles.map(async (f) => {
						const name = f.name || f.file?.filename || 'file';
						const text = await fetchTextFileContent(f);
						return `<document filename="${escapeXmlAttr(name)}">\n${text}\n</document>`;
					})
				);
				return blocks.join('\n\n') + '\n\n';
			};

			messages = (
				await Promise.all(
					messages.map(async (message) => {
						// Structured content_blocks travel through to the backend untouched —
						// `blocks_to_api_messages` on the server is the single source of truth
						// for the internal-message → API-message conversion.
						if (
							message?.role === 'assistant' &&
							Array.isArray(message?.content_blocks) &&
							message.content_blocks.length > 0
						) {
							return {
								role: 'assistant',
								content_blocks: message.content_blocks,
								...(message.tool_result_bodies
									? { tool_result_bodies: message.tool_result_bodies }
									: {}),
								...(message.reasoning_details_per_round
									? { reasoning_details_per_round: message.reasoning_details_per_round }
									: {}),
								...(message.reasoning_details
									? { reasoning_details: message.reasoning_details }
									: {})
							};
						}

						if (message.role === 'tool') {
							return {
								role: 'tool',
								content: message.content ?? '',
								...(message.tool_call_id ? { tool_call_id: message.tool_call_id } : {})
							};
						}

						if (message.tool_calls) {
							if (Array.isArray(message.reasoning_details)) {
								const signatureDetail = message.reasoning_details.find(
									(d) => d.type === 'reasoning.encrypted' && d.data
								);

								if (signatureDetail) {
									message.tool_calls = message.tool_calls.map((tc) => ({
										...tc,
										extra_content: { google: { thought_signature: signatureDetail.data } }
									}));
								}
							}

							return {
								role: 'assistant',
								content: (message?.merged?.content ?? message.content) || null,
								tool_calls: message.tool_calls,
								// OpenAI Responses API (and Anthropic) require the reasoning that
								// led to a function_call to be preserved on the assistant message
								// in follow-up requests. Dropping it breaks the reasoning chain on
								// multi-turn tool-call conversations.
								...(message.reasoning_details
									? { reasoning_details: message.reasoning_details }
									: {})
							};
						}

						const hasImages = message.files?.some((file) => file.type === 'image');
						const isUser = message.role === 'user';
						const modelSupportsVision = model?.info?.meta?.capabilities?.vision ?? true;

						// Check if message has PDF files
						const hasPdfFiles = message.files?.some(
							(file) =>
								file.type === 'file' &&
								(file.name?.toLowerCase().endsWith('.pdf') ||
									file.file?.filename?.toLowerCase().endsWith('.pdf'))
						);

						// docx/xlsx/pptx/etc. — always sent as file parts so the backend
						// can text-extract (or PDF-convert per processing_mode). Unlike
						// images/PDFs these don't gate on vision capability — extracted
						// text works on every model.
						const hasExtractableFiles = message.files?.some(isExtractableFile);
						const shouldAttachImages = isUser && hasImages && modelSupportsVision;
						const shouldSendFilesToModel = isUser && !containerWorkspaceActive;
						// PDFs use OpenRouter's native file-parser path, so container mode
						// still sends them to the model while also copying them into /workspace/inputs.
						const shouldAttachPdfFiles = isUser && hasPdfFiles && modelSupportsVision;
						const shouldAttachExtractableFiles = shouldSendFilesToModel && hasExtractableFiles;

						const textPrefix =
							isUser && !containerWorkspaceActive ? await buildTextFileBlocks(message.files) : '';
						const baseText = message?.merged?.content ?? message.content ?? '';

						if (
							isUser &&
							(shouldAttachImages || shouldAttachPdfFiles || shouldAttachExtractableFiles)
						) {
							return {
								role: message.role,
								content: [
									{
										type: 'text',
										text: textPrefix + baseText
									},
									// Add image content parts (vision-capable models only).
									...(shouldAttachImages
										? message.files
												.filter((file: any) => file.type === 'image' && getFileContentUrl(file))
												.map((file: any) => ({
													type: 'image_url',
													image_url: {
														url: getFileContentUrl(file),
														full_quality: file.fullQuality === true
													}
												}))
										: []),
									// PDF file parts for OpenRouter's file-parser plugin
									// (vision-capable models only; existing behavior).
									...(shouldAttachPdfFiles
										? message.files
												.filter(
													(file) =>
														file.type === 'file' &&
														(file.name?.toLowerCase().endsWith('.pdf') ||
															file.file?.filename?.toLowerCase().endsWith('.pdf'))
												)
												.map((file) => ({
													type: 'file',
													file: {
														filename: file.name || file.file?.filename || 'document.pdf',
														file_data: file.url || `${WEBUI_API_BASE_URL}/files/${file.id}/content`
													}
												}))
										: []),
									// docx/xlsx/pptx/etc. — backend extracts on receipt and
									// replaces this part with either a <document> text part
									// (mode == 'text') or a PDF binary part routed through
									// the file-parser plugin (mode == 'pdf').
									...(shouldAttachExtractableFiles
										? message.files.filter(isExtractableFile).map((file) => ({
												type: 'file',
												file: {
													filename: file.name || file.file?.filename || 'document',
													file_data: file.url || `${WEBUI_API_BASE_URL}/files/${file.id}/content`,
													processing_mode: file.processing_mode === 'pdf' ? 'pdf' : 'text'
												}
											}))
										: [])
								]
							};
						}

						return {
							role: message.role,
							content: isUser ? textPrefix + baseText : baseText,
							...(message.reasoning_details ? { reasoning_details: message.reasoning_details } : {})
						};
					})
				)
			).filter(
				(message) =>
					message?.role === 'user' ||
					message?.role === 'tool' ||
					hasMessageContent(message?.content) ||
					message?.reasoning_details ||
					message?.tool_calls?.length ||
					(Array.isArray(message?.content_blocks) && message.content_blocks.length > 0)
			);
		} // end if (!useV21Body)

		const toolIds = [];
		const toolServerIds = [];

		for (const toolId of requestSelectedToolIds) {
			if (toolId.startsWith('direct_server:')) {
				let serverId = toolId.replace('direct_server:', '');
				// Check if serverId is a number
				if (!isNaN(parseInt(serverId))) {
					toolServerIds.push(parseInt(serverId));
				} else {
					toolServerIds.push(serverId);
				}
			} else {
				toolIds.push(toolId);
			}
		}

		let selectedToolServers = [];
		if (toolServerIds.length > 0) {
			await loadToolServers().catch((error) => {
				if (!requestStoppedBeforePost()) toast.error(`${error}`);
				throw error;
			});
			if (requestStoppedBeforePost()) return null;

			selectedToolServers = ($toolServers ?? []).filter(
				(server, idx) => toolServerIds.includes(idx) || toolServerIds.includes(server?.id)
			);

			if (selectedToolServers.length < toolServerIds.length) {
				const error = $i18n.t('Failed to load selected tool servers.');
				if (!requestStoppedBeforePost()) toast.error(error);
				throw new Error(error);
			}
		}

		// In stream-v2.1, `_messages` is the persisted branch ending at the
		// just-created assistant placeholder (`responseMessageId`). For a brand-new
		// chat that makes the branch look like [user, empty assistant], which used
		// to suppress title/tag generation because it no longer matched the
		// first-turn length checks. Gate on the conversation before the current
		// assistant response instead.
		const firstTurnMessages = useV21Body
			? _messages.filter((message) => message?.id !== responseMessageId)
			: messages;
		const isFirstTurn =
			firstTurnMessages.filter((message) => message?.role === 'user').length === 1 &&
			firstTurnMessages.every((message) => ['system', 'user'].includes(message?.role));

		if (requestStoppedBeforePost()) return null;

		const [res] = await chatCompletion(
			localStorage.token,
			{
				stream: stream,
				model: model.id,
				...(useV21Body
					? {
							leaf_message_id: leafMessageId,
							...(v2NewUserMessage ? { new_user_message: v2NewUserMessage } : {})
						}
					: { messages: messages }),
				params: {
					...requestSettings?.params,
					...requestParams,
					stop:
						(requestParams?.stop ?? requestSettings?.params?.stop ?? undefined)
							? (
									requestParams?.stop.split(',').map((token) => token.trim()) ??
									requestSettings.params.stop
								).map((str) =>
									decodeURIComponent(JSON.parse('"' + str.replace(/\"/g, '\\"') + '"'))
								)
							: undefined
				},

				files: (files?.length ?? 0) > 0 ? files : undefined,

				filter_ids: requestSelectedFilterIds.length > 0 ? requestSelectedFilterIds : undefined,
				tool_ids: toolIds.length > 0 ? toolIds : undefined,
				tool_servers: selectedToolServers,
				tool_selection: buildToolSelectionEnvelope(
					requestSelectedToolIds,
					selectedToolServers,
					requestFeatures,
					(requestParams as any)?.subagentExternalToolsEnabled ?? subagentExternalToolsEnabled
				),
				features: requestFeatures,
				variables: {
					...getPromptVariables(
						requestUserName,
						requestSettings?.userLocation ? userLocation : undefined
					)
				},
				model_item: requestModelItem,

				session_id: requestSessionId,
				chat_id: _chatId,
				id: responseMessageId,
				generation_id: generationId,
				turn_id: turnId,
				...((opts as any)?.supersedeActiveTurn ? { supersede_active_turn: true } : {}),

				// Lets the backend stamp `Current Date: YYYY-MM-DD (TZ)` in the
				// system prompt using the user's local time, not the server's UTC.
				timezone: Intl?.DateTimeFormat?.()?.resolvedOptions?.()?.timeZone,

				background_tasks: {
					...(!requestTemporaryChatEnabled &&
					isFirstTurn &&
					(requestSelectedModels[0] === model.id || requestAtSelectedModelId !== undefined)
						? {
								title_generation: requestSettings?.title?.auto ?? true,
								tags_generation: requestSettings?.autoTags ?? true
							}
						: {}),
					follow_up_generation: requestSettings?.autoFollowUps ?? true
				},

				...(stream && (model.info?.meta?.capabilities?.usage ?? false)
					? {
							stream_options: {
								include_usage: true
							}
						}
					: {}),

				// Include reasoning effort parameter
				reasoning: requestReasoning,

				// Include service tier for OpenRouter / OpenAI-compatible APIs.
				// Skip the field entirely when the selected model has service_tier
				// disabled in its meta — some providers (e.g. Gemini via OpenRouter)
				// don't support it and including a stale value from localStorage
				// can confuse them.
				...((model?.info?.meta as any)?.service_tier?.enabled === false
					? {}
					: { service_tier: requestServiceTier }),

				...(opts.stripProvider ? { strip_provider: true } : {})
			},
			`${WEBUI_BASE_URL}/api`,
			requestController
		).catch(async (error) => {
			console.log(error);
			chatStreamDebug('[chat-stream] chatCompletion .catch fired', {
				responseMessageId,
				name: error?.name,
				message: error?.message,
				stack: error?.stack
			});

			const stopped =
				generationLifecycles.isStopped(responseMessageId, generationId) ||
				isUserStoppedMessageId(responseMessageId, _history.messages);
			const stillVisible = generationLifecycles.isVisible(
				responseMessageId,
				generationId,
				getVisibleChatId(),
				navigateGeneration
			);
			if (stopped || error?.name === 'AbortError' || !stillVisible) {
				if (stopped) {
					responseMessage.done = true;
					responseMessage.userStopped = true;
					if (stillVisible && history.messages[responseMessageId]) {
						history.messages[responseMessageId] = responseMessage;
						history = { ...history };
					}
				}
				return [null, null] as [null, null];
			}

			let errorMessage = error;
			if (error?.error?.message) {
				errorMessage = error.error.message;
			} else if (error?.message) {
				errorMessage = error.message;
			}

			if (typeof errorMessage === 'object') {
				errorMessage = $i18n.t(`Uh-oh! There was an issue with the response.`);
			}

			// A network-level failure is a connectivity event, not a model error:
			// mark it so the retry loops treat it as "waiting for connection"
			// (no scary toast mid-outage, no no-progress budget burn). The final
			// exhaustion path still surfaces it if connectivity never returns.
			const networkFailure = isNetworkFetchError(error);
			if (!suppressErrorToast && !networkFailure) toast.error(`${errorMessage}`);
			responseMessage.error = {
				content: error,
				...(networkFailure ? { network: true } : {})
			};

			responseMessage.done = true;

			history.messages[responseMessageId] = responseMessage;
			history.currentId = responseMessageId;

			// MUST be a destructurable pair: chatCompletion() THROWS on a
			// network-level fetch failure, and returning bare `null` here made
			// `const [res, controller] = ...` throw "null is not iterable" —
			// which crashed clean out of sendMessageSocket, SKIPPED the retry
			// loop entirely (the message stranded as done+error with action
			// buttons), and surfaced as an unhandled rejection. The error state
			// set above is exactly what the retry loop keys off; let it run.
			return [null, null] as [null, null];
		});
		chatStreamDebug('[chat-stream] generation request resolved', {
			responseMessageId,
			generationId,
			aborted: requestController.signal.aborted
		});

		const requestStopped = () =>
			generationLifecycles.isStopped(responseMessageId, generationId) ||
			isUserStoppedMessageId(responseMessageId, _history.messages);
		const requestVisible = () =>
			generationLifecycles.isVisible(
				responseMessageId,
				generationId,
				getVisibleChatId(),
				navigateGeneration
			);
		// `responseMessage` belongs to THIS send's `_history`, which may not be the
		// visible history at all (the user can navigate mid-send) — so it is always
		// written, and the live row only when this request still owns the view.
		const finishStoppedResponse = () => {
			markTurnStopped(responseMessageId, {
				maps: [_history.messages, requestVisible() ? history.messages : null],
				chatId: _chatId
			});
			if (requestVisible() && history.messages[responseMessageId]) {
				history.messages[responseMessageId] = responseMessage;
				history = { ...history };
			}
		};
		const handleTaskEnvelope = async (taskResponse) => {
			if (taskResponse.kind === 'parse-error') {
				if (!requestVisible()) return;
				console.error('Error parsing async chat task response', taskResponse.error);
				await handleOpenAIError(
					{ message: 'Could not read the backend task response.' },
					responseMessage,
					'async-task-response-parse'
				);
				return;
			}

			const payload = taskResponse.payload;
			if (taskResponse.kind === 'stopped' || requestStopped()) {
				finishStoppedResponse();
				return;
			}
			if (!requestVisible()) return;
			if (payload?.cancelled) {
				// The backend cancelled this generation (a Stop from another tab, or
				// a queue/turn cancel). finishStoppedResponse latches it locally.
				finishStoppedResponse();
				return;
			}
			if (payload?.error) {
				await handleOpenAIError(payload.error, responseMessage, 'async-task-payload-error');
				return;
			}
			if (payload?.pending) {
				// markAccepted keeps the record live, which is what the composer's
				// derived working state reads.
				generationLifecycles.markAccepted(responseMessageId, generationId);
				if (_chatId && !_chatId.startsWith('local:')) {
					startResumeTaskPolling(_chatId);
				}
				return;
			}
			if (payload?.task_id) {
				const acceptance = generationLifecycles.markAccepted(
					responseMessageId,
					generationId,
					payload.task_id
				);
				if (acceptance === 'stopped') {
					finishStoppedResponse();
					return;
				}
				// markAccepted already recorded the task id on the lifecycle record,
				// which is where `taskIds` reads it from.
				return;
			}
			await handleOpenAIError(
				{ message: 'Chat request did not start a backend task.' },
				responseMessage,
				'async-task-missing-task-id'
			);
		};

		if (res) {
			if (stream) {
				if (!res.ok) {
					if (requestStopped()) {
						await handleTaskEnvelope(await readAsyncTaskResponse(res, requestStopped));
						return;
					}
					if (!requestVisible()) return;
					let errorPayload = null;
					try {
						errorPayload = await res.json();
					} catch {
						if (requestStopped() || !requestVisible()) return;
						errorPayload = { message: `HTTP ${res.status}` };
					}
					chatStreamDebug('[chat-stream] HTTP non-OK', {
						responseMessageId,
						status: res.status,
						errorPayload
					});
					await handleOpenAIError(errorPayload, responseMessage, `http-${res.status}`);
				} else if (res.body) {
					const contentType = res.headers.get('content-type') ?? '';
					const isEventStream = contentType.includes('text/event-stream');
					// Decide by what the server actually SENT, never by socket state:
					// a JSON body here is the async-task envelope ({status, task_id})
					// and must take the task path even if the socket is momentarily
					// down (delivery rides the stream room once it reconnects, with
					// replay/snapshot catch-up). The old `|| !$socket?.connected`
					// check made a briefly-disconnected tab try to SSE-parse that
					// JSON envelope — the turn then produced nothing client-side.
					const shouldUseDirectStream = isEventStream;

					if (shouldUseDirectStream) {
						if (requestStopped() || !requestVisible()) {
							requestController.abort();
							await res.body.cancel().catch(() => undefined);
							if (requestStopped()) finishStoppedResponse();
							return;
						}
						responseMessage.done = false;
						history.messages[responseMessageId] = responseMessage;
						history = { ...history };
						const textStream = await createOpenAITextStream(
							res.body,
							requestSettings.splitLargeChunks
						);
						try {
							for await (const update of textStream) {
								const { value, done, sources, error, usage, selectedModelId, aborted } = update;

								if (!requestVisible()) {
									requestController.abort();
									break;
								}
								if (requestStopped()) {
									finishStoppedResponse();
									break;
								}

								// Handle aborts FIRST (before any early-exit check) so a
								// user-driven stopResponse() flow still finalizes the message
								// instead of silently breaking out.
								if (aborted) {
									chatStreamDebug('[chat-stream] direct stream aborted', {
										responseMessageId,
										contentLen: responseMessage.content?.length ?? 0,
										userInitiatedStop
									});
									cancelStreamingMessageFlush(responseMessageId);
									responseMessage.done = true;
									history.messages[responseMessageId] = responseMessage;
									history = { ...history };
									break;
								}

								// A superseding run of this same message invalidates only
								// this stream. Sibling model controllers are independent.
								const activeLifecycle = generationLifecycles.get(responseMessageId);
								if (
									activeLifecycle?.generationId !== generationId ||
									activeLifecycle.controller !== requestController
								) {
									requestController.abort();
									break;
								}

								if (error) {
									chatStreamDebug('[chat-stream] direct stream error', {
										responseMessageId,
										error
									});
									cancelStreamingMessageFlush(responseMessageId);
									await handleOpenAIError(error, responseMessage, 'direct-stream-error');
									break;
								}

								if (sources && !responseMessage?.sources) {
									responseMessage.sources = sources;
								}

								if (selectedModelId) {
									responseMessage.selectedModelId = selectedModelId;
									responseMessage.arena = true;
								}

								if (usage) {
									responseMessage.usage = usage;
									applyUsageToChatTokenStats(_chatId, responseMessageId, usage);
									chatTokenStatsRefreshTrigger.update((n) => n + 1);
								}

								if (done) {
									cancelStreamingMessageFlush(responseMessageId);
									responseMessage.done = true;
									history.messages[responseMessageId] = responseMessage;
									history = { ...history };

									// We must save the chat here for direct streams, as there is no backend socket event to do it for us
									if (!requestTemporaryChatEnabled && isVisibleChatEvent(_chatId)) {
										await chatCompletedHandler(
											_chatId,
											model.id,
											responseMessageId,
											createMessagesList(history, responseMessageId)
										);
									}
									break;
								}

								if (!(responseMessage.content == '' && value == '\n')) {
									responseMessage.content += value;
									history.messages[responseMessageId] = responseMessage;
									scheduleStreamingMessageFlush(responseMessageId, {
										ownerId: responseMessageId
									});

									if (navigator.vibrate && (requestSettings?.hapticFeedback ?? false)) {
										navigator.vibrate(5);
									}
								}
							}
						} catch (e: any) {
							// Defense in depth: openAIStreamToIterator already converts thrown
							// errors into in-band updates, so this should rarely fire. If it
							// does, route through the same error path so the message finalizes.
							chatStreamDebug('[chat-stream] for-await threw', {
								responseMessageId,
								name: e?.name,
								message: e?.message
							});
							if (requestStopped()) {
								finishStoppedResponse();
							} else if (!requestVisible()) {
								return;
							} else if (e?.name === 'AbortError') {
								cancelStreamingMessageFlush(responseMessageId);
								responseMessage.done = true;
								history.messages[responseMessageId] = responseMessage;
								history = { ...history };
							} else {
								await handleOpenAIError(
									{ message: e?.message ?? String(e) },
									responseMessage,
									'for-await-throw'
								);
							}
						}
					} else {
						await handleTaskEnvelope(await readAsyncTaskResponse(res, requestStopped));
					}
				} else {
					if (requestStopped()) {
						finishStoppedResponse();
					} else if (requestVisible()) {
						await handleOpenAIError(
							{ message: 'Streaming response body is missing.' },
							responseMessage,
							'res-body-missing'
						);
					}
				}
			} else {
				// Non-streaming backend requests still return the same asynchronous
				// task envelope. Use the cancellation-aware reader so AbortError
				// during body parsing is a clean Stop, not an escaped rejection.
				await handleTaskEnvelope(await readAsyncTaskResponse(res, requestStopped));
			}
		}

		if (requestVisible()) {
			await tick();
			applyScrollIntent();
		}
	};

	const handleOpenAIError = async (error, responseMessage, source: string = 'unknown') => {
		let errorMessage = '';
		const innerError = error ?? { message: '' };
		const generationErrorCode = getChatGenerationErrorCode(innerError);
		const forceToast = isNonRetryableChatGenerationError(innerError);
		const showErrorToast = (message: unknown) => {
			if (suppressErrorToast && !forceToast) return;
			if (typeof message === 'string') {
				toast.error(message);
				return;
			}
			try {
				toast.error(JSON.stringify(message));
			} catch {
				toast.error(String(message));
			}
		};

		console.error(innerError);
		chatStreamDebug('[chat-stream] handleOpenAIError', {
			source,
			responseMessageId: responseMessage?.id,
			errorShape:
				innerError && typeof innerError === 'object' ? Object.keys(innerError) : typeof innerError,
			error: innerError
		});

		if (typeof innerError === 'string') {
			showErrorToast(innerError);
			errorMessage = innerError;
		} else if (innerError && typeof innerError === 'object' && 'content' in innerError) {
			// Canonical backend error shape ({content: str}) — the middleware
			// normalizes provider errors to this before emit/persist.
			showErrorToast(innerError.content);
			errorMessage = innerError.content;
		} else if (innerError && typeof innerError === 'object' && 'detail' in innerError) {
			// FastAPI error
			const detail = innerError.detail;
			const detailMessage =
				detail && typeof detail === 'object'
					? ((detail as any).message ?? (detail as any).content ?? detail)
					: detail;
			showErrorToast(detailMessage);
			errorMessage = detailMessage as any;
		} else if (innerError && typeof innerError === 'object' && 'error' in innerError) {
			// OpenAI error
			if (
				innerError.error &&
				typeof innerError.error === 'object' &&
				'message' in innerError.error
			) {
				showErrorToast(innerError.error.message);
				errorMessage = innerError.error.message;
			} else {
				showErrorToast(innerError.error);
				errorMessage = innerError.error;
			}
		} else if (innerError && typeof innerError === 'object' && 'message' in innerError) {
			// OpenAI error
			showErrorToast(innerError.message);
			errorMessage = innerError.message;
		}

		// OpenRouter docs: errors arrive as either the WRAPPED pre-stream shape
		// `{error: {code, message, metadata: {error_type, provider_name, raw}}}`
		// or the UNWRAPPED mid-stream shape where those fields sit at the top of
		// the chunk alongside `choices`. Read both.
		// Ref: https://openrouter.ai/docs/api/reference/errors
		const orError =
			innerError && typeof innerError === 'object' && 'error' in innerError && innerError.error
				? innerError.error
				: innerError;
		const code = orError?.code ?? innerError?.code;
		const meta = orError?.metadata ?? innerError?.metadata ?? {};
		const errorType = meta?.error_type;
		const providerName = meta?.provider_name;
		const rawText = meta?.raw;

		// Friendly labels for the documented OpenRouter HTTP codes so users can
		// tell at a glance whether to retry, switch model, or top up credits.
		// 408/502/503 are documented; 504 + the timeout error_type also occur in
		// practice (OpenRouter's own gateway timeout — not in the public table).
		const codeLabel = (() => {
			switch (Number(code)) {
				case 400:
					return 'Bad request';
				case 401:
					return 'Auth failed';
				case 402:
					return 'Out of credits';
				case 403:
					return 'Moderation block';
				case 408:
					return 'Request timeout';
				case 429:
					return 'Rate limited';
				case 502:
					return 'Provider down / bad response';
				case 503:
					return 'No available provider';
				case 504:
					return 'Gateway timeout';
				default:
					return null;
			}
		})();

		let displayMessage =
			typeof errorMessage === 'string' ? errorMessage : JSON.stringify(errorMessage);

		if (code || errorType || providerName) {
			const parts: string[] = [];
			if (code) parts.push(codeLabel ? `${codeLabel} (${code})` : `HTTP ${code}`);
			if (errorType && errorType !== codeLabel?.toLowerCase()) parts.push(String(errorType));
			if (providerName) parts.push(`via ${providerName}`);
			displayMessage = `${parts.join(' · ')}: ${displayMessage || '(no message)'}`;
		}
		// Append the raw upstream error if present and short — useful for
		// timeouts where `raw` often holds the actual provider response.
		if (typeof rawText === 'string' && rawText.length > 0 && rawText.length < 800) {
			displayMessage += `\n\n${rawText}`;
		} else if (rawText && typeof rawText === 'object') {
			try {
				const j = JSON.stringify(rawText);
				if (j.length < 800) displayMessage += `\n\n${j}`;
			} catch {
				// non-serializable, ignore
			}
		}

		const fallback = $i18n.t(`Uh-oh! There was an issue with the response.`);

		responseMessage.error = {
			content: displayMessage || fallback,
			...(generationErrorCode ? { code: generationErrorCode } : {})
		};
		responseMessage.done = true;

		cancelStreamingMessageFlush(responseMessage.id);
		history.messages[responseMessage.id] = responseMessage;

		// Admission/identity failures are terminal for this exact request. Persist
		// that fact immediately: the placeholder was durably appended before the
		// POST, and relying on a stream terminal event here is impossible because
		// the backend deliberately rejected the request before a task was created.
		if (isNonRetryableChatGenerationError(responseMessage.error)) {
			const lifecycle = generationLifecycles.get(responseMessage.id);
			const errorChatId = lifecycle?.chatId ?? getVisibleChatId();
			const ownsIdentity =
				!lifecycle ||
				!responseMessage.generation_id ||
				lifecycle.generationId === responseMessage.generation_id;
			if (
				ownsIdentity &&
				errorChatId &&
				!errorChatId.startsWith('local:') &&
				!$temporaryChatEnabled
			) {
				await saveChatHandler(errorChatId, history, params, [
					{
						op: 'update_message_content',
						message_id: responseMessage.id,
						content: responseMessage.content ?? '',
						done: true,
						error: responseMessage.error,
						retrying: null,
						generation_id: responseMessage.generation_id,
						turn_id: responseMessage.turn_id
					}
				]).catch((persistError) => {
					console.error('Failed to persist terminal chat admission error', persistError);
				});
			}
		}
	};

	const performStopResponse = async () => {
		const visibleChatId = getVisibleChatId();
		const stopNavigationGeneration = navigateGeneration;
		const stoppedMessageIds = new Set<string>();
		const responseMessage = history.messages[history.currentId];
		if (responseMessage) {
			if (responseMessage.parentId !== null && history.messages[responseMessage.parentId]) {
				for (const messageId of history.messages[responseMessage.parentId].childrenIds) {
					const message = history.messages[messageId];
					if (message?.role === 'assistant' && message.done !== true) {
						stoppedMessageIds.add(messageId);
					}
				}
			} else {
				if (history.currentId && responseMessage.done !== true) {
					stoppedMessageIds.add(history.currentId);
				}
			}
		}
		if (visibleChatId) {
			for (const record of generationLifecycles.activeForChat(visibleChatId)) {
				stoppedMessageIds.add(record.messageId);
			}
		}

		const generationTargetsById = new Map(
			visibleChatId
				? generationLifecycles
						.generationsForStop(visibleChatId, stoppedMessageIds)
						.map((target) => [target.generation_id, target])
				: []
		);
		for (const messageId of stoppedMessageIds) {
			const message = history.messages[messageId];
			const generationId = String(message?.generation_id ?? '');
			const turnId = String(message?.turn_id ?? '');
			if (generationId && turnId) {
				generationTargetsById.set(generationId, {
					generation_id: generationId,
					message_id: messageId,
					turn_id: turnId
				});
			}
		}
		const generationTargets = [...generationTargetsById.values()];

		// Latch every local effect before the first await. From this point onward,
		// preflight continuations and response-body readers observe Stop, all sibling
		// controllers are aborted, and navigation cannot redirect this operation at
		// another chat's history/controller. The composer follows automatically:
		// `localStop` suppresses the retry-loop term of `generating`, and the
		// latched lifecycle records drop the chat out of `generating`/`taskIds`.
		localStop = { at: Date.now() };
		for (const messageId of stoppedMessageIds) {
			markTurnStopped(messageId, { chatId: visibleChatId });
			releaseStreamMirror(messageId);
			const message = history.messages[messageId];
			if (message) history.messages[messageId] = { ...message };
			// The backend's chat:tasks:cancel does this too, but it can only arrive
			// after the task actually unwinds. Flip locally so the cards stop
			// spinning "Researching…" the instant Stop is pressed.
			flipRunningSubagentsTerminal(messageId);
		}
		// Stop also means "don't hand off to the queue drain". The backend agrees —
		// a user-stopped turn makes maybe_drain_queue pause the queue instead of
		// popping it — so a bridge left standing here would be waiting on a drain
		// that is never coming, which is the whole failure mode this guards.
		clearQueueDrainPending();
		stopResumeTaskPolling({ force: true });
		history = { ...history };

		// Detached subagent redos are separate backend tasks, so a Stop with only a
		// redo in flight still has server-side work to do.
		const hasLiveSubagentRerun = hasActiveDetachedSubagentRerun(get(subagentLiveStates));

		if (
			(generationTargets.length > 0 || hasLiveSubagentRerun) &&
			visibleChatId &&
			!visibleChatId.startsWith('local:') &&
			!$temporaryChatEnabled
		) {
			// The local latch above only stops THIS tab. If the request never
			// reaches the server the backend keeps generating (and keeps billing)
			// with no UI showing it — the one failure mode where dropping the
			// request silently is unacceptable. The endpoint is idempotent (it
			// writes cancellation intent, then stops whatever it finds), so retry a
			// network-level failure a few times; a real HTTP answer, success or
			// error, is final.
			const STOP_RETRY_DELAYS_MS = [500, 1500, 4000];
			let stopError: unknown = null;
			for (let attempt = 0; ; attempt++) {
				try {
					const stopped = await stopChatGenerations(localStorage.token, visibleChatId, {
						generations: generationTargets,
						// Stop means "halt this chat" — detached subagent redos included.
						include_subagent_reruns: true
					});
					stopError = null;
					// Only after the server confirms it actually cancelled rerun tasks:
					// flipping optimistically would freeze a card whose redo is still
					// running (mirrors the per-card Stop button's own rule).
					if ((stopped?.subagent_rerun_task_ids?.length ?? 0) > 0) {
						flipRunningSubagentRerunsTerminal();
					}
					break;
				} catch (error) {
					stopError = error;
					if (!isNetworkFetchError(error) || attempt >= STOP_RETRY_DELAYS_MS.length) break;
					await new Promise((r) => setTimeout(r, STOP_RETRY_DELAYS_MS[attempt]));
				}
			}
			if (
				stopError &&
				stopNavigationGeneration === navigateGeneration &&
				getVisibleChatId() === visibleChatId
			) {
				toast.error(`${stopError}`);
			}
		}

		// Reconcile the token pill to the authoritative DB total. A stop can land
		// inside the per-chat push throttle window, dropping the final
		// chat:token-usage push; and unlike the clean `done` path, the cancel path
		// has no other reconcile. Bump the (trailing-debounced) refresh so the pill
		// settles on the exact committed total instead of the last optimistic value.
		if (
			stopNavigationGeneration === navigateGeneration &&
			visibleChatId &&
			getVisibleChatId() === visibleChatId &&
			!visibleChatId.startsWith('local:') &&
			!$temporaryChatEnabled
		) {
			chatTokenStatsRefreshTrigger.update((n) => n + 1);
		}

		if (
			stopNavigationGeneration === navigateGeneration &&
			visibleChatId &&
			getVisibleChatId() === visibleChatId &&
			autoScroll
		) {
			scrollToBottom();
		}
	};

	const stopResponse = (): Promise<void> => {
		// Escape can originate in nested composer/editor handlers, and the Stop
		// button can be activated repeatedly before the first request settles.
		// Keep cancellation single-flight so repeated input events share one local
		// latch and one backend request. A new chat has no server id until its
		// create call returns, so key that short window by navigation generation.
		const stopKey = getVisibleChatId() || `pending:${navigateGeneration}`;
		const existing = stopResponsesInProgress.get(stopKey);
		if (existing) return existing;

		let operation: Promise<void>;
		operation = performStopResponse().finally(() => {
			if (stopResponsesInProgress.get(stopKey) === operation) {
				stopResponsesInProgress.delete(stopKey);
			}
		});
		stopResponsesInProgress.set(stopKey, operation);
		return operation;
	};

	/**
	 * Editing, retrying, or deleting a message is a branch replacement, not a
	 * queued follow-up. Release the current turn before Messages.svelte mutates
	 * durable ancestry. The backend registry remains the atomic authority; this
	 * preflight also reconciles work started by another browser session.
	 */
	const prepareBranchReplacement = (): Promise<boolean> => {
		if (branchReplacementPromise) return branchReplacementPromise;

		const operation = (async () => {
			const replacementChatId = getVisibleChatId();
			const replacementNavigationGeneration = navigateGeneration;
			const persistentChat =
				!!replacementChatId && !replacementChatId.startsWith('local:') && !$temporaryChatEnabled;
			let liveOperations: ServerGenerationOperation[] = [];

			if (persistentChat) {
				const workState = await getChatWorkState(localStorage.token, replacementChatId).catch(
					() => null
				);
				if (workState === null) {
					toast.error(
						$i18n.t('Could not verify whether this chat is still generating. Please try again.')
					);
					return false;
				}
				liveOperations = generationLifecycles.reconcileServerOperations(
					workState?.generations,
					replacementNavigationGeneration,
					replacementChatId
				);
				reconcileQueueDrain(workState);
			}

			const hasLiveWork =
				liveOperations.length > 0 ||
				generationLifecycles.activeForChat(replacementChatId).length > 0 ||
				activeSendRetryLoops > 0;
			if (!hasLiveWork) return true;

			toast.info($i18n.t('Stopping the current response before changing this branch.'));
			await stopResponse();

			// The Stop endpoint waits for task teardown, but keep a short
			// authoritative poll here for the pre-bind window and multi-worker event
			// propagation. Do not start the replacement until both server and local
			// retry ownership are gone.
			const releaseDeadline = Date.now() + 5000;
			while (Date.now() < releaseDeadline) {
				if (
					navigateGeneration !== replacementNavigationGeneration ||
					getVisibleChatId() !== replacementChatId
				) {
					return false;
				}

				let serverIdle = true;
				if (persistentChat) {
					const workState = await getChatWorkState(localStorage.token, replacementChatId).catch(
						() => null
					);
					serverIdle = workState !== null && (workState?.generations?.length ?? 0) === 0;
					if (workState !== null) {
						generationLifecycles.reconcileServerOperations(
							workState?.generations,
							replacementNavigationGeneration,
							replacementChatId
						);
						reconcileQueueDrain(workState);
					}
				}

				const localIdle =
					generationLifecycles.activeForChat(replacementChatId).length === 0 &&
					activeSendRetryLoops === 0;
				if (serverIdle && localIdle) return true;
				await new Promise((resolve) => setTimeout(resolve, 250));
			}

			toast.error(
				$i18n.t(
					'The current response could not be stopped safely. Your change was not applied; please try again.'
				)
			);
			return false;
		})();

		branchReplacementPromise = operation.finally(() => {
			branchReplacementPromise = null;
		});
		return branchReplacementPromise;
	};

	const submitMessage = async (parentId, prompt) => {
		let userPrompt = prompt;
		let userMessageId = uuidv4();

		let userMessage = {
			id: userMessageId,
			parentId: parentId,
			childrenIds: [],
			role: 'user',
			content: userPrompt,
			models: selectedModels,
			timestamp: Math.floor(Date.now() / 1000) // Unix epoch
		};

		if (parentId !== null) {
			history.messages[parentId].childrenIds = [
				...history.messages[parentId].childrenIds,
				userMessageId
			];
		}

		history.messages[userMessageId] = userMessage;
		history.currentId = userMessageId;

		await tick();

		if (autoScroll) {
			scrollToBottom();
		}

		// 'preserve': sending must never yank a reader who has scrolled up back
		// to the bottom. The bottom-pin only applies when the user is already
		// following the bottom (autoScroll true) — 'preserve' honors that
		// instead of forcing it.
		await sendMessage(history, userMessageId, { scrollBehavior: 'preserve' });
	};

	const retryWithoutProviderRestrictions = async (message) => {
		if (!history.currentId) return;

		const userMessage = history.messages[message.parentId];
		if (!userMessage) return;
		const retryChatId = getVisibleChatId();
		if (!retryChatId) return;

		const model = $models.find((m) => m.id === (message.selectedModelId ?? message.model));
		if (!model) return;

		// Preserve tool context from the failed message so retry continues from where it left off
		const originalToolContext = getRetryableToolContext(message?.content ?? '');
		// Snapshot BEFORE the content resets below: the assembly leaf may be
		// pinned to this message only if a prior run actually DELIVERED content
		// (the row then exists server-side with real history). A network-failed
		// send has no server-side row — pinning the leaf to it made assembly
		// manufacture a role-less orphan (see the primary retry loop).
		const hadDeliveredContent =
			!!originalToolContext?.hasCompletedToolCall ||
			(Array.isArray(message.content_blocks) && message.content_blocks.length > 0) ||
			!!(message.content ?? '').trim();
		let savedToolContent = null;
		let savedReasoningDetails = null;
		let savedReasoningDetailsPerRound = null;
		if (originalToolContext?.hasCompletedToolCall) {
			savedToolContent = originalToolContext.content;
			savedReasoningDetails = message.reasoning_details || null;
			savedReasoningDetailsPerRound = message.reasoning_details_per_round || null;
			message.content = savedToolContent;
			message.preservedToolContext = true;
		} else {
			message.content = '';
		}

		message.error = null;
		message.providerFailed = false;
		message.done = false;
		history.messages[message.id] = message;
		history = { ...history };

		// Bumping the retry-loop count is what makes the composer show this turn as
		// live: `generating` counts an active loop as work in flight, including the
		// gaps between attempts when no lifecycle record is armed.
		activeSendRetryLoops++;
		try {
			const MAX_NO_PROGRESS = 5;
			const ABSOLUTE_RETRY_CEILING = 100;
			let consecutiveNoProgress = 0;
			let lastCompletedToolCalls = -1;
			const _history = cloneState(history);
			const generationId = uuidv4();
			const turnId = uuidv4();

			// Mirrors the primary send loop: Stop ends this loop wherever it is, and
			// the terminal state is published to BOTH the live message and this
			// loop's detached snapshot. `_history` here is a real clone (not an alias
			// of `history`), so writing only the snapshot — as the countdown ticker
			// and the defensive cleanup below did — republished `done:false` over the
			// cancel and stranded the message spinning forever.
			const retryStopped = () =>
				generationLifecycles.isStopped(message.id, generationId) ||
				isUserStoppedMessageId(message.id);
			// A newer generation owns this message id now (continue / rewind /
			// regenerate reuse it) — get out of the way without touching its state.
			const retrySuperseded = () => !generationLifecycles.isCurrent(message.id, generationId);
			const retryShouldExit = () => retryStopped() || retrySuperseded();
			const finalizeStoppedRetry = () => {
				if (retrySuperseded() || !retryStopped()) return;
				// This retry is always driven from the visible chat, so the default
				// chat id is correct here.
				markTurnStopped(message.id, { maps: [history.messages, _history.messages] });
				const live = history.messages[message.id];
				if (live) {
					history.messages[message.id] = { ...live };
					history = { ...history };
				}
			};

			for (let attempt = 1; attempt <= ABSOLUTE_RETRY_CEILING; attempt++) {
				let responseMessage = _history.messages[message.id];

				if (attempt > 1) {
					// Preserve tool context so retry continues from where it left off
					if (savedToolContent) {
						responseMessage.content = savedToolContent;
						responseMessage.preservedToolContext = true;
						if (savedReasoningDetails) {
							responseMessage.reasoning_details = savedReasoningDetails;
						}
						if (savedReasoningDetailsPerRound) {
							responseMessage.reasoning_details_per_round = savedReasoningDetailsPerRound;
						}
					} else if (
						!Array.isArray(responseMessage.content_blocks) ||
						responseMessage.content_blocks.length === 0
					) {
						// Structured turns carry their partial history in content_blocks;
						// content is a projection — wiping it just blanks the visible turn.
						responseMessage.content = '';
					}
					responseMessage.error = null;
					responseMessage.done = false;
					responseMessage.retrying = null;
					_history.messages[message.id] = responseMessage;
					history.messages[message.id] = cloneState(responseMessage);
					history = { ...history };
				}

				suppressErrorToast = consecutiveNoProgress + 1 < MAX_NO_PROGRESS;

				await sendMessageSocket(
					model,
					createMessagesList(_history, message.id),
					_history,
					message.id,
					retryChatId,
					{
						stripProvider: true,
						scrollBehavior: 'preserve',
						generationId,
						turnId,
						supersedeActiveTurn: true,
						// Unlike a brand-new send, `message` here is the ALREADY-ERRORED
						// assistant turn being retried — it can carry a full content_blocks
						// history (e.g. many rounds of tool calls from a deep-research turn).
						// sendMessageSocket's `leafMessageId` defaults to responseMessage.parentId
						// (the user message), which is right for a fresh send but WRONG for a
						// retry: under the v2.1 body the backend reconstructs the entire outbound
						// history by walking chat_message rows from leaf_message_id (ignoring the
						// client's `messages` array), so defaulting to parentId drops this message
						// — and all its content_blocks tool history — from the resend entirely.
						// That collapsed a poisoned/errored deep-research turn's retry down to
						// just system+user. Pin the leaf to message.id so the backend's walk
						// includes it, on every attempt (not just attempt > 1 — this message
						// already has real history from before the first retry too). Only
						// when the message actually delivered content (row exists
						// server-side) — see hadDeliveredContent above.
						...(hadDeliveredContent ? { leafMessageId: message.id } : {})
					}
				);
				if (getVisibleChatId() !== retryChatId) {
					generationLifecycles.terminal(message.id, generationId);
					break;
				}

				// Wait for response to actually complete (socket-based delivery)
				{
					const msg = history.messages[message.id];
					if (!msg?.done && !msg?.error) {
						while (true) {
							await new Promise((r) => setTimeout(r, 100));
							if (getVisibleChatId() !== retryChatId) {
								generationLifecycles.terminal(message.id, generationId);
								break;
							}
							// This wait has no inactivity backstop, so without an explicit
							// Stop check a cancelled attempt (which delivers neither done nor
							// error) spun here until the user navigated away.
							if (retryShouldExit()) {
								finalizeStoppedRetry();
								break;
							}
							const m = history.messages[message.id];
							if (m?.done || m?.error) break;
						}
					}
				}
				if (getVisibleChatId() !== retryChatId) break;

				suppressErrorToast = false;

				// Sync from reactive history back to _history
				const completedMsg = history.messages[message.id];
				if (completedMsg) {
					_history.messages[message.id] = cloneState(completedMsg);
				}
				responseMessage = _history.messages[message.id];

				if (!responseMessage.error) break;
				// A user Stop is terminal — never auto-retry it, even if a late
				// error event landed on the message after the cancel.
				if (retryShouldExit()) {
					finalizeStoppedRetry();
					break;
				}
				if (isNonRetryableChatGenerationError(responseMessage.error)) break;

				// Save tool context from failed attempt for next retry
				const failedToolContext = getRetryableToolContext(responseMessage.content);
				if (failedToolContext?.hasCompletedToolCall) {
					savedToolContent = failedToolContext.content;
					savedReasoningDetails = responseMessage.reasoning_details || null;
					savedReasoningDetailsPerRound = responseMessage.reasoning_details_per_round || null;
				}

				// Connectivity event, not a model failure — see the primary send
				// loop: no budget burn, wait holds while offline, re-send is safe
				// (backend dedupes by assistant message id).
				const networkFailure = isNetworkFetchError(responseMessage.error);

				// Reset the consecutive-failure counter whenever the agent made
				// forward progress (a new tool call completed) since the last failure.
				// Structured blocks first (v2.1); legacy content parse otherwise.
				const completedNow =
					countCompletedStructuredToolCalls(responseMessage) ||
					countCompletedToolCalls(responseMessage.content);
				if (networkFailure) {
					// leave the budget untouched
				} else if (lastCompletedToolCalls >= 0 && completedNow > lastCompletedToolCalls) {
					consecutiveNoProgress = 0;
				} else {
					consecutiveNoProgress += 1;
				}
				lastCompletedToolCalls = completedNow;

				if (consecutiveNoProgress < MAX_NO_PROGRESS && !skipRemainingRetriesSet.has(message.id)) {
					generationLifecycles.retry(message.id, generationId);
					const displayAttempt = Math.max(consecutiveNoProgress, 1);
					const waitSeconds = networkFailure ? 2 : Math.min(displayAttempt, 5) * 2;
					responseMessage.error = null;
					responseMessage.done = false;
					if (
						!Array.isArray(responseMessage.content_blocks) ||
						responseMessage.content_blocks.length === 0
					) {
						responseMessage.content = '';
					}
					responseMessage.retrying = {
						attempt: displayAttempt,
						maxAttempts: MAX_NO_PROGRESS,
						countdown: waitSeconds,
						...(networkFailure ? { reason: 'network' } : {})
					};
					_history.messages[message.id] = responseMessage;
					history.messages[message.id] = cloneState(responseMessage);
					history = { ...history };

					await new Promise((resolve) => {
						let remaining = waitSeconds;
						let heldTicks = 0;
						const ticker = setInterval(() => {
							// Stop first — the connectivity hold below returns ahead of every
							// other check, so it used to swallow the cancel entirely.
							if (retryShouldExit()) {
								clearInterval(ticker);
								resolve();
								return;
							}
							const offline = !$online && !$socket?.connected;
							if (networkFailure && offline && heldTicks < 300) {
								heldTicks++;
								responseMessage.retrying = {
									attempt: displayAttempt,
									maxAttempts: MAX_NO_PROGRESS,
									countdown: remaining,
									reason: 'network'
								};
								_history.messages[message.id] = responseMessage;
								history.messages[message.id] = cloneState(responseMessage);
								history = { ...history };
								return;
							}
							remaining--;
							if (remaining <= 0) {
								clearInterval(ticker);
								resolve();
								return;
							}
							responseMessage.retrying = {
								attempt: displayAttempt,
								maxAttempts: MAX_NO_PROGRESS,
								countdown: remaining,
								...(networkFailure ? { reason: 'network' } : {})
							};
							_history.messages[message.id] = responseMessage;
							history.messages[message.id] = cloneState(responseMessage);
							history = { ...history };
						}, 1000);
					});

					if (retryShouldExit()) {
						finalizeStoppedRetry();
						break;
					}
					continue;
				}
				break;
			}
			skipRemainingRetriesSet.delete(message.id);

			// Defensive cleanup mirrors the primary retry loop: never leave
			// `retrying` set after the loop exits, and never do it by republishing
			// this loop's detached snapshot (which would stomp a newer generation).
			{
				const liveMsg = history.messages[message.id];
				if (liveMsg?.retrying) {
					liveMsg.retrying = null;
					history.messages[message.id] = { ...liveMsg };
					history = { ...history };
				}
				const snapshotMsg = _history.messages[message.id];
				if (snapshotMsg?.retrying) snapshotMsg.retrying = null;
			}
		} finally {
			activeSendRetryLoops = Math.max(0, activeSendRetryLoops - 1);
			chatStreamDebug(
				'[chat-stream] retryWithoutProviderRestrictions finally — clearing controller',
				{
					messageId: message?.id
				}
			);
			const finalMessage = history.messages[message?.id];
			if (finalMessage?.done || finalMessage?.error || isUserStoppedMessageId(message?.id)) {
				settleGenerationLifecycle(message?.id);
			}
		}
	};

	const regenerateResponse = async (message, suggestionPrompt = null) => {
		console.log('regenerateResponse');

		if (history.currentId) {
			let userMessage = history.messages[message.parentId];

			if (autoScroll) {
				scrollToBottom();
			}

			await sendMessage(history, userMessage.id, {
				scrollBehavior: 'preserve',
				supersedeActiveTurn: true,
				...(suggestionPrompt
					? {
							messages: [
								...createMessagesList(history, message.id),
								{
									role: 'user',
									content: suggestionPrompt
								}
							]
						}
					: {}),
				...((userMessage?.models ?? [...selectedModels]).length > 1
					? {
							// If multiple models are selected, use the model from the message
							modelId: message.model,
							modelIdx: message.modelIdx
						}
					: {})
			});
		}
	};

	// Legacy-path-only (v1 body / temp chats): inline the kept prefix's lazy
	// tool-result bodies client-side so the messages array sent upstream carries
	// them. Persisted chats on the v2.1 protocol never call this — the backend
	// copies bodies row-to-row via `copy_tool_result_bodies_from` on the
	// append_message op and assembles the request from the persisted row.
	const hydrateLazyToolResultBodiesForRetry = async (
		message: any,
		context: any,
		chatId: string | null
	) => {
		if (!chatId || !context?.content_blocks) return null;
		const bodies: Record<string, any> = {};
		const fetches: Promise<void>[] = [];

		for (const block of context.content_blocks ?? []) {
			if (block?.type !== 'tool_calls' || !Array.isArray(block.results)) continue;
			for (const result of block.results) {
				if (!result?.result_ref || result?.content) continue;
				const toolCallId = result.tool_call_id || result.result_ref;
				fetches.push(
					getChatMessageToolResult(localStorage.token, chatId, message.id, toolCallId).then(
						(body) => {
							if (body) bodies[result.result_ref] = body;
						}
					)
				);
			}
		}

		if (fetches.length === 0) return null;
		await Promise.all(fetches);
		return Object.keys(bodies).length > 0 ? bodies : null;
	};

	// The slim chat-open projection ships the reasoning_details replay-context
	// fields only on the CURRENT LEAF (they averaged ~18KB per assistant message
	// and only matter when a turn becomes the base of a new branch). Rewind /
	// retry can target any finished message, so hydrate the fields on demand
	// from the siblings endpoint (which serves them leaf-slim) before building
	// the rebase context. Mutates the in-memory message so the append op below
	// carries the context onto the new sibling row.
	const ensureReasoningDetailsForRebase = async (message, _chatId) => {
		if (!message?.id || !_chatId || _chatId.startsWith('local:')) return;
		if (message.reasoning_details_per_round || message.reasoning_details) return;
		if (!Array.isArray(message?.content_blocks) || message.content_blocks.length === 0) return;
		try {
			const siblings = await getChatMessagesSiblings(localStorage.token, _chatId, message.id);
			const self = Array.isArray(siblings)
				? siblings.find((m) => (m?.id ?? m?.message_id) === message.id)
				: null;
			if (self?.reasoning_details_per_round) {
				message.reasoning_details_per_round = self.reasoning_details_per_round;
			}
			if (self?.reasoning_details) {
				message.reasoning_details = self.reasoning_details;
			}
		} catch (err) {
			// Non-fatal: the rebase proceeds without provider reasoning context
			// (the model simply re-reasons), same as pre-hydration behavior for
			// chats that never had these fields.
			console.error('Failed to hydrate reasoning context for rebase', err);
		}
	};

	type RetryFromLastRequestResult = 'started' | 'unavailable' | 'blocked';
	let retryFromLastRequestInFlight = false;

	const retryFromLastRequest = async (
		message,
		modelId: string
	): Promise<RetryFromLastRequestResult> => {
		if (retryFromLastRequestInFlight) {
			toast.info($i18n.t('Retry is already starting.'));
			return 'blocked';
		}
		if (!history.currentId) {
			return 'unavailable';
		}

		retryFromLastRequestInFlight = true;
		try {
			const _chatId = getVisibleChatId();
			const isTempChat = $temporaryChatEnabled || _chatId?.startsWith('local:');
			const targetModelId = modelId;
			const model = $models.find((m) => m.id === targetModelId);
			if (!model) {
				toast.error($i18n.t(`Model {{modelId}} not found`, { modelId: targetModelId }));
				return 'blocked';
			}

			if (!isTempChat) {
				await ensureReasoningDetailsForRebase(message, _chatId);
			}
			const structuredContext = getStructuredRetryLastRequestContext(message);
			// v2.1 + persisted chat: the server copies the kept tool-result bodies
			// row-to-row (`copy_tool_result_bodies_from` on the append op below) and
			// assembles the outbound request from the persisted row, so no client
			// round-trip is needed. See createRewindBranch for the full rationale.
			const serverCarriesToolBodies =
				($config as any)?.features?.stream_protocol_version === 'v2.1' && !isTempChat;
			let structuredToolResultBodies = null;
			if (structuredContext && !serverCarriesToolBodies) {
				try {
					structuredToolResultBodies = await hydrateLazyToolResultBodiesForRetry(
						message,
						structuredContext,
						_chatId
					);
				} catch (err) {
					console.error('Failed to hydrate lazy tool results for retry', err);
					toast.error($i18n.t('Failed to load tool result for retry.'));
					return 'blocked';
				}
			}
			const legacyToolContext = structuredContext
				? null
				: getRetryableToolContext(message?.content ?? '');
			if (!structuredContext && !legacyToolContext?.content) {
				return 'unavailable';
			}

			const responseMessageId = uuidv4();
			const responseMessage: any = {
				parentId: message.parentId,
				id: responseMessageId,
				childrenIds: [],
				role: 'assistant',
				content: structuredContext
					? structuredContext.content
					: `${legacyToolContext?.content ?? ''}\n\n`,
				model: targetModelId,
				modelName: model.name ?? targetModelId,
				modelIdx: message.modelIdx ?? 0,
				timestamp: Math.floor(Date.now() / 1000),
				...(structuredContext
					? {
							content_blocks: structuredContext.content_blocks,
							...(structuredToolResultBodies
								? { tool_result_bodies: structuredToolResultBodies }
								: {}),
							...(structuredContext.reasoning_details_per_round
								? { reasoning_details_per_round: structuredContext.reasoning_details_per_round }
								: {})
						}
					: {
							preservedToolContext: true,
							...(message.reasoning_details
								? { reasoning_details: message.reasoning_details }
								: {}),
							...(message.reasoning_details_per_round
								? { reasoning_details_per_round: message.reasoning_details_per_round }
								: {})
						})
			};
			const generation = prepareGenerationLifecycle(_chatId, responseMessage);

			history.messages[responseMessageId] = responseMessage;
			history.currentId = responseMessageId;

			if (message.parentId !== null && history.messages[message.parentId]) {
				history.messages[message.parentId].childrenIds = [
					...history.messages[message.parentId].childrenIds,
					responseMessageId
				];
			}

			history = history;

			if (_chatId && !isTempChat) {
				await saveChatHandler(_chatId, history, params, [
					{
						op: 'append_message',
						message_id: responseMessage.id,
						parent_id: responseMessage.parentId ?? null,
						role: 'assistant',
						content: responseMessage.content ?? '',
						model: responseMessage.model,
						modelName: responseMessage.modelName,
						modelIdx: responseMessage.modelIdx,
						generation_id: generation.generationId,
						turn_id: generation.turnId,
						timestamp: responseMessage.timestamp,
						...(responseMessage.content_blocks
							? {
									content_blocks: responseMessage.content_blocks,
									// Server-side row-to-row body carry (see createRewindBranch).
									copy_tool_result_bodies_from: message.id
								}
							: {}),
						...(responseMessage.reasoning_details_per_round
							? { reasoning_details_per_round: responseMessage.reasoning_details_per_round }
							: {}),
						...(responseMessage.tool_result_bodies
							? { tool_result_bodies: responseMessage.tool_result_bodies }
							: {}),
						...(responseMessage.reasoning_details
							? { reasoning_details: responseMessage.reasoning_details }
							: {}),
						...(responseMessage.preservedToolContext ? { preservedToolContext: true } : {})
					},
					{ op: 'set_history_current_id', current_id: responseMessage.id }
				]);
			}

			await tick();

			if (autoScroll) {
				scrollToBottom();
			}

			const messages = createMessagesList(history, responseMessageId);
			await sendMessageSocket(model, messages, history, responseMessageId, _chatId, {
				leafMessageId: structuredContext ? responseMessageId : undefined,
				scrollBehavior: 'preserve',
				generationId: generation.generationId,
				turnId: generation.turnId,
				supersedeActiveTurn: true
			});

			return 'started';
		} finally {
			retryFromLastRequestInFlight = false;
		}
	};

	// Re-entrancy guard: a rewind builds a sibling node, persists, and sends. Two
	// overlapping calls (double-click on a boundary, or two boundaries submitted in
	// quick succession) would race on history.currentId / parent.childrenIds / the
	// task registry, spawning competing siblings. Serialize them.
	let rewindInFlight = false;
	// Returns true only when the rewind actually committed a sibling and started
	// generating. EVERY failure path reports itself — a rewind that quietly
	// returned false left the user staring at an unchanged transcript with no
	// idea whether anything had been sent (the composer had already closed), so
	// "nothing happened" and "it's just slow" were indistinguishable. The caller
	// (RewindBoundary via ContentRenderer) keeps its composer open on false.
	const rewindAndInsert = async (message, cutIndex, steerText = '', modelId = null) => {
		if (rewindInFlight) return false;
		rewindInFlight = true;
		try {
			// Honor the model picker: a rewind is a user-initiated new request, so it
			// must run on the CURRENTLY selected model (like a fresh send), not the
			// model that originally produced the turn — otherwise "switch to a bigger
			// model and redo from here" silently re-runs on the old model, and the
			// sibling persists the old model id so later retries inherit it too.
			// createRewindBranch still falls back to message.model when the picker is
			// empty; the rewind-&-redo-subagent flow deliberately bypasses this and
			// keeps the original model (it re-runs the SAME turn, not a new request).
			if (!modelId) {
				modelId =
					atSelectedModel?.id ||
					selectedModels[message?.modelIdx ?? 0] ||
					selectedModels.find((id) => id) ||
					null;
			}
			return await _rewindAndInsertImpl(message, cutIndex, steerText, modelId);
		} catch (error) {
			// createRewindBranch persists before it resumes, so a failed PATCH (the
			// common case on a flaky mobile link) used to throw straight through an
			// un-awaited call into an unhandled rejection: no toast, no console
			// trail the user would ever see, composer already gone.
			console.error('rewind failed', error);
			toast.error(
				$i18n.t('Could not rewind: {{error}}', {
					error: (error as any)?.detail ?? (error as any)?.message ?? `${error}`
				})
			);
			return false;
		} finally {
			rewindInFlight = false;
		}
	};
	const _rewindAndInsertImpl = async (message, cutIndex, steerText = '', modelId = null) => {
		// Block-level rewind: keep this assistant turn's content_blocks up to
		// `cutIndex`, optionally inject a user message at that boundary, and resume
		// generation inline — as a NEW SIBLING node so the full original survives as
		// a navigable branch (< 2/2 >). Generalizes retryFromLastRequest (which
		// auto-picks the last completed tool boundary) to a user-chosen cut + a
		// user_steer block. See plan: silly-rolling-bird.md.
		const branch = await createRewindBranch(message, cutIndex, steerText, modelId);
		if (!branch) return false;

		const messages = createMessagesList(history, branch.responseMessageId);
		await sendMessageSocket(
			branch.model,
			messages,
			history,
			branch.responseMessageId,
			branch._chatId,
			{
				leafMessageId: branch.responseMessageId,
				scrollBehavior: 'preserve',
				generationId: branch.generationId,
				turnId: branch.turnId,
				supersedeActiveTurn: true
			}
		);

		return true;
	};

	// Create the rewound SIBLING branch (everything _rewindAndInsertImpl does up to
	// and including persistence) WITHOUT resuming generation. Returns
	// `{ responseMessageId, model, _chatId, isTempChat }` so the caller decides when
	// (or whether) to `sendMessageSocket`. The "rewind & redo subagent" flow uses
	// this to interpose a subagent redo between branch creation and the parent
	// resume; the plain rewind UI resumes immediately via _rewindAndInsertImpl.
	const createRewindBranch = async (message, cutIndex, steerText = '', modelId = null) => {
		if (!history.currentId || !message?.id) {
			toast.error($i18n.t('Rewind is only available on structured responses.'));
			return null;
		}
		if (!Array.isArray(message?.content_blocks) || message.content_blocks.length === 0) {
			toast.error($i18n.t('Rewind is only available on structured responses.'));
			return null;
		}

		const targetModelId = modelId ?? message?.selectedModelId ?? message?.model;
		const model = $models.find((m) => m.id === targetModelId);
		if (!model) {
			toast.error($i18n.t(`Model {{modelId}} not found`, { modelId: targetModelId }));
			return null;
		}

		const _chatId = getVisibleChatId();
		const isTempChat = $temporaryChatEnabled || _chatId?.startsWith('local:');

		// Rewind is offered ONLY on finished/stopped turns (ResponseMessage gates
		// onRewind on message.done === true), so there is never a live generation to
		// stop here — which structurally removes the live-stop races (late
		// chat:tasks:cancel marking the sibling done, leftover stop latches). Guard
		// defensively in case this is ever called programmatically on a live turn.
		if (message?.done !== true) {
			toast.error($i18n.t('Wait for this response to finish before rewinding.'));
			return null;
		}

		if (!isTempChat) {
			await ensureReasoningDetailsForRebase(message, _chatId);
		}
		const ctx = getRewindContext(message, cutIndex, steerText);
		if (!ctx) {
			return null;
		}

		// Tool-result bodies for the kept prefix: on the v2.1 protocol the server
		// assembles the outbound request from the persisted sibling row, so the
		// bodies never need to pass through this client at all — the append op
		// below carries `copy_tool_result_bodies_from` and the backend copies
		// them row-to-row from the original message. Fetching them here (one GET
		// per kept tool call, each detoasting the original row's full body map)
		// then re-uploading them made every rewind take seconds on agentic turns.
		// Only the legacy v1 body (temp/local chats, pre-v2.1 backends) still
		// sends client-side messages upstream and needs the bodies inlined here.
		const serverCarriesToolBodies =
			($config as any)?.features?.stream_protocol_version === 'v2.1' && !isTempChat;
		let structuredToolResultBodies = null;
		if (!serverCarriesToolBodies) {
			try {
				structuredToolResultBodies = await hydrateLazyToolResultBodiesForRetry(
					message,
					ctx,
					_chatId
				);
			} catch (err) {
				console.error('Failed to hydrate lazy tool results for rewind', err);
				toast.error($i18n.t('Failed to load tool result for retry.'));
				return null;
			}
		}

		// Carry only the surviving tool calls' subagent runs so kept subagent cards
		// render faithfully; dropping the cut ones avoids resurrecting "[No output…]"
		// for calls that no longer exist after the cut. Match the backend's recovery
		// keying (messages.py _subagent_final_text_lookup): runs are recoverable BOTH
		// by tool_call_id AND by subagent_id — a run whose tool_call_id is empty/
		// missing (legacy / continuation entries) is only matchable by subagent_id, so
		// keying solely on tool_call_id would drop it and the resumed turn would send
		// "[No output…]" for a subagent that actually finished.
		const keptToolCallIds = new Set<string>();
		const keptSubagentIds = new Set<string>();
		for (const block of ctx.content_blocks) {
			if (block?.type !== 'tool_calls') continue;
			for (const call of block.content ?? []) {
				const id = call?.id ?? call?.tool_call_id;
				if (id) keptToolCallIds.add(id);
			}
			for (const result of block.results ?? []) {
				const sid = result?.subagent_id;
				if (sid) keptSubagentIds.add(sid);
			}
		}
		let survivingSubagentRuns: Record<string, any> | null = null;
		if (message?.subagent_runs && typeof message.subagent_runs === 'object') {
			const filtered: Record<string, any> = {};
			for (const [key, run] of Object.entries(message.subagent_runs as Record<string, any>)) {
				if (!run || typeof run !== 'object') continue;
				const tcid = (run as any).tool_call_id;
				const sid = (run as any).subagent_id;
				if ((tcid && keptToolCallIds.has(tcid)) || (sid && keptSubagentIds.has(sid))) {
					filtered[key] = run;
				}
			}
			if (Object.keys(filtered).length > 0) survivingSubagentRuns = filtered;
		}

		const responseMessageId = uuidv4();
		if (survivingSubagentRuns) {
			survivingSubagentRuns = Object.fromEntries(
				Object.entries(survivingSubagentRuns).map(([key, run]) => [
					key,
					{ ...(run as any), parent_message_id: responseMessageId }
				])
			);
		}
		const responseMessage: any = {
			parentId: message.parentId,
			id: responseMessageId,
			childrenIds: [],
			role: 'assistant',
			content: ctx.content,
			model: targetModelId,
			modelName: model.name ?? targetModelId,
			modelIdx: message.modelIdx ?? 0,
			timestamp: Math.floor(Date.now() / 1000),
			content_blocks: ctx.content_blocks,
			...(structuredToolResultBodies ? { tool_result_bodies: structuredToolResultBodies } : {}),
			...(survivingSubagentRuns ? { subagent_runs: survivingSubagentRuns } : {}),
			...(ctx.reasoning_details_per_round
				? { reasoning_details_per_round: ctx.reasoning_details_per_round }
				: {})
		};
		const generation = prepareGenerationLifecycle(_chatId, responseMessage);

		// Preserve the reader's viewport across the sibling swap. Replacing the tall
		// original node with the truncated sibling collapses the container's
		// scrollHeight mid-render, so the browser clamps scrollTop toward 0 and the
		// reader lands at the top of the chat. Anchor to the swapped node's own top:
		// M' occupies M's slot and renders the identical kept prefix, so aligning
		// M'.top to where M.top sat keeps the boundary region fixed on screen. Same
		// pattern as Messages.svelte's deleteMessage anchor restore.
		const scrollContainer = document.getElementById('messages-container');
		const prevScrollTop = scrollContainer?.scrollTop ?? 0;
		const prevScrollHeight = scrollContainer?.scrollHeight ?? 0;
		const swappedEl = document.getElementById(`message-${message.id}`);
		const swappedTopBefore = swappedEl ? swappedEl.getBoundingClientRect().top : null;

		history.messages[responseMessageId] = responseMessage;
		history.currentId = responseMessageId;

		if (message.parentId !== null && history.messages[message.parentId]) {
			history.messages[message.parentId].childrenIds = [
				...history.messages[message.parentId].childrenIds,
				responseMessageId
			];
		}

		history = history;

		if (_chatId && !isTempChat) {
			await saveChatHandler(_chatId, history, params, [
				{
					op: 'append_message',
					message_id: responseMessage.id,
					parent_id: responseMessage.parentId ?? null,
					role: 'assistant',
					content: responseMessage.content ?? '',
					model: responseMessage.model,
					modelName: responseMessage.modelName,
					modelIdx: responseMessage.modelIdx,
					generation_id: generation.generationId,
					turn_id: generation.turnId,
					timestamp: responseMessage.timestamp,
					content_blocks: responseMessage.content_blocks,
					// Bodies are copied server-side from the original row (pruned to
					// the kept refs) — see serverCarriesToolBodies above.
					copy_tool_result_bodies_from: message.id,
					...(responseMessage.reasoning_details_per_round
						? { reasoning_details_per_round: responseMessage.reasoning_details_per_round }
						: {}),
					...(responseMessage.tool_result_bodies
						? { tool_result_bodies: responseMessage.tool_result_bodies }
						: {}),
					...(responseMessage.subagent_runs ? { subagent_runs: responseMessage.subagent_runs } : {})
				},
				{ op: 'set_history_current_id', current_id: responseMessage.id }
			]);
		}

		await tick();

		if (autoScroll) {
			// Following the bottom — keep following onto the fresh sibling.
			scrollToBottom();
		} else if (scrollContainer) {
			// Reading up at the boundary — undo whatever clamp/shift the swap caused.
			// autoScroll is gesture-owned intent; never rewrite it here.
			const newEl = document.getElementById(`message-${responseMessageId}`);
			if (swappedTopBefore !== null && newEl) {
				scrollContainer.scrollTop += newEl.getBoundingClientRect().top - swappedTopBefore;
			} else {
				scrollContainer.scrollTop =
					prevScrollTop + (scrollContainer.scrollHeight - prevScrollHeight);
			}
		}

		return {
			responseMessageId,
			model,
			_chatId,
			isTempChat,
			generationId: generation.generationId,
			turnId: generation.turnId
		};
	};

	// Merge one backend-authoritative adopted subagent answer into the visible
	// history without replacing sibling runs/results. Persisted v2.1 generation
	// already reads the corrected row from the server; this mirror keeps display
	// and the legacy client-assembled request path equally correct.
	const applyAdoptedSubagentResult = (adopted: any) => {
		const parentMessageId = adopted?.parent_message_id;
		const run = adopted?.run;
		if (!parentMessageId || !run || !history.messages?.[parentMessageId]) return;

		const parentMessage = history.messages[parentMessageId];
		const entryKey =
			adopted?.entry_key || run.entry_key || run.subagent_id || run.chat_id || run.tool_call_id;
		if (!entryKey) return;

		const existingRuns =
			parentMessage.subagent_runs && typeof parentMessage.subagent_runs === 'object'
				? parentMessage.subagent_runs
				: {};
		const normalizedRun = {
			...(existingRuns[entryKey] ?? {}),
			...run,
			entry_key: entryKey,
			parent_message_id: parentMessageId,
			status: 'done',
			error: null
		};

		const blocks = Array.isArray(parentMessage.content_blocks)
			? cloneState(parentMessage.content_blocks)
			: [];
		const toolCallId = normalizedRun.tool_call_id;
		for (const block of blocks) {
			if (block?.type !== 'tool_calls') continue;
			const calls = Array.isArray(block.content) ? block.content : [];
			if (!calls.some((call: any) => (call?.id ?? call?.tool_call_id ?? '') === toolCallId)) {
				continue;
			}
			const results = Array.isArray(block.results) ? [...block.results] : [];
			const idx = results.findIndex((result: any) => result?.tool_call_id === toolCallId);
			const replacement: any = {
				...(idx >= 0 ? results[idx] : {}),
				tool_call_id: toolCallId,
				subagent_id: normalizedRun.subagent_id || normalizedRun.chat_id || '',
				content: normalizedRun.final_text || ''
			};
			delete replacement.error;
			delete replacement.error_reason;
			if (idx >= 0) results[idx] = replacement;
			else results.push(replacement);
			block.results = results;
			break;
		}

		history = {
			...history,
			messages: {
				...history.messages,
				[parentMessageId]: {
					...parentMessage,
					subagent_runs: {
						...existingRuns,
						[entryKey]: normalizedRun
					},
					...(blocks.length > 0 ? { content_blocks: blocks } : {})
				}
			}
		};

		subagentLiveStates.update((states) => {
			const out = { ...states };
			const aliases = [
				entryKey,
				toolCallId,
				normalizedRun.subagent_id,
				normalizedRun.chat_id
			].filter(Boolean);
			const current = findSubagentRunEntry(states, parentMessageId, aliases)?.[1] ?? {};
			const next = { ...current, ...normalizedRun, live: false };
			setSubagentRunAliases(out, next, aliases, parentMessageId);
			return out;
		});
	};

	const captureRewindSwapAnchor = (messageId: string) => {
		const scrollContainer = document.getElementById('messages-container');
		const swappedElement = document.getElementById(`message-${messageId}`);
		return {
			scrollContainer,
			previousScrollTop: scrollContainer?.scrollTop ?? 0,
			previousScrollHeight: scrollContainer?.scrollHeight ?? 0,
			swappedTopBefore: swappedElement ? swappedElement.getBoundingClientRect().top : null
		};
	};

	// Install a backend-committed rewind sibling into this tab. Adoption and
	// rerun use the same atomic graph primitive server-side and must mirror it
	// identically client-side; keeping one installer prevents their branch/cache/
	// viewport behavior from drifting apart.
	const installCommittedRewindBranch = async (
		committed: any,
		sourceMessage: any,
		chatId: string,
		anchor: ReturnType<typeof captureRewindSwapAnchor>
	) => {
		const responseMessage = committed?.branch_message;
		const messageId = String(committed?.parent_message_id || responseMessage?.id || '');
		if (!responseMessage || !messageId) {
			throw new Error('The rewound branch was committed without a message body.');
		}

		const parentId: string | null = responseMessage.parentId ?? sourceMessage.parentId ?? null;
		const nextMessages: Record<string, any> = {
			...history.messages,
			[messageId]: {
				...responseMessage,
				id: messageId,
				parentId,
				childrenIds: Array.isArray(responseMessage.childrenIds) ? responseMessage.childrenIds : []
			}
		};
		if (parentId && nextMessages[parentId]) {
			nextMessages[parentId] = {
				...nextMessages[parentId],
				childrenIds: [...new Set([...(nextMessages[parentId].childrenIds ?? []), messageId])]
			};
		}
		history = {
			...history,
			messages: nextMessages,
			currentId: messageId
		};

		resetSaveSnapshot(chatId);
		invalidateChatOpenCache(chatId);
		const savedUserId = $user?.id;
		if (savedUserId) {
			handleLocalChatSaved(localStorage.token, savedUserId, chatId);
		}
		if (committed.updated_at != null) {
			patchSidebarUpdatedAt(chatId, committed.updated_at);
			invalidateFolderChatLists([chat?.folder_id], 'chat:updated:origin');
		}

		await tick();
		if (autoScroll) {
			scrollToBottom();
		} else if (anchor.scrollContainer) {
			const newElement = document.getElementById(`message-${messageId}`);
			if (anchor.swappedTopBefore !== null && newElement) {
				anchor.scrollContainer.scrollTop +=
					newElement.getBoundingClientRect().top - anchor.swappedTopBefore;
			} else {
				anchor.scrollContainer.scrollTop =
					anchor.previousScrollTop +
					(anchor.scrollContainer.scrollHeight - anchor.previousScrollHeight);
			}
		}

		return { responseMessage, messageId };
	};

	// "Rewind & use latest": import manually repaired hidden-chat branches.
	// The backend commits the sibling + ALL selected run/result replacements in
	// one guarded transaction. Nothing is installed locally and parent generation
	// never starts unless that complete durable commit succeeds.
	const rewindAdoptSubagents = async (detail: any) => {
		if (rewindRedoInFlight || rewindInFlight) return;

		const parentMessageId = detail?.parentMessageId;
		let entries: any[] = Array.isArray(detail?.entries) ? detail.entries : [];
		entries = entries
			.map((entry) => ({
				entryKey: String(entry?.entryKey ?? ''),
				toolCallId: String(entry?.toolCallId ?? ''),
				subagentId: String(entry?.subagentId ?? '')
			}))
			.filter((entry) => entry.entryKey);
		entries = [...new Map(entries.map((entry) => [entry.entryKey, entry])).values()];
		if (!parentMessageId || entries.length === 0) return;

		const _chatId = getVisibleChatId();
		if (!_chatId || _chatId.startsWith('local:')) {
			toast.error($i18n.t('Cannot use repaired subagent answers: no saved parent chat.'));
			return;
		}
		const message = history.messages[parentMessageId];
		if (!message) {
			toast.error($i18n.t('The main agent message is no longer loaded.'));
			return;
		}
		if (message.done !== true) {
			toast.error($i18n.t('Stop the main agent before using a repaired subagent answer.'));
			return;
		}
		if (generating) {
			toast.error($i18n.t('The main agent is already running.'));
			return;
		}

		let cut = -1;
		const located: any[] = [];
		const cuts: number[] = [];
		for (const entry of entries) {
			const entryCut = getSubagentToolCallCutIndex(message, {
				tool_call_id: entry.toolCallId,
				subagent_id: entry.subagentId,
				entry_key: entry.entryKey
			});
			if (entryCut < 0) continue;
			located.push(entry);
			cuts.push(entryCut);
			if (entryCut > cut) cut = entryCut;
		}
		if (cut < 0 || located.length === 0) {
			toast.error(
				$i18n.t("Cannot use subagent answer: couldn't locate its tool call in the main turn.")
			);
			return;
		}
		if (!canBatchSubagentToolCallCuts(message, cuts)) {
			toast.error($i18n.t('Those subagents ran in different rounds — use them one at a time.'));
			return;
		}

		rewindRedoInFlight = true;
		rewindInFlight = true;
		const myGeneration = navigateGeneration;
		const operationId = uuidv4();
		const branchMessageId = uuidv4();
		const targetModelId = message?.selectedModelId ?? message?.model;
		const model = $models.find((candidate) => candidate.id === targetModelId);
		if (!model) {
			rewindInFlight = false;
			rewindRedoInFlight = false;
			toast.error($i18n.t(`Model {{modelId}} not found`, { modelId: targetModelId }));
			return;
		}

		// Preserve the same visual anchor as ordinary rewind. The server call does
		// not mutate this tab until it returns a fully committed sibling.
		const anchor = captureRewindSwapAnchor(message.id);

		markOfflineRaceDirty(_chatId);
		try {
			const committed = await rewindAdoptSubagentResults(localStorage.token, {
				parent_chat_id: _chatId,
				source_parent_message_id: parentMessageId,
				branch_message_id: branchMessageId,
				entry_keys: located.map((entry) => entry.entryKey),
				operation_id: operationId
			});
			if (myGeneration !== navigateGeneration || getVisibleChatId() !== _chatId) return;

			const { messageId: mPrimeId } = await installCommittedRewindBranch(
				committed,
				message,
				_chatId,
				anchor
			);

			for (const adopted of committed.adoptions ?? []) {
				applyAdoptedSubagentResult(adopted);
			}

			// The atomic checkpoint is durably `done:true` so a browser crash
			// cannot leave a phantom running parent. This tab owns the actual resume
			// now; flip only its in-memory copy before launching the generation.
			if (history.messages[mPrimeId]) {
				history.messages[mPrimeId] = {
					...history.messages[mPrimeId],
					done: false,
					error: null,
					userStopped: false
				};
				history = { ...history };
			}
			const messages = createMessagesList(history, mPrimeId);
			await sendMessageSocket(model, messages, history, mPrimeId, _chatId, {
				leafMessageId: mPrimeId,
				scrollBehavior: 'preserve',
				supersedeActiveTurn: true
			});
		} catch (error: any) {
			console.error('rewind & use latest subagent answer failed', error);
			const messageText = error?.message ?? error?.detail?.message ?? error?.detail ?? error;
			toast.error(`${messageText}`);
		} finally {
			unmarkOfflineRaceDirty(_chatId);
			rewindInFlight = false;
			rewindRedoInFlight = false;
		}
	};

	// "Rewind & redo subagent(s)": the user clicked redo on a subagent card but the
	// parent model already continued past that result (HTTP 409
	// `subagent_parent_moved_on`), so an in-place rewrite would corrupt the parent
	// transcript (closed providers' signed reasoning refers to the old result).
	// Instead: rewind the parent to ONE fresh SIBLING branch M' that ends right
	// after the selected subagents' tool-call block(s) (so the redo's
	// unconsumed-guard passes against M'), redo every SELECTED subagent against M'
	// in parallel, then — once they have ALL finished — resume the parent once from
	// the fresh results. Supports a parallel fan-out: redo several subagents from
	// the same turn together. The sibling is committed by the same guarded atomic
	// graph primitive as rewind-adopt; rerunSubagent then owns each child redo and
	// the task-registry barrier (subagent_rerun_entry_keys) gates parent resume.
	// Triggered by SubagentBlock's bubbling `subagent:rewind-redo` CustomEvent,
	// whose detail carries { parentMessageId, scope, entries:[{entryKey,toolCallId,subagentId,scope?}] }.
	let rewindRedoInFlight = false;
	const rewindRedoSubagent = async (detail) => {
		// Serialize against itself AND the plain rewind path (both mint sibling
		// branches + drive the task registry) so a double-dispatch can't spawn
		// competing branches/resumes.
		if (rewindRedoInFlight || rewindInFlight) return;

		const parentMessageId = detail?.parentMessageId;
		const scope = detail?.scope === 'from_launch' ? 'from_launch' : 'this_turn';
		// Normalize to a list of entries (back-compat with a single {entryKey,...}).
		let entries: any[] = Array.isArray(detail?.entries) ? detail.entries : [];
		if (entries.length === 0 && detail?.entryKey) {
			entries = [
				{ entryKey: detail.entryKey, toolCallId: detail.toolCallId, subagentId: detail.subagentId }
			];
		}
		entries = entries
			.map((e) => ({
				entryKey: String(e?.entryKey ?? ''),
				toolCallId: String(e?.toolCallId ?? ''),
				subagentId: String(e?.subagentId ?? ''),
				scope: e?.scope === 'from_launch' ? 'from_launch' : scope
			}))
			.filter((e) => e.entryKey);
		// De-dupe by entryKey.
		entries = [...new Map(entries.map((e) => [e.entryKey, e])).values()];
		if (!parentMessageId || entries.length === 0) return;

		const _chatId = getVisibleChatId();
		if (!_chatId || _chatId.startsWith('local:')) {
			toast.error($i18n.t('Cannot redo subagent: no saved parent chat.'));
			return;
		}
		const message = history.messages[parentMessageId];
		if (!message) {
			toast.error($i18n.t('Cannot redo subagent: the main agent message is no longer loaded.'));
			return;
		}
		if (message.done !== true) {
			toast.error($i18n.t('Stop the main agent before redoing this subagent.'));
			return;
		}
		if (generating) {
			toast.error($i18n.t('The main agent is already running.'));
			return;
		}

		// Locate each selected subagent's tool-call block in the ORIGINAL (moved-on)
		// parent message; the branch cut is AFTER the latest of them so every
		// selected subagent (and any in-between sibling) is preserved in M'. Drop
		// entries we can't locate.
		let cut = -1;
		const located: any[] = [];
		const cutByKey = new Map<string, number>();
		for (const e of entries) {
			const c = getSubagentToolCallCutIndex(message, {
				tool_call_id: e.toolCallId,
				subagent_id: e.subagentId,
				entry_key: e.entryKey
			});
			if (c < 0) continue;
			located.push(e);
			cutByKey.set(e.entryKey, c);
			if (c > cut) cut = c;
		}
		if (cut < 0 || located.length === 0) {
			toast.error(
				$i18n.t("Cannot redo subagent: couldn't locate its tool call in the main agent's turn.")
			);
			return;
		}
		// Batch only when the selected subagents are in one tool-call round OR in
		// adjacent pure-subagent fanout blocks with no parent text/reasoning/non-
		// subagent tool output between them. This mirrors the backend guard: pure
		// sibling subagent blocks do not consume one another's results; parent output
		// does.
		if (!canBatchSubagentToolCallCuts(message, [...cutByKey.values()])) {
			toast.error($i18n.t('Those subagents ran in different rounds — redo them one at a time.'));
			return;
		}
		// Drop any selected entries we couldn't locate so the resume gate can fail
		// closed over exactly the subagents we will actually redo.
		entries = located;

		const sessionId = $socket?.id;
		if (!sessionId) {
			toast.error($i18n.t('Cannot redo subagent: no socket session.'));
			return;
		}

		const liveFor = (e: any, targetParentMessageId = parentMessageId) =>
			findSubagentRunEntry(get(subagentLiveStates), targetParentMessageId, [
				e.entryKey,
				e.toolCallId,
				e.subagentId
			])?.[1] ?? null;
		const isTerminal = (st: any) => st === 'done' || st === 'error' || st === 'cancelled';
		// Legacy freshness fallback for a server that does not return rerun_id.
		// Current servers give every detached rerun a stable generation id, which
		// is stronger than a second-resolution ended_at comparison.
		const priorEndedAt = new Map<string, any>();
		for (const e of located) priorEndedAt.set(e.entryKey, liveFor(e)?.ended_at ?? null);

		rewindRedoInFlight = true;
		rewindInFlight = true;
		const myGeneration = navigateGeneration;
		const operationId = uuidv4();
		const branchMessageId = uuidv4();
		const targetModelId = message?.selectedModelId ?? message?.model;
		const branchModel = $models.find((candidate) => candidate.id === targetModelId);
		if (!branchModel) {
			rewindInFlight = false;
			rewindRedoInFlight = false;
			toast.error($i18n.t(`Model {{modelId}} not found`, { modelId: targetModelId }));
			return;
		}
		const anchor = captureRewindSwapAnchor(message.id);
		markOfflineRaceDirty(_chatId);
		try {
			// 1) Atomically append and select one sibling M' ending after the
			//    selected fan-out — WITHOUT resuming generation yet. A failed
			//    preflight/commit changes neither the DB nor this tab.
			const committed = await rewindSubagentsForRerun(localStorage.token, {
				parent_chat_id: _chatId,
				source_parent_message_id: parentMessageId,
				branch_message_id: branchMessageId,
				entry_keys: located.map((entry) => entry.entryKey),
				operation_id: operationId
			});
			if (myGeneration !== navigateGeneration || getVisibleChatId() !== _chatId) return;
			const { messageId: mPrimeId } = await installCommittedRewindBranch(
				committed,
				message,
				_chatId,
				anchor
			);

			// 2) Redo every selected subagent against M' in parallel (each is an
			//    independent subagent / hidden chat; the backend's per-subagent CAS +
			//    atomic per-key subagent_runs write make concurrent reruns on one
			//    message safe). Keep only the ones whose rerun actually launched.
			const fired: any[] = [];
			await Promise.all(
				located.map(async (e) => {
					try {
						const rerunRes = await rerunSubagent(localStorage.token, {
							parent_chat_id: _chatId,
							parent_message_id: mPrimeId,
							session_id: sessionId,
							entry_key: e.entryKey,
							scope: e.scope
						});
						fired.push({
							...e,
							taskId: rerunRes?.task_id,
							rerunId: rerunRes?.rerun_id
						});
					} catch (err: any) {
						console.error('rewind & redo: rerun failed for', e.entryKey, err);
						toast.error(`${err?.message ?? err?.detail ?? err}`);
					}
				})
			);
			if (fired.length === 0) {
				// None launched; M' survives as a branch carrying the prior answers.
				return;
			}

			// Optimistically flip each fired card to running so the spinners show
			// while the backend's chat:subagent:start events are in flight.
			const flippedKeys = new Set<string>();
			subagentLiveStates.update((s) => {
				const out = { ...s };
				for (const e of fired) {
					const aliases = [e.entryKey, e.toolCallId, e.subagentId].filter(Boolean);
					const cur =
						findSubagentRunEntry(s, mPrimeId, aliases)?.[1] ||
						findSubagentRunEntry(s, parentMessageId, aliases)?.[1] ||
						null;
					if (!cur) continue;
					if (!shouldApplyRerunOptimisticState(cur, e.rerunId)) {
						// Socket terminal beat the HTTP response/optimistic pass.
						continue;
					}
					flippedKeys.add(e.entryKey);
					const next: any = {
						...cur,
						parent_message_id: mPrimeId,
						rerun: true,
						rerun_task_id: e.taskId,
						rerun_id: e.rerunId,
						status: 'running',
						live: true,
						content_blocks: [],
						content: '',
						previous_final_text: cur.final_text || cur.previous_final_text,
						final_text: undefined,
						error: undefined,
						stale: false,
						started_at: Math.floor(Date.now() / 1000),
						ended_at: undefined
					};
					const keys = [
						e.entryKey,
						cur.tool_call_id,
						cur.subagent_id,
						cur.chat_id,
						cur.entry_key
					].filter(Boolean);
					setSubagentRunAliases(out, next, keys, mPrimeId);
				}
				return out;
			});

			// 3) Barrier: wait for ALL fired reruns to finish. A rerun is finished
			//    when its task leaves the registry (subagent_rerun_entry_keys, seen
			//    then gone — authoritative even if a socket terminal is missed) OR its
			//    flipped card reaches a terminal status. `unseenPolls` covers reruns
			//    that fail/block so fast they never register (the task registers
			//    synchronously at creation, so never seeing ANY after a few polls means
			//    they already finished). No wall-clock cap: long-running subagents should
			//    keep working until they finish or the user navigates/stops them.
			const firedKeys = fired.map((e) => e.entryKey);
			const seen = new Set<string>();
			let unseenPolls = 0;
			while (true) {
				await new Promise((r) => setTimeout(r, 2000));
				if (myGeneration !== navigateGeneration || getVisibleChatId() !== _chatId) {
					return; // navigated away — don't resume into a stale view
				}
				// Stop applies to the PARENT resume this barrier exists to perform.
				// Detached subagent redos are deliberately outside the chat Stop's
				// reach, but without this check the barrier polled on regardless and
				// then started a whole new parent generation the user had cancelled.
				if (isUserStoppedMessageId(mPrimeId)) {
					return;
				}
				const taskRes = await getChatWorkState(localStorage.token, _chatId).catch(() => null);
				const rerunKeys = new Set<string>(taskRes?.subagent_rerun_entry_keys ?? []);
				for (const k of firedKeys) if (rerunKeys.has(k)) seen.add(k);
				if (seen.size === 0) {
					if (++unseenPolls >= 5) {
						break;
					}
					continue;
				}
				unseenPolls = 0;
				const done = fired.every((e) => {
					const goneFromRegistry = seen.has(e.entryKey) && !rerunKeys.has(e.entryKey);
					const cardTerminal =
						flippedKeys.has(e.entryKey) && isTerminal(liveFor(e, mPrimeId)?.status);
					return goneFromRegistry || cardTerminal;
				});
				if (done) {
					break;
				}
			}

			// 4) Read the authoritative outcome of each fired redo from M'.subagent_runs
			//    and refresh the in-memory copy. Match by tool_call_id / subagent_id
			//    (entryKey may be an alias, not the canonical dict key).
			const resolvedByKey = new Map<string, any>();
			try {
				const refetched = await getChatByIdTail(localStorage.token, _chatId);
				const refMsg = refetched?.chat?.history?.messages?.[mPrimeId];
				if (refMsg) {
					const runs = refMsg.subagent_runs;
					if (runs && typeof runs === 'object') {
						for (const e of fired) {
							const r =
								runs[e.entryKey] ??
								Object.values(runs).find(
									(rr: any) =>
										rr &&
										((e.toolCallId && rr.tool_call_id === e.toolCallId) ||
											(e.subagentId && rr.subagent_id === e.subagentId))
								) ??
								null;
							if (r) resolvedByKey.set(e.entryKey, r);
						}
						if (history.messages[mPrimeId]) history.messages[mPrimeId].subagent_runs = runs;
					}
					if (Array.isArray(refMsg.content_blocks) && history.messages[mPrimeId]) {
						history.messages[mPrimeId].content_blocks = refMsg.content_blocks;
					}
					history = history;
				}
			} catch (err) {
				console.error('rewind & redo: outcome refetch failed', err);
			}
			for (const e of fired) {
				if (!resolvedByKey.has(e.entryKey)) {
					const lv = liveFor(e, mPrimeId);
					if (lv) resolvedByKey.set(e.entryKey, lv);
				}
			}

			if (
				myGeneration !== navigateGeneration ||
				getVisibleChatId() !== _chatId ||
				isUserStoppedMessageId(mPrimeId)
			) {
				return;
			}

			// 5) FAIL-CLOSED resume gate over the user's full SELECTION (`located`),
			//    not just the reruns that launched (`fired`): resume ONLY when EVERY
			//    selected subagent produced a FRESH terminal answer — status 'done' AND
			//    the exact rerun generation id returned by its launch. A selected redo
			//    that failed to even launch (transient error / race) would otherwise be
			//    silently dropped and the parent resumed from M' carrying its STALE
			//    result, contradicting the selection. Any not-fresh ⇒ don't resume; the
			//    branch stays for the user to retry.
			const notFresh = located.filter((e) => {
				const r = resolvedByKey.get(e.entryKey);
				return !isFreshRerunResult(r, e.rerunId, priorEndedAt.get(e.entryKey));
			});
			if (notFresh.length > 0) {
				toast.error(
					$i18n.t('{{n}} subagent redo(s) did not complete; the main agent was not resumed.', {
						n: notFresh.length
					})
				);
				return; // leave M' as a branch; user can retry or continue manually
			}

			// 6) Resume the parent from M' with the fresh subagent results. The backend
			//    re-seeds content_blocks from the persisted M' row and the read-path
			//    reconcile (_subagent_final_text_lookup) feeds the new final_texts.
			await tick();
			const messages = createMessagesList(history, mPrimeId);
			await sendMessageSocket(branchModel, messages, history, mPrimeId, _chatId, {
				leafMessageId: mPrimeId,
				scrollBehavior: 'preserve',
				supersedeActiveTurn: true
			});
		} catch (err: any) {
			console.error('rewind & redo subagent failed', err);
			toast.error(`${err?.message ?? err}`);
		} finally {
			unmarkOfflineRaceDirty(_chatId);
			rewindInFlight = false;
			rewindRedoInFlight = false;
		}
	};

	const regenerateWithModel = async (message, newModelId, preserveToolContext = false) => {
		console.log('regenerateWithModel', message, newModelId, preserveToolContext);

		if (!history.currentId) {
			return;
		}

		let userMessage = history.messages[message.parentId];

		if (preserveToolContext) {
			const retryResult = await retryFromLastRequest(message, newModelId);
			if (retryResult !== 'unavailable') {
				return;
			}
		}

		if (autoScroll) {
			scrollToBottom();
		}

		await sendMessage(history, userMessage.id, {
			modelId: newModelId,
			modelIdx: message.modelIdx,
			supersedeActiveTurn: true,
			scrollBehavior: 'preserve'
		});
	};

	const continueResponse = async () => {
		console.log('continueResponse');
		const _chatId = getVisibleChatId();

		if (history.currentId && history.messages[history.currentId].done == true) {
			if (blockParentGenerationDuringSubagentRerun()) return;
			const responseMessage = history.messages[history.currentId];

			if (shouldContinueFromLastToolRequest(responseMessage)) {
				await retryFromLastRequest(
					responseMessage,
					responseMessage?.selectedModelId ?? responseMessage.model
				);
				return;
			}

			const generation = prepareGenerationLifecycle(_chatId, responseMessage);
			responseMessage.done = false;
			// Continue reuses this assistant id, which already completed once — clear it
			// from the completion-dedup set so the continuation's terminal chat:done runs
			// chatCompletedHandler in full and clears generating/taskIds (it early-returns
			// on a duplicate id). Mirrors the observer-side reactivation in chatDeltaHandler.
			_completedMessageIds.delete(responseMessage.id);
			await tick();

			const model = $models
				.filter((m) => m.id === (responseMessage?.selectedModelId ?? responseMessage.model))
				.at(0);

			if (model) {
				await sendMessageSocket(
					model,
					createMessagesList(history, responseMessage.id),
					history,
					responseMessage.id,
					_chatId,
					{
						scrollBehavior: 'preserve',
						generationId: generation.generationId,
						turnId: generation.turnId,
						supersedeActiveTurn: true,
						// Same fix as retryWithoutProviderRestrictions / sendMessage's retry
						// loop: this reuses responseMessage.id (an already-completed turn that
						// may carry real content_blocks), so the leaf must be pinned to it —
						// the default (responseMessage.parentId) would drop its content_blocks
						// from the v2.1 backend's leaf-walk reconstruction entirely.
						leafMessageId: responseMessage.id
					}
				);
			}
		}
	};

	const mergeResponses = async (messageId, responses, _chatId) => {
		console.log('mergeResponses', messageId, responses);
		const mergeHistory = history;
		const mergeParams = cloneState(params ?? {});
		const mergeSplitLargeChunks = $settings.splitLargeChunks;
		const message = mergeHistory.messages[messageId];
		if (!message) return;
		const mergedResponse = {
			status: true,
			content: ''
		};
		message.merged = mergedResponse;
		mergeHistory.messages[messageId] = message;
		const mergeGenerationId = uuidv4();
		generationLifecycles.begin({
			chatId: _chatId,
			messageId,
			generationId: mergeGenerationId,
			turnId: uuidv4(),
			navigationGeneration: navigateGeneration
		});
		const mergeController = new AbortController();
		attachGenerationController(messageId, mergeGenerationId, mergeController);

		try {
			// The begin()/attachGenerationController above already registered this
			// merge as live work; the composer derives from that.
			const [res] = await generateMoACompletion(
				localStorage.token,
				message.model,
				mergeHistory.messages[message.parentId].content,
				responses,
				mergeController
			);

			if (
				res &&
				res.ok &&
				res.body &&
				!generationLifecycles.isStopped(messageId, mergeGenerationId) &&
				generationLifecycles.isVisible(
					messageId,
					mergeGenerationId,
					getVisibleChatId(),
					navigateGeneration
				)
			) {
				const textStream = await createOpenAITextStream(res.body, mergeSplitLargeChunks);
				for await (const update of textStream) {
					if (
						generationLifecycles.isStopped(messageId, mergeGenerationId) ||
						!generationLifecycles.isVisible(
							messageId,
							mergeGenerationId,
							getVisibleChatId(),
							navigateGeneration
						)
					) {
						mergeController.abort();
						break;
					}
					const { value, done, sources, error, usage } = update;
					if (error || done) {
						break;
					}

					if (mergedResponse.content == '' && value == '\n') {
						continue;
					} else {
						mergedResponse.content += value;
						mergeHistory.messages[messageId] = message;
					}

					if (autoScroll) {
						scrollToBottom();
					}
				}

				if (
					!generationLifecycles.isStopped(messageId, mergeGenerationId) &&
					generationLifecycles.isVisible(
						messageId,
						mergeGenerationId,
						getVisibleChatId(),
						navigateGeneration
					)
				) {
					await saveChatHandler(_chatId, mergeHistory, mergeParams, [
						{
							op: 'update_message_content',
							message_id: messageId,
							content: message.content,
							merged: mergedResponse
						}
					]);
				}
			} else {
				console.error(res);
			}
		} catch (e) {
			console.error(e);
		} finally {
			chatStreamDebug('[chat-stream] MoA generation finally — clearing controller', {
				messageId
			});
			settleGenerationLifecycle(messageId);
		}
	};

	const initChatHandler = async (history) => {
		let _chatId = getVisibleChatId();

		if (!$temporaryChatEnabled) {
			chat = await createNewChat(
				localStorage.token,
				{
					id: _chatId,
					title: $i18n.t('New Chat'),
					models: selectedModels,
					system: $settings.system ?? undefined,
					params: params,
					history: history,
					messages: createMessagesList(history, history.currentId),
					tags: [],
					timestamp: Date.now()
				},
				$selectedFolder?.id
			);

			_chatId = chat.id;
			rememberPersistedSelectedModels(_chatId);

			// Order matters here: update the URL FIRST, then $chatId. The
			// `activeChatId` reactive falls back to `isPersistentChatView()`
			// (which reads `window.location.pathname`, untracked by Svelte) when
			// `routeChatId` is stale. If we set $chatId first, the reactive
			// flushes on the next microtask while pathname is still `/`,
			// computes `activeChatId = ''`, and Navbar mounts with no chat —
			// hiding the token-stats box (and any other persistent-chat-only UI)
			// until the user navigates again. Doing replaceState first means the
			// reactive sees the new pathname and resolves activeChatId
			// correctly on the same flush.
			window.history.replaceState(history.state, '', `/c/${_chatId}`);
			chatId.set(_chatId);

			await tick();

			// Optimistic sidebar patch — backend's chat:created broadcast skips this
			// originating session via X-Session-Id, so we patch locally to surface the
			// new chat in the sidebar immediately without a refetch.
			if (chat?.id && chat?.folder_id == null && !chat?.archived) {
				const row = decorate({
					id: chat.id,
					title: chat.title,
					updated_at: chat.updated_at,
					created_at: chat.created_at,
					pinned: chat.pinned,
					archived: chat.archived,
					folder_id: chat.folder_id
				});
				if (row.pinned) {
					pinnedChats.update((arr) => upsertSorted(arr, row));
				} else {
					chats.update((arr) => upsertSorted(arr, row));
				}
			}
			invalidateFolderChatLists([chat?.folder_id], 'chat:created:origin');

			selectedFolder.set(null);
		} else {
			_chatId = `local:${$socket?.id}`; // Use socket id for temporary chat
			await chatId.set(_chatId);
		}
		await tick();
		// The chat row was just created FROM the current toolbar state, so every
		// binding is already durable — record that so the sync effect doesn't
		// immediately re-PATCH the values the create call just wrote.
		markChatParamsPersisted();

		return _chatId;
	};

	// True when the visible chat is backed by the DB (server-driven drain
	// applies). Temp / local: chats have no DB queue and keep the client-side
	// drain (dequeueAndSend) as their only mechanism.
	const isServerDrainChat = () => {
		const _chatId = getVisibleChatId();
		return !!_chatId && !_chatId.startsWith('local:') && !$temporaryChatEnabled;
	};

	// Append a queue item with the text + currently-attached files + any
	// `@`-mention, plus a self-contained send spec the backend drain consumes.
	// Clears the input afterwards so the user can keep typing. `chatFiles` is NOT
	// moved here (it gets moved during the normal send path in submitPrompt) —
	// the queue item carries its own per-message files.
	const enqueueMessage = async (
		userPrompt: string,
		mode: 'after_final' | 'steer' = 'after_final'
	) => {
		const originChatId = getVisibleChatId();
		const originNavigationGeneration = navigateGeneration;
		const originServerDrain =
			!!originChatId && !originChatId.startsWith('local:') && !$temporaryChatEnabled;
		const itemFiles = cloneState(files);
		const atModelId = atSelectedModel?.id ?? null;
		const queuedParentMessageId = history.currentId ?? null;
		const sendSpec = await captureQueueSendSpec(
			userPrompt,
			itemFiles,
			atModelId,
			queuedParentMessageId
		);

		const item: QueuedMessage = {
			id: uuidv4(),
			prompt: userPrompt,
			files: itemFiles,
			atSelectedModelId: atModelId,
			createdAt: Date.now(),
			// `mode` lives at the TOP level of the item (not just inside sendSpec)
			// because the backend reads it there: pop_steer_items_by_id filters on
			// item["mode"] == "steer" and the drain ignores those, while
			// after_final items flow through the normal drain.
			mode,
			...(sendSpec ? { sendSpec } : {})
		};
		const stillOnOrigin =
			originNavigationGeneration === navigateGeneration && getVisibleChatId() === originChatId;
		if (stillOnOrigin) {
			queue = [...queue, item];
			files = [];
			prompt = '';
			messageInput?.setText('');
		}

		// Persist immediately so it survives reload / tab close / zero-tab drain.
		// DB chats use the atomic append_queue_item op (no whole-array clobber if
		// two tabs enqueue concurrently); temp chats fall back to the in-memory
		// queue + set_queue snapshot (which persistQueue no-ops for local: ids).
		if (originServerDrain && originChatId) {
			// Mark it unconfirmed so a concurrent chat:queue:updated broadcast can't drop
			// our chip before our own append commits (see the merge in chat:queue:updated).
			pendingQueueItemIds.add(item.id);
			void patchChat(localStorage.token, originChatId, [
				{ op: 'append_queue_item', item: cloneState(item) }
			])
				.then(() => {
					// Committed: server now owns it; broadcasts will carry it.
					pendingQueueItemIds.delete(item.id);
				})
				.catch((error) => {
					console.error('Failed to persist queued message', error);
					pendingQueueItemIds.delete(item.id);
					// Roll back the optimistic chip: the server (and therefore every other
					// client) never got this item, so leaving it would show a phantom queued
					// message that this tab alone believes exists and that never drains.
					if (getVisibleChatId() === originChatId) {
						queue = queue.filter((q) => q.id !== item.id);
						toast.error($i18n.t('Failed to queue message. Please try again.'));
					}
				});
		} else if (stillOnOrigin) {
			void persistQueue().catch((error) => {
				console.error('Failed to persist queued message', error);
			});
		}
	};

	// STEER: enqueue a message the backend agentic loop injects at its next
	// tool-call boundary (mid-task), rather than after the whole response. The
	// durable queue item (mode:'steer') IS the signal — the loop polls
	// pop_steer_items_by_id each round — so this survives reload / tab close /
	// zero open tabs by construction, exactly like an after_final queue item.
	// Steering only makes sense for server-driven (DB) chats; temp/local chats
	// have no backend loop to inject into, so we degrade to after_final there.
	const steerMessage = async (userPrompt: string) => {
		// A steer is delivered as a `user_steer` content block built purely from
		// TEXT — the backend can't carry attachments on a steer (it would pop the
		// item but inject text only → files lost). So if there are files staged, no
		// text, or this is a temp/local chat (no backend loop), route to
		// after_final instead, where the normal follow-up pipeline carries the
		// attachments and the message generates as its own turn.
		if (!isServerDrainChat() || userPrompt.trim() === '' || (files?.length ?? 0) > 0) {
			await enqueueMessage(userPrompt, 'after_final');
			return;
		}
		await enqueueMessage(userPrompt, 'steer');
	};

	// Exact match only, mirroring `is_compact_command` in utils/compaction.py.
	// "/compact the logs into one file" is an ordinary instruction and must be
	// delivered as one.
	const isCompactCommand = (text: unknown): boolean =>
		typeof text === 'string' && text.trim().toLowerCase() === '/compact';

	const runCompactCommand = async () => {
		const _chatId = getVisibleChatId();
		if (!_chatId || _chatId.startsWith('local:')) {
			toast.error($i18n.t('Compaction needs a saved chat.'));
			return;
		}
		const model = selectedModels?.[0];
		if (!model) {
			toast.error($i18n.t('Select a model first.'));
			return;
		}
		try {
			const res = await compactChat(localStorage.token, _chatId, model, history.currentId);
			if (res?.compacted === false) {
				toast.info($i18n.t('Nothing to compact — the context was already compacted.'));
				return;
			}
			// The socket push lands too, but the acting tab shouldn't wait on a
			// round trip through the server to see its own divider.
			applyCompactionBlocks(res?.message_id, res?.content_blocks);
			toast.success($i18n.t('Context compacted.'));
		} catch (error) {
			toast.error(`${error?.detail ?? error}`);
		}
	};

	// Splice a compaction anchor into a message already in the local history.
	// Shared by the command's own response and the `chat:message:compacted`
	// broadcast (which is what a SECOND tab, or a queued `/compact` run by the
	// drain with no tab attached, arrives on).
	const applyCompactionBlocks = (messageId?: string, contentBlocks?: unknown) => {
		if (!messageId || !Array.isArray(contentBlocks)) return;
		const message = history.messages[messageId];
		if (!message) return;
		message.content_blocks = contentBlocks;
		history.messages[messageId] = message;
	};

	const editQueuedMessage = (id: string, nextText: string) => {
		queue = queue.map((q) =>
			q.id === id
				? {
						...q,
						prompt: nextText,
						...(q.sendSpec ? { sendSpec: { ...q.sendSpec, content: nextText } } : {})
					}
				: q
		);
		if (isServerDrainChat()) {
			// Edit in place (position preserved) so the chip doesn't jump to the
			// tail and the drain / steer-injection order is unchanged. The single
			// atomic op also avoids the remove+append two-write window.
			const _chatId = getVisibleChatId();
			const updated = queue.find((q) => q.id === id);
			if (updated) {
				// Mark this edit pending so a concurrent broadcast's stale copy doesn't
				// revert it before our update commits (see the chat:queue:updated merge).
				pendingQueueItemIds.add(id);
				void patchChat(localStorage.token, _chatId, [
					{ op: 'update_queue_item', item: cloneState(updated) }
				])
					.then(() => pendingQueueItemIds.delete(id))
					.catch((error) => {
						pendingQueueItemIds.delete(id);
						console.error('Failed to persist queued message edit', error);
					});
			}
		} else {
			void persistQueue().catch((error) => {
				console.error('Failed to persist queued message edit', error);
			});
		}
	};

	const removeQueuedMessage = (id: string) => {
		queue = queue.filter((q) => q.id !== id);
		if (isServerDrainChat()) {
			const _chatId = getVisibleChatId();
			// Mark this removal pending so a concurrent broadcast's snapshot (taken
			// before our remove committed) doesn't resurrect the just-removed chip.
			pendingQueueItemIds.delete(id);
			pendingRemovedQueueItemIds.add(id);
			void patchChat(localStorage.token, _chatId, [{ op: 'remove_queue_item', item_id: id }])
				.then(() => pendingRemovedQueueItemIds.delete(id))
				.catch((error) => {
					pendingRemovedQueueItemIds.delete(id);
					console.error('Failed to persist queued message removal', error);
				});
		} else {
			void persistQueue().catch((error) => {
				console.error('Failed to persist queued message removal', error);
			});
		}
	};

	// "Send now": resume a queue the user PAUSED by pressing Stop. We deliberately
	// keep Stop = pause-not-abandon, and this is the explicit one-click resume. For
	// server-drain chats it asks the backend to drain the head immediately (which
	// then chains the rest on clean completion); the chat:queue:drained broadcast
	// attaches this tab. Clearing the Stop intent FIRST is load-bearing: the
	// drained-attach guard (C09) stops any drain landing within STOP_RACE_WINDOW_MS
	// of a Stop, so the generation we are about to ask for would be killed on
	// arrival. (This used to be two separate resets — the latch and the timestamp —
	// which is precisely the pair that could be forgotten apart.)
	const sendQueuedNow = async () => {
		if (queue.length === 0) return;
		localStop = null;
		if (isServerDrainChat()) {
			const _chatId = getVisibleChatId();
			try {
				await drainChatQueue(localStorage.token, _chatId);
			} catch (error) {
				console.error('Failed to drain queue on Send now', error);
			}
		} else {
			// Temp/local chats have no backend loop — fall back to the client drain.
			void dequeueAndSend();
		}
	};

	// Pop the head of the queue and send it directly. We deliberately bypass
	// submitPrompt here so we don't clobber the user's in-flight typing /
	// attached files in the input bar — the queued message owns its own
	// snapshot. If submitPrompt had been called instead, its `messageInput
	// .setText('')` / `files = []` lines would wipe whatever the user has
	// staged for their *next* manual send.
	//
	// The auto-send reactive guards entry; dequeueAndSend itself is just "send
	// the next one." Multiple queued messages drain one at a time: the reactive
	// fires again on each subsequent natural completion.
	const dequeueAndSend = async () => {
		if (queueSending) return;
		if (queue.length === 0) return;
		queueSending = true;
		let allowFollowUpDrain = false;
		try {
			const next = queue[0];
			queue = queue.slice(1);

			try {
				await persistQueue();
				allowFollowUpDrain = true;
			} catch (error) {
				queue = [next, ...queue];
				console.error('Failed to persist queued message removal before send', error);
				toast.error($i18n.t('Failed to send queued message. Please try again.'));
				return;
			}

			const itemFiles = Array.isArray(next.files) ? cloneState(next.files) : [];

			// Validate model selection is still sensible. If selected models drifted
			// to invalid ids (model deleted, etc.), drop the queued send rather than
			// silently failing in sendMessage.
			const _selectedModels = selectedModels.map((modelId) =>
				$models.map((m) => m.id).includes(modelId) ? modelId : ''
			);
			if (!arraysEqual(selectedModels, _selectedModels)) {
				selectedModels = _selectedModels;
			}
			if (selectedModels.includes('')) {
				toast.error($i18n.t('Model not selected — queued message dropped'));
				return;
			}

			// Mirror submitPrompt's chatFiles accumulation: move text-extraction
			// kinds onto the chat-wide files list so subsequent turns see them.
			chatFiles.push(
				...itemFiles.filter((item) =>
					['doc', 'text', 'file', 'note', 'chat', 'folder', 'collection'].includes(item.type)
				)
			);
			chatFiles = chatFiles.filter(
				(item, index, array) =>
					array.findIndex((i) => JSON.stringify(i) === JSON.stringify(item)) === index
			);

			// Build the user message from the snapshot. Parented to the current
			// head of the chat so the queued message lands right after the just-
			// completed assistant turn (which is what the user expected when they
			// pressed Enter).
			const messages = createMessagesList(history, history.currentId);
			const userMessageId = uuidv4();
			// If the user @-mentioned a specific model at queue time, the user
			// message records THAT model only — mirrors submitPrompt's normal path
			// where `models: selectedModels` reflects atSelectedModel's effect.
			const messageModels = next.atSelectedModelId ? [next.atSelectedModelId] : selectedModels;
			const userMessage = {
				id: userMessageId,
				parentId: messages.length !== 0 ? messages.at(-1).id : null,
				childrenIds: [],
				role: 'user',
				content: next.prompt,
				files: itemFiles.length > 0 ? itemFiles : undefined,
				timestamp: Math.floor(Date.now() / 1000),
				models: messageModels
			};
			history.messages[userMessageId] = userMessage;
			history.currentId = userMessageId;
			if (messages.length !== 0) {
				history.messages[messages.at(-1).id].childrenIds.push(userMessageId);
			}

			await tick();

			// `newChat: true` is fine: sendMessage only fires initChatHandler when
			// the message has no parent, which only happens on the very first send
			// in a chat. Queued sends always have a parent (the just-completed
			// assistant turn).
			//
			// If the queued message had no @-mention, the user might have set one
			// AFTER queueing (for their next manual send). sendMessage prefers
			// atSelectedModel over selectedModels when modelId isn't passed, so
			// that stale @-mention would leak in and route the queued send to the
			// wrong model. Temporarily detach atSelectedModel for the duration of
			// the call.
			const restoreAtSelected = !next.atSelectedModelId ? atSelectedModel : undefined;
			if (!next.atSelectedModelId) atSelectedModel = undefined;
			try {
				await sendMessage(history, userMessageId, {
					newChat: true,
					// 'preserve': this drain fires from a PASSIVE falling-edge reactive
					// (the prior turn finishing), not a contemporaneous user gesture.
					// If the reader scrolled up to re-read while the queued message was
					// waiting, their most-recent intent is "stay here" — honor it instead
					// of yanking to the bottom. Matches every other programmatic re-send
					// (regenerate/retry/rewind) and the DB-backed drain (loadChat is
					// isSameChatReload-gated and never re-arms a scrolled-up reader). A
					// user still tailing (autoScroll true) keeps following.
					scrollBehavior: 'preserve',
					...(next.atSelectedModelId ? { modelId: next.atSelectedModelId } : {})
				});
			} finally {
				if (restoreAtSelected !== undefined) atSelectedModel = restoreAtSelected;
			}
		} finally {
			queueSending = false;
			await tick();
			const lastMsg = history?.currentId ? history.messages[history.currentId] : null;
			const finishedCleanly = lastMsg?.done === true && !lastMsg?.error;
			if (
				allowFollowUpDrain &&
				!isServerDrainChat() &&
				queue.length > 0 &&
				!generating &&
				$isLastActiveTab &&
				!loading &&
				finishedCleanly &&
				!userInitiatedStop
			) {
				void dequeueAndSend();
			}
		}
	};

	// Tracks the last values we PATCHed for non-message fields so subsequent
	// saveChatHandler calls without explicit ops can diff and only send what
	// actually changed. Reset by loadChat / initChatHandler when the active
	// chat switches.
	let lastSavedSnapshot: {
		chatId: string | null;
		models: string[];
		params: any;
		files: any[];
		queue: any[];
		currentId: string | null;
	} = {
		chatId: null,
		models: [],
		params: {},
		files: [],
		queue: [],
		currentId: null
	};

	const resetSaveSnapshot = (_chatId: string | null = null) => {
		lastSavedSnapshot = {
			chatId: _chatId,
			models: cloneState(selectedModels ?? []),
			params: cloneState(params ?? {}),
			files: cloneState(chatFiles ?? []),
			queue: cloneState(queue ?? []),
			currentId: history?.currentId ?? null
		};
	};

	// Update the sidebar's `updated_at` for a chat in place and reorder. Used
	// by the originating tab when it won't receive its own `chat:updated`
	// socket event (skip_sid'd PATCH responses and stream-done events).
	const patchSidebarUpdatedAt = (id: string, updatedAt: number) => {
		const patch = (c: any) =>
			c.id === id ? { ...c, updated_at: updatedAt, time_range: getTimeRange(updatedAt) } : c;
		chats.update((arr) =>
			arr ? [...arr].map(patch).sort((a, b) => (b.updated_at ?? 0) - (a.updated_at ?? 0)) : arr
		);
		pinnedChats.update((arr) => (arr ? arr.map(patch) : arr));
	};

	const invalidateFolderChatLists = (
		folderIds: Array<string | null | undefined>,
		reason: string
	) => {
		const ids = Array.from(new Set(folderIds.filter((id): id is string => !!id)));
		if (ids.length === 0) return;
		folderChatListInvalidation.update((state) => ({
			folderIds: ids,
			seq: state.seq + 1,
			reason
		}));
	};

	const saveChatHandler = async (
		_chatId,
		history,
		nextParams = params,
		ops: PatchChatOp[] | null = null
	) => {
		if (!isVisibleChatEvent(_chatId)) return;
		if ($temporaryChatEnabled) return;
		if (!_chatId || _chatId.startsWith('local:')) return;

		// Local write to this chat — its cached open-snapshot is now stale (item 2).
		invalidateChatOpenCache(_chatId);
		// The offline (IDB) copy is now stale too, but it is deliberately KEPT:
		// the zero-network serve gates compare updatedAt exactly (a stale entry
		// simply never race-serves online), while the offline fallback prefers a
		// stale copy over none at all — your most-recently-written chats are
		// exactly the ones you want readable offline. Pinned copies additionally
		// get a debounced freshness refetch; auto copies self-heal via the next
		// open or prefetch sweep.
		const _saveHandlerUserId = $user?.id;
		if (_saveHandlerUserId) {
			handleLocalChatSaved(localStorage.token, _saveHandlerUserId, _chatId);
		}

		let opList: PatchChatOp[];

		if (ops !== null) {
			opList = ops;
		} else {
			opList = [];
			if (lastSavedSnapshot.chatId !== _chatId) {
				opList.push({ op: 'set_models', models: selectedModels ?? [] });
				opList.push({ op: 'set_files', files: chatFiles ?? [] });
				opList.push({ op: 'set_queue', queue: queue ?? [] });
				if (history?.currentId) {
					opList.push({ op: 'set_history_current_id', current_id: history.currentId });
				}
				for (const [key, value] of Object.entries(nextParams ?? {})) {
					opList.push({ op: 'set_param', key, value });
				}
			} else {
				if (JSON.stringify(lastSavedSnapshot.models) !== JSON.stringify(selectedModels)) {
					opList.push({ op: 'set_models', models: selectedModels ?? [] });
				}
				if (JSON.stringify(lastSavedSnapshot.files) !== JSON.stringify(chatFiles)) {
					opList.push({ op: 'set_files', files: chatFiles ?? [] });
				}
				if (JSON.stringify(lastSavedSnapshot.queue) !== JSON.stringify(queue)) {
					opList.push({ op: 'set_queue', queue: queue ?? [] });
				}
				if (lastSavedSnapshot.currentId !== history?.currentId && history?.currentId) {
					opList.push({ op: 'set_history_current_id', current_id: history.currentId });
				}
				const prevParams = lastSavedSnapshot.params ?? {};
				const allParamKeys = new Set([
					...Object.keys(prevParams),
					...Object.keys(nextParams ?? {})
				]);
				for (const key of allParamKeys) {
					const prev = prevParams[key];
					const next = (nextParams ?? {})[key];
					if (JSON.stringify(prev) !== JSON.stringify(next)) {
						opList.push({ op: 'set_param', key, value: next });
					}
				}
			}
		}

		if (opList.length === 0) {
			return;
		}

		// While the PATCH is in flight the sidebar row still carries the pre-save
		// updated_at, which the (kept, now-stale) offline IDB entry also matches —
		// so the zero-network race tier could serve the pre-edit body during this
		// window. Mark the id dirty; the race tier skips dirty ids. After the
		// PATCH lands, patchSidebarUpdatedAt bumps the row past the IDB entry and
		// the updatedAt-equality gate takes over again.
		markOfflineRaceDirty(_chatId);
		let res;
		try {
			res = await patchChat(localStorage.token, _chatId, opList);
		} finally {
			unmarkOfflineRaceDirty(_chatId);
		}
		resetSaveSnapshot(_chatId);

		// `chat:updated` socket events are skip_sid'd, so this tab won't receive
		// the broadcast for its own PATCH. Patch the sidebar locally so the row
		// reorders with the freshly-bumped `updated_at`.
		const updatedAt = res?.updated_at;
		if (updatedAt != null) {
			patchSidebarUpdatedAt(_chatId, updatedAt);
			invalidateFolderChatLists([chat?.folder_id], 'chat:updated:origin');
		}
	};

	const MAX_DRAFT_LENGTH = 5000;
	let saveDraftTimeout = null;

	const saveDraft = async (draft, chatId = null) => {
		if (saveDraftTimeout) {
			clearTimeout(saveDraftTimeout);
		}

		if (draft.prompt !== null && draft.prompt.length < MAX_DRAFT_LENGTH) {
			saveDraftTimeout = setTimeout(async () => {
				await sessionStorage.setItem(
					`chat-input${chatId ? `-${chatId}` : ''}`,
					JSON.stringify(draft)
				);
			}, 500);
		} else {
			sessionStorage.removeItem(`chat-input${chatId ? `-${chatId}` : ''}`);
		}
	};

	const clearDraft = async (chatId = null) => {
		if (saveDraftTimeout) {
			clearTimeout(saveDraftTimeout);
		}
		await sessionStorage.removeItem(`chat-input${chatId ? `-${chatId}` : ''}`);
	};

	const moveChatHandler = async (chatId, folderId) => {
		if (chatId && folderId) {
			const previousFolderId =
				get(chats)?.find((c) => c.id === chatId)?.folder_id ??
				get(pinnedChats)?.find((c) => c.id === chatId)?.folder_id ??
				chat?.folder_id ??
				null;
			const res = await updateChatFolderIdById(localStorage.token, chatId, folderId).catch(
				(error) => {
					toast.error(`${error}`);
					return null;
				}
			);

			if (res) {
				chats.update((arr) =>
					arr ? arr.map((c) => (c.id === chatId ? { ...c, folder_id: folderId } : c)) : arr
				);
				pinnedChats.update((arr) =>
					arr.map((c) => (c.id === chatId ? { ...c, folder_id: folderId } : c))
				);
				invalidateFolderChatLists([previousFolderId, folderId], 'chat:folder:origin');

				toast.success($i18n.t('Chat moved successfully'));
			}
		} else {
			toast.error($i18n.t('Failed to move chat'));
		}
	};

	// Promote the in-memory temporary chat into a permanent saved chat. Triggered
	// from both the navbar button and the Cmd/Ctrl+S keyboard shortcut. The earlier
	// implementation passed the user's first-message `content` directly as the
	// title, which fails when the content is a multimodal list (image/file/etc.) —
	// the API rejects the non-string title and the user sees a vague generic
	// "Failed to save conversation" toast. Coerce to text first, fall back when
	// empty, and surface real error messages so the user can act on them.
	const saveTempChatHandler = async () => {
		try {
			if (!history?.currentId || !Object.keys(history.messages).length) {
				toast.error($i18n.t('No conversation to save'));
				return;
			}

			const messagesList = createMessagesList(history, history.currentId);
			const firstUserContent = messagesList.find((m) => m.role === 'user')?.content;
			let title = getStringMessageContent(firstUserContent ?? '').trim();
			if (!title) {
				title = $i18n.t('New Chat');
			}
			if (title.length > 50) {
				title = `${title.slice(0, 50)}...`;
			}

			const savedChat = await createNewChat(
				localStorage.token,
				{
					id: uuidv4(),
					title,
					models: selectedModels,
					params: params,
					history: history,
					messages: messagesList,
					timestamp: Date.now()
				},
				null
			);

			if (!savedChat) {
				toast.error($i18n.t('Failed to save conversation'));
				return;
			}

			temporaryChatEnabled.set(false);
			chatId.set(savedChat.id);
			if (savedChat?.id && savedChat?.folder_id == null && !savedChat?.archived) {
				const row = decorate({
					id: savedChat.id,
					title: savedChat.title,
					updated_at: savedChat.updated_at,
					created_at: savedChat.created_at,
					pinned: savedChat.pinned,
					archived: savedChat.archived,
					folder_id: savedChat.folder_id
				});
				if (row.pinned) {
					pinnedChats.update((arr) => upsertSorted(arr, row));
				} else {
					chats.update((arr) => upsertSorted(arr, row));
				}
			}

			await goto(`/c/${savedChat.id}`);
			toast.success($i18n.t('Conversation saved successfully'));
		} catch (error) {
			console.error('Error saving conversation:', error);
			const detail =
				(error && (error.detail || error.message)) || (typeof error === 'string' ? error : null);
			toast.error(
				detail
					? `${$i18n.t('Failed to save conversation')}: ${detail}`
					: $i18n.t('Failed to save conversation')
			);
		}
	};
	// Freshness of the rendered view, surfaced as the navbar's sync mark
	// (SyncStatus.svelte). Offline wins (saved data, pen lifted).
	$effect(() => {
		chatFreshness.set(!$online ? 'offline' : $chatId && chatViewUnverified ? 'syncing' : 'fresh');
	});
	// Single source of truth for reconciling selectedModels against $models.
	// This is the ONLY place in the app that automatically rewrites
	// selectedModels — ModelSelector.svelte must never do so on its own
	// (beyond guarding against an empty array), because two automatic writers
	// reacting to the same $models change in one flush can desync the
	// two-level bind chain (Chat -> Navbar -> ModelSelector -> keyed-each
	// bind:value -> Selector): one writer wipes an unrecognized id to '' and
	// the other refills it in the same tick, but the Selector's own bound
	// `value` is left stuck at '' — the picker shows "Select a model" while
	// the placeholder above the composer (which receives the refilled array
	// as a one-way prop) correctly shows the model name.
	//
	// Gated on `!loading` rather than `!chatIdProp` so this also covers a
	// LOADED chat (chatIdProp set) whose persisted model id no longer exists
	// in $models — previously that case only ran through ModelSelector's own
	// (now-removed) wipe-to-'' block with nothing to refill it, leaving
	// "Select a model" and a "Model not selected" toast on send. Checking
	// `loading` (not `chatIdProp`) avoids racing loadChat's restore of
	// selectedModels at ~line 4944: while a chat is loading/navigating this
	// block must not run, matching the existing guard style in
	// onSelectedModelIdsChange (~line 1683).
	$effect(() => {
		if ($models.length > 0 && !loading) {
			const allIds = $models.map((m) => m.id);
			const visibleIds = $models.filter((m) => !(m?.info?.meta?.hidden ?? false)).map((m) => m.id);

			// Drop only genuinely STALE non-empty ids. Two kinds of entries survive:
			// - valid ids, checked against allIds (not visibleIds): an already-
			//   selected hidden model is still valid — it can arrive via a loaded
			//   chat whose persisted model was later hidden;
			// - '' entries: these are pending Add-Model slots (ModelSelector's Add
			//   button appends '' so the user can pick a second model) and must
			//   survive reconciliation in place, or clicking Add Model would be
			//   instantly undone by this block.
			const stripped = selectedModels.filter((id) => id === '' || allIds.includes(id));
			const hasRealSelection = stripped.some((id) => id !== '');

			if (stripped.length === selectedModels.length && hasRealSelection) {
				// Nothing stale AND at least one real selection (a mixed
				// ['valid-id', ''] mid-Add-Model state lands here) — strict no-op.
				// This guard is what keeps the block from looping: once the array
				// holds only valid ids and pending '' slots, re-running produces an
				// identical `stripped` and short-circuits without assigning.
			} else if (hasRealSelection) {
				// Some stale ids were dropped but at least one valid id survives
				// (pending '' slots kept in place) — preserve the rest of a
				// multi-model selection instead of discarding it wholesale.
				if (!arraysEqual(selectedModels, stripped)) {
					selectedModels = stripped;
				}
			} else {
				// No non-empty entry survives — boot/new-chat ([''] initial state)
				// or every real id went stale (e.g. ['stale', ''] → ['']). Refill
				// from the default chain.
				let refilled: string[] = [];
				if ($settings?.models) {
					refilled = $settings.models.filter((id) => visibleIds.includes(id));
				}
				if (refilled.length === 0 && $config?.default_models) {
					refilled = $config.default_models.split(',').filter((id) => visibleIds.includes(id));
				}
				if (refilled.length === 0) {
					refilled = [visibleIds[0] ?? ''];
				}
				// Value-equality guard: when the refill chain bottoms out at ['']
				// (every model hidden, no matching defaults) the result equals the
				// current selection — assigning a NEW array anyway would re-trigger
				// this block (it depends on selectedModels) on every flush and trip
				// Svelte's "Infinite loop detected" guard.
				if (!arraysEqual(selectedModels, refilled)) {
					selectedModels = refilled;
				}
			}
		}
	});
	$effect(() => {
		selectedModelIds = atSelectedModel !== undefined ? [atSelectedModel.id] : selectedModels;
	});
	// Explicitly read $page and chatIdProp here so Svelte still re-evaluates
	// activeChatId when SvelteKit navigates (the `resolveRouteChatId()` call
	// below also reads them, but Svelte's compiler doesn't trace through
	// function calls when computing reactive dependencies).
	$effect(() => {
		activeChatId = (() => {
			void $page;
			void chatIdProp;
			// Re-resolve at evaluation time. `routeChatId` is reactive on $page and
			// chatIdProp, but not on `window.location.pathname` — which can be
			// updated out-of-band via `history.replaceState` (e.g. when persisting a
			// brand-new chat in `initChatHandler`). Without a fresh resolution here,
			// activeChatId can be momentarily empty after a new chat is created,
			// hiding the Navbar's persistent-chat UI (token-stats box, etc.) until
			// the user navigates explicitly.
			const currentRouteChatId = resolveRouteChatId();
			if (currentRouteChatId) {
				return currentRouteChatId;
			}

			const currentChatId = $chatId ?? '';
			if ($temporaryChatEnabled || currentChatId.startsWith('local:') || isPersistentChatView()) {
				return currentChatId;
			}

			return '';
		})();
	});
	// Mirror + persist every per-chat toolbar toggle. One effect over the
	// binding table (see ChatParamBinding): it reads each toggle, so any change
	// re-runs it, and each key is independently guarded by its own equality
	// check. `chatParamPersisted[key] === undefined` means the saved value isn't
	// known yet — never PATCH before a load, or opening a chat would write the
	// defaults over the user's saved selection.
	$effect(() => {
		void chatParamRetryTick;
		const chatIdToPersist = activeChatId;
		const canPersist = !!chatIdToPersist && !loading && !$temporaryChatEnabled;
		for (const binding of chatParamBindings) {
			const current = binding.read();
			const key = binding.key;
			if (!binding.equals(current, params[key] ?? binding.fallback)) {
				params = { ...params, [key]: binding.clone(current) };
			}
			if (!canPersist) continue;
			if (chatParamPersisted[key] === undefined) continue;
			if (binding.equals(current, chatParamPersisted[key])) continue;
			if (chatParamUnconfirmed.has(key)) continue;
			void persistChatParam(binding, chatIdToPersist);
		}
	});
	$effect(() => {
		tokenUsageGroups = $tokenUsageGroupsStore;
	});
	// Get relevant groups for currently selected models
	// The resetTrigger dependency forces re-evaluation when reset times pass
	let relevantGroups = $derived(
		(() => {
			// Reference resetTrigger to make this reactive to reset events
			const _ = resetTrigger;

			return Object.entries(tokenUsageGroups)
				.filter(([groupName, groupData]) => {
					const modelList = (groupData as any).models || [];
					return selectedModelIds.some((modelId) => modelList.includes(modelId));
				})
				.map(([groupName, groupData]) => {
					// Compute effective usage (0 if past reset time)
					const effectiveUsage = getEffectiveUsage(groupData);
					return [groupName, { ...groupData, effectiveUsage }] as [string, any];
				});
		})()
	);
	$effect(() => {
		const _ = _nowTick; // reactive dep so off-peak boundary crossings re-evaluate
		const flexEnabled = $config?.features?.flex_auto_flip_enabled ?? false;
		const startHour = $config?.features?.flex_auto_flip_off_peak_start_hour ?? 13;
		const endHour = $config?.features?.flex_auto_flip_off_peak_end_hour ?? 5;
		const tz = $config?.features?.flex_auto_flip_off_peak_timezone ?? 'America/Los_Angeles';
		const thresholdRatio = $config?.features?.flex_auto_flip_threshold_ratio ?? 0.8;
		if (
			flexEnabled &&
			!serviceTierUserTouched &&
			(!taskIds || taskIds.length === 0) &&
			serviceTier === 'default' &&
			// Multi-model chats hide the tier selector entirely, so never flip one.
			selectedModelIds?.length === 1 &&
			modelSupportsFlexTier(selectedModelIds[0]) &&
			(isOffPeakHour(new Date(), startHour, endHour, tz) ||
				isApproachingAnyLimit(relevantGroups, thresholdRatio))
		) {
			// Silent flip — the composer border/pill turning terracotta is the only
			// signal (the user asked for no toast here). Still fully reversible: the
			// moment the user picks a tier by hand, `serviceTierUserTouched` latches
			// and this block stands down for the rest of the chat.
			serviceTier = 'flex';
			if (subagentsEnabled) subagentServiceTier = 'flex';
		}
	});
	// A user toggle during live work is a latest-value socket operation. The
	// lifecycle dependency covers the pre-task window: if the user clicks while
	// the initial provider request is still being registered, the newest
	// selection is sent as soon as that task id becomes available.
	$effect(() => {
		void liveToolSelectionRevision;
		void generationRevision;
		const socketConnected = !!$socket?.connected;
		const chatIdToUpdate = activeChatId;
		const records = generationLifecycles.activeForChat(chatIdToUpdate);
		if (records.length === 0) {
			liveToolSelectionSentByTask.clear();
			liveToolSelectionPending = false;
			return;
		}

		const currentKey = liveToolSelectionKey();
		if (!liveToolSelectionPending) {
			for (const record of records) {
				for (const taskId of record.taskIds) {
					if (!liveToolSelectionSentByTask.has(taskId)) {
						liveToolSelectionSentByTask.set(taskId, currentKey);
					}
				}
			}
			return;
		}

		if (socketConnected && records.some((record) => record.taskIds.size > 0)) {
			void syncLiveToolSelection(currentKey);
		}
	});
	// Non-enumerable marker the offline chatStore attaches when `chat` was
	// served from the IndexedDB cache after a network failure — read directly
	// off `chat` (never through a spread) since spreads/destructures drop
	// non-enumerable properties silently.
	let offlineCopyInfo = $derived((chat as any)?.__offlineCopy ?? null);
	// Re-arm the per-group reset timeouts whenever the groups change — via the
	// mount-time fetch OR a token-usage:update socket push (whose next_reset_at
	// can move, e.g. a rolling window restarting). Idempotent and cheap: it
	// clears + re-schedules one timeout per group.
	$effect(() => {
		if (tokenUsageGroups) {
			scheduleGroupResetChecks();
		}
	});
	$effect(() => {
		if (taskIds && taskIds.length > 0) {
			if (_serviceTierBaseline === null) {
				_serviceTierBaseline = serviceTier;
			} else if (serviceTier !== _serviceTierBaseline) {
				const cid = getVisibleChatId();
				if (cid && $socket) {
					for (const tid of taskIds) {
						$socket.emit('service-tier-switch', {
							chat_id: cid,
							task_id: tid,
							service_tier: serviceTier
						});
					}
				}
				_serviceTierBaseline = serviceTier;
			}
		} else {
			_serviceTierBaseline = null;
		}
	});
	// Subscription-provider usage for the currently selected models: map each
	// model to its OpenAI connection index (chasing a workspace model's
	// base_model_id — custom models carry no urlIdx of their own) and pick the
	// matching snapshot entries.
	let relevantSubscriptions = $derived(
		(() => {
			const subs = $subscriptionUsageStore ?? {};
			if (Object.keys(subs).length === 0) return [];
			const seen = new Set<string>();
			const out: any[] = [];
			for (const modelId of selectedModelIds) {
				let model = $models.find((m) => m.id === modelId);
				if (model && model.urlIdx === undefined && model?.info?.base_model_id) {
					model = $models.find((m) => m.id === model.info.base_model_id) ?? model;
				}
				const urlIdx = model?.owned_by === 'openai' ? model?.urlIdx : undefined;
				if (urlIdx === undefined || urlIdx === null) continue;
				const key = String(urlIdx);
				if (seen.has(key) || !subs[key]) continue;
				seen.add(key);
				out.push(subs[key]);
			}
			return out;
		})()
	);
	// One-line stand-in for the token-usage panel while the on-screen keyboard
	// is up (the full panel is kb-hidden): the group closest to its limit —
	// what the user actually needs to know mid-typing — or, with no limits
	// configured, the biggest group's running total.
	let kbTokenSummary = $derived(
		(() => {
			// Everything with a real ceiling competes on ratio-to-limit: token
			// groups with a limit AND subscription windows (used_percent IS the
			// ratio). The one nearest its ceiling wins the single kb line.
			const candidates: any[] = [];
			for (const [name, g] of relevantGroups ?? []) {
				if (g.limit) {
					candidates.push({
						name,
						total: g.effectiveUsage.total,
						limit: g.limit,
						ratio: g.effectiveUsage.total / g.limit
					});
				}
			}
			for (const sub of relevantSubscriptions ?? []) {
				for (const w of sub.windows ?? []) {
					candidates.push({
						name: `${formatSubscriptionLimitLabel(sub.name, w)} ${formatWindowLabel(w)}`,
						subscription: true,
						window: w,
						ratio: (w.used_percent ?? 0) / 100
					});
				}
			}
			if (candidates.length > 0) {
				return candidates.reduce((a, b) => (b.ratio > a.ratio ? b : a));
			}
			if (!relevantGroups || relevantGroups.length === 0) return null;
			const [name, g] = relevantGroups.reduce((a, b) =>
				b[1].effectiveUsage.total > a[1].effectiveUsage.total ? b : a
			);
			return { name, total: g.effectiveUsage.total, limit: null, ratio: 0 };
		})()
	);
	let hideBottomChromeForEdit = $derived($mobile && keyboardShown && $messageEditingIds.size > 0);
	$effect(() => {
		const editing = $messageEditingIds.size > 0;
		if (editing && !anyMessageEditing) {
			clearExpansionHold(); // editScroll.ts owns the viewport from here
			disengageAutoScroll();
			// The entry anchor in editScroll.ts owns the viewport through the
			// markdown→textarea swap. The swap can legitimately move even the
			// engine anchor's border box (parent-child margin collapse through
			// the message wrapper changes with the edit UI), so the engine must
			// absorb the swap as the new baseline rather than "correct" it and
			// fight the edit anchor's finished positioning.
			rebaselineOnNextScrollCorrection = true;
			captureScrollCorrectionAnchor();
		} else if (!editing && anyMessageEditing) {
			// Never leave the one-shot armed past the edit (an edit with no
			// resize delivery would otherwise silently absorb the next
			// unrelated shift instead of correcting it).
			rebaselineOnNextScrollCorrection = false;
		}
		anyMessageEditing = editing;
	});
	$effect(() => {
		if (chatIdProp) {
			// This effect should react only to the route prop. navigateHandler
			// synchronously reads and resets a large amount of chat state before
			// its first await; without untrack, all of that became accidental
			// dependencies. Mobile sidebar teardown/editor binding changes could
			// then rerun the handler after its one-shot preload was consumed,
			// eventually taking the missing-chat fallback back to `/`.
			untrack(() => {
				void navigateHandler();
			});
		}
	});
	$effect(() => {
		if (selectedModels && chatIdProp !== '') {
			saveSessionSelectedModels();
		}
	});
	$effect(() => {
		if (selectedModels && selectedModels.length > 0) {
			persistSelectedModelsForChat();
		}
	});
	$effect(() => {
		if (!arraysEqual(selectedModelIds, oldSelectedModelIds)) {
			onSelectedModelIdsChange();
		}
	});
	$effect(() => {
		routeChatId = resolveRouteChatId();
	});
	$effect(() => {
		if (routeChatId && routeChatId !== $chatId) {
			chatId.set(routeChatId);
		}
	});
	$effect(() => {
		if (selectedModels !== null) {
			savedModelIds();
		}
	});
	$effect(() => {
		if (messagesReady) messageHeightSweeper.schedule();
	});
	// Anything that turns following back on (submit, near-bottom re-engage,
	// programmatic pin) makes the pill redundant — hide it immediately.
	$effect(() => {
		if (autoScroll) {
			showJumpToBottom = false;
			// This also covers position-based re-engagement after the reader scrolls
			// back down. Without retiring the temporary spacer here, "the bottom"
			// remains the spacer's bottom and the real action row stays marooned
			// above a large blank band.
			if (composerCompensation > 0) {
				clearComposerCompensation();
				scrollToBottomNow();
			}
		}
	});
	// activeChatId + the current leaf id are deliberate extra triggers: the
	// message list remounts on chat switches and when the first message of a
	// fresh chat arrives, without either bound element changing identity. The
	// leaf id goes through a PRIMITIVE intermediate so per-token `history`
	// reassignments (up to one per frame while streaming) don't re-run the
	// tick+getElementById below — Svelte only propagates when the value
	// actually changes. The tick() defers to after the DOM patch so
	// getElementById can see the fresh <ul>; observeMessagesContent is
	// idempotent when nothing changed.
	let currentLeafIdForObserve = $derived(history?.currentId ?? null);
	$effect(() => {
		(activeChatId,
			currentLeafIdForObserve,
			messagesContentElement,
			messagesContainerElement,
			tick().then(() => observeMessagesContent(messagesContentElement, messagesContainerElement)));
	});
	// Re-arm the one-time draft-kept toast once connectivity returns, so the
	// next offline attempt (a later disconnect) shows it again.
	$effect(() => {
		if ($online) offlineDraftToastShown = false;
	});
	// Falling-edge watcher: TEMP/LOCAL CHATS ONLY. DB-backed chats are drained
	// server-side (the backend pops the queue on clean completion and starts the
	// next generation, surviving reloads / closed tabs), so this client-side
	// fallback would double-send for them. Temp chats have no DB queue, so they
	// still need this: when `generating` goes true→false and the response landed
	// cleanly, auto-send the head of the queue. Gated on $isLastActiveTab so two
	// tabs don't both fire.
	$effect(() => {
		const justFinished = _wasGenerating && !generating;
		_wasGenerating = generating;
		if (justFinished && queue.length > 0 && !queueSending && !loading) {
			const lastMsg = history?.currentId ? history.messages[history.currentId] : null;
			const finishedCleanly =
				lastMsg?.done === true && !lastMsg?.error && lastMsg?.userStopped !== true;
			if (finishedCleanly && !userInitiatedStop) {
				if (!isServerDrainChat()) {
					// Temp/local chats have no backend loop — client falling-edge drain.
					if ($isLastActiveTab) void dequeueAndSend();
				} else {
					// DB chats drain server-side. The next generation's first event
					// (chat:user-message) lands a beat later, so bridge the input bar's
					// working state across that gap instead of flicking to the idle
					// "Send a Message" affordance. The bridge is retired by whichever
					// comes first: handleRemoteUserMessage, `generating` going true, the
					// queue emptying, or the work-state poll that markQueueDrainPending
					// arms alongside it reporting no drain (see `reconcileQueueDrain`).
					markQueueDrainPending();
				}
			}
		}
	});
	// Once a generation is actually attached (generating true) or there is nothing
	// left to drain, the drain-pending input-bar bridge is no longer needed.
	$effect(() => {
		if (generating) clearQueueDrainPending();
	});
	$effect(() => {
		if ((queue?.length ?? 0) === 0) clearQueueDrainPending();
	});
</script>

<svelte:head>
	<title>
		{$settings.showChatTitleInTab !== false && $chatTitle
			? `${$chatTitle.length > 30 ? `${$chatTitle.slice(0, 30)}...` : $chatTitle} • ${$WEBUI_NAME}`
			: `${$WEBUI_NAME}`}
	</title>
</svelte:head>

<audio id="audioElement" src="" style="display: none;"></audio>

<EventConfirmDialog
	bind:show={showEventConfirmation}
	title={eventConfirmationTitle}
	message={eventConfirmationMessage}
	input={eventConfirmationInput}
	inputPlaceholder={eventConfirmationInputPlaceholder}
	inputValue={eventConfirmationInputValue}
	onconfirm={(e) => {
		if (e.detail) {
			eventCallback(e.detail);
		} else {
			eventCallback(true);
		}
	}}
	oncancel={() => {
		eventCallback(false);
	}}
/>

<div
	class="h-screen max-h-[100dvh] transition-width duration-200 ease-in-out {$showSidebar
		? '  md:max-w-[calc(100%-260px)]'
		: ' '} w-full max-w-full flex flex-col"
	id="chat-container"
>
	{#if !loading}
		<div in:fade={{ duration: 50 }} class="w-full h-full flex flex-col">
			{#if $selectedFolder && $selectedFolder?.meta?.background_image_url}
				<div
					class="absolute {$showSidebar
						? 'md:max-w-[calc(100%-260px)] md:translate-x-[260px]'
						: ''} top-0 left-0 w-full h-full bg-cover bg-center bg-no-repeat"
					style="background-image: url({$selectedFolder?.meta?.background_image_url})  "
				></div>

				<div
					class="absolute top-0 left-0 w-full h-full bg-linear-to-t from-white to-white/85 dark:from-gray-900 dark:to-gray-900/90 z-0"
				></div>
			{:else if $settings?.backgroundImageUrl ?? $config?.license_metadata?.background_image_url ?? null}
				<div
					class="absolute {$showSidebar
						? 'md:max-w-[calc(100%-260px)] md:translate-x-[260px]'
						: ''} top-0 left-0 w-full h-full bg-cover bg-center bg-no-repeat"
					style="background-image: url({$settings?.backgroundImageUrl ??
						$config?.license_metadata?.background_image_url})  "
				></div>

				<div
					class="absolute top-0 left-0 w-full h-full bg-linear-to-t from-white to-white/85 dark:from-gray-900 dark:to-gray-900/90 z-0"
				></div>
			{/if}

			<PaneGroup direction="horizontal" class="w-full h-full">
				<Pane defaultSize={50} minSize={30} class="h-full flex relative max-w-full flex-col">
					<Navbar
						bind:this={navbarElement}
						chat={{
							id: activeChatId,
							chat: {
								title: $chatTitle,
								models: selectedModels,
								system: $settings.system ?? undefined,
								params: params,
								history: history,
								timestamp: Date.now()
							}
						}}
						{history}
						title={$chatTitle}
						{selectedModels}
						onModelsChange={handleSelectedModelsChange}
						shareEnabled={!!history.currentId}
						{initNewChat}
						archiveChatHandler={() => {}}
						{moveChatHandler}
						onSaveTempChat={saveTempChatHandler}
					/>

					<div class="flex flex-col flex-auto z-10 w-full @container overflow-auto">
						{#if offlineCopyInfo}
							<div
								class="mx-auto w-full max-w-full px-3 pt-1.5 pb-1 text-xs text-center text-gray-500 dark:text-gray-400 flex-none"
							>
								{$i18n.t('Offline copy')} · {$i18n.t('saved {{time}}', {
									time: dayjs(offlineCopyInfo.storedAt).fromNow()
								})}
							</div>
						{/if}
						{#if ($settings?.landingPageMode === 'chat' && !$selectedFolder) || history.currentId !== null}
							<div
								class=" pb-2.5 flex flex-col justify-between w-full flex-auto overflow-auto h-0 max-w-full z-10 scrollbar-hidden"
								id="messages-container"
								style="overflow-anchor: none; overscroll-behavior: contain;"
								bind:this={messagesContainerElement}
								onscroll={onScroll}
								onpointerdown={onPointerDown}
								onkeydown={onContainerKeyDown}
								onclickcapture={armExpansionHold}
								use:passive={['wheel', () => onWheel]}
								use:passive={['touchstart', () => onTouchStart]}
								use:passive={['touchmove', () => onTouchMove]}
								use:passive={['touchend', () => onTouchEnd]}
								onsubagentexpand={() => {
									// User expanded a subagent card to read it — stop following
									// the stream so the ResizeObserver / auto-scroll doesn't yank
									// the viewport to the bottom as the body (and any ongoing
									// generation) grows the page.
									disengageAutoScroll();
								}}
								onsubagentcollapse={() => {
									// User collapsed the card. Resume following the stream ONLY if
									// they're back near the bottom — otherwise collapsing a card
									// they scrolled up to read would yank them down. Without this,
									// auto-scroll stayed off for the rest of the turn.
									if (isNearBottom(messagesContainerElement)) autoScroll = true;
								}}
								onsubagentrewindredo={(e) => {
									// A subagent redo was blocked because the parent moved on, and the
									// user confirmed "Rewind & redo" in the card. Rewind the parent to
									// a sibling branch, redo the subagent there, then resume the parent.
									rewindRedoSubagent(e.detail);
								}}
								onsubagentrewindadopt={(e) => {
									rewindAdoptSubagents(e.detail);
								}}
							>
								<div
									class=" h-full w-full flex flex-col"
									style="opacity: {messagesReady ? 1 : 0}; transition: opacity 80ms ease;"
									bind:this={messagesContentElement}
								>
									<Messages
										chatId={activeChatId}
										bind:history
										structureRevision={messageStructureRevision}
										bind:autoScroll
										allowPagination={initialScrollSettled}
										scrollReady={messagesReady}
										bind:prompt
										setInputText={(text) => {
											messageInput?.setText(text);
										}}
										{selectedModels}
										{atSelectedModel}
										{sendMessage}
										{prepareBranchReplacement}
										{showMessage}
										{submitMessage}
										{continueResponse}
										{regenerateResponse}
										{rewindAndInsert}
										{retryWithoutProviderRestrictions}
										{markSkipRemainingRetries}
										{regenerateWithModel}
										{mergeResponses}
										{chatActionHandler}
										topPadding={true}
									bottomPadding={files.length > 0}
									{onSelect}
								/>
							</div>
							</div>

							<!-- Token Usage Display (kb-hide: reference info, not worth rows
							     of space while the on-screen keyboard is up) -->
							{#if (relevantGroups.length > 0 || relevantSubscriptions.length > 0) && !hideBottomChromeForEdit}
								<div class="kb-hide mx-auto inset-x-0 flex justify-center w-full">
									<div
										class="px-3 pb-2 w-full {($settings?.widescreenMode ?? null)
											? 'max-w-full'
											: 'max-w-6xl'}"
									>
										<div class="bg-gray-50 dark:bg-gray-850 rounded-lg p-3 text-xs">
											{#each relevantGroups as [groupName, groupData]}
												{@const effectiveUsage = groupData.effectiveUsage}
												{@const isOverLimit =
													groupData.limit && effectiveUsage.total > groupData.limit}
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
															>{formatSubscriptionLimitLabel(sub.name, w)} · {formatWindowLabel(
																w
															)}</span
														>
														<div
															class="flex items-center gap-2 min-w-0 {ratio >= 1
																? 'text-error-brick dark:text-error-brick-dark'
																: 'text-gray-600 dark:text-gray-400'}"
														>
															{#if w.resets_at}
																<span class="truncate text-gray-400 dark:text-gray-500"
																	>{formatResetsIn(w.resets_at, _nowTick)}</span
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
								</div>

								<!-- Typing-mode stand-in (kb-only): one thin line with a meter for
								     the group nearest its limit, so limit awareness survives the
								     keyboard without the panel's ~50px. -->
								{#if kbTokenSummary}
									<div class="kb-only mx-auto inset-x-0 justify-center w-full">
										<div
											class="px-3 pb-0.5 w-full {($settings?.widescreenMode ?? null)
												? 'max-w-full'
												: 'max-w-6xl'}"
										>
											<div
												class="flex items-center gap-2 text-[10px] leading-4 {kbTokenSummary.ratio >=
												1
													? 'text-error-brick dark:text-error-brick-dark'
													: kbTokenSummary.ratio >= 0.8
														? 'text-warning dark:text-warning-dark'
														: 'text-gray-500'}"
											>
												<span class="font-medium truncate max-w-[35%]">{kbTokenSummary.name}</span>
												{#if kbTokenSummary.subscription}
													<div
														class="flex-1 h-[3px] rounded-full bg-gray-200 dark:bg-gray-800 overflow-hidden"
													>
														<div
															class="h-full rounded-full {kbTokenSummary.ratio >= 1
																? 'bg-error-brick dark:bg-error-brick-dark'
																: kbTokenSummary.ratio >= 0.8
																	? 'bg-warning dark:bg-warning-dark'
																	: 'bg-gray-400 dark:bg-gray-600'}"
															style="width: {Math.min(
																100,
																Math.round(kbTokenSummary.ratio * 100)
															)}%"
														></div>
													</div>
													<span class="shrink-0 tabular-nums"
														>{formatUsedPercent(kbTokenSummary.window)}</span
													>
												{:else if kbTokenSummary.limit}
													<div
														class="flex-1 h-[3px] rounded-full bg-gray-200 dark:bg-gray-800 overflow-hidden"
													>
														<div
															class="h-full rounded-full {kbTokenSummary.ratio >= 1
																? 'bg-error-brick dark:bg-error-brick-dark'
																: kbTokenSummary.ratio >= 0.8
																	? 'bg-warning dark:bg-warning-dark'
																	: 'bg-gray-400 dark:bg-gray-600'}"
															style="width: {Math.min(
																100,
																Math.round(kbTokenSummary.ratio * 100)
															)}%"
														></div>
													</div>
													<span class="shrink-0 tabular-nums"
														>{formatTokensCompact(kbTokenSummary.total)} / {formatTokensCompact(
															kbTokenSummary.limit
														)}</span
													>
												{:else}
													<span class="shrink-0 tabular-nums ml-auto"
														>{formatTokensCompact(kbTokenSummary.total)}
														{$i18n.t('tokens')}</span
													>
												{/if}
											</div>
										</div>
									</div>
								{/if}
							{/if}

							<div
							class=" pb-composer relative"
							class:hidden={hideBottomChromeForEdit}
							bind:this={composerElement}
						>
								{#if showJumpToBottom}
									<!-- Floating jump-to-bottom: getting back to the latest message
									     from deep scrollback was a long manual scroll (no affordance
									     existed). Sits above the composer, out of the content flow. -->
									<div
										class="absolute bottom-full left-0 right-0 mb-3 flex justify-center pointer-events-none z-20"
									>
										<!-- Pressing this must not move focus. Pointer-down on a button
										     blurs whatever was focused, so mid-typing this dropped the
										     keyboard (and with it typing mode) and only THEN scrolled —
										     you got sent back to the full-height layout for asking to see
										     the bottom of the conversation. Cancelling the pointerdown
										     default keeps the caret where it was; per the Pointer Events
										     spec it suppresses the compatibility mouse events but not the
										     click, so the button still fires. (Same trick every rich-text
										     toolbar uses to keep the editor's selection.) -->
										<button
											type="button"
											class="pointer-events-auto tap-target flex items-center justify-center size-9 rounded-full bg-white dark:bg-gray-850 border border-gray-100 dark:border-gray-800 shadow-lg text-gray-600 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-800 transition"
											aria-label={$i18n.t('Scroll to bottom')}
											onpointerdown={(e) => e.preventDefault()}
											onclick={() => engageAndScrollToBottom('smooth')}
										>
											<ChevronDown className="size-4" strokeWidth="2" />
										</button>
									</div>
								{/if}
								<MessageInput
									bind:this={messageInput}
									{history}
									{turnLive}
									bind:selectedModels
									onSelectionTouched={markToolSelectionDirty}
									onServiceTierTouched={handleServiceTierTouched}
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
									toolServers={$toolServers}
									{stopResponse}
									{queue}
									{editQueuedMessage}
									{removeQueuedMessage}
									{sendQueuedNow}
									{userInitiatedStop}
									{createMessagePair}
									onChange={(data) => {
										if (!$temporaryChatEnabled) {
											// Stamp whether the tool/feature selection is user-curated so
											// the restore path knows if the draft's tool state is real
											// intent or just captured programmatic defaults.
											saveDraft({ ...data, toolSelectionDirty }, getDraftChatId());
										}
										// Capture reasoning effort from MessageInput (only if changed to prevent infinite loop)
										if (data.reasoning && data.reasoning.effort !== reasoning.effort) {
											reasoning = data.reasoning;
										}
									}}
									onupload={async (e) => {
										const { type, data } = e.detail;

										if (type === 'web') {
											await uploadWeb(data);
										} else if (type === 'youtube') {
											await uploadYoutubeTranscription(data);
										} else if (type === 'google-drive') {
											await uploadGoogleDriveFile(data);
										}
									}}
									onsubmit={async (e) => {
										clearDraft(getDraftChatId());
										if (e.detail || files.length > 0) {
											await tick();

											submitPrompt(e.detail.replaceAll('\n\n', '\n'));
										}
									}}
									onsteer={async (e) => {
										clearDraft(getDraftChatId());
										if (e.detail || files.length > 0) {
											await tick();
											steerMessage(e.detail.replaceAll('\n\n', '\n'));
										}
									}}
									onqueueAfterFinal={async (e) => {
										clearDraft(getDraftChatId());
										if (e.detail || files.length > 0) {
											await tick();
											enqueueMessage(e.detail.replaceAll('\n\n', '\n'));
										}
									}}
								/>

								<div
									class="absolute bottom-1 text-xs text-gray-500 text-center line-clamp-1 right-0 left-0"
								>
									<!-- {$i18n.t('LLMs can make mistakes. Verify important information.')} -->
								</div>
							</div>
						{:else}
							<!-- max-md: top-align the new-chat placeholder. Vertically centering
							     it puts the composer mid-screen, and iOS (which ignores
							     interactive-widget=resizes-content) pans the page to the focused
							     input when the keyboard opens — scrolling the navbar (and its
							     model selector) out of reach. Top-aligned, everything the user
							     needs stays above the keyboard. -->
							<div class="flex items-center max-md:items-start h-full max-md:overflow-y-auto">
								<Placeholder
									{relevantGroups}
									{relevantSubscriptions}
									{history}
									bind:selectedModels
									onSelectionTouched={markToolSelectionDirty}
									onServiceTierTouched={handleServiceTierTouched}
									bind:messageInput
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
									toolServers={$toolServers}
									{stopResponse}
									{createMessagePair}
									{onSelect}
									onChange={(data) => {
										if (!$temporaryChatEnabled) {
											saveDraft({ ...data, toolSelectionDirty }, getDraftChatId());
										}
									}}
									onupload={async (e) => {
										const { type, data } = e.detail;

										if (type === 'web') {
											await uploadWeb(data);
										} else if (type === 'youtube') {
											await uploadYoutubeTranscription(data);
										}
									}}
									onsubmit={async (e) => {
										clearDraft(getDraftChatId());
										if (e.detail || files.length > 0) {
											await tick();
											submitPrompt(e.detail.replaceAll('\n\n', '\n'));
										}
									}}
								/>
							</div>
						{/if}
					</div>
				</Pane>

				<ChatControls
					bind:this={controlPaneComponent}
					bind:history
					bind:chatFiles
					bind:params
					bind:files
					bind:pane={controlPane}
					chatId={activeChatId}
					modelId={selectedModelIds?.at(0) ?? null}
					models={selectedModelIds.reduce((a, e, i, arr) => {
						const model = $models.find((m) => m.id === e);
						if (model) {
							return [...a, model];
						}
						return a;
					}, [])}
					{submitPrompt}
					{stopResponse}
					{showMessage}
					{eventTarget}
				/>
			</PaneGroup>
		</div>
	{:else if loading}
		<div class="flex flex-col h-full w-full">
			<div class="flex items-center w-full px-4 h-12">
				<div class="h-5 w-48 bg-gray-200 dark:bg-gray-800 rounded animate-pulse mx-auto"></div>
			</div>
			<div class="flex flex-col flex-1 overflow-hidden px-6 pt-4 gap-6">
				{#each Array(3) as _, i}
					<div class="flex gap-3 {i % 2 === 1 ? 'justify-end' : ''}">
						{#if i % 2 === 0}
							<div
								class="size-7 rounded-full bg-gray-200 dark:bg-gray-800 animate-pulse flex-shrink-0"
							></div>
						{/if}
						<div
							class="flex flex-col gap-1.5 {i % 2 === 1 ? 'items-end' : ''}"
							style="max-width: 65%;"
						>
							<div
								class="h-3.5 rounded bg-gray-200 dark:bg-gray-800 animate-pulse"
								style="width: {120 + i * 40}px"
							></div>
							<div
								class="h-3.5 rounded bg-gray-200 dark:bg-gray-800 animate-pulse"
								style="width: {180 + i * 20}px"
							></div>
						</div>
					</div>
				{/each}
			</div>
			<div class="px-4 pb-4">
				<div class="h-12 rounded-xl bg-gray-200 dark:bg-gray-800 animate-pulse"></div>
			</div>
		</div>
	{/if}
</div>
