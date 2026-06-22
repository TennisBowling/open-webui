export type ToolName = 'web_search' | 'web_fetch' | string;

export type DecodedToolArguments =
	| Record<string, unknown>
	| unknown[]
	| string
	| number
	| boolean
	| null;

export interface ToolCallSummary {
	kind: 'web_search' | 'web_fetch' | 'browser' | 'generic';
	title: string;
	subtitle?: string;
	badge?: string;
}

export interface WebSearchItem {
	index: number;
	title: string;
	url: string;
	domain: string;
	snippet: string;
}

export interface ParsedWebSearchResult {
	ok: boolean;
	query: string;
	declaredCount: number | null;
	results: WebSearchItem[];
	raw: string;
}

export interface WebFetchPage {
	index: number;
	title: string;
	url: string;
	domain: string;
	published: string;
	author: string;
	description: string;
	content: string;
	characters: number;
}

export interface ParsedWebFetchResult {
	ok: boolean;
	declaredCount: number | null;
	pages: WebFetchPage[];
	totalCharacters: number;
	raw: string;
}

export interface ParsedBrowserResult {
	// Page URL the action happened on. Prefer the result text's `URL:` line,
	// falling back to the `url` argument (navigate).
	url: string;
	domain: string;
	// Page title from the result text's `Title:` line, when present.
	title: string;
	// The remaining body — the accessibility snapshot tree
	// (`[ref=eN] role "name"` lines) or any other returned text.
	snapshot: string;
	raw: string;
}

export type ToolResultLookup = Map<string, unknown> | Record<string, unknown> | null | undefined;

export interface ToolResultEntry {
	tool_call_id: string;
	content?: unknown;
	files?: unknown[];
	embeds?: unknown[];
	subagent_id?: string;
	error?: boolean;
	error_reason?: string;
	notice?: string;
	[key: string]: unknown;
}

const JSON_START_CHARS = new Set([
	'{',
	'[',
	'"',
	'-',
	'0',
	'1',
	'2',
	'3',
	'4',
	'5',
	'6',
	'7',
	'8',
	'9'
]);

const looksLikeJSON = (value: string) => {
	const trimmed = value.trim();
	if (!trimmed) return false;
	const first = trimmed[0];
	return JSON_START_CHARS.has(first) || first === 't' || first === 'f' || first === 'n';
};

