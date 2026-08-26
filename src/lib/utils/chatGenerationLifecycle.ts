export type ChatGenerationPhase =
	| 'preparing'
	| 'requesting'
	| 'accepted'
	| 'observing'
	| 'stopped'
	| 'terminal';

export type ChatGenerationRecord = {
	chatId: string;
	messageId: string;
	generationId: string;
	turnId: string;
	navigationGeneration: number;
	phase: ChatGenerationPhase;
	controller: AbortController | null;
	taskIds: Set<string>;
};

export type BeginChatGeneration = Pick<
	ChatGenerationRecord,
	'chatId' | 'messageId' | 'generationId' | 'turnId' | 'navigationGeneration'
>;

export type ServerGenerationOperation = {
	generation_id: string;
	chat_id: string;
	message_id: string;
	turn_id: string;
	task_id: string;
};

/**
 * Phases where the backend does not yet know about this generation: the POST is
 * being assembled or is in flight. The server's work state cannot contradict
 * them, so a reconcile must leave them alone.
 */
const LOCAL_ONLY_PHASES: ReadonlySet<ChatGenerationPhase> = new Set(['preparing', 'requesting']);
const SETTLED_PHASES: ReadonlySet<ChatGenerationPhase> = new Set(['stopped', 'terminal']);

/**
 * Message-scoped ownership for browser chat requests.
 *
 * The UI may have several sibling model requests in flight. Keeping their
 * controller, backend task ids, navigation identity, and Stop latch together
 * prevents a terminal event for one sibling—or a stale continuation from a
 * prior chat—from changing another request's lifecycle.
 *
 * This registry is also the ONLY source of truth for "is this chat generating".
 * Chat.svelte derives its composer state from `activeForChat()` rather than
 * maintaining a parallel boolean, so subscribers are notified on every
 * mutation.
 */
export class ChatGenerationLifecycleRegistry {
	#records = new Map<string, ChatGenerationRecord>();
	#listeners = new Set<() => void>();

	/**
	 * Observe every mutation. Returns an unsubscribe function. Used to drive the
	 * derived `generating` state — without it, a UI reading `activeForChat()`
	 * would never re-evaluate when a record settles.
	 */
	subscribe(listener: () => void): () => void {
		this.#listeners.add(listener);
		return () => this.#listeners.delete(listener);
	}

	#notify(): void {
		for (const listener of this.#listeners) listener();
	}

	begin(input: BeginChatGeneration): { record: ChatGenerationRecord; fresh: boolean } {
		const existing = this.#records.get(input.messageId);
		if (existing?.generationId === input.generationId) {
			if (existing.phase !== 'stopped') {
				existing.phase = 'preparing';
			}
			this.#notify();
			return { record: existing, fresh: false };
		}

		if (existing?.controller && !existing.controller.signal.aborted) {
			existing.controller.abort();
		}
		const record: ChatGenerationRecord = {
			...input,
			phase: 'preparing',
			controller: null,
			taskIds: new Set()
		};
		this.#records.set(input.messageId, record);
		this.#trim();
		this.#notify();
		return { record, fresh: true };
	}

