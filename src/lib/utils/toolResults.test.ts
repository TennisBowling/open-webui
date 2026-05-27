import { describe, expect, it } from 'vitest';

import {
	hydrateToolResultsInBlocks,
	mergeToolResultEntries,
	parseWebFetchResult,
	parseWebSearchResult
} from './toolResults';

const searchMarkdown = `## Search Results for: stream v2

Found 1 result.

### Result 1
**Title:** Stream v2 docs
**URL:** https://example.com/stream-v2
**Snippet:** Tool result bodies arrive separately.
`;

const fetchMarkdown = `## Fetched Content
Retrieved content from 1 URL(s).

### Page 1: Stream v2 docs
**URL:** https://example.com/stream-v2
**Published:** 
**Author:** 
**Description:** Test page

**Content:**

Tool result bodies arrive separately.

---
`;

describe('stream-v2 tool result hydration', () => {
	it('keeps an existing full result when a slim placeholder arrives later', () => {
		const merged = mergeToolResultEntries([{ tool_call_id: 'call_1' }], undefined, [
			{ tool_call_id: 'call_1', content: fetchMarkdown }
		]);

		expect(merged).toHaveLength(1);
		expect(merged[0].content).toBe(fetchMarkdown);
		expect(parseWebFetchResult(merged[0].content).ok).toBe(true);
	});

	it('hydrates slim web_search placeholders from the tool result cache', () => {
		const [block] = hydrateToolResultsInBlocks(
			[
				{
					type: 'tool_calls',
					content: [{ id: 'call_1', function: { name: 'web_search', arguments: '{}' } }],
					results: [{ tool_call_id: 'call_1' }]
				}
			],
			new Map([['call_1', { tool_call_id: 'call_1', content: searchMarkdown }]])
		) as any[];

		expect(block.results[0].content).toBe(searchMarkdown);
		expect(parseWebSearchResult(block.results[0].content).ok).toBe(true);
	});

	it('adds cached results to a tool_calls block that arrived before its result stub', () => {
		const [block] = hydrateToolResultsInBlocks(
			[
				{
					type: 'tool_calls',
					content: [{ id: 'call_1', function: { name: 'web_fetch', arguments: '{}' } }]
				}
			],
			{ call_1: { tool_call_id: 'call_1', content: fetchMarkdown } }
		) as any[];

		expect(block.results[0].tool_call_id).toBe('call_1');
		expect(block.results[0].content).toBe(fetchMarkdown);
		expect(parseWebFetchResult(block.results[0].content).ok).toBe(true);
	});
});
