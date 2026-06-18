import { describe, expect, it } from 'vitest';

import { computeAgenticRenderItems } from './agenticGroups';

const text = (content = '') => ({ type: 'text', content });
const reasoning = (content = 'thinking') => ({ type: 'reasoning', content });
const toolCalls = () => ({ type: 'tool_calls', content: [{ id: 'c1' }] });
const steer = (content = 'focus on tests') => ({ type: 'user_steer', content });

describe('computeAgenticRenderItems', () => {
	it('returns nothing for empty input', () => {
		expect(computeAgenticRenderItems([], true)).toEqual([]);
		expect(computeAgenticRenderItems(null as any, true)).toEqual([]);
	});

	it('renders a single text block standalone', () => {
		expect(computeAgenticRenderItems([text('hi')], true)).toEqual([{ kind: 'block', index: 0 }]);
	});

	it('disabled -> every block standalone in order (legacy layout)', () => {
		const blocks = [reasoning(), toolCalls(), text('final')];
		expect(computeAgenticRenderItems(blocks, false)).toEqual([
			{ kind: 'block', index: 0 },
			{ kind: 'block', index: 1 },
			{ kind: 'block', index: 2 }
		]);
	});

	it('bundles a reasoning+tool burst and leaves the final answer inline', () => {
		// reasoning, empty placeholder, tool_calls, empty placeholder, final text
		const blocks = [reasoning(), text(''), toolCalls(), text(''), text('final answer')];
		expect(computeAgenticRenderItems(blocks, true)).toEqual([
			{ kind: 'group', indices: [0, 1, 2, 3] },
			{ kind: 'block', index: 4 }
		]);
	});

	it('splits into two groups when commentary appears between bursts', () => {
		const blocks = [
			reasoning(),
			toolCalls(),
			text(''),
			text('Now let me verify'), // commentary -> standalone, ends burst
			reasoning(),
			toolCalls(),
			text('done')
		];
		expect(computeAgenticRenderItems(blocks, true)).toEqual([
			{ kind: 'group', indices: [0, 1, 2] },
			{ kind: 'block', index: 3 },
			{ kind: 'group', indices: [4, 5] },
			{ kind: 'block', index: 6 }
		]);
	});

	it('renders leading commentary inline, then groups the work', () => {
		const blocks = [text('let me look into this'), reasoning(), toolCalls()];
		expect(computeAgenticRenderItems(blocks, true)).toEqual([
			{ kind: 'block', index: 0 },
			{ kind: 'group', indices: [1, 2] }
		]);
	});

	it('does NOT group pure reasoning (no tool calls) — stays a normal collapsible', () => {
		// reasoning model: think, then answer, no tools.
		const blocks = [reasoning(), text('the answer')];
		expect(computeAgenticRenderItems(blocks, true)).toEqual([
			{ kind: 'block', index: 0 },
			{ kind: 'block', index: 1 }
		]);
	});

	it('absorbs interleaved reasoning once a tool call is present', () => {
		const blocks = [reasoning(), toolCalls(), reasoning(), toolCalls(), text('final')];
		expect(computeAgenticRenderItems(blocks, true)).toEqual([
			{ kind: 'group', indices: [0, 1, 2, 3] },
			{ kind: 'block', index: 4 }
		]);
	});

	it('handles a trailing open burst (still streaming, no final text yet)', () => {
		const blocks = [reasoning(), toolCalls(), text('')];
		expect(computeAgenticRenderItems(blocks, true)).toEqual([{ kind: 'group', indices: [0, 1, 2] }]);
	});

	it('a user_steer block breaks the agentic burst and renders standalone', () => {
		// work burst, then the user steers mid-task, then work resumes: the steer
		// must NOT be swallowed into the Working group — it renders inline as its
		// own user bubble, ending the first burst and starting a new one.
		const blocks = [
			reasoning(),
			toolCalls(),
			text(''),
			steer('actually, focus on the tests'),
			text(''),
			toolCalls(),
			text('done')
		];
		expect(computeAgenticRenderItems(blocks, true)).toEqual([
			{ kind: 'group', indices: [0, 1, 2] },
			{ kind: 'block', index: 3 },
			{ kind: 'group', indices: [4, 5] },
			{ kind: 'block', index: 6 }
		]);
	});
});
