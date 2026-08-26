import { hydrateToolResultsInBlocks, mergeToolResultEntries } from '$lib/utils/toolResults';

// Structural shape of the per-message StreamMirror that applyDeltaOp operates
// on. The full mirror type (with version / pending_deltas / snapshot fields)
// lives in Chat.svelte; applyDeltaOp only reads content_blocks + tool_results,
// so the reducer is typed against just that surface.
export type StreamMirrorBlocks = {
	content_blocks: any[];
	tool_results: Map<string, any>;
};

// Merge a streamed string fragment (reasoning_details text/data/summary).
// Streams send true deltas, which must append byte-for-byte; the only
// compatibility case handled is a provider resending a cumulative full prefix
// (chunk starts with the ENTIRE existing string). Partial suffix/prefix
// overlap dedupe is deliberately NOT done: it cannot be distinguished from
// legitimately repeated text and eats real characters ('bana'+'na', doubled
// letters split across tokens). Mirrors backend merge_streamed_field — keep
// both in sync so the live view matches what the backend persists.
export const mergeStreamedString = (
	existing: string | null | undefined,
	chunk: string | null | undefined
) => {
	if (!chunk) return existing ?? '';
	existing = existing ?? '';
	if (!existing) return chunk;
	if (chunk === existing) return existing;
	if (chunk.startsWith(existing)) return chunk;
	return existing + chunk;
};

// Merge ONE streamed reasoning_details fragment into an accumulated list, in
// place. Match by (id, type), adopting an id-less entry of the same
// (type, index) when the provider only emits `id` on a later chunk; id-less
// fragments match on (type, index). text/data/summary accumulate via
// mergeStreamedString; id/signature/format/index adopt the latest non-null
// value; `type` is part of the match key and is never overwritten. Mirrors
// _apply_reasoning_detail_delta in backend middleware.py — keep in lockstep.
export const mergeReasoningDetail = (details: any[], detail: any) => {
	const detailId = detail.id ?? null;
	const detailType = detail.type ?? null;
	const detailIdx = detail.index ?? 0;

	let existing = null;
	if (detailId !== null) {
		existing =
			details.find((d) => d.id === detailId && d.type === detailType) ??
			details.find((d) => d.id == null && d.type === detailType && d.index === detailIdx);
	} else {
		existing = details.find((d) => d.type === detailType && d.index === detailIdx);
	}

	if (!existing) {
		details.push({ ...detail });
		return;
	}
	if (detail.text) existing.text = mergeStreamedString(existing.text, detail.text);
	if (detail.data) existing.data = mergeStreamedString(existing.data, detail.data);
	if (detail.summary) existing.summary = mergeStreamedString(existing.summary, detail.summary);
	if (detail.id != null) existing.id = detail.id;
	if (detail.signature != null) existing.signature = detail.signature;
	if (detail.format != null) existing.format = detail.format;
	if (detail.index != null) existing.index = detail.index;
};

export const normalizeStreamingContentBlocks = (blocks: any[] = []) => {
	for (let i = 1; i < blocks.length; i++) {
		const prev = blocks[i - 1];
		const cur = blocks[i];
		if (!prev || !cur || prev.type !== cur.type || !['text', 'reasoning'].includes(cur.type)) {
			continue;
		}
		const prevText = prev.content || '';
		const curText = cur.content || '';
		if (curText.includes(prevText)) {
			prev.content = curText;
		} else if (!prevText.includes(curText)) {
			prev.content = `${prevText}${curText}`;
		}
		blocks.splice(i, 1);
		i -= 1;
	}
};

export const bumpStreamingBlockRevision = (block: any) => {
	if (!block || typeof block !== 'object') return;
	const next = ((block as any).__owui_rev ?? 0) + 1;
	try {
		Object.defineProperty(block, '__owui_rev', {
			value: next,
			writable: true,
			configurable: true,
			enumerable: false
		});
	} catch {
		(block as any).__owui_rev = next;
	}
};

