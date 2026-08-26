import { WEBUI_API_BASE_URL } from '$lib/constants';

/**
 * Conversation Token Stats Response
 */
export interface ChatTokenStats {
	chat_id: string;
	user_id: string;
	model_id: string | null;
	total_input_tokens: number;
	total_output_tokens: number;
	total_tokens: number;
	total_cache_read_tokens: number;
	last_input_tokens: number;
	last_output_tokens: number;
	last_cache_read_tokens: number;
	message_count: number;
	cost?: number;
	created_at: number;
	updated_at: number;
}

/**
 * Heatmap data point
 */
export interface HeatmapDataPoint {
	date: string;
	tokens: number;
	level: number; // 0-4 scale for color intensity
}

/**
 * Heatmap Response
 */
export interface HeatmapResponse {
	year: number;
	data: HeatmapDataPoint[];
	max_tokens: number;
	total_days_active: number;
}

/**
 * Model usage breakdown
 */
export interface ModelUsage {
	model_id: string;
	model_name: string | null;
	total_input_tokens: number;
	total_output_tokens: number;
	total_tokens: number;
	total_cache_read_tokens: number;
	conversation_count: number;
	message_count: number;
	percentage: number;
	cost?: number;
	unpriced_tokens?: number;
	rate_source?: string | null;
}

/**
 * Top chat by tokens
 */
export interface TopChat {
	chat_id: string;
	title: string | null;
	model_id: string | null;
	total_tokens: number;
	total_input_tokens: number;
	total_output_tokens: number;
	total_cache_read_tokens: number;
	last_cache_read_tokens: number;
	message_count: number;
	cost?: number;
}

/**
 * User Wrapped Summary
 */
export interface WrappedSummary {
	year: number;
	total_conversations: number;
	total_messages: number;
	total_input_tokens: number;
	total_output_tokens: number;
	total_tokens: number;
	total_cache_read_tokens: number;
	days_active: number;
	most_active_day: {
		date: string;
		tokens: number;
		messages: number;
		day_of_week: string;
	} | null;
	favorite_model: {
		model_id: string;
		total_tokens: number;
		percentage: number;
	} | null;
	top_chats: TopChat[];
}

/**
 * Global Wrapped Summary (Admin)
 */
export interface GlobalWrappedSummary {
	year: number;
	total_users_active: number;
	total_conversations: number;
	total_messages: number;
	total_tokens: number;
	total_cache_read_tokens: number;
	top_models: ModelUsage[];
	busiest_day: {
		date: string;
		tokens: number;
		day_of_week: string;
	} | null;
}

/**
 * Admin per-user usage stats
 */
export interface UserUsage {
	user_id: string;
	name: string | null;
	email: string | null;
	role: string | null;
	total_input_tokens: number;
	total_output_tokens: number;
	total_tokens: number;
	total_cache_read_tokens: number;
	conversation_count: number;
	message_count: number;
	days_active: number;
	avg_tokens_per_active_day: number;
	avg_tokens_per_message: number;
	cache_read_rate: number;
	last_active_at: number | null;
	cost?: number;
	unpriced_tokens?: number;
}

/**
 * Total spend KPI (Admin only)
 */
export interface TotalSpend {
	total_cost: number;
	embedded_cost: number;
	rate_card_cost: number;
	total_tokens: number;
	unpriced_tokens: number;
	priced_model_count: number;
	unpriced_model_count: number;
	start_ts: number | null;
	end_ts: number | null;
}

/**
 * Single day of spend for the cost trend
 */
export interface DailySpendPoint {
	date: string;
	cost: number;
	embedded_cost: number;
	rate_card_cost: number;
}

/**
 * Pricing catalog row (synced from OpenRouter)
 */
export interface PricingCatalogRow {
	slug: string;
	model_name: string | null;
	prompt_rate: number | null;
	completion_rate: number | null;
	cache_read_rate: number | null;
	web_search_rate: number | null;
	is_free: boolean;
	synced_at: number | null;
}

