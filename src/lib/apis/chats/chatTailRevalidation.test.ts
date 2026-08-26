import { afterEach, describe, expect, it, vi } from 'vitest';

import { getChatByIdTail } from './index';

const cached = {
	id: 'chat-1',
	updated_at: 100,
	chat: {
		history: {
			currentId: 'message-1',
			messages: {
				'message-1': {
					id: 'message-1',
					parentId: null,
					childrenIds: [],
					role: 'assistant',
					content: 'old',
					_rev: '1'
				}
			}
		}
	}
};

afterEach(() => {
	vi.unstubAllGlobals();
});

describe('conditional stitched-tail semantics', () => {
	it('marks only an actual HTTP 304 as not modified', async () => {
		vi.stubGlobal(
			'fetch',
			vi.fn().mockResolvedValue(
				new Response(null, {
					status: 304,
					headers: { etag: 'W/\"same\"' }
				})
			)
		);

		const result = await getChatByIdTail('token', 'chat-1', 25, {
			etagEntry: { data: structuredClone(cached), etag: 'W/\"same\"' }
		});

		expect(result.__notModified).toBe(true);
		expect(result.chat.history.messages['message-1'].content).toBe('old');
	});

	it('keeps a same-second 200 distinguishable so its changed row is applied', async () => {
		vi.stubGlobal(
			'fetch',
			vi.fn().mockResolvedValue(
				new Response(
					JSON.stringify({
						id: 'chat-1',
						title: 'chat',
						updated_at: 100,
						history: {
							currentId: 'message-1',
							sibling_stubs: []
						},
						branch: [
							{
								id: 'message-1',
								parentId: null,
								childrenIds: [],
								role: 'assistant',
								content: 'new',
								_rev: '2'
							}
						],
						tags: [],
						active: { task_ids: [], streams: [] }
					}),
					{ status: 200, headers: { 'content-type': 'application/json', etag: 'W/\"new\"' } }
				)
			)
		);

		const result = await getChatByIdTail('token', 'chat-1', 25, {
			etagEntry: { data: structuredClone(cached), etag: 'W/\"same\"' }
		});

		expect(result.__notModified).toBeUndefined();
		expect(result.updated_at).toBe(100);
		expect(result.chat.history.currentId).toBe('message-1');
		expect(result.chat.history.messages['message-1'].content).toBe('new');
	});
});
