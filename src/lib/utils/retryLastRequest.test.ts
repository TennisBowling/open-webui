import { describe, expect, it } from 'vitest';

import {
	getStructuredRetryLastRequestContext,
	getRewindContext,
	getRewindCutIndices,
	getSubagentToolCallCutIndex,
	canBatchSubagentToolCallCuts,
	countCompletedStructuredToolCalls,
	isCompletedToolCallsBlock
} from './retryLastRequest';

describe('getStructuredRetryLastRequestContext', () => {
	it('keeps all rounds through the last completed tool call and drops the final answer', () => {
		const message = {
			content_blocks: [
				{ type: 'reasoning', content: 'thinking before first tool' },
				{
					type: 'tool_calls',
					content: [{ id: 'call_1', function: { name: 'web_search', arguments: '{}' } }],
					results: [{ tool_call_id: 'call_1', content: 'search results' }]
				},
				{ type: 'reasoning', content: 'thinking before second tool' },
				{
					type: 'tool_calls',
					content: [{ id: 'call_2', function: { name: 'web_fetch', arguments: '{}' } }],
					results: [{ tool_call_id: 'call_2', content: 'fetched page' }]
				},
				{ type: 'reasoning', content: 'final answer thinking' },
				{ type: 'text', content: 'final answer' }
			],
			reasoning_details_per_round: [['r1'], ['r2'], ['final reasoning']]
		};

		const context = getStructuredRetryLastRequestContext(message);

		expect(context?.content_blocks).toEqual(message.content_blocks.slice(0, 4));
		expect(context?.reasoning_details_per_round).toEqual([['r1'], ['r2']]);
		expect(context?.content).toBe('');
	});

	it('requires result bodies or durable refs instead of stream-v2.1 slim placeholders', () => {
		const context = getStructuredRetryLastRequestContext({
			content_blocks: [
				{
					type: 'tool_calls',
					content: [{ id: 'call_1', function: { name: 'web_fetch', arguments: '{}' } }],
					results: [{ tool_call_id: 'call_1' }]
				}
			]
		});

		expect(context).toBeNull();
	});

	it('accepts durable lazy result refs as completed tool rounds', () => {
		const context = getStructuredRetryLastRequestContext({
			content_blocks: [
				{
					type: 'tool_calls',
					content: [{ id: 'call_1', function: { name: 'web_fetch', arguments: '{}' } }],
					results: [{ tool_call_id: 'call_1', result_ref: 'call_1', result_lazy: true }]
				}
			]
		});

		expect(context?.content_blocks).toHaveLength(1);
	});

	it('returns null when there is no completed tool round', () => {
		expect(
			getStructuredRetryLastRequestContext({ content_blocks: [{ type: 'text', content: 'hi' }] })
		).toBeNull();
	});
});

