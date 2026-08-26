// Offline chat store — IndexedDB-backed persistence for chat payloads so
// previously-opened chats can render with zero network bytes when offline or
// on terrible cell connections (see plan-v4-final.md WS-A).
//
// Hard invariants:
// - This module must NEVER throw into a caller. IDB can fail for many
//   reasons (private browsing, quota exceeded, corrupt DB, browser bugs) and
//   the app must silently degrade to network-only behavior in every case.
// - `pinned` replaces the earlier "complete" concept: this store never
//   contains tool_result_bodies regardless of how an entry was fetched (the
//   backend strips them from every non-message-scoped chat response), so an
//   entry is never "complete" in a strict sense. `pinned=true` only means
//   "the user explicitly asked to keep this chat available offline" (evict
//   last); `pinned=false` means "auto-cached opportunistically" (evict
//   first).
// - Never overwrite a newer `updatedAt` with an older one (compare-and-set
//   inside a single transaction) — safe across multiple tabs.

import { openDB, type IDBPDatabase } from 'idb';

const DB_NAME = 'owui-offline';
const DB_VERSION = 1;
const STORE_NAME = 'chats';
const BY_USER_STORED_INDEX = 'by-user-stored';

// Two-class eviction cap: once an individual user's auto-cached + pinned
// entries together exceed this count, auto-cached entries are evicted first
// (oldest storedAt), then pinned entries only if that alone isn't enough.
const MAX_ENTRIES_PER_USER = 200;

export type OfflineChatEntry = {
	userId: string;
	chatId: string;
	updatedAt: number;
	storedAt: number;
	data: any;
	tags: any[];
	pinned: boolean;
	approxSize: number;
	// Server-minted opaque validator from the tail response's ETag header.
	// Replayed as If-None-Match on the next open so an unchanged chat costs a
	// ~300-byte 304 instead of a full tail download. Absent on entries stored
	// before this existed (or from old servers) — they stay unconditional
	// until the next 200 upgrades them.
	etag?: string;
};

// What putChat accepts: the raw body; approxSize is computed here at pack time.
export type OfflineChatWrite = Omit<OfflineChatEntry, 'approxSize'>;

// What actually sits in IDB: exactly one of `data` (uncompressed, legacy /
// no-CompressionStream fallback) or `dataGz` (gzipped JSON bytes). getChat
// materializes `data` on the way out so callers never see the difference.
type StoredChatRecord = Omit<OfflineChatEntry, 'data'> & {
	data?: any;
	dataGz?: Uint8Array;
};

export type OfflineChatMeta = {
	updatedAt: number;
	storedAt: number;
	pinned: boolean;
	etag?: string;
};

// gzip via the native streams API — Safari 16.4+, Chrome 80+, Firefox 113+.
// Older browsers fall back to storing the uncompressed object; the read path
// accepts both representations forever, so no migration is ever needed.
const hasCompressionStreams =
	typeof CompressionStream === 'function' && typeof DecompressionStream === 'function';

// Serialize (+ gzip when available) a chat body. JSON.stringify runs
// synchronously at call time, which gives snapshot semantics for live,
// in-place-mutated chat objects and drops non-enumerable markers
// (__tailTags/__offlineCopy) — the same guarantees the structuredClone it
// replaced provided. Returns null for non-serializable input.
async function packChatData(
	value: any
): Promise<{ data?: any; dataGz?: Uint8Array; approxSize: number } | null> {
	let json: string;
	try {
		json = JSON.stringify(value);
	} catch {
		return null;
	}
	if (typeof json !== 'string') return null;

	if (hasCompressionStreams) {
		try {
			const stream = new Blob([json]).stream().pipeThrough(new CompressionStream('gzip'));
			const bytes = new Uint8Array(await new Response(stream).arrayBuffer());
			return { dataGz: bytes, approxSize: bytes.byteLength };
		} catch (err) {
			console.error('offline chatStore: gzip failed, storing uncompressed', err);
		}
	}
	// Parse back so the stored object is decoupled from the caller's live one.
	return { data: JSON.parse(json), approxSize: json.length };
}

// Materialize the body of a stored record; null when the record is corrupt
// (undecodable gzip) or empty.
async function unpackChatData(record: StoredChatRecord): Promise<any | null> {
	if (record.data !== undefined) return record.data;
	if (record.dataGz) {
		try {
			const stream = new Blob([record.dataGz])
				.stream()
				.pipeThrough(new DecompressionStream('gzip'));
			const json = await new Response(stream).text();
			return JSON.parse(json);
		} catch (err) {
			console.error('offline chatStore: gunzip failed', err);
			return null;
		}
	}
	return null;
}

