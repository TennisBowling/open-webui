<script lang="ts">
	import fileSaver from 'file-saver';
	const { saveAs } = fileSaver;

	import { toast } from '$lib/utils/toast';
	import { getContext } from 'svelte';
	import {
		archiveChatById,
		getAllArchivedChats,
		getArchivedChatList,
		unarchiveAllChats
	} from '$lib/apis/chats';

	import ChatsModal from './ChatsModal.svelte';
	import UnarchiveAllConfirmDialog from '$lib/components/common/ConfirmDialog.svelte';
	import Spinner from '../common/Spinner.svelte';

	const i18n = getContext('i18n');

	interface Props {
		show?: boolean;
		onUpdate?: any;
	}

	let { show = $bindable(false), onUpdate = () => {} }: Props = $props();

	let loading = $state(false);
	let chatList = $state(null);
	let page = 1;

	let query = $state('');
	let orderBy = $state('updated_at');
	let direction = $state('desc');

	let allChatsLoaded = $state(false);
	let chatListLoading = $state(false);
	let searchDebounceTimeout;

	let showUnarchiveAllConfirmDialog = $state(false);

	let filter = $state({});

	const searchHandler = async () => {
		if (!show) {
			return;
		}

		if (searchDebounceTimeout) {
			clearTimeout(searchDebounceTimeout);
		}

		page = 1;
		chatList = null;

		if (query === '') {
			chatList = await getArchivedChatList(localStorage.token, page, filter);
		} else {
			searchDebounceTimeout = setTimeout(async () => {
				chatList = await getArchivedChatList(localStorage.token, page, filter);
			}, 500);
		}

		if ((chatList ?? []).length === 0) {
			allChatsLoaded = true;
		} else {
			allChatsLoaded = false;
		}
	};

	const loadMoreChats = async () => {
		chatListLoading = true;
		page += 1;

		let newChatList = [];

		if (query) {
			newChatList = await getArchivedChatList(localStorage.token, page, filter);
		} else {
			newChatList = await getArchivedChatList(localStorage.token, page, filter);
		}

		// once the bottom of the list has been reached (no results) there is no need to continue querying
		allChatsLoaded = newChatList.length === 0;

		if (newChatList.length > 0) {
			chatList = [...chatList, ...newChatList];
		}

		chatListLoading = false;
	};

	const exportChatsHandler = async () => {
		const chats = await getAllArchivedChats(localStorage.token);
		let blob = new Blob([JSON.stringify(chats)], {
			type: 'application/json'
		});
		saveAs(blob, `${$i18n.t('archived-chat-export')}-${Date.now()}.json`);
	};

	const unarchiveHandler = async (chatId) => {
		const res = await archiveChatById(localStorage.token, chatId).catch((error) => {
			toast.error(`${error}`);
		});

		onUpdate();
		init();
	};

	const unarchiveAllHandler = async () => {
		loading = true;
		try {
			await unarchiveAllChats(localStorage.token);
			toast.success($i18n.t('All chats have been unarchived.'));
			onUpdate();
			await init();
		} catch (error) {
			toast.error(`${error}`);
		} finally {
			loading = false;
		}
	};

	const init = async () => {
		chatList = await getArchivedChatList(localStorage.token);
	};

	$effect(() => {
		filter = {
			...(query ? { query } : {}),
			...(orderBy ? { order_by: orderBy } : {}),
			...(direction ? { direction } : {})
		};
	});
	$effect(() => {
		if (filter !== null) {
			searchHandler();
		}
	});
	$effect(() => {
		if (show) {
			init();
		}
	});
</script>

<UnarchiveAllConfirmDialog
	bind:show={showUnarchiveAllConfirmDialog}
	message={$i18n.t('Are you sure you want to unarchive all archived chats?')}
	confirmLabel={$i18n.t('Unarchive All')}
	onconfirm={() => {
		unarchiveAllHandler();
	}}
/>

<ChatsModal
	bind:show
	bind:query
	bind:orderBy
	bind:direction
	title={$i18n.t('Archived Chats')}
	emptyPlaceholder={$i18n.t('You have no archived conversations.')}
	{chatList}
	{allChatsLoaded}
	{chatListLoading}
	onUpdate={() => {
		init();
	}}
	loadHandler={loadMoreChats}
	{unarchiveHandler}
>
	{#snippet footer()}
		<div>
			<div class="flex flex-wrap text-sm font-medium gap-1.5 mt-2 m-1 justify-end w-full">
				<button
					class=" px-3.5 py-1.5 font-medium hover:bg-black/5 dark:hover:bg-white/5 border-hairline border-gray-200 dark:border-gray-800 rounded-full"
					disabled={loading}
					onclick={() => {
						showUnarchiveAllConfirmDialog = true;
					}}
				>
					{#if loading}
						<Spinner className="size-4" />
					{:else}
						{$i18n.t('Unarchive All Archived Chats')}
					{/if}
				</button>

				<button
					class="px-3.5 py-1.5 font-medium hover:bg-black/5 dark:hover:bg-white/5 border-hairline border-gray-200 dark:border-gray-800 rounded-full"
					disabled={loading}
					onclick={() => {
						exportChatsHandler();
					}}
				>
					{$i18n.t('Export All Archived Chats')}
				</button>
			</div>
		</div>
	{/snippet}
</ChatsModal>