// Apply one v2.1 delta op against a StreamMirror's content_blocks array.
// Mirrors the backend's serialize/append logic in middleware.py — the two
// must stay in lockstep, exactly like the reasoning_details merger does.
//
// Returns true when the op revealed STRUCTURAL INCOHERENCE between this mirror
// and the server (an append/close targeting a block that doesn't exist here or
// has the wrong type — i.e. a block_open/tool_call_add this mirror never
// applied). The op is still absorbed best-effort so text keeps rendering, but
// a fabricated block has to GUESS its type ('text'): if the missing open was a
// reasoning block, reasoning would render as answer prose forever. Callers
// must treat `true` as "resync from the authoritative snapshot".
export const applyDeltaOp = (
	mirror: StreamMirrorBlocks,
	op: string,
	payload: any
): boolean => {
	if (!payload) payload = {};
	const changedBlocks: any[] = [];
	let structuralGap = false;
	if (op === 'text_append') {
		const idx = payload.block_idx;
		const block = mirror.content_blocks[idx];
		const text = payload.text || '';
		if (block && (block.type === 'text' || block.type === 'reasoning')) {
			const current = block.content || '';
			block.content =
				text.includes(current) && text.length > current.length ? text : current + text;
			changedBlocks.push(block);
		} else if (block) {
			// Block exists but is a different type (e.g. tool_calls) — the server
			// thinks this index is a text/reasoning block. The mirrors diverged;
			// dropping the text silently would lose content.
			structuralGap = true;
		} else if (idx === mirror.content_blocks.length) {
			const prev = mirror.content_blocks[idx - 1];
			// Defensive: if an out-of-order replace/open made the server send a
			// full prefix as the next append, update the existing tail block
			// instead of creating duplicate progressively-longer text blocks.
			if (
				prev &&
				(prev.type === 'text' || prev.type === 'reasoning') &&
				text.startsWith(prev.content || '')
			) {
				prev.content = text;
				changedBlocks.push(prev);
			} else {
				// The block_open for this index was never applied here — its type
				// is unknown. Render as text for now, but report the gap so the
				// caller re-syncs (this is exactly how reasoning used to get stuck
				// rendering as the final answer after a missed block_open).
				const nextBlock = { type: 'text', content: text };
				mirror.content_blocks.push(nextBlock);
				changedBlocks.push(nextBlock);
				structuralGap = true;
			}
		} else {
			// Append beyond the end with a hole in between — content lost.
			structuralGap = true;
		}
	} else if (op === 'block_open') {
		const block: any = { type: payload.type, content: '' };
		if (payload.attrs && typeof payload.attrs === 'object') {
			Object.assign(block, payload.attrs);
		}
		if (payload.type === 'tool_calls' && !Array.isArray(block.content)) {
			block.content = [];
		}
		if (typeof payload.block_idx === 'number') {
			if (payload.block_idx > mirror.content_blocks.length) {
				// Opening past the end means this mirror never applied the opens
				// for the indices in between — assigning directly would create
				// ARRAY HOLES (undefined entries that serialize as JSON null and
				// then poison anything that persists a clone of these blocks,
				// e.g. a rewind sibling). content_blocks must stay dense: pad
				// with inert placeholders so indices line up, and report the gap
				// so the caller heals from the authoritative snapshot. The
				// placeholder type is deliberately NOT text/reasoning — the
				// adjacent-block normalizer would merge those and shift every
				// later server index; unknown types render nothing and are
				// replaced wholesale when the heal snapshot lands.
				while (mirror.content_blocks.length < payload.block_idx) {
					mirror.content_blocks.push({ type: 'placeholder', content: '' });
				}
				structuralGap = true;
			}
			const existing = mirror.content_blocks[payload.block_idx];
			if (
				existing?.type === payload.type &&
				(payload.type === 'text' || payload.type === 'reasoning') &&
				typeof existing.content === 'string' &&
				existing.content
			) {
				// Defensive against out-of-order socket delivery from older servers:
				// text_append may have created this block before block_open arrives.
				block.content = existing.content;
			}
			mirror.content_blocks[payload.block_idx] = block;
		} else {
			mirror.content_blocks.push(block);
		}
	} else if (op === 'block_close') {
		const block = mirror.content_blocks[payload.block_idx];
		if (block) {
			if (payload.duration != null) block.duration = payload.duration;
			if (payload.output != null) block.output = payload.output;
			if (payload.ended != null) block.ended = payload.ended;
			if (payload.ended_at != null) block.ended_at = payload.ended_at;
			if (Array.isArray(payload.results)) {
				block.results = mergeToolResultEntries(
					payload.results,
					mirror.tool_results,
					Array.isArray(block.results) ? block.results : []
				);
			}
			changedBlocks.push(block);
		} else {
			// Closing a block this mirror never opened — e.g. a reasoning block's
			// duration stamp landing on nothing. The mirrors diverged.
			structuralGap = true;
		}
	} else if (op === 'tool_call_add') {
		const block = mirror.content_blocks[payload.block_idx];
		if (block && block.type === 'tool_calls') {
			if (!Array.isArray(block.content)) block.content = [];
			block.content.push(payload.tool_call);
			changedBlocks.push(block);
		} else {
			structuralGap = true;
		}
	} else if (op === 'tool_call_args_append') {
		// Tool-call argument fragments always append to the most-recently
		// added tool call, so scan from the END (newest block / newest call
		// first) and stop at the first match. This turns the common case into
		// O(1) instead of O(blocks × calls-per-block) on every args chunk —
		// the latter is quadratic across a 100–300 tool-call response.
		const blocks = mirror.content_blocks;
		let found = false;
		for (let bi = blocks.length - 1; bi >= 0 && !found; bi--) {
			const block = blocks[bi];
			if (block?.type !== 'tool_calls' || !Array.isArray(block.content)) continue;
			for (let ci = block.content.length - 1; ci >= 0; ci--) {
				const tc = block.content[ci];
				if (tc?.id === payload.tool_call_id || tc?.tool_call_id === payload.tool_call_id) {
					const fn = tc.function || (tc.function = {});
					fn.arguments = (fn.arguments || '') + (payload.args_delta || '');
					found = true;
					break;
				}
			}
		}
		if (!found) {
			// Args for a tool call this mirror never saw added — divergence.
			structuralGap = true;
		}
	} else if (op === 'reasoning_detail_merge') {
		const detail = payload.detail || {};
		const target = mirror.content_blocks.find(
			(b) => b?.type === 'reasoning' && Array.isArray(b.details)
		);
		if (target) {
			mergeReasoningDetail(target.details, detail);
			changedBlocks.push(target);
		} else {
			// The server has a reasoning block with details; this mirror doesn't.
			structuralGap = true;
		}
	} else if (op === 'sources' || op === 'selected_model_id' || op === 'usage') {
		// Carried on the message, not on a content block — handled by the caller.
	} else if (op === 'replace') {
		if (typeof payload.block_idx === 'number' && Array.isArray(payload.content_blocks)) {
			if (payload.block_idx > 0) {
				const replacementBlocks = hydrateToolResultsInBlocks(
					payload.content_blocks,
					mirror.tool_results,
					mirror.content_blocks.slice(
						payload.block_idx,
						payload.block_idx + payload.content_blocks.length
					)
				);
				mirror.content_blocks.splice(
					payload.block_idx,
					payload.content_blocks.length,
					...replacementBlocks
				);
				changedBlocks.push(...replacementBlocks);
			} else {
				mirror.content_blocks = hydrateToolResultsInBlocks(
					payload.content_blocks.slice(),
					mirror.tool_results,
					mirror.content_blocks
				);
				changedBlocks.push(...mirror.content_blocks);
			}
		} else if (Array.isArray(payload.content_blocks)) {
			mirror.content_blocks = hydrateToolResultsInBlocks(
				payload.content_blocks.slice(),
				mirror.tool_results,
				mirror.content_blocks
			);
			changedBlocks.push(...mirror.content_blocks);
		}
	} else {
		console.warn('[chat:delta] unknown op', op, payload);
	}
	if (op !== 'text_append' && op !== 'tool_call_args_append' && op !== 'reasoning_detail_merge') {
		normalizeStreamingContentBlocks(mirror.content_blocks);
	}
	for (const block of changedBlocks) {
		bumpStreamingBlockRevision(block);
	}
	return structuralGap;
};

