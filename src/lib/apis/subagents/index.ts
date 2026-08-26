import { SUBAGENTS_API_BASE_URL } from '$lib/constants';

export type SubagentsConfig = {
	ENABLE_SUBAGENTS: boolean;
	SUBAGENT_DEFAULT_MODEL: string;
	// Empty disables automatic context fallback. When set, a subagent whose
	// active model exhausts its input context retries that turn on this model
	// and keeps using it for later turns.
	SUBAGENT_CONTEXT_FALLBACK_MODEL: string;
	SUBAGENT_SYSTEM_PROMPT: string;
	SUBAGENT_SYSTEM_PROMPT_APPEND: string;
	SUBAGENT_PARENT_PROMPT: string;
	// Empty string = let the model use its own default. Otherwise one of
	// minimal / low / medium / high / xhigh (or any provider-specific value).
	SUBAGENT_DEFAULT_REASONING_EFFORT: string;
	// Empty string = don't send a `service_tier` field (provider uses its own
	// default). Otherwise: any string the chosen provider accepts (e.g.
	// `default`, `flex`, `priority`). Per-chat `chat.params.subagentServiceTier`
	// overrides this when set.
	SUBAGENT_DEFAULT_SERVICE_TIER: string;
	// Global admin gate. When false, subagents only get built-in web_search/fetch
	// regardless of per-chat tool selections.
	SUBAGENT_ALLOW_EXTERNAL_TOOLS: boolean;
	// Appended only when subagents receive selected external tools.
	SUBAGENT_EXTERNAL_TOOLS_PROMPT: string;
};

export type SubagentsConfigUpdate = Partial<SubagentsConfig>;

export const getSubagentsConfig = async (token: string): Promise<SubagentsConfig> => {
	let error: any = null;

	const res = await fetch(`${SUBAGENTS_API_BASE_URL}/config`, {
		method: 'GET',
		headers: {
			'Content-Type': 'application/json',
			Authorization: `Bearer ${token}`
		}
	})
		.then(async (r) => {
			if (!r.ok) throw await r.json();
			return r.json();
		})
		.catch((err) => {
			console.error(err);
			error = err?.detail ?? err;
			return null;
		});

	if (error) {
		throw error;
	}

	return res as SubagentsConfig;
};

export const updateSubagentsConfig = async (
	token: string,
	payload: SubagentsConfigUpdate
): Promise<SubagentsConfig> => {
	let error: any = null;

	const res = await fetch(`${SUBAGENTS_API_BASE_URL}/config/update`, {
		method: 'POST',
		headers: {
			'Content-Type': 'application/json',
			Authorization: `Bearer ${token}`
		},
		body: JSON.stringify({ ...payload })
	})
		.then(async (r) => {
			if (!r.ok) throw await r.json();
			return r.json();
		})
		.catch((err) => {
			console.error(err);
			error = err?.detail ?? err;
			return null;
		});

	if (error) {
		throw error;
	}

	return res as SubagentsConfig;
};

export type SubagentRerunScope = 'this_turn' | 'from_launch';

export type SubagentRerunPayload = {
	parent_chat_id: string;
	parent_message_id: string;
	session_id: string;
	entry_key: string;
	scope: SubagentRerunScope;
};

export type SubagentRerunStopPayload = {
	parent_chat_id: string;
	parent_message_id: string;
	entry_key: string;
	subagent_id?: string;
};

export type SubagentAdoptResponse = {
	status: boolean;
	parent_chat_id: string;
	parent_message_id: string;
	entry_key: string;
	run: Record<string, any>;
};

export type SubagentRewindAdoptPayload = {
	parent_chat_id: string;
	source_parent_message_id: string;
	branch_message_id: string;
	entry_keys: string[];
	operation_id?: string;
};

export type SubagentRewindAdoptResponse = {
	status: boolean;
	parent_chat_id: string;
	source_parent_message_id: string;
	parent_message_id: string;
	branch_message: Record<string, any>;
	adoptions: SubagentAdoptResponse[];
	entry_keys: string[];
	idempotent: boolean;
	updated_at?: number;
};

export type SubagentRewindRerunPayload = SubagentRewindAdoptPayload;
export type SubagentRewindRerunResponse = Omit<SubagentRewindAdoptResponse, 'adoptions'>;

