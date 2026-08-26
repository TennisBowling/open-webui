import { describe, expect, it } from 'vitest';

import {
	REASONING_EFFORT_ORDER,
	getEffectiveReasoning,
	clampEffortToEffective,
	orderReasoningEfforts
} from './reasoning';

// Helpers to build resolved-model shapes.
const withAdmin = (reasoning: any) => ({ info: { meta: { reasoning } } });
const withDiscovered = (reasoning: any) => ({ reasoning });

describe('REASONING_EFFORT_ORDER', () => {
	it('includes max as the strongest tier, last', () => {
		expect(REASONING_EFFORT_ORDER).toEqual(['none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max']);
	});

	it('orderReasoningEfforts filters + orders canonically', () => {
		expect(orderReasoningEfforts(['max', 'low', 'high', 'bogus'] as any)).toEqual([
			'low',
			'high',
			'max'
		]);
	});
});

describe('getEffectiveReasoning precedence', () => {
	it('defaults to low/medium/high when there is no config', () => {
		const eff = getEffectiveReasoning({});
		expect(eff.enabled).toBe(true);
		expect(eff.allowedEfforts).toEqual(['low', 'medium', 'high']);
		expect(eff.source).toBe('default');
	});

	it('admin-disabled hides all controls', () => {
		const eff = getEffectiveReasoning(withAdmin({ enabled: false }));
		expect(eff.enabled).toBe(false);
		expect(eff.allowedEfforts).toEqual([]);
		expect(eff.source).toBe('disabled');
	});

	it('admin manual supported_efforts wins and is ordered', () => {
		const eff = getEffectiveReasoning(
			withAdmin({ enabled: true, supported_efforts: ['max', 'low', 'high'], source: 'manual' })
		);
		expect(eff.allowedEfforts).toEqual(['low', 'high', 'max']);
		expect(eff.source).toBe('manual');
	});

	it('legacy extra_efforts adds to base', () => {
		const eff = getEffectiveReasoning(withAdmin({ extra_efforts: ['xhigh', 'minimal'] }));
		expect(eff.allowedEfforts).toEqual(['minimal', 'low', 'medium', 'high', 'xhigh']);
		expect(eff.source).toBe('legacy');
	});

	it('uses discovered supported_efforts when no admin config', () => {
		const eff = getEffectiveReasoning(
			withDiscovered({ supported_efforts: ['minimal', 'high'], default_effort: 'high', is_reasoning: true })
		);
		expect(eff.allowedEfforts).toEqual(['minimal', 'high']);
		expect(eff.defaultEffort).toBe('high');
		expect(eff.source).toBe('discovered');
	});

	it('admin manual beats discovered', () => {
		const model = {
			info: { meta: { reasoning: { supported_efforts: ['low', 'medium'] } } },
			reasoning: { supported_efforts: ['high', 'xhigh', 'max'] }
		};
		const eff = getEffectiveReasoning(model);
		expect(eff.allowedEfforts).toEqual(['low', 'medium']);
		expect(eff.source).toBe('manual');
	});

	it('legacy extra beats discovered', () => {
		const model = {
			info: { meta: { reasoning: { extra_efforts: ['minimal'] } } },
			reasoning: { supported_efforts: ['high', 'max'] }
		};
		const eff = getEffectiveReasoning(model);
		expect(eff.source).toBe('legacy');
		expect(eff.allowedEfforts).toEqual(['minimal', 'low', 'medium', 'high']);
	});

	it('discovered mandatory drops the none option', () => {
		const eff = getEffectiveReasoning(
			withDiscovered({ supported_efforts: ['none', 'low', 'high'], mandatory: true })
		);
		expect(eff.mandatory).toBe(true);
		expect(eff.allowedEfforts).toEqual(['low', 'high']);
	});

	it('admin manual keeps none even against a discovered mandatory flag', () => {
		const model = {
			info: { meta: { reasoning: { supported_efforts: ['none', 'low', 'high'] } } },
			reasoning: { mandatory: true }
		};
		const eff = getEffectiveReasoning(model);
		// Admin list is authoritative; not re-filtered by discovered mandatory.
		expect(eff.allowedEfforts).toEqual(['none', 'low', 'high']);
	});

	it('falls back to a stored discovery snapshot when no live reasoning', () => {
		const eff = getEffectiveReasoning(
			withAdmin({ discovery: { supported_efforts: ['low', 'high'] } })
		);
		expect(eff.source).toBe('discovered');
		expect(eff.allowedEfforts).toEqual(['low', 'high']);
	});

	it('reasoning model with no effort granularity falls back to base', () => {
		const eff = getEffectiveReasoning(withDiscovered({ mandatory: false, is_reasoning: true }));
		expect(eff.source).toBe('default');
		expect(eff.allowedEfforts).toEqual(['low', 'medium', 'high']);
	});

	it('drops unknown discovered efforts', () => {
		const eff = getEffectiveReasoning(withDiscovered({ supported_efforts: ['high', 'ultra', 'low'] }));
		expect(eff.allowedEfforts).toEqual(['low', 'high']);
	});
});

describe('clampEffortToEffective', () => {
	const eff = getEffectiveReasoning(
		withDiscovered({ supported_efforts: ['minimal', 'high'], default_effort: 'high' })
	);

	it('keeps a valid desired effort', () => {
		expect(clampEffortToEffective('minimal', eff)).toBe('minimal');
	});

	it('falls back to the default effort when desired is invalid', () => {
		expect(clampEffortToEffective('medium', eff)).toBe('high'); // medium not allowed → default_effort
	});

	it('falls back to medium then first when no default', () => {
		const e2 = getEffectiveReasoning(withDiscovered({ supported_efforts: ['minimal', 'high'] }));
		expect(clampEffortToEffective('xhigh', e2)).toBe('minimal'); // no medium, no default → first
		const e3 = getEffectiveReasoning({});
		expect(clampEffortToEffective('xhigh', e3)).toBe('medium'); // medium present
	});

	it('returns null when nothing is allowed', () => {
		const disabled = getEffectiveReasoning(withAdmin({ enabled: false }));
		expect(clampEffortToEffective('high', disabled)).toBe(null);
	});
});