describe('getRewindContext', () => {
	const sampleMessage = () => ({
		content_blocks: [
			{ type: 'reasoning', content: 'thinking before first tool' },
			{
				type: 'tool_calls',
				content: [{ id: 'call_1', function: { name: 'web_search', arguments: '{}' } }],
				results: [{ tool_call_id: 'call_1', content: 'search results' }]
			},
			{ type: 'reasoning', content: 'thinking before second tool' },
			{
				type: 'tool_calls',
				content: [{ id: 'call_2', function: { name: 'web_fetch', arguments: '{}' } }],
				results: [{ tool_call_id: 'call_2', content: 'fetched page' }]
			},
			{ type: 'text', content: 'final answer' }
		],
		reasoning_details_per_round: [['r1'], ['r2'], ['final reasoning']]
	});

	it('keeps blocks before the cut, injects the steer, and parks a trailing text block', () => {
		const context = getRewindContext(sampleMessage(), 2, 'go fix bar.py instead');

		expect(context?.content).toBe('');
		expect(context?.content_blocks).toEqual([
			{ type: 'reasoning', content: 'thinking before first tool' },
			{
				type: 'tool_calls',
				content: [{ id: 'call_1', function: { name: 'web_search', arguments: '{}' } }],
				results: [{ tool_call_id: 'call_1', content: 'search results' }]
			},
			{ type: 'user_steer', content: 'go fix bar.py instead' },
			{ type: 'text', content: '' }
		]);
	});

	it('trims reasoning_details_per_round to the number of retained tool rounds', () => {
		// cut=4 keeps both tool_calls blocks (2 rounds) but drops the final answer.
		const context = getRewindContext(sampleMessage(), 4, 'keep going');
		expect(context?.reasoning_details_per_round).toEqual([['r1'], ['r2']]);

		// cut=2 keeps only the first tool round.
		const earlier = getRewindContext(sampleMessage(), 2, 'redirect');
		expect(earlier?.reasoning_details_per_round).toEqual([['r1']]);
	});

	it('omits the user_steer block when the steer text is empty/whitespace (pure rewind)', () => {
		const blank = getRewindContext(sampleMessage(), 2, '   ');
		expect(blank?.content_blocks).toEqual([
			{ type: 'reasoning', content: 'thinking before first tool' },
			{
				type: 'tool_calls',
				content: [{ id: 'call_1', function: { name: 'web_search', arguments: '{}' } }],
				results: [{ tool_call_id: 'call_1', content: 'search results' }]
			},
			{ type: 'text', content: '' }
		]);
		expect(blank?.content_blocks.some((b) => b.type === 'user_steer')).toBe(false);
		// Always ends on the empty stream-target text block.
		expect(blank?.content_blocks.at(-1)).toEqual({ type: 'text', content: '' });
	});

	it('trims surrounding whitespace from the steer text', () => {
		const context = getRewindContext(sampleMessage(), 2, '  look at bar.py \n');
		expect(context?.content_blocks.find((b) => b.type === 'user_steer')).toEqual({
			type: 'user_steer',
			content: 'look at bar.py'
		});
	});

	it('clamps an out-of-range cut to the full block list', () => {
		const context = getRewindContext(sampleMessage(), 999, 'after everything');
		// All 5 original blocks retained + steer + trailing text.
		expect(context?.content_blocks).toHaveLength(7);
		// Two tool rounds retained → trimmed to two per-round entries (the final
		// answer emission's reasoning is dropped, matching getStructuredRetry…).
		expect(context?.reasoning_details_per_round).toEqual([['r1'], ['r2']]);
	});

	it('supports a cut at index 0 (keep nothing, redirect from the start)', () => {
		const context = getRewindContext(sampleMessage(), 0, 'start over with this');
		expect(context?.content_blocks).toEqual([
			{ type: 'user_steer', content: 'start over with this' },
			{ type: 'text', content: '' }
		]);
		expect(context?.reasoning_details_per_round).toEqual([]);
	});

	it('does not mutate the source message', () => {
		const message = sampleMessage();
		const snapshot = JSON.parse(JSON.stringify(message));
		getRewindContext(message, 2, 'redirect');
		expect(message).toEqual(snapshot);
	});

	it('returns null when there are no content_blocks', () => {
		expect(getRewindContext({ content_blocks: [] }, 1, 'x')).toBeNull();
		expect(getRewindContext({}, 1, 'x')).toBeNull();
	});

	it('never clones null/undefined block entries into the persisted prefix', () => {
		// A stream mirror that missed early block_opens used to leave array
		// holes that serialize as JSON null (production chat 625192d9); the
		// rewind sibling must persist a dense, well-formed prefix regardless.
		const poisoned = {
			content_blocks: [
				null,
				undefined,
				{ type: 'reasoning', content: 'round 2 thinking' },
				{
					type: 'tool_calls',
					content: [{ id: 'call_2', function: { name: 'web_fetch', arguments: '{}' } }],
					results: [{ tool_call_id: 'call_2', content: 'fetched page' }]
				}
			]
		};
		// Cut is expressed in the RAW index space (4 = keep everything shown).
		const context = getRewindContext(poisoned, 4, 'keep going');
		expect(context?.content_blocks).toEqual([
			{ type: 'reasoning', content: 'round 2 thinking' },
			{
				type: 'tool_calls',
				content: [{ id: 'call_2', function: { name: 'web_fetch', arguments: '{}' } }],
				results: [{ tool_call_id: 'call_2', content: 'fetched page' }]
			},
			{ type: 'user_steer', content: 'keep going' },
			{ type: 'text', content: '' }
		]);
	});

	it('interprets the cut in the raw index space when nulls precede it', () => {
		// Filtering must happen AFTER slicing — otherwise blocks beyond the
		// UI's cut boundary would leak into the kept prefix.
		const poisoned = {
			content_blocks: [
				null,
				{ type: 'reasoning', content: 'kept' },
				{ type: 'text', content: 'dropped by the cut' }
			]
		};
		const context = getRewindContext(poisoned, 2, '');
		expect(context?.content_blocks).toEqual([
			{ type: 'reasoning', content: 'kept' },
			{ type: 'text', content: '' }
		]);
	});
});

