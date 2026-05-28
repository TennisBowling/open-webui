type RetryContext = {
	content: string;
	content_blocks: any[];
	reasoning_details_per_round?: any[];
};

const cloneValue = <T>(value: T): T => {
	if (typeof structuredClone === 'function') return structuredClone(value);
	return JSON.parse(JSON.stringify(value));
};

const hasOwn = (value: object, key: string) => Object.prototype.hasOwnProperty.call(value, key);

const getToolCallId = (call: any) => call?.id ?? call?.tool_call_id ?? '';

const toolResultHasBody = (result: any) =>
	!!result &&
	typeof result === 'object' &&
	!Array.isArray(result) &&
	(hasOwn(result, 'content') || hasOwn(result, 'result_ref'));

const isCompletedToolCallsBlock = (block: any) => {
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
		content_blocks: cloneValue(blocks.slice(0, lastCompletedToolBlockIndex + 1))
	};

	if (Array.isArray(message?.reasoning_details_per_round)) {
		context.reasoning_details_per_round = cloneValue(
			message.reasoning_details_per_round.slice(0, toolRoundCountAtLastCompletedBlock)
		);
	}

	return context;
};
