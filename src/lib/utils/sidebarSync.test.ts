import { beforeEach, describe, expect, it, vi } from 'vitest';
import { get } from 'svelte/store';

const chatApi = vi.hoisted(() => ({
	getChatList: vi.fn(),
	getPinnedChatList: vi.fn(),
	getAllTags: vi.fn(),
	getChatById: vi.fn()
}));

const folderApi = vi.hoisted(() => ({
	getFolders: vi.fn()
}));

vi.mock('$lib/apis/chats', () => chatApi);
vi.mock('$lib/apis/folders', () => folderApi);
vi.mock('$lib/utils', () => ({
	getTimeRange: (ts: number) => `range-${ts}`
}));

import {
	chats,
	currentChatPage,
	folderChatListInvalidation,
	folders,
	pinnedChats,
	scrollPaginationEnabled,
	tags
} from '$lib/stores';
import { applySidebarEvent, refreshSidebarSnapshot } from './sidebarSync';

describe('sidebarSync', () => {
	beforeEach(() => {
		vi.clearAllMocks();
		Object.defineProperty(globalThis, 'localStorage', {
			value: {
				removeItem: vi.fn(),
				getItem: vi.fn(),
				setItem: vi.fn()
			},
			configurable: true
		});

		chats.set([]);
		pinnedChats.set([]);
		tags.set([]);
		folders.set([]);
		currentChatPage.set(3);
		scrollPaginationEnabled.set(false);
		folderChatListInvalidation.set({ folderIds: [], seq: 0, reason: '' });
	});

	it('upserts top-level created chats without resetting pagination', async () => {
		await applySidebarEvent(
			'chat:created',
			{
				id: 'c1',
				title: 'New work',
				updated_at: 20,
				created_at: 10,
				pinned: false,
				archived: false,
				folder_id: null
			},
			'token'
		);

		expect(get(chats)).toEqual([
			expect.objectContaining({ id: 'c1', title: 'New work', time_range: 'range-20' })
		]);
		expect(get(currentChatPage)).toBe(3);
		expect(get(folderChatListInvalidation).seq).toBe(0);
	});

	it('invalidates an open folder list for folder-contained chat changes', async () => {
		await applySidebarEvent(
			'chat:created',
			{
				id: 'c2',
				title: 'Folder chat',
				updated_at: 30,
				created_at: 30,
				pinned: false,
				archived: false,
				folder_id: 'f1'
			},
			'token'
		);

		expect(get(chats)).toEqual([]);
		expect(get(folderChatListInvalidation)).toEqual({
			folderIds: ['f1'],
			seq: 1,
			reason: 'chat:created'
		});
	});

	it('refreshes the sidebar snapshot for missed-event recovery', async () => {
		folderApi.getFolders.mockResolvedValue([{ id: 'f1', name: 'Folder', updated_at: 4 }]);
		chatApi.getChatList.mockResolvedValue([{ id: 'c1', title: 'Chat', updated_at: 3 }]);
		chatApi.getPinnedChatList.mockResolvedValue([{ id: 'p1', title: 'Pinned', updated_at: 2 }]);
		chatApi.getAllTags.mockResolvedValue([{ id: 't1', name: 'Tag' }]);

		await refreshSidebarSnapshot('token', 'test');

		expect(folderApi.getFolders).toHaveBeenCalledWith('token');
		expect(chatApi.getChatList).toHaveBeenCalledWith('token', 1);
		expect(get(folders)).toEqual([{ id: 'f1', name: 'Folder', updated_at: 4 }]);
		expect(get(chats)).toEqual([
			{ id: 'c1', title: 'Chat', updated_at: 3, time_range: 'range-3' }
		]);
		expect(get(pinnedChats)).toEqual([{ id: 'p1', title: 'Pinned', updated_at: 2 }]);
		expect(get(tags)).toEqual([{ id: 't1', name: 'Tag' }]);
		// applyChatsWindow preserves the already-loaded pagination tail: the
		// page counter survives a snapshot refresh (set to 3 by the earlier
		// pagination test in this file) instead of resetting to 1.
		expect(get(currentChatPage)).toBe(3);
		expect(get(scrollPaginationEnabled)).toBe(true);
		expect(get(folderChatListInvalidation)).toEqual({
			folderIds: ['f1'],
			seq: 1,
			reason: 'snapshot:test'
		});
	});
});
