import { describe, expect, it } from 'vitest';

import {
	applyDeltaOp,
	decideSnapshotAdoption,
	mergeReasoningDetail,
	mergeStreamedString,
	type StreamMirrorBlocks
} from './stream-protocol';

const makeMirror = (blocks: any[] = []): StreamMirrorBlocks => ({
	content_blocks: blocks,
	tool_results: new Map()
});

describe('mergeStreamedString', () => {
	it('appends incremental deltas byte-for-byte, never deduping partial overlaps', () => {
		// Regression: the old suffix-overlap heuristic silently dropped
		// legitimately repeated text (a letter here and there), diverging the
		// live view from the backend-persisted content.
		expect(mergeStreamedString('bana', 'na')).toBe('banana');
		expect(mergeStreamedString('Hmm..', '.')).toBe('Hmm...');
		expect(mergeStreamedString('count 1', '11')).toBe('count 111');
		expect(mergeStreamedString('the code ((', '(x))')).toBe('the code (((x))');
		expect(mergeStreamedString('I think the answer', 'r is')).toBe('I think the answerr is');
	});

	it('collapses only cumulative full-prefix resends', () => {
		expect(mergeStreamedString('I think', 'I think about X')).toBe('I think about X');
		expect(mergeStreamedString('I think', 'I think')).toBe('I think');
		expect(mergeStreamedString('', 'x')).toBe('x');
		expect(mergeStreamedString('x', '')).toBe('x');
	});
});

describe('mergeReasoningDetail', () => {
	it('matches id-less fragments on (type, index) and accumulates byte-exact', () => {
		const details: any[] = [];
		mergeReasoningDetail(details, { type: 'reasoning.text', index: 0, text: 'bana' });
		mergeReasoningDetail(details, { type: 'reasoning.text', index: 0, text: 'na' });
		expect(details).toEqual([{ type: 'reasoning.text', index: 0, text: 'banana' }]);
	});

	it('adopts an id-less entry when the id only arrives on a later chunk', () => {
		const details: any[] = [{ type: 'reasoning.text', index: 0, text: 'bana' }];
		mergeReasoningDetail(details, { type: 'reasoning.text', index: 0, id: 'r1', text: 'na' });
		expect(details).toEqual([{ type: 'reasoning.text', index: 0, id: 'r1', text: 'banana' }]);
		// Subsequent id-keyed fragments keep matching the adopted entry.
		mergeReasoningDetail(details, { type: 'reasoning.text', index: 0, id: 'r1', text: '!' });
		expect(details[0].text).toBe('banana!');
	});

	it('appends fragments with a new (type, index) or id', () => {
		const details: any[] = [{ type: 'reasoning.text', index: 0, text: 'a' }];
		mergeReasoningDetail(details, { type: 'reasoning.encrypted', index: 1, data: 'zzz' });
		expect(details).toHaveLength(2);
	});
});

describe('applyDeltaOp structural-gap reporting', () => {
	it('applies a normal open → append sequence without reporting a gap', () => {
		const mirror = makeMirror();
		expect(applyDeltaOp(mirror, 'block_open', { block_idx: 0, type: 'reasoning' })).toBe(false);
		expect(applyDeltaOp(mirror, 'text_append', { block_idx: 0, text: 'thinking…' })).toBe(false);
		expect(mirror.content_blocks[0].type).toBe('reasoning');
		expect(mirror.content_blocks[0].content).toBe('thinking…');
	});

	it('reports a gap when an append fabricates a block whose open was missed', () => {
		// This is the exact reasoning-rendered-as-answer corruption: the
		// block_open{type:'reasoning'} was lost (network blip), so the append
		// has to guess 'text'. The op still renders, but the caller must be
		// told to re-sync from the snapshot.
		const mirror = makeMirror();
		const gap = applyDeltaOp(mirror, 'text_append', { block_idx: 0, text: 'secret thoughts' });
		expect(gap).toBe(true);
		expect(mirror.content_blocks[0]).toMatchObject({ type: 'text', content: 'secret thoughts' });
	});

	it('does not report a gap for the full-prefix tail-update rescue', () => {
		const mirror = makeMirror([{ type: 'text', content: 'par' }]);
		const gap = applyDeltaOp(mirror, 'text_append', { block_idx: 1, text: 'partial answer' });
		expect(gap).toBe(false);
		expect(mirror.content_blocks).toHaveLength(1);
		expect(mirror.content_blocks[0].content).toBe('partial answer');
	});

	it('reports a gap when an append targets a mistyped block', () => {
		const mirror = makeMirror([{ type: 'tool_calls', content: [] }]);
		const gap = applyDeltaOp(mirror, 'text_append', { block_idx: 0, text: 'lost text' });
		expect(gap).toBe(true);
	});

	it('reports a gap when an append lands beyond the end with a hole', () => {
		const mirror = makeMirror();
		const gap = applyDeltaOp(mirror, 'text_append', { block_idx: 3, text: 'orphan' });
		expect(gap).toBe(true);
		expect(mirror.content_blocks).toHaveLength(0);
	});

	it('reports a gap when closing a block that was never opened', () => {
		const mirror = makeMirror();
		expect(applyDeltaOp(mirror, 'block_close', { block_idx: 2, duration: 4 })).toBe(true);
	});

	it('pads missed indices densely (never array holes) when an open lands past the end', () => {
		// Assigning blocks[2] on a length-0 array used to create JS holes that
		// serialize as JSON null — a rewind sibling then persisted them and the
		// backend's conversation assembly crashed on `null.get(...)`. The open
		// must pad the skipped indices with inert blocks AND report the gap so
		// the caller heals the placeholders from the authoritative snapshot.
		const mirror = makeMirror();
		const gap = applyDeltaOp(mirror, 'block_open', { block_idx: 2, type: 'reasoning' });
		expect(gap).toBe(true);
		// Padded with non-mergeable placeholders so later server indices stay
		// aligned until the heal snapshot replaces the blocks wholesale.
		expect(mirror.content_blocks).toHaveLength(3);
		expect(mirror.content_blocks.every((b) => b && typeof b === 'object')).toBe(true);
		expect(mirror.content_blocks[2].type).toBe('reasoning');
		// Dense = JSON round-trip carries no nulls.
		expect(JSON.parse(JSON.stringify(mirror.content_blocks)).every((b: any) => b !== null)).toBe(
			true
		);
	});

	it('does not report a gap for an in-order open at exactly the next index', () => {
		const mirror = makeMirror([{ type: 'reasoning', content: 'done thinking' }]);
		expect(applyDeltaOp(mirror, 'block_open', { block_idx: 1, type: 'tool_calls' })).toBe(false);
		expect(mirror.content_blocks).toHaveLength(2);
	});

	it('reports a gap for tool_call_add onto a missing/mistyped block', () => {
		const mirror = makeMirror([{ type: 'text', content: 'hi' }]);
		expect(applyDeltaOp(mirror, 'tool_call_add', { block_idx: 0, tool_call: { id: 't1' } })).toBe(
			true
		);
		expect(applyDeltaOp(mirror, 'tool_call_add', { block_idx: 5, tool_call: { id: 't1' } })).toBe(
			true
		);
	});

	it('reports a gap for args appended to an unknown tool call', () => {
		const mirror = makeMirror([{ type: 'tool_calls', content: [{ id: 'known' }] }]);
		expect(
			applyDeltaOp(mirror, 'tool_call_args_append', { tool_call_id: 'unknown', args_delta: '{' })
		).toBe(true);
		expect(
			applyDeltaOp(mirror, 'tool_call_args_append', { tool_call_id: 'known', args_delta: '{' })
		).toBe(false);
	});

	it('reports a gap for a reasoning_detail_merge with no reasoning block', () => {
		const mirror = makeMirror();
		expect(
			applyDeltaOp(mirror, 'reasoning_detail_merge', { detail: { type: 'text', text: 'x' } })
		).toBe(true);
	});

	it('never reports a gap for message-level and structural ops', () => {
		const mirror = makeMirror();
		expect(applyDeltaOp(mirror, 'sources', { sources: [] })).toBe(false);
		expect(applyDeltaOp(mirror, 'usage', { usage: {} })).toBe(false);
		expect(applyDeltaOp(mirror, 'selected_model_id', { model_id: 'm' })).toBe(false);
		expect(
			applyDeltaOp(mirror, 'replace', {
				block_idx: 0,
				content_blocks: [{ type: 'text', content: 'a' }]
			})
		).toBe(false);
	});
});

