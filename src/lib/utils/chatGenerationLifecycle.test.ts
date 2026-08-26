import { describe, expect, it, vi } from 'vitest';

import { ChatGenerationLifecycleRegistry } from './chatGenerationLifecycle';

const begin = (
	registry: ChatGenerationLifecycleRegistry,
	messageId: string,
	generationId: string,
	chatId = 'chat-a',
	navigationGeneration = 1
) =>
	registry.begin({
		chatId,
		messageId,
		generationId,
		turnId: 'user-1',
		navigationGeneration
	});

describe('ChatGenerationLifecycleRegistry', () => {
	it('latches Stop and aborts every sibling controller synchronously', () => {
		const registry = new ChatGenerationLifecycleRegistry();
		const first = new AbortController();
		const second = new AbortController();
		const firstAbort = vi.spyOn(first, 'abort');
		const secondAbort = vi.spyOn(second, 'abort');

		begin(registry, 'assistant-1', 'generation-1');
		begin(registry, 'assistant-2', 'generation-2');
		registry.attachController('assistant-1', 'generation-1', first);
		registry.attachController('assistant-2', 'generation-2', second);

		const stopped = registry.stop('chat-a', ['assistant-1', 'assistant-2']);

		expect(stopped).toHaveLength(2);
		expect(registry.generationsForStop('chat-a')).toEqual([
			{
				generation_id: 'generation-1',
				message_id: 'assistant-1',
				turn_id: 'user-1'
			},
			{
				generation_id: 'generation-2',
				message_id: 'assistant-2',
				turn_id: 'user-1'
			}
		]);
		expect(firstAbort).toHaveBeenCalledOnce();
		expect(secondAbort).toHaveBeenCalledOnce();
		expect(registry.activeForChat('chat-a')).toEqual([]);
	});

	it('never lets a terminal sibling clear another active generation', () => {
		const registry = new ChatGenerationLifecycleRegistry();
		begin(registry, 'assistant-1', 'generation-1');
		begin(registry, 'assistant-2', 'generation-2');

		registry.terminal('assistant-1', 'generation-1');

		expect(registry.activeForChat('chat-a').map((record) => record.messageId)).toEqual([
			'assistant-2'
		]);
	});

	it('releases the request controller once the backend task owns the run', () => {
		const registry = new ChatGenerationLifecycleRegistry();
		const controller = new AbortController();
		begin(registry, 'assistant-1', 'generation-1');
		registry.attachController('assistant-1', 'generation-1', controller);

		expect(registry.markAccepted('assistant-1', 'generation-1', 'task-1')).toBe('active');
		expect(registry.get('assistant-1')?.controller).toBeNull();
		expect(registry.taskIdsForChat('chat-a')).toEqual(['task-1']);
	});

	it('rejects stale controllers and task envelopes from superseded runs', () => {
		const registry = new ChatGenerationLifecycleRegistry();
		begin(registry, 'assistant-1', 'generation-old');
		begin(registry, 'assistant-1', 'generation-new');
		const staleController = new AbortController();

		expect(registry.attachController('assistant-1', 'generation-old', staleController)).toBe(false);
		expect(staleController.signal.aborted).toBe(true);
		expect(registry.markAccepted('assistant-1', 'generation-old', 'task-old')).toBe('stale');
		expect(registry.taskIdsForChat('chat-a')).toEqual([]);
	});

	it('ties visibility to both chat and navigation generation', () => {
		const registry = new ChatGenerationLifecycleRegistry();
		begin(registry, 'assistant-1', 'generation-1', 'chat-a', 4);

		expect(registry.isVisible('assistant-1', 'generation-1', 'chat-a', 4)).toBe(true);
		expect(registry.isVisible('assistant-1', 'generation-1', 'chat-b', 4)).toBe(false);
		expect(registry.isVisible('assistant-1', 'generation-1', 'chat-a', 5)).toBe(false);
	});

	it('keeps the same cancellation latch across automatic retries', () => {
		const registry = new ChatGenerationLifecycleRegistry();
		begin(registry, 'assistant-1', 'generation-1');
		registry.stop('chat-a', ['assistant-1']);

		const retry = begin(registry, 'assistant-1', 'generation-1');

		expect(retry.fresh).toBe(false);
		expect(retry.record.phase).toBe('stopped');
	});

	it('scopes a stopped turn away from a later edit-resend branch', () => {
		const registry = new ChatGenerationLifecycleRegistry();
		begin(registry, 'assistant-original', 'generation-original');
		registry.stop('chat-a', ['assistant-original']);

		// Editing the user prompt creates a new user/assistant branch with fresh
		// identities. The old turn's sticky Stop must remain true for the old
		// assistant without becoming cancellation authority over this new one.
		const resent = begin(registry, 'assistant-resent', 'generation-resent');

		expect(registry.isStopped('assistant-original', 'generation-original')).toBe(true);
		expect(resent.fresh).toBe(true);
		expect(resent.record.phase).toBe('preparing');
		expect(registry.isStopped('assistant-resent', 'generation-resent')).toBe(false);
		expect(registry.activeForChat('chat-a').map((record) => record.messageId)).toEqual([
			'assistant-resent'
		]);
	});

	it('latches a named message even after a failed attempt settled it terminal', () => {
		const registry = new ChatGenerationLifecycleRegistry();
		begin(registry, 'assistant-1', 'generation-1');
		// A provider error settles the record; the client retry loop then keeps
		// driving the same (message, generation) through its countdown.
		registry.terminal('assistant-1', 'generation-1');

		expect(registry.stop('chat-a', ['assistant-1'])).toHaveLength(1);
		expect(registry.isStopped('assistant-1', 'generation-1')).toBe(true);
		expect(registry.generationsForStop('chat-a', ['assistant-1'])).toEqual([
			{
				generation_id: 'generation-1',
				message_id: 'assistant-1',
				turn_id: 'user-1'
			}
		]);
	});

	it('leaves finished work alone when Stop names no message', () => {
		const registry = new ChatGenerationLifecycleRegistry();
		begin(registry, 'assistant-1', 'generation-1');
		registry.terminal('assistant-1', 'generation-1');

		expect(registry.stop('chat-a')).toEqual([]);
		expect(registry.isStopped('assistant-1', 'generation-1')).toBe(false);
	});

	it('filters only the exact stopped generation from server work state', () => {
		const registry = new ChatGenerationLifecycleRegistry();
		begin(registry, 'assistant-1', 'generation-old');
		registry.stop('chat-a', ['assistant-1']);

		const live = registry.reconcileServerOperations(
			[
				{
					generation_id: 'generation-old',
					chat_id: 'chat-a',
					message_id: 'assistant-1',
					turn_id: 'turn-old',
					task_id: 'task-old'
				},
				{
					generation_id: 'generation-new',
					chat_id: 'chat-a',
					message_id: 'assistant-1',
					turn_id: 'turn-new',
					task_id: 'task-new'
				}
			],
			2
		);

		expect(live.map((operation) => operation.generation_id)).toEqual(['generation-new']);
		expect(registry.generationsForStop('chat-a')).toEqual([
			{
				generation_id: 'generation-new',
				message_id: 'assistant-1',
				turn_id: 'turn-new'
			}
		]);
	});
});

