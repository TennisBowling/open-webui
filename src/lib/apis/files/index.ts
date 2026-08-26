import { v4 as uuidv4 } from 'uuid';
import { WEBUI_API_BASE_URL } from '$lib/constants';

export type UploadFileOptions = {
	onProgress?: (progress: number) => void;
	// Optional: called whenever the retry wrapper is about to retry a failed
	// attempt, e.g. to surface "retrying (2/3)..." in the UI. Purely
	// informational - callers that don't pass this still get retries.
	onRetry?: (attempt: number, maxAttempts: number, err: UploadError) => void;
	signal?: AbortSignal;
	process?: boolean;
	processInBackground?: boolean;
};

// Typed upload failure so the retry wrapper can make retry decisions without
// brittle string-matching on error messages.
//   - 'network': onerror/ontimeout - transient, retry-eligible.
//   - 'abort': explicit user/caller cancellation - never retry.
//   - 'http': server responded with a status. Only 5xx (or a missing status,
//     which XHR reports as 0 on some connection failures) is retry-eligible;
//     4xx means the server understood and rejected the request (bad auth,
//     file too large/wrong type, etc.) and retrying cannot help.
export type UploadErrorKind = 'network' | 'abort' | 'http';

export class UploadError extends Error {
	kind: UploadErrorKind;
	status?: number;

	constructor(kind: UploadErrorKind, message: string, status?: number) {
		super(message);
		this.name = 'UploadError';
		this.kind = kind;
		this.status = status;
	}

	get retryable(): boolean {
		if (this.kind === 'network') return true;
		if (this.kind === 'http') return !this.status || this.status >= 500;
		return false;
	}
}

const tryParseJson = (text: string) => {
	try {
		return JSON.parse(text);
	} catch {
		return null;
	}
};

const coerceToString = (value: any) => {
	if (typeof value === 'string') return value;
	if (typeof value === 'number' || typeof value === 'boolean') return String(value);
	if (value instanceof Error) return value.message || String(value);
	if (value === null || value === undefined) return '';
	try {
		return JSON.stringify(value);
	} catch {
		return String(value);
	}
};

// "No progress" timeout: if the upload hasn't emitted a progress tick (or
// completed) within this window of its own start/last-progress point, we
// treat the connection as dead and fail with a retry-eligible network error.
// This is intentionally NOT a flat total-upload-time timeout - a large file
// crawling along on bad cellular but still making progress must not be
// killed, so xhr.timeout is bumped forward on every onprogress tick.
const UPLOAD_NO_PROGRESS_TIMEOUT_MS = 30_000;

