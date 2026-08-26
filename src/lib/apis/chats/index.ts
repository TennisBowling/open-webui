import { WEBUI_API_BASE_URL } from '$lib/constants';
import { getTimeRange } from '$lib/utils';
import { get } from 'svelte/store';
import { socket } from '$lib/stores';

// Tag every mutating chat/folder request with the originating tab's socket id
// so the backend's broadcast_sidebar_event helper can skip it — the originating
// tab already updates optimistically (or refetches) on its own; receiving its
// own event back would cause double work and possible duplicate inserts.
const sessionHeader = (): Record<string, string> => {
	const sid = get(socket)?.id;
	return sid ? { 'X-Session-Id': sid } : {};
};

// Structured chat-fetch error — used by getChatMeta/getChatByIdTail so callers
// (offline-store fallback logic in Chat.svelte) can reliably distinguish "the
// network request never reached the server" (safe to fall back to a local
// offline copy) from "the server responded with a real HTTP error" (e.g. 401
// for a deleted/access-revoked chat — must NEVER resurrect from stale local
// cache while online).
export type ChatFetchError = {
	isNetworkError: boolean;
	status?: number;
	detail?: string;
};

const toChatFetchError = (err: any): ChatFetchError => {
	// A real, parsed HTTP error response (server actually answered) always wins
	// over the navigator.onLine heuristic — onLine can misreport false (VPNs,
	// captive portals, virtual NICs) even while requests are succeeding, and we
	// must never let that misclassify a genuine 401/404 (deleted/revoked chat)
	// as a network failure, which would let it resurrect from the offline cache.
	if (err && typeof err === 'object' && err.__structuredHttpError) {
		return { isNetworkError: false, status: err.status, detail: err.detail };
	}
	const offline = typeof navigator !== 'undefined' && navigator.onLine === false;
	if (err instanceof TypeError || offline) {
		return { isNetworkError: true, detail: err?.message ?? 'Network error' };
	}
	return {
		isNetworkError: false,
		detail: typeof err === 'string' ? err : (err?.detail ?? err?.message ?? undefined)
	};
};

export const createNewChat = async (token: string, chat: object, folderId: string | null) => {
	let error = null;

	const res = await fetch(`${WEBUI_API_BASE_URL}/chats/new`, {
		method: 'POST',
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			authorization: `Bearer ${token}`,
			...sessionHeader()
		},
		body: JSON.stringify({
			chat: chat,
			folder_id: folderId ?? null
		})
	})
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json();
		})
		.catch((err) => {
			error = err;
			console.error(err);
			return null;
		});

	if (error) {
		throw error;
	}

	return res;
};

export const unarchiveAllChats = async (token: string) => {
	let error = null;

	const res = await fetch(`${WEBUI_API_BASE_URL}/chats/unarchive/all`, {
		method: 'POST',
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			...(token && { authorization: `Bearer ${token}` }),
			...sessionHeader()
		}
	})
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json();
		})
		.then((json) => {
			return json;
		})
		.catch((err) => {
			error = err.detail;

			console.error(err);
			return null;
		});

	if (error) {
		throw error;
	}

	return res;
};

export const importChat = async (
	token: string,
	chat: object,
	meta: object | null,
	pinned?: boolean,
	folderId?: string | null,
	createdAt: number | null = null,
	updatedAt: number | null = null
) => {
	let error = null;

	const res = await fetch(`${WEBUI_API_BASE_URL}/chats/import`, {
		method: 'POST',
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			authorization: `Bearer ${token}`,
			...sessionHeader()
		},
		body: JSON.stringify({
			chat: chat,
			meta: meta ?? {},
			pinned: pinned,
			folder_id: folderId,
			created_at: createdAt ?? null,
			updated_at: updatedAt ?? null
		})
	})
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json();
		})
		.catch((err) => {
			error = err;
			console.error(err);
			return null;
		});

	if (error) {
		throw error;
	}

	return res;
};

export const getChatList = async (
	token: string = '',
	page: number | null = null,
	include_pinned: boolean = false,
	include_folders: boolean = false
) => {
	let error = null;
	const searchParams = new URLSearchParams();

	if (page !== null) {
		searchParams.append('page', `${page}`);
	}

	if (include_folders) {
		searchParams.append('include_folders', 'true');
	}

	if (include_pinned) {
		searchParams.append('include_pinned', 'true');
	}

	const res = await fetch(`${WEBUI_API_BASE_URL}/chats/?${searchParams.toString()}`, {
		method: 'GET',
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			...(token && { authorization: `Bearer ${token}` }),
			...sessionHeader()
		}
	})
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json();
		})
		.then((json) => {
			return json;
		})
		.catch((err) => {
			error = err;
			console.error(err);
			return null;
		});

	if (error) {
		throw error;
	}

	return res.map((chat) => ({
		...chat,
		time_range: getTimeRange(chat.updated_at)
	}));
};

export const getChatCount = async (token: string = '') => {
	let error = null;

	const res = await fetch(`${WEBUI_API_BASE_URL}/chats/count`, {
		method: 'GET',
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			...(token && { authorization: `Bearer ${token}` }),
			...sessionHeader()
		}
	})
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json();
		})
		.catch((err) => {
			error = err;
			console.error(err);
			return null;
		});

	if (error) {
		throw error;
	}

	return res;
};

export const getChatListByUserId = async (
	token: string = '',
	userId: string,
	page: number = 1,
	filter?: object
) => {
	let error = null;

	const searchParams = new URLSearchParams();

	searchParams.append('page', `${page}`);

	if (filter) {
		Object.entries(filter).forEach(([key, value]) => {
			if (value !== undefined && value !== null) {
				searchParams.append(key, value.toString());
			}
		});
	}

	const res = await fetch(
		`${WEBUI_API_BASE_URL}/chats/list/user/${userId}?${searchParams.toString()}`,
		{
			method: 'GET',
			headers: {
				Accept: 'application/json',
				'Content-Type': 'application/json',
				...(token && { authorization: `Bearer ${token}` }),
				...sessionHeader()
			}
		}
	)
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json();
		})
		.then((json) => {
			return json;
		})
		.catch((err) => {
			error = err;
			console.error(err);
			return null;
		});

	if (error) {
		throw error;
	}

	return res.map((chat) => ({
		...chat,
		time_range: getTimeRange(chat.updated_at)
	}));
};