// Snapshot-adoption decision for a per-message stream mirror. Pure so the
// rules are unit-testable — they went wrong twice as inline heuristics:
// first "keep richer" (character counts) kept corrupted live blocks over the
// correct snapshot; then "adopt iff newer" both let an EMPTY same-run
// snapshot wipe a finished answer (version 0 DB fallback at the terminal
// race) AND refused the healing snapshot whenever the corrupted live mirror
// was AHEAD of the server's snapshot cadence.
//
//  - 'ignore': stale-run snapshot (raced a retry) — nothing in it is usable.
//  - 'adopt' : replace mirror blocks/version with the snapshot's.
//  - 'keep'  : keep live blocks AND version (never rewind the version below
//              the content it describes).
//
// `heal` marks an AUTHORITATIVE reconcile (structural gap detected, server
// sent op:snapshot after dropping an oversized op, or terminal chat:done):
// the server state must win even at a LOWER version — the caller then
// replays deltas forward from the adopted version to catch back up.
// The one thing heal does not override is the empty-over-content guard: a
// same-run snapshot with no content never wipes a message that has some
// (a retry's run-advance legitimately blanks; that adopts via runAdvanced).
export type SnapshotAdoptionInput = {
	snapRun: number; // 0 = unknown
	snapVersion: number;
	snapTerminal: boolean;
	snapHasContent: boolean;
	mirrorRun: number; // 0 = unknown
	mirrorVersion: number;
	liveHasContent: boolean;
	heal?: boolean;
};

