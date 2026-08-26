// Intrinsic image sizes, known BEFORE the bytes arrive.
//
// An <img> with no reserved box is the single biggest source of layout shift in
// a chat transcript: it lays out at zero height, then snaps to 400-800px the
// moment it decodes, shoving every turn below it. Scrolling back through an old
// conversation is where it hurts most — images realize as they approach the
// viewport, so the reader is looking straight at the thing that moves.
//
// The cure is the boring, canonical one: give every <img> width/height
// attributes so the browser reserves the exact box up front. This module is
// where those numbers come from, cheapest source first:
//
//   1. an in-memory / localStorage cache of sizes measured on a previous view
//      (exact, zero cost, survives reloads — the common case),
//   2. one BATCHED `POST /files/dimensions` covering every locally-stored image
//      about to render (first view of a chat; the server memoizes into
//      file.meta so it is a PIL header read once per file, ever),
//   3. the decoded bytes themselves (always exact, but only known once the
//      image has loaded — at which point it is too late to avoid the shift, so
//      the value is cached for next time).
//
// Sizes are cached per served variant: inline images request a width-capped
// thumbnail, so `f:{id}@768` (what actually renders) is tracked separately from
// `f:{id}` (the original the server reports).

import { WEBUI_API_BASE_URL } from '$lib/constants';

export type ImageDims = { width: number; height: number };

const STORAGE_KEY = 'owui:image-dims:v1';
// Each entry is ~28 bytes of JSON; 4k entries is a ~110KB budget for a cache
// that removes visible jank from every chat the user has ever opened.
const MAX_ENTRIES = 4000;

const memory = new Map<string, ImageDims>();
let hydrated = false;
let flushTimer: ReturnType<typeof setTimeout> | null = null;
let dirty = false;

const isBrowser = typeof window !== 'undefined';

const hydrate = () => {
	if (hydrated || !isBrowser) return;
	hydrated = true;
	try {
		const raw = localStorage.getItem(STORAGE_KEY);
		if (!raw) return;
		const parsed = JSON.parse(raw);
		if (!parsed || typeof parsed !== 'object') return;
		for (const [key, value] of Object.entries(parsed)) {
			if (Array.isArray(value) && value.length === 2) {
				const [width, height] = value as [number, number];
				if (width > 0 && height > 0) memory.set(key, { width, height });
			}
		}
	} catch {
		// Corrupt/oversized payload — start clean rather than break rendering.
	}
};

const persist = () => {
	if (!isBrowser || !dirty) return;
	dirty = false;
	try {
		// Map iterates in insertion order, so trimming from the front drops the
		// least recently WRITTEN entries (re-measuring a dropped image costs one
		// load, nothing more).
		const entries = [...memory.entries()];
		const kept =
			entries.length > MAX_ENTRIES ? entries.slice(entries.length - MAX_ENTRIES) : entries;
		if (kept.length !== entries.length) {
			memory.clear();
			for (const [key, value] of kept) memory.set(key, value);
		}
		const payload: Record<string, [number, number]> = {};
		for (const [key, value] of kept) payload[key] = [value.width, value.height];
		localStorage.setItem(STORAGE_KEY, JSON.stringify(payload));
	} catch {
		// Quota / private mode: the in-memory cache still serves this session.
	}
};

const schedulePersist = () => {
	dirty = true;
	if (flushTimer !== null) return;
	flushTimer = setTimeout(() => {
		flushTimer = null;
		persist();
	}, 500);
};

if (isBrowser) {
	// pagehide (not unload) is the reliable last-write hook on iOS Safari.
	window.addEventListener('pagehide', persist);
}

/** Cache key for a locally stored file, optionally for a width-capped variant. */
export const fileDimsKey = (fileId: string, width?: number | null) =>
	width ? `f:${fileId}@${width}` : `f:${fileId}`;

/** Cache key for a remote URL. data:/blob: URLs are deliberately not cacheable. */
export const urlDimsKey = (url: string) =>
	url && !url.startsWith('data:') && !url.startsWith('blob:') ? `u:${url}` : '';

export const getCachedImageDims = (key: string): ImageDims | null => {
	if (!key) return null;
	hydrate();
	return memory.get(key) ?? null;
};

export const cacheImageDims = (key: string, dims: ImageDims | null | undefined) => {
	if (!key || !dims) return;
	const width = Math.round(dims.width);
	const height = Math.round(dims.height);
	if (!(width > 0) || !(height > 0)) return;
	hydrate();
	const existing = memory.get(key);
	if (existing && existing.width === width && existing.height === height) return;
	// Re-insert so the entry moves to the young end of the trim order.
	memory.delete(key);
	memory.set(key, { width, height });
	schedulePersist();
};

