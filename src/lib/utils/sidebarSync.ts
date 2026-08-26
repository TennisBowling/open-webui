// Cross-tab / cross-device sidebar sync — receives `type` events from the
// backend's broadcast_sidebar_event helper and patches the chat-list stores
// in place. The originating tab is excluded server-side (via X-Session-Id),
// so this only ever runs in *other* tabs / devices of the same user.
//
// Patching in place preserves the user's pagination + scroll position, which
// is why we avoid the `currentChatPage.set(1); chats.set(getChatList(...))`
// pattern that the legacy `chat:title` handler used.
//
// `chat:updated` events ARE skip_sid'd — the originating tab updates its
// sidebar via optimistic patches on the PATCH response (saveChatHandler in
// Chat.svelte) and via the chat:done handler. Other tabs receive the
// broadcast and patch in place.

import { get, writable } from 'svelte/store';
import {
	chats,
	channels,
	pinnedChats,
	tags,
	folders,
	folderChatListInvalidation,
	chatId,
	chatTitle,
	currentChatPage,
	scrollPaginationEnabled
} from '$lib/stores';
import { getChatList, getPinnedChatList, getAllTags, getChatById } from '$lib/apis/chats';
import { getFolders } from '$lib/apis/folders';
import { getChannels } from '$lib/apis/channels';
import { getBootstrap, type BootstrapInclude } from '$lib/apis';
import { getTimeRange } from '$lib/utils';
import { writeLocalStorageCache } from '$lib/utils/cache';
import { removeOfflineChat, purgeOfflineChatsForUser } from '$lib/offline/manager';
import {
	SIDEBAR_CHATS_CACHE_KEY,
	SIDEBAR_PINNED_CHATS_CACHE_KEY,
	SIDEBAR_TAGS_CACHE_KEY,
	SIDEBAR_FOLDERS_CACHE_KEY,
	SIDEBAR_CHANNELS_CACHE_KEY,
	getSidebarCacheKey
} from '$lib/constants/cache';
import {
	BOOTSTRAP_BUNDLE_ETAG_KEY,
	BOOTSTRAP_SIDEBAR_ETAG_KEY,
	readBootstrapCache,
	buildBootstrapEtagsHeader,
	applyUnchangedFromCache,
	mergeBootstrapCacheComponents
} from '$lib/utils/bootstrap';

type ChatRow = {
	id: string;
	title: string;
	updated_at: number;
	created_at: number;
	pinned?: boolean;
	archived?: boolean;
	folder_id?: string | null;
	time_range?: string;
	[k: string]: any;
};

// "Is the sidebar's `updated_at` window provably fresh right now?" — true at
// boot, set false by the socket-disconnect handler (+layout.svelte), and
// restored true only once a reconcile-on-reconnect (or an equivalent
// bootstrap 200/304) has actually completed. Consulted by Chat.svelte's
// offline/soft-nav cache-serve gate: without this true, a zero-network serve
// is treated as a miss (falls through to the network path) even if the
// in-memory LRU / IDB entry's updatedAt otherwise matches the sidebar's
// cached value — closing the staleness hole where a short (<30s) disconnect
// blip could otherwise let a chat serve from a slightly-stale cache entry.
export const sidebarReconcileClean = writable(true);

export const decorate = (row: ChatRow): ChatRow => ({
	...row,
	time_range: getTimeRange(row.updated_at)
});

const uniqueFolderIds = (ids: any[] = []) =>
	Array.from(new Set(ids.filter((id) => typeof id === 'string' && id.length > 0)));

const invalidateFolderChatLists = (folderIds: any[] = [], reason = 'sidebar-event') => {
	const ids = uniqueFolderIds(folderIds);
	if (ids.length === 0) return;
	folderChatListInvalidation.update((state) => ({
		folderIds: ids,
		seq: state.seq + 1,
		reason
	}));
};

const removeById = (arr: any[] | null, id: string) => (arr ? arr.filter((c) => c.id !== id) : arr);

export const upsertSorted = (arr: any[] | null, row: ChatRow): ChatRow[] => {
	const existing = (arr ?? []).find((c) => c.id === row.id);
	const shouldKeepExisting =
		existing &&
		typeof existing.updated_at === 'number' &&
		typeof row.updated_at === 'number' &&
		existing.updated_at > row.updated_at;
	const resolved = shouldKeepExisting ? existing : row;
	const base = (arr ?? []).filter((c) => c.id !== row.id);
	base.unshift(decorate(resolved));
	// Keep the list ordered by updated_at desc; the sidebar then groups by time_range.
	base.sort((a, b) => (b.updated_at ?? 0) - (a.updated_at ?? 0));
	return base;
};

