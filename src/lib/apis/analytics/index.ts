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
	year?: number
): Promise<ModelUsage[]> => {
	let error = null;

	const params = new URLSearchParams();
	if (year) {
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
	limit: number = 100
): Promise<UserUsage[]> => {
	let error = null;

	const params = new URLSearchParams();
	if (year) {
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