describe('ChatGenerationLifecycleRegistry change notification', () => {
	// The composer's live state is DERIVED from activeForChat(), so a mutation
	// that doesn't notify is a mutation the UI never sees.
	it('notifies on every state transition', () => {
		const registry = new ChatGenerationLifecycleRegistry();
		const listener = vi.fn();
		registry.subscribe(listener);

		begin(registry, 'assistant-1', 'generation-1');
		expect(listener).toHaveBeenCalledTimes(1);

		registry.attachController('assistant-1', 'generation-1', new AbortController());
		expect(listener).toHaveBeenCalledTimes(2);

		registry.markAccepted('assistant-1', 'generation-1', 'task-1');
		expect(listener).toHaveBeenCalledTimes(3);

		registry.terminal('assistant-1', 'generation-1');
		expect(listener).toHaveBeenCalledTimes(4);
	});

	it('stops notifying after unsubscribe', () => {
		const registry = new ChatGenerationLifecycleRegistry();
		const listener = vi.fn();
		registry.subscribe(listener)();
		begin(registry, 'assistant-1', 'generation-1');
		expect(listener).not.toHaveBeenCalled();
	});
});

describe('ChatGenerationLifecycleRegistry authoritative reconcile', () => {
	// The failure this settle exists for: a tab misses a terminal chat:done on a
	// weak link, so its record stays `accepted` forever. Everything that asks
	// "is this chat generating" then answers yes for the rest of the session —
	// the composer sits on its Stop button while the answer renders as finished.
	it('settles accepted records the server no longer lists', () => {
		const registry = new ChatGenerationLifecycleRegistry();
		begin(registry, 'assistant-1', 'generation-1');
		registry.markAccepted('assistant-1', 'generation-1', 'task-1');
		expect(registry.activeForChat('chat-a')).toHaveLength(1);

		registry.reconcileServerOperations([], 3, 'chat-a');
		expect(registry.activeForChat('chat-a')).toHaveLength(0);
	});

	it('settles observed (remote) records the server no longer lists', () => {
		const registry = new ChatGenerationLifecycleRegistry();
		registry.observe('chat-a', 'assistant-remote', 3);
		expect(registry.activeForChat('chat-a')).toHaveLength(1);

		registry.reconcileServerOperations([], 3, 'chat-a');
		expect(registry.activeForChat('chat-a')).toHaveLength(0);
	});

	it('leaves a still-local pre-POST record alone', () => {
		// The server has not been told about this generation yet, so "absent from
		// the work state" means "not yet", not "finished". Settling it here would
		// abandon a send that is still being assembled.
		const registry = new ChatGenerationLifecycleRegistry();
		begin(registry, 'assistant-1', 'generation-1'); // preparing
		registry.reconcileServerOperations([], 3, 'chat-a');
		expect(registry.activeForChat('chat-a')).toHaveLength(1);

		registry.attachController('assistant-1', 'generation-1', new AbortController()); // requesting
		registry.reconcileServerOperations([], 3, 'chat-a');
		expect(registry.activeForChat('chat-a')).toHaveLength(1);
	});

	it('leaves other chats alone', () => {
		const registry = new ChatGenerationLifecycleRegistry();
		begin(registry, 'assistant-2', 'generation-2', 'chat-b');
		registry.markAccepted('assistant-2', 'generation-2');
		registry.reconcileServerOperations([], 3, 'chat-a');
		expect(registry.activeForChat('chat-b')).toHaveLength(1);
	});

	it('settles nothing without a chat id (non-authoritative call)', () => {
		const registry = new ChatGenerationLifecycleRegistry();
		begin(registry, 'assistant-1', 'generation-1');
		registry.markAccepted('assistant-1', 'generation-1');
		registry.reconcileServerOperations([], 3);
		expect(registry.activeForChat('chat-a')).toHaveLength(1);
	});
});