export const getArchivedChatList = async (
	token: string = '',
	page: number = 1,
	filter?: object
) => {
	let error = null;

	const searchParams = new URLSearchParams();
	searchParams.append('page', `${page}`);

	if (filter) {
		Object.entries(filter).forEach(([key, value]) => {
			if (value !== undefined && value !== null) {
				searchParams.append(key, value.toString());
			}
		});
	}

	const res = await fetch(`${WEBUI_API_BASE_URL}/chats/archived?${searchParams.toString()}`, {
		method: 'GET',
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			...(token && { authorization: `Bearer ${token}` }),
			...sessionHeader()
		}
	})
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json();
		})
		.then((json) => {
			return json;
		})
		.catch((err) => {
			error = err;
			console.error(err);
			return null;
		});

	if (error) {
		throw error;
	}

	return res.map((chat) => ({
		...chat,
		time_range: getTimeRange(chat.updated_at)
	}));
};

export const getAllChats = async (token: string) => {
	let error = null;

	const res = await fetch(`${WEBUI_API_BASE_URL}/chats/all`, {
		method: 'GET',
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			...(token && { authorization: `Bearer ${token}` }),
			...sessionHeader()
		}
	})
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json();
		})
		.then((json) => {
			return json;
		})
		.catch((err) => {
			error = err;
			console.error(err);
			return null;
		});

	if (error) {
		throw error;
	}

	return res;
};

export type ChatSearchParams = {
	text?: string;
	page?: number;
	limit?: number;
	folder_ids?: string[];
	tag_ids?: string[];
	pinned?: boolean | null;
	archived?: boolean | null;
	shared?: boolean | null;
	updated_after?: number | null;
	updated_before?: number | null;
	sort?: 'relevance' | 'recent';
};

export type ChatSearchHit = {
	id: string;
	title: string;
	updated_at: number;
	created_at: number;
	archived: boolean;
	pinned: boolean;
	folder_id: string | null;
	snippet: string | null;
	match_count: number;
	matched_message_id: string | null;
	matched_role: string | null;
	score: number;
	time_range?: string;
};

export type ChatSearchFacets = {
	folders: { id: string; name: string; count: number }[];
	tags: { id: string; name: string; count: number }[];
	models: { id: string; name: string; count: number }[];
};

export type ChatSearchResponse = {
	total: number;
	hits: ChatSearchHit[];
	facets: ChatSearchFacets;
	used_fuzzy: boolean;
	did_you_mean: string | null;
};

export const searchChats = async (
	token: string,
	params: ChatSearchParams,
	signal?: AbortSignal
): Promise<ChatSearchResponse> => {
	const qs = new URLSearchParams();
	qs.append('text', params.text ?? '');
	qs.append('page', `${params.page ?? 1}`);
	if (params.limit) qs.append('limit', `${params.limit}`);
	if (params.sort) qs.append('sort', params.sort);
	if (params.pinned !== undefined && params.pinned !== null)
		qs.append('pinned', `${params.pinned}`);
	if (params.archived !== undefined && params.archived !== null)
		qs.append('archived', `${params.archived}`);
	if (params.shared !== undefined && params.shared !== null)
		qs.append('shared', `${params.shared}`);
	if (params.updated_after) qs.append('updated_after', `${params.updated_after}`);
	if (params.updated_before) qs.append('updated_before', `${params.updated_before}`);
	for (const fid of params.folder_ids ?? []) qs.append('folder_ids', fid);
	for (const tid of params.tag_ids ?? []) qs.append('tag_ids', tid);

	const res = await fetch(`${WEBUI_API_BASE_URL}/chats/search?${qs.toString()}`, {
		method: 'GET',
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			...(token && { authorization: `Bearer ${token}` }),
			...sessionHeader()
		},
		signal
	});

	if (!res.ok) {
		throw await res.json();
	}
	const json = (await res.json()) as ChatSearchResponse;
	for (const hit of json.hits ?? []) {
		hit.time_range = getTimeRange(hit.updated_at);
	}
	return json;
};

// Back-compat helper: callers that only need a chat list (no snippets) can
// keep using getChatListBySearchText. Returns a list shape compatible with
// the old signature so legacy callers don't break.
export const getChatListBySearchText = async (token: string, text: string, page: number = 1) => {
	const res = await searchChats(token, { text, page });
	return res.hits.map((h) => ({
		id: h.id,
		title: h.title,
		updated_at: h.updated_at,
		created_at: h.created_at,
		time_range: h.time_range
	}));
};

export const getChatsByFolderId = async (token: string, folderId: string) => {
	let error = null;

	const res = await fetch(`${WEBUI_API_BASE_URL}/chats/folder/${folderId}`, {
		method: 'GET',
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			...(token && { authorization: `Bearer ${token}` }),
			...sessionHeader()
		}
	})
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json();
		})
		.then((json) => {
			return json;
		})
		.catch((err) => {
			error = err;
			console.error(err);
			return null;
		});

	if (error) {
		throw error;
	}

	return res;
};

export const getChatListByFolderId = async (token: string, folderId: string, page: number = 1) => {
	let error = null;

	const searchParams = new URLSearchParams();
	if (page !== null) {
		searchParams.append('page', `${page}`);
	}

	const res = await fetch(
		`${WEBUI_API_BASE_URL}/chats/folder/${folderId}/list?${searchParams.toString()}`,
		{
			method: 'GET',
			headers: {
				Accept: 'application/json',
				'Content-Type': 'application/json',
				...(token && { authorization: `Bearer ${token}` }),
				...sessionHeader()
			}
		}
	)
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json();
		})
		.then((json) => {
			return json;
		})
		.catch((err) => {
			error = err;
			console.error(err);
			return null;
		});

	if (error) {
		throw error;
	}

	return res;
};

