import { WEBUI_API_BASE_URL } from '$lib/constants';

export type VideoQuality = string;

export type VideoJobParams = {
	fps?: number;
	quality?: VideoQuality;
	start?: number | null;
	end?: number | null;
	audio?: boolean;
};

export type VideoJob = {
	id: string;
	chat_id?: string | null;
	status: 'queued' | 'running' | 'completed' | 'failed' | 'canceled';
	stage: string;
	stage_label: string;
	source_type: 'url' | 'upload';
	source_url?: string | null;
	title?: string | null;
	params: VideoJobParams & Record<string, any>;
	progress: { percent?: number | null; detail?: string | null; label?: string | null };
	result: {
		file_id?: string;
		filename?: string;
		duration?: number;
		width?: number;
		height?: number;
		size?: number;
		frames?: number;
		estimated_tokens?: number;
		has_audio?: boolean;
		source_duration?: number;
		source?: string;
		fallback_used?: boolean;
	};
	error?: string | null;
	created_at: number;
	updated_at: number;
};

export type VideoConfig = {
	enabled: boolean;
	url_ingest_enabled: boolean;
	default_fps: number;
	default_quality: string;
	default_audio: boolean;
	max_source_size_mb: number;
	warn_duration_seconds: number;
	qualities: string[];
};

const request = async (
	token: string,
	path: string,
	{ method = 'GET', body }: { method?: string; body?: any } = {}
) => {
	let error: string | null = null;

	const res = await fetch(`${WEBUI_API_BASE_URL}/videos${path}`, {
		method,
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			authorization: `Bearer ${token}`
		},
		...(body !== undefined ? { body: JSON.stringify(body) } : {})
	})
		.then(async (r) => {
			if (!r.ok) throw await r.json();
			return r.json();
		})
		.catch((err) => {
			error = err?.detail ?? err?.message ?? `${err}`;
			return null;
		});

	if (error) throw error;
	return res;
};

export const getVideoConfig = async (token: string): Promise<VideoConfig> =>
	request(token, '/config');

export const updateVideoConfig = async (
	token: string,
	config: Partial<VideoConfig>
): Promise<VideoConfig> => request(token, '/config', { method: 'POST', body: config });

/**
 * Frames + prompt-token estimate for the composer. Advisory only — the backend
 * never blocks a send on this.
 */
export const estimateVideoTokens = async (
	token: string,
	body: { duration: number; fps?: number; audio?: boolean; has_audio?: boolean }
): Promise<{ frames: number; estimated_tokens: number }> =>
	request(token, '/estimate', { method: 'POST', body });

export const createVideoJob = async (
	token: string,
	body: {
		source_type: 'url' | 'upload';
		url?: string;
		file_id?: string;
		chat_id?: string | null;
		fps?: number;
		quality?: string;
		start?: number | null;
		end?: number | null;
		audio?: boolean;
	}
): Promise<VideoJob> => request(token, '/jobs', { method: 'POST', body });

/** Jobs still running for this user — the rehydrate call on composer mount. */
export const getActiveVideoJobs = async (token: string): Promise<{ jobs: VideoJob[] }> =>
	request(token, '/jobs/active');

/**
 * Re-read specific jobs including terminal ones. Needed when the tab was closed
 * across a job finishing, which `/jobs/active` deliberately omits.
 */
export const getVideoJobsByIds = async (
	token: string,
	ids: string[]
): Promise<{ jobs: VideoJob[] }> =>
	request(token, '/jobs/by-ids', { method: 'POST', body: { ids } });

export const getVideoJob = async (token: string, id: string): Promise<VideoJob> =>
	request(token, `/jobs/${id}`);

export const cancelVideoJob = async (token: string, id: string): Promise<VideoJob> =>
	request(token, `/jobs/${id}/cancel`, { method: 'POST' });

export const deleteVideoJob = async (token: string, id: string): Promise<{ ok: boolean }> =>
	request(token, `/jobs/${id}`, { method: 'DELETE' });
