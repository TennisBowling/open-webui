<script lang="ts">
	import fileSaver from 'file-saver';
	const { saveAs } = fileSaver;

	import {
		chats,
		user,
		settings,
		scrollPaginationEnabled,
		currentChatPage,
		pinnedChats
	} from '$lib/stores';

	import {
		archiveAllChats,
		deleteAllChats,
		getAllChats,
		getChatList,
		importChat,
		getPinnedChatList
	} from '$lib/apis/chats';
	import { getImportOrigin, convertOpenAIChats } from '$lib/utils';
	import { onMount, getContext } from 'svelte';
	import { goto } from '$app/navigation';
	import { toast } from '$lib/utils/toast';
	import ArchivedChatsModal from '$lib/components/layout/ArchivedChatsModal.svelte';
	import Switch from '$lib/components/common/Switch.svelte';
	import { purgeAll as purgeOfflineChatData } from '$lib/offline/chatStore';
	import {
		offlineChatMeta,
		refreshOfflineChatMeta,
		resetOfflineChatMeta,
		prefetchOfflineChats,
		purgeOfflineChatsForUser,
		requestPersistentStorage,
		scheduleOfflinePrefetch,
		type PrefetchProgress
	} from '$lib/offline/manager';
	import { online } from '$lib/stores';

	const i18n = getContext('i18n');

	interface Props {
		saveSettings: Function;
	}

	let { saveSettings }: Props = $props();

	// Chats
	let importFiles = $state();

	let showArchiveConfirm = $state(false);
	let showDeleteConfirm = $state(false);
	let showArchivedChatsModal = $state(false);

	let chatImportInputElement: HTMLInputElement = $state();

	// Offline storage
	let offlineChatStorage = $state(false);
	let clearingOfflineData = $state(false);
	let offlineStorageEstimate: { usage?: number; quota?: number } | null = $state(null);

	const formatBytes = (bytes?: number) => {
		if (bytes === undefined || bytes === null || Number.isNaN(bytes)) return '';
		if (bytes < 1024) return `${bytes} B`;
		const units = ['KB', 'MB', 'GB', 'TB'];
		let value = bytes;
		let unitIndex = -1;
		do {
			value /= 1024;
			unitIndex++;
		} while (value >= 1024 && unitIndex < units.length - 1);
		return `${value.toFixed(1)} ${units[unitIndex]}`;
	};

	const refreshOfflineStorageEstimate = async () => {
		try {
			if (navigator?.storage?.estimate) {
				offlineStorageEstimate = await navigator.storage.estimate();
			} else {
				offlineStorageEstimate = null;
			}
		} catch (e) {
			// best-effort only; hide the readout if unsupported/fails
			offlineStorageEstimate = null;
		}
	};

	const toggleOfflineChatStorage = async () => {
		saveSettings({ offlineChatStorage });
		if (offlineChatStorage && $user?.id) {
			requestPersistentStorage();
			await refreshOfflineChatMeta($user.id);
			// Start filling the store right away so the toggle has visible effect.
			scheduleOfflinePrefetch(localStorage.token, $user.id, 1000);
		}
	};

	const clearOfflineDataHandler = async () => {
		clearingOfflineData = true;
		try {
			// Scoped to the signed-in account — other accounts on a shared device
			// keep their own offline copies.
			if ($user?.id) {
				await purgeOfflineChatsForUser($user.id);
			} else {
				await purgeOfflineChatData();
			}
			resetOfflineChatMeta();
			toast.success($i18n.t('Offline data cleared.'));
		} catch (e) {
			toast.error(`${e}`);
		} finally {
			clearingOfflineData = false;
			refreshOfflineStorageEstimate();
		}
	};

	// Manual "download now" sweep — same bounded stale-only prefetch the app
	// runs in the background, but user-initiated with visible progress. Handy
	// right before going somewhere with no signal.
	let downloadProgress: PrefetchProgress | null = $state(null);
	let downloading = $state(false);
	const downloadRecentHandler = async () => {
		if (downloading || !$user?.id) return;
		if (!$online) {
			toast.error($i18n.t('You are offline.'));
			return;
		}
		downloading = true;
		downloadProgress = null;
		try {
			const result = await prefetchOfflineChats({
				token: localStorage.token,
				userId: $user.id,
				onProgress: (p) => {
					downloadProgress = p;
				}
			});
			if (result === null) {
				toast.error($i18n.t('Could not download chats right now.'));
			} else if (result.total === 0) {
				toast.success($i18n.t('Offline copies are already up to date.'));
			} else {
				toast.success(
					$i18n.t('{{COUNT}} chats downloaded for offline access.', {
						COUNT: `${result.downloaded}`
					})
				);
			}
		} finally {
			downloading = false;
			downloadProgress = null;
			refreshOfflineStorageEstimate();
		}
	};

	const importChats = async (_chats) => {
		for (const chat of _chats) {
			console.log(chat);

			if (chat.chat) {
				await importChat(
					localStorage.token,
					chat.chat,
					chat.meta ?? {},
					false,
					null,
					chat?.created_at ?? null,
					chat?.updated_at ?? null
				);
			} else {
				// Legacy format
				await importChat(localStorage.token, chat, {}, false, null);
			}
		}

		currentChatPage.set(1);
		await chats.set(await getChatList(localStorage.token, $currentChatPage));
		pinnedChats.set(await getPinnedChatList(localStorage.token));
		scrollPaginationEnabled.set(true);
	};

	const exportChats = async () => {
		let blob = new Blob([JSON.stringify(await getAllChats(localStorage.token))], {
			type: 'application/json'
		});
		saveAs(blob, `chat-export-${Date.now()}.json`);
	};

	const archiveAllChatsHandler = async () => {
		await goto('/');
		await archiveAllChats(localStorage.token).catch((error) => {
			toast.error(`${error}`);
		});

		currentChatPage.set(1);
		await chats.set(await getChatList(localStorage.token, $currentChatPage));
		pinnedChats.set([]);
		scrollPaginationEnabled.set(true);
	};

	const deleteAllChatsHandler = async () => {
		await goto('/');
		const res = await deleteAllChats(localStorage.token).catch((error) => {
			toast.error(`${error}`);
			return null;
		});

		// Purge local offline copies only once the server confirmed the delete —
		// a failed call (e.g. offline) must not destroy still-readable copies.
		if (res && $user?.id) {
			void purgeOfflineChatsForUser($user.id);
		}

		currentChatPage.set(1);
		await chats.set(await getChatList(localStorage.token, $currentChatPage));
		scrollPaginationEnabled.set(true);
	};

	const handleArchivedChatsChange = async () => {
		currentChatPage.set(1);
		await chats.set(await getChatList(localStorage.token, $currentChatPage));

		scrollPaginationEnabled.set(true);
	};

	onMount(() => {
		offlineChatStorage = $settings?.offlineChatStorage ?? false;
		refreshOfflineStorageEstimate();
		if (offlineChatStorage && $user?.id && $offlineChatMeta === null) {
			void refreshOfflineChatMeta($user.id);
		}
	});
	let offlineChatCount = $derived($offlineChatMeta?.size ?? null);
	let offlineKeptCount = $derived(
		$offlineChatMeta ? [...$offlineChatMeta.values()].filter((m) => m.pinned).length : 0
	);
	$effect(() => {
		if (importFiles) {
			console.log(importFiles);

			let reader = new FileReader();
			reader.onload = (event) => {
				let chats = JSON.parse(event.target.result);
				console.log(chats);
				if (getImportOrigin(chats) == 'openai') {
					try {
						chats = convertOpenAIChats(chats);
					} catch (error) {
						console.log('Unable to import chats:', error);
					}
				}
				importChats(chats);
			};

			if (importFiles.length > 0) {
				reader.readAsText(importFiles[0]);
			}
		}
	});
