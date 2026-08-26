// Offline-chat manager — the reactive layer over the raw IDB chatStore.
//
// Owns three things:
// 1. `offlineChatMeta` — a Svelte store mirroring listChatMeta() so the
//    sidebar can synchronously answer "will this chat open offline?" (used
//    for dimming unavailable chats while offline and for the Keep-offline
//    menu state). `null` until the first load completes; a Map afterwards.
// 2. Snapshot writes — every code path that persists a chat offline
//    (auto-cache on open, pin, prefetch) goes through saveOfflineChatSnapshot
//    so the stored shape stays identical everywhere and the meta store never
//    drifts from IDB.
// 3. Prefetch — a bounded, sequential, stale-only sweep of recent + pinned
//    chats so offline reading works without having opened each chat first.
//    Deliberately conservative for metered iOS connections: a boot where
//    nothing changed downloads zero chat bodies.
//
// Like chatStore, nothing here may throw into a caller.

import { get, writable, type Writable } from 'svelte/store';

import { chats, pinnedChats, online, settings } from '$lib/stores';
import { getChatByIdTail } from '$lib/apis/chats';
import {
	putChat,
	getChat,
	deleteChat,
	setPinned,
	listChatMeta,
	evictIfOverCap,
	purgeUser,
	type OfflineChatMeta
} from '$lib/offline/chatStore';

export const offlineChatMeta: Writable<Map<string, OfflineChatMeta> | null> = writable(null);

export const isOfflineChatStorageEnabled = (): boolean =>
	(get(settings) as any)?.offlineChatStorage === true;

const updateMeta = (chatId: string, meta: OfflineChatMeta | null) => {
	offlineChatMeta.update((m) => {
		// Before the first listChatMeta() completes we don't know the full set,
		// so a partial write would masquerade as "these are the ONLY offline
		// chats" and mis-dim everything else. Drop the update; the pending
		// refresh will pick this entry up from IDB anyway.
		if (m === null) return m;
		const next = new Map(m);
		if (meta === null) {
			next.delete(chatId);
		} else {
			next.set(chatId, meta);
		}
		return next;
	});
};

let metaRefreshPromise: Promise<void> | null = null;
let metaRefreshUserId: string | null = null;
export const refreshOfflineChatMeta = async (userId: string): Promise<void> => {
	if (!userId) return;
	// Coalesce only same-user calls; a different user must not be handed the
	// in-flight map of the previous account.
	if (metaRefreshPromise && metaRefreshUserId === userId) return metaRefreshPromise;
	if (metaRefreshPromise) {
		await metaRefreshPromise.catch(() => {});
	}
	metaRefreshUserId = userId;
	metaRefreshPromise = (async () => {
		const meta = await listChatMeta(userId);
		offlineChatMeta.set(meta);
	})();
	try {
		await metaRefreshPromise;
	} finally {
		metaRefreshPromise = null;
		metaRefreshUserId = null;
	}
};

export const resetOfflineChatMeta = () => {
	offlineChatMeta.set(new Map());
};

// Persist one chat snapshot (the getChatByIdTail response shape). putChat
// serializes (and gzips) the body synchronously at call time — snapshot
// semantics for live objects, and JSON serialization drops the non-enumerable
// __tailTags/__offlineCopy markers, matching what the offline serve path
// expects to re-attach from `tags`. Callers on hot paths pass `tags` /
// `updatedAt` captured at fetch time and defer the heavy serialize to idle.
export const saveOfflineChatSnapshot = async ({
	userId,
	chatId,
	chat,
	pinned = false,
	tags,
	updatedAt,
	etag
}: {
	userId: string;
	chatId: string;
	chat: any;
	pinned?: boolean;
	tags?: any[];
	updatedAt?: number;
	etag?: string;
}): Promise<boolean> => {
	if (!userId || !chatId || chatId.startsWith('local:')) return false;
	const resolvedUpdatedAt = updatedAt ?? chat?.updated_at;
	if (typeof resolvedUpdatedAt !== 'number') return false;

	try {
		const resolvedTags = Array.isArray(tags)
			? structuredClone(tags)
			: Array.isArray(chat?.__tailTags)
				? structuredClone(chat.__tailTags)
				: [];
		// The server-minted validator rides on the fetched chat as a
		// non-enumerable __etag (attached in getChatByIdTail); persisting it is
		// what makes the NEXT open conditional.
		const resolvedEtag = etag ?? (typeof chat?.__etag === 'string' ? chat.__etag : undefined);
		// putChat reports the post-CAS truth (our write, or the newer existing
		// entry it kept, pin preserved either way) — mirror exactly that.
		const storedMeta = await putChat({
			userId,
			chatId,
			updatedAt: resolvedUpdatedAt,
			storedAt: Date.now(),
			data: chat,
			tags: resolvedTags,
			pinned,
			etag: resolvedEtag
		});
		if (storedMeta === null) return false;
		updateMeta(chatId, storedMeta);
		const evicted = await evictIfOverCap(userId);
		for (const evictedId of evicted) {
			updateMeta(evictedId, null);
		}
		return true;
	} catch (err) {
		console.error('offline manager: saveOfflineChatSnapshot failed', err);
		return false;
	}
};

