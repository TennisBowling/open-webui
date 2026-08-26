import { decode } from 'html-entities';
import { removeDetails, removeAllDetails } from '$lib/utils';

type RetryContext = {
	content: string;
	content_blocks: any[];
	reasoning_details_per_round?: any[];
};

/**
 * Structural deep clone that is safe on Svelte 5 `$state` proxies.
 *
 * These helpers are called with `message.content_blocks` /
 * `message.reasoning_details_per_round` taken straight out of Chat.svelte's
 * `$state` history, so every object they touch is a reactive proxy.
 * `structuredClone()` throws `DataCloneError` on a proxy — which silently broke
 * every rewind / retry-from-last-request — so it must never be used here.
 *
 * A recursive plain-object/array copy is an exact clone for this data: blocks
 * and reasoning details are pure JSON (they round-trip through the DB as JSON),
 * and returning PLAIN objects is required anyway — the result is assigned onto
 * a new message and persisted, so it must not stay bound to the source's
 * reactive graph.
 */
const cloneValue = <T>(value: T): T => {
	if (value === null || typeof value !== 'object') return value;
	if (Array.isArray(value)) return value.map((entry) => cloneValue(entry)) as unknown as T;
	const out: Record<string, unknown> = {};
	for (const key of Object.keys(value as Record<string, unknown>)) {
		out[key] = cloneValue((value as Record<string, unknown>)[key]);
	}
	return out as T;
};

// content_blocks is a dense list of block OBJECTS, but a message that arrived
// through a degraded path (e.g. a stream mirror that missed early opens, or a
// row persisted by an older client) can carry null/undefined entries. Those are
// display-inert and must never be cloned into a NEW persisted branch. Applied
// to an already-sliced prefix — cut indices are computed against the message's
// RAW block array, so slicing must happen in that index space first.
const dropInvalidBlocks = (blocks: any[]): any[] =>
	blocks.filter((b: any) => b && typeof b === 'object');

const hasOwn = (value: object, key: string) => Object.prototype.hasOwnProperty.call(value, key);

const getToolCallId = (call: any) => call?.id ?? call?.tool_call_id ?? '';
const SUBAGENT_TOOL_NAMES = new Set([
	'subagent_launch',
	'subagent_continue',
	'subagent_agent_launch'
]);
const getToolCallName = (call: any) => String(call?.function?.name ?? '');

const isPureSubagentToolCallsBlock = (block: any) => {
	if (block?.type !== 'tool_calls') return false;
	const calls = Array.isArray(block.content) ? block.content : [];
	return (
		calls.length > 0 && calls.every((call: any) => SUBAGENT_TOOL_NAMES.has(getToolCallName(call)))
	);
};

const blockHasMeaningfulParentOutput = (block: any) => {
	if (!block || typeof block !== 'object') return !!block;
	const btype = block.type;
	const content = block.content;

	if (btype === 'text' || btype === 'reasoning') {
		return typeof content === 'string' && content.trim().length > 0;
	}

	if (btype === 'tool_calls') {
		if (isPureSubagentToolCallsBlock(block)) return false;
		return Boolean(
			(Array.isArray(block.content) && block.content.length > 0) ||
				(Array.isArray(block.results) && block.results.length > 0)
		);
	}

	if (typeof content === 'string') return content.trim().length > 0;
	if (Array.isArray(content)) return content.length > 0;
	if (content && typeof content === 'object') return Object.keys(content).length > 0;
	return content != null;
};

const toolResultHasBody = (result: any) =>
	!!result &&
	typeof result === 'object' &&
	!Array.isArray(result) &&
	(hasOwn(result, 'content') || hasOwn(result, 'result_ref'));

export const isCompletedToolCallsBlock = (block: any) => {
	if (block?.type !== 'tool_calls') return false;
	const calls = Array.isArray(block.content) ? block.content : [];
	const results = Array.isArray(block.results) ? block.results : [];
	if (calls.length === 0 || results.length === 0) return false;

	const resultsById = new Map<string, any>(
		results
			.map((result: any): [string, any] => [result?.tool_call_id ?? '', result])
			.filter(([toolCallId]: [string, any]) => !!toolCallId)
	);

	return calls.every((call: any) => {
		const result = resultsById.get(getToolCallId(call));
		return toolResultHasBody(result);
	});
};

