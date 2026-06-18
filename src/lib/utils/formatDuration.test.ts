import { describe, expect, it } from 'vitest';

import { formatDuration } from './index';

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