describe('getRewindCutIndices ("between requests" boundaries)', () => {
	const tc = (id: string, calls: string[], complete = true) => ({
		type: 'tool_calls',
		content: calls.map((c) => ({ id: c, function: { name: 'f', arguments: '{}' } })),
		results: complete ? calls.map((c) => ({ tool_call_id: c, content: 'ok' })) : []
	});

	it('puts a cut ONLY after each completed tool round, never mid-round', () => {
		// reasoning(0) tool_calls(1) reasoning(2) tool_calls(3) text(4)
		const blocks = [
			{ type: 'reasoning', content: 'a' },
			tc('call_1', ['call_1']),
			{ type: 'reasoning', content: 'b' },
			tc('call_2', ['call_2']),
			{ type: 'text', content: 'answer' }
		];
		// Cuts after idx1 (=>2) and idx3 (=>4). NOT 1 or 3 (that would cut between a
		// reasoning block and its own tool calls — mid-round).
		expect([...getRewindCutIndices(blocks)].sort((a, b) => a - b)).toEqual([2, 4]);
	});

	it('treats a parallel tool batch as ONE indivisible round (cut only after the whole block)', () => {
		// One block with three parallel calls.
		const blocks = [tc('p', ['c1', 'c2', 'c3']), { type: 'text', content: 'done' }];
		// The only cut is after the whole block (index 1); there is no cut "between"
		// the parallel calls because they live in a single block.
		expect([...getRewindCutIndices(blocks)]).toEqual([1]);
	});

	it('offers no cut after a parallel batch until ALL of its calls have results', () => {
		const incomplete = {
			type: 'tool_calls',
			content: [
				{ id: 'c1', function: { name: 'f', arguments: '{}' } },
				{ id: 'c2', function: { name: 'f', arguments: '{}' } }
			],
			results: [{ tool_call_id: 'c1', content: 'ok' }] // c2 still running
		};
		expect(getRewindCutIndices([incomplete, { type: 'text', content: '' }]).size).toBe(0);
		expect(isCompletedToolCallsBlock(incomplete)).toBe(false);
	});

	it('counts lazy result refs as complete (results offloaded out-of-line)', () => {
		const lazy = {
			type: 'tool_calls',
			content: [{ id: 'c1', function: { name: 'f', arguments: '{}' } }],
			results: [{ tool_call_id: 'c1', result_ref: 'c1', result_lazy: true }]
		};
		expect([...getRewindCutIndices([lazy])]).toEqual([1]);
	});

	it('has no cuts in a tool-free message', () => {
		const blocks = [
			{ type: 'reasoning', content: 'thinking' },
			{ type: 'text', content: 'just an answer' }
		];
		expect(getRewindCutIndices(blocks).size).toBe(0);
	});
});