let dbPromise: Promise<IDBPDatabase> | null = null;

const getDb = (): Promise<IDBPDatabase> => {
	if (dbPromise) return dbPromise;

	dbPromise = openDB(DB_NAME, DB_VERSION, {
		upgrade(db) {
			if (!db.objectStoreNames.contains(STORE_NAME)) {
				const store = db.createObjectStore(STORE_NAME, {
					keyPath: ['userId', 'chatId']
				});
				store.createIndex(BY_USER_STORED_INDEX, ['userId', 'storedAt']);
			}
		}
	}).catch((err) => {
		console.error('offline chatStore: failed to open IndexedDB', err);
		// Reset so a later call can retry (e.g. transient failure) instead of
		// permanently caching a rejected promise.
		dbPromise = null;
		throw err;
	});

	return dbPromise;
};

// Compare-and-set write: only writes when there is no existing entry, or the
// existing entry's updatedAt is not newer than the incoming one. Runs inside
// a single readwrite transaction so it's safe across concurrent tabs.
// `pinned` is sticky: an auto-cache rewrite (pinned=false) of an entry the
// user explicitly pinned must not demote it — demotion goes through
// setPinned() only.
// Returns the meta of whatever the store holds AFTER the call (the incoming
// entry, or the newer existing one the CAS kept), so callers can update
// reactive mirrors without re-reading the full body. Null on failure.
export async function putChat(entry: OfflineChatWrite): Promise<OfflineChatMeta | null> {
	try {
		// Pack (stringify + gzip) BEFORE opening the transaction: awaiting a
		// non-IDB promise inside it would auto-commit and break the CAS.
		const packed = await packChatData(entry.data);
		if (!packed) return null;
		const { data: _rawData, ...metaFields } = entry;
		const record: StoredChatRecord = {
			...metaFields,
			approxSize: packed.approxSize,
			...(packed.dataGz ? { dataGz: packed.dataGz } : { data: packed.data })
		};

		const db = await getDb();
		const tx = db.transaction(STORE_NAME, 'readwrite');
		const store = tx.objectStore(STORE_NAME);
		const existing = await store.get([entry.userId, entry.chatId]);

		let stored: StoredChatRecord = existing ?? record;
		if (!existing || record.updatedAt >= existing.updatedAt) {
			stored = { ...record, pinned: record.pinned || !!existing?.pinned };
			await store.put(stored);
		}

		await tx.done;
		return {
			updatedAt: stored.updatedAt,
			storedAt: stored.storedAt,
			pinned: !!stored.pinned,
			etag: stored.etag
		};
	} catch (err) {
		console.error('offline chatStore: putChat failed', err);
		return null;
	}
}

// Flip the pinned class of an existing entry. Returns the resulting meta, or
// null when there is no entry to flip (the caller then decides whether to
// fetch + putChat instead).
export async function setPinned(
	userId: string,
	chatId: string,
	pinned: boolean
): Promise<OfflineChatMeta | null> {
	try {
		const db = await getDb();
		const tx = db.transaction(STORE_NAME, 'readwrite');
		const store = tx.objectStore(STORE_NAME);
		const existing = await store.get([userId, chatId]);
		if (!existing) {
			await tx.done;
			return null;
		}
		await store.put({ ...existing, pinned });
		await tx.done;
		return {
			updatedAt: existing.updatedAt,
			storedAt: existing.storedAt,
			pinned,
			etag: existing.etag
		};
	} catch (err) {
		console.error('offline chatStore: setPinned failed', err);
		return null;
	}
}

export async function getChat(userId: string, chatId: string): Promise<OfflineChatEntry | null> {
	try {
		const db = await getDb();
		const record: StoredChatRecord | undefined = await db.get(STORE_NAME, [userId, chatId]);
		if (!record) return null;
		const data = await unpackChatData(record);
		if (data === null || data === undefined) {
			// Corrupt (undecodable gzip) or empty — drop it so it can't shadow a
			// future healthy write, and report a miss.
			await db.delete(STORE_NAME, [userId, chatId]).catch(() => {});
			return null;
		}
		return { ...record, data };
	} catch (err) {
		console.error('offline chatStore: getChat failed', err);
		return null;
	}
}

export async function deleteChat(userId: string, chatId: string): Promise<void> {
	try {
		const db = await getDb();
		await db.delete(STORE_NAME, [userId, chatId]);
	} catch (err) {
		console.error('offline chatStore: deleteChat failed', err);
	}
}