export const getAllArchivedChats = async (token: string) => {
	let error = null;

	const res = await fetch(`${WEBUI_API_BASE_URL}/chats/all/archived`, {
		method: 'GET',
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			...(token && { authorization: `Bearer ${token}` }),
			...sessionHeader()
		}
	})
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json();
		})
		.then((json) => {
			return json;
		})
		.catch((err) => {
			error = err;
			console.error(err);
			return null;
		});

	if (error) {
		throw error;
	}

	return res;
};

export const getAllUserChats = async (token: string) => {
	let error = null;

	const res = await fetch(`${WEBUI_API_BASE_URL}/chats/all/db`, {
		method: 'GET',
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			...(token && { authorization: `Bearer ${token}` }),
			...sessionHeader()
		}
	})
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json();
		})
		.then((json) => {
			return json;
		})
		.catch((err) => {
			error = err;
			console.error(err);
			return null;
		});

	if (error) {
		throw error;
	}

	return res;
};

export const getAllTags = async (token: string) => {
	let error = null;

	const res = await fetch(`${WEBUI_API_BASE_URL}/chats/all/tags`, {
		method: 'GET',
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			...(token && { authorization: `Bearer ${token}` }),
			...sessionHeader()
		}
	})
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json();
		})
		.then((json) => {
			return json;
		})
		.catch((err) => {
			error = err;
			console.error(err);
			return null;
		});

	if (error) {
		throw error;
	}

	return res;
};

export const getPinnedChatList = async (token: string = '') => {
	let error = null;

	const res = await fetch(`${WEBUI_API_BASE_URL}/chats/pinned`, {
		method: 'GET',
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			...(token && { authorization: `Bearer ${token}` }),
			...sessionHeader()
		}
	})
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json();
		})
		.then((json) => {
			return json;
		})
		.catch((err) => {
			error = err;
			console.error(err);
			return null;
		});

	if (error) {
		throw error;
	}

	return res.map((chat) => ({
		...chat,
		time_range: getTimeRange(chat.updated_at)
	}));
};

export const getChatListByTagName = async (token: string = '', tagName: string) => {
	let error = null;

	const res = await fetch(`${WEBUI_API_BASE_URL}/chats/tags`, {
		method: 'POST',
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			...(token && { authorization: `Bearer ${token}` }),
			...sessionHeader()
		},
		body: JSON.stringify({
			name: tagName
		})
	})
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json();
		})
		.then((json) => {
			return json;
		})
		.catch((err) => {
			error = err;
			console.error(err);
			return null;
		});

	if (error) {
		throw error;
	}

	return res.map((chat) => ({
		...chat,
		time_range: getTimeRange(chat.updated_at)
	}));
};

export const getChatById = async (token: string, id: string) => {
	let error = null;

	const res = await fetch(`${WEBUI_API_BASE_URL}/chats/${id}`, {
		method: 'GET',
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			...(token && { authorization: `Bearer ${token}` }),
			...sessionHeader()
		}
	})
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json();
		})
		.then((json) => {
			return json;
		})
		.catch((err) => {
			error = err.detail;

			console.error(err);
			return null;
		});

	if (error) {
		throw error;
	}

	return res;
};

// Slim metadata variant of GET /chats/{id} — returns only ids/title/params/models/files/queue
// plus `history: {currentId, sibling_stubs: [{id, parentId, childrenIds, role, model?, modelIdx?}]}`.
// Used by loadChat to avoid downloading every message body on chat open; paginated
// branch messages are fetched separately via getChatMessagesBranch.
// Optional conditional-open support: pass the stored offline entry
// ({data, tags, etag, updatedAt}) and the request goes out with
// If-None-Match. A 304 comes back as an internal {__notModified} marker that
// getChatByIdTail substitutes with the stored copy — no caller above that
// layer ever sees it.
export type ChatEtagEntry = {
	data: any;
	tags?: any[];
	etag?: string;
	updatedAt?: number;
} | null;

export const getChatMeta = async (
	token: string,
	id: string,
	includeTail?: number,
	opts?: { etagEntry?: ChatEtagEntry; tailManifest?: boolean }
) => {
	let error = null;

	// include_tail asks the server to additionally embed the branch page + tags in
	// the meta_only body (Contract 2) so chat open costs ONE request. Old servers
	// ignore the param and simply omit `branch`, which callers detect and fall back.
	//
	// tail_manifest (Contract 3, incremental open): the caller holds a stored copy
	// whose messages carry `_rev` row versions — ask for the branch WINDOW as a
	// lean [{id, parentId, role, rev}] manifest instead of full bodies, so only
	// changed/missing rows are downloaded afterwards. Old servers (and legacy
	// blob-stored chats) ignore it and return `branch`; callers handle both.
	const tailQs = includeTail && includeTail > 0 ? `&include_tail=${includeTail}` : '';
	const manifestQs = opts?.tailManifest ? '&tail_manifest=true' : '';
	const inm = opts?.etagEntry?.etag;
	const res = await fetch(
		`${WEBUI_API_BASE_URL}/chats/${id}?meta_only=true${tailQs}${manifestQs}`,
		{
			method: 'GET',
			headers: {
				Accept: 'application/json',
				'Content-Type': 'application/json',
				...(token && { authorization: `Bearer ${token}` }),
				...(inm ? { 'If-None-Match': inm } : {}),
				...sessionHeader()
			}
		}
	)
		.then(async (res) => {
			// Must precede the !res.ok throw: 304 is not "ok" and has no body.
			if (res.status === 304) {
				return { __notModified: true, __etag: res.headers.get('etag') || inm || null };
			}
			if (!res.ok) {
				const body = await res.json().catch(() => ({}));
				throw { __structuredHttpError: true, status: res.status, detail: body?.detail ?? body };
			}
			const json = await res.json();
			// Validator for the NEXT open — opaque to us, minted by the server.
			// Non-enumerable so it never leaks into clones/serializations.
			try {
				Object.defineProperty(json, '__etag', {
					value: res.headers.get('etag') || null,
					enumerable: false,
					configurable: true
				});
			} catch {
				// non-object body — nothing to tag
			}
			return json;
		})
		.catch((err) => {
			error = toChatFetchError(err);
			console.error(err);
			return null;
		});

	if (error) {
		throw error;
	}

	return res;
};

