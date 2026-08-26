export type ChatHistoryMessage = {
	id?: string;
	parentId?: string | null;
	childrenIds?: string[];
	role?: string;
	[key: string]: any;
};

export type ChatHistoryMessages = Record<string, ChatHistoryMessage>;

export type HistoryGraphNode = {
	id: string;
	message: ChatHistoryMessage;
	depth: number;
	column: number;
	parentId: string | null;
	childrenIds: string[];
	orphaned: boolean;
	cyclic: boolean;
	versionIndex: number;
	versionCount: number;
};

export type HistoryGraph = {
	nodes: HistoryGraphNode[];
	edges: Array<{ source: string; target: string }>;
	activePathIds: Set<string>;
	branchPointCount: number;
	orphanCount: number;
};

export const buildHistoryChildrenIndex = (messages: ChatHistoryMessages): Map<string, string[]> => {
	const index = new Map<string, string[]>();
	for (const [id, child] of Object.entries(messages ?? {})) {
		if (child?.parentId != null && child.parentId !== id) {
			const children = index.get(child.parentId) ?? [];
			children.push(id);
			index.set(child.parentId, children);
		}
	}
	return index;
};

const orderedChildIds = (messageId: string, parentChildIndex: Map<string, string[]>): string[] =>
	parentChildIndex.get(messageId) ?? [];

export const getOrderedChildIds = (
	messages: ChatHistoryMessages,
	messageId: string,
	childrenIndex?: Map<string, string[]>
): string[] => orderedChildIds(messageId, childrenIndex ?? buildHistoryChildrenIndex(messages));

/**
 * Resolve the leaf selected by a branch-version click.
 *
 * Parent links are ordered oldest -> newest by message-map insertion order, so
 * the final child is the branch the existing UI has historically selected.
 * Missing ancestors and cycles are tolerated: the deepest valid node remains
 * selectable instead of throwing and leaving the branch pointer unchanged.
 */
export const findDeepestBranchLeaf = (
	messages: ChatHistoryMessages,
	startId: string | null | undefined
): string | null => {
	if (!startId || !messages?.[startId]) return null;
	const seen = new Set<string>();
	const parentChildIndex = buildHistoryChildrenIndex(messages);
	let currentId = startId;

	while (messages[currentId] && !seen.has(currentId)) {
		seen.add(currentId);
		const children = orderedChildIds(currentId, parentChildIndex);
		let nextId: string | undefined;
		for (let index = children.length - 1; index >= 0; index--) {
			if (!seen.has(children[index])) {
				nextId = children[index];
				break;
			}
		}
		if (!nextId) break;
		currentId = nextId;
	}

	return currentId;
};

export const getActiveHistoryPath = (
	messages: ChatHistoryMessages,
	currentId: string | null | undefined
): Set<string> => {
	const active = new Set<string>();
	let cursor = currentId ?? null;
	while (cursor && messages?.[cursor] && !active.has(cursor)) {
		active.add(cursor);
		const parentId = messages[cursor]?.parentId;
		cursor = parentId && parentId !== cursor ? parentId : null;
	}
	return active;
};

/**
 * Build a deterministic forest layout from parent links. It does not depend
 * on object insertion order being parent-before-child and deliberately keeps
 * orphan/cyclic rows in the result as separate roots so preserved responses
 * can never disappear merely because one ancestry edge is damaged.
 */
export const buildHistoryGraph = (
	messages: ChatHistoryMessages,
	currentId: string | null | undefined
): HistoryGraph => {
	const ids = Object.keys(messages ?? {}).filter((id) => !!messages[id]);
	const order = new Map(ids.map((id, index) => [id, index]));
	const childIndex = new Map<string, string[]>();
	const validParent = new Map<string, string | null>();
	const orphaned = new Set<string>();

	for (const id of ids) childIndex.set(id, []);
	for (const id of ids) {
		const parentId = messages[id]?.parentId ?? null;
		if (parentId && parentId !== id && messages[parentId]) {
			validParent.set(id, parentId);
			childIndex.get(parentId)?.push(id);
		} else {
			validParent.set(id, null);
			if (parentId) orphaned.add(id);
		}
	}

	for (const children of childIndex.values()) {
		children.sort((a, b) => (order.get(a) ?? 0) - (order.get(b) ?? 0));
	}

	const roots = ids.filter((id) => validParent.get(id) === null);
	const naturalRoots = ids.filter((id) => messages[id]?.parentId == null);
	const versionInfo = new Map<string, { index: number; count: number }>();
	for (const children of childIndex.values()) {
		children.forEach((childId, index) => {
			versionInfo.set(childId, { index: index + 1, count: children.length });
		});
	}
	naturalRoots.forEach((rootId, index) => {
		versionInfo.set(rootId, { index: index + 1, count: naturalRoots.length });
	});
	const visited = new Set<string>();
	const cyclic = new Set<string>();
	const positioned = new Map<string, { depth: number; column: number }>();
	let nextColumn = 0;

	const placeComponent = (rootId: string) => {
		const active = new Set<string>();
		const stack: Array<{ id: string; depth: number; expanded: boolean }> = [
			{ id: rootId, depth: 0, expanded: false }
		];

		while (stack.length > 0) {
			const frame = stack.pop()!;
			if (positioned.has(frame.id)) {
				active.delete(frame.id);
				continue;
			}
			if (frame.expanded) {
				active.delete(frame.id);
				const childColumns = (childIndex.get(frame.id) ?? [])
					.map((childId) => positioned.get(childId)?.column)
					.filter((column): column is number => column !== undefined);
				const column =
					childColumns.length === 0 ? nextColumn++ : (childColumns[0] + childColumns.at(-1)!) / 2;
				positioned.set(frame.id, { depth: frame.depth, column });
				continue;
			}

			visited.add(frame.id);
			active.add(frame.id);
			stack.push({ ...frame, expanded: true });
			const children = childIndex.get(frame.id) ?? [];
			for (let index = children.length - 1; index >= 0; index--) {
				const childId = children[index];
				if (active.has(childId)) {
					cyclic.add(frame.id);
					cyclic.add(childId);
					continue;
				}
				if (!positioned.has(childId)) {
					stack.push({ id: childId, depth: frame.depth + 1, expanded: false });
				}
			}
		}
	};

	for (const rootId of roots) placeComponent(rootId);
	// A component made entirely of a parent cycle has no natural root. Start it
	// explicitly so all of its rows remain visible in the overview.
	for (const id of ids) {
		if (!visited.has(id)) {
			orphaned.add(id);
			cyclic.add(id);
			placeComponent(id);
		}
	}

	const nodes = ids.map((id) => {
		const parentId = validParent.get(id) ?? null;
		const versions = versionInfo.get(id) ?? { index: 1, count: 1 };
		const position = positioned.get(id) ?? { depth: 0, column: nextColumn++ };
		return {
			id,
			message: { id, ...messages[id] },
			depth: position.depth,
			column: position.column,
			parentId,
			childrenIds: childIndex.get(id) ?? [],
			orphaned: orphaned.has(id),
			cyclic: cyclic.has(id),
			versionIndex: versions.index,
			versionCount: versions.count
		};
	});

	const edges = nodes
		.filter((node) => node.parentId && messages[node.parentId])
		.map((node) => ({ source: node.parentId!, target: node.id }));

	return {
		nodes,
		edges,
		activePathIds: getActiveHistoryPath(messages, currentId),
		branchPointCount: nodes.filter((node) => node.childrenIds.length > 1).length,
		orphanCount: new Set([...orphaned, ...cyclic]).size
	};
};
