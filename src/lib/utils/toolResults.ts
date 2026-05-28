export type ToolName = 'web_search' | 'web_fetch' | string;

export type DecodedToolArguments =
	| Record<string, unknown>
	| unknown[]
	| string
	| number
	| boolean
	| null;

export interface ToolCallSummary {
	kind: 'web_search' | 'web_fetch' | 'generic';
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

export type ToolResultLookup = Map<string, unknown> | Record<string, unknown> | null | undefined;

export interface ToolResultEntry {
	tool_call_id: string;
	content?: unknown;
	files?: unknown[];
	embeds?: unknown[];
	subagent_id?: string;
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

			// Stream-v2 content_blocks intentionally carry slim result placeholders
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

	return {
		ok: results.length > 0,
		query: queryFromArgs || queryFromHeader,
		declaredCount,
		results,
		raw
	};
};

export const parseWebFetchResult = (rawResult: unknown): ParsedWebFetchResult => {
	const raw = decodeToolResultText(rawResult);
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

	return {
		ok: pages.length > 0,
		declaredCount,
		pages,
		totalCharacters: pages.reduce((sum, page) => sum + page.characters, 0),
		raw
	};
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

	return {
		kind: 'generic',
		title: `View Result from ${name}`
	};
};