// Batch message fetch for the incremental open (Contract 3): after diffing the
// branch manifest against the stored copy's `_rev` versions, download ONLY the
// missing/changed rows. `leaf` marks the current leaf so the server's slim
// projection keeps its reasoning_details replay context.
export const getChatMessagesByIds = async (
	token: string,
	id: string,
	messageIds: string[],
	leaf?: string
) => {
	const params = new URLSearchParams();
	params.set('ids', messageIds.join(','));
	if (leaf) params.set('leaf', leaf);

	const res = await fetch(
		`${WEBUI_API_BASE_URL}/chats/${id}/messages/by-ids?${params.toString()}`,
		{
			method: 'GET',
			headers: {
				Accept: 'application/json',
				'Content-Type': 'application/json',
				...(token && { authorization: `Bearer ${token}` }),
				...sessionHeader()
			}
		}
	);
	if (!res.ok) throw await res.json().catch(() => ({ detail: `HTTP ${res.status}` }));
	return res.json();
};

// Complete branch topology with short text previews. This is fetched only
// when Chat Overview opens, keeping ordinary chat-open metadata lean while
// still making every preserved prompt/response version identifiable.
export const getChatMessagesOverview = async (token: string, id: string) => {
	const res = await fetch(`${WEBUI_API_BASE_URL}/chats/${id}/messages/overview`, {
		method: 'GET',
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			...(token && { authorization: `Bearer ${token}` }),
			...sessionHeader()
		}
	});
	if (!res.ok) throw await res.json().catch(() => ({ detail: `HTTP ${res.status}` }));
	return res.json();
};

// Branch-aware ancestor pagination. Walks parent_id from `leaf` toward the root,
// returning up to `limit` messages. `before` slices ancestors strictly older than
// the given message_id (used to load more on scroll up).
export const getChatMessagesBranch = async (
	token: string,
	id: string,
	{ leaf, before, limit = 7 }: { leaf?: string; before?: string; limit?: number } = {},
	signal?: AbortSignal
) => {
	let error = null;

	const params = new URLSearchParams();
	if (leaf) params.set('leaf', leaf);
	if (before) params.set('before', before);
	if (limit !== undefined) params.set('limit', String(limit));

	const res = await fetch(
		`${WEBUI_API_BASE_URL}/chats/${id}/messages${params.toString() ? `?${params.toString()}` : ''}`,
		{
			method: 'GET',
			headers: {
				Accept: 'application/json',
				'Content-Type': 'application/json',
				...(token && { authorization: `Bearer ${token}` }),
				...sessionHeader()
			},
			signal
		}
	)
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json();
		})
		.catch((err) => {
			if (err?.name === 'AbortError') throw err;
			error = err.detail ?? err;
			console.error(err);
			return null;
		});

	if (error) {
		throw error;
	}

	return res;
};

export const stitchChatFromMetaAndMessages = (meta: any, branchPage: any) => {
	if (!meta) return null;

	const messagesMap: Record<string, any> = {};
	for (const stub of meta?.history?.sibling_stubs ?? []) {
		if (!stub?.id) continue;
		messagesMap[stub.id] = {
			...stub,
			id: stub.id,
			parentId: stub.parentId ?? null,
			childrenIds: Array.isArray(stub.childrenIds) ? stub.childrenIds : [],
			role: stub.role,
			content: '',
			_stub: true
		};
	}

	const branchMessages = Array.isArray(branchPage)
		? branchPage
		: Array.isArray(branchPage?.messages)
			? branchPage.messages
			: [];
	for (const msg of branchMessages) {
		if (!msg?.id) continue;
		messagesMap[msg.id] = { ...(messagesMap[msg.id] ?? {}), ...msg, _stub: false };
	}

	return {
		...meta,
		chat: {
			id: meta.id,
			title: meta.title,
			params: meta.params ?? {},
			models: meta.models ?? [],
			files: meta.files ?? [],
			queue: meta.queue ?? [],
			history: {
				currentId: meta?.history?.currentId ?? null,
				messages: messagesMap
			},
			messages: Object.values(messagesMap)
		}
	};
};