const uploadFileWithProgress = async (
	token: string,
	url: string,
	formData: FormData,
	onProgress: (progress: number) => void,
	signal?: AbortSignal
) => {
	let error: UploadError | null = null;

	const res = await new Promise<any | null>((resolve) => {
		const xhr = new XMLHttpRequest();
		const startTime = Date.now();
		const abortHandler = () => {
			try {
				xhr.abort();
			} catch (err) {
				// Ignore abort errors
			}
		};

		xhr.open('POST', url, true);
		// Note: Do NOT set xhr.responseType = 'json' as it causes issues on iOS Safari
		// where accessing xhr.responseText throws InvalidStateError.
		// Instead, we parse the response text manually.
		xhr.setRequestHeader('Accept', 'application/json');
		xhr.setRequestHeader('authorization', `Bearer ${token}`);

		// Effective "no progress for N seconds" timeout - see comment above.
		xhr.timeout = UPLOAD_NO_PROGRESS_TIMEOUT_MS;

		if (signal) {
			if (signal.aborted) {
				error = new UploadError('abort', 'Upload aborted');
				resolve(null);
				return;
			}
			signal.addEventListener('abort', abortHandler);
		}

		if (xhr.upload) {
			xhr.upload.onprogress = (event) => {
				// Bump the timeout forward from "now" so it only fires after a
				// stretch of true silence, not merely a slow-but-live transfer.
				xhr.timeout = Date.now() - startTime + UPLOAD_NO_PROGRESS_TIMEOUT_MS;

				if (!event.lengthComputable) return;
				const progress = Math.round((event.loaded / event.total) * 100);
				onProgress(Math.max(0, Math.min(100, progress)));
			};
		}

		xhr.onload = () => {
			if (signal) signal.removeEventListener('abort', abortHandler);
			const status = xhr.status;

			// Parse response - use responseText since we're not setting responseType
			let response: any = null;
			try {
				const responseText = xhr.responseText || '';
				if (responseText) {
					response = tryParseJson(responseText);
				}
			} catch (e) {
				// On some browsers/environments, accessing responseText might fail
				// In that case, try xhr.response as fallback
				try {
					response = xhr.response;
					if (typeof response === 'string') {
						response = tryParseJson(response);
					}
				} catch {
					// Ignore fallback errors
				}
			}

			if (status >= 200 && status < 300) {
				if (response && typeof response === 'object') {
					resolve(response);
					return;
				}

				error = new UploadError(
					'http',
					'Upload succeeded but the server returned an invalid response',
					status
				);
				resolve(null);
				return;
			}

			let message: any = null;
			if (typeof response === 'string' && response) {
				message = response;
			} else if (response?.detail) {
				message = response.detail;
			} else if (response?.message) {
				message = response.message;
			} else if (xhr.statusText) {
				message = xhr.statusText;
			} else {
				message = `Upload failed with status ${status}`;
			}

			const messageText = coerceToString(message) || `Upload failed with status ${status}`;
			error = new UploadError(
				'http',
				status ? `HTTP ${status}: ${messageText}` : messageText,
				status || undefined
			);
			resolve(null);
		};

		xhr.onerror = () => {
			if (signal) signal.removeEventListener('abort', abortHandler);
			error = new UploadError('network', 'Network error while uploading file');
			resolve(null);
		};

		xhr.ontimeout = () => {
			if (signal) signal.removeEventListener('abort', abortHandler);
			error = new UploadError('network', 'Upload timed out while waiting for progress');
			resolve(null);
		};

		xhr.onabort = () => {
			if (signal) signal.removeEventListener('abort', abortHandler);
			error = new UploadError('abort', 'Upload aborted');
			resolve(null);
		};

		xhr.send(formData);
	});

	if (error) {
		throw error;
	}

	return res;
};

// Attempt-scoped sleep that resolves early (without retrying) if the caller's
// AbortSignal fires while we're in the backoff delay - otherwise a queued
// retry could fire seconds after the caller already gave up.
const backoffDelay = (ms: number, signal?: AbortSignal): Promise<void> =>
	new Promise((resolve) => {
		if (signal?.aborted) {
			resolve();
			return;
		}
		const timer = setTimeout(() => {
			signal?.removeEventListener('abort', onAbort);
			resolve();
		}, ms);
		const onAbort = () => {
			clearTimeout(timer);
			resolve();
		};
		signal?.addEventListener('abort', onAbort, { once: true });
	});

// 1 initial attempt + 3 retries (using these delays, in order) = 4 attempts
// total. Reused (never regenerated) upload_id + File object across retries.
const RETRY_BACKOFF_MS = [1000, 3000, 8000];
const MAX_UPLOAD_ATTEMPTS = 1 + RETRY_BACKOFF_MS.length;