/**
 * Pricing override row (admin-managed)
 */
export interface PricingOverrideRow {
	model_id: string;
	mode: string;
	alias_slug: string | null;
	prompt_rate: number | null;
	completion_rate: number | null;
	cache_read_rate: number | null;
	note: string | null;
	updated_by: string | null;
	updated_at: number | null;
}

/**
 * Per-model pricing resolution status (admin mapping cockpit)
 */
export interface ResolvedModelStatus {
	model_id: string;
	total_tokens: number;
	rate_card_tokens: number;
	priced: boolean;
	rate_source: string | null;
	effective_rate: {
		prompt: number | null;
		completion: number | null;
		cache_read: number | null;
	} | null;
}

/**
 * Admin subagent analytics
 */
export interface SubagentAnalytics {
	year: number;
	total_subagent_chats: number;
	parent_chat_count: number;
	request_count: number;
	total_input_tokens: number;
	total_output_tokens: number;
	total_tokens: number;
	total_cache_read_tokens: number;
	token_share_percent: number;
	avg_tokens_per_subagent: number;
	avg_requests_per_subagent: number;
	avg_subagents_per_parent: number;
	status_counts: Record<string, number>;
	top_parent_chats: Array<Record<string, any>>;
	top_subagents: Array<Record<string, any>>;
	top_users: Array<Record<string, any>>;
	top_models: ModelUsage[];
}

/**
 * Cache Intelligence types
 */
export interface CacheBucket {
	key: string;
	label: string;
	lower_seconds: number;
}

export interface CacheCurvePoint {
	bucket: string;
	requests: number;
	prompt_tokens: number;
	cache_read_tokens: number;
	hit_ratio: number; // 0..1
}

export interface CacheGroupStats {
	key: string;
	label: string;
	kind: 'gateway' | 'vendor' | 'model';
	prompt_tokens: number;
	cache_read_tokens: number;
	total_tokens: number;
	request_count: number;
	hit_rate: number; // percent
	savings_usd: number;
	unpriced_cache_tokens: number;
	est_ttl_seconds: number | null;
	est_ttl_capped: boolean;
	curve: CacheCurvePoint[]; // conversational
	curve_agentic: CacheCurvePoint[];
	conversational_requests: number;
	agentic_requests: number;
	conversational_hit_rate: number;
	agentic_hit_rate: number;
}

export interface CacheUserStats {
	user_id: string;
	name: string | null;
	email: string | null;
	prompt_tokens: number;
	cache_read_tokens: number;
	hit_rate: number;
	savings_usd: number;
}

export interface CacheAnalytics {
	group_by: 'gateway' | 'vendor' | 'model';
	start_ts: number;
	end_ts: number;
	buckets: CacheBucket[];
	prompt_tokens: number;
	cache_read_tokens: number;
	total_tokens: number;
	request_count: number;
	eligible_request_count: number;
	conversational_request_count: number;
	agentic_request_count: number;
	conversational_cache_read_tokens: number;
	agentic_cache_read_tokens: number;
	conversational_hit_rate: number;
	agentic_hit_rate: number;
	hit_rate: number;
	savings_usd: number;
	unpriced_cache_tokens: number;
	groups: CacheGroupStats[];
	users: CacheUserStats[];
}

/**
 * Get token usage stats for a specific chat
 */
