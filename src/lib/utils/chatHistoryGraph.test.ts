import { describe, expect, it } from 'vitest';

import { buildHistoryGraph, findDeepestBranchLeaf, getActiveHistoryPath } from './chatHistoryGraph';

describe('chat history branch traversal', () => {
	it('keeps prompt and response versions independently reachable', () => {
		const messages = {
			root: { id: 'root', parentId: null, childrenIds: ['prompt-old', 'prompt-new'] },
			'prompt-old': { id: 'prompt-old', parentId: 'root', childrenIds: ['answer-a', 'answer-b'] },
			'answer-a': { id: 'answer-a', parentId: 'prompt-old', childrenIds: [] },
			'answer-b': { id: 'answer-b', parentId: 'prompt-old', childrenIds: ['follow-up'] },
			'follow-up': { id: 'follow-up', parentId: 'answer-b', childrenIds: [] },
			'prompt-new': { id: 'prompt-new', parentId: 'root', childrenIds: [] }
		};

		expect(findDeepestBranchLeaf(messages, 'answer-a')).toBe('answer-a');
		expect(findDeepestBranchLeaf(messages, 'answer-b')).toBe('follow-up');
		expect(findDeepestBranchLeaf(messages, 'prompt-old')).toBe('follow-up');
		expect(findDeepestBranchLeaf(messages, 'prompt-new')).toBe('prompt-new');
	});

	it('stops safely at missing children and cycles', () => {
		const messages = {
			a: { id: 'a', parentId: 'b', childrenIds: ['missing', 'b'] },
			b: { id: 'b', parentId: 'a', childrenIds: [] }
		};

		expect(findDeepestBranchLeaf(messages, 'a')).toBe('b');
		expect(getActiveHistoryPath(messages, 'b')).toEqual(new Set(['b', 'a']));
	});

	it('discovers children from parent links when a cached child list is stale', () => {
		const messages = {
			root: { id: 'root', parentId: null, childrenIds: [] },
			child: { id: 'child', parentId: 'root', childrenIds: [] }
		};

		expect(findDeepestBranchLeaf(messages, 'root')).toBe('child');
	});
});

describe('chat overview graph', () => {
	it('lays out child-before-parent input and preserves orphan rows', () => {
		const graph = buildHistoryGraph(
			{
				child: { id: 'child', parentId: 'parent', childrenIds: [] },
				orphan: { id: 'orphan', parentId: 'gone', childrenIds: [] },
				parent: { id: 'parent', parentId: null, childrenIds: ['child'] }
			},
			'child'
		);

		expect(graph.nodes).toHaveLength(3);
		expect(graph.nodes.find((node) => node.id === 'child')?.depth).toBe(1);
		expect(graph.nodes.find((node) => node.id === 'orphan')?.orphaned).toBe(true);
		expect(graph.nodes.find((node) => node.id === 'orphan')?.versionCount).toBe(1);
		expect(graph.activePathIds).toEqual(new Set(['child', 'parent']));
		expect(graph.edges).toEqual([{ source: 'parent', target: 'child' }]);
	});

	it('reports version positions from authoritative parent links', () => {
		const graph = buildHistoryGraph(
			{
				root: { id: 'root', parentId: null, childrenIds: [] },
				one: { id: 'one', parentId: 'root', childrenIds: [] },
				two: { id: 'two', parentId: 'root', childrenIds: [] }
			},
			'two'
		);

		expect(graph.branchPointCount).toBe(1);
		expect(graph.nodes.find((node) => node.id === 'one')).toMatchObject({
			versionIndex: 1,
			versionCount: 2
		});
		expect(graph.nodes.find((node) => node.id === 'two')).toMatchObject({
			versionIndex: 2,
			versionCount: 2
		});
	});

	it('lays out a very deep history without recursive-stack growth', () => {
		const messages: Record<string, any> = {};
		for (let index = 0; index < 20_000; index++) {
			messages[`message-${index}`] = {
				id: `message-${index}`,
				parentId: index === 0 ? null : `message-${index - 1}`
			};
		}

		const graph = buildHistoryGraph(messages, 'message-19999');

		expect(graph.nodes).toHaveLength(20_000);
		expect(graph.activePathIds.size).toBe(20_000);
		expect(graph.nodes.at(-1)?.depth).toBe(19_999);
	});
});
