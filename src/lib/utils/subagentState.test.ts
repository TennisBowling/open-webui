import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';

import {
	activeSubagentRerunEntryKeys,
	activeSubagentStreamMessageIds,
	compareSubagentRerunGeneration,
	countUniqueSubagentContinuations,
	findSubagentRunEntry,
	hasActiveDetachedSubagentRerun,
	isFreshRerunResult,
	seedPersistedSubagentRuns,
	setSubagentRunAliases,
	shouldApplyIncomingSubagentGeneration,
	shouldParentFinalizeSubagentRun,
	shouldApplyRerunOptimisticState,
	subagentRunHasActiveRerunKey,
	subagentScopedStateKey
} from './subagentState';

const failed = {
	parent_message_id: 'parent-old',
	entry_key: 'child-1',
	tool_call_id: 'call-1',
	subagent_id: 'child-1',
	chat_id: 'child-1',
	status: 'error'
};

const repaired = {
	...failed,
	parent_message_id: 'parent-repaired',
	status: 'done',
	final_text: 'repaired answer'
};

describe('subagent state branch scoping', () => {
	it('keeps contradictory rewind siblings independently addressable', () => {
		const states: Record<string, any> = {};
		setSubagentRunAliases(states, failed);
		setSubagentRunAliases(states, repaired);

		expect(findSubagentRunEntry(states, 'parent-old', ['call-1'])?.[1]).toBe(failed);
		expect(findSubagentRunEntry(states, 'parent-repaired', ['call-1'])?.[1]).toBe(repaired);
		expect(states[subagentScopedStateKey('parent-old', 'call-1')]).toBe(failed);
		expect(states[subagentScopedStateKey('parent-repaired', 'call-1')]).toBe(repaired);
	});

	it('never accepts another parent message through a legacy raw alias', () => {
		const states = { 'call-1': failed };

		expect(findSubagentRunEntry(states, 'parent-repaired', ['call-1'], { scan: false })).toBeNull();
		expect(findSubagentRunEntry(states, 'parent-old', ['call-1'], { scan: false })?.[1]).toBe(
			failed
		);
	});

	it('does not let hydration order overwrite a legacy alias across parents', () => {
		const states: Record<string, any> = {};
		setSubagentRunAliases(states, failed);
		setSubagentRunAliases(states, repaired);

		expect(states['call-1']).toBe(failed);
		expect(findSubagentRunEntry(states, 'parent-repaired', ['child-1'])?.[1]).toBe(repaired);
	});

	it('counts aliased/carried continuation runs once', () => {
		const continuation = {
			parent_message_id: 'parent-old',
			entry_key: 'child-1#continue-call',
			tool_call_id: 'continue-call',
			subagent_id: 'child-1',
			chat_id: 'child-1',
			assistant_msg_id: 'continue-answer',
			continuation: true
		};
		const carried = { ...continuation, parent_message_id: 'parent-repaired' };
		const states: Record<string, any> = {};
		setSubagentRunAliases(states, continuation);
		setSubagentRunAliases(states, carried);

		expect(Object.values(states).length).toBeGreaterThan(1);
		expect(countUniqueSubagentContinuations(states, 'child-1')).toBe(1);
		expect(countUniqueSubagentContinuations(states, 'another-child')).toBe(0);
	});

	it('seeds durable sibling outcomes under the containing message scope', () => {
		const states = seedPersistedSubagentRuns({
			'parent-old': {
				id: 'parent-old',
				subagent_runs: {
					'child-1': failed,
					malformed: null
				}
			},
			'parent-repaired': {
				id: 'parent-repaired',
				subagent_runs: {
					// Simulate an older copied row whose embedded attribution was
					// never rewritten. The containing message still owns the run.
					'child-1': { ...repaired, parent_message_id: 'parent-old' }
				}
			},
			ignored: { id: 'ignored', subagent_runs: [] }
		});

		expect(findSubagentRunEntry(states, 'parent-old', ['child-1'])?.[1].status).toBe('error');
		expect(findSubagentRunEntry(states, 'parent-repaired', ['child-1'])?.[1].status).toBe('done');
		expect(
			findSubagentRunEntry(states, 'parent-repaired', ['child-1'])?.[1].parent_message_id
		).toBe('parent-repaired');
	});
});

