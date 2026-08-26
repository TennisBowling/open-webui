/**
 * Frontend subagent state is stored in one flat Svelte record for cheap updates,
 * but a tool call is only unique inside its containing parent assistant message.
 * Rewind siblings deliberately reuse tool_call_id, subagent_id, chat_id and
 * entry_key while carrying different outcomes. Composite keys make the parent
 * message part of the identity without forcing every consumer onto a nested map.
 */

const SCOPE_PREFIX = '@subagent-parent:';
const SCOPE_SEPARATOR = '\u001f';

const uniqueStrings = (values: unknown[]): string[] =>
	Array.from(
		new Set(
			values.filter((value): value is string => typeof value === 'string' && value.length > 0)
		)
	);

export const subagentRunAliases = (run: any, extra: unknown[] = []): string[] =>
	uniqueStrings([...extra, run?.entry_key, run?.tool_call_id, run?.subagent_id, run?.chat_id]);

export const subagentScopedStateKey = (parentMessageId: string, alias: string): string =>
	parentMessageId && alias ? `${SCOPE_PREFIX}${parentMessageId}${SCOPE_SEPARATOR}${alias}` : alias;

export const subagentScopedStateKeys = (parentMessageId: string, aliases: unknown[]): string[] => {
	const normalized = uniqueStrings(aliases);
	return parentMessageId
		? normalized.map((alias) => subagentScopedStateKey(parentMessageId, alias))
		: normalized;
};

const runMatchesAlias = (run: any, aliases: Set<string>): boolean =>
	aliases.has(run?.entry_key) ||
	aliases.has(run?.tool_call_id) ||
	aliases.has(run?.subagent_id) ||
	aliases.has(run?.chat_id);

/**
 * Resolve a run for a rendered parent message. Scoped aliases always win.
 * A legacy unscoped alias is accepted only when it has no parent attribution or
 * belongs to the requested parent, so branch B can never answer a lookup for A.
 */
export const findSubagentRunEntry = (
	states: Record<string, any> | null | undefined,
	parentMessageId: string,
	aliases: unknown[],
	options: { scan?: boolean } = {}
): [string, any] | null => {
	if (!states) return null;
	const normalized = uniqueStrings(aliases);
	if (normalized.length === 0) return null;

	if (parentMessageId) {
		for (const key of subagentScopedStateKeys(parentMessageId, normalized)) {
			const run = states[key];
			if (run && (!run.parent_message_id || run.parent_message_id === parentMessageId)) {
				return [key, run];
			}
		}
	}

	for (const alias of normalized) {
		const run = states[alias];
		if (
			run &&
			(!parentMessageId || !run.parent_message_id || run.parent_message_id === parentMessageId)
		) {
			return [alias, run];
		}
	}

	if (options.scan === false) return null;
	const aliasSet = new Set(normalized);
	for (const [key, run] of Object.entries(states)) {
		if (!run || typeof run !== 'object') continue;
		if (parentMessageId && run.parent_message_id && run.parent_message_id !== parentMessageId) {
			continue;
		}
		if (runMatchesAlias(run, aliasSet)) return [key, run];
	}
	return null;
};

/**
 * Write every parent-scoped alias for a run. A legacy raw alias is retained for
 * old/no-context consumers, but it is never overwritten by another parent
 * message; scoped consumers therefore remain exact and legacy behavior remains
 * deterministic instead of last-hydrated-branch-wins.
 */
export const setSubagentRunAliases = (
	states: Record<string, any>,
	run: any,
	aliases: unknown[] = [],
	parentMessageId = run?.parent_message_id ?? ''
): void => {
	if (!run || typeof run !== 'object') return;
	const normalized = subagentRunAliases(run, aliases);
	if (normalized.length === 0) return;

	for (const key of subagentScopedStateKeys(parentMessageId, normalized)) {
		states[key] = run;
	}

	for (const alias of normalized) {
		const existing = states[alias];
		if (
			!existing ||
			!parentMessageId ||
			!existing.parent_message_id ||
			existing.parent_message_id === parentMessageId
		) {
			states[alias] = run;
		}
	}
};

