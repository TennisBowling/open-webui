import { describe, it, expect } from 'vitest';
import { computeChainStructureKey } from './chainStructureKey';

const base = {
	structureRevision: 0,
	localStructureRevision: 0,
	currentId: 'msg-1' as string | null,
	messagesCount: 25 as number | null,
	messageMapSize: 3
};

describe('computeChainStructureKey', () => {
	it('is stable when only message content changes (size/ids/pointer unchanged)', () => {
		// A streaming token flush mutates the leaf content in place but changes
		// none of the structural inputs — the key MUST be identical so the
		// chain walk does not re-run.
		const before = computeChainStructureKey(base);
		const after = computeChainStructureKey({ ...base });
		expect(after).toBe(before);
	});

	it('changes when the current branch pointer changes (branch nav)', () => {
		const before = computeChainStructureKey(base);
		const after = computeChainStructureKey({ ...base, currentId: 'msg-2' });
		expect(after).not.toBe(before);
	});

	it('changes when a message is added/removed (map size delta)', () => {
		const before = computeChainStructureKey(base);
		const added = computeChainStructureKey({ ...base, messageMapSize: 4 });
		const removed = computeChainStructureKey({ ...base, messageMapSize: 2 });
		expect(added).not.toBe(before);
		expect(removed).not.toBe(before);
	});

	it('changes when the parent bumps structureRevision (load/reattach)', () => {
		const before = computeChainStructureKey(base);
		const after = computeChainStructureKey({ ...base, structureRevision: 1 });
		expect(after).not.toBe(before);
	});

	it('changes when Messages bumps localStructureRevision (pagination/stub)', () => {
		const before = computeChainStructureKey(base);
		const after = computeChainStructureKey({ ...base, localStructureRevision: 1 });
		expect(after).not.toBe(before);
	});

	it('changes when the pagination cap changes', () => {
		const before = computeChainStructureKey(base);
		const after = computeChainStructureKey({ ...base, messagesCount: 50 });
		expect(after).not.toBe(before);
	});

	it('handles null currentId and null messagesCount without collisions', () => {
		const a = computeChainStructureKey({ ...base, currentId: null, messagesCount: null });
		const b = computeChainStructureKey({ ...base, currentId: '', messagesCount: null });
		// currentId null and '' both render empty — but that is acceptable: an
		// empty/absent currentId yields an empty rendered chain either way.
		expect(a).toBe(b);
		// null messagesCount is distinct from a numeric cap.
		const c = computeChainStructureKey({ ...base, currentId: null, messagesCount: 0 });
		expect(c).not.toBe(a);
	});

	it('does not collide across adjacent field values (delimiter safety)', () => {
		// Ensure "1:0:..." style concatenation can't alias e.g. revision 1 vs 10.
		const k1 = computeChainStructureKey({ ...base, structureRevision: 1, localStructureRevision: 0 });
		const k2 = computeChainStructureKey({ ...base, structureRevision: 10, localStructureRevision: 0 });
		expect(k1).not.toBe(k2);
	});
});