const decodeHtmlAttributeEntities = (value: string) => {
	if (!value.includes('&')) return value;
	return value
		.replace(/&quot;/g, '"')
		.replace(/&#34;/g, '"')
		.replace(/&#x27;/gi, "'")
		.replace(/&#39;/g, "'")
		.replace(/&lt;/g, '<')
		.replace(/&gt;/g, '>')
		.replace(/&amp;/g, '&');
};

export const decodePossiblyNestedJSON = (input: unknown, maxDepth = 4): unknown => {
	let value = input;

	for (let i = 0; i < maxDepth; i += 1) {
		if (typeof value !== 'string') break;

		const decoded = decodeHtmlAttributeEntities(value);
		const trimmed = decoded.trim();
		if (!looksLikeJSON(trimmed)) return decoded;

		try {
			value = JSON.parse(trimmed);
		} catch {
			return decoded;
		}
	}

	return value;
};

export const decodeToolArguments = (raw: unknown): DecodedToolArguments => {
	return decodePossiblyNestedJSON(raw) as DecodedToolArguments;
};

export const getToolArgumentsObject = (raw: unknown): Record<string, unknown> => {
	const decoded = decodeToolArguments(raw);
	return decoded && typeof decoded === 'object' && !Array.isArray(decoded)
		? (decoded as Record<string, unknown>)
		: {};
};

export const decodeToolResultText = (raw: unknown): string => {
	const decoded = decodePossiblyNestedJSON(raw);

	if (decoded == null) return '';
	if (typeof decoded === 'string') return decoded;
	if (typeof decoded === 'object') return JSON.stringify(decoded, null, 2);
	return String(decoded);
};

export const formatToolValue = (raw: unknown): string => {
	const decoded = decodePossiblyNestedJSON(raw);

	if (decoded == null) return '';
	if (typeof decoded === 'object') return JSON.stringify(decoded, null, 2);
	return String(decoded);
};

export const isWebToolName = (name: unknown): name is 'web_search' | 'web_fetch' => {
	return name === 'web_search' || name === 'web_fetch';
};

// Container browser tools keep their natural `browser_*` names (they are NOT
// MCP-aliased), so the UI can recognize them directly. They render with the
// same pretty header + rich expand treatment as the web tools.
const BROWSER_TOOL_NAMES = new Set([
	'browser_navigate',
	'browser_snapshot',
	'browser_screenshot',
	'browser_click',
	'browser_type',
	'browser_press_key',
	'browser_select',
	'browser_back',
	'browser_wait'
]);

export const isBrowserToolName = (name: unknown): name is string => {
	return typeof name === 'string' && BROWSER_TOOL_NAMES.has(name);
};

// Tools that opt into the rich (pretty header + custom expand body) rendering
// in Collapsible instead of the bare generic args/result markdown dump.
export const isRichToolName = (name: unknown): boolean => {
	return isWebToolName(name) || isBrowserToolName(name);
};

const escapeRegExp = (value: string) => value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

const hasOwn = (value: object, key: string) => Object.prototype.hasOwnProperty.call(value, key);

const getToolResultId = (value: unknown) => {
	if (!value || typeof value !== 'object' || Array.isArray(value)) return '';
	const id = (value as Record<string, unknown>).tool_call_id;
	return typeof id === 'string' ? id : '';
};

const getToolResultFromLookup = (lookup: ToolResultLookup, toolCallId: string) => {
	if (!lookup || !toolCallId) return undefined;
	if (lookup instanceof Map) return lookup.get(toolCallId);
	return lookup[toolCallId];
};

export const normalizeToolResultEntry = (toolCallId: string, raw: unknown): ToolResultEntry => {
	if (raw && typeof raw === 'object' && !Array.isArray(raw)) {
		const obj = { ...(raw as Record<string, unknown>) };
		const id =
			typeof obj.tool_call_id === 'string' && obj.tool_call_id ? obj.tool_call_id : toolCallId;
		if (id) obj.tool_call_id = id;
		if (!hasOwn(obj, 'content') && hasOwn(obj, 'result')) {
			obj.content = obj.result;
		}
		return obj as ToolResultEntry;
	}

	return {
		tool_call_id: toolCallId,
		content: raw ?? ''
	};
};

const toolResultHasContent = (entry: unknown) =>
	!!entry && typeof entry === 'object' && !Array.isArray(entry) && hasOwn(entry, 'content');

export const mergeToolResultEntries = (
	incomingResults: unknown[] = [],
	knownResults?: ToolResultLookup,
	existingResults: unknown[] = []
): ToolResultEntry[] => {
	const existingById = new Map<string, unknown>();
	for (const existing of existingResults) {
		const id = getToolResultId(existing);
		if (id) existingById.set(id, existing);
	}

	return incomingResults
		.map((incomingRaw) => {
			const incomingId = getToolResultId(incomingRaw);
			const existingRaw = incomingId ? existingById.get(incomingId) : undefined;
			const knownRaw = incomingId ? getToolResultFromLookup(knownResults, incomingId) : undefined;
			const existing =
				existingRaw !== undefined ? normalizeToolResultEntry(incomingId, existingRaw) : null;
			const known = knownRaw !== undefined ? normalizeToolResultEntry(incomingId, knownRaw) : null;
			const incoming = normalizeToolResultEntry(
				incomingId || getToolResultId(known) || getToolResultId(existing),
				incomingRaw
			);
			const id = incoming.tool_call_id || known?.tool_call_id || existing?.tool_call_id || '';
			const merged: ToolResultEntry = {
				...(existing ?? {}),
				...(known ?? {}),
				...incoming,
				tool_call_id: id
			};

			// Stream-v2.1 content_blocks intentionally carry slim result placeholders
			// (`{ tool_call_id }`) while the heavy body travels via tool_call:result.
			// Never let those placeholders erase a full body that arrived earlier or
			// was supplied by the snapshot endpoint.
			if (!toolResultHasContent(incoming)) {
				if (toolResultHasContent(known)) {
					merged.content = known?.content;
				} else if (toolResultHasContent(existing)) {
					merged.content = existing?.content;
				}
			}

			return merged;
		})
		.filter((entry) => !!entry.tool_call_id);
};

export const hydrateToolResultsInBlock = (
	block: unknown,
	knownResults?: ToolResultLookup,
	previousBlock?: unknown
): unknown => {
	if (!block || typeof block !== 'object' || Array.isArray(block)) return block;
	const typedBlock = block as Record<string, unknown>;
	if (typedBlock.type !== 'tool_calls') return block;

	const incomingResults = Array.isArray(typedBlock.results) ? typedBlock.results : [];
	const previousResults =
		previousBlock && typeof previousBlock === 'object' && !Array.isArray(previousBlock)
			? Array.isArray((previousBlock as Record<string, unknown>).results)
				? ((previousBlock as Record<string, unknown>).results as unknown[])
				: []
			: [];
	const mergedResults = mergeToolResultEntries(incomingResults, knownResults, previousResults);
	const seen = new Set(mergedResults.map((result) => result.tool_call_id).filter(Boolean));

	const calls = Array.isArray(typedBlock.content) ? typedBlock.content : [];
	for (const call of calls) {
		if (!call || typeof call !== 'object' || Array.isArray(call)) continue;
		const toolCallId =
			typeof (call as Record<string, unknown>).id === 'string'
				? ((call as Record<string, unknown>).id as string)
				: typeof (call as Record<string, unknown>).tool_call_id === 'string'
					? ((call as Record<string, unknown>).tool_call_id as string)
					: '';
		if (!toolCallId || seen.has(toolCallId)) continue;
		const knownRaw = getToolResultFromLookup(knownResults, toolCallId);
		if (knownRaw !== undefined) {
			mergedResults.push(normalizeToolResultEntry(toolCallId, knownRaw));
			seen.add(toolCallId);
		}
	}

	if (mergedResults.length === 0 && !Array.isArray(typedBlock.results)) return block;
	return {
		...typedBlock,
		results: mergedResults
	};
};

export const hydrateToolResultsInBlocks = (
	blocks: unknown[] = [],
	knownResults?: ToolResultLookup,
	previousBlocks: unknown[] = []
) => {
	return blocks.map((block, index) =>
		hydrateToolResultsInBlock(block, knownResults, previousBlocks[index])
	);
};

const extractMarkdownField = (block: string, label: string) => {
	const fieldRegex = new RegExp(`^\\s*\\*\\*${escapeRegExp(label)}:\\*\\*\\s*(.*)$`, 'im');
	return block.match(fieldRegex)?.[1]?.trim() ?? '';
};

export const getDomain = (url: string) => {
	try {
		return new URL(url).hostname.replace(/^www\./i, '');
	} catch {
		return '';
	}
};

export const truncateMiddle = (value: string, max = 96) => {
	if (value.length <= max) return value;
	const keep = Math.max(8, Math.floor((max - 1) / 2));
	return `${value.slice(0, keep)}…${value.slice(value.length - keep)}`;
};

export const truncateEnd = (value: string, max = 120) => {
	if (value.length <= max) return value;
	return `${value.slice(0, Math.max(0, max - 1)).trimEnd()}…`;
};

export const compactWhitespace = (value: string) => value.replace(/\s+/g, ' ').trim();

export const previewText = (value: string, max = 900) => {
	// Previews are rendered for every fetched page card. Do not normalize an
	// entire 100k+ character page just to show the first few lines.
	const head = value.length > max * 4 ? value.slice(0, max * 4) : value;
	return truncateEnd(compactWhitespace(head), max);
};

export const formatCharacterCount = (count: number) => {
	if (!Number.isFinite(count) || count <= 0) return '0 chars';
	if (count < 1000) return `${count} chars`;
	if (count < 1000000) return `${(count / 1000).toFixed(count < 10000 ? 1 : 0)}k chars`;
	return `${(count / 1000000).toFixed(1)}m chars`;
};

export const formatCount = (count: number, singular: string, plural = `${singular}s`) => {
	return `${count} ${count === 1 ? singular : plural}`;
};

const PARSE_CACHE_MAX_ENTRIES = 16;
const PARSE_CACHE_MAX_RAW_CHARS = 4_000_000;

type ParseCache<T> = {
	entries: Map<string, { value: T; cost: number }>;
	totalCost: number;
};

const webSearchParseCache: ParseCache<ParsedWebSearchResult> = { entries: new Map(), totalCost: 0 };
const webFetchParseCache: ParseCache<ParsedWebFetchResult> = { entries: new Map(), totalCost: 0 };
const browserParseCache: ParseCache<ParsedBrowserResult> = { entries: new Map(), totalCost: 0 };

const hashString = (value: string) => {
	let hash = 2166136261;
	for (let i = 0; i < value.length; i += 1) {
		hash ^= value.charCodeAt(i);
		hash = Math.imul(hash, 16777619);
	}
	return (hash >>> 0).toString(36);
};

const stableText = (value: unknown) => {
	if (value == null) return '';
	if (typeof value === 'string') return value;
	try {
		return JSON.stringify(value);
	} catch {
		return String(value);
	}
};

const cacheKey = (kind: string, raw: string, args?: unknown) => {
	const argsText = stableText(args);
	return `${kind}:${raw.length}:${hashString(raw)}:${argsText.length}:${hashString(argsText)}`;
};

const getCachedParse = <T>(cache: ParseCache<T>, key: string): T | null => {
	const entry = cache.entries.get(key);
	if (!entry) return null;
	cache.entries.delete(key);
	cache.entries.set(key, entry);
	return entry.value;
};

const setCachedParse = <T>(cache: ParseCache<T>, key: string, value: T, cost: number) => {
	if (cost > PARSE_CACHE_MAX_RAW_CHARS) return value;
	const existing = cache.entries.get(key);
	if (existing) {
		cache.totalCost -= existing.cost;
		cache.entries.delete(key);
	}
	cache.entries.set(key, { value, cost });
	cache.totalCost += cost;
	while (
		cache.entries.size > PARSE_CACHE_MAX_ENTRIES ||
		cache.totalCost > PARSE_CACHE_MAX_RAW_CHARS
	) {
		const oldestKey = cache.entries.keys().next().value;
		if (!oldestKey) break;
		const oldest = cache.entries.get(oldestKey);
		cache.entries.delete(oldestKey);
		cache.totalCost -= oldest?.cost ?? 0;
	}
	return value;
};

export const clearToolResultParseCaches = () => {
	for (const cache of [webSearchParseCache, webFetchParseCache, browserParseCache]) {
		cache.entries.clear();
		cache.totalCost = 0;
	}
};

export const getToolResultParseCacheStats = () => ({
	webSearch: { entries: webSearchParseCache.entries.size, chars: webSearchParseCache.totalCost },
	webFetch: { entries: webFetchParseCache.entries.size, chars: webFetchParseCache.totalCost },
	browser: { entries: browserParseCache.entries.size, chars: browserParseCache.totalCost }
});

const parseDeclaredSearchCount = (text: string) => {
	const match = text.match(/\bFound\s+(\d+)\s+results?\b/i);
	return match ? Number.parseInt(match[1], 10) : null;
};

const parseDeclaredFetchCount = (text: string) => {
	const match = text.match(/\bRetrieved\s+content\s+from\s+(\d+)\s+URL\(s\)/i);
	return match ? Number.parseInt(match[1], 10) : null;
};

export const parseWebSearchResult = (
	rawResult: unknown,
	rawArgs?: unknown
): ParsedWebSearchResult => {
	const raw = decodeToolResultText(rawResult);
	const key = cacheKey('web_search', raw, rawArgs);
	const cached = getCachedParse(webSearchParseCache, key);
	if (cached) return cached;
	const args = getToolArgumentsObject(rawArgs);
	const queryFromArgs = typeof args.query === 'string' ? args.query : '';
	const queryFromHeader = raw.match(/^##\s*Search Results for:\s*(.+?)\s*$/im)?.[1]?.trim() ?? '';
	const declaredCount = parseDeclaredSearchCount(raw);

	const results: WebSearchItem[] = [];
	const headingRegex = /^###\s*Result\s+(\d+)\b.*$/gim;
	const headings = [...raw.matchAll(headingRegex)];

	for (let i = 0; i < headings.length; i += 1) {
		const heading = headings[i];
		const nextHeading = headings[i + 1];
		const start = (heading.index ?? 0) + heading[0].length;
		const end = nextHeading?.index ?? raw.length;
		const block = raw.slice(start, end);

		const index = Number.parseInt(heading[1] ?? `${i + 1}`, 10) || i + 1;
		const title = extractMarkdownField(block, 'Title');
		const url = extractMarkdownField(block, 'URL');
		const snippet = extractMarkdownField(block, 'Snippet');

		if (!title && !url && !snippet) continue;

		results.push({
			index,
			title: title || url || `Result ${index}`,
			url,
			domain: getDomain(url),
			snippet
		});
	}

	return setCachedParse(
		webSearchParseCache,
		key,
		{
			ok: results.length > 0,
			query: queryFromArgs || queryFromHeader,
			declaredCount,
			results,
			raw
		},
		raw.length
	);
};

export const parseWebFetchResult = (rawResult: unknown): ParsedWebFetchResult => {
	const raw = decodeToolResultText(rawResult);
	const key = cacheKey('web_fetch', raw);
	const cached = getCachedParse(webFetchParseCache, key);
	if (cached) return cached;
	const declaredCount = parseDeclaredFetchCount(raw);
	const pages: WebFetchPage[] = [];
	const pageHeadingRegex = /^###\s*Page\s+(\d+)\s*:\s*(.*?)\s*$/gim;
	const allHeadings = [...raw.matchAll(pageHeadingRegex)];
	const headings = allHeadings.filter((heading, idx) => {
		if (idx === 0) return true;

		// Page headings generated by web_fetch are separated by a standalone
		// markdown rule. Fetched page bodies can themselves contain headings like
		// "### Page 2", so only treat subsequent matches as page boundaries when
		// they are immediately preceded by that generated separator.
		const position = heading.index ?? 0;
		const prefix = raw.slice(Math.max(0, position - 24), position);
		return /\n---\s*\n\s*$/.test(prefix);
	});

	for (let i = 0; i < headings.length; i += 1) {
		const heading = headings[i];
		const nextHeading = headings[i + 1];
		const start = (heading.index ?? 0) + heading[0].length;
		const end = nextHeading?.index ?? raw.length;
		let block = raw.slice(start, end).trim();

		// Remove the separator that web_fetch appends after each page while keeping
		// separators that may legitimately appear inside the fetched document body.
		block = block.replace(/\n---\s*$/, '').trim();

		const index = Number.parseInt(heading[1] ?? `${i + 1}`, 10) || i + 1;
		const title = heading[2]?.trim() || `Page ${index}`;
		const url = extractMarkdownField(block, 'URL');
		const published = extractMarkdownField(block, 'Published');
		const author = extractMarkdownField(block, 'Author');
		const description = extractMarkdownField(block, 'Description');

		const contentMarker = block.match(/\*\*Content:\*\*\s*/i);
		let content = '';
		if (contentMarker?.index != null) {
			content = block.slice(contentMarker.index + contentMarker[0].length).trim();
			content = content.replace(/\n---\s*$/, '').trim();
		}

		pages.push({
			index,
			title,
			url,
			domain: getDomain(url),
			published,
			author,
			description,
			content,
			characters: content.length
		});
	}

	return setCachedParse(
		webFetchParseCache,
		key,
		{
			ok: pages.length > 0,
			declaredCount,
			pages,
			totalCharacters: pages.reduce((sum, page) => sum + page.characters, 0),
			raw
		},
		raw.length
	);
};

// The container browser tools return a text block shaped by the CAM daemon's
// `_format_snapshot`:
//     Title: <title>
//     URL: <url>
//
//     <accessibility snapshot tree>
// `Title:`/`URL:` may be absent (e.g. screenshot-only actions, error recovery
// text). Everything after the leading metadata lines is treated as the body.
export const parseBrowserResult = (rawResult: unknown, rawArgs?: unknown): ParsedBrowserResult => {
	const raw = decodeToolResultText(rawResult);
	const key = cacheKey('browser', raw, rawArgs);
	const cached = getCachedParse(browserParseCache, key);
	if (cached) return cached;
	const args = getToolArgumentsObject(rawArgs);
	const urlFromArgs = typeof args.url === 'string' ? args.url.trim() : '';

	let title = '';
	let url = '';
	const lines = raw.split('\n');
	let bodyStart = 0;

	// Consume the leading `Title:` / `URL:` metadata lines (in either order),
	// stopping at the first blank line or non-metadata content.
	for (let i = 0; i < lines.length; i += 1) {
		const line = lines[i];
		const titleMatch = line.match(/^Title:\s*(.*)$/);
		const urlMatch = line.match(/^URL:\s*(.*)$/);
		if (titleMatch) {
			title = titleMatch[1].trim();
			bodyStart = i + 1;
			continue;
		}
		if (urlMatch) {
			url = urlMatch[1].trim();
			bodyStart = i + 1;
			continue;
		}
		break;
	}

	let snapshot = lines.slice(bodyStart).join('\n').replace(/^\n+/, '').trimEnd();
	// When there were no metadata lines at all, the whole text is the body.
	if (!title && !url) snapshot = raw.trim();

	const finalUrl = url || urlFromArgs;
	return setCachedParse(
		browserParseCache,
		key,
		{
			url: finalUrl,
			domain: getDomain(finalUrl),
			title,
			snapshot,
			raw
		},
		raw.length
	);
};

export const getToolCallSummary = (
	name: ToolName,
	rawArgs: unknown,
	rawResult: unknown,
	done: boolean
): ToolCallSummary => {
	// Keep the generic path intentionally cheap. Tool call rows render even when
	// collapsed, so only decode potentially huge result strings for the web tools
	// that actually use the richer summary metadata.
	if (name === 'web_search') {
		const args = getToolArgumentsObject(rawArgs);
		const summary =
			rawResult && typeof rawResult === 'object' && !Array.isArray(rawResult)
				? ((rawResult as Record<string, any>).summary ?? null)
				: null;
		const rawResultString = typeof rawResult === 'string' ? rawResult : '';
		const queryFromArgs = typeof args.query === 'string' ? args.query.trim() : '';
		const decodedResultForFallback = done && !queryFromArgs ? decodeToolResultText(rawResult) : '';
		const query =
			queryFromArgs ||
			decodedResultForFallback.match(/^##\s*Search Results for:\s*(.+?)\s*$/im)?.[1]?.trim() ||
			'';
		const count = done
			? typeof summary?.result_count === 'number'
				? summary.result_count
				: (parseDeclaredSearchCount(rawResultString) ??
					(decodedResultForFallback ? parseDeclaredSearchCount(decodedResultForFallback) : null))
			: null;

		return {
			kind: 'web_search',
			title: query ? `Search: “${truncateEnd(query, 72)}”` : 'web_search',
			subtitle: done && count != null ? formatCount(count, 'result') : undefined,
			badge: 'web'
		};
	}

	if (name === 'web_fetch') {
		const args = getToolArgumentsObject(rawArgs);
		const summary =
			rawResult && typeof rawResult === 'object' && !Array.isArray(rawResult)
				? ((rawResult as Record<string, any>).summary ?? null)
				: null;
		const rawResultString = typeof rawResult === 'string' ? rawResult : '';
		const urlsArg = typeof args.urls === 'string' ? args.urls : '';
		const requestedUrls = urlsArg
			.split(/[\n,]+/)
			.map((url) => url.trim())
			.filter(Boolean);
		const count = done
			? typeof summary?.page_count === 'number'
				? summary.page_count
				: parseDeclaredFetchCount(rawResultString)
			: null;

		return {
			kind: 'web_fetch',
			title: done
				? `Fetched ${count != null ? formatCount(count, 'page') : 'web pages'}`
				: `Fetching ${requestedUrls.length ? formatCount(requestedUrls.length, 'URL') : 'web pages'}…`,
			subtitle: done ? undefined : requestedUrls.map((url) => getDomain(url) || url).join(', '),
			badge: 'web'
		};
	}

	if (isBrowserToolName(name)) {
		return getBrowserToolCallSummary(name, rawArgs, rawResult, done);
	}

	return {
		kind: 'generic',
		title: `View Result from ${name}`
	};
};

// Pull the host the action settled on out of the result text's `URL:` line
// without fully parsing the (potentially large) snapshot tree. Cheap: scans
// only the first handful of lines.
const browserHostFromResult = (rawResult: unknown): string => {
	const raw = typeof rawResult === 'string' ? rawResult : decodeToolResultText(rawResult);
	if (!raw) return '';
	const match = raw.match(/^\s*URL:\s*(\S+)/m);
	return match ? getDomain(match[1]) : '';
};

const truncatePreview = (value: string, max = 40): string => {
	const trimmed = value.trim();
	if (!trimmed) return '';
	return truncateEnd(compactWhitespace(trimmed), max);
};

const getBrowserToolCallSummary = (
	name: string,
	rawArgs: unknown,
	rawResult: unknown,
	done: boolean
): ToolCallSummary => {
	const args = getToolArgumentsObject(rawArgs);
	// Only decode the result host when the action is finished AND the host can't
	// already be derived from the args (navigate carries its own url) — keeps the
	// collapsed-row path from decoding big snapshots on every render.
	const onSuffix = (host: string) => (host ? ` on ${host}` : '');

	const summary = (title: string): ToolCallSummary => ({
		kind: 'browser',
		title,
		badge: 'browser'
	});

	switch (name) {
		case 'browser_navigate': {
			const url = typeof args.url === 'string' ? args.url.trim() : '';
			const host = getDomain(url) || (done ? browserHostFromResult(rawResult) : '') || url;
			return {
				kind: 'browser',
				title: done ? `Browsed ${host || 'page'}` : `Browsing ${host || 'page'}…`,
				subtitle: url || undefined,
				badge: 'browser'
			};
		}
		case 'browser_snapshot': {
			const host = done ? browserHostFromResult(rawResult) : '';
			return summary(done ? `Read page${onSuffix(host)}` : 'Reading page…');
		}
		case 'browser_screenshot': {
			const host = done ? browserHostFromResult(rawResult) : '';
			return summary(done ? `Captured screenshot${onSuffix(host)}` : 'Capturing screenshot…');
		}
		case 'browser_click': {
			const ref = typeof args.ref === 'string' ? args.ref : '';
			const target = ref ? ` ${ref}` : '';
			return summary(done ? `Clicked${target}` : `Clicking${target}…`);
		}
		case 'browser_type': {
			const ref = typeof args.ref === 'string' ? args.ref : '';
			const text = typeof args.text === 'string' ? truncatePreview(args.text) : '';
			const into = ref ? ` into ${ref}` : '';
			const preview = text ? ` “${text}”` : '';
			return summary(done ? `Typed${preview}${into}` : `Typing${into}…`);
		}
		case 'browser_select': {
			const ref = typeof args.ref === 'string' ? args.ref : '';
			const target = ref ? ` ${ref}` : '';
			return summary(done ? `Selected option${target}` : `Selecting option${target}…`);
		}
		case 'browser_press_key': {
			const key = typeof args.key === 'string' ? args.key.trim() : '';
			const target = key ? ` ${key}` : ' key';
			return summary(done ? `Pressed${target}` : `Pressing${target}…`);
		}
		case 'browser_back': {
			return summary(done ? 'Went back' : 'Going back…');
		}
		case 'browser_wait': {
			return summary(done ? 'Waited for page' : 'Waiting for page…');
		}
		default: {
			return summary(done ? 'Browser action' : 'Browser action…');
		}
	}
};