describe('decideSnapshotAdoption', () => {
	const base = {
		snapRun: 0,
		snapVersion: 0,
		snapTerminal: false,
		snapHasContent: true,
		mirrorRun: 0,
		mirrorVersion: 0,
		liveHasContent: false
	};

	it('ignores a stale-run snapshot entirely', () => {
		expect(
			decideSnapshotAdoption({ ...base, snapRun: 100, mirrorRun: 200, snapVersion: 999 })
		).toBe('ignore');
	});

	it('adopts a newer-run snapshot even when empty (a retry legitimately blanks)', () => {
		expect(
			decideSnapshotAdoption({
				...base,
				snapRun: 200,
				mirrorRun: 100,
				snapHasContent: false,
				liveHasContent: true
			})
		).toBe('adopt');
	});

	it('NEVER wipes real content with an empty same-run snapshot — the "answer disappeared at done" wipe', () => {
		// Terminal DB-fallback race: version 0, terminal status, empty body,
		// while the tab holds the full streamed answer.
		expect(
			decideSnapshotAdoption({
				...base,
				snapVersion: 0,
				snapTerminal: true,
				snapHasContent: false,
				mirrorVersion: 0,
				liveHasContent: true
			})
		).toBe('keep');
		// Same guard even under heal (terminal authority).
		expect(
			decideSnapshotAdoption({
				...base,
				snapVersion: 500,
				snapTerminal: true,
				snapHasContent: false,
				mirrorVersion: 400,
				liveHasContent: true,
				heal: true
			})
		).toBe('keep');
	});

	it('adopts when the snapshot covers the mirror version', () => {
		expect(decideSnapshotAdoption({ ...base, snapVersion: 10, mirrorVersion: 10 })).toBe('adopt');
		expect(decideSnapshotAdoption({ ...base, snapVersion: 11, mirrorVersion: 10 })).toBe('adopt');
	});

	it('keeps live blocks when the snapshot lags and there is no authority', () => {
		expect(
			decideSnapshotAdoption({
				...base,
				snapVersion: 5,
				mirrorVersion: 10,
				liveHasContent: true
			})
		).toBe('keep');
	});

	it('heal adopts EVEN when the (possibly corrupted) mirror is ahead — the failed-heal hole', () => {
		expect(
			decideSnapshotAdoption({
				...base,
				snapVersion: 5,
				mirrorVersion: 10,
				liveHasContent: true,
				heal: true
			})
		).toBe('adopt');
	});

	it('terminal snapshots with content adopt even at a lower version', () => {
		expect(
			decideSnapshotAdoption({
				...base,
				snapVersion: 0,
				snapTerminal: true,
				mirrorVersion: 500,
				liveHasContent: true
			})
		).toBe('adopt');
	});

	it('adopts an empty snapshot when the tab has nothing to lose', () => {
		expect(
			decideSnapshotAdoption({ ...base, snapHasContent: false, liveHasContent: false })
		).toBe('adopt');
	});
});