export const uploadFile = async (
	token: string,
	file: File,
	metadata?: object | null,
	options: UploadFileOptions = {}
) => {
	// Stable across every retry of THIS logical upload (same File), so the
	// backend can dedupe a retry that lands after a prior attempt actually
	// succeeded but whose response was lost in transit. A fresh uploadFile()
	// call (a different file/attachment) gets its own id.
	const uploadId = uuidv4();
	const metadataWithUploadId = { ...(metadata ?? {}), upload_id: uploadId };

	const query = new URLSearchParams();
	if (options.process === false) query.set('process', 'false');
	if (options.processInBackground === false) query.set('process_in_background', 'false');
	const uploadUrl = `${WEBUI_API_BASE_URL}/files/${query.toString() ? `?${query.toString()}` : ''}`;

	const buildFormData = () => {
		const data = new FormData();
		data.append('file', file);
		data.append('metadata', JSON.stringify(metadataWithUploadId));
		return data;
	};

	const attempt = async (attemptNumber: number): Promise<any> => {
		if (options.signal?.aborted) {
			throw new UploadError('abort', 'Upload aborted');
		}

		try {
			if (options.onProgress) {
				return await uploadFileWithProgress(
					token,
					uploadUrl,
					buildFormData(),
					options.onProgress,
					options.signal
				);
			}

			const res = await fetch(uploadUrl, {
				method: 'POST',
				headers: {
					Accept: 'application/json',
					authorization: `Bearer ${token}`
				},
				signal: options.signal,
				body: buildFormData()
			});

			if (!res.ok) {
				let detail: any = null;
				try {
					detail = await res.json();
				} catch {
					// ignore parse failure, fall back to status text below
				}
				const message = detail?.detail ?? detail?.message ?? res.statusText ?? `HTTP ${res.status}`;
				throw new UploadError('http', coerceToString(message), res.status);
			}

			return await res.json();
		} catch (err: any) {
			// fetch() throws a DOMException('AbortError') on abort, not our
			// UploadError - normalize it so downstream retry logic is uniform.
			if (err instanceof UploadError) throw err;
			if (err?.name === 'AbortError') {
				throw new UploadError('abort', 'Upload aborted');
			}
			throw new UploadError('network', coerceToString(err?.message ?? err));
		}
	};

	let lastError: UploadError | null = null;

	for (let i = 0; i < MAX_UPLOAD_ATTEMPTS; i++) {
		if (options.signal?.aborted) {
			throw lastError ?? new UploadError('abort', 'Upload aborted');
		}

		try {
			return await attempt(i + 1);
		} catch (err: any) {
			const uploadErr: UploadError =
				err instanceof UploadError ? err : new UploadError('network', coerceToString(err));
			lastError = uploadErr;

			if (uploadErr.kind === 'abort') {
				// Explicit cancellation - respect it immediately, never retry.
				throw uploadErr;
			}

			const isLastAttempt = i === MAX_UPLOAD_ATTEMPTS - 1;
			if (!uploadErr.retryable || isLastAttempt) {
				console.error('File upload failed (not retrying):', uploadErr);
				throw uploadErr;
			}

			const delay = RETRY_BACKOFF_MS[Math.min(i, RETRY_BACKOFF_MS.length - 1)];
			console.debug(
				`File upload attempt ${i + 1}/${MAX_UPLOAD_ATTEMPTS} failed (${uploadErr.kind}${
					uploadErr.status ? ` ${uploadErr.status}` : ''
				}), retrying in ${delay}ms...`
			);
			options.onRetry?.(i + 2, MAX_UPLOAD_ATTEMPTS, uploadErr);

			await backoffDelay(delay, options.signal);
		}
	}

	// Unreachable, but keeps TypeScript happy.
	throw lastError ?? new UploadError('network', 'Upload failed');
};

export const uploadDir = async (token: string) => {
	let error = null;

	const res = await fetch(`${WEBUI_API_BASE_URL}/files/upload/dir`, {
		method: 'POST',
		headers: {
			Accept: 'application/json',
			authorization: `Bearer ${token}`
		}
	})
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json();
		})
		.catch((err) => {
			error = err.detail;
			return null;
		});

	if (error) {
		throw error;
	}

	return res;
};

