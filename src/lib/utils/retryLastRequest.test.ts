import { describe, expect, it } from 'vitest';

import { getStructuredRetryLastRequestContext } from './retryLastRequest';

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

	it('requires result bodies instead of stream-v2 slim placeholders', () => {
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

	it('returns null when there is no completed tool round', () => {
		expect(getStructuredRetryLastRequestContext({ content_blocks: [{ type: 'text', content: 'hi' }] })).toBeNull();
	});
});