describe('ChatGenerationLifecycleRegistry.observe', () => {
	it('marks remote work active without inventing a generation identity', () => {
		const registry = new ChatGenerationLifecycleRegistry();
		registry.observe('chat-a', 'assistant-remote', 3);
		expect(registry.activeForChat('chat-a')).toHaveLength(1);
		// Nothing to address a Stop request to, so it must not produce a target.
		expect(registry.generationsForStop('chat-a')).toEqual([]);
	});

	it('never downgrades a live local generation', () => {
		const registry = new ChatGenerationLifecycleRegistry();
		begin(registry, 'assistant-1', 'generation-1');
		registry.observe('chat-a', 'assistant-1', 3);
		expect(registry.get('assistant-1')?.generationId).toBe('generation-1');
	});

	it('replaces a settled record', () => {
		const registry = new ChatGenerationLifecycleRegistry();
		begin(registry, 'assistant-1', 'generation-1');
		registry.terminal('assistant-1', 'generation-1');
		registry.observe('chat-a', 'assistant-1', 3);
		expect(registry.activeForChat('chat-a')).toHaveLength(1);
	});
});

describe('ChatGenerationLifecycleRegistry.retry', () => {
	// Between attempts the failed attempt has already settled the record, but the
	// retry loop still owns the turn — without re-arming, the composer would flick
	// back to idle mid-countdown.
	it('re-arms a settled record', () => {
		const registry = new ChatGenerationLifecycleRegistry();
		begin(registry, 'assistant-1', 'generation-1');
		registry.terminal('assistant-1', 'generation-1');
		expect(registry.activeForChat('chat-a')).toHaveLength(0);

		expect(registry.retry('assistant-1', 'generation-1')).toBe(true);
		expect(registry.activeForChat('chat-a')).toHaveLength(1);
	});

	it('refuses a stopped record so Stop always wins over a pending retry', () => {
		const registry = new ChatGenerationLifecycleRegistry();
		begin(registry, 'assistant-1', 'generation-1');
		registry.stop('chat-a', ['assistant-1']);

		expect(registry.retry('assistant-1', 'generation-1')).toBe(false);
		expect(registry.activeForChat('chat-a')).toHaveLength(0);
	});

	it('refuses a superseded generation id', () => {
		const registry = new ChatGenerationLifecycleRegistry();
		begin(registry, 'assistant-1', 'generation-1');
		expect(registry.retry('assistant-1', 'generation-other')).toBe(false);
	});
});

