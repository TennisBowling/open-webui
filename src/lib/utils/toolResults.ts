export type ToolName = 'web_search' | 'web_fetch' | string;

export type DecodedToolArguments =
	| Record<string, unknown>
	| unknown[]
	| string
	| number
	| boolean
	| null;

// Glyph shown at the head of a collapsed tool-call row. Kept as a small closed
// set so `ToolCallIcon` can map it to an inline SVG without a dynamic import.
export type ToolIcon =
	| 'search'
	| 'globe'
	| 'browser'
	| 'terminal'
	| 'file'
	| 'file-edit'
	| 'image'
	| 'plug'
	| 'tool';

export interface ToolCallSummary {
	kind: 'web_search' | 'web_fetch' | 'browser' | 'shell' | 'file' | 'generic';
	icon: ToolIcon;
	// Verb phrase — "Searched", "Ran", "Read". Never the raw tool name unless
	// there is nothing better to say about it.
	title: string;
	// The one argument that identifies this call: the query, the path, the
	// command. Rendered muted next to the title.
	detail?: string;
	// `detail` is code (a path/command/tool name) and should be monospaced.
	detailMono?: boolean;
	// Trailing count/size chip — "10 results", "exit 1".
	meta?: string;
	// Renders `meta` in the error color (a non-zero shell exit, mainly).
	metaError?: boolean;
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
			icon: 'search',
			title: done ? 'Searched' : 'Searching',
			detail: query ? `“${truncateEnd(query, 96)}”` : undefined,
			meta: done && count != null ? formatCount(count, 'result') : undefined
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

		const hosts = requestedUrls.map((url) => getDomain(url) || url).filter(Boolean);
		const size = typeof summary?.size === 'number' ? summary.size : null;

		return {
			kind: 'web_fetch',
			icon: 'globe',
			title: done ? 'Read' : 'Reading',
			detail: hosts.length ? truncateEnd(hosts.join(', '), 72) : undefined,
			meta: done
				? count != null && count > 1
					? formatCount(count, 'page')
					: size != null
						? formatCharacterCount(size)
						: undefined
				: undefined
		};
	}

	if (isBrowserToolName(name)) {
		return getBrowserToolCallSummary(name, rawArgs, rawResult, done);
	}

	return getGenericToolCallSummary(name, rawArgs, rawResult, done);
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

	const summary = (title: string, detail?: string): ToolCallSummary => ({
		kind: 'browser',
		icon: 'browser',
		title,
		detail
	});

	switch (name) {
		case 'browser_navigate': {
			const url = typeof args.url === 'string' ? args.url.trim() : '';
			const host = getDomain(url) || (done ? browserHostFromResult(rawResult) : '') || url;
			return summary(done ? 'Browsed' : 'Browsing', host || undefined);
		}
		case 'browser_snapshot': {
			const host = done ? browserHostFromResult(rawResult) : '';
			return summary(done ? 'Read page' : 'Reading page', host || undefined);
		}
		case 'browser_screenshot': {
			const host = done ? browserHostFromResult(rawResult) : '';
			return summary(done ? 'Captured screenshot' : 'Capturing screenshot', host || undefined);
		}
		case 'browser_click': {
			const ref = typeof args.ref === 'string' ? args.ref : '';
			return summary(done ? 'Clicked' : 'Clicking', ref || undefined);
		}
		case 'browser_type': {
			const ref = typeof args.ref === 'string' ? args.ref : '';
			const text = typeof args.text === 'string' ? truncatePreview(args.text) : '';
			const into = ref ? ` → ${ref}` : '';
			return summary(done ? 'Typed' : 'Typing', text ? `“${text}”${into}` : ref || undefined);
		}
		case 'browser_select': {
			const ref = typeof args.ref === 'string' ? args.ref : '';
			return summary(done ? 'Selected option' : 'Selecting option', ref || undefined);
		}
		case 'browser_press_key': {
			const key = typeof args.key === 'string' ? args.key.trim() : '';
			return summary(done ? 'Pressed' : 'Pressing', key || 'key');
		}
		case 'browser_back': {
			return summary(done ? 'Went back' : 'Going back');
		}
		case 'browser_wait': {
			return summary(done ? 'Waited for page' : 'Waiting for page');
		}
		default: {
			return summary(done ? 'Browser action' : 'Browser action');
		}
	}
};

// ---------------------------------------------------------------------------
// Generic (non web/browser) tool calls
// ---------------------------------------------------------------------------

