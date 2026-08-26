<script lang="ts">
	import { getContext } from 'svelte';
	import { models, config } from '$lib/stores';

	import { toast } from '$lib/utils/toast';
	import {
		deleteSharedChatById,
		getChatById,
		getChatByShareId,
		shareChatById
	} from '$lib/apis/chats';
	import { copyToClipboard } from '$lib/utils';

	import Modal from '../common/Modal.svelte';
	import Link from '../icons/Link.svelte';
	import XMark from '$lib/components/icons/XMark.svelte';

	interface Props {
		chatId: any;
		show?: boolean;
	}

	let { chatId, show = $bindable(false) }: Props = $props();

	let chat = $state(null);
	let shareUrl = $state(null);
	let selectedMode = $state('snapshot');
	let sharedCurrentId = $state(null);
	const i18n = getContext('i18n');

	let shareLink = $derived(
		chat?.share_id ? `${window.location.origin}/s/${chat.share_id}` : shareUrl
	);

	// In snapshot mode the shared copy is frozen at its last push; it is stale
	// only if the owner's live conversation leaf has advanced past the snapshot's
	// leaf. Comparing history.currentId (not updated_at) means pinning, renaming,
	// archiving or moving the chat never falsely reports "new messages".
	let snapshotStale = $derived(
		chat?.share_id &&
			selectedMode === 'snapshot' &&
			!!chat?.chat?.history?.currentId &&
			chat.chat.history.currentId !== sharedCurrentId
	);

	const refreshChatState = async () => {
		const _chat = await getChatById(localStorage.token, chatId);
		chat = _chat;
		selectedMode = chat?.meta?.share?.mode ?? 'snapshot';
		if (chat?.share_id) {
			const shared = await getChatByShareId(localStorage.token, chat.share_id).catch(() => null);
			sharedCurrentId = shared?.chat?.history?.currentId ?? null;
		} else {
			sharedCurrentId = null;
		}
	};

	const shareLocalChat = async () => {
		const sharedChat = await shareChatById(localStorage.token, chatId, selectedMode);
		shareUrl = `${window.location.origin}/s/${sharedChat.id}`;
		await refreshChatState();

		return shareUrl;
	};

	const applyMode = async (mode) => {
		// Re-selecting the current mode is a no-op — pushing a fresh snapshot is a
		// separate, explicitly-labelled action ("Update snapshot").
		if (mode === selectedMode) return;
		const previous = selectedMode;
		selectedMode = mode;

		if (chat?.share_id) {
			try {
				await shareChatById(localStorage.token, chatId, mode);
				await refreshChatState();
				toast.success($i18n.t('Sharing settings updated'));
			} catch (error) {
				// Reconcile the optimistic switch back to reality so the control
				// never lies about the active mode.
				selectedMode = previous;
				toast.error(`${error}`);
			}
		}
	};

	const updateSnapshot = async () => {
		await shareChatById(localStorage.token, chatId, 'snapshot');
		await refreshChatState();
		toast.success($i18n.t('Snapshot updated'));
	};

	const stopSharing = async () => {
		const res = await deleteSharedChatById(localStorage.token, chatId);

		if (res) {
			await refreshChatState();
			shareUrl = null;
		}
	};

	const copyLink = async () => {
		if (shareLink) {
			copyToClipboard(shareLink);
			toast.success($i18n.t('Copied shared chat URL to clipboard!'));
		}
	};

	const shareChat = async () => {
		const _chat = chat.chat;

		toast.success($i18n.t('Redirecting you to Open WebUI Community'));
		const url = 'https://openwebui.com';

		const tab = await window.open(`${url}/chats/upload`, '_blank');
		window.addEventListener(
			'message',
			(event) => {
				if (event.origin !== url) return;
				if (event.data === 'loaded') {
					tab.postMessage(
						JSON.stringify({
							chat: _chat,
							models: $models.filter((m) => _chat.models.includes(m.id))
						}),
						'*'
					);
				}
			},
			false
		);
	};

	$effect(() => {
		if (show) {
			(async () => {
				if (chatId) {
					await refreshChatState();
				} else {
					chat = null;
				}
			})();
		}
	});