// Cheap metadata listing (no `data` payload) — used for sidebar affordances
// (e.g. "available offline" dimming) without paying for the full JSON blobs.
export async function listChatMeta(userId: string): Promise<Map<string, OfflineChatMeta>> {
	const result = new Map<string, OfflineChatMeta>();
	try {
		const db = await getDb();
		const tx = db.transaction(STORE_NAME, 'readonly');
		const index = tx.store.index(BY_USER_STORED_INDEX);
		let cursor = await index.openCursor(
			IDBKeyRange.bound([userId, -Infinity], [userId, Infinity])
		);
		while (cursor) {
			const value = cursor.value as OfflineChatEntry;
			result.set(value.chatId, {
				updatedAt: value.updatedAt,
				storedAt: value.storedAt,
				pinned: !!value.pinned,
				etag: value.etag
			});
			cursor = await cursor.continue();
		}
		await tx.done;
	} catch (err) {
		console.error('offline chatStore: listChatMeta failed', err);
		return new Map();
	}
	return result;
}

// Clears one user's entries only (e.g. after that user deletes all their
// chats) — other accounts on a shared device keep their offline copies.
export async function purgeUser(userId: string): Promise<void> {
	try {
		const db = await getDb();
		const tx = db.transaction(STORE_NAME, 'readwrite');
		const index = tx.store.index(BY_USER_STORED_INDEX);
		let cursor = await index.openCursor(
			IDBKeyRange.bound([userId, -Infinity], [userId, Infinity])
		);
		while (cursor) {
			await cursor.delete();
			cursor = await cursor.continue();
		}
		await tx.done;
	} catch (err) {
		console.error('offline chatStore: purgeUser failed', err);
	}
}

// Clears the ENTIRE object store (all users). Uses store.clear() inside a
// transaction rather than indexedDB.deleteDatabase, which blocks
// indefinitely if another tab still holds the DB open (versionchange
// semantics) — a real risk for a shared-device / multi-tab sign-out.
export async function purgeAll(): Promise<void> {
	try {
		const db = await getDb();
		const tx = db.transaction(STORE_NAME, 'readwrite');
		await tx.objectStore(STORE_NAME).clear();
		await tx.done;
	} catch (err) {
		console.error('offline chatStore: purgeAll failed', err);
	}
}

// Two-class eviction: auto-cached (pinned=false) entries are evicted first,
// oldest storedAt first, down to under the cap. Pinned entries are only
// evicted if the auto-cached class alone can't bring the user under the cap,
// and even then oldest-storedAt-within-pinned first.
// Returns the evicted chatIds so reactive mirrors can drop them too.
export async function evictIfOverCap(userId: string): Promise<string[]> {
	const evicted: string[] = [];
	try {
		const db = await getDb();
		const tx = db.transaction(STORE_NAME, 'readwrite');
		const index = tx.store.index(BY_USER_STORED_INDEX);

		const auto: { chatId: string; storedAt: number }[] = [];
		const pinned: { chatId: string; storedAt: number }[] = [];

		let cursor = await index.openCursor(
			IDBKeyRange.bound([userId, -Infinity], [userId, Infinity])
		);
		let total = 0;
		while (cursor) {
			const value = cursor.value as OfflineChatEntry;
			total++;
			if (value.pinned) {
				pinned.push({ chatId: value.chatId, storedAt: value.storedAt });
			} else {
				auto.push({ chatId: value.chatId, storedAt: value.storedAt });
			}
			cursor = await cursor.continue();
		}

		let overBy = total - MAX_ENTRIES_PER_USER;
		if (overBy > 0) {
			auto.sort((a, b) => a.storedAt - b.storedAt);
			const toEvictFromAuto = auto.slice(0, Math.min(overBy, auto.length));
			for (const item of toEvictFromAuto) {
				await tx.objectStore(STORE_NAME).delete([userId, item.chatId]);
				evicted.push(item.chatId);
			}
			overBy -= toEvictFromAuto.length;

			if (overBy > 0) {
				pinned.sort((a, b) => a.storedAt - b.storedAt);
				const toEvictFromPinned = pinned.slice(0, Math.min(overBy, pinned.length));
				for (const item of toEvictFromPinned) {
					await tx.objectStore(STORE_NAME).delete([userId, item.chatId]);
					evicted.push(item.chatId);
				}
			}
		}

		await tx.done;
	} catch (err) {
		console.error('offline chatStore: evictIfOverCap failed', err);
	}
	return evicted;
}