describe('ChatGenerationLifecycleRegistry.latchStopped', () => {
	// Stop has to leave a latch even on a turn this tab never started — a chat
	// opened mid-stream, or a peer device's send. `stop()` only visits records
	// that already exist, so that case left nothing behind, which is why a second
	// parallel "stopped message ids" set existed at all.
	it('creates a stopped record for a turn this tab never owned', () => {
		const registry = new ChatGenerationLifecycleRegistry();
		registry.latchStopped({
			chatId: 'chat-a',
			messageId: 'observed-1',
			generationId: 'gen-remote',
			turnId: 'turn-remote',
			navigationGeneration: 1
		});
		expect(registry.isStopped('observed-1')).toBe(true);
		expect(registry.activeForChat('chat-a')).toEqual([]);
		expect(registry.generationsForStop('chat-a', ['observed-1'])).toEqual([
			{ generation_id: 'gen-remote', message_id: 'observed-1', turn_id: 'turn-remote' }
		]);
	});

	it('latches an existing record and aborts its controller', () => {
		const registry = new ChatGenerationLifecycleRegistry();
		begin(registry, 'assistant-1', 'generation-1');
		const controller = new AbortController();
		registry.attachController('assistant-1', 'generation-1', controller);

		registry.latchStopped({
			chatId: 'chat-a',
			messageId: 'assistant-1',
			navigationGeneration: 1
		});

		expect(registry.isStopped('assistant-1', 'generation-1')).toBe(true);
		expect(controller.signal.aborted).toBe(true);
		expect(registry.activeForChat('chat-a')).toEqual([]);
	});

	it('NEVER overwrites the identity of a record it already owns', () => {
		// The incoming ids are only a fallback recovered from the persisted row.
		// Clobbering a live generationId would make isCurrent() — the "has a newer
		// generation taken over?" check the retry loops exit on — report false for
		// the run that is actually current.
		const registry = new ChatGenerationLifecycleRegistry();
		begin(registry, 'assistant-1', 'generation-1');
		registry.latchStopped({
			chatId: 'chat-a',
			messageId: 'assistant-1',
			generationId: 'stale-from-row',
			turnId: 'stale-turn',
			navigationGeneration: 1
		});
		expect(registry.get('assistant-1')?.generationId).toBe('generation-1');
		expect(registry.isCurrent('assistant-1', 'generation-1')).toBe(true);
	});

	it('fills in identity only when it has none', () => {
		const registry = new ChatGenerationLifecycleRegistry();
		registry.observe('chat-a', 'assistant-1', 1); // no identity yet
		registry.latchStopped({
			chatId: 'chat-a',
			messageId: 'assistant-1',
			generationId: 'gen-from-row',
			turnId: 'turn-from-row',
			navigationGeneration: 1
		});
		expect(registry.get('assistant-1')?.generationId).toBe('gen-from-row');
		expect(registry.isStopped('assistant-1')).toBe(true);
	});

	it('keeps the message out of generating/taskIds', () => {
		const registry = new ChatGenerationLifecycleRegistry();
		begin(registry, 'assistant-1', 'generation-1');
		registry.markAccepted('assistant-1', 'generation-1', 'task-1');
		expect(registry.taskIdsForChat('chat-a')).toEqual(['task-1']);

		registry.latchStopped({ chatId: 'chat-a', messageId: 'assistant-1', navigationGeneration: 1 });
		expect(registry.taskIdsForChat('chat-a')).toEqual([]);
		expect(registry.activeForChat('chat-a')).toEqual([]);
	});

	it('survives a later retry re-arm attempt (Stop wins)', () => {
		const registry = new ChatGenerationLifecycleRegistry();
		begin(registry, 'assistant-1', 'generation-1');
		registry.latchStopped({ chatId: 'chat-a', messageId: 'assistant-1', navigationGeneration: 1 });
		expect(registry.retry('assistant-1', 'generation-1')).toBe(false);
		expect(registry.isStopped('assistant-1')).toBe(true);
	});

	it('is cleared by a genuinely NEW generation on the same message', () => {
		const registry = new ChatGenerationLifecycleRegistry();
		begin(registry, 'assistant-1', 'generation-1');
		registry.latchStopped({ chatId: 'chat-a', messageId: 'assistant-1', navigationGeneration: 1 });
		expect(registry.isStopped('assistant-1')).toBe(true);

		begin(registry, 'assistant-1', 'generation-2');
		expect(registry.isStopped('assistant-1')).toBe(false);
		expect(registry.activeForChat('chat-a')).toHaveLength(1);
	});
});

describe('observe() vs a stopped record', () => {
	// A delta arriving after Stop — the tail of the run we just cancelled, or a
	// peer restarting the turn — must not silently un-stop it. Before `stopped`
	// lived in the registry this was guaranteed by a separate sticky set.
	it('refuses to resurrect a stopped record', () => {
		const registry = new ChatGenerationLifecycleRegistry();
		begin(registry, 'assistant-1', 'generation-1');
		registry.latchStopped({ chatId: 'chat-a', messageId: 'assistant-1', navigationGeneration: 1 });

		registry.observe('chat-a', 'assistant-1', 1);

		expect(registry.isStopped('assistant-1')).toBe(true);
		expect(registry.activeForChat('chat-a')).toEqual([]);
	});

	it('still replaces a merely FINISHED record', () => {
		const registry = new ChatGenerationLifecycleRegistry();
		begin(registry, 'assistant-1', 'generation-1');
		registry.terminal('assistant-1', 'generation-1');
		registry.observe('chat-a', 'assistant-1', 1);
		expect(registry.activeForChat('chat-a')).toHaveLength(1);
	});
});