/**
 * Number of tool calls in this message's structured `content_blocks` whose
 * results carry durable bodies (inline content or a `result_ref`). This is the
 * forward-progress metric for the auto-retry loops: v2.1 turns keep their tool
 * history HERE — `content` is a text-only projection, so the legacy
 * `countCompletedToolCalls(content)` HTML parser always sees zero for them
 * (which made every mid-turn failure look like "no progress" and, worse, left
 * the retry's assembly leaf unpinned so the whole accumulated turn was dropped
 * from the resend). Returns 0 for legacy messages without blocks — callers
 * fall back to the content parser.
 */
export const countCompletedStructuredToolCalls = (message: any): number => {
	const blocks = Array.isArray(message?.content_blocks) ? message.content_blocks : [];
	let completed = 0;
	for (const block of blocks) {
		if (block?.type !== 'tool_calls') continue;
		const calls = Array.isArray(block.content) ? block.content : [];
		const results = Array.isArray(block.results) ? block.results : [];
		const resultsById = new Map<string, any>(
			results
				.map((result: any): [string, any] => [result?.tool_call_id ?? '', result])
				.filter(([toolCallId]: [string, any]) => !!toolCallId)
		);
		for (const call of calls) {
			if (toolResultHasBody(resultsById.get(getToolCallId(call)))) completed += 1;
		}
	}
	return completed;
};

/**
 * Round-boundary cut indices for block-level rewind. A valid "between requests"
 * boundary sits immediately AFTER a COMPLETED tool_calls block (all of its —
 * possibly parallel — calls have result bodies). Returns the set of indices
 * `i + 1` for every completed tool_calls block at index `i`. Because the unit is
 * a whole tool_calls block, a parallel batch of calls is never split, and a cut
 * never lands mid-round (e.g. between a reasoning block and its own tool calls).
 */
export const getRewindCutIndices = (contentBlocks: any[] | null | undefined): Set<number> => {
	const cuts = new Set<number>();
	const blocks = Array.isArray(contentBlocks) ? contentBlocks : [];
	for (let i = 0; i < blocks.length; i += 1) {
		if (isCompletedToolCallsBlock(blocks[i])) cuts.add(i + 1);
	}
	return cuts;
};

/**
 * Locate the rewind cut that sits immediately AFTER the tool_calls block that
 * owns a given subagent run, for the "rewind & redo subagent" flow. Returns
 * `blockIndex + 1` (the index of the FIRST block to discard — same convention as
 * getRewindContext's `cutIndex`), or `-1` when the block can't be located.
 *
 * Matching mirrors the backend's `_find_parent_tool_call_block`
 * (utils/subagent.py): prefer an exact `tool_call_id` match on a call; fall back
 * to a block whose results carry the run's `subagent_id` (covers continuation /
 * empty-tool_call_id entries). Unlike getRewindCutIndices this does NOT require
 * the block to be "completed" — the redo rewrites that result, so cutting right
 * after the block is valid regardless of the current result body.
 */
