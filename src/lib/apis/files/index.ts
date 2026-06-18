import { WEBUI_API_BASE_URL } from '$lib/constants';

export type UploadFileOptions = {
	onProgress?: (progress: number) => void;
	signal?: AbortSignal;
	process?: boolean;
	processInBackground?: boolean;
};

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

const uploadFileWithProgress = async (
	token: string,
	url: string,
	formData: FormData,
	onProgress: (progress: number) => void,
	signal?: AbortSignal
) => {
	let error: any = null;

	const res = await new Promise<any | null>((resolve) => {
		const xhr = new XMLHttpRequest();
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

		if (signal) {
			if (signal.aborted) {
				error = new DOMException('Upload aborted', 'AbortError');
				resolve(null);
				return;
			}
			signal.addEventListener('abort', abortHandler);
		}

		if (xhr.upload) {
			xhr.upload.onprogress = (event) => {
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

				error = 'Upload succeeded but the server returned an invalid response';
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
			error = status ? `HTTP ${status}: ${messageText}` : messageText;
			resolve(null);
		};

		xhr.onerror = () => {
			if (signal) signal.removeEventListener('abort', abortHandler);
			error = 'Network error while uploading file';
			resolve(null);
		};

		xhr.onabort = () => {
			if (signal) signal.removeEventListener('abort', abortHandler);
			error = new DOMException('Upload aborted', 'AbortError');
			resolve(null);
		};

		xhr.send(formData);
	});

	if (error) {
		throw error;
	}

	return res;
};

export const uploadFile = async (
	token: string,
	file: File,
	metadata?: object | null,
	options: UploadFileOptions = {}
) => {
	const data = new FormData();
	data.append('file', file);
	if (metadata) {
		data.append('metadata', JSON.stringify(metadata));
	}

	let error = null;

	const query = new URLSearchParams();
	if (options.process === false) query.set('process', 'false');
	if (options.processInBackground === false) query.set('process_in_background', 'false');
	const uploadUrl = `${WEBUI_API_BASE_URL}/files/${query.toString() ? `?${query.toString()}` : ''}`;

	const res = options.onProgress
		? await uploadFileWithProgress(
				token,
				uploadUrl,
				data,
				options.onProgress,
				options.signal
			).catch((err) => {
				error = err;
				console.error(err);
				return null;
			})
		: await fetch(uploadUrl, {
				method: 'POST',
				headers: {
					Accept: 'application/json',
					authorization: `Bearer ${token}`
				},
				signal: options.signal,
				body: data
			})
				.then(async (res) => {
					if (!res.ok) throw await res.json();
					return res.json();
				})
				.catch((err) => {
					error = err?.detail ?? err?.message ?? err;
					console.error(err);
					return null;
				});

	if (error) {
		throw error;
	}

	return res;
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

export const updateFileProcessingMode = async (
	token: string,
	id: string,
	mode: 'text' | 'pdf'
) => {
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
// The object URL is cached per file id at module scope so the same image shown
// in the composer, message history, and the lightbox is fetched once, and so
// streaming re-renders reuse it instead of refetching. In-flight requests are
// deduped so N concurrent mounts share a single network round-trip.
const fileObjectUrlCache = new Map<string, string>();
const fileObjectUrlInflight = new Map<string, Promise<string>>();

export const getFileObjectUrlById = async (
	token: string,
	id: string,
	signal?: AbortSignal
): Promise<string> => {
	const cached = fileObjectUrlCache.get(id);
	if (cached) return cached;

	if (signal?.aborted) {
		throw new DOMException('Image load aborted', 'AbortError');
	}

	// The shared network fetch intentionally runs WITHOUT any single caller's
	// abort signal: when several components show the same image, one unmounting
	// must not cancel the request the others are still waiting on. The fetch
	// always completes and populates the cache (a useful side effect).
	let shared = fileObjectUrlInflight.get(id);
	if (!shared) {
		shared = (async () => {
			const res = await fetch(`${WEBUI_API_BASE_URL}/files/${id}/content`, {
				method: 'GET',
				headers: {
					...(token && { authorization: `Bearer ${token}` })
				}
			});
			if (!res.ok) {
				throw new Error(`Failed to load image (HTTP ${res.status})`);
			}
			const objectUrl = URL.createObjectURL(await res.blob());
			fileObjectUrlCache.set(id, objectUrl);
			return objectUrl;
		})();

		fileObjectUrlInflight.set(id, shared);
		shared.finally(() => {
			if (fileObjectUrlInflight.get(id) === shared) {
				fileObjectUrlInflight.delete(id);
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

export const revokeFileObjectUrlById = (id: string) => {
	const objectUrl = fileObjectUrlCache.get(id);
	if (objectUrl) {
		URL.revokeObjectURL(objectUrl);
		fileObjectUrlCache.delete(id);
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