// Arguments worth showing next to a tool name in the collapsed row, most
// identifying first. `bash` carries `command`, the file tools carry `path`,
// MCP search tools carry `q`/`query`, and so on. Anything not listed falls back
// to the first short-ish string argument.
const PRIMARY_ARG_KEYS = [
	'command',
	'cmd',
	'path',
	'file_path',
	'filename',
	'query',
	'q',
	'url',
	'urls',
	'source',
	'prompt',
	'question',
	'name',
	'title',
	'text',
	'city',
	'location',
	'id'
];

const SHELL_TOOL_RE = /(^|_)(bash|shell|sh|exec|run_command|terminal)$/i;
const READ_TOOL_RE = /(^|_)(read|read_file|cat|view|open_file)$/i;
const WRITE_TOOL_RE = /(^|_)(write|write_file|create_file|save_file)$/i;
const EDIT_TOOL_RE = /(^|_)(edit|edit_file|patch|apply_patch|str_replace)$/i;
const LIST_TOOL_RE = /(^|_)(ls|list|list_files|list_directory|glob|find)$/i;
const SEARCH_TOOL_RE = /(^|_)(search|grep|search_web|websearch|query)$/i;
const IMAGE_TOOL_RE = /(^|_)(view_image|image|screenshot|render_image)$/i;

// Strip the container workspace prefix so paths read as project-relative.
const shortenPath = (value: string) => {
	const trimmed = value.trim().replace(/^sandbox:/i, '');
	const relative = trimmed.replace(/^\/workspace\/?/, '');
	return truncateMiddle(relative || trimmed, 64);
};

const firstLine = (value: string) => {
	const line = value.split('\n').find((candidate) => candidate.trim().length > 0) ?? '';
	return line.trim();
};

export const getToolArgumentPreview = (args: Record<string, unknown>): string => {
	const keys = [
		...PRIMARY_ARG_KEYS.filter((key) => typeof args[key] === 'string' && args[key]),
		...Object.keys(args).filter(
			(key) => !PRIMARY_ARG_KEYS.includes(key) && typeof args[key] === 'string' && args[key]
		)
	];
	const key = keys[0];
	if (!key) {
		const entries = Object.entries(args).filter(([, value]) => value != null && value !== '');
		if (!entries.length) return '';
		return truncateEnd(
			entries.map(([k, v]) => `${k}: ${compactWhitespace(String(v))}`).join(', '),
			72
		);
	}
	const value = String(args[key]);
	if (key === 'path' || key === 'file_path' || key === 'source' || key === 'filename') {
		return shortenPath(value);
	}
	return truncateEnd(compactWhitespace(firstLine(value)), 96);
};

// The tool name the model sees is not always the name a human wants to read:
// MCP tools ship as `mcp_<8hex>_<real name>` and many use kebab-case.
export const friendlyToolName = (name: string) => {
	const match = typeof name === 'string' ? name.match(/^mcp_[0-9a-f]{8}_(.+)$/) : null;
	return match ? match[1] : (name ?? '');
};

// `get_comments` / `notion-fetch` → "Get comments" / "Notion fetch". The row
// reads as a sentence next to its argument, so the identifier casing has to go.
const humanizeToolName = (name: string) => {
	const words = name.replace(/[-_]+/g, ' ').replace(/\s+/g, ' ').trim();
	if (!words) return '';
	return words.charAt(0).toUpperCase() + words.slice(1);
};

const getGenericToolCallSummary = (
	name: ToolName,
	rawArgs: unknown,
	rawResult: unknown,
	done: boolean
): ToolCallSummary => {
	const args = getToolArgumentsObject(rawArgs);
	const label = friendlyToolName(String(name ?? ''));

	if (SHELL_TOOL_RE.test(label)) {
		const command = typeof args.command === 'string' ? args.command : '';
		// A non-zero exit is the single most useful thing to surface without
		// expanding, and the shell preamble is the first line of the result.
		const resultHead = done ? decodeToolResultText(rawResult).slice(0, 200) : '';
		const exitMatch = resultHead.match(/^exit_code:\s*(-?\d+)/m);
		const exitCode = exitMatch ? Number.parseInt(exitMatch[1], 10) : null;
		return {
			kind: 'shell',
			icon: 'terminal',
			title: done ? 'Ran' : 'Running',
			// Flattened, not first-line: heredocs and `python3 -c "` make the first
			// line a stub that says nothing about what actually ran.
			detail: command ? truncateEnd(compactWhitespace(command), 96) : undefined,
			detailMono: true,
			meta: exitCode ? `exit ${exitCode}` : undefined,
			metaError: !!exitCode
		};
	}

	const pathArg =
		typeof args.path === 'string'
			? args.path
			: typeof args.file_path === 'string'
				? args.file_path
				: typeof args.source === 'string'
					? args.source
					: '';

	if (pathArg && (READ_TOOL_RE.test(label) || WRITE_TOOL_RE.test(label) || EDIT_TOOL_RE.test(label))) {
		const verbDone = READ_TOOL_RE.test(label) ? 'Read' : WRITE_TOOL_RE.test(label) ? 'Wrote' : 'Edited';
		const verbLive = READ_TOOL_RE.test(label)
			? 'Reading'
			: WRITE_TOOL_RE.test(label)
				? 'Writing'
				: 'Editing';
		return {
			kind: 'file',
			icon: EDIT_TOOL_RE.test(label) || WRITE_TOOL_RE.test(label) ? 'file-edit' : 'file',
			title: done ? verbDone : verbLive,
			detail: shortenPath(pathArg),
			detailMono: true
		};
	}

	const detail = getToolArgumentPreview(args);
	const isMcp = typeof name === 'string' && /^mcp_[0-9a-f]{8}_/.test(name);
	const icon: ToolIcon = IMAGE_TOOL_RE.test(label)
		? 'image'
		: SEARCH_TOOL_RE.test(label)
			? 'search'
			: LIST_TOOL_RE.test(label)
				? 'file'
				: isMcp
					? 'plug'
					: 'tool';

	return {
		kind: 'generic',
		icon,
		title: humanizeToolName(label) || 'Tool',
		detailMono: false,
		detail: detail || undefined
	};
};