export const getSubagentToolCallCutIndex = (message: any, run: any): number => {
	const blocks = Array.isArray(message?.content_blocks) ? message.content_blocks : [];
	if (blocks.length === 0) return -1;

	let wantedToolCallId = String(run?.tool_call_id ?? '');
	const wantedSubagentId = String(run?.subagent_id ?? run?.chat_id ?? '');

	// Recover the authoritative tool_call_id from the parent message's
	// subagent_runs when the caller didn't supply one. This is load-bearing for a
	// STRANDED / incomplete subagent (e.g. one that stuck at 'running' after a task
	// death): its LAUNCH call is always present in content_blocks, but it has NO
	// result row — so the subagent_id->results fallback below can't find it — and
	// the live card can lose its tool_call_id. subagent_runs always records the
	// tool_call_id, so resolving it here lets us match the always-present call.
	// Without this, redoing such a subagent fails with "couldn't locate its tool
	// call in the main agent's turn." (Matched the clicked run by subagent_id /
	// chat_id, OR by the entry_key the caller passed.)
	if (!wantedToolCallId) {
		const runs = message?.subagent_runs;
		const wantedEntryKey = String(run?.entry_key ?? '');
		if (runs && typeof runs === 'object') {
			for (const [entryKey, r] of Object.entries(runs)) {
				const rr = r as any;
				if (!rr || typeof rr !== 'object') continue;
				const sid = String(rr.subagent_id ?? rr.chat_id ?? '');
				const tcid = String(rr.tool_call_id ?? '');
				if (!tcid) continue;
				const matches =
					(wantedSubagentId && sid === wantedSubagentId) ||
					(wantedEntryKey && (entryKey === wantedEntryKey || rr.entry_key === wantedEntryKey));
				if (matches) {
					wantedToolCallId = tcid;
					break;
				}
			}
		}
	}

	let fallbackIndex = -1;
	for (let i = 0; i < blocks.length; i += 1) {
		const block = blocks[i];
		if (block?.type !== 'tool_calls') continue;

		const calls = Array.isArray(block.content) ? block.content : [];
		if (wantedToolCallId) {
			for (const call of calls) {
				if (getToolCallId(call) === wantedToolCallId) return i + 1;
			}
		}

		if (wantedSubagentId && fallbackIndex < 0) {
			const results = Array.isArray(block.results) ? block.results : [];
			const matchesSubagent = results.some(
				(r: any) => r && String(r.subagent_id ?? '') === wantedSubagentId
			);
			if (matchesSubagent) fallbackIndex = i + 1;
		}
	}

	return fallbackIndex;
};

/**
 * True when several selected subagent tool-call cuts can share ONE rewind branch.
 *
 * The common case is one parallel tool_calls block (all cuts equal). Some providers
 * can also emit separate consecutive tool_calls blocks that contain ONLY subagent
 * launches/continues, with no parent text/reasoning between them. The backend's
 * rerun guard treats those pure subagent fanout blocks as not consuming an earlier
 * subagent result, so the frontend should allow batching them too. Any parent text,
 * reasoning, or non-subagent tool block between the earliest and latest selected
 * cuts means the transcript is not safe to batch.
 */
export const canBatchSubagentToolCallCuts = (message: any, cuts: number[]): boolean => {
	const blocks = Array.isArray(message?.content_blocks) ? message.content_blocks : [];
	const unique = [
		...new Set(cuts.filter((c) => Number.isFinite(c) && c >= 0 && c <= blocks.length))
	].sort((a, b) => a - b);
	if (unique.length <= 1) return true;

	const earliestCut = unique[0];
	const latestCut = unique[unique.length - 1];
	for (let i = earliestCut; i < latestCut; i += 1) {
		if (blockHasMeaningfulParentOutput(blocks[i])) return false;
	}
	return true;
};

export const getStructuredRetryLastRequestContext = (message: any): RetryContext | null => {
	const blocks = Array.isArray(message?.content_blocks) ? message.content_blocks : [];
	if (blocks.length === 0) return null;

	let lastCompletedToolBlockIndex = -1;
	let toolRoundCountAtLastCompletedBlock = 0;
	let toolRoundCount = 0;

	for (let i = 0; i < blocks.length; i += 1) {
		if (blocks[i]?.type !== 'tool_calls') continue;
		toolRoundCount += 1;
		if (isCompletedToolCallsBlock(blocks[i])) {
			lastCompletedToolBlockIndex = i;
			toolRoundCountAtLastCompletedBlock = toolRoundCount;
		}
	}

	if (lastCompletedToolBlockIndex < 0) return null;

	const context: RetryContext = {
		content: '',
		content_blocks: dropInvalidBlocks(cloneValue(blocks.slice(0, lastCompletedToolBlockIndex + 1)))
	};

	if (Array.isArray(message?.reasoning_details_per_round)) {
		context.reasoning_details_per_round = cloneValue(
			message.reasoning_details_per_round.slice(0, toolRoundCountAtLastCompletedBlock)
		);
	}

	return context;
};

