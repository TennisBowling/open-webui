<script lang="ts">
	import { onMount, onDestroy, tick, getContext } from 'svelte';
	import { goto } from '$app/navigation';
	import { page } from '$app/stores';

	import dayjs from 'dayjs';

	import {
		settings,
		chatId,
		WEBUI_NAME,
		models,
		modelsLoaded,
		config,
		sharedContext,
		showFilePreview,
		previewFile,
		previewFileSiblings
	} from '$lib/stores';
	import { convertMessagesToHistory, createMessagesList } from '$lib/utils';

	import { getChatByShareId, cloneSharedChatById, getSharedChatModels } from '$lib/apis/chats';

	import Messages from '$lib/components/chat/Messages.svelte';
	import FilePreview from '$lib/components/chat/ChatControls/FilePreview.svelte';

	import { getUserById, getUserSettings } from '$lib/apis/users';
	import { getModels } from '$lib/apis';
	import { toast } from '$lib/utils/toast';
	import localizedFormat from 'dayjs/plugin/localizedFormat';

	const i18n = getContext('i18n');
	dayjs.extend(localizedFormat);

	let loaded = $state(false);

	let autoScroll = $state(true);
	let messagesContainerElement: HTMLDivElement;

	// let chatId = $page.params.id;
	let showModelSelector = false;
	let selectedModels = $state(['']);

	let chat = $state(null);
	let user = $state(null);

	let title = $state('');
	let files = [];

	let messages: any[] = $state([]);
	let history = $state({
		messages: {},
		currentId: null
	});

	let loadedSig: string | null = null;
	let latestSig: string | null = $state(null);
	let newMessagesAvailable = $state(false);
	let dismissedSig: string | null = $state(null);

	// Signature of the conversation's latest leaf: its id + whether it is done.
	// Freshness keys on this (not updated_at, which bumps on pin/rename/archive)
	// AND only signals on a COMPLETE leaf, so a viewer is never sent to a
	// mid-stream partial reply.
	const leafSignature = (history: any): string | null => {
		const cid = history?.currentId;
		if (!cid) return null;
		return `${cid}:${history?.messages?.[cid]?.done ? 1 : 0}`;
	};

	//////////////////////////
	// Web functions
	//////////////////////////

	const loadSharedChat = async () => {
		modelsLoaded.set(false);

		// Try to load user settings if logged in, otherwise use local storage
		if (localStorage.token) {
			const userSettings = await getUserSettings(localStorage.token).catch((error) => {
				console.error(error);
				return null;
			});

			if (userSettings) {
				settings.set(userSettings.ui);
			} else {
				let localStorageSettings = {} as Parameters<(typeof settings)['set']>[0];

				try {
					localStorageSettings = JSON.parse(localStorage.getItem('settings') ?? '{}');
				} catch (e: unknown) {
					console.error('Failed to parse settings from localStorage', e);
				}

				settings.set(localStorageSettings);
			}

			try {
				models.set(
					await getModels(
						localStorage.token,
						$config?.features?.enable_direct_connections && ($settings?.directConnections ?? null)
					)
				);
			} finally {
				modelsLoaded.set(true);
			}
		} else {
			// Anonymous user - use default/local settings
			let localStorageSettings = {} as Parameters<(typeof settings)['set']>[0];

			try {
				localStorageSettings = JSON.parse(localStorage.getItem('settings') ?? '{}');
			} catch (e: unknown) {
				console.error('Failed to parse settings from localStorage', e);
			}

			settings.set(localStorageSettings);
			modelsLoaded.set(true);
		}

		await chatId.set($page.params.id);
		chat = await getChatByShareId(localStorage.token ?? '', shareId).catch(async (error) => {
			await goto('/');
			return null;
		});

		if (chat) {
			sharedContext.set({ shareId });
			loadedSig = leafSignature(chat?.chat?.history);
			latestSig = null;
			newMessagesAvailable = false;
			dismissedSig = null;

			// Load share-scoped model display-names so model headers resolve for all viewers
			const shareModels = await getSharedChatModels(localStorage.token ?? '', shareId).catch(
				(error) => {
					console.error(error);
					return [];
				}
			);

			if (Array.isArray(shareModels) && shareModels.length > 0) {
				if (localStorage.token) {
					// Logged-in: merge any missing ids into the existing models list
					const existing = $models ?? [];
					const existingIds = new Set(existing.map((m) => m?.id));
					const merged = [...existing];
					for (const sm of shareModels) {
						if (sm?.id && !existingIds.has(sm.id)) {
							merged.push(sm);
						}
					}
					models.set(merged);
				} else {
					// Anonymous: getModels never ran, so seed from the share list
					models.set(shareModels);
				}
			}

			// Try to get user info if we have a token, otherwise just proceed without it
			if (localStorage.token) {
				user = await getUserById(localStorage.token, chat.user_id).catch((error) => {
					console.error(error);
					return null;
				});
			}

			const chatContent = chat.chat;

			if (chatContent) {
				console.log(chatContent);

				selectedModels =
					(chatContent?.models ?? undefined) !== undefined
						? chatContent.models
						: [chatContent.models ?? ''];
				history =
					(chatContent?.history ?? undefined) !== undefined
						? chatContent.history
						: convertMessagesToHistory(chatContent.messages);
				title = chatContent.title;

				autoScroll = true;
				await tick();

				if (messages.length > 0 && messages.at(-1)?.id && messages.at(-1)?.id in history.messages) {
					history.messages[messages.at(-1)?.id].done = true;
				}
				await tick();

				return true;
			} else {
				return null;
			}
		}
	};

	const cloneSharedChat = async () => {
		if (!chat) return;

		// Redirect to login if not authenticated
		if (!localStorage.token) {
			const currentUrl = `${window.location.pathname}${window.location.search}`;
			const encodedUrl = encodeURIComponent(currentUrl);
			await goto(`/auth?redirect=${encodedUrl}`);
			return;
		}

		const res = await cloneSharedChatById(localStorage.token, shareId).catch((error) => {
			toast.error(`${error}`);
			return null;
		});

		if (res) {
			goto(`/c/${res.id}`);
		}
	};

	const reloadSharedChat = async () => {
		newMessagesAvailable = false;
		dismissedSig = null;
		latestSig = null;
		loaded = false;
		if (await loadSharedChat()) {
			await tick();
			loaded = true;
		}
	};

	const checkForUpdates = async () => {
		// Detect newer content without heavy polling. Works for BOTH modes: in
		// live mode the original's leaf advances on new messages; in snapshot mode
		// the frozen clone's leaf changes only when the owner pushes "Update
		// snapshot". Keyed on the leaf signature (id + done) rather than
		// updated_at, so pin/rename/archive/move never raise a false signal.
		if (!chat || document.visibilityState === 'hidden') {
			return;
		}

		const latest = await getChatByShareId(localStorage.token ?? '', shareId).catch(() => null);
		const sig = leafSignature(latest?.chat?.history);

		// Only invite a refresh when the latest leaf is COMPLETE (":1") and differs
		// from what we loaded — never mid-stream, and it re-fires once a streamed
		// reply finishes (a viewer who refreshed mid-stream sees the ":0"→":1" flip).
		if (sig && sig !== loadedSig && sig.endsWith(':1')) {
			latestSig = sig;
			newMessagesAvailable = true;
		}
	};

	let pollInterval: ReturnType<typeof setInterval> | null = null;

	onMount(() => {
		document.addEventListener('visibilitychange', checkForUpdates);
		window.addEventListener('focus', checkForUpdates);
		// Light poll so a viewer watching a live share in a focused tab still gets
		// the "new messages" banner without switching away. Only live shares can
		// change behind a focused tab — a snapshot only moves when the owner pushes
		// an update, which the visibility/focus checks above cover; polling the
		// full chat blob every 30s for a frozen share was pure waste.
		pollInterval = setInterval(() => {
			if (shareMode === 'live') {
				checkForUpdates();
			}
		}, 30000);
	});

	onDestroy(() => {
		document.removeEventListener('visibilitychange', checkForUpdates);
		window.removeEventListener('focus', checkForUpdates);
		if (pollInterval) clearInterval(pollInterval);
		sharedContext.set({ shareId: null });
	});
	let shareMode = $derived(chat?.meta?.share?.mode ?? 'snapshot');
	// Coerced share id (the /s/[id] route always has a defined param); used
	// wherever a plain string is required so the calls stay type-clean.
	let shareId = $derived($page.params.id ?? '');
	$effect(() => {
		messages = createMessagesList(history, history.currentId);
	});
	$effect(() => {
		if ($page.params.id) {
			(async () => {
				if (await loadSharedChat()) {
					await tick();
					loaded = true;
				} else {
					await goto('/');
				}
			})();
		}
	});