export const getChatByIdTail = async (
	token: string,
	id: string,
	limit = 25,
	opts?: { etagEntry?: ChatEtagEntry }
) => {
	// Single-request open (Contract 2): ask the meta_only endpoint to embed the
	// branch page + tags. If the server honored include_tail (`branch` present),
	// stitch from that exactly as the two-request flow would and surface tags via
	// a non-enumerable prop so loadChat can skip a separate getTagsById call. An
	// old server omits `branch`, so we transparently fall back to two requests.
	//
	// Incremental open (Contract 3): when the stored copy's messages carry `_rev`
	// row versions (Postgres xmin, stamped by the server on every serialized
	// message), ask for a branch MANIFEST instead of bodies. The ladder is
	// strictly fail-safe: 304 → stored copy verbatim; manifest → diff + fetch
	// only changed rows; anything surprising → plain full tail.
	const localMessages = opts?.etagEntry?.data?.chat?.history?.messages;
	const tailManifest =
		!!localMessages &&
		Object.values(localMessages).some((m: any) => m && m._rev != null && m._stub !== true);
	const meta = await getChatMeta(token, id, limit, { ...(opts ?? {}), tailManifest });

	if (meta && typeof meta === 'object' && (meta as any).__notModified) {
		// 304 — the server confirmed the stored copy IS the current tail.
		// Substitute it here so no caller ever sees a sentinel: entry.data is a
		// previously stored stitched-tail (fresh JSON.parse per IDB read, owned
		// by this load), shape-identical to a fresh stitch by construction.
		const entry = opts?.etagEntry;
		if (!entry?.data) {
			// Paranoia: the entry vanished between the caller reading it and the
			// response landing — refetch unconditionally.
			return getChatByIdTail(token, id, limit);
		}
		Object.defineProperty(entry.data, '__tailTags', {
			value: Array.isArray(entry.tags) ? entry.tags : [],
			enumerable: false,
			configurable: true
		});
		Object.defineProperty(entry.data, '__etag', {
			value: (meta as any).__etag || entry.etag || null,
			enumerable: false,
			configurable: true
		});
		// Preserve the semantic distinction between an authoritative HTTP 304
		// and a 200 response whose coarse updated_at/currentId happen to match
		// the cached copy. The local-first caller must apply every 200 because
		// message-row revisions can change within the same integer second.
		Object.defineProperty(entry.data, '__notModified', {
			value: true,
			enumerable: false,
			configurable: true
		});
		// A 304 is an AUTHORITATIVE "no live work" answer, not just "unchanged
		// bytes": the server's conditional open force-200s whenever the chat has
		// active tasks/streams (_chat_has_active_work). Stamp the empty state so
		// the client skips the task-ids and active-streams round-trips entirely.
		Object.defineProperty(entry.data, '__active', {
			value: { task_ids: [], streams: [] },
			enumerable: false,
			configurable: true
		});
		return entry.data;
	}

	const etag = (meta as any)?.__etag ?? null;

	if (meta && typeof meta === 'object' && 'branch_manifest' in meta) {
		// Contract 3 response: the current-branch window as [{id, parentId, role,
		// rev}], no bodies. Reuse every local message whose _rev still matches;
		// batch-fetch the rest; deletions fall out because the stitch below
		// rebuilds the message map from the manifest alone (a local row absent
		// from the manifest simply never enters the new view).
		const { branch_manifest, tags, active, ...metaOnly } = meta as any;
		const manifest = Array.isArray(branch_manifest) ? branch_manifest : [];
		const local = localMessages ?? {};
		const reuse: Record<string, any> = {};
		const needIds: string[] = [];
		for (const row of manifest) {
			if (!row?.id) continue;
			const lm = (local as any)[row.id];
			if (lm && lm._stub !== true && lm._rev != null && String(lm._rev) === String(row.rev)) {
				reuse[row.id] = lm;
			} else {
				needIds.push(row.id);
			}
		}

		// Degenerate diff (nothing reusable / empty window): a plain full tail is
		// the same bytes in ONE round-trip, so don't pay the extra one. The
		// recursive call drops etagEntry, so it can never re-enter this branch.
		if (manifest.length === 0 || needIds.length >= manifest.length) {
			return getChatByIdTail(token, id, limit);
		}

		let fetched: any[] = [];
		if (needIds.length > 0) {
			try {
				fetched = await getChatMessagesByIds(
					token,
					id,
					needIds,
					metaOnly?.history?.currentId ?? undefined
				);
			} catch (err) {
				console.error('incremental open: by-ids fetch failed, full tail fallback', err);
				return getChatByIdTail(token, id, limit);
			}
			if (!Array.isArray(fetched)) {
				return getChatByIdTail(token, id, limit);
			}
		}

		const fetchedById = new Map(fetched.filter((m: any) => m?.id).map((m: any) => [m.id, m]));
		const branchPage: any[] = [];
		for (const row of manifest) {
			if (!row?.id) continue;
			const msg = reuse[row.id] ?? fetchedById.get(row.id);
			if (!msg) {
				// A row vanished between the manifest and the batch fetch (delete
				// race) — the window is no longer coherent; take the full tail.
				return getChatByIdTail(token, id, limit);
			}
			branchPage.push(msg);
		}

		const stitched = stitchChatFromMetaAndMessages(metaOnly, branchPage);
		if (stitched) {
			Object.defineProperty(stitched, '__tailTags', {
				value: Array.isArray(tags) ? tags : [],
				enumerable: false,
				configurable: true
			});
			Object.defineProperty(stitched, '__etag', {
				value: etag,
				enumerable: false,
				configurable: true
			});
			// Bundled task/stream state (destructured out of the meta so it never
			// stitches into — or persists with — the chat body). Non-enumerable:
			// stored copies must NOT carry stale activity claims.
			if (active && typeof active === 'object') {
				Object.defineProperty(stitched, '__active', {
					value: active,
					enumerable: false,
					configurable: true
				});
			}
		}
		return stitched;
	}

	if (meta && typeof meta === 'object' && 'branch' in meta) {
		const { branch, tags, active, ...metaOnly } = meta as any;
		const stitched = stitchChatFromMetaAndMessages(metaOnly, branch);
		if (stitched) {
			Object.defineProperty(stitched, '__tailTags', {
				value: Array.isArray(tags) ? tags : [],
				enumerable: false,
				configurable: true
			});
			Object.defineProperty(stitched, '__etag', {
				value: etag,
				enumerable: false,
				configurable: true
			});
			if (active && typeof active === 'object') {
				Object.defineProperty(stitched, '__active', {
					value: active,
					enumerable: false,
					configurable: true
				});
			}
		}
		return stitched;
	}

	const leafId = meta?.history?.currentId;
	const branchPage = leafId
		? await getChatMessagesBranch(token, id, { leaf: leafId, limit }).catch(() => null)
		: [];

	const stitched = stitchChatFromMetaAndMessages(meta, branchPage);
	if (stitched && etag) {
		Object.defineProperty(stitched, '__etag', {
			value: etag,
			enumerable: false,
			configurable: true
		});
	}
	return stitched;
};