/**
 * Build the seed context for a block-level REWIND: keep the assistant turn's
 * content_blocks up to (but not including) `cutIndex`, optionally inject a
 * user message at that boundary, and resume generation from there.
 *
 * `cutIndex` is the index of the FIRST block to DISCARD — i.e. we keep
 * `content_blocks.slice(0, cutIndex)`. This mirrors the "rewind to here" UI
 * boundary, which sits before a rendered block.
 *
 * The returned prefix always ends with an empty `text` block so the backend
 * stream has a valid append target (streamed text attaches to the tail block;
 * it only opens a fresh text block when the tail is `reasoning` or the list is
 * empty — see middleware.py stream_body_handler). When `steerText` is non-empty
 * a `user_steer` block is inserted just before that trailing text block, exactly
 * like the live mid-task steer injection — `_expand_assistant` turns it into a
 * real `{role:"user"}` turn, so the model resumes inline from the redirect. An
 * empty `steerText` degrades to a pure rewind+regenerate from the cut.
 *
 * `reasoning_details_per_round` is indexed per tool-call round, so it is sliced
 * to the number of `tool_calls` blocks retained in the prefix (matching
 * getStructuredRetryLastRequestContext) to keep per-round reasoning aligned.
 *
 * NOTE: tool_result_bodies are NOT handled here — for persisted v2.1 chats the
 * backend copies the surviving bodies row-to-row (`copy_tool_result_bodies_from`
 * on the append_message op); only the legacy v1/temp-chat path hydrates them
 * client-side (see hydrateLazyToolResultBodiesForRetry in Chat.svelte).
 */
export const getRewindContext = (
	message: any,
	cutIndex: number,
	steerText: string
): RetryContext | null => {
	const blocks = Array.isArray(message?.content_blocks) ? message.content_blocks : [];
	if (blocks.length === 0) return null;

	// Keep blocks BEFORE the cut. Clamp into [0, blocks.length].
	const cut = Math.max(0, Math.min(Math.floor(cutIndex), blocks.length));
	const keptBlocks: any[] = dropInvalidBlocks(cloneValue(blocks.slice(0, cut)));

	// Count retained tool-call rounds to trim the per-round reasoning carrier.
	let toolRoundCount = 0;
	for (let i = 0; i < keptBlocks.length; i += 1) {
		if (keptBlocks[i]?.type === 'tool_calls') toolRoundCount += 1;
	}

	const text = (steerText ?? '').trim();
	if (text) {
		keptBlocks.push({ type: 'user_steer', content: text });
	}
	// Trailing empty text block = the next-round stream target (mirrors the loop).
	keptBlocks.push({ type: 'text', content: '' });

	const context: RetryContext = {
		content: '',
		content_blocks: keptBlocks
	};

	if (Array.isArray(message?.reasoning_details_per_round)) {
		context.reasoning_details_per_round = cloneValue(
			message.reasoning_details_per_round.slice(0, toolRoundCount)
		);
	}

	return context;
};

export const getRetryableToolContext = (content = '') => {
	content = getStringMessageContent(content);

	const toolCallsRegex = /<details\s+type="tool_calls"[^>]*>[\s\S]*?<\/details>/gi;
	const toolCallMatches = [...content.matchAll(toolCallsRegex)];

	if (toolCallMatches.length === 0) {
		return null;
	}

	const completedToolCallMatch =
		[...toolCallMatches].reverse().find((match) => /\bdone="true"/i.test(match[0])) ?? null;

	if (completedToolCallMatch) {
		const endIndex = (completedToolCallMatch.index ?? 0) + completedToolCallMatch[0].length;

		return {
			content: content.substring(0, endIndex).trim(),
			endIndex,
			hasCompletedToolCall: true
		};
	}

	const firstToolCallMatch = toolCallMatches[0];
	const endIndex = firstToolCallMatch.index ?? 0;
	const preservedContent = content.substring(0, endIndex).trim();

	if (!preservedContent) {
		return null;
	}

	return {
		content: preservedContent,
		endIndex,
		hasCompletedToolCall: false
	};
};

// Count tool-call blocks that have finished (done="true"). Used by the retry
// loops as a forward-progress signal: if more tool calls have completed than
// at the previous failed attempt, the agent advanced and the consecutive-
// failure counter resets — so a genuinely progressing turn is never cut off.
export const countCompletedToolCalls = (content = '') => {
	const text = getStringMessageContent(content) || '';
	const blocks = [...text.matchAll(/<details\s+type="tool_calls"[^>]*>[\s\S]*?<\/details>/gi)];
	return blocks.filter((m) => /\bdone="true"/i.test(m[0])).length;
};