export const decideSnapshotAdoption = (
	i: SnapshotAdoptionInput
): 'ignore' | 'adopt' | 'keep' => {
	if (i.snapRun && i.mirrorRun && i.snapRun < i.mirrorRun) return 'ignore';
	const runAdvanced = !!(i.snapRun && (!i.mirrorRun || i.snapRun > i.mirrorRun));
	if (!i.snapHasContent && i.liveHasContent && !runAdvanced) return 'keep';
	if (runAdvanced) return 'adopt';
	if (i.heal) return 'adopt';
	if (i.snapVersion >= i.mirrorVersion) return 'adopt';
	if (i.snapTerminal) return 'adopt';
	return 'keep';
};

export const compactStreamOps: Record<string, string> = {
	t: 'text_append',
	o: 'block_open',
	c: 'block_close',
	a: 'tool_call_add',
	g: 'tool_call_args_append',
	r: 'reasoning_detail_merge',
	s: 'sources',
	m: 'selected_model_id',
	u: 'usage',
	p: 'replace',
	x: 'snapshot'
};

export const decodeCompactStreamPayload = (op: string, frame: any[]) => {
	const legacyPayload =
		frame.length === 3 && frame[2] && typeof frame[2] === 'object' && !Array.isArray(frame[2]);
	if (op === 'text_append') {
		return legacyPayload ? frame[2] : { block_idx: frame[2], text: frame[3] ?? '' };
	}
	if (op === 'block_open') {
		return legacyPayload
			? frame[2]
			: { block_idx: frame[2], type: frame[3], attrs: frame[4] ?? {} };
	}
	if (op === 'block_close') {
		if (legacyPayload) return frame[2];
		return {
			block_idx: frame[2],
			...(frame[3] != null ? { duration: frame[3] } : {}),
			...(frame[4] != null ? { output: frame[4] } : {}),
			...(frame[5] != null ? { ended: frame[5] } : {}),
			...(Array.isArray(frame[6]) ? { results: frame[6] } : {}),
			...(frame[7] != null ? { ended_at: frame[7] } : {})
		};
	}
	if (op === 'tool_call_add') {
		return legacyPayload ? frame[2] : { block_idx: frame[2], tool_call: frame[3] };
	}
	if (op === 'tool_call_args_append') {
		return legacyPayload ? frame[2] : { tool_call_id: frame[2], args_delta: frame[3] ?? '' };
	}
	if (op === 'reasoning_detail_merge') {
		if (legacyPayload && 'detail' in frame[2]) return frame[2];
		return { detail: frame[2] ?? {} };
	}
	if (op === 'sources') {
		return legacyPayload ? frame[2] : { sources: Array.isArray(frame[2]) ? frame[2] : [] };
	}
	if (op === 'selected_model_id') {
		return legacyPayload ? frame[2] : { model_id: frame[2] };
	}
	if (op === 'usage') {
		return legacyPayload ? frame[2] : { usage: frame[2] ?? {} };
	}
	if (op === 'replace') {
		return legacyPayload
			? frame[2]
			: { block_idx: frame[2] ?? 0, content_blocks: Array.isArray(frame[3]) ? frame[3] : [] };
	}
	return legacyPayload ? frame[2] : (frame[2] ?? {});
};