// Sibling-branch lazy load — hits GET /chats/{id}/messages/{message_id}/siblings.
// Returns full-content messages sharing the given message's parent_id (used when
// the user clicks a branch-switch arrow and the target branch wasn't paginated in).
export const getChatMessagesSiblings = async (token: string, id: string, message_id: string) => {
	let error = null;

	const res = await fetch(`${WEBUI_API_BASE_URL}/chats/${id}/messages/${message_id}/siblings`, {
		method: 'GET',
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			...(token && { authorization: `Bearer ${token}` }),
			...sessionHeader()
		}
	})
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json();
		})
		.catch((err) => {
			error = err.detail ?? err;
			console.error(err);
			return null;
		});

	if (error) {
		throw error;
	}

	return res;
};

export const getChatMessageToolResult = async (
	token: string,
	id: string,
	message_id: string,
	tool_call_id: string
) => {
	let firstError = null;

	const fetchResult = async (url: string) =>
		fetch(url, {
			method: 'GET',
			headers: {
				Accept: 'application/json',
				'Content-Type': 'application/json',
				...(token && { authorization: `Bearer ${token}` }),
				...sessionHeader()
			}
		}).then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json();
		});

	// Also serves lazy reasoning bodies (ref = "reasoning:{block_index}") —
	// encode so the ref rides safely as a path segment.
	const ref = encodeURIComponent(tool_call_id);

	try {
		return await fetchResult(
			`${WEBUI_API_BASE_URL}/chats/${id}/messages/${message_id}/tool-results/${ref}`
		);
	} catch (err) {
		firstError = err;
	}

	try {
		return await fetchResult(
			`${WEBUI_API_BASE_URL}/chats/share/${id}/messages/${message_id}/tool-results/${ref}`
		);
	} catch (err) {
		console.error(err);
		throw (err as any)?.detail ?? (firstError as any)?.detail ?? err ?? firstError;
	}
};

// The `<compacted_context>` a `compaction` content block stands for. Only the
// narrative is stored on the block; the mechanical sections (user instructions +
// tool-call index) are a pure function of the tree, so the server re-renders the
// whole envelope from the same generators the outbound path uses.
export const getChatMessageCompaction = async (
	token: string,
	id: string,
	message_id: string,
	block_index: number
) => {
	const res = await fetch(
		`${WEBUI_API_BASE_URL}/chats/${id}/messages/${message_id}/compaction/${block_index}`,
		{
			method: 'GET',
			headers: {
				Accept: 'application/json',
				'Content-Type': 'application/json',
				...(token && { authorization: `Bearer ${token}` }),
				...sessionHeader()
			}
		}
	).then(async (r) => {
		if (!r.ok) throw await r.json();
		return r.json();
	});

	return res;
};

export const compactChat = async (token: string, id: string, model: string, leaf_id?: string) => {
	const res = await fetch(`${WEBUI_API_BASE_URL}/chats/${id}/compact`, {
		method: 'POST',
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			...(token && { authorization: `Bearer ${token}` }),
			...sessionHeader()
		},
		body: JSON.stringify({ model, leaf_id })
	}).then(async (r) => {
		if (!r.ok) throw await r.json();
		return r.json();
	});

	return res;
};

export const getChatByShareId = async (token: string, share_id: string) => {
	let error = null;

	const res = await fetch(`${WEBUI_API_BASE_URL}/chats/share/${share_id}`, {
		method: 'GET',
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			...(token && { authorization: `Bearer ${token}` }),
			...sessionHeader()
		}
	})
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json();
		})
		.then((json) => {
			return json;
		})
		.catch((err) => {
			error = err;

			console.error(err);
			return null;
		});

	if (error) {
		throw error;
	}

	return res;
};

export const getChatPinnedStatusById = async (token: string, id: string) => {
	let error = null;

	const res = await fetch(`${WEBUI_API_BASE_URL}/chats/${id}/pinned`, {
		method: 'GET',
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			...(token && { authorization: `Bearer ${token}` }),
			...sessionHeader()
		}
	})
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json();
		})
		.then((json) => {
			return json;
		})
		.catch((err) => {
			error = err;

			if ('detail' in err) {
				error = err.detail;
			} else {
				error = err;
			}

			console.error(err);
			return null;
		});

	if (error) {
		throw error;
	}

	return res;
};

export const toggleChatPinnedStatusById = async (token: string, id: string) => {
	let error = null;

	const res = await fetch(`${WEBUI_API_BASE_URL}/chats/${id}/pin`, {
		method: 'POST',
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			...(token && { authorization: `Bearer ${token}` }),
			...sessionHeader()
		}
	})
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json();
		})
		.then((json) => {
			return json;
		})
		.catch((err) => {
			error = err;

			if ('detail' in err) {
				error = err.detail;
			} else {
				error = err;
			}

			console.error(err);
			return null;
		});

	if (error) {
		throw error;
	}

	return res;
};

export const cloneChatById = async (token: string, id: string, title?: string) => {
	let error = null;

	const res = await fetch(`${WEBUI_API_BASE_URL}/chats/${id}/clone`, {
		method: 'POST',
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			...(token && { authorization: `Bearer ${token}` }),
			...sessionHeader()
		},
		body: JSON.stringify({
			...(title && { title: title })
		})
	})
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json();
		})
		.then((json) => {
			return json;
		})
		.catch((err) => {
			error = err;

			if ('detail' in err) {
				error = err.detail;
			} else {
				error = err;
			}

			console.error(err);
			return null;
		});

	if (error) {
		throw error;
	}

	return res;
};

export const cloneSharedChatById = async (token: string, id: string) => {
	let error = null;

	const res = await fetch(`${WEBUI_API_BASE_URL}/chats/${id}/clone/shared`, {
		method: 'POST',
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			...(token && { authorization: `Bearer ${token}` }),
			...sessionHeader()
		}
	})
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json();
		})
		.then((json) => {
			return json;
		})
		.catch((err) => {
			error = err;

			if ('detail' in err) {
				error = err.detail;
			} else {
				error = err;
			}

			console.error(err);
			return null;
		});

	if (error) {
		throw error;
	}

	return res;
};

