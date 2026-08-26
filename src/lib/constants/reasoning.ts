import type { ExtraReasoningEffort, ReasoningEffort } from '$lib/apis';

// Canonical ascending-by-strength order. Keep in lockstep with the backend
// `KNOWN_EFFORTS` in `open_webui/utils/openrouter_reasoning.py`. `max` is the
// strongest tier and only appears for models that explicitly advertise it.
export const REASONING_EFFORT_ORDER: ReasoningEffort[] = [
	'none',
	'minimal',
	'low',
	'medium',
	'high',
	'xhigh',
	'max'
];

export const BASE_REASONING_EFFORTS: ReasoningEffort[] = ['low', 'medium', 'high'];
export const EXTRA_REASONING_EFFORTS: ExtraReasoningEffort[] = ['none', 'minimal', 'xhigh', 'max'];

export const orderReasoningEfforts = (efforts: ReasoningEffort[] | (ReasoningEffort | string)[]) =>
	REASONING_EFFORT_ORDER.filter((effort) => efforts.includes(effort));

const isKnownEffort = (e: unknown): e is ReasoningEffort =>
	typeof e === 'string' && (REASONING_EFFORT_ORDER as string[]).includes(e);

const filterAndOrder = (list: unknown): ReasoningEffort[] => {
	if (!Array.isArray(list)) return [];
	return orderReasoningEfforts(Array.from(new Set(list.filter(isKnownEffort))) as ReasoningEffort[]);
};

/**
 * The reasoning-effort options that should actually be offered for a model,
 * resolved from (in precedence order):
 *
 *   1. Admin explicitly disabled reasoning        → no controls
 *   2. Admin manual override (`supported_efforts`) → that exact set
 *   3. Legacy admin `extra_efforts`               → base(low/med/high) ∪ extras
 *   4. Discovered `supported_efforts` (OpenRouter) → that set
 *   5. Default                                     → base(low/med/high)
 *
 * Admin-authored config (2/3/1) always beats live discovery (4). The discovered
 * descriptor lives on `model.reasoning` (injected by the backend for OpenRouter
 * models) with a fallback to a stored `meta.reasoning.discovery` snapshot.
 */
export type EffectiveReasoningSource = 'disabled' | 'manual' | 'legacy' | 'discovered' | 'default';

export interface EffectiveReasoning {
	enabled: boolean;
	allowedEfforts: string[];
	defaultEffort: string | null;
	mandatory: boolean;
	source: EffectiveReasoningSource;
}

const build = (
	list: unknown,
	defaultEffort: unknown,
	mandatory: unknown,
	source: EffectiveReasoningSource
): EffectiveReasoning => {
	let allowed = filterAndOrder(list) as string[];
	// A model that mandates reasoning cannot be turned off — drop the "none"
	// (reasoning-off) option so the selector can't send it.
	if (mandatory) {
		allowed = allowed.filter((e) => e !== 'none');
	}
	const def =
		typeof defaultEffort === 'string' && allowed.includes(defaultEffort) ? defaultEffort : null;
	return {
		enabled: allowed.length > 0,
		allowedEfforts: allowed,
		defaultEffort: def,
		mandatory: !!mandatory,
		source
	};
};

export const getEffectiveReasoning = (model: any): EffectiveReasoning => {
	const adminR = model?.info?.meta?.reasoning;
	// Discovered descriptor: live (top-level, base models) or a stored snapshot.
	const disc = model?.reasoning ?? adminR?.discovery;

	// 1. Admin explicitly disabled reasoning for this model.
	if (adminR?.enabled === false) {
		return {
			enabled: false,
			allowedEfforts: [],
			defaultEffort: null,
			mandatory: false,
			source: 'disabled'
		};
	}

	// 2. Admin manual override — an explicit supported set wins over everything.
	//    The admin's list is authoritative and is NOT re-filtered by the
	//    provider's `mandatory` flag (that would silently drop an effort the
	//    admin deliberately included).
	if (Array.isArray(adminR?.supported_efforts) && adminR.supported_efforts.length > 0) {
		return build(
			adminR.supported_efforts,
			adminR.default_effort ?? disc?.default_effort,
			adminR.mandatory === true,
			'manual'
		);
	}

	// 3. Legacy admin `extra_efforts` (base low/med/high are implied).
	const legacyExtra = (adminR?.extra_efforts ?? []).filter((e: unknown) => isKnownEffort(e));
	if (legacyExtra.length > 0) {
		return build([...BASE_REASONING_EFFORTS, ...legacyExtra], null, false, 'legacy');
	}

	// 4. Discovered supported efforts (OpenRouter auto-discovery).
	if (Array.isArray(disc?.supported_efforts) && disc.supported_efforts.length > 0) {
		return build(disc.supported_efforts, disc.default_effort, disc.mandatory, 'discovered');
	}

	// 5. Default: the historical behaviour — low/medium/high, enabled.
	return build(BASE_REASONING_EFFORTS, null, false, 'default');
};

export const getAllowedEffortsForModel = (model: any): string[] =>
	getEffectiveReasoning(model).allowedEfforts;

/**
 * Clamp a desired effort to a model's allowed set. Prefers: the desired value if
 * valid → the model's default effort → `medium` → the first allowed. Returns
 * `null` when nothing is allowed (reasoning off).
 */
export const clampEffortToEffective = (
	effort: string | null | undefined,
	effective: EffectiveReasoning
): string | null => {
	const allowed = effective.allowedEfforts;
	if (!allowed || allowed.length === 0) return null;
	if (effort && allowed.includes(effort)) return effort;
	if (effective.defaultEffort && allowed.includes(effective.defaultEffort)) {
		return effective.defaultEffort;
	}
	return allowed.includes('medium') ? 'medium' : allowed[0];
};