/**
 * Create one rewind sibling and install every selected repaired child answer
 * in a single guarded backend transaction. A failed preflight/commit creates
 * no branch; a successful response is already durable before parent generation
 * is resumed.
 */
export const rewindAdoptSubagentResults = async (
	token: string,
	payload: SubagentRewindAdoptPayload
): Promise<SubagentRewindAdoptResponse> => {
	let error: any = null;

	const res = await fetch(`${SUBAGENTS_API_BASE_URL}/adopt/rewind`, {
		method: 'POST',
		headers: {
			'Content-Type': 'application/json',
			Authorization: `Bearer ${token}`
		},
		body: JSON.stringify({ ...payload })
	})
		.then(async (r) => {
			if (!r.ok) throw await r.json();
			return r.json();
		})
		.catch((err) => {
			console.error(err);
			error = err?.detail ?? err;
			return null;
		});

	if (error) {
		throw error;
	}

	return res as SubagentRewindAdoptResponse;
};

/**
 * Create and select the rewind checkpoint for one or more detached reruns in a
 * guarded backend transaction. The returned sibling is already durable; the
 * caller may then launch each rerun against its new parent message id.
 */
export const rewindSubagentsForRerun = async (
	token: string,
	payload: SubagentRewindRerunPayload
): Promise<SubagentRewindRerunResponse> => {
	let error: any = null;

	const res = await fetch(`${SUBAGENTS_API_BASE_URL}/rerun/rewind`, {
		method: 'POST',
		headers: {
			'Content-Type': 'application/json',
			Authorization: `Bearer ${token}`
		},
		body: JSON.stringify({ ...payload })
	})
		.then(async (r) => {
			if (!r.ok) throw await r.json();
			return r.json();
		})
		.catch((err) => {
			console.error(err);
			error = err?.detail ?? err;
			return null;
		});

	if (error) {
		throw error;
	}

	return res as SubagentRewindRerunResponse;
};

/**
 * Trigger a user-initiated rerun of a subagent turn. The backend kicks the
 * actual rerun off as a background task; live progress streams back through
 * the same `chat:subagent:update` socket events the original launch used,
 * so the SubagentBlock refreshes in place — no need to poll or block here.
 */
export const rerunSubagent = async (
	token: string,
	payload: SubagentRerunPayload
): Promise<{ status: boolean; task_id?: string; rerun_id?: string }> => {
	let error: any = null;

	const res = await fetch(`${SUBAGENTS_API_BASE_URL}/rerun`, {
		method: 'POST',
		headers: {
			'Content-Type': 'application/json',
			Authorization: `Bearer ${token}`
		},
		body: JSON.stringify({ ...payload })
	})
		.then(async (r) => {
			if (!r.ok) throw await r.json();
			return r.json();
		})
		.catch((err) => {
			console.error(err);
			// The backend surfaces a structured 409 detail for a blocked rerun:
			// `{ message, code }` (code === 'subagent_parent_moved_on' when the
			// parent already continued — the caller offers the rewind & redo flow).
			// Unwrapping `.detail` preserves that object so the caller can read
			// `err.code` / `err.message`; other errors degrade to a string detail.
			error = err?.detail ?? err;
			return null;
		});

	if (error) {
		throw error;
	}

	return res as { status: boolean; task_id?: string; rerun_id?: string };
};

export const stopSubagentRerun = async (
	token: string,
	payload: SubagentRerunStopPayload
): Promise<{ status: boolean; task_ids: string[]; stopped: number }> => {
	let error: any = null;

	const res = await fetch(`${SUBAGENTS_API_BASE_URL}/rerun/stop`, {
		method: 'POST',
		headers: {
			'Content-Type': 'application/json',
			Authorization: `Bearer ${token}`
		},
		body: JSON.stringify({ ...payload })
	})
		.then(async (r) => {
			if (!r.ok) throw await r.json();
			return r.json();
		})
		.catch((err) => {
			console.error(err);
			error = err?.detail ?? err;
			return null;
		});

	if (error) {
		throw error;
	}

	return res as { status: boolean; task_ids: string[]; stopped: number };
};
