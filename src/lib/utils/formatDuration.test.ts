import { describe, expect, it } from 'vitest';

import { formatDuration, formatDurationLong } from './index';

describe('formatDuration', () => {
	it('renders sub-minute durations as seconds', () => {
		expect(formatDuration(0)).toBe('0s');
		expect(formatDuration(1)).toBe('1s');
		expect(formatDuration(45)).toBe('45s');
		expect(formatDuration(59)).toBe('59s');
	});

	it('renders minute-scale durations as "Xm Ys"', () => {
		expect(formatDuration(60)).toBe('1m 0s');
		expect(formatDuration(83)).toBe('1m 23s');
		expect(formatDuration(120)).toBe('2m 0s');
		expect(formatDuration(3599)).toBe('59m 59s');
	});

	it('drops the seconds segment at the hour scale', () => {
		expect(formatDuration(3600)).toBe('1h 0m');
		expect(formatDuration(3840)).toBe('1h 4m');
		expect(formatDuration(7320)).toBe('2h 2m');
	});

	it('floors fractional seconds', () => {
		expect(formatDuration(45.9)).toBe('45s');
		expect(formatDuration(83.4)).toBe('1m 23s');
	});

	it('clamps invalid / negative input to "0s"', () => {
		expect(formatDuration(-5)).toBe('0s');
		expect(formatDuration(NaN)).toBe('0s');
		expect(formatDuration(undefined as unknown as number)).toBe('0s');
	});
});

describe('formatDurationLong', () => {
	// Stands in for i18next under this project's en-US convention: every value is
	// an empty string and `returnEmptyString: false` makes it fall back to the
	// key, which is then interpolated. So these are the literal strings the UI
	// renders in English.
	const t = (key: string, options?: Record<string, unknown>) =>
		key.replace(/\{\{(\w+)\}\}/g, (_, name) => String(options?.[name] ?? ''));

	it('spells out sub-minute durations, with singular/plural agreement', () => {
		expect(formatDurationLong(0, t)).toBe('0 seconds');
		expect(formatDurationLong(1, t)).toBe('1 second');
		expect(formatDurationLong(45, t)).toBe('45 seconds');
		expect(formatDurationLong(59, t)).toBe('59 seconds');
	});

	it('spells out minute-scale durations and drops a zero seconds segment', () => {
		expect(formatDurationLong(60, t)).toBe('1 minute');
		expect(formatDurationLong(61, t)).toBe('1 minute 1 second');
		expect(formatDurationLong(83, t)).toBe('1 minute 23 seconds');
		expect(formatDurationLong(120, t)).toBe('2 minutes');
		expect(formatDurationLong(3599, t)).toBe('59 minutes 59 seconds');
	});

	it('spells out hour-scale durations and drops a zero minutes segment', () => {
		expect(formatDurationLong(3600, t)).toBe('1 hour');
		expect(formatDurationLong(3660, t)).toBe('1 hour 1 minute');
		expect(formatDurationLong(3840, t)).toBe('1 hour 4 minutes');
		expect(formatDurationLong(7320, t)).toBe('2 hours 2 minutes');
	});

	it('floors fractional seconds and clamps invalid / negative input', () => {
		expect(formatDurationLong(83.4, t)).toBe('1 minute 23 seconds');
		expect(formatDurationLong(-5, t)).toBe('0 seconds');
		expect(formatDurationLong(NaN, t)).toBe('0 seconds');
		expect(formatDurationLong(undefined as unknown as number, t)).toBe('0 seconds');
	});
});