// ---------------------------------------------------------------------------
// Result / argument shaping for the expanded panel
// ---------------------------------------------------------------------------

export type GenericToolResult =
	| { kind: 'empty' }
	| { kind: 'error'; message: string; details: string }
	| { kind: 'shell'; exitCode: number | null; stdout: string; stderr: string }
	| { kind: 'json'; text: string }
	| { kind: 'text'; text: string };

// Shell tools answer with the CAM preamble:
//     exit_code: 0
//
//     --- stdout ---
//     …
//     --- stderr ---
//     …
const SHELL_RESULT_RE = /^exit_code:\s*(-?\d+)\s*$/m;

export const parseGenericToolResult = (raw: unknown, errored = false): GenericToolResult => {
	const text = decodeToolResultText(raw);
	const trimmed = text.trim();

	if (!trimmed) return { kind: 'empty' };

	const shellMatch = trimmed.match(SHELL_RESULT_RE);
	if (shellMatch && /^exit_code:/.test(trimmed)) {
		const exitCode = Number.parseInt(shellMatch[1], 10);
		const body = trimmed.slice(shellMatch.index! + shellMatch[0].length);
		const stdoutMatch = body.match(/---\s*stdout\s*---\n?([\s\S]*?)(?=\n---\s*stderr\s*---|$)/i);
		const stderrMatch = body.match(/---\s*stderr\s*---\n?([\s\S]*)$/i);
		return {
			kind: 'shell',
			exitCode,
			stdout: (stdoutMatch?.[1] ?? (stdoutMatch || stderrMatch ? '' : body)).replace(/^\n+/, '').trimEnd(),
			stderr: (stderrMatch?.[1] ?? '').replace(/^\n+/, '').trimEnd()
		};
	}

	if (errored || /^Error:\s/i.test(trimmed)) {
		const withoutPrefix = trimmed.replace(/^Error:\s*/i, '');
		const [head, ...rest] = withoutPrefix.split('\n');
		return { kind: 'error', message: head.trim(), details: rest.join('\n').trim() };
	}

	if (looksLikeJSON(trimmed)) {
		try {
			const parsed = JSON.parse(trimmed);
			if (parsed && typeof parsed === 'object') {
				return { kind: 'json', text: JSON.stringify(parsed, null, 2) };
			}
		} catch {
			// fall through to plain text
		}
	}

	return { kind: 'text', text };
};

export interface ToolArgEntry {
	key: string;
	value: string;
	// `block` values get their own full-width code panel; `inline` values sit on
	// the same line as their key.
	kind: 'inline' | 'block';
}

const INLINE_ARG_MAX = 88;

export const getToolArgEntries = (rawArgs: unknown): ToolArgEntry[] => {
	const decoded = decodeToolArguments(rawArgs);
	if (decoded == null || decoded === '') return [];
	if (typeof decoded !== 'object' || Array.isArray(decoded)) {
		const value = String(decoded);
		return [{ key: '', value, kind: value.includes('\n') ? 'block' : 'inline' }];
	}

	const args = decoded as Record<string, unknown>;
	const keys = Object.keys(args);
	const ordered = [
		...PRIMARY_ARG_KEYS.filter((key) => keys.includes(key)),
		...keys.filter((key) => !PRIMARY_ARG_KEYS.includes(key))
	];

	return ordered.map((key) => {
		const raw = args[key];
		const value =
			typeof raw === 'string'
				? raw
				: raw == null
					? String(raw)
					: JSON.stringify(raw, null, raw && typeof raw === 'object' ? 2 : undefined);
		return {
			key,
			value,
			kind: value.includes('\n') || value.length > INLINE_ARG_MAX ? 'block' : 'inline'
		};
	});
};