export const removeOfflineChat = async (userId: string, chatId: string): Promise<void> => {
	if (!userId || !chatId) return;
	await deleteChat(userId, chatId);
	updateMeta(chatId, null);
};

// All of one user's offline copies (delete-all-chats flows).
export const purgeOfflineChatsForUser = async (userId: string): Promise<void> => {
	if (!userId) return;
	await purgeUser(userId);
	if (get(offlineChatMeta) !== null) {
		offlineChatMeta.set(new Map());
	}
};

// Pin ("keep offline") / unpin. Pinning a chat we don't have locally fetches
// it first — that's the whole point of the affordance. Unpinning keeps the
// entry as an auto-cached copy (it just loses eviction priority).
export const setOfflineChatPinned = async (
	token: string,
	userId: string,
	chatId: string,
	pinned: boolean
): Promise<boolean> => {
	if (!userId || !chatId) return false;

	const flippedMeta = await setPinned(userId, chatId, pinned);
	if (flippedMeta) {
		updateMeta(chatId, flippedMeta);
		return true;
	}
	if (!pinned) {
		// Nothing stored, nothing to unpin.
		return false;
	}

	// Pinning with no local entry: fetch the tail and store it pinned.
	try {
		const chat = await getChatByIdTail(token, chatId);
		if (!chat) return false;
		return await saveOfflineChatSnapshot({ userId, chatId, chat, pinned: true });
	} catch (err) {
		console.error('offline manager: pin fetch failed', err);
		return false;
	}
};

// Called after a completed local save to a chat. The stored copy (if any) is
// now stale but deliberately KEPT — the online serve gates compare updatedAt
// exactly, so a stale entry never race-serves, while offline a stale copy
// beats none. Pinned copies get a debounced freshness refetch so
// "keep offline" stays current without spamming a fetch per save.
const PINNED_SAVE_REFRESH_DEBOUNCE_MS = 15_000;
const pinnedRefreshTimers = new Map<string, ReturnType<typeof setTimeout>>();
export const handleLocalChatSaved = (token: string, userId: string, chatId: string) => {
	if (!userId || !chatId || chatId.startsWith('local:')) return;
	const entry = get(offlineChatMeta)?.get(chatId);
	if (!entry?.pinned) return;
	if (!isOfflineChatStorageEnabled()) return;

	const existing = pinnedRefreshTimers.get(chatId);
	if (existing) clearTimeout(existing);
	pinnedRefreshTimers.set(
		chatId,
		setTimeout(async () => {
			pinnedRefreshTimers.delete(chatId);
			if (!get(online) || !isOfflineChatStorageEnabled()) return;
			try {
				// Pass the stored entry so this refresh rides the conditional /
				// incremental ladder (304 or a rows-only delta) instead of
				// re-downloading the whole tail after every local save.
				const etagEntry = await getChat(userId, chatId);
				const chat = await getChatByIdTail(token, chatId, 25, { etagEntry });
				if (chat && chat !== etagEntry?.data) {
					// putChat's sticky pin preserves the pinned class on this rewrite.
					await saveOfflineChatSnapshot({ userId, chatId, chat });
				}
			} catch {
				// transient — the next prefetch sweep retries via staleness
			}
		}, PINNED_SAVE_REFRESH_DEBOUNCE_MS)
	);
};

// ---------------------------------------------------------------------------
// Prefetch
// ---------------------------------------------------------------------------

// How many recent sidebar chats are candidates per sweep, how many pinned
// entries outside the recent window get a freshness re-fetch per sweep, and
// how old a pinned copy must be before that re-fetch is considered.
const PREFETCH_RECENT_MAX = 15;
const PREFETCH_PINNED_MAX = 10;
const PINNED_REFRESH_MIN_AGE_MS = 24 * 60 * 60 * 1000;
// Floor between automatic sweeps (manual "Download now" bypasses it).
const PREFETCH_MIN_INTERVAL_MS = 10 * 60 * 1000;

let prefetchInFlight = false;
let lastPrefetchAt = 0;

export type PrefetchProgress = {
	total: number;
	done: number;
	downloaded: number;
};