</script>

<svelte:head>
	<title>
		{title
			? `${title.length > 30 ? `${title.slice(0, 30)}...` : title} • ${$WEBUI_NAME}`
			: `${$WEBUI_NAME}`}
	</title>
</svelte:head>

{#if loaded}
	<div
		class="h-screen max-h-[100dvh] w-full flex flex-col text-gray-700 dark:text-gray-100 bg-white dark:bg-gray-900"
	>
		<div class="flex flex-col flex-auto justify-center relative">
			{#if newMessagesAvailable && latestSig !== dismissedSig}
				<div class="absolute top-3 left-0 right-0 z-30 flex justify-center px-2">
					<div
						class="flex items-center gap-2 pl-3.5 pr-2 py-1 text-sm bg-manilla dark:bg-gray-850 border border-hairline rounded-full shadow-sm"
					>
						<span
							>{shareMode === 'live'
								? $i18n.t('New messages available')
								: $i18n.t('Updated version available')}</span
						>
						<button
							class="px-2.5 py-1 text-xs font-medium bg-book-cloth hover:bg-kraft text-white transition-colors duration-200 ease-paper rounded-full"
							onclick={reloadSharedChat}
						>
							{$i18n.t('Refresh')}
						</button>
						<button
							class="p-1 text-gray-500 hover:text-gray-700 dark:hover:text-gray-300 transition-colors duration-200 ease-paper rounded-full"
							aria-label={$i18n.t('Dismiss')}
							onclick={() => (dismissedSig = latestSig)}
						>
							<svg
								xmlns="http://www.w3.org/2000/svg"
								fill="none"
								viewBox="0 0 24 24"
								stroke-width="2"
								stroke="currentColor"
								class="w-3.5 h-3.5"
							>
								<path stroke-linecap="round" stroke-linejoin="round" d="M6 18 18 6M6 6l12 12" />
							</svg>
						</button>
					</div>
				</div>
			{/if}
			<div
				class="@container flex flex-col w-full flex-auto overflow-auto h-0"
				id="messages-container"
			>
				<div class="pt-5 px-2 w-full max-w-[1600px] mx-auto">
					<div class="px-3">
						<div class=" text-2xl font-semibold line-clamp-1">
							{title}
						</div>

						<div class="flex text-sm justify-between items-center mt-1">
							<div class="text-gray-400">
								{dayjs(chat.chat.timestamp).format('LLL')}
							</div>
						</div>
					</div>
				</div>

				<div class=" h-full w-full max-w-[1600px] mx-auto flex flex-col py-2">
					<div class="w-full">
						<Messages
							className="h-full flex pt-4 pb-8 "
							{user}
							chatId={$chatId}
							readOnly={true}
							widescreen={true}
							{selectedModels}
							bind:history
							bind:messages
							bind:autoScroll
							bottomPadding={files.length > 0}
							sendMessage={() => {}}
							continueResponse={() => {}}
							regenerateResponse={() => {}}
						/>
					</div>
				</div>
			</div>

			<div
				class="absolute bottom-0 right-0 left-0 flex justify-center w-full bg-linear-to-b from-transparent to-white dark:to-gray-900"
			>
				<div class="pb-5">
					<button
						class="px-3.5 py-1.5 text-sm font-medium bg-book-cloth hover:bg-kraft text-white dark:bg-book-cloth dark:text-white dark:hover:bg-kraft transition-colors duration-200 ease-paper rounded-full"
						onclick={cloneSharedChat}
					>
						{$i18n.t('Clone Chat')}
					</button>
				</div>
			</div>

			{#if $showFilePreview}
				<div class="fixed inset-0 z-40 flex justify-end">
					<button
						class="absolute inset-0 w-full h-full bg-black/30 dark:bg-black/50"
						aria-label={$i18n.t('Close')}
						onclick={() => {
							showFilePreview.set(false);
							previewFile.set(null);
							previewFileSiblings.set([]);
						}}
					></button>
					<div
						class="relative z-10 h-full w-full max-w-2xl bg-white dark:bg-gray-900 border-l border-hairline shadow-xl overflow-hidden"
					>
						<FilePreview />
					</div>
				</div>
			{/if}
		</div>
	</div>
{/if}
