<script lang="ts">
	import { dispatchComponentEvent } from '$lib/utils/componentEvents';
	import {
		addTagById,
		deleteTagById,
		getAllTags,
		getChatList,
		getChatListByTagName,
		getTagsById,
		patchChat
	} from '$lib/apis/chats';
	import {
		tags as _tags,
		chats,
		pinnedChats,
		currentChatPage,
		scrollPaginationEnabled
	} from '$lib/stores';
	import { onMount } from 'svelte';

	const dispatch = (type: string, detail?: unknown) =>
		dispatchComponentEvent(eventProps, type, detail);

	import Tags from '../common/Tags.svelte';
	import { toast } from '$lib/utils/toast';

	interface Props {
		chatId?: string;
	}

	let { chatId = '', ...eventProps }: Props & Record<string, unknown> = $props();
	let tags = $state([]);

	const getTags = async () => {
		return await getTagsById(localStorage.token, chatId).catch(async (error) => {
			return [];
		});
	};

	const addTag = async (tagName) => {
		const res = await addTagById(localStorage.token, chatId, tagName).catch(async (error) => {
			toast.error(`${error}`);
			return null;
		});
		if (!res) {
			return;
		}

		tags = await getTags();
		await patchChat(localStorage.token, chatId, [{ op: 'set_tags', tags }]);

		await _tags.set(await getAllTags(localStorage.token));
		dispatch('add', {
			name: tagName
		});
	};

	const deleteTag = async (tagName) => {
		const res = await deleteTagById(localStorage.token, chatId, tagName);
		tags = await getTags();
		await patchChat(localStorage.token, chatId, [{ op: 'set_tags', tags }]);

		await _tags.set(await getAllTags(localStorage.token));
		dispatch('delete', {
			name: tagName
		});
	};

	onMount(async () => {
		if (chatId) {
			tags = await getTags();
		}
	});
</script>

<Tags
	{tags}
	ondelete={(e) => {
		deleteTag(e.detail);
	}}
	onadd={(e) => {
		addTag(e.detail);
	}}
/>