export const shareChatById = async (token: string, id: string, mode?: string) => {
	let error = null;

	const res = await fetch(`${WEBUI_API_BASE_URL}/chats/${id}/share`, {
		method: 'POST',
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			...(token && { authorization: `Bearer ${token}` }),
			...sessionHeader()
		},
		body: JSON.stringify(mode ? { mode } : {})
	})
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json();
		})
		.then((json) => {
			return json;
		})
		.catch((err) => {
			error = err;

			console.error(err);
			return null;
		});

	if (error) {
		throw error;
	}

	return res;
};

export const getSharedChatModels = async (token: string, share_id: string) => {
	let error = null;

	const res = await fetch(`${WEBUI_API_BASE_URL}/chats/share/${share_id}/models`, {
		method: 'GET',
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			...(token && { authorization: `Bearer ${token}` }),
			...sessionHeader()
		}
	})
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json();
		})
		.then((json) => {
			return json;
		})
		.catch((err) => {
			error = err;

			console.error(err);
			return null;
		});

	if (error) {
		throw error;
	}

	return res;
};

export const updateChatFolderIdById = async (token: string, id: string, folderId?: string) => {
	let error = null;

	const res = await fetch(`${WEBUI_API_BASE_URL}/chats/${id}/folder`, {
		method: 'POST',
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			...(token && { authorization: `Bearer ${token}` }),
			...sessionHeader()
		},
		body: JSON.stringify({
			folder_id: folderId
		})
	})
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json();
		})
		.then((json) => {
			return json;
		})
		.catch((err) => {
			error = err;

			console.error(err);
			return null;
		});

	if (error) {
		throw error;
	}

	return res;
};

export const archiveChatById = async (token: string, id: string) => {
	let error = null;

	const res = await fetch(`${WEBUI_API_BASE_URL}/chats/${id}/archive`, {
		method: 'POST',
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			...(token && { authorization: `Bearer ${token}` }),
			...sessionHeader()
		}
	})
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json();
		})
		.then((json) => {
			return json;
		})
		.catch((err) => {
			error = err;

			console.error(err);
			return null;
		});

	if (error) {
		throw error;
	}

	return res;
};

export const deleteSharedChatById = async (token: string, id: string) => {
	let error = null;

	const res = await fetch(`${WEBUI_API_BASE_URL}/chats/${id}/share`, {
		method: 'DELETE',
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			...(token && { authorization: `Bearer ${token}` }),
			...sessionHeader()
		}
	})
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json();
		})
		.then((json) => {
			return json;
		})
		.catch((err) => {
			error = err;

			console.error(err);
			return null;
		});

	if (error) {
		throw error;
	}

	return res;
};

export type PatchChatOp =
	| { op: 'set_param'; key: string; value: unknown }
	| { op: 'set_meta'; key: string; value: unknown }
	| { op: 'set_models'; models: string[] }
	| { op: 'set_files'; files: unknown[] }
	| { op: 'set_queue'; queue: unknown[] }
	| { op: 'append_queue_item'; item: Record<string, unknown> }
	| { op: 'remove_queue_item'; item_id: string }
	| { op: 'update_queue_item'; item: Record<string, unknown> }
	| { op: 'set_question_state'; tool_call_id: string; patch: Record<string, unknown> }
	| { op: 'set_tags'; tags: unknown[] }
	| { op: 'set_history_current_id'; current_id: string }
	| {
			op: 'fork_message_version';
			message_id: string;
			source_message_id: string;
			content: unknown;
			files?: unknown[];
			models?: string[];
	  }
	| {
			op: 'append_message';
			message_id: string;
			parent_id: string | null;
			role: string;
			content: unknown;
			[k: string]: unknown;
	  }
	| {
			op: 'update_message_content';
			message_id: string;
			content: unknown;
			files?: unknown[];
			[k: string]: unknown;
	  }
	| { op: 'set_message_annotation'; message_id: string; annotation: unknown }
	| { op: 'delete_message'; message_id: string };

const patchChatTails = new Map<string, Promise<void>>();

const patchChatNow = async (token: string, id: string, ops: PatchChatOp[]) => {
	const res = await fetch(`${WEBUI_API_BASE_URL}/chats/${id}`, {
		method: 'PATCH',
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			...(token && { authorization: `Bearer ${token}` }),
			...sessionHeader()
		},
		body: JSON.stringify({ ops })
	});
	if (!res.ok) {
		const error = await res.json();
		console.error(error);
		throw error;
	}
	return res.json();
};

/**
 * Serialize every PATCH mutation of one chat at the API boundary.
 *
 * A stopped generation can finish its placeholder PATCH after the UI is ready
 * for the next turn. Without one shared queue, that next turn's child PATCH can
 * overtake its parent and leave a durable branch with a missing ancestor.
 * Different chats remain independent, and a failed mutation does not poison the
 * queue for later recovery writes.
 */
export const patchChat = (token: string, id: string, ops: PatchChatOp[]) => {
	const previous = patchChatTails.get(id) ?? Promise.resolve();
	const result = previous.then(() => patchChatNow(token, id, ops));
	const tail = result.then(
		() => undefined,
		() => undefined
	);
	patchChatTails.set(id, tail);
	void tail.then(() => {
		if (patchChatTails.get(id) === tail) patchChatTails.delete(id);
	});
	return result;
};

// Immediately drain the next queued message for a chat (server-driven). Used by
// the "Send now" affordance to resume a queue the user paused by pressing Stop —
// the backend pops the head and starts its generation (then chains the rest on
// clean completion), exactly like an automatic post-completion drain. No-ops if a
// generation is already in flight (the drain ownership marker guards it).
export const drainChatQueue = async (token: string, id: string) => {
	let error = null;

	const res = await fetch(`${WEBUI_API_BASE_URL}/chats/${id}/queue/drain`, {
		method: 'POST',
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			...(token && { authorization: `Bearer ${token}` }),
			...sessionHeader()
		}
	})
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json();
		})
		.catch((err) => {
			error = err;
			console.error(err);
			return null;
		});

	if (error) {
		throw error;
	}

	return res;
};