// The page-1 window size applyChatsWindow works in; the localStorage cache
// mirrors at most this many rows so a long-paged session doesn't bloat it.
const CHATS_CACHE_WINDOW = 60;

// Write-through, NOT invalidate: the event handlers below keep the stores
// authoritative, so persist them. Clearing here meant any session with chat
// activity left an empty cache behind — every cold PWA boot then painted a
// blank sidebar and visibly "loaded in" (the exact stale-beats-empty case the
// cache exists for).
const persistChatsCaches = () => {
	const chatsList = get(chats);
	if (Array.isArray(chatsList)) {
		writeSidebarCache(
			SIDEBAR_CHATS_CACHE_KEY,
			'chats:first-page',
			chatsList.slice(0, CHATS_CACHE_WINDOW)
		);
	}
	const pinned = get(pinnedChats);
	if (Array.isArray(pinned)) {
		writeSidebarCache(SIDEBAR_PINNED_CHATS_CACHE_KEY, 'pinned-chats', pinned);
	}
};

const isIncomingOlder = (current: any, incoming: any) =>
	typeof current?.updated_at === 'number' &&
	typeof incoming?.updated_at === 'number' &&
	current.updated_at > incoming.updated_at;

const patchSorted = (arr: any[] | null, id: string, data: any, patcher: (c: any) => any) =>
	arr
		? [...arr]
				.map((c) => (c.id === id && !isIncomingOlder(c, data) ? patcher(c) : c))
				.sort((a, b) => (b.updated_at ?? 0) - (a.updated_at ?? 0))
		: arr;

const currentUserId = (): string | null => {
	try {
		return JSON.parse(localStorage.getItem('sessionUser') ?? 'null')?.id ?? null;
	} catch {
		return null;
	}
};

// Pagination-preserving chat-list merge (Wire item #7). Instead of resetting to
// page 1 and discarding every loaded page, we upsert the fresh page-1 window
// (60 rows): update/insert those rows, drop ids that vanished from within the
// window, and keep the already-loaded tail (older pages) + scroll state.
const applyChatsWindow = (freshChatsRaw: any[]) => {
	const freshRows = freshChatsRaw.map(decorate);
	chats.update((cur) => {
		if (!Array.isArray(cur) || cur.length === 0) return freshRows;
		if (freshRows.length === 0) return freshRows; // page 1 empty -> no chats at all
		const oldestFresh = Math.min(...freshRows.map((r) => r.updated_at ?? 0));
		const freshIds = new Set(freshRows.map((r) => r.id));
		// Keep only the already-loaded tail: rows strictly older than the window
		// that weren't re-surfaced in the fresh page. Rows inside the window range
		// that are absent from the fresh page were deleted/moved -> dropped.
		const tail = cur.filter((r) => (r.updated_at ?? 0) < oldestFresh && !freshIds.has(r.id));
		const merged = [...freshRows, ...tail];
		merged.sort((a, b) => (b.updated_at ?? 0) - (a.updated_at ?? 0));
		return merged;
	});
	// Preserve currentChatPage + scrollPaginationEnabled (the loaded tail stays).
	if (!get(scrollPaginationEnabled)) scrollPaginationEnabled.set(true);
};

const writeSidebarCache = (name: string, key: string, value: any) => {
	// No TTL: sidebar caches persist so the paint is always instant regardless of
	// age (stale-then-revalidate); freshness comes from push events + reconciles.
	writeLocalStorageCache(name, getSidebarCacheKey(currentUserId(), key), value);
};

