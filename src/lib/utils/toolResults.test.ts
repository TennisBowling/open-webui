import { beforeEach, describe, expect, it } from 'vitest';

import {
	cleanExcerpt,
	clearToolResultParseCaches,
	getToolArgEntries,
	getToolCallSummary,
	parseGenericToolResult,
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

describe('tool call summaries', () => {
	it('describes a web_search call by its query and result count', () => {
		const summary = getToolCallSummary(
			'web_search',
			JSON.stringify({ query: 'darn tough socks' }),
			{ summary: { result_count: 10 } },
			true
		);

		expect(summary).toMatchObject({ icon: 'search', title: 'Searched', meta: '10 results' });
		expect(summary.detail).toContain('darn tough socks');
	});

	it('flattens a multi-line shell command and surfaces a non-zero exit', () => {
		const summary = getToolCallSummary(
			'bash',
			JSON.stringify({ command: 'python3 -c "\nimport sys\nsys.exit(1)\n"' }),
			'exit_code: 1\n\n--- stderr ---\nboom\n',
			true
		);

		expect(summary).toMatchObject({ icon: 'terminal', title: 'Ran', meta: 'exit 1', metaError: true });
		expect(summary.detail).toBe('python3 -c " import sys sys.exit(1) "');
	});

	it('humanizes MCP tool names and drops the alias prefix', () => {
		const summary = getToolCallSummary(
			'mcp_1234abcd_get-comments',
			JSON.stringify({ q: 'annual fee' }),
			'',
			true
		);

		expect(summary).toMatchObject({ icon: 'plug', title: 'Get comments', detail: 'annual fee' });
	});

	it('shortens workspace paths for file tools', () => {
		const summary = getToolCallSummary(
			'read',
			JSON.stringify({ path: '/workspace/src/config.py' }),
			'',
			true
		);

		expect(summary).toMatchObject({ icon: 'file', title: 'Read', detail: 'src/config.py' });
	});
});

describe('generic tool result shapes', () => {
	it('splits a shell result into exit code, stdout and stderr', () => {
		const parsed = parseGenericToolResult(
			'exit_code: 2\n\n--- stdout ---\nhello\n--- stderr ---\nnope\n'
		);

		expect(parsed).toEqual({ kind: 'shell', exitCode: 2, stdout: 'hello', stderr: 'nope' });
	});

	it('detects errors reported in the result body', () => {
		const parsed = parseGenericToolResult("Error: path escapes workspace: '/tmp/x'");

		expect(parsed).toMatchObject({ kind: 'error', message: "path escapes workspace: '/tmp/x'" });
	});

	it('pretty-prints JSON results and reports empty ones', () => {
		expect(parseGenericToolResult('{"a":1}')).toEqual({ kind: 'json', text: '{\n  "a": 1\n}' });
		expect(parseGenericToolResult('   ')).toEqual({ kind: 'empty' });
	});

	it('splits arguments into inline fields and block panes', () => {
		const entries = getToolArgEntries(JSON.stringify({ path: 'a.py', command: 'ls\n-la' }));

		expect(entries[0]).toEqual({ key: 'command', value: 'ls\n-la', kind: 'block' });
		expect(entries[1]).toEqual({ key: 'path', value: 'a.py', kind: 'inline' });
	});
});

describe('excerpt cleanup', () => {
	it('drops scrape noise, repeated runs and the row title', () => {
		const snippet = [
			'FREE RETURNS 90 Day Returns & Exchanges',
			"# Men's Hiker Micro Crew Midweight Hiking Socks",
			'$26.00',
			'26.00 USD Cushion / Charcoal / XL',
			'Cushion / Black',
			'Materials: 61% Merino Wool, 36% Nylon, 3% Lycra Spandex 61% Merino Wool, 36% Nylon, 3% Lycra Spandex 61% Merino Wool, 36% Nylon, 3% Lycra Spandex'
		].join('\n[...]\n');

		const excerpt = cleanExcerpt(snippet, 320, "Men's Hiker Micro Crew Midweight Hiking Socks");

		expect(excerpt).toBe(
			'FREE RETURNS 90 Day Returns & Exchanges · Materials: 61% Merino Wool, 36% Nylon, 3% Lycra Spandex'
		);
	});

	it('unwraps markdown links and falls back when nothing reads as prose', () => {
		expect(cleanExcerpt('Read it on [OneFootball](https://onefootball.com/x) today please')).toBe(
			'Read it on OneFootball today please'
		);
		expect(cleanExcerpt('26.0 / XL')).toBe('26.0 / XL');
	});
});