export const getFiles = async (token: string = '') => {
	let error = null;

	const res = await fetch(`${WEBUI_API_BASE_URL}/files/`, {
		method: 'GET',
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			authorization: `Bearer ${token}`
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

export const getFileById = async (token: string, id: string) => {
	let error = null;

	const res = await fetch(`${WEBUI_API_BASE_URL}/files/${id}`, {
		method: 'GET',
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			authorization: `Bearer ${token}`
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

export const getSharedFileById = async (token: string, share_id: string, id: string) => {
	let error = null;

	const res = await fetch(`${WEBUI_API_BASE_URL}/files/share/${share_id}/${id}`, {
		method: 'GET',
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			...(token && { authorization: `Bearer ${token}` })
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

export const updateFileDataContentById = async (token: string, id: string, content: string) => {
	let error = null;

	const res = await fetch(`${WEBUI_API_BASE_URL}/files/${id}/data/content/update`, {
		method: 'POST',
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			authorization: `Bearer ${token}`
		},
		body: JSON.stringify({
			content: content
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

export const ensureFilePreviewById = async (token: string, id: string) => {
	let error = null;

	const res = await fetch(`${WEBUI_API_BASE_URL}/files/${id}/preview`, {
		method: 'POST',
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			authorization: `Bearer ${token}`
		}
	})
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json();
		})
		.catch((err) => {
			error = err?.detail ?? err;
			console.error(err);
			return null;
		});

	if (error) {
		throw error;
	}

	return res;
};

export const updateFileProcessingMode = async (token: string, id: string, mode: 'text' | 'pdf') => {
	let error = null;

	const res = await fetch(`${WEBUI_API_BASE_URL}/files/${id}/process-mode`, {
		method: 'POST',
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			authorization: `Bearer ${token}`
		},
		body: JSON.stringify({ mode })
	})
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json();
		})
		.catch((err) => {
			error = err?.detail ?? err;
			console.error(err);
			return null;
		});

	if (error) {
		throw error;
	}

	return res;
};

export const getFileContentById = async (id: string) => {
	let error = null;

	const res = await fetch(`${WEBUI_API_BASE_URL}/files/${id}/content`, {
		method: 'GET',
		headers: {
			Accept: 'application/json'
		},
		credentials: 'include'
	})
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return await res.blob();
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

// Authenticated image loading.
//
// A bare <img src="/api/v1/files/{id}/content"> can only authenticate via the
// HttpOnly `token` cookie, which is frequently absent on reverse-proxy /
// separate-domain / iOS-PWA / Safari-ITP deployments — producing the broken
// "blue square with a ?" placeholder. The rest of the app authenticates with an
// `Authorization: Bearer` header, which <img> cannot send. These helpers fetch
// the bytes with the Bearer header and hand back an object URL that <img> can
// render unconditionally.
//
// The object URL is cached per (file id, width) at module scope so the same
// image shown in the composer, message history, and the lightbox is fetched
// once, and so streaming re-renders reuse it instead of refetching. In-flight
// requests are deduped so N concurrent mounts share a single network round-trip.
//
// `width` keys the cache separately from full-res: an inline thumbnail
// (`w=768`) must never collide with the full-res bytes the lightbox/download
// fetch (no `w`), and vice-versa.
//
// LRU-bounded: each entry is a live object URL pinning its decoded Blob in
// memory for the app's lifetime (the SPA document never unloads), so an
// unbounded cache grew by every image ever viewed — a leading long-session
// memory grower on iOS, which kills PWAs under memory pressure. Reads
// re-insert (Map preserves insertion order) so anything recently rendered
// stays resident; evicted URLs are revoked, and Image.svelte auto-retries
// once on <img> error, so a rare evicted-then-redecoded image self-heals
// with a refetch instead of breaking.
const FILE_OBJECT_URL_CACHE_MAX = 40;
const fileObjectUrlCache = new Map<string, string>();
const fileObjectUrlInflight = new Map<string, Promise<string>>();

const fileObjectUrlKey = (id: string, width?: number | null) => (width ? `${id}#w${width}` : id);

const touchFileObjectUrl = (key: string, objectUrl: string) => {
	fileObjectUrlCache.delete(key);
	fileObjectUrlCache.set(key, objectUrl);
	while (fileObjectUrlCache.size > FILE_OBJECT_URL_CACHE_MAX) {
		const oldestKey = fileObjectUrlCache.keys().next().value as string;
		const oldestUrl = fileObjectUrlCache.get(oldestKey);
		fileObjectUrlCache.delete(oldestKey);
		if (oldestUrl) URL.revokeObjectURL(oldestUrl);
	}
};

// Keep the uploader's already-local image bytes available to Image.svelte after
// the server returns the durable file id. Without this handoff, replacing the
// temporary blob: preview with `/api/v1/files/{id}/content` immediately triggers
// a download of a thumbnail the same browser effectively just uploaded.
//
// The durable URL remains on the message/file descriptor, so persistence,
// cross-device rendering, and provider payload construction still use the
// authenticated server file. This cache entry is local to the current SPA and
// follows the same bounded/revoked LRU lifecycle as fetched thumbnails.
export const primeFileObjectUrlById = (id: string, blob: Blob, width?: number | null): string => {
	if (!id || !blob) return '';

	const key = fileObjectUrlKey(id, width);
	const cached = fileObjectUrlCache.get(key);
	if (cached) {
		touchFileObjectUrl(key, cached);
		return cached;
	}

	const objectUrl = URL.createObjectURL(blob);
	touchFileObjectUrl(key, objectUrl);
	return objectUrl;
};

export const getFileObjectUrlById = async (
	token: string,
	id: string,
	signal?: AbortSignal,
	shareId?: string | null,
	width?: number | null
): Promise<string> => {
	const key = fileObjectUrlKey(id, width);
	const cached = fileObjectUrlCache.get(key);
	if (cached) {
		touchFileObjectUrl(key, cached);
		return cached;
	}

	if (signal?.aborted) {
		throw new DOMException('Image load aborted', 'AbortError');
	}

	// The shared network fetch intentionally runs WITHOUT any single caller's
	// abort signal: when several components show the same image, one unmounting
	// must not cancel the request the others are still waiting on. The fetch
	// always completes and populates the cache (a useful side effect).
	let shared = fileObjectUrlInflight.get(key);
	if (!shared) {
		shared = (async () => {
			// On the shared chat page the owner-authenticated route is unavailable
			// (anon / non-owner viewers); fetch the identical bytes via the
			// membership-authorized share route instead. Cached by key either way.
			const base = shareId
				? `${WEBUI_API_BASE_URL}/files/share/${shareId}/${id}/content`
				: `${WEBUI_API_BASE_URL}/files/${id}/content`;
			const url = width ? `${base}?w=${width}` : base;
			const res = await fetch(url, {
				method: 'GET',
				headers: {
					...(token && { authorization: `Bearer ${token}` })
				}
			});
			if (!res.ok) {
				throw new Error(`Failed to load image (HTTP ${res.status})`);
			}
			const objectUrl = URL.createObjectURL(await res.blob());
			touchFileObjectUrl(key, objectUrl);
			return objectUrl;
		})();

		fileObjectUrlInflight.set(key, shared);
		shared.finally(() => {
			if (fileObjectUrlInflight.get(key) === shared) {
				fileObjectUrlInflight.delete(key);
			}
		});
	}

	// Each caller can independently give up via its own signal without killing
	// the shared fetch for the others.
	if (!signal) {
		return shared;
	}
	return new Promise<string>((resolve, reject) => {
		const onAbort = () => reject(new DOMException('Image load aborted', 'AbortError'));
		signal.addEventListener('abort', onAbort, { once: true });
		shared!.then(resolve, reject).finally(() => {
			signal.removeEventListener('abort', onAbort);
		});
	});
};

// Revoke every cached variant (full-res + any thumbnail widths) for a file id.
export const revokeFileObjectUrlById = (id: string) => {
	const prefix = `${id}#w`;
	for (const key of [...fileObjectUrlCache.keys()]) {
		if (key === id || key.startsWith(prefix)) {
			const objectUrl = fileObjectUrlCache.get(key);
			if (objectUrl) URL.revokeObjectURL(objectUrl);
			fileObjectUrlCache.delete(key);
		}
	}
};

export const deleteFileById = async (token: string, id: string) => {
	let error = null;

	const res = await fetch(`${WEBUI_API_BASE_URL}/files/${id}`, {
		method: 'DELETE',
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			authorization: `Bearer ${token}`
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

export const deleteAllFiles = async (token: string) => {
	let error = null;

	const res = await fetch(`${WEBUI_API_BASE_URL}/files/all`, {
		method: 'DELETE',
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			authorization: `Bearer ${token}`
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