	/**
	 * Record generation work this tab did NOT start — a peer device's turn, or a
	 * stream we attached to from a delta/snapshot. It has no local controller and
	 * no known generation identity yet; the next server work-state reconcile
	 * fills that in, or settles it if the turn has already finished.
	 *
	 * Modelling observed work as a record (instead of flipping a separate
	 * "generating" flag) is what keeps one predicate — `activeForChat()` — true
	 * for every kind of in-flight turn.
	 */
	observe(chatId: string, messageId: string, navigationGeneration: number): void {
		if (!chatId || !messageId) return;
		const existing = this.#records.get(messageId);
		// A STOPPED record is the user's standing intent for this message and
		// outranks anything we merely observe: a delta arriving after Stop (a peer
		// restarting the turn, or the tail of the run we just cancelled) must not
		// silently un-stop it. Only a finished record is free to be replaced.
		if (existing && existing.phase !== 'terminal') return;
		this.#records.set(messageId, {
			chatId,
			messageId,
			generationId: '',
			turnId: '',
			navigationGeneration,
			phase: 'observing',
			controller: null,
			taskIds: new Set()
		});
		this.#trim();
		this.#notify();
	}

	get(messageId: string | null | undefined): ChatGenerationRecord | null {
		if (!messageId) return null;
		return this.#records.get(messageId) ?? null;
	}

	isCurrent(messageId: string, generationId: string): boolean {
		return this.#records.get(messageId)?.generationId === generationId;
	}

	isVisible(
		messageId: string,
		generationId: string,
		chatId: string | null | undefined,
		navigationGeneration: number
	): boolean {
		const record = this.#records.get(messageId);
		return (
			record?.generationId === generationId &&
			record.chatId === chatId &&
			record.navigationGeneration === navigationGeneration
		);
	}

	isStopped(messageId: string, generationId?: string): boolean {
		const record = this.#records.get(messageId);
		return (
			record?.phase === 'stopped' &&
			(generationId === undefined || record.generationId === generationId)
		);
	}

	attachController(messageId: string, generationId: string, controller: AbortController): boolean {
		const record = this.#records.get(messageId);
		if (!record || record.generationId !== generationId) {
			controller.abort();
			return false;
		}
		if (record.phase === 'stopped') {
			controller.abort();
			return false;
		}
		if (
			record.controller &&
			record.controller !== controller &&
			!record.controller.signal.aborted
		) {
			record.controller.abort();
		}
		record.controller = controller;
		record.phase = 'requesting';
		this.#notify();
		return true;
	}

	markAccepted(
		messageId: string,
		generationId: string,
		taskId?: string
	): 'active' | 'stopped' | 'stale' {
		const record = this.#records.get(messageId);
		if (!record || record.generationId !== generationId) return 'stale';
		if (record.phase === 'stopped') return 'stopped';
		if (record.phase === 'terminal') return 'stale';
		if (taskId) record.taskIds.add(taskId);
		// The POST/body-read phase is over. Backend task ids + socket/stream state
		// own the remaining lifetime; retaining this controller would make a
		// later chat-open unable to distinguish a live local preflight/direct
		// stream from a completed task whose hidden-tab terminal event was missed.
		record.controller = null;
		record.phase = 'accepted';
		this.#notify();
		return 'active';
	}

	/**
	 * Re-arm a record for another client-side attempt. A failed attempt settles
	 * the record `terminal`, but a retry loop still owns the turn during its
	 * countdown — without this the derived "generating" state would drop to false
	 * between attempts and the composer would flicker back to idle mid-retry.
	 * A stopped record stays stopped: Stop always wins over a pending retry.
	 */
	retry(messageId: string, generationId: string): boolean {
		const record = this.#records.get(messageId);
		if (!record || record.generationId !== generationId) return false;
		if (record.phase === 'stopped') return false;
		record.phase = 'preparing';
		this.#notify();
		return true;
	}

	/**
	 * Latch ONE message as stopped, creating the record when this tab never had
	 * one. Returns the record.
	 *
	 * The create half is what makes Stop work on a turn this tab only OBSERVES —
	 * a chat opened mid-stream, or a peer device's send. Such a turn has no local
	 * record, so `stop()` (which only visits records that already exist) left no
	 * latch behind, and every guard that asks "was this stopped?" had nothing to
	 * read. That gap is why a second, parallel per-tab set of stopped message ids
	 * existed at all.
	 *
	 * An EXISTING record keeps its own generation identity: the incoming values
	 * are only a fallback recovered from the persisted row, and overwriting a
	 * live `generationId` with them would make `isCurrent()` — the "has a newer
	 * generation taken over this message?" check that retry loops exit on — start
	 * reporting false for the run that is actually current.
	 */
	latchStopped(input: {
		chatId: string;
		messageId: string;
		generationId?: string;
		turnId?: string;
		navigationGeneration: number;
	}): ChatGenerationRecord | null {
		if (!input.messageId) return null;
		let record = this.#records.get(input.messageId);
		if (!record) {
			record = {
				chatId: input.chatId,
				messageId: input.messageId,
				generationId: input.generationId ?? '',
				turnId: input.turnId ?? '',
				navigationGeneration: input.navigationGeneration,
				phase: 'stopped',
				controller: null,
				taskIds: new Set()
			};
			this.#records.set(input.messageId, record);
			this.#trim();
			this.#notify();
			return record;
		}
		// Fill in identity only where we genuinely have none.
		if (!record.generationId && input.generationId) record.generationId = input.generationId;
		if (!record.turnId && input.turnId) record.turnId = input.turnId;
		record.phase = 'stopped';
		if (record.controller && !record.controller.signal.aborted) {
			record.controller.abort();
		}
		this.#notify();
		return record;
	}

	stop(chatId: string, messageIds?: Iterable<string>): ChatGenerationRecord[] {
		const wanted = messageIds ? new Set(messageIds) : null;
		const stopped: ChatGenerationRecord[] = [];
		for (const record of this.#records.values()) {
			if (record.chatId !== chatId || (wanted && !wanted.has(record.messageId))) continue;
			// A NAMED message is authoritative: the caller resolved it from live
			// history (not done) or from server work state, so its record must be
			// latched even when a previous attempt settled it `terminal`. That is
			// exactly the retry-countdown state — the failed attempt's terminal
			// event settles the record, then a client-side retry loop keeps driving
			// the same (message, generation). Skipping it here let Stop pass
			// straight through: the countdown kept running, re-armed the message as
			// not-done, and fired another attempt. Only the unscoped "stop whatever
			// is live" form leaves genuinely finished work alone.
			if (record.phase === 'terminal' && !wanted) continue;
			record.phase = 'stopped';
			if (record.controller && !record.controller.signal.aborted) {
				record.controller.abort();
			}
			stopped.push(record);
		}
		if (stopped.length > 0) this.#notify();
		return stopped;
	}

	terminal(messageId: string, generationId?: string): ChatGenerationRecord | null {
		const record = this.#records.get(messageId);
		if (!record || (generationId !== undefined && record.generationId !== generationId)) {
			return null;
		}
		record.phase = 'terminal';
		record.controller = null;
		this.#notify();
		return record;
	}

	activeForChat(chatId: string | null | undefined): ChatGenerationRecord[] {
		if (!chatId) return [];
		return [...this.#records.values()].filter(
			(record) => record.chatId === chatId && !SETTLED_PHASES.has(record.phase)
		);
	}

	taskIdsForChat(chatId: string | null | undefined): string[] {
		return [...new Set(this.activeForChat(chatId).flatMap((record) => [...record.taskIds]))];
	}

	/**
	 * Fold the server's authoritative work state for one chat into the registry.
	 *
	 * Returns the operations that are still live. When `chatId` is given the
	 * server answer is treated as COMPLETE for that chat: any record the server
	 * no longer lists has finished, so it is settled `terminal` here.
	 *
	 * That settle is load-bearing. Without it a record whose terminal event this
	 * tab missed (a dropped socket frame on a weak link) stayed `accepted`
	 * forever, and every consumer that asks "is this chat generating" answered
	 * yes for the rest of the session — the composer sat on its Stop button
	 * while the message itself rendered as finished. Records still in a
	 * LOCAL_ONLY phase are exempt: their POST hasn't reached the server yet, so
	 * absence from the list means "not yet", not "finished".
	 */
	reconcileServerOperations(
		operations: ServerGenerationOperation[] | null | undefined,
		navigationGeneration: number,
		chatId?: string | null
	): ServerGenerationOperation[] {
		const live = (operations ?? []).filter(
			(operation) => !this.isStopped(operation.message_id, operation.generation_id)
		);
		for (const operation of live) {
			let record = this.#records.get(operation.message_id);
			if (!record || record.generationId !== operation.generation_id) {
				record = this.begin({
					chatId: operation.chat_id,
					messageId: operation.message_id,
					generationId: operation.generation_id,
					turnId: operation.turn_id,
					navigationGeneration
				}).record;
			}
			if (operation.task_id) {
				record.taskIds.add(operation.task_id);
			}
			// Do not detach a local request controller merely because polling saw
			// its pre-task operation. Once transport ownership has handed off,
			// accepted accurately describes both bound and still-pending server
			// work.
			if (!record.controller && !SETTLED_PHASES.has(record.phase)) {
				record.phase = 'accepted';
			}
		}

		if (chatId) {
			const stillLive = new Set(live.map((operation) => operation.message_id));
			let settled = false;
			for (const record of this.#records.values()) {
				if (record.chatId !== chatId) continue;
				if (stillLive.has(record.messageId)) continue;
				if (SETTLED_PHASES.has(record.phase) || LOCAL_ONLY_PHASES.has(record.phase)) continue;
				record.phase = 'terminal';
				record.controller = null;
				settled = true;
			}
			if (settled) this.#notify();
		}

		return live;
	}

	generationsForStop(
		chatId: string,
		messageIds?: Iterable<string>
	): Array<{ generation_id: string; message_id: string; turn_id: string }> {
		const wanted = messageIds ? new Set(messageIds) : null;
		return [...this.#records.values()]
			.filter(
				(record) =>
					record.chatId === chatId &&
					(!wanted || wanted.has(record.messageId)) &&
					// Same rule as stop(): a named message is authoritative. A locally
					// `terminal` record can still own live backend work (a terminal
					// event for a superseded run, or a client retry loop between
					// attempts), so its identity must reach the Stop request.
					(record.phase !== 'terminal' || !!wanted)
			)
			// An observed record has no generation identity of its own yet; a Stop
			// request built from it would be addressed to nothing.
			.filter((record) => !!record.generationId && !!record.turnId)
			.map((record) => ({
				generation_id: record.generationId,
				message_id: record.messageId,
				turn_id: record.turnId
			}));
	}

	#trim(max = 300): void {
		if (this.#records.size <= max) return;
		for (const [messageId, record] of this.#records) {
			if (SETTLED_PHASES.has(record.phase)) {
				this.#records.delete(messageId);
				if (this.#records.size <= max) return;
			}
		}
	}
}