describe('subagent rerun generation identity', () => {
	it('does not overwrite a fast terminal socket result with late optimistic running state', () => {
		const terminal = { rerun_id: 'attempt-2', status: 'done', final_text: 'fresh' };

		expect(shouldApplyRerunOptimisticState(terminal, 'attempt-2')).toBe(false);
		expect(shouldApplyRerunOptimisticState(terminal, 'attempt-3')).toBe(true);
	});

	it('uses rerun_id instead of ambiguous second-resolution timestamps', () => {
		const run = { rerun_id: 'attempt-2', status: 'done', ended_at: 100 };

		// Same ended_at as the previous attempt is still fresh when the
		// generation id proves which rerun produced it.
		expect(isFreshRerunResult(run, 'attempt-2', 100)).toBe(true);
		expect(isFreshRerunResult(run, 'attempt-1', 99)).toBe(false);
		// Compatibility fallback for an older server with no rerun_id.
		expect(isFreshRerunResult({ status: 'done', ended_at: 101 }, undefined, 100)).toBe(true);
	});

	it('orders same-second reruns by their persisted monotonic attempt', () => {
		const oldRun = {
			rerun_id: 'attempt-id-1',
			rerun_attempt: 7,
			started_at: 100
		};
		const newRun = {
			rerun_id: 'attempt-id-2',
			rerun_attempt: 8,
			started_at: 100
		};

		expect(compareSubagentRerunGeneration(oldRun, newRun)).toBe(1);
		expect(compareSubagentRerunGeneration(newRun, oldRun)).toBe(-1);
		expect(compareSubagentRerunGeneration(newRun, { ...newRun })).toBe(0);
	});

	it('fails closed when a differing incoming generation cannot be ordered', () => {
		const oldRun = {
			rerun_id: 'attempt-id-1',
			rerun_attempt: 7,
			started_at: 100
		};
		const newRun = {
			rerun_id: 'attempt-id-2',
			rerun_attempt: 8,
			started_at: 100
		};

		expect(shouldApplyIncomingSubagentGeneration(oldRun, newRun)).toBe(true);
		expect(shouldApplyIncomingSubagentGeneration(newRun, oldRun)).toBe(false);
		expect(
			shouldApplyIncomingSubagentGeneration(
				{ rerun_id: 'ambiguous-1', started_at: 100 },
				{ rerun_id: 'ambiguous-2', started_at: 100 }
			)
		).toBe(false);
		expect(shouldApplyIncomingSubagentGeneration(newRun, { ...newRun })).toBe(true);
	});

	it('normalizes the consolidated reload rerun ownership contract', () => {
		const active = {
			subagent_rerun_entry_keys: ['child-1', '', null, 'child-1#continue-1']
		};
		const keys = activeSubagentRerunEntryKeys(active);

		expect([...keys]).toEqual(['child-1', 'child-1#continue-1']);
		expect(
			subagentRunHasActiveRerunKey(keys, 'unrelated', {
				subagent_id: 'child-1',
				tool_call_id: 'call-1'
			})
		).toBe(true);
		expect(activeSubagentRerunEntryKeys(undefined).size).toBe(0);
	});

	it('derives active stream ownership only from the chat-open bundle', () => {
		const ids = activeSubagentStreamMessageIds({
			streams: [
				{ message_id: 'parent-1' },
				{ message_id: '' },
				{ message_id: 'parent-1' },
				{ message_id: 'parent-2' },
				null
			]
		});

		expect([...ids]).toEqual(['parent-1', 'parent-2']);
		expect(activeSubagentStreamMessageIds({ streams: 'malformed' }).size).toBe(0);
	});

	it('keeps detached reruns out of an overlapping parent finalizer', () => {
		const inline = { parent_message_id: 'parent-1', status: 'running' };
		const detached = { ...inline, rerun: true, rerun_id: 'attempt-id-1' };

		expect(shouldParentFinalizeSubagentRun(inline, 'parent-1')).toBe(true);
		expect(shouldParentFinalizeSubagentRun(detached, 'parent-1')).toBe(false);
		expect(shouldParentFinalizeSubagentRun(inline, 'parent-2')).toBe(false);
	});

	it('detects active detached reruns without confusing inline children', () => {
		expect(
			hasActiveDetachedSubagentRerun({
				inline: { status: 'running', ended_at: null },
				rerun: {
					status: 'running',
					ended_at: null,
					rerun_id: 'generation-2'
				}
			})
		).toBe(true);
		expect(
			hasActiveDetachedSubagentRerun({
				inline: { status: 'running', ended_at: null },
				done: { status: 'done', rerun: true, ended_at: 123 }
			})
		).toBe(false);
	});
});

describe('chat reload hydration ownership', () => {
	it('does not reach into post-paint reconciler locals', () => {
		const chatSource = readFileSync(
			new URL('../components/chat/Chat.svelte', import.meta.url),
			'utf8'
		);
		const hydrationStart = chatSource.indexOf(
			'// Hydrate the subagent live-state store with anything persisted'
		);
		const hydrationEnd = chatSource.indexOf(
			'// Re-arm follow-to-bottom only on a genuine navigation/open.',
			hydrationStart
		);
		expect(hydrationStart).toBeGreaterThan(0);
		expect(hydrationEnd).toBeGreaterThan(hydrationStart);
		const hydration = chatSource.slice(hydrationStart, hydrationEnd);

		expect(hydration).toContain('activeSubagentStreamMessageIds(loadActiveState)');
		expect(hydration).toContain('activeSubagentRerunEntryKeys(loadActiveState)');
		expect(hydration).not.toContain('_taskRes?.');
		expect(hydration).not.toContain('new Set(activeStreamMessageIds)');
		expect(hydration).not.toContain('_activeStreamsRes?.');
		expect(hydration).toContain('subagentLiveStates.set(seeded)');
	});
});

describe('manual subagent repair workflow', () => {
	it('uses one atomic rewind commit instead of the legacy direct-adopt race', () => {
		const cardSource = readFileSync(
			new URL('../components/chat/Messages/Markdown/SubagentBlock.svelte', import.meta.url),
			'utf8'
		);
		const chatSource = readFileSync(
			new URL('../components/chat/Chat.svelte', import.meta.url),
			'utf8'
		);
		const apiSource = readFileSync(new URL('../apis/subagents/index.ts', import.meta.url), 'utf8');

		expect(cardSource).toContain(
			"new CustomEvent(action === 'adopt' ? 'subagentrewindadopt' : 'subagentrewindredo'"
		);
		expect(cardSource).not.toContain('adoptSubagentResult');
		expect(cardSource).not.toContain('subagent:result-adopted');
		expect(chatSource).toContain('rewindAdoptSubagentResults(');
		expect(chatSource).not.toContain('resumeParentAfterAdoptedResult');
		expect(apiSource).not.toContain('/adopt`,');
		expect(apiSource).toContain('/adopt/rewind');
	});
});