</script>

<Modal bind:show size="md">
	<div>
		<div class=" flex justify-between dark:text-gray-300 px-5 pt-4 pb-0.5">
			<div class=" text-lg font-medium self-center">{$i18n.t('Share Chat')}</div>
			<button
				class="tap-target self-center p-1 rounded-full text-gray-500 hover:text-gray-700 dark:text-gray-400 dark:hover:text-gray-200 hover:bg-gray-100 dark:hover:bg-gray-800 transition"
				onclick={() => {
					show = false;
				}}
			>
				<XMark className={'size-5'} />
			</button>
		</div>

		{#if chat}
			<div class="px-5 pt-3 pb-5 w-full flex flex-col justify-center">
				<div class="text-sm dark:text-gray-300 mb-3">
					{$i18n.t('Choose what people with the link can see.')}
				</div>

				<!-- Frozen / live mode control -->
				<div class="flex flex-col gap-2">
					<button
						type="button"
						class="text-left rounded-2xl border-hairline p-3 transition-colors duration-200 ease-paper {selectedMode ===
						'snapshot'
							? 'bg-manilla'
							: 'hover:bg-gray-50 dark:hover:bg-gray-850'}"
						onclick={() => applyMode('snapshot')}
					>
						<div class="flex items-center gap-2">
							<div
								class="size-4 rounded-full border-hairline flex items-center justify-center shrink-0 {selectedMode ===
								'snapshot'
									? 'border-book-cloth'
									: ''}"
							>
								{#if selectedMode === 'snapshot'}
									<div class="size-2 rounded-full bg-book-cloth"></div>
								{/if}
							</div>
							<div class="text-sm font-medium">{$i18n.t('Snapshot (frozen)')}</div>
						</div>
						<div class="text-xs text-gray-500 dark:text-gray-400 mt-1 pl-6">
							{$i18n.t(
								'Viewers see the conversation exactly as it is now. New messages you send stay private until you update the snapshot.'
							)}
						</div>
					</button>

					<button
						type="button"
						class="text-left rounded-2xl border-hairline p-3 transition-colors duration-200 ease-paper {selectedMode ===
						'live'
							? 'bg-manilla'
							: 'hover:bg-gray-50 dark:hover:bg-gray-850'}"
						onclick={() => applyMode('live')}
					>
						<div class="flex items-center gap-2">
							<div
								class="size-4 rounded-full border-hairline flex items-center justify-center shrink-0 {selectedMode ===
								'live'
									? 'border-book-cloth'
									: ''}"
							>
								{#if selectedMode === 'live'}
									<div class="size-2 rounded-full bg-book-cloth"></div>
								{/if}
							</div>
							<div class="text-sm font-medium">{$i18n.t('Live')}</div>
						</div>
						<div class="text-xs text-gray-500 dark:text-gray-400 mt-1 pl-6">
							{$i18n.t(
								'Viewers see your latest messages whenever they reload. New messages you send become visible.'
							)}
						</div>
					</button>
				</div>

				{#if chat.share_id}
					<!-- Existing link -->
					<div class="mt-4 flex items-center gap-2">
						<a
							href="/s/{chat.share_id}"
							target="_blank"
							class="flex-1 min-w-0 truncate text-sm px-3 py-1.5 rounded-full border-hairline bg-gray-50 dark:bg-gray-850 dark:text-gray-300"
							title={shareLink}
						>
							{shareLink}
						</a>
						<button
							type="button"
							class="shrink-0 flex items-center gap-1 px-3.5 py-1.5 text-sm font-medium bg-book-cloth hover:bg-kraft text-white dark:bg-book-cloth dark:text-white dark:hover:bg-kraft transition-colors duration-200 ease-paper rounded-full"
							onclick={copyLink}
						>
							<Link />
							{$i18n.t('Copy link')}
						</button>
					</div>

					<div
						class="text-xs mt-2 {snapshotStale
							? 'text-warning dark:text-warning-dark'
							: 'text-gray-500 dark:text-gray-400'}"
					>
						{#if selectedMode === 'live'}
							{$i18n.t('Viewers see your latest messages when they reload.')}
						{:else if snapshotStale}
							{$i18n.t(
								'You have sent new messages since this snapshot. Use “Update snapshot” to share them.'
							)}
						{:else}
							{$i18n.t('This snapshot is up to date.')}
						{/if}
					</div>

					<div class="flex flex-wrap items-center justify-between gap-2 mt-4">
						<button
							type="button"
							class="text-sm text-error-brick dark:text-error-brick-dark hover:underline"
							onclick={stopSharing}
						>
							{$i18n.t('Stop sharing')}
						</button>

						<div class="flex flex-wrap items-center gap-1">
							{#if $config?.features.enable_community_sharing}
								<button
									class="self-center flex items-center gap-1 px-3.5 py-1.5 text-sm font-medium bg-gray-100 hover:bg-gray-200 text-gray-800 dark:bg-gray-850 dark:text-white dark:hover:bg-gray-800 transition-colors duration-200 ease-paper rounded-full"
									type="button"
									onclick={() => {
										shareChat();
										show = false;
									}}
								>
									{$i18n.t('Share to Open WebUI Community')}
								</button>
							{/if}

							{#if selectedMode === 'snapshot'}
								<button
									type="button"
									class="self-center flex items-center gap-1 px-3.5 py-1.5 text-sm font-medium bg-book-cloth hover:bg-kraft text-white dark:bg-book-cloth dark:text-white dark:hover:bg-kraft transition-colors duration-200 ease-paper rounded-full"
									onclick={updateSnapshot}
								>
									{$i18n.t('Update snapshot')}
								</button>
							{/if}
						</div>
					</div>
				{:else}
					<!-- Not yet shared -->
					<div class="flex flex-wrap justify-end gap-1 mt-4">
						{#if $config?.features.enable_community_sharing}
							<button
								class="self-center flex items-center gap-1 px-3.5 py-1.5 text-sm font-medium bg-gray-100 hover:bg-gray-200 text-gray-800 dark:bg-gray-850 dark:text-white dark:hover:bg-gray-800 transition-colors duration-200 ease-paper rounded-full"
								type="button"
								onclick={() => {
									shareChat();
									show = false;
								}}
							>
								{$i18n.t('Share to Open WebUI Community')}
							</button>
						{/if}

						<button
							class="self-center flex items-center gap-1 px-3.5 py-1.5 text-sm font-medium bg-book-cloth hover:bg-kraft text-white dark:bg-book-cloth dark:text-white dark:hover:bg-kraft transition-colors duration-200 ease-paper rounded-full"
							type="button"
							id="copy-and-share-chat-button"
							onclick={async () => {
								const isSafari = /^((?!chrome|android).)*safari/i.test(navigator.userAgent);

								if (isSafari) {
									// Oh, Safari, you're so special, let's give you some extra love and attention
									const getUrlPromise = async () => {
										const url = await shareLocalChat();
										return new Blob([url], { type: 'text/plain' });
									};

									navigator.clipboard
										.write([
											new ClipboardItem({
												'text/plain': getUrlPromise()
											})
										])
										.then(() => {
											return true;
										})
										.catch((error) => {
											console.error('Async: Could not copy text: ', error);
											return false;
										});
								} else {
									copyToClipboard(await shareLocalChat());
								}

								toast.success($i18n.t('Copied shared chat URL to clipboard!'));
							}}
						>
							<Link />
							{$i18n.t('Create link')}
						</button>
					</div>
				{/if}
			</div>
		{/if}
	</div>
</Modal>