export const updateChatById = async (token: string, id: string, chat: object) => {
	let error = null;

	const res = await fetch(`${WEBUI_API_BASE_URL}/chats/${id}`, {
		method: 'POST',
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			...(token && { authorization: `Bearer ${token}` }),
			...sessionHeader()
		},
		body: JSON.stringify({
			chat: chat
		})
	})
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json();
		})
		.then((json) => {
			return json;
		})
		.catch((err) => {
			error = err;

			console.error(err);
			return null;
		});

	if (error) {
		throw error;
	}

	return res;
};

export const deleteChatById = async (token: string, id: string) => {
	let error = null;

	const res = await fetch(`${WEBUI_API_BASE_URL}/chats/${id}`, {
		method: 'DELETE',
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			...(token && { authorization: `Bearer ${token}` }),
			...sessionHeader()
		}
	})
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json();
		})
		.then((json) => {
			return json;
		})
		.catch((err) => {
			error = err.detail;

			console.error(err);
			return null;
		});

	if (error) {
		throw error;
	}

	return res;
};

export const getTagsById = async (token: string, id: string) => {
	let error = null;

	const res = await fetch(`${WEBUI_API_BASE_URL}/chats/${id}/tags`, {
		method: 'GET',
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			...(token && { authorization: `Bearer ${token}` }),
			...sessionHeader()
		}
	})
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json();
		})
		.then((json) => {
			return json;
		})
		.catch((err) => {
			error = err;

			console.error(err);
			return null;
		});

	if (error) {
		throw error;
	}

	return res;
};

export const addTagById = async (token: string, id: string, tagName: string) => {
	let error = null;

	const res = await fetch(`${WEBUI_API_BASE_URL}/chats/${id}/tags`, {
		method: 'POST',
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			...(token && { authorization: `Bearer ${token}` }),
			...sessionHeader()
		},
		body: JSON.stringify({
			name: tagName
		})
	})
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json();
		})
		.then((json) => {
			return json;
		})
		.catch((err) => {
			error = err.detail;
			console.error(err);
			return null;
		});

	if (error) {
		throw error;
	}

	return res;
};

export const deleteTagById = async (token: string, id: string, tagName: string) => {
	let error = null;

	const res = await fetch(`${WEBUI_API_BASE_URL}/chats/${id}/tags`, {
		method: 'DELETE',
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			...(token && { authorization: `Bearer ${token}` }),
			...sessionHeader()
		},
		body: JSON.stringify({
			name: tagName
		})
	})
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json();
		})
		.then((json) => {
			return json;
		})
		.catch((err) => {
			error = err;

			console.error(err);
			return null;
		});

	if (error) {
		throw error;
	}

	return res;
};
export const deleteTagsById = async (token: string, id: string) => {
	let error = null;

	const res = await fetch(`${WEBUI_API_BASE_URL}/chats/${id}/tags/all`, {
		method: 'DELETE',
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			...(token && { authorization: `Bearer ${token}` }),
			...sessionHeader()
		}
	})
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json();
		})
		.then((json) => {
			return json;
		})
		.catch((err) => {
			error = err;

			console.error(err);
			return null;
		});

	if (error) {
		throw error;
	}

	return res;
};

export const deleteAllChats = async (token: string) => {
	let error = null;

	const res = await fetch(`${WEBUI_API_BASE_URL}/chats/`, {
		method: 'DELETE',
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			...(token && { authorization: `Bearer ${token}` }),
			...sessionHeader()
		}
	})
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json();
		})
		.then((json) => {
			return json;
		})
		.catch((err) => {
			error = err.detail;

			console.error(err);
			return null;
		});

	if (error) {
		throw error;
	}

	return res;
};

export const archiveAllChats = async (token: string) => {
	let error = null;

	const res = await fetch(`${WEBUI_API_BASE_URL}/chats/archive/all`, {
		method: 'POST',
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			...(token && { authorization: `Bearer ${token}` }),
			...sessionHeader()
		}
	})
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json();
		})
		.then((json) => {
			return json;
		})
		.catch((err) => {
			error = err.detail;

			console.error(err);
			return null;
		});

	if (error) {
		throw error;
	}

	return res;
};

export const browserHumanHandoff = async (
	token: string,
	chatId: string,
	payload:
		| { session: string; action: 'snapshot' }
		| { session: string; action: 'click'; x: number; y: number }
		| { session: string; action: 'drag'; x: number; y: number; x2: number; y2: number }
		| { session: string; action: 'scroll'; delta_y: number; delta_x?: number }
		| { session: string; action: 'type'; text: string; x?: number; y?: number }
		| { session: string; action: 'dismiss' }
) => {
	const res = await fetch(
		`${WEBUI_API_BASE_URL}/chats/${encodeURIComponent(chatId)}/browser/handoff`,
		{
			method: 'POST',
			headers: {
				Accept: 'application/json',
				'Content-Type': 'application/json',
				...(token && { authorization: `Bearer ${token}` })
			},
			body: JSON.stringify(payload)
		}
	);

	if (!res.ok) {
		const error = await res.json().catch(() => ({ detail: res.statusText }));
		throw error;
	}
	return res.json();
};

/**
 * Lightweight live browser state for the panel's verification poller.
 * Host-file read only (no container round-trip, no detection re-run, no daemon
 * mutex contention) — the replacement for the old POST-snapshot refresh loop.
 * Returns 204-shaped null when nothing is available.
 */
export const browserLiveFrame = async (token: string, chatId: string): Promise<any | null> => {
	const res = await fetch(
		`${WEBUI_API_BASE_URL}/streams/browser/live?chat_id=${encodeURIComponent(chatId)}`,
		{
			method: 'GET',
			headers: {
				Accept: 'application/json',
				...(token && { authorization: `Bearer ${token}` })
			}
		}
	);
	if (res.status === 204) return null;
	if (!res.ok) {
		throw await res.json().catch(() => ({ detail: res.statusText }));
	}
	return res.json();
};