const applySidebarComponents = (components: Record<string, any>, reason: string) => {
	if (Array.isArray(components.folders)) {
		folders.set(components.folders);
		invalidateFolderChatLists(
			components.folders.map((folder: any) => folder?.id),
			`snapshot:${reason}`
		);
		writeSidebarCache(SIDEBAR_FOLDERS_CACHE_KEY, 'folders', components.folders);
	}
	if (Array.isArray(components.tags)) {
		tags.set(components.tags);
		writeSidebarCache(SIDEBAR_TAGS_CACHE_KEY, 'tags', components.tags);
	}
	if (Array.isArray(components.pinned)) {
		const decorated = components.pinned.map((c: any) => decorate(c));
		pinnedChats.set(decorated);
		writeSidebarCache(SIDEBAR_PINNED_CHATS_CACHE_KEY, 'pinned-chats', decorated);
	}
	if (Array.isArray(components.chats)) {
		const decorated = components.chats.map((c: any) => decorate(c));
		applyChatsWindow(components.chats);
		// The cache mirrors page 1 only (what the sidebar paints instantly).
		writeSidebarCache(SIDEBAR_CHATS_CACHE_KEY, 'chats:first-page', decorated);
	}
	if (Array.isArray(components.channels)) {
		channels.set(components.channels);
		writeSidebarCache(SIDEBAR_CHANNELS_CACHE_KEY, 'channels', components.channels);
	}
};

const SIDEBAR_BOOTSTRAP_INCLUDE: BootstrapInclude[] = [
	'folders',
	'tags',
	'pinned',
	'chats',
	'channels'
];

// Route the reconcile through ONE conditional bootstrap request (If-None-Match +
// x-bootstrap-etags) instead of 4 unconditional GETs (Wire item #2). Returns
// true when it handled the reconcile (incl. 304/401), false to fall back.
const refreshSidebarViaBootstrap = async (token: string, reason: string): Promise<boolean> => {
	const userId = currentUserId();
	const cached = readBootstrapCache(userId);
	const sidebarEtagKey = BOOTSTRAP_SIDEBAR_ETAG_KEY(userId);
	const ifNoneMatch = localStorage.getItem(sidebarEtagKey);
	const etagsHeader = buildBootstrapEtagsHeader(cached?.components_etags, SIDEBAR_BOOTSTRAP_INCLUDE);

	const res = await getBootstrap(token, {
		include: SIDEBAR_BOOTSTRAP_INCLUDE,
		ifNoneMatch,
		bootstrapEtags: etagsHeader
	});

	if (!res) return false; // endpoint unavailable / transient -> legacy fallback
	if (res.status === 401) return true; // auth handled by the boot layer; don't fan out
	if (res.status === 304) return true; // nothing changed since last reconcile

	// 200
	let { components, missing } = applyUnchangedFromCache(res, cached);
	let etags = res.components_etags;
	if (missing.length > 0) {
		const full = await getBootstrap(token, { include: SIDEBAR_BOOTSTRAP_INCLUDE });
		if (full?.status === 200) {
			components = full.components;
			etags = full.components_etags;
		}
	}

	applySidebarComponents(components, reason);
	if (res.bundle_etag) localStorage.setItem(sidebarEtagKey, res.bundle_etag);
	// Keep the shared boot cache's per-component etags + bodies fresh so the next
	// cold/warm boot benefits from these refreshed components too.
	mergeBootstrapCacheComponents(userId, components, etags);
	return true;
};

// Legacy 4-fetch fallback (still pagination-preserving for chats). Returns
// whether the chats list — the component `sidebarReconcileClean` actually
// exists to vouch for (see Chat.svelte's zero-network cache-serve gate,
// which compares against the sidebar's `updated_at`) — was actually
// refreshed. A total HTTP-layer outage (e.g. proxy down while the socket
// stays connected) resolves every fetch to null via the `.catch(() => null)`
// guards below; in that case nothing was applied and the caller must NOT
// mark the reconcile clean.
const refreshSidebarViaFetch = async (token: string, reason: string): Promise<boolean> => {
	const [freshFolders, freshChats, freshPinned, freshTags] = await Promise.all([
		getFolders(token).catch(() => null),
		getChatList(token, 1).catch(() => null),
		getPinnedChatList(token).catch(() => null),
		getAllTags(token).catch(() => null)
	]);

	if (Array.isArray(freshFolders)) {
		folders.set(freshFolders);
		invalidateFolderChatLists(
			freshFolders.map((folder) => folder?.id),
			`snapshot:${reason}`
		);
		writeSidebarCache(SIDEBAR_FOLDERS_CACHE_KEY, 'folders', freshFolders);
	}
	if (Array.isArray(freshChats)) {
		applyChatsWindow(freshChats);
	}
	if (Array.isArray(freshPinned)) pinnedChats.set(freshPinned);
	if (Array.isArray(freshTags)) {
		tags.set(freshTags);
		writeSidebarCache(SIDEBAR_TAGS_CACHE_KEY, 'tags', freshTags);
	}
	if (Array.isArray(freshChats) || Array.isArray(freshPinned)) {
		persistChatsCaches();
	}

	return Array.isArray(freshChats);
};