// `edit`-shaped calls carry the before/after text as two arguments. Rendering
// them as a diff is the only way the change is actually readable.
export const getToolEditDiff = (
	name: string,
	rawArgs: unknown
): { path: string; oldText: string; newText: string } | null => {
	if (!EDIT_TOOL_RE.test(friendlyToolName(name ?? ''))) return null;
	const args = getToolArgumentsObject(rawArgs);
	const oldText = typeof args.old_string === 'string' ? args.old_string : '';
	const newText = typeof args.new_string === 'string' ? args.new_string : '';
	if (!oldText && !newText) return null;
	const path = typeof args.path === 'string' ? args.path : ((args.file_path as string) ?? '');
	return { path, oldText, newText };
};

// ---------------------------------------------------------------------------
// Excerpt cleanup
// ---------------------------------------------------------------------------

// Search snippets and fetched page bodies are raw scrape: image refs, markdown
// link syntax, and the extractor's `[...]` elision markers stitching together
// dozens of near-identical fragments ("$26.00 USD Cushion / Black", "26.0",
// "26.00 USD Cushion / Charcoal / XL", …). Showing that verbatim is what made
// the result list unreadable, so the excerpt is rebuilt from the fragments that
// actually say something: prose-like, not already said, in order.
const isProseFragment = (fragment: string) => {
	if (fragment.length < 24) return false;
	// Extractors split mid-sentence ("ve ever owned, return them for another").
	// A usable fragment starts the way a sentence does.
	if (!/^["“'([]?[A-Z0-9]/.test(fragment)) return false;
	const words = fragment.split(/\s+/).filter(Boolean);
	if (words.length < 5) return false;
	// "26.00 USD Cushion / Charcoal / XL" — variant/SKU rows, never prose.
	if ((fragment.match(/\//g) ?? []).length > 1) return false;
	const digits = fragment.replace(/[^0-9]/g, '').length;
	if (digits / fragment.length > 0.18) return false;
	const letters = fragment.replace(/[^a-z]/gi, '').length;
	return letters / fragment.length > 0.55;
};

// One scraped line often repeats the same clause for every product variant
// ("… 61% Merino Wool, 36% Nylon, 3% Lycra Spandex" ×20). Cut at the first
// repetition so the excerpt says it once.
const collapseRepeatedRuns = (fragment: string) => {
	const words = fragment.split(' ');
	if (words.length > 400) words.length = 400;
	for (let i = 0; i < words.length; i += 1) {
		for (let run = 4; run <= 12; run += 1) {
			if (i + 2 * run > words.length) break;
			if (words.slice(i, i + run).join(' ') === words.slice(i + run, i + 2 * run).join(' ')) {
				return words.slice(0, i + run).join(' ');
			}
		}
	}
	return fragment;
};

export const cleanExcerpt = (value: string, max = 320, avoid = '') => {
	if (!value) return '';
	const head = value.length > max * 8 ? value.slice(0, max * 8) : value;
	const stripped = head
		.replace(/!\[[^\]]*\]\([^)]*\)/g, ' ')
		.replace(/\[([^\]]+)\]\([^)]*\)/g, '$1')
		.replace(/^#{1,6}\s+/gm, '')
		.replace(/[*_`>|]/g, ' ');

	const kept: string[] = [];
	// `avoid` is the row's own title: a snippet that just restates it is dead
	// space in a list where the title is already the first thing you read.
	const seen = avoid ? [compactWhitespace(avoid).toLowerCase()] : [];
	let keptLength = 0;
	for (const rawFragment of stripped.split(/\[\.\.\.\]|\n{2,}|\n/)) {
		const fragment = collapseRepeatedRuns(
			compactWhitespace(rawFragment).replace(/^[-•*+]\s+/, '')
		);
		if (!isProseFragment(fragment)) continue;
		const lower = fragment.toLowerCase();
		// Scrapes repeat the same sentence with small variations; keep the first.
		if (seen.some((previous) => previous.includes(lower.slice(0, 40)))) continue;
		seen.push(lower);
		kept.push(fragment);
		keptLength += fragment.length + 3;
		if (keptLength >= max) break;
	}

	// Nothing prose-like survived (a pure price table, say) — fall back to the
	// flattened text so the row is never mysteriously blank.
	const joined = kept.length ? kept.join(' · ') : compactWhitespace(stripped);
	return truncateEnd(joined, max);
};