/**
 * Build the minimum durable card state directly from the messages returned by
 * chat-open. Richer hydration can subsequently merge task/stream ownership,
 * but a bug in that enrichment must never erase persisted subagent outcomes.
 *
 * The containing message is authoritative for parent attribution. Old rows can
 * carry a stale parent_message_id after being copied onto a rewind sibling, and
 * trusting that embedded value would reintroduce cross-branch alias collisions.
 */
export const seedPersistedSubagentRuns = (
	messages: Record<string, any> | null | undefined
): Record<string, any> => {
	const states: Record<string, any> = {};
	if (!messages || typeof messages !== 'object' || Array.isArray(messages)) return states;

	for (const [messageKey, message] of Object.entries(messages)) {
		if (!message || typeof message !== 'object' || Array.isArray(message)) continue;
		const runs = message.subagent_runs;
		if (!runs || typeof runs !== 'object' || Array.isArray(runs)) continue;
		const parentMessageId =
			typeof message.id === 'string' && message.id.length > 0 ? message.id : messageKey;
		if (!parentMessageId) continue;

		for (const [entryKey, value] of Object.entries(runs)) {
			if (!value || typeof value !== 'object' || Array.isArray(value)) continue;
			const run: any = {
				...value,
				entry_key:
					typeof (value as any).entry_key === 'string' && (value as any).entry_key
						? (value as any).entry_key
						: entryKey,
				subagent_id: (value as any).subagent_id || (value as any).chat_id || '',
				chat_id: (value as any).chat_id || (value as any).subagent_id || '',
				parent_message_id: parentMessageId
			};
			setSubagentRunAliases(states, run, [entryKey], parentMessageId);
		}
	}
	return states;
};

/**
 * Count continuation turns for one hidden subagent in an alias-indexed store.
 * One logical run can occur under many state keys and can be carried onto
 * several rewind siblings; its stable entry/assistant/tool id must count once.
 */
export const countUniqueSubagentContinuations = (
	states: Record<string, any> | null | undefined,
	subagentId: string
): number => {
	if (!states || !subagentId) return 0;
	const identities = new Set<string>();
	for (const run of Object.values(states)) {
		if (
			!run ||
			typeof run !== 'object' ||
			run.continuation !== true ||
			(run.subagent_id !== subagentId && run.chat_id !== subagentId)
		) {
			continue;
		}
		const identity =
			run.entry_key ||
			run.assistant_msg_id ||
			run.tool_call_id ||
			`${run.subagent_id ?? run.chat_id}:${run.started_at ?? ''}`;
		if (identity) identities.add(String(identity));
	}
	return identities.size;
};

export const isTerminalSubagentStatus = (status: unknown): boolean =>
	status === 'done' || status === 'error' || status === 'cancelled';

/**
 * Normalize the rerun ownership keys bundled into a consolidated chat-open or
 * /api/tasks/chat response. Keeping this in a pure helper makes reload
 * hydration independent of function-local request variables.
 */
export const activeSubagentRerunEntryKeys = (active: any): Set<string> =>
	new Set(
		uniqueStrings(
			Array.isArray(active?.subagent_rerun_entry_keys) ? active.subagent_rerun_entry_keys : []
		)
	);

export const activeSubagentStreamMessageIds = (active: any): Set<string> =>
	new Set(
		uniqueStrings(
			(Array.isArray(active?.streams) ? active.streams : []).map(
				(stream: any) => stream?.message_id
			)
		)
	);

export const subagentRunHasActiveRerunKey = (
	activeEntryKeys: Set<string>,
	entryKey: unknown,
	run: any
): boolean =>
	uniqueStrings([entryKey, run?.entry_key, run?.subagent_id, run?.tool_call_id]).some((key) =>
		activeEntryKeys.has(key)
	);

