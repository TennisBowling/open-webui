import { beforeEach, describe, expect, it } from 'vitest';

import {
	clearToolResultParseCaches,
	getToolResultParseCacheStats,
	hydrateToolResultsInBlocks,
	mergeToolResultEntries,
	parseBrowserResult,
	parseWebFetchResult,
	parseWebSearchResult
} from './toolResults';

const searchMarkdown = `## Search Results for: stream v2.1

Found 1 result.

### Result 1
**Title:** Stream v2.1 docs
**URL:** https://example.com/stream-v2.1
**Snippet:** Tool result bodies arrive separately.
`;

const fetchMarkdown = `## Fetched Content
Retrieved content from 1 URL(s).

### Page 1: Stream v2.1 docs
**URL:** https://example.com/stream-v2.1
**Published:** 
**Author:** 
**Description:** Test page

**Content:**

Tool result bodies arrive separately.

---
`;

describe('stream-v2.1 tool result hydration', () => {
	beforeEach(() => {
		clearToolResultParseCaches();
	});

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

	it('reuses parsed web_fetch results across repeated parses', () => {
		const first = parseWebFetchResult(fetchMarkdown);
		const second = parseWebFetchResult(fetchMarkdown);

		expect(second).toBe(first);
		expect(getToolResultParseCacheStats().webFetch.entries).toBe(1);
	});

	it('keeps parser caches bounded by entry count', () => {
		for (let i = 0; i < 20; i += 1) {
			parseWebSearchResult(`${searchMarkdown}\n### Result ${i + 2}\n**Title:** ${i}\n`);
		}

		expect(getToolResultParseCacheStats().webSearch.entries).toBeLessThanOrEqual(16);
	});

	it('separates browser parse cache entries by args fallback URL', () => {
		const raw = 'Title: Example\n\nSnapshot text';
		const first = parseBrowserResult(raw, { url: 'https://a.example/' });
		const second = parseBrowserResult(raw, { url: 'https://b.example/' });

		expect(first.url).toBe('https://a.example/');
		expect(second.url).toBe('https://b.example/');
		expect(second).not.toBe(first);
		expect(getToolResultParseCacheStats().browser.entries).toBe(2);
	});
});
