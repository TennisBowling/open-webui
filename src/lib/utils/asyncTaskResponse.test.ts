import { describe, expect, it } from 'vitest';

import { readAsyncTaskResponse } from './asyncTaskResponse';

describe('readAsyncTaskResponse', () => {
	it('treats a body-read abort caused by an immediate user stop as cancellation', async () => {
		let stopped = false;
		const response = {
			json: async () => {
				stopped = true;
				throw new DOMException('The operation was aborted', 'AbortError');
			}
		};

		await expect(readAsyncTaskResponse(response, () => stopped)).resolves.toEqual({
			kind: 'stopped'
		});
	});

	it('still reports a body parse failure when the user did not stop', async () => {
		const error = new SyntaxError('Invalid JSON');
		const response = {
			json: async () => {
				throw error;
			}
		};

		await expect(readAsyncTaskResponse(response, () => false)).resolves.toEqual({
			kind: 'parse-error',
			error
		});
	});

	it('returns a valid task envelope when the request remains active', async () => {
		const payload = { status: true, task_id: 'task-123' };
		const response = { json: async () => payload };

		await expect(readAsyncTaskResponse(response, () => false)).resolves.toEqual({
			kind: 'payload',
			payload
		});
	});

	it('retains a late task id so cancellation can stop it defensively', async () => {
		const payload = { status: true, task_id: 'task-late' };
		const response = { json: async () => payload };

		await expect(readAsyncTaskResponse(response, () => true)).resolves.toEqual({
			kind: 'stopped',
			payload
		});
	});
});