export const isDetachedSubagentRerun = (run: any): boolean =>
	run?.rerun === true || run?.detached_rerun === true || Boolean(run?.rerun_task_id);

export const hasActiveDetachedSubagentRerun = (
	states: Record<string, any> | null | undefined
): boolean =>
	Boolean(
		states &&
			Object.values(states).some(
				(run: any) =>
					run &&
					run.status === 'running' &&
					run.ended_at == null &&
					(isDetachedSubagentRerun(run) || Boolean(run.rerun_id))
			)
	);

/**
 * The parent turn's terminal sweep owns inline children only. A detached rerun
 * owns its own terminal write and can overlap a parent action in another tab.
 */
export const shouldParentFinalizeSubagentRun = (run: any, parentMessageId: string): boolean =>
	Boolean(
		run &&
			run.status === 'running' &&
			(!run.parent_message_id || run.parent_message_id === parentMessageId) &&
			!isDetachedSubagentRerun(run)
	);

/**
 * Compare two detached rerun generations.
 *
 * rerun_attempt is a persisted monotonic counter and is authoritative. The
 * second-resolution started_at comparison is retained only for older servers.
 * null means the generations differ but the legacy evidence cannot order them.
 */
export const compareSubagentRerunGeneration = (existing: any, incoming: any): number | null => {
	const existingId = existing?.rerun_id;
	const incomingId = incoming?.rerun_id;
	if (!existingId || !incomingId) return null;
	if (existingId === incomingId) return 0;

	const existingAttempt = Number(existing?.rerun_attempt);
	const incomingAttempt = Number(incoming?.rerun_attempt);
	if (
		Number.isFinite(existingAttempt) &&
		existingAttempt > 0 &&
		Number.isFinite(incomingAttempt) &&
		incomingAttempt > 0 &&
		existingAttempt !== incomingAttempt
	) {
		return incomingAttempt > existingAttempt ? 1 : -1;
	}

	const existingStartedAt = Number(existing?.started_at);
	const incomingStartedAt = Number(incoming?.started_at);
	if (
		Number.isFinite(existingStartedAt) &&
		Number.isFinite(incomingStartedAt) &&
		existingStartedAt !== incomingStartedAt
	) {
		return incomingStartedAt > existingStartedAt ? 1 : -1;
	}
	return null;
};

/**
 * A differing rerun id is safe to apply only when persisted ordering evidence
 * proves it is newer. "Unknown" must fail closed: otherwise a delayed start
 * event from a legacy/same-second attempt can turn a terminal card back into a
 * spinner after the newer generation already completed.
 */
export const shouldApplyIncomingSubagentGeneration = (existing: any, incoming: any): boolean => {
	const existingId = existing?.rerun_id;
	const incomingId = incoming?.rerun_id;
	if (!existingId || !incomingId || existingId === incomingId) return true;
	return compareSubagentRerunGeneration(existing, incoming) === 1;
};

/**
 * A rerun POST can resolve after its socket terminal event. In that ordering,
 * the optimistic "running" write for the same generation must be skipped.
 */
export const shouldApplyRerunOptimisticState = (
	currentRun: any,
	rerunId: string | null | undefined
): boolean =>
	!(rerunId && currentRun?.rerun_id === rerunId && isTerminalSubagentStatus(currentRun?.status));

/**
 * Prove that a batch redo result belongs to the attempt just launched.
 * rerun_id is authoritative; ended_at remains only a compatibility fallback
 * for older servers and deliberately cannot be the primary identity because it
 * has one-second resolution.
 */
export const isFreshRerunResult = (
	run: any,
	rerunId: string | null | undefined,
	priorEndedAt: unknown
): boolean =>
	run?.status === 'done' &&
	(rerunId ? run?.rerun_id === rerunId : run?.ended_at != null && run.ended_at !== priorEndedAt);