describe('getSubagentToolCallCutIndex', () => {
	const subagentMessage = () => ({
		content_blocks: [
			{ type: 'reasoning', content: 'thinking' },
			{
				type: 'tool_calls',
				content: [{ id: 'call_sa1', function: { name: 'subagent_launch', arguments: '{}' } }],
				results: [{ tool_call_id: 'call_sa1', content: 'answer', subagent_id: 'sa1' }]
			},
			{ type: 'text', content: 'parent answer after the subagent' },
			{
				type: 'tool_calls',
				content: [{ id: 'call_sa2', function: { name: 'subagent_continue', arguments: '{}' } }],
				results: [{ tool_call_id: 'call_sa2', content: 'more', subagent_id: 'sa1' }]
			}
		]
	});

	it('returns the index right after the block matched by tool_call_id', () => {
		expect(getSubagentToolCallCutIndex(subagentMessage(), { tool_call_id: 'call_sa1' })).toBe(2);
		expect(getSubagentToolCallCutIndex(subagentMessage(), { tool_call_id: 'call_sa2' })).toBe(4);
	});

	it('falls back to matching a block by the run subagent_id when no tool_call_id', () => {
		// First block whose results carry sa1 → index 1, cut = 2.
		expect(getSubagentToolCallCutIndex(subagentMessage(), { subagent_id: 'sa1' })).toBe(2);
	});

	it('prefers an exact tool_call_id match over the subagent_id fallback', () => {
		expect(
			getSubagentToolCallCutIndex(subagentMessage(), {
				tool_call_id: 'call_sa2',
				subagent_id: 'sa1'
			})
		).toBe(4);
	});

	it('matches a continuation entry whose run carries chat_id but not subagent_id', () => {
		expect(getSubagentToolCallCutIndex(subagentMessage(), { chat_id: 'sa1' })).toBe(2);
	});

	it('returns -1 when the subagent tool call is not present', () => {
		expect(getSubagentToolCallCutIndex(subagentMessage(), { tool_call_id: 'nope' })).toBe(-1);
		expect(getSubagentToolCallCutIndex(subagentMessage(), { subagent_id: 'other' })).toBe(-1);
		expect(getSubagentToolCallCutIndex({ content_blocks: [] }, { tool_call_id: 'x' })).toBe(-1);
		expect(getSubagentToolCallCutIndex(null, { tool_call_id: 'x' })).toBe(-1);
	});

	it('produces a cut that getRewindContext keeps the subagent block within', () => {
		const message = subagentMessage();
		const cut = getSubagentToolCallCutIndex(message, { tool_call_id: 'call_sa1' });
		const ctx = getRewindContext(message, cut, '');
		// Kept prefix = reasoning + the subagent tool_calls block, then trailing text('').
		expect(ctx?.content_blocks.length).toBe(3);
		expect(ctx?.content_blocks[1].type).toBe('tool_calls');
		expect(ctx?.content_blocks[1].content[0].id).toBe('call_sa1');
		expect(ctx?.content_blocks[2]).toEqual({ type: 'text', content: '' });
	});

	// A STRANDED subagent (e.g. one that stuck at 'running' after a task death): its
	// LAUNCH call is present in content_blocks but it has NO result row, and the live
	// card may have lost its tool_call_id — so a redo passes only subagent_id/entry_key.
	// The locator must recover the tool_call_id from subagent_runs (which records it)
	// to match the always-present call, else redo fails "couldn't locate its tool call".
	const strandedMessage = () => ({
		content_blocks: [
			{ type: 'reasoning', content: 'thinking' },
			{
				type: 'tool_calls',
				content: [
					{ id: 'call_done', function: { name: 'subagent_launch', arguments: '{}' } },
					{ id: 'call_stranded', function: { name: 'subagent_launch', arguments: '{}' } }
				],
				// Only the DONE sibling has a result; the stranded one never produced one.
				results: [{ tool_call_id: 'call_done', content: 'answer', subagent_id: 'sa_done' }]
			},
			{ type: 'text', content: 'parent answer' }
		],
		subagent_runs: {
			sa_done: { subagent_id: 'sa_done', tool_call_id: 'call_done', status: 'done' },
			sa_stranded: {
				entry_key: 'sa_stranded',
				subagent_id: 'sa_stranded',
				tool_call_id: 'call_stranded',
				status: 'running'
			}
		}
	});

	it('recovers tool_call_id from subagent_runs for a stranded subagent with no result row (by subagent_id)', () => {
		// Empty tool_call_id + no result row => without recovery this returns -1.
		expect(
			getSubagentToolCallCutIndex(strandedMessage(), {
				tool_call_id: '',
				subagent_id: 'sa_stranded'
			})
		).toBe(2);
	});

	it('recovers tool_call_id from subagent_runs by entry_key when subagent_id is also missing', () => {
		expect(getSubagentToolCallCutIndex(strandedMessage(), { entry_key: 'sa_stranded' })).toBe(2);
	});

	it('still returns -1 for a stranded subagent absent from subagent_runs and results', () => {
		expect(
			getSubagentToolCallCutIndex(strandedMessage(), {
				tool_call_id: '',
				subagent_id: 'sa_ghost'
			})
		).toBe(-1);
	});
});