/**
 * Scale `dims` to fit inside a `box`×`box` square, never upscaling — the same
 * transform PIL's `Image.thumbnail` applies server-side, so the reserved box
 * matches the served thumbnail (worst case ±1px of rounding, which the
 * scroll-anchoring engine absorbs).
 */
export const fitInsideBox = (dims: ImageDims, box: number): ImageDims => {
	const { width, height } = dims;
	if (!(width > 0) || !(height > 0) || !(box > 0)) return dims;
	if (width <= box && height <= box) return dims;
	const aspect = width / height;
	let x = box;
	let y = box;
	if (x / y >= aspect) {
		x = Math.max(Math.round(y * aspect), 1);
	} else {
		y = Math.max(Math.round(x / aspect), 1);
	}
	return { width: x, height: y };
};

// ---- Batched server lookup ------------------------------------------------
//
// Every Image component that mounts without a cached size asks for one; the
// asks are coalesced into a single request per animation-frame-ish window, so
// opening a chat with 30 photos costs ONE small JSON round trip.

type PendingEntry = {
	resolvers: ((dims: ImageDims | null) => void)[];
};

const pending = new Map<string, Map<string, PendingEntry>>(); // shareKey -> fileId -> entry
const requested = new Set<string>(); // shareKey|fileId already asked for (miss included)
let batchTimer: ReturnType<typeof setTimeout> | null = null;

const BATCH_WINDOW_MS = 16;

const flushBatch = async () => {
	batchTimer = null;
	const batches = [...pending.entries()];
	pending.clear();

	for (const [shareKey, entries] of batches) {
		const ids = [...entries.keys()];
		const shareId = shareKey || null;
		let dimensions: Record<string, ImageDims> = {};
		try {
			const res = await fetch(`${WEBUI_API_BASE_URL}/files/dimensions`, {
				method: 'POST',
				headers: {
					'Content-Type': 'application/json',
					...(typeof localStorage !== 'undefined' && localStorage.token
						? { authorization: `Bearer ${localStorage.token}` }
						: {})
				},
				body: JSON.stringify({ ids, ...(shareId ? { share_id: shareId } : {}) })
			});
			if (res.ok) {
				const json = await res.json();
				dimensions = json?.dimensions ?? {};
			}
		} catch {
			// Offline / server without the endpoint: fall back to measuring on load.
		}

		for (const [fileId, entry] of entries) {
			const dims = dimensions[fileId];
			const value =
				dims && dims.width > 0 && dims.height > 0
					? { width: dims.width, height: dims.height }
					: null;
			if (value) cacheImageDims(fileDimsKey(fileId), value);
			for (const resolve of entry.resolvers) resolve(value);
		}
	}
};

/**
 * Intrinsic size of a locally stored image file, from the server. Resolves null
 * when unknown (non-raster, unauthorized, offline) — callers then reserve
 * nothing and measure on load. Asked at most once per file per session.
 */
export const fetchFileImageDims = (
	fileId: string,
	shareId?: string | null
): Promise<ImageDims | null> => {
	if (!fileId || !isBrowser) return Promise.resolve(null);

	const cached = getCachedImageDims(fileDimsKey(fileId));
	if (cached) return Promise.resolve(cached);

	const shareKey = shareId ?? '';
	const requestKey = `${shareKey}|${fileId}`;
	if (requested.has(requestKey) && !pending.get(shareKey)?.has(fileId)) {
		// Already asked and answered (a miss) — don't re-ask on every remount.
		return Promise.resolve(null);
	}
	requested.add(requestKey);

	let group = pending.get(shareKey);
	if (!group) {
		group = new Map();
		pending.set(shareKey, group);
	}
	let entry = group.get(fileId);
	if (!entry) {
		entry = { resolvers: [] };
		group.set(fileId, entry);
	}

	if (batchTimer === null) {
		batchTimer = setTimeout(flushBatch, BATCH_WINDOW_MS);
	}

	return new Promise<ImageDims | null>((resolve) => {
		entry!.resolvers.push(resolve);
	});
};

/** Measure an already-loaded <img> and remember it under every relevant key. */
export const rememberLoadedImage = (img: HTMLImageElement, keys: (string | null | undefined)[]) => {
	const width = img.naturalWidth;
	const height = img.naturalHeight;
	if (!(width > 0) || !(height > 0)) return null;
	const dims = { width, height };
	for (const key of keys) {
		if (key) cacheImageDims(key, dims);
	}
	return dims;
};