export const getChatTokenStats = async (
	token: string,
	chatId: string
): Promise<ChatTokenStats | null> => {
	let error = null;

	const res = await fetch(`${WEBUI_API_BASE_URL}/analytics/chat/${chatId}`, {
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
		.catch((err) => {
			error = err;
			console.error('Error fetching chat token stats:', err);
			return null;
		});

	if (error) {
		console.error(error);
		return null;
	}

	return res;
};

/**
 * Get user's wrapped summary
 */
export const getUserWrapped = async (
	token: string,
	year?: number
): Promise<WrappedSummary | null> => {
	let error = null;

	const params = new URLSearchParams();
	if (year) {
		params.append('year', year.toString());
	}

	const res = await fetch(`${WEBUI_API_BASE_URL}/analytics/user/wrapped?${params.toString()}`, {
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
		.catch((err) => {
			error = err;
			console.error('Error fetching user wrapped:', err);
			return null;
		});

	if (error) {
		console.error(error);
		return null;
	}

	return res;
};

/**
 * Get user's activity heatmap data
 */
export const getUserHeatmap = async (
	token: string,
	year?: number
): Promise<HeatmapResponse | null> => {
	let error = null;

	const params = new URLSearchParams();
	if (year) {
		params.append('year', year.toString());
	}

	const res = await fetch(`${WEBUI_API_BASE_URL}/analytics/user/heatmap?${params.toString()}`, {
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
		.catch((err) => {
			error = err;
			console.error('Error fetching user heatmap:', err);
			return null;
		});

	if (error) {
		console.error(error);
		return null;
	}

	return res;
};

/**
 * Get user's per-model usage breakdown
 */
export const getUserModelUsage = async (token: string, year?: number): Promise<ModelUsage[]> => {
	let error = null;

	const params = new URLSearchParams();
	if (year) {
		params.append('year', year.toString());
	}

	const res = await fetch(`${WEBUI_API_BASE_URL}/analytics/user/models?${params.toString()}`, {
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
		.catch((err) => {
			error = err;
			console.error('Error fetching user model usage:', err);
			return [];
		});

	if (error) {
		console.error(error);
		return [];
	}

	return res || [];
};

/**
 * Get user's top chats by token count
 */
export const getUserTopChats = async (
	token: string,
	year?: number,
	limit: number = 10
): Promise<TopChat[]> => {
	let error = null;

	const params = new URLSearchParams();
	if (year) {
		params.append('year', year.toString());
	}
	params.append('limit', limit.toString());

	const res = await fetch(`${WEBUI_API_BASE_URL}/analytics/user/top-chats?${params.toString()}`, {
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
		.catch((err) => {
			error = err;
			console.error('Error fetching user top chats:', err);
			return [];
		});

	if (error) {
		console.error(error);
		return [];
	}

	return res || [];
};

/**
 * Get global/site-wide wrapped summary (Admin only)
 */
export const getGlobalWrapped = async (
	token: string,
	year?: number
): Promise<GlobalWrappedSummary | null> => {
	let error = null;

	const params = new URLSearchParams();
	if (year) {
		params.append('year', year.toString());
	}

	const res = await fetch(`${WEBUI_API_BASE_URL}/analytics/global/wrapped?${params.toString()}`, {
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
		.catch((err) => {
			error = err;
			console.error('Error fetching global wrapped:', err);
			return null;
		});

	if (error) {
		console.error(error);
		return null;
	}

	return res;
};

/**
 * Get global model usage (Admin only)
 */
export const getGlobalModelUsage = async (
	token: string,
	limit: number = 20,
	year?: number,
	window?: { start_ts?: number; end_ts?: number }
): Promise<ModelUsage[]> => {
	let error = null;

	const params = new URLSearchParams();
	if (window?.start_ts != null && window?.end_ts != null) {
		params.append('start_ts', Math.floor(window.start_ts).toString());
		params.append('end_ts', Math.floor(window.end_ts).toString());
	} else if (year) {
		params.append('year', year.toString());
	}
	params.append('limit', limit.toString());

	const res = await fetch(`${WEBUI_API_BASE_URL}/analytics/global/models?${params.toString()}`, {
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
		.catch((err) => {
			error = err;
			console.error('Error fetching global model usage:', err);
			return [];
		});

	if (error) {
		console.error(error);
		return [];
	}

	return res || [];
};

/**
 * Get per-user usage leaderboard (Admin only)
 */
export const getGlobalUserUsage = async (
	token: string,
	year?: number,
	limit: number = 100,
	window?: { start_ts?: number; end_ts?: number }
): Promise<UserUsage[]> => {
	let error = null;

	const params = new URLSearchParams();
	if (window?.start_ts != null && window?.end_ts != null) {
		params.append('start_ts', Math.floor(window.start_ts).toString());
		params.append('end_ts', Math.floor(window.end_ts).toString());
	} else if (year) {
		params.append('year', year.toString());
	}
	params.append('limit', limit.toString());

	const res = await fetch(`${WEBUI_API_BASE_URL}/analytics/global/users?${params.toString()}`, {
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
		.catch((err) => {
			error = err;
			console.error('Error fetching global user usage:', err);
			return [];
		});

	if (error) {
		console.error(error);
		return [];
	}

	return res || [];
};

/**
 * Get subagent usage analytics (Admin only)
 */
export const getGlobalSubagentUsage = async (
	token: string,
	year?: number
): Promise<SubagentAnalytics | null> => {
	let error = null;

	const params = new URLSearchParams();
	if (year) {
		params.append('year', year.toString());
	}

	const res = await fetch(`${WEBUI_API_BASE_URL}/analytics/global/subagents?${params.toString()}`, {
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
		.catch((err) => {
			error = err;
			console.error('Error fetching subagent analytics:', err);
			return null;
		});

	if (error) {
		console.error(error);
		return null;
	}

	return res;
};

/**
 * Get global activity heatmap (Admin only)
 */
export const getGlobalHeatmap = async (
	token: string,
	year?: number
): Promise<HeatmapResponse | null> => {
	let error = null;

	const params = new URLSearchParams();
	if (year) {
		params.append('year', year.toString());
	}

	const res = await fetch(`${WEBUI_API_BASE_URL}/analytics/global/heatmap?${params.toString()}`, {
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
		.catch((err) => {
			error = err;
			console.error('Error fetching global heatmap:', err);
			return null;
		});

	if (error) {
		console.error(error);
		return null;
	}

	return res;
};

/**
 * Format token count for display (e.g., 1234 -> "1.2K")
 */
export const formatTokenCount = (count: number): string => {
	if (count < 1000) {
		return count.toString();
	} else if (count < 1000000) {
		return (count / 1000).toFixed(1) + 'K';
	} else {
		return (count / 1000000).toFixed(1) + 'M';
	}
};

/**
 * Format a USD cost value. Returns '—' for null/undefined.
 */
export const formatCost = (cost: number | null | undefined): string => {
	if (cost == null) return '—';
	if (cost === 0) return '$0.00';
	if (cost > 0 && cost < 0.01) return '<$0.01';
	const digits = cost < 100 ? 2 : 2;
	return (
		'$' +
		cost.toLocaleString(undefined, {
			minimumFractionDigits: digits,
			maximumFractionDigits: digits
		})
	);
};

const _windowParams = (year?: number, window?: { start_ts?: number; end_ts?: number }) => {
	const params = new URLSearchParams();
	if (window?.start_ts != null && window?.end_ts != null) {
		params.append('start_ts', Math.floor(window.start_ts).toString());
		params.append('end_ts', Math.floor(window.end_ts).toString());
	} else if (year) {
		params.append('year', year.toString());
	}
	return params;
};

const _getJSON = async (token: string, path: string, fallback: any) => {
	let error = null;
	const res = await fetch(`${WEBUI_API_BASE_URL}${path}`, {
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
		.catch((err) => {
			error = err;
			console.error(`Error fetching ${path}:`, err);
			return fallback;
		});
	if (error) return fallback;
	return res ?? fallback;
};

/**
 * Get site-wide spend KPI (Admin only)
 */
export const getGlobalSpend = async (
	token: string,
	year?: number,
	window?: { start_ts?: number; end_ts?: number }
): Promise<TotalSpend | null> => {
	const params = _windowParams(year, window);
	return await _getJSON(token, `/analytics/global/spend?${params.toString()}`, null);
};

/**
 * Get daily spend trend (Admin only)
 */
export const getGlobalSpendTrend = async (
	token: string,
	year?: number,
	window?: { start_ts?: number; end_ts?: number }
): Promise<DailySpendPoint[]> => {
	const params = _windowParams(year, window);
	return await _getJSON(token, `/analytics/global/spend-trend?${params.toString()}`, []);
};

/**
 * Get most expensive chats (Admin only)
 */
export const getTopChatsByCost = async (
	token: string,
	limit: number = 10,
	year?: number,
	window?: { start_ts?: number; end_ts?: number }
): Promise<TopChat[]> => {
	const params = _windowParams(year, window);
	params.append('limit', limit.toString());
	return await _getJSON(token, `/analytics/global/top-chats-by-cost?${params.toString()}`, []);
};

/**
 * Get site-wide cache intelligence (Admin only).
 * groupBy ∈ 'gateway' | 'vendor' | 'model'.
 */
export const getGlobalCacheAnalytics = async (
	token: string,
	groupBy: 'gateway' | 'vendor' | 'model' = 'gateway',
	year?: number,
	window?: { start_ts?: number; end_ts?: number }
): Promise<CacheAnalytics | null> => {
	const params = _windowParams(year, window);
	params.append('group_by', groupBy);
	return await _getJSON(token, `/analytics/global/cache?${params.toString()}`, null);
};

/**
 * Get the synced OpenRouter pricing catalog (Admin only)
 */
export const getPricingCatalog = async (
	token: string
): Promise<{ catalog: PricingCatalogRow[]; synced_at: number | null }> => {
	return await _getJSON(token, `/analytics/pricing/catalog`, { catalog: [], synced_at: null });
};

/**
 * Get overrides + per-model resolution status (Admin only)
 */
export const getPricingOverrides = async (
	token: string
): Promise<{ overrides: PricingOverrideRow[]; resolution: ResolvedModelStatus[] }> => {
	return await _getJSON(token, `/analytics/pricing/overrides`, { overrides: [], resolution: [] });
};

/**
 * Create/update a pricing override (Admin only)
 */
export const upsertPricingOverride = async (
	token: string,
	override: {
		model_id: string;
		mode: string;
		alias_slug?: string | null;
		prompt_rate?: number | null;
		completion_rate?: number | null;
		cache_read_rate?: number | null;
		note?: string | null;
	}
): Promise<any> => {
	let error = null;
	const res = await fetch(`${WEBUI_API_BASE_URL}/analytics/pricing/overrides`, {
		method: 'POST',
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			...(token && { authorization: `Bearer ${token}` })
		},
		body: JSON.stringify(override)
	})
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json();
		})
		.catch((err) => {
			error = err;
			return null;
		});
	if (error) throw error;
	return res;
};

/**
 * Delete a pricing override (Admin only)
 */
export const deletePricingOverride = async (token: string, modelId: string): Promise<any> => {
	let error = null;
	const res = await fetch(
		`${WEBUI_API_BASE_URL}/analytics/pricing/overrides/${encodeURIComponent(modelId)}`,
		{
			method: 'DELETE',
			headers: {
				Accept: 'application/json',
				...(token && { authorization: `Bearer ${token}` })
			}
		}
	)
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json();
		})
		.catch((err) => {
			error = err;
			return null;
		});
	if (error) throw error;
	return res;
};

/**
 * Trigger an immediate OpenRouter catalog sync (Admin only)
 */
export const syncPricing = async (token: string): Promise<any> => {
	let error = null;
	const res = await fetch(`${WEBUI_API_BASE_URL}/analytics/pricing/sync`, {
		method: 'POST',
		headers: {
			Accept: 'application/json',
			...(token && { authorization: `Bearer ${token}` })
		}
	})
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json();
		})
		.catch((err) => {
			error = err;
			return null;
		});
	if (error) throw error;
	return res;
};
