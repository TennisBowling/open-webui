type TurnModelSelection = {
	explicitModelIds?: readonly string[] | null;
	explicitModelId?: string | null;
	mentionedModelId?: string | null;
	selectedModelIds?: readonly string[] | null;
};

type RetryModelSelection = {
	selectedModelIds?: readonly string[] | null;
	modelIdx?: number | null;
	fallbackModelId?: string | null;
};

const NON_RETRYABLE_GENERATION_ERROR_CODES = new Set([
	'chat_generation_in_progress',
	'generation_id_conflict',
	'generation_identity_required',
	'generation_reservation_lost'
]);

/**
 * FastAPI wraps application errors in `detail`, while provider adapters often
 * wrap them in `error`. Walk those two well-known envelopes so admission error
 * handling does not depend on which transport delivered the response.
 */
export const getChatGenerationErrorCode = (error: unknown): string | null => {
	let current = error;
	const seen = new Set<unknown>();
	for (let depth = 0; depth < 4; depth++) {
		if (!current || typeof current !== 'object' || seen.has(current)) return null;
		seen.add(current);
		const value = current as Record<string, unknown>;
		if (typeof value.code === 'string' && value.code.length > 0) return value.code;
		if (value.detail && typeof value.detail === 'object') {
			current = value.detail;
			continue;
		}
		if (value.error && typeof value.error === 'object') {
			current = value.error;
			continue;
		}
		return null;
	}
	return null;
};

/** Admission/identity failures cannot succeed by replaying the same request. */
export const isNonRetryableChatGenerationError = (error: unknown): boolean => {
	const code = getChatGenerationErrorCode(error);
	return code !== null && NON_RETRYABLE_GENERATION_ERROR_CODES.has(code);
};

/**
 * Resolve the model for an explicit retry action.
 *
 * The chat picker is the user's current intent. The completed response model is
 * only a fallback for stale/imported messages or while no picker selection is
 * available. For multi-model turns, keep the response's model slot stable.
 */
export const resolveRetryModelId = ({
	selectedModelIds = [],
	modelIdx = 0,
	fallbackModelId = null
}: RetryModelSelection): string | null => {
	const index = Number.isInteger(modelIdx) && Number(modelIdx) >= 0 ? Number(modelIdx) : 0;
	return (
		selectedModelIds?.[index] ??
		selectedModelIds?.find((modelId) => Boolean(modelId)) ??
		fallbackModelId
	);
};

/**
 * Resolve and copy the model ids for one turn.
 *
 * Callers keep this snapshot for the user row, assistant placeholder, and
 * generation request. Later picker/revalidation changes therefore cannot splice
 * a different model identity into an already-started turn.
 */
export const snapshotTurnModelIds = ({
	explicitModelIds = null,
	explicitModelId = null,
	mentionedModelId = null,
	selectedModelIds = []
}: TurnModelSelection): string[] => {
	if (explicitModelIds !== null) return [...explicitModelIds];
	if (explicitModelId) return [explicitModelId];
	if (mentionedModelId) return [mentionedModelId];
	return [...(selectedModelIds ?? [])];
};

/**
 * Did Stop claim one of the exact assistant placeholders for this send?
 *
 * A chat-wide "Stop happened" latch is intentionally not an input. It remains
 * useful for tearing down the turn that was stopped, but a later edit-resend is
 * a new turn with new message/generation identities and must not inherit it.
 */
export const wasGenerationStartStopped = (
	responseMessageIds: Iterable<string>,
	isMessageStopped: (messageId: string) => boolean
): boolean => [...responseMessageIds].some((messageId) => isMessageStopped(messageId));

type LoadedModelSelection = {
	persistedModelIds: readonly string[];
	currentModelIds: readonly string[];
	revalidationStartedAtRevision?: number | null;
	currentRevision: number;
};

/**
 * Apply server model state unless the user changed the picker after the
 * provisional chat revalidation began. In that case the explicit user intent
 * wins and will be persisted once revalidation releases the writer gate.
 */
export const resolveLoadedModelIds = ({
	persistedModelIds,
	currentModelIds,
	revalidationStartedAtRevision = null,
	currentRevision
}: LoadedModelSelection): { modelIds: string[]; preservedUserSelection: boolean } => {
	const preservedUserSelection =
		revalidationStartedAtRevision !== null && currentRevision > revalidationStartedAtRevision;

	return {
		modelIds: [...(preservedUserSelection ? currentModelIds : persistedModelIds)],
		preservedUserSelection
	};
};

export const hasAssistantResponseBody = (message: any): boolean => {
	if (typeof message?.content === 'string' && message.content.trim().length > 0) return true;
	if (!Array.isArray(message?.content_blocks)) return false;
	return message.content_blocks.some((block: any) => {
		if (!block) return false;
		if (typeof block.content === 'string') return block.content.trim().length > 0;
		if (Array.isArray(block.content)) return block.content.length > 0;
		return false;
	});
};

/**
 * Stamp `ended_at`/`duration` on every reasoning/tool_calls block a message left
 * open, and report whether anything changed.
 *
 * `duration == null` is the ONLY thing the renderer reads to choose between
 * "Thought for N seconds" and a spinning "Thinking…" (`blocksToDisplayMarkdown`
 * emits `done="false"`, and Collapsible turns that into a Spinner + shimmer).
 * Nothing about the message being done enters into it — so a turn stopped while
 * the model was thinking kept spinning forever, under a finished message.
 *
 * The backend closes these blocks too (`_finalize_open_agentic_blocks`) and
 * pushes the result, but the tab that pressed Stop DROPS inbound content for a
 * user-stopped message by design — that guard is what keeps late tokens from
 * landing after a cancel. So the stopping tab has to close them itself, which is
 * also instant and works with no connection at all. The two are complementary,
 * not redundant: observers get the push, the stopper does it locally.
 *
 * Mutates in place (the caller owns the message and re-publishes it) and is
 * idempotent — a block that already has `ended_at` is left alone, so a later
 * server push can't double-stamp or shrink a duration.
 */
export const closeOpenAgenticBlocks = (message: any): boolean => {
	if (!Array.isArray(message?.content_blocks)) return false;
	const now = Date.now() / 1000;
	let changed = false;
	for (const block of message.content_blocks) {
		if (!block || (block.type !== 'reasoning' && block.type !== 'tool_calls')) continue;
		if (block.started_at == null || block.ended_at != null) continue;
		block.ended_at = now;
		block.duration = Math.max(0, Math.floor(now - block.started_at));
		changed = true;
	}
	return changed;
};

/**
 * Resolve a persisted assistant row after the server authoritatively reports no
 * live generation. Absence of work is not proof of successful completion: an
 * empty, explicitly unfinished placeholder is an interrupted turn and must be
 * shown as such instead of becoming a blank successful response.
 */
export const inactiveAssistantTerminalPatch = (
	message: any
): { done: true; error?: { content: string } } => {
	if (
		message?.done === false &&
		!message?.error &&
		message?.userStopped !== true &&
		!hasAssistantResponseBody(message)
	) {
		return {
			done: true,
			error: {
				content: 'The model request ended before a response could be saved. Please retry.'
			}
		};
	}
	return { done: true };
};
