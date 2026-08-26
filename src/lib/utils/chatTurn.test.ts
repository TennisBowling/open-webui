import { describe, expect, it } from 'vitest';

import {
	getChatGenerationErrorCode,
	hasAssistantResponseBody,
	inactiveAssistantTerminalPatch,
	isNonRetryableChatGenerationError,
	resolveLoadedModelIds,
	resolveRetryModelId,
	snapshotTurnModelIds,
	wasGenerationStartStopped
} from './chatTurn';

describe('chat generation admission errors', () => {
	it('reads the FastAPI detail envelope used by chat completion conflicts', () => {
		const error = {
			detail: {
				message: 'The chat is already processing a different turn.',
				code: 'chat_generation_in_progress'
			}
		};

		expect(getChatGenerationErrorCode(error)).toBe('chat_generation_in_progress');
		expect(isNonRetryableChatGenerationError(error)).toBe(true);
	});

	it('recognizes a terminal error after it has been normalized onto the message', () => {
		expect(
			isNonRetryableChatGenerationError({
				content: 'The generation id is already owned by another request.',
				code: 'generation_id_conflict'
			})
		).toBe(true);
	});

	it('leaves provider and network failures eligible for the existing retry policy', () => {
		expect(isNonRetryableChatGenerationError({ error: { code: 503 } })).toBe(false);
		expect(isNonRetryableChatGenerationError({ message: 'Failed to fetch' })).toBe(false);
	});
});

describe('resolveRetryModelId', () => {
	it('uses the active picker instead of silently reusing the completed response model', () => {
		expect(
			resolveRetryModelId({
				selectedModelIds: ['gemini'],
				modelIdx: 0,
				fallbackModelId: 'deepseek'
			})
		).toBe('gemini');
	});

	it('keeps the same picker slot for a multi-model response', () => {
		expect(
			resolveRetryModelId({
				selectedModelIds: ['gemini', 'claude'],
				modelIdx: 1,
				fallbackModelId: 'deepseek'
			})
		).toBe('claude');
	});

	it('falls back to the response model when the picker is unavailable', () => {
		expect(resolveRetryModelId({ selectedModelIds: [], fallbackModelId: 'deepseek' })).toBe(
			'deepseek'
		);
	});
});

describe('snapshotTurnModelIds', () => {
	it('uses one copied explicit snapshot for the whole turn', () => {
		const explicit = ['gemini-long-context'];
		const resolved = snapshotTurnModelIds({
			explicitModelIds: explicit,
			mentionedModelId: 'mentioned-model',
			selectedModelIds: ['old-model']
		});

		explicit[0] = 'mutated-after-send';
		expect(resolved).toEqual(['gemini-long-context']);
	});

	it('prefers a mention over the ordinary picker when no explicit snapshot exists', () => {
		expect(
			snapshotTurnModelIds({
				mentionedModelId: 'gemini-long-context',
				selectedModelIds: ['old-model']
			})
		).toEqual(['gemini-long-context']);
	});
});

describe('wasGenerationStartStopped', () => {
	it('does not inherit Stop from the turn that an edit-resend replaces', () => {
		const stoppedMessageIds = new Set(['assistant-original']);

		expect(wasGenerationStartStopped(['assistant-resent'], (id) => stoppedMessageIds.has(id))).toBe(
			false
		);
	});

	it('detects an immediate Stop that claimed this send’s fresh placeholder', () => {
		const stoppedMessageIds = new Set(['assistant-resent']);

		expect(wasGenerationStartStopped(['assistant-resent'], (id) => stoppedMessageIds.has(id))).toBe(
			true
		);
	});
});

describe('resolveLoadedModelIds', () => {
	it('applies persisted state on an ordinary chat load', () => {
		expect(
			resolveLoadedModelIds({
				persistedModelIds: ['server-model'],
				currentModelIds: ['previous-chat-model'],
				currentRevision: 4
			})
		).toEqual({
			modelIds: ['server-model'],
			preservedUserSelection: false
		});
	});

	it('does not overwrite a picker change made during local-first revalidation', () => {
		expect(
			resolveLoadedModelIds({
				persistedModelIds: ['old-model'],
				currentModelIds: ['gemini-long-context'],
				revalidationStartedAtRevision: 7,
				currentRevision: 8
			})
		).toEqual({
			modelIds: ['gemini-long-context'],
			preservedUserSelection: true
		});
	});
});

describe('inactive assistant reconciliation', () => {
	it('does not convert an orphaned empty placeholder into a clean completion', () => {
		expect(
			inactiveAssistantTerminalPatch({
				role: 'assistant',
				content: '',
				content_blocks: [],
				done: false
			})
		).toEqual({
			done: true,
			error: {
				content: 'The model request ended before a response could be saved. Please retry.'
			}
		});
	});

	it('cleanly terminalizes a stored answer from a missed done event', () => {
		const message = {
			role: 'assistant',
			content: '',
			content_blocks: [{ type: 'text', content: 'Recovered answer' }],
			done: false
		};
		expect(hasAssistantResponseBody(message)).toBe(true);
		expect(inactiveAssistantTerminalPatch(message)).toEqual({ done: true });
	});

	it('preserves an explicit persisted error', () => {
		expect(
			inactiveAssistantTerminalPatch({
				content: '',
				done: false,
				error: { content: 'Provider failed' }
			})
		).toEqual({ done: true });
	});
});