</script>

<ArchivedChatsModal bind:show={showArchivedChatsModal} onUpdate={handleArchivedChatsChange} />

<div id="tab-chats" class="flex flex-col h-full justify-between space-y-3 text-sm">
	<div class=" space-y-2 overflow-y-scroll max-h-[28rem] md:max-h-full">
		<div class="flex flex-col">
			<input
				id="chat-import-input"
				bind:this={chatImportInputElement}
				bind:files={importFiles}
				type="file"
				accept=".json"
				hidden
			/>
			<button
				class=" flex rounded-md py-2 px-3.5 w-full hover:bg-gray-200 dark:hover:bg-gray-800 transition"
				onclick={() => {
					chatImportInputElement.click();
				}}
			>
				<div class=" self-center mr-3">
					<svg
						xmlns="http://www.w3.org/2000/svg"
						viewBox="0 0 16 16"
						fill="currentColor"
						class="w-4 h-4"
					>
						<path
							fill-rule="evenodd"
							d="M4 2a1.5 1.5 0 0 0-1.5 1.5v9A1.5 1.5 0 0 0 4 14h8a1.5 1.5 0 0 0 1.5-1.5V6.621a1.5 1.5 0 0 0-.44-1.06L9.94 2.439A1.5 1.5 0 0 0 8.878 2H4Zm4 9.5a.75.75 0 0 1-.75-.75V8.06l-.72.72a.75.75 0 0 1-1.06-1.06l2-2a.75.75 0 0 1 1.06 0l2 2a.75.75 0 1 1-1.06 1.06l-.72-.72v2.69a.75.75 0 0 1-.75.75Z"
							clip-rule="evenodd"
						/>
					</svg>
				</div>
				<div class=" self-center text-sm font-medium">{$i18n.t('Import Chats')}</div>
			</button>

			{#if $user?.role === 'admin' || ($user.permissions?.chat?.export ?? true)}
				<button
					class=" flex rounded-md py-2 px-3.5 w-full hover:bg-gray-200 dark:hover:bg-gray-800 transition"
					onclick={() => {
						exportChats();
					}}
				>
					<div class=" self-center mr-3">
						<svg
							xmlns="http://www.w3.org/2000/svg"
							viewBox="0 0 16 16"
							fill="currentColor"
							class="w-4 h-4"
						>
							<path
								fill-rule="evenodd"
								d="M4 2a1.5 1.5 0 0 0-1.5 1.5v9A1.5 1.5 0 0 0 4 14h8a1.5 1.5 0 0 0 1.5-1.5V6.621a1.5 1.5 0 0 0-.44-1.06L9.94 2.439A1.5 1.5 0 0 0 8.878 2H4Zm4 3.5a.75.75 0 0 1 .75.75v2.69l.72-.72a.75.75 0 1 1 1.06 1.06l-2 2a.75.75 0 0 1-1.06 0l-2-2a.75.75 0 0 1 1.06-1.06l.72.72V6.25A.75.75 0 0 1 8 5.5Z"
								clip-rule="evenodd"
							/>
						</svg>
					</div>
					<div class=" self-center text-sm font-medium">{$i18n.t('Export Chats')}</div>
				</button>
			{/if}
		</div>

		<hr class=" border-gray-100 dark:border-gray-850" />

		<div class="flex flex-col">
			<button
				class=" flex rounded-md py-2 px-3.5 w-full hover:bg-gray-200 dark:hover:bg-gray-800 transition"
				onclick={() => {
					showArchivedChatsModal = true;
				}}
			>
				<div class=" self-center mr-3">
					<svg
						xmlns="http://www.w3.org/2000/svg"
						viewBox="0 0 24 24"
						fill="currentColor"
						class="size-4"
					>
						<path
							d="M3.375 3C2.339 3 1.5 3.84 1.5 4.875v.75c0 1.036.84 1.875 1.875 1.875h17.25c1.035 0 1.875-.84 1.875-1.875v-.75C22.5 3.839 21.66 3 20.625 3H3.375Z"
						/>
						<path
							fill-rule="evenodd"
							d="m3.087 9 .54 9.176A3 3 0 0 0 6.62 21h10.757a3 3 0 0 0 2.995-2.824L20.913 9H3.087ZM12 10.5a.75.75 0 0 1 .75.75v4.94l1.72-1.72a.75.75 0 1 1 1.06 1.06l-3 3a.75.75 0 0 1-1.06 0l-3-3a.75.75 0 1 1 1.06-1.06l1.72 1.72v-4.94a.75.75 0 0 1 .75-.75Z"
							clip-rule="evenodd"
						/>
					</svg>
				</div>
				<div class=" self-center text-sm font-medium">{$i18n.t('Archived Chats')}</div>
			</button>

			{#if showArchiveConfirm}
				<div class="flex justify-between rounded-md items-center py-2 px-3.5 w-full transition">
					<div class="flex items-center space-x-3">
						<svg
							xmlns="http://www.w3.org/2000/svg"
							viewBox="0 0 16 16"
							fill="currentColor"
							class="w-4 h-4"
						>
							<path d="M2 3a1 1 0 0 1 1-1h10a1 1 0 0 1 1 1v1a1 1 0 0 1-1 1H3a1 1 0 0 1-1-1V3Z" />
							<path
								fill-rule="evenodd"
								d="M13 6H3v6a2 2 0 0 0 2 2h6a2 2 0 0 0 2-2V6ZM5.72 7.47a.75.75 0 0 1 1.06 0L8 8.69l1.22-1.22a.75.75 0 1 1 1.06 1.06L9.06 9.75l1.22 1.22a.75.75 0 1 1-1.06 1.06L8 10.81l-1.22 1.22a.75.75 0 0 1-1.06-1.06l1.22-1.22-1.22-1.22a.75.75 0 0 1 0-1.06Z"
								clip-rule="evenodd"
							/>
						</svg>
						<span>{$i18n.t('Are you sure?')}</span>
					</div>

					<div class="flex space-x-1.5 items-center">
						<button
							class="hover:text-white transition"
							onclick={() => {
								archiveAllChatsHandler();
								showArchiveConfirm = false;
							}}
						>
							<svg
								xmlns="http://www.w3.org/2000/svg"
								viewBox="0 0 20 20"
								fill="currentColor"
								class="w-4 h-4"
							>
								<path
									fill-rule="evenodd"
									d="M16.704 4.153a.75.75 0 01.143 1.052l-8 10.5a.75.75 0 01-1.127.075l-4.5-4.5a.75.75 0 011.06-1.06l3.894 3.893 7.48-9.817a.75.75 0 011.05-.143z"
									clip-rule="evenodd"
								/>
							</svg>
						</button>
						<button
							class="hover:text-white transition"
							onclick={() => {
								showArchiveConfirm = false;
							}}
						>
							<svg
								xmlns="http://www.w3.org/2000/svg"
								viewBox="0 0 20 20"
								fill="currentColor"
								class="w-4 h-4"
							>
								<path
									d="M6.28 5.22a.75.75 0 00-1.06 1.06L8.94 10l-3.72 3.72a.75.75 0 101.06 1.06L10 11.06l3.72 3.72a.75.75 0 101.06-1.06L11.06 10l3.72-3.72a.75.75 0 00-1.06-1.06L10 8.94 6.28 5.22z"
								/>
							</svg>
						</button>
					</div>
				</div>
			{:else}
				<button
					class=" flex rounded-md py-2 px-3.5 w-full hover:bg-gray-200 dark:hover:bg-gray-800 transition"
					onclick={() => {
						showArchiveConfirm = true;
					}}
				>
					<div class=" self-center mr-3">
						<svg
							xmlns="http://www.w3.org/2000/svg"
							viewBox="0 0 24 24"
							fill="currentColor"
							class="size-4"
						>
							<path
								d="M3.375 3C2.339 3 1.5 3.84 1.5 4.875v.75c0 1.036.84 1.875 1.875 1.875h17.25c1.035 0 1.875-.84 1.875-1.875v-.75C22.5 3.839 21.66 3 20.625 3H3.375Z"
							/>
							<path
								fill-rule="evenodd"
								d="m3.087 9 .54 9.176A3 3 0 0 0 6.62 21h10.757a3 3 0 0 0 2.995-2.824L20.913 9H3.087Zm6.163 3.75A.75.75 0 0 1 10 12h4a.75.75 0 0 1 0 1.5h-4a.75.75 0 0 1-.75-.75Z"
								clip-rule="evenodd"
							/>
						</svg>
					</div>
					<div class=" self-center text-sm font-medium">{$i18n.t('Archive All Chats')}</div>
				</button>
			{/if}

			{#if showDeleteConfirm}
				<div class="flex justify-between rounded-md items-center py-2 px-3.5 w-full transition">
					<div class="flex items-center space-x-3">
						<svg
							xmlns="http://www.w3.org/2000/svg"
							viewBox="0 0 16 16"
							fill="currentColor"
							class="w-4 h-4"
						>
							<path d="M2 3a1 1 0 0 1 1-1h10a1 1 0 0 1 1 1v1a1 1 0 0 1-1 1H3a1 1 0 0 1-1-1V3Z" />
							<path
								fill-rule="evenodd"
								d="M13 6H3v6a2 2 0 0 0 2 2h6a2 2 0 0 0 2-2V6ZM5.72 7.47a.75.75 0 0 1 1.06 0L8 8.69l1.22-1.22a.75.75 0 1 1 1.06 1.06L9.06 9.75l1.22 1.22a.75.75 0 1 1-1.06 1.06L8 10.81l-1.22 1.22a.75.75 0 0 1-1.06-1.06l1.22-1.22-1.22-1.22a.75.75 0 0 1 0-1.06Z"
								clip-rule="evenodd"
							/>
						</svg>
						<span>{$i18n.t('Are you sure?')}</span>
					</div>

					<div class="flex space-x-1.5 items-center">
						<button
							class="hover:text-white transition"
							onclick={() => {
								deleteAllChatsHandler();
								showDeleteConfirm = false;
							}}
						>
							<svg
								xmlns="http://www.w3.org/2000/svg"
								viewBox="0 0 20 20"
								fill="currentColor"
								class="w-4 h-4"
							>
								<path
									fill-rule="evenodd"
									d="M16.704 4.153a.75.75 0 01.143 1.052l-8 10.5a.75.75 0 01-1.127.075l-4.5-4.5a.75.75 0 011.06-1.06l3.894 3.893 7.48-9.817a.75.75 0 011.05-.143z"
									clip-rule="evenodd"
								/>
							</svg>
						</button>
						<button
							class="hover:text-white transition"
							onclick={() => {
								showDeleteConfirm = false;
							}}
						>
							<svg
								xmlns="http://www.w3.org/2000/svg"
								viewBox="0 0 20 20"
								fill="currentColor"
								class="w-4 h-4"
							>
								<path
									d="M6.28 5.22a.75.75 0 00-1.06 1.06L8.94 10l-3.72 3.72a.75.75 0 101.06 1.06L10 11.06l3.72 3.72a.75.75 0 101.06-1.06L11.06 10l3.72-3.72a.75.75 0 00-1.06-1.06L10 8.94 6.28 5.22z"
								/>
							</svg>
						</button>
					</div>
				</div>
			{:else}
				<button
					class=" flex rounded-md py-2 px-3.5 w-full hover:bg-gray-200 dark:hover:bg-gray-800 transition"
					onclick={() => {
						showDeleteConfirm = true;
					}}
				>
					<div class=" self-center mr-3">
						<svg
							xmlns="http://www.w3.org/2000/svg"
							viewBox="0 0 16 16"
							fill="currentColor"
							class="w-4 h-4"
						>
							<path
								fill-rule="evenodd"
								d="M4 2a1.5 1.5 0 0 0-1.5 1.5v9A1.5 1.5 0 0 0 4 14h8a1.5 1.5 0 0 0 1.5-1.5V6.621a1.5 1.5 0 0 0-.44-1.06L9.94 2.439A1.5 1.5 0 0 0 8.878 2H4Zm7 7a.75.75 0 0 1-.75.75h-4.5a.75.75 0 0 1 0-1.5h4.5A.75.75 0 0 1 11 9Z"
								clip-rule="evenodd"
							/>
						</svg>
					</div>
					<div class=" self-center text-sm font-medium">{$i18n.t('Delete All Chats')}</div>
				</button>
			{/if}
		</div>

		<hr class=" border-gray-100 dark:border-gray-850" />

		<div class="flex flex-col px-3.5 py-1 gap-2">
			<div class=" py-0.5 flex w-full justify-between">
				<div id="offline-chat-storage-label" class=" self-center text-sm font-medium">
					{$i18n.t('Store chats for offline access')}
				</div>

				<div class="flex items-center gap-2 p-1">
					<Switch
						ariaLabelledbyId="offline-chat-storage-label"
						tooltip={true}
						bind:state={offlineChatStorage}
						onchange={toggleOfflineChatStorage}
					/>
				</div>
			</div>

			<div class=" text-xs text-gray-500">
				{$i18n.t(
					'Chat messages will be saved on this device, including content. Turn off and clear if this is a shared device.'
				)}
			</div>

			{#if offlineChatStorage}
				<div class="flex items-center justify-between mt-1">
					<button
						class=" flex rounded-md py-1.5 px-3 text-xs hover:bg-gray-200 dark:hover:bg-gray-800 transition border border-gray-100 dark:border-gray-850 disabled:opacity-50"
						disabled={downloading}
						onclick={downloadRecentHandler}
					>
						{#if downloading}
							{downloadProgress && downloadProgress.total > 0
								? $i18n.t('Downloading... {{DONE}}/{{TOTAL}}', {
										DONE: `${downloadProgress.done}`,
										TOTAL: `${downloadProgress.total}`
									})
								: $i18n.t('Checking...')}
						{:else}
							{$i18n.t('Download recent chats')}
						{/if}
					</button>

					{#if offlineChatCount !== null}
						<div class=" text-xs text-gray-500">
							{offlineKeptCount > 0
								? $i18n.t('{{COUNT}} chats stored · {{KEPT}} kept offline', {
										COUNT: `${offlineChatCount}`,
										KEPT: `${offlineKeptCount}`
									})
								: $i18n.t('{{COUNT}} chats stored', { COUNT: `${offlineChatCount}` })}
						</div>
					{/if}
				</div>
			{/if}

			<div class="flex items-center justify-between mt-1">
				<button
					class=" flex rounded-md py-1.5 px-3 text-xs hover:bg-gray-200 dark:hover:bg-gray-800 transition border border-gray-100 dark:border-gray-850 disabled:opacity-50"
					disabled={clearingOfflineData}
					onclick={clearOfflineDataHandler}
				>
					{clearingOfflineData ? $i18n.t('Clearing...') : $i18n.t('Clear offline data')}
				</button>

				{#if offlineStorageEstimate?.usage !== undefined}
					<div class=" text-xs text-gray-500">
						{$i18n.t('Using approximately {{size}}', {
							size: formatBytes(offlineStorageEstimate.usage)
						})}
					</div>
				{/if}
			</div>
		</div>
	</div>
</div>