describe('canBatchSubagentToolCallCuts', () => {
	const subagentBlock = (id: string, sid: string) => ({
		type: 'tool_calls',
		content: [{ id, function: { name: 'subagent_launch', arguments: '{}' } }],
		results: [{ tool_call_id: id, content: 'answer', subagent_id: sid }]
	});

	it('allows same-block cuts', () => {
		const message = { content_blocks: [subagentBlock('a', 'sa1')] };
		expect(canBatchSubagentToolCallCuts(message, [1, 1])).toBe(true);
	});

	it('allows separated pure subagent fanout blocks with only empty text between', () => {
		const message = {
			content_blocks: [
				subagentBlock('a', 'sa1'),
				{ type: 'text', content: '' },
				subagentBlock('b', 'sa2')
			]
		};

		expect(canBatchSubagentToolCallCuts(message, [1, 3])).toBe(true);
	});

	it('blocks batching across parent text', () => {
		const message = {
			content_blocks: [
				subagentBlock('a', 'sa1'),
				{ type: 'text', content: 'parent used the first result' },
				subagentBlock('b', 'sa2')
			]
		};

		expect(canBatchSubagentToolCallCuts(message, [1, 3])).toBe(false);
	});

	it('blocks batching across non-subagent tool calls', () => {
		const message = {
			content_blocks: [
				subagentBlock('a', 'sa1'),
				{
					type: 'tool_calls',
					content: [{ id: 'search', function: { name: 'web_search', arguments: '{}' } }],
					results: [{ tool_call_id: 'search', content: 'results' }]
				},
				subagentBlock('b', 'sa2')
			]
		};

		expect(canBatchSubagentToolCallCuts(message, [1, 3])).toBe(false);
	});
});

