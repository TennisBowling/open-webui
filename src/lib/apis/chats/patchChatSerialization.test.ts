import { afterEach, describe, expect, it, vi } from 'vitest';

import { patchChat } from './index';

const okResponse = () =>
	new Response(JSON.stringify({ updated_at: 1, ops_applied: ['append_message'] }), {
		status: 200,
		headers: { 'content-type': 'application/json' }
	});

afterEach(() => {
	vi.restoreAllMocks();
	vi.unstubAllGlobals();
});

describe('patchChat serialization', () => {
	it('keeps same-chat parent and child mutations in call order', async () => {
		let releaseParent!: (response: Response) => void;
		const fetchMock = vi
			.fn()
			.mockImplementationOnce(() => new Promise<Response>((resolve) => (releaseParent = resolve)))
			.mockResolvedValueOnce(okResponse());
		vi.stubGlobal('fetch', fetchMock);

		const parent = patchChat('token', 'ordered-chat', [
			{
				op: 'append_message',
				message_id: 'parent',
				parent_id: null,
				role: 'assistant',
				content: ''
			}
		]);
		await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));

		const child = patchChat('token', 'ordered-chat', [
			{
				op: 'append_message',
				message_id: 'child',
				parent_id: 'parent',
				role: 'user',
				content: 'continue'
			}
		]);
		await Promise.resolve();
		expect(fetchMock).toHaveBeenCalledTimes(1);

		releaseParent(okResponse());
		await parent;
		await child;

		expect(fetchMock).toHaveBeenCalledTimes(2);
		expect(JSON.parse(fetchMock.mock.calls[0][1].body as string).ops[0].message_id).toBe('parent');
		expect(JSON.parse(fetchMock.mock.calls[1][1].body as string).ops[0].parent_id).toBe('parent');
	});

	it('does not serialize unrelated chats', async () => {
		const fetchMock = vi.fn().mockImplementation(async () => okResponse());
		vi.stubGlobal('fetch', fetchMock);

		await Promise.all([
			patchChat('token', 'parallel-chat-a', [{ op: 'set_models', models: ['A'] }]),
			patchChat('token', 'parallel-chat-b', [{ op: 'set_models', models: ['B'] }])
		]);

		expect(fetchMock).toHaveBeenCalledTimes(2);
	});

	it('sends edits as source-derived fork operations', async () => {
		const fetchMock = vi.fn().mockImplementation(async () => okResponse());
		vi.stubGlobal('fetch', fetchMock);

		await patchChat('token', 'versioned-chat', [
			{
				op: 'fork_message_version',
				message_id: 'prompt-v2',
				source_message_id: 'prompt-v1',
				content: 'edited',
				models: ['model-a']
			}
		]);

		const op = JSON.parse(fetchMock.mock.calls[0][1].body as string).ops[0];
		expect(op).toMatchObject({
			op: 'fork_message_version',
			message_id: 'prompt-v2',
			source_message_id: 'prompt-v1'
		});
		expect(op).not.toHaveProperty('parent_id');
		expect(op).not.toHaveProperty('role');
	});

	it('allows a later recovery mutation after a failed PATCH', async () => {
		const fetchMock = vi
			.fn()
			.mockResolvedValueOnce(
				new Response(JSON.stringify({ detail: 'conflict' }), {
					status: 409,
					headers: { 'content-type': 'application/json' }
				})
			)
			.mockResolvedValueOnce(okResponse());
		vi.stubGlobal('fetch', fetchMock);
		vi.spyOn(console, 'error').mockImplementation(() => {});

		const failed = patchChat('token', 'recovering-chat', [{ op: 'set_models', models: ['old'] }]);
		const recovered = patchChat('token', 'recovering-chat', [
			{ op: 'set_models', models: ['new'] }
		]);

		await expect(failed).rejects.toEqual({ detail: 'conflict' });
		await expect(recovered).resolves.toMatchObject({ updated_at: 1 });
		expect(fetchMock).toHaveBeenCalledTimes(2);
	});
});