export const shouldContinueFromLastToolRequest = (message) => {
	const content = getStringMessageContent(message?.content);
	const toolContext = getRetryableToolContext(content);
	if (!toolContext?.hasCompletedToolCall) {
		return false;
	}

	const trailingContent = removeAllDetails(content.slice(toolContext.endIndex)).trim();

	return trailingContent.length === 0;
};

export const normalizeToolCallArguments = (value = '') => {
	const decodedValue = decode(value);

	try {
		const parsedValue = JSON.parse(decodedValue);
		return typeof parsedValue === 'string' ? parsedValue : JSON.stringify(parsedValue);
	} catch (error) {
		return decodedValue;
	}
};

export const normalizeToolResultContent = (value = '') => {
	const decodedValue = decode(value);

	try {
		const parsedValue = JSON.parse(decodedValue);
		return typeof parsedValue === 'string' ? parsedValue : JSON.stringify(parsedValue);
	} catch (error) {
		return decodedValue;
	}
};

export const getStringMessageContent = (value) => {
	if (typeof value === 'string') {
		// A handful of corrupted/legacy rows persisted the literal 3-char string
		// "null" (a JSON-encoded None) into `content` instead of a real null —
		// seen in production on a message whose real answer lived entirely in
		// `content_blocks`. Treat it as empty so it never gets mistaken for real
		// assistant text (e.g. by the legacy tool-call HTML regexes below).
		return value === 'null' ? '' : value;
	}

	if (Array.isArray(value)) {
		return value
			.map((part) => {
				if (typeof part === 'string') {
					return part;
				}

				if (part?.type === 'text' && typeof part.text === 'string') {
					return part.text;
				}

				return '';
			})
			.join('\n');
	}

	return '';
};

export const hasMessageContent = (value) => {
	if (typeof value === 'string') {
		// See getStringMessageContent above: the literal string "null" is a
		// corrupted-row artifact, not real content.
		return value.trim().length > 0 && value.trim() !== 'null';
	}

	if (Array.isArray(value)) {
		return value.length > 0;
	}

	return value != null;
};

export const normalizePreservedAssistantContent = (value = '') => {
	const normalizedValue = getStringMessageContent(value);
	return removeDetails(normalizedValue, ['reasoning']).trim();
};

