export type AsyncTaskResponseRead =
	| { kind: 'payload'; payload: any }
	| { kind: 'stopped'; payload?: any }
	| { kind: 'parse-error'; error: unknown };

/**
 * Read the async chat-task envelope and re-check cancellation after the
 * asynchronous body read. Stop can abort the response between the caller's
 * pre-read cancellation check and `response.json()` completing; that is a
 * successful user cancellation, not a malformed/missing task envelope.
 */
export const readAsyncTaskResponse = async (
	response: Pick<Response, 'json'>,
	isStopped: () => boolean
): Promise<AsyncTaskResponseRead> => {
	try {
		const payload = await response.json();
		return isStopped() ? { kind: 'stopped', payload } : { kind: 'payload', payload };
	} catch (error) {
		return isStopped() ? { kind: 'stopped' } : { kind: 'parse-error', error };
	}
};
