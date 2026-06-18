/**
 * Structural key for the rendered message chain (Fix B: streaming reactivity
 * isolation).
 *
 * The Messages component re-walks the message linked list (currentId → root) to
 * build the rendered array. Historically that walk re-ran on EVERY `history`
 * reassignment — including the ~60/sec streaming content flushes — making each
 * streaming frame O(chat length) and re-rendering the whole list. That is the
 * dominant "lag gets worse as the message grows" cost, worst on mobile.
 *
 * The fix gates the walk on this key, which is a pure function of the chain's
 * STRUCTURE and deliberately independent of message CONTENT. A streaming token
 * flush mutates the leaf message's content in place but changes none of these
 * inputs, so the key is stable and the walk does not re-run; only the streaming
 * leaf's own subtree repaints.
 *
 * Inputs:
 * - `structureRevision`: bumped by the parent (Chat.svelte) on full history
 *   rebuilds (load / reattach) where id-count and currentId might coincide.
 * - `localStructureRevision`: bumped by Messages.svelte's own graph-shape
 *   mutations (pagination / stub hydration / branch-affecting edits).
 * - `currentId`: the branch pointer; changes on every branch navigation.
 * - `messagesCount`: the pagination cap.
 * - `messageMapSize`: number of messages in the map — auto-detects add/delete
 *   without having to instrument every message-creation site in Chat.svelte.
 */
export function computeChainStructureKey(input: {
	structureRevision: number;
	localStructureRevision: number;
	currentId: string | null | undefined;
	messagesCount: number | null;
	messageMapSize: number;
}): string {
	const { structureRevision, localStructureRevision, currentId, messagesCount, messageMapSize } =
		input;
	return `${structureRevision}:${localStructureRevision}:${currentId ?? ''}:${
		messagesCount ?? 'null'
	}:${messageMapSize}`;
}