export const expandPreservedToolContextMessage = (message) => {
	if (!message?.preservedToolContext) {
		return [message];
	}

	const content = getStringMessageContent(message.content);
	const toolCallsRegex = /<details\s+type="tool_calls"([^>]*)>[\s\S]*?<\/details>/gi;
	const matches = [...content.matchAll(toolCallsRegex)];

	if (matches.length === 0) {
		return [message];
	}

	// Parse all completed tool calls with their positions
	const parsedToolCalls = [];
	for (const match of matches) {
		const matchStart = match.index ?? 0;
		const matchEnd = matchStart + match[0].length;

		const attributes = {};
		const attributeRegex = /(\w+)="([^"]*)"/g;
		let attributeMatch;
		while ((attributeMatch = attributeRegex.exec(match[1] ?? '')) !== null) {
			attributes[attributeMatch[1]] = attributeMatch[2];
		}

		if (attributes.done === 'true' && attributes.id && attributes.name) {
			parsedToolCalls.push({
				matchStart,
				matchEnd,
				id: attributes.id,
				name: attributes.name,
				arguments: attributes.arguments ?? '',
				result: attributes.result ?? ''
			});
		}
	}

	if (parsedToolCalls.length === 0) {
		return [message];
	}

	// Group consecutive tool calls (no meaningful text between them = parallel calls from same turn)
	const groups = [{ toolCalls: [parsedToolCalls[0]], textBeforeStart: 0 }];

	for (let i = 1; i < parsedToolCalls.length; i++) {
		const prevEnd = parsedToolCalls[i - 1].matchEnd;
		const currStart = parsedToolCalls[i].matchStart;
		const textBetween = normalizePreservedAssistantContent(content.slice(prevEnd, currStart));

		if (textBetween) {
			// Meaningful text between — new group (separate model turn)
			groups.push({ toolCalls: [parsedToolCalls[i]], textBeforeStart: prevEnd });
		} else {
			// No text between — same group (parallel calls from one turn)
			groups[groups.length - 1].toolCalls.push(parsedToolCalls[i]);
		}
	}

	// Compute trailing text (content after the last tool call)
	const lastParsedTc = parsedToolCalls[parsedToolCalls.length - 1];
	const trailingText = normalizePreservedAssistantContent(content.slice(lastParsedTc.matchEnd));
	const hasTrailingText = !!trailingText;

	// Where do reasoning_details belong?
	//
	// Newer messages have `reasoning_details_per_round` — one entry per stream
	// round (tool-call round or the trailing text round). When present, attach
	// per-round entries to the matching tool_calls group (and any final entry
	// to the trailing text). This preserves multi-round reasoning continuity
	// for OpenAI's Responses API.
	//
	// Older messages only have a flat `message.reasoning_details` (the last
	// round's reasoning, since earlier rounds were overwritten by index). Fall
	// back to the legacy heuristic for those:
	// - No trailing text (retry): attach to last group.
	// - Has trailing text: attach only to the trailing text message.

	const reasoningPerRound = Array.isArray(message.reasoning_details_per_round)
		? message.reasoning_details_per_round
		: null;

	const expandedMessages = [];

	for (let groupIdx = 0; groupIdx < groups.length; groupIdx++) {
		const group = groups[groupIdx];
		const isLastGroup = groupIdx === groups.length - 1;
		const firstTc = group.toolCalls[0];
		const textBefore = normalizePreservedAssistantContent(
			content.slice(group.textBeforeStart, firstTc.matchStart)
		);

		let groupReasoningDetails;
		if (reasoningPerRound && reasoningPerRound[groupIdx] !== undefined) {
			groupReasoningDetails = reasoningPerRound[groupIdx];
		} else if (isLastGroup && !hasTrailingText) {
			// Legacy: attach the (single) reasoning_details to the last group
			// when there's no trailing text.
			groupReasoningDetails = message.reasoning_details;
		} else {
			groupReasoningDetails = undefined;
		}

		expandedMessages.push({
			...message,
			role: 'assistant',
			content: textBefore,
			tool_calls: group.toolCalls.map((tc) => ({
				id: tc.id,
				type: 'function',
				function: {
					name: tc.name,
					arguments: normalizeToolCallArguments(tc.arguments)
				}
			})),
			preservedToolContext: undefined,
			reasoning_details: groupReasoningDetails
		});

		// Individual tool result messages
		for (const tc of group.toolCalls) {
			expandedMessages.push({
				role: 'tool',
				tool_call_id: tc.id,
				content: normalizeToolResultContent(tc.result)
			});
		}
	}

	// Trailing text = model's final response after all tool calls completed.
	// Per-round: anything beyond `groups.length` is the final round's reasoning.
	// Legacy: keep `message.reasoning_details` (the spread already includes it).
	if (hasTrailingText) {
		let trailingReasoning;
		if (reasoningPerRound && reasoningPerRound.length > groups.length) {
			trailingReasoning = reasoningPerRound[reasoningPerRound.length - 1];
		} else if (!reasoningPerRound) {
			trailingReasoning = message.reasoning_details;
		} else {
			trailingReasoning = undefined;
		}

		expandedMessages.push({
			...message,
			content: trailingText,
			preservedToolContext: undefined,
			tool_calls: undefined,
			reasoning_details: trailingReasoning
		});
	}

	return expandedMessages;
};

export const expandMessagesForToolResumption = (messages = []) => {
	return messages.flatMap((message) => {
		// Preferred path: assistant messages that carry structured content_blocks
		// pass through as-is. The backend's blocks_to_api_messages converts them
		// to OpenAI shape — the same function the live tool loop uses, so live
		// and replay produce byte-identical messages and prompt caching holds.
		if (
			message?.role === 'assistant' &&
			Array.isArray(message?.content_blocks) &&
			message.content_blocks.length > 0
		) {
			return [message];
		}

		if (message?.preservedToolContext) {
			return expandPreservedToolContextMessage(message);
		}

		// Legacy fallback: assistant messages stored before the content_blocks
		// migration only have tool-call HTML in their content string. Recover
		// tool_calls by parsing the HTML — drift-prone, hence the migration.
		if (message?.role === 'assistant' && !message?.tool_calls) {
			const content = getStringMessageContent(message.content ?? '');
			if (/<details\s+type="tool_calls"[^>]+done="true"/.test(content)) {
				return expandPreservedToolContextMessage({ ...message, preservedToolContext: true });
			}
		}

		return [message];
	});
};