// One bounded sweep. Sequential on purpose (never competes with a user
// action for bandwidth) and stale-only: a chat is fetched ONLY when the
// sidebar shows a newer updated_at than the stored copy (or no copy exists).
export const prefetchOfflineChats = async ({
	token,
	userId,
	onProgress
}: {
	token: string;
	userId: string;
	onProgress?: (p: PrefetchProgress) => void;
}): Promise<PrefetchProgress | null> => {
	if (!userId || !token) return null;
	if (prefetchInFlight) return null;
	if (!isOfflineChatStorageEnabled()) return null;
	if (!get(online)) return null;

	prefetchInFlight = true;
	try {
		// Make sure the meta map reflects IDB before computing staleness.
		if (get(offlineChatMeta) === null) {
			await refreshOfflineChatMeta(userId);
		}
		const meta = get(offlineChatMeta) ?? new Map<string, OfflineChatMeta>();

		// Candidate rows: pinned sidebar chats + the recent window, deduped,
		// newest first. These rows carry the server updated_at we compare with.
		const rows: { id: string; updated_at?: number }[] = [];
		const seen = new Set<string>();
		for (const row of [...(get(pinnedChats) ?? []), ...(get(chats) ?? [])]) {
			if (!row?.id || seen.has(row.id) || row.id.startsWith('local:')) continue;
			seen.add(row.id);
			rows.push(row);
			if (rows.length >= PREFETCH_RECENT_MAX * 2) break;
		}

		const stale = rows
			.filter((row) => {
				const entry = meta.get(row.id);
				if (!entry) return true;
				return typeof row.updated_at === 'number' && row.updated_at > entry.updatedAt;
			})
			.slice(0, PREFETCH_RECENT_MAX);

		// Pinned-offline entries that are NOT in the sidebar window: no server
		// updated_at to compare against, so re-fetch age-gated (once a day).
		const now = Date.now();
		const stalePinned: string[] = [];
		for (const [chatId, entry] of meta) {
			if (!entry.pinned || seen.has(chatId)) continue;
			if (now - entry.storedAt >= PINNED_REFRESH_MIN_AGE_MS) {
				stalePinned.push(chatId);
			}
			if (stalePinned.length >= PREFETCH_PINNED_MAX) break;
		}

		const targets = [...stale.map((r) => r.id), ...stalePinned];
		const progress: PrefetchProgress = { total: targets.length, done: 0, downloaded: 0 };
		onProgress?.(progress);

		for (const chatId of targets) {
			// Connectivity or the user's mind can change mid-sweep.
			if (!get(online) || !isOfflineChatStorageEnabled()) break;
			try {
				// Conditional refetch: with the stored entry's etag the server can
				// answer 304 (~300 bytes) — the norm for the daily pinned refresh.
				// getChatByIdTail substitutes the entry's own data on 304 (same
				// object reference), which re-persists idempotently below and
				// bumps storedAt so the age gate resets.
				const etagEntry = await getChat(userId, chatId);
				const chat = await getChatByIdTail(token, chatId, 25, { etagEntry });
				if (chat) {
					const ok = await saveOfflineChatSnapshot({ userId, chatId, chat });
					if (ok && chat !== etagEntry?.data) progress.downloaded++;
				}
			} catch (err: any) {
				// A real HTTP status (401/404: deleted or access revoked) means the
				// local copy must go too — same rule as the serve path.
				if (err && !err.isNetworkError && (err.status === 401 || err.status === 404)) {
					await removeOfflineChat(userId, chatId);
				}
			}
			progress.done++;
			onProgress?.({ ...progress });
			// Small yield between fetches so a sweep never starves user actions.
			await new Promise((r) => setTimeout(r, 150));
		}

		lastPrefetchAt = Date.now();
		return progress;
	} catch (err) {
		console.error('offline manager: prefetch failed', err);
		return null;
	} finally {
		prefetchInFlight = false;
	}
};

// Throttled, idle-scheduled wrapper for automatic sweeps (boot + back-online).
export const scheduleOfflinePrefetch = (token: string, userId: string, delayMs = 4000) => {
	if (!userId || !token) return;
	if (!isOfflineChatStorageEnabled()) return;
	if (Date.now() - lastPrefetchAt < PREFETCH_MIN_INTERVAL_MS) return;

	setTimeout(() => {
		const run = () => void prefetchOfflineChats({ token, userId });
		if (typeof (window as any).requestIdleCallback === 'function') {
			(window as any).requestIdleCallback(run, { timeout: 10_000 });
		} else {
			run();
		}
	}, delayMs);
};

// Best-effort durable-storage request — on iOS/Safari this materially lowers
// the odds of the offline DB being evicted under storage pressure.
let persistRequested = false;
export const requestPersistentStorage = () => {
	if (persistRequested) return;
	persistRequested = true;
	try {
		void navigator?.storage?.persist?.();
	} catch {
		// unsupported — fine
	}
};