export const refreshSidebarSnapshot = async (token: string, reason = 'manual') => {
	const handled = await refreshSidebarViaBootstrap(token, reason).catch((err) => {
		console.error('sidebar bootstrap reconcile failed', reason, err);
		return false;
	});
	if (handled) {
		sidebarReconcileClean.set(true);
		return;
	}
	const chatsRefreshed = await refreshSidebarViaFetch(token, reason).catch((err) => {
		console.error('sidebar legacy-fetch reconcile failed', reason, err);
		return false;
	});
	if (chatsRefreshed) {
		sidebarReconcileClean.set(true);
	}
};

export const applySidebarEvent = async (type: string, data: any, token: string): Promise<void> => {
	if (!type) return;

	switch (type) {
		case 'chat:created': {
			if (!data?.id) return;
			const row = decorate(data);
			if (row.pinned) {
				pinnedChats.update((arr) => upsertSorted(arr, row));
			} else if (row.archived) {
				// Archived chats don't show in the sidebar — nothing to do.
			} else if (row.folder_id == null) {
				chats.update((arr) => upsertSorted(arr, row));
			}
			invalidateFolderChatLists([row.folder_id], 'chat:created');
			persistChatsCaches();
			return;
		}

		case 'chat:deleted': {
			if (!data?.id) return;
			chats.update((arr) => removeById(arr, data.id));
			pinnedChats.update((arr) => removeById(arr, data.id) ?? []);
			const deletedForUserId = currentUserId();
			if (deletedForUserId) {
				void removeOfflineChat(deletedForUserId, data.id);
			}
			invalidateFolderChatLists([data.folder_id], 'chat:deleted');
			persistChatsCaches();
			return;
		}

		case 'chat:title':
		case 'chat:renamed': {
			if (!data?.id) return;
			const patch = (c: any) =>
				c.id === data.id
					? {
							...c,
							title: data.title ?? c.title,
							pinned: data.pinned ?? c.pinned,
							archived: data.archived ?? c.archived,
							folder_id: data.folder_id ?? c.folder_id,
							updated_at: data.updated_at ?? c.updated_at,
							time_range: getTimeRange(data.updated_at ?? c.updated_at)
						}
					: c;
			chats.update((arr) => patchSorted(arr, data.id, data, patch));
			pinnedChats.update((arr) => patchSorted(arr, data.id, data, patch) ?? []);
			invalidateFolderChatLists([data.folder_id], 'chat:renamed');

			// If the receiving tab is currently viewing the renamed chat,
			// update the title in the header too — Chat.svelte does not
			// re-read on its own. (Fixes the long-standing bug where the
			// visible-chat gate suppressed sidebar updates entirely.)
			if (get(chatId) === data.id && data.title) {
				chatTitle.set(data.title);
			}

			persistChatsCaches();
			return;
		}

		case 'chat:pinned': {
			if (!data?.id) return;
			const targetPinned = !!data.pinned;
			let row: ChatRow | undefined = data.title ? data : undefined;
			chats.update((arr) => {
				if (!arr) return arr;
				const idx = arr.findIndex((c) => c.id === data.id);
				if (idx >= 0) {
					if (!row || isIncomingOlder(arr[idx], row)) row = arr[idx];
					return [...arr.slice(0, idx), ...arr.slice(idx + 1)];
				}
				return arr;
			});
			pinnedChats.update((arr) => {
				const idx = arr.findIndex((c) => c.id === data.id);
				if (idx >= 0) {
					if (!row || isIncomingOlder(arr[idx], row)) row = arr[idx];
					return [...arr.slice(0, idx), ...arr.slice(idx + 1)];
				}
				return arr;
			});

			// If we don't have the row locally (it was on a later, unloaded page),
			// fetch a minimal record so we can still place it correctly.
			if (!row) {
				const fetched = await getChatById(token, data.id).catch(() => null);
				if (!fetched) {
					persistChatsCaches();
					return;
				}
				row = {
					id: fetched.id,
					title: fetched.title,
					updated_at: fetched.updated_at,
					created_at: fetched.created_at,
					pinned: targetPinned,
					archived: fetched.archived,
					folder_id: fetched.folder_id
				};
			}

			const patched: ChatRow = decorate({ ...row, pinned: targetPinned });
			if (targetPinned) {
				pinnedChats.update((arr) => upsertSorted(arr, patched));
			} else if (patched.folder_id == null && !patched.archived) {
				chats.update((arr) => upsertSorted(arr, patched));
			}
			invalidateFolderChatLists([patched.folder_id], 'chat:pinned');
			persistChatsCaches();
			return;
		}

		case 'chat:archived': {
			if (!data?.id) return;
			if (data.archived) {
				chats.update((arr) => removeById(arr, data.id));
				pinnedChats.update((arr) => removeById(arr, data.id) ?? []);
			} else {
				const row = decorate(data as ChatRow);
				if (row.pinned) {
					pinnedChats.update((arr) => upsertSorted(arr, row));
				} else if (row.folder_id == null) {
					chats.update((arr) => upsertSorted(arr, row));
				}
			}
			invalidateFolderChatLists([data.folder_id], 'chat:archived');
			persistChatsCaches();
			return;
		}

		case 'chat:folder': {
			if (!data?.id) return;
			const nextFolderId = data.folder_id ?? null;
			const row = data.title ? decorate(data as ChatRow) : null;
			chats.update((arr) => removeById(arr, data.id));
			pinnedChats.update((arr) => removeById(arr, data.id) ?? []);
			if (row?.pinned) {
				pinnedChats.update((arr) => upsertSorted(arr, row));
			} else if (row && !row.archived && nextFolderId == null) {
				chats.update((arr) => upsertSorted(arr, row));
			}
			invalidateFolderChatLists([data.previous_folder_id, nextFolderId], 'chat:folder');
			persistChatsCaches();
			return;
		}

		case 'chat:tags': {
			const fresh = await getAllTags(token).catch(() => null);
			if (Array.isArray(fresh)) {
				tags.set(fresh);
				writeSidebarCache(SIDEBAR_TAGS_CACHE_KEY, 'tags', fresh);
			}
			return;
		}

		case 'folder:created':
		case 'folder:updated': {
			const fresh = await getFolders(token).catch(() => null);
			if (Array.isArray(fresh)) {
				folders.set(fresh);
				writeSidebarCache(SIDEBAR_FOLDERS_CACHE_KEY, 'folders', fresh);
			}
			return;
		}

		case 'folder:deleted': {
			// Cascade: chats inside the folder were deleted server-side. Refetch
			// folders + first-page chats + pinned (the cascaded chats could have
			// been pinned). Cheaper than fanning out N chat:deleted events and
			// safer than guessing which chats were inside.
			await refreshSidebarSnapshot(token, 'folder:deleted');
			return;
		}

		case 'chats:bulk': {
			// archive_all / unarchive_all / delete_all — easier to refetch the
			// world than to model the bulk transition locally.
			if (data?.operation === 'delete_all') {
				const bulkUserId = currentUserId();
				if (bulkUserId) {
					void purgeOfflineChatsForUser(bulkUserId);
				}
			}
			await refreshSidebarSnapshot(token, data?.operation ?? 'chats:bulk');
			return;
		}

		case 'chat:updated': {
			if (!data?.id) return;
			const ts = data.updated_at;
			if (ts == null) return;
			const patch = (c: any) =>
				c.id === data.id
					? {
							...c,
							updated_at: ts,
							time_range: getTimeRange(ts),
							folder_id: data.folder_id ?? c.folder_id
						}
					: c;
			chats.update((arr) => patchSorted(arr, data.id, data, patch));
			pinnedChats.update((arr) => patchSorted(arr, data.id, data, patch) ?? []);
			invalidateFolderChatLists([data.folder_id], 'chat:updated');
			persistChatsCaches();
			return;
		}

		default:
			return;
	}
};

export const SIDEBAR_EVENT_TYPES = new Set([
	'chat:created',
	'chat:deleted',
	'chat:renamed',
	'chat:title',
	'chat:updated',
	'chat:pinned',
	'chat:archived',
	'chat:folder',
	'chat:tags',
	'folder:created',
	'folder:updated',
	'folder:deleted',
	'chats:bulk'
]);