describe('countCompletedStructuredToolCalls (auto-retry forward-progress metric)', () => {
	it('counts per-call completion across blocks, including partial parallel rounds', () => {
		const message = {
			content_blocks: [
				{ type: 'reasoning', content: 'thinking' },
				{
					type: 'tool_calls',
					content: [
						{ id: 'c1', function: { name: 'web_search', arguments: '{}' } },
						{ id: 'c2', function: { name: 'web_fetch', arguments: '{}' } }
					],
					// c1 completed inline, c2 completed via a durable lazy ref.
					results: [
						{ tool_call_id: 'c1', content: 'results' },
						{ tool_call_id: 'c2', result_ref: 'c2', result_lazy: true }
					]
				},
				{
					type: 'tool_calls',
					content: [
						{ id: 'c3', function: { name: 'web_fetch', arguments: '{}' } },
						{ id: 'c4', function: { name: 'web_fetch', arguments: '{}' } }
					],
					// c3 completed; c4 still pending (no result row) — the round the
					// model errored inside. Progress must still count 3, not 0.
					results: [{ tool_call_id: 'c3', content: 'page' }]
				},
				{ type: 'text', content: '' }
			]
		};
		expect(countCompletedStructuredToolCalls(message)).toBe(3);
	});

	it('ignores slim stream placeholders without bodies', () => {
		const message = {
			content_blocks: [
				{
					type: 'tool_calls',
					content: [{ id: 'c1', function: { name: 'web_search', arguments: '{}' } }],
					// A v2.1 slim placeholder (no content, no result_ref) is not durable
					// progress — retrying could not replay it.
					results: [{ tool_call_id: 'c1' }]
				}
			]
		};
		expect(countCompletedStructuredToolCalls(message)).toBe(0);
	});

	it('returns 0 for legacy messages without content_blocks (caller falls back to the content parser)', () => {
		expect(countCompletedStructuredToolCalls({ content: '<details type="tool_calls">…</details>' })).toBe(0);
		expect(countCompletedStructuredToolCalls({})).toBe(0);
		expect(countCompletedStructuredToolCalls(null)).toBe(0);
	});

	it('tolerates null block entries', () => {
		const message = {
			content_blocks: [
				null,
				{
					type: 'tool_calls',
					content: [{ id: 'c1', function: { name: 'x', arguments: '{}' } }],
					results: [{ tool_call_id: 'c1', content: 'ok' }]
				}
			]
		};
		expect(countCompletedStructuredToolCalls(message)).toBe(1);
	});
});

describe('reactive-proxy inputs', () => {
	// Chat.svelte holds `history` as Svelte 5 `$state`, so every message — and
	// every content block inside it — reaches these helpers as a deep reactive
	// PROXY. `structuredClone()` throws DataCloneError on a proxy, which made
	// every rewind and every retry-from-last-request die before it did anything
	// (silently: the call site never awaited the rejection). The clone used here
	// must therefore be structural, not structuredClone.
	const asStateLikeProxy = <T extends object>(value: T): T =>
		new Proxy(value, {
			get(target, key, receiver) {
				const entry = Reflect.get(target, key, receiver);
				return entry && typeof entry === 'object' ? asStateLikeProxy(entry) : entry;
			}
		});

	const message = () =>
		asStateLikeProxy({
			content_blocks: [
				{ type: 'reasoning', content: 'thinking' },
				{
					type: 'tool_calls',
					content: [{ id: 'call_1', function: { name: 'search', arguments: '{}' } }],
					results: [{ tool_call_id: 'call_1', content: 'result' }]
				},
				{ type: 'text', content: 'answer' }
			],
			reasoning_details_per_round: [[{ type: 'reasoning.text', text: 'r1' }]]
		});

	it('structuredClone would reject these inputs (guards the regression)', () => {
		expect(() => structuredClone(message().content_blocks)).toThrow();
	});

	it('getRewindContext builds a branch from proxied blocks', () => {
		const context = getRewindContext(message(), 2, 'go again');
		expect(context).not.toBeNull();
		expect(context?.content_blocks.map((block: any) => block.type)).toEqual([
			'reasoning',
			'tool_calls',
			'user_steer',
			'text'
		]);
		expect(context?.reasoning_details_per_round).toEqual([[{ type: 'reasoning.text', text: 'r1' }]]);
	});

	it('getStructuredRetryLastRequestContext works on proxied blocks', () => {
		const context = getStructuredRetryLastRequestContext(message());
		expect(context?.content_blocks.map((block: any) => block.type)).toEqual([
			'reasoning',
			'tool_calls'
		]);
	});

	it('returns plain, detached objects (never bound to the source proxy)', () => {
		const source = message();
		const context = getRewindContext(source, 3, '');
		const cloned: any = context?.content_blocks[1];
		cloned.results[0].content = 'mutated';
		expect((source.content_blocks[1] as any).results[0].content).toBe('result');
		expect(() => structuredClone(context?.content_blocks)).not.toThrow();
	});
});
