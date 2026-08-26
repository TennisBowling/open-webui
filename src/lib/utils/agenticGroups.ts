// Grouping logic for the "Working for X" agentic-step bundling feature.
//
// A model's agentic turn is a flat array of content_blocks:
//   reasoning, tool_calls, reasoning, tool_calls, …, text (final answer)
// with occasional commentary `text` blocks the model emits between tool calls.
//
// We bundle each *contiguous burst* of agentic work into one collapsible
// "Working" group. A burst absorbs reasoning, tool_calls, and the empty
// placeholder `text` blocks the backend appends after each tool round. A
// NON-empty text block is real prose (commentary or the final answer) — it
// renders inline and ends the current burst, so work→commentary→work yields
// two separate groups.
//
// A group must contain at least one `tool_calls` block. A run of pure
// reasoning (e.g. a reasoning model that thinks then answers with no tools)
// renders inline as today's familiar "Thought for X" collapsible — wrapping it
// in a Working card would nest a redundant second timer. Reasoning that is
// interleaved with tool calls is still absorbed into the group.
//
// This is a pure function so the core logic is unit-tested without the DOM.

export type AgenticRenderItem =
	| { kind: 'block'; index: number }
	| { kind: 'group'; indices: number[] };

const isEmptyText = (block: any): boolean =>
	block?.type === 'text' && `${block?.content ?? ''}`.trim().length === 0;

const isAgentic = (block: any): boolean =>
	block?.type === 'reasoning' || block?.type === 'tool_calls';

// A tool_calls block that contains an `ask_user` call renders an interactive
// question card the user must see and act on. It must NOT be absorbed into a
// collapsible "Working" group (those auto-collapse when finished, hiding the
// card) — so it breaks the burst and renders standalone, like a user_steer.
const hasAskUserCall = (block: any): boolean =>
	block?.type === 'tool_calls' &&
	Array.isArray(block?.content) &&
	block.content.some((call: any) => call?.function?.name === 'ask_user');

// A tool_calls block that contains a `show_widget` call renders an inline
// data-viz visualization (a sandboxed-iframe widget) that is the whole point of
// the call — the user must SEE it. Absorbing it into a "Working" card would
// auto-collapse and hide the visualization once the burst finishes, so it breaks
// the burst and renders standalone, exactly like an ask_user card.
const hasShowWidgetCall = (block: any): boolean =>
	block?.type === 'tool_calls' &&
	Array.isArray(block?.content) &&
	block.content.some((call: any) => call?.function?.name === 'show_widget');

// Tool-call blocks that must always render standalone (never buried in a
// collapsible group) because they carry user-facing UI the user must see.
const mustRenderStandalone = (block: any): boolean =>
	hasAskUserCall(block) || hasShowWidgetCall(block);

// Decide how to render a message's content_blocks.
//
// `enabled === false` reproduces the legacy flat layout exactly: one
// standalone block per index, in order.
export const computeAgenticRenderItems = (
	blocks: any[],
	enabled: boolean
): AgenticRenderItem[] => {
	const list = Array.isArray(blocks) ? blocks : [];
	if (!enabled) {
		return list.map((_, index) => ({ kind: 'block', index }));
	}

	const items: AgenticRenderItem[] = [];
	let pending: number[] = []; // indices accumulating into the current burst
	let pendingHasToolCalls = false;

	const flush = () => {
		if (pending.length === 0) return;
		if (pendingHasToolCalls) {
			items.push({ kind: 'group', indices: pending });
		} else {
			// No tool call in this run (pure reasoning and/or empty placeholders).
			// Emit each index standalone: reasoning renders as its normal
			// collapsible, empty text renders nothing — matching legacy behavior.
			for (const index of pending) items.push({ kind: 'block', index });
		}
		pending = [];
		pendingHasToolCalls = false;
	};

	for (let i = 0; i < list.length; i++) {
		const block = list[i];
		if (mustRenderStandalone(block)) {
			// A card the user must see and possibly act on (ask_user question,
			// show_widget visualization). Close any open burst and emit it on its
			// own, like a user_steer, so a collapsing Working card can't hide it.
			flush();
			items.push({ kind: 'block', index: i });
		} else if (isAgentic(block)) {
			pending.push(i);
			if (block?.type === 'tool_calls') pendingHasToolCalls = true;
		} else if (isEmptyText(block)) {
			// Absorb the post-tool placeholder into the current burst. If no burst
			// is open yet it still accumulates, and flush() emits it standalone
			// (renders nothing) when the run has no tool call.
			pending.push(i);
		} else {
			// Real prose (commentary or final answer): close the burst, emit inline.
			flush();
			items.push({ kind: 'block', index: i });
		}
	}
	flush();

	return items;
};
