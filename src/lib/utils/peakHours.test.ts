import { describe, expect, it } from 'vitest';

import {
	DEFAULT_PEAK_NOTE,
	PEAK_SOON_WINDOW_MINUTES,
	formatBlock,
	formatBlocks,
	getPeakHoursConfig,
	getPeakStatus,
	isValidBlock,
	isWithinBlock,
	minutesToHHMM,
	minutesUntilEnd,
	minutesUntilStart,
	parseHHMM,
	utcMinutesOfDay
} from './peakHours';

// Build a Date at a specific UTC wall-clock time.
const atUtc = (h: number, m: number) => new Date(Date.UTC(2024, 0, 1, h, m, 0));

describe('parseHHMM', () => {
	it('parses valid times to minutes-from-midnight', () => {
		expect(parseHHMM('00:00')).toBe(0);
		expect(parseHHMM('01:00')).toBe(60);
		expect(parseHHMM('09:30')).toBe(570);
		expect(parseHHMM('23:59')).toBe(1439);
	});

	it('tolerates a single-digit hour and trailing seconds', () => {
		expect(parseHHMM('9:05')).toBe(545);
		expect(parseHHMM(' 06:00 ')).toBe(360);
		expect(parseHHMM('06:00:00')).toBe(360);
	});

	it('rejects out-of-range and malformed input', () => {
		expect(parseHHMM('24:00')).toBeNull();
		expect(parseHHMM('12:60')).toBeNull();
		expect(parseHHMM('-1:00')).toBeNull();
		expect(parseHHMM('12')).toBeNull();
		expect(parseHHMM('12:5')).toBeNull();
		expect(parseHHMM('abc')).toBeNull();
		expect(parseHHMM('')).toBeNull();
		expect(parseHHMM(null)).toBeNull();
		expect(parseHHMM(undefined)).toBeNull();
		expect(parseHHMM(600 as unknown as string)).toBeNull();
	});
});

describe('minutesToHHMM', () => {
	it('zero-pads and wraps', () => {
		expect(minutesToHHMM(0)).toBe('00:00');
		expect(minutesToHHMM(60)).toBe('01:00');
		expect(minutesToHHMM(570)).toBe('09:30');
		expect(minutesToHHMM(1439)).toBe('23:59');
		expect(minutesToHHMM(1440)).toBe('00:00');
		expect(minutesToHHMM(-60)).toBe('23:00');
	});

	it('round-trips with parseHHMM', () => {
		for (const t of ['00:00', '01:00', '09:30', '13:45', '23:59']) {
			expect(minutesToHHMM(parseHHMM(t)!)).toBe(t);
		}
	});
});

describe('utcMinutesOfDay', () => {
	it('reads UTC components only', () => {
		expect(utcMinutesOfDay(atUtc(0, 0))).toBe(0);
		expect(utcMinutesOfDay(atUtc(9, 30))).toBe(570);
		expect(utcMinutesOfDay(atUtc(23, 59))).toBe(1439);
	});
});

describe('isValidBlock', () => {
	it('accepts well-formed distinct windows', () => {
		expect(isValidBlock({ start: '01:00', end: '04:00' })).toBe(true);
		expect(isValidBlock({ start: '22:00', end: '02:00' })).toBe(true);
	});

	it('rejects equal endpoints and malformed times', () => {
		expect(isValidBlock({ start: '01:00', end: '01:00' })).toBe(false);
		expect(isValidBlock({ start: '99:00', end: '04:00' })).toBe(false);
		expect(isValidBlock({ start: '', end: '04:00' })).toBe(false);
		expect(isValidBlock(null)).toBe(false);
		expect(isValidBlock(undefined)).toBe(false);
	});
});

describe('isWithinBlock', () => {
	const block = { start: '06:00', end: '10:00' };

	it('is inclusive of start, exclusive of end', () => {
		expect(isWithinBlock(parseHHMM('06:00')!, block)).toBe(true);
		expect(isWithinBlock(parseHHMM('07:30')!, block)).toBe(true);
		expect(isWithinBlock(parseHHMM('09:59')!, block)).toBe(true);
		expect(isWithinBlock(parseHHMM('10:00')!, block)).toBe(false);
		expect(isWithinBlock(parseHHMM('05:59')!, block)).toBe(false);
	});

	it('handles windows that wrap past midnight', () => {
		const wrap = { start: '22:00', end: '02:00' };
		expect(isWithinBlock(parseHHMM('22:00')!, wrap)).toBe(true);
		expect(isWithinBlock(parseHHMM('23:30')!, wrap)).toBe(true);
		expect(isWithinBlock(parseHHMM('00:30')!, wrap)).toBe(true);
		expect(isWithinBlock(parseHHMM('01:59')!, wrap)).toBe(true);
		expect(isWithinBlock(parseHHMM('02:00')!, wrap)).toBe(false);
		expect(isWithinBlock(parseHHMM('12:00')!, wrap)).toBe(false);
	});

	it('rejects invalid/equal blocks', () => {
		expect(isWithinBlock(100, { start: '01:00', end: '01:00' })).toBe(false);
		expect(isWithinBlock(100, { start: 'x', end: '01:00' })).toBe(false);
	});
});

describe('minutesUntilStart / minutesUntilEnd', () => {
	const block = { start: '06:00', end: '10:00' };

	it('counts forward to the start', () => {
		expect(minutesUntilStart(parseHHMM('05:55')!, block)).toBe(5);
		expect(minutesUntilStart(parseHHMM('06:00')!, block)).toBe(0);
		expect(minutesUntilStart(parseHHMM('06:01')!, block)).toBe(MINUTES(23, 59));
	});

	it('counts forward to the end, never reporting 0', () => {
		expect(minutesUntilEnd(parseHHMM('09:58')!, block)).toBe(2);
		expect(minutesUntilEnd(parseHHMM('06:00')!, block)).toBe(240);
		// Exactly at the (exclusive) end normalises to a full day rather than 0.
		expect(minutesUntilEnd(parseHHMM('10:00')!, block)).toBe(1440);
	});

	it('returns Infinity for invalid endpoints', () => {
		expect(minutesUntilStart(0, { start: 'x', end: '10:00' })).toBe(Infinity);
		expect(minutesUntilEnd(0, { start: '06:00', end: 'x' })).toBe(Infinity);
	});
});

// helper for the wrap math above
function MINUTES(h: number, m: number) {
	return h * 60 + m;
}

describe('getPeakHoursConfig', () => {
	const wrap = (peak_hours: any) => ({ info: { meta: { peak_hours } } });

	it('returns null when absent, disabled, or empty', () => {
		expect(getPeakHoursConfig(undefined)).toBeNull();
		expect(getPeakHoursConfig({})).toBeNull();
		expect(getPeakHoursConfig(wrap(null))).toBeNull();
		expect(getPeakHoursConfig(wrap({ enabled: false, blocks: [{ start: '01:00', end: '02:00' }] }))).toBeNull();
		expect(getPeakHoursConfig(wrap({ enabled: true, blocks: [] }))).toBeNull();
		expect(getPeakHoursConfig(wrap({ enabled: true }))).toBeNull();
	});

	it('drops invalid blocks and keeps valid ones', () => {
		const cfg = getPeakHoursConfig(
			wrap({
				enabled: true,
				blocks: [
					{ start: '01:00', end: '04:00' },
					{ start: '05:00', end: '05:00' }, // invalid (equal)
					{ start: 'nope', end: '10:00' } // invalid (parse)
				],
				note: 'hi'
			})
		);
		expect(cfg).not.toBeNull();
		expect(cfg!.blocks).toEqual([{ start: '01:00', end: '04:00' }]);
		expect(cfg!.note).toBe('hi');
	});

	it('treats a missing enabled flag as on when blocks are valid', () => {
		const cfg = getPeakHoursConfig(wrap({ blocks: [{ start: '01:00', end: '04:00' }] }));
		expect(cfg).not.toBeNull();
	});

	it('ignores a non-string note', () => {
		const cfg = getPeakHoursConfig(
			wrap({ enabled: true, blocks: [{ start: '01:00', end: '04:00' }], note: 123 })
		);
		expect(cfg!.note).toBeUndefined();
	});
});

describe('getPeakStatus', () => {
	const cfg = {
		enabled: true,
		blocks: [
			{ start: '01:00', end: '04:00' },
			{ start: '06:00', end: '10:00' }
		]
	};

	it('reports none when nothing applies', () => {
		expect(getPeakStatus(null, atUtc(12, 0)).state).toBe('none');
		expect(getPeakStatus(cfg, atUtc(12, 0)).state).toBe('none');
		expect(getPeakStatus(cfg, atUtc(5, 0)).state).toBe('none'); // 49 min before 06:00
	});

	it('reports active inside a window with minutes-until-end', () => {
		const s = getPeakStatus(cfg, atUtc(7, 0));
		expect(s.state).toBe('active');
		expect(s.block).toEqual({ start: '06:00', end: '10:00' });
		expect(s.minutesUntilEnd).toBe(180);
		expect(s.minutesUntilStart).toBeNull();
	});

	it('reports soon within the 10-minute lead', () => {
		const s = getPeakStatus(cfg, atUtc(5, 55));
		expect(s.state).toBe('soon');
		expect(s.block).toEqual({ start: '06:00', end: '10:00' });
		expect(s.minutesUntilStart).toBe(5);
		expect(s.minutesUntilEnd).toBeNull();
	});

	it('treats exactly 10 minutes out as soon, 11 as none', () => {
		expect(getPeakStatus(cfg, atUtc(5, 50)).state).toBe('soon');
		expect(getPeakStatus(cfg, atUtc(5, 49)).state).toBe('none');
	});

	it('prefers active over soon when both could apply', () => {
		// Inside 01:00–04:00 while 04:00... not adjacent; build an adjacency case.
		const adj = {
			enabled: true,
			blocks: [
				{ start: '06:00', end: '10:00' },
				{ start: '10:05', end: '12:00' }
			]
		};
		// 09:58: inside first window, and 10:05 starts in 7 min -> active wins.
		const s = getPeakStatus(adj, atUtc(9, 58));
		expect(s.state).toBe('active');
		expect(s.block).toEqual({ start: '06:00', end: '10:00' });
	});

	it('picks the soonest-ending active window among overlaps', () => {
		const overlap = {
			enabled: true,
			blocks: [
				{ start: '06:00', end: '12:00' },
				{ start: '07:00', end: '08:00' }
			]
		};
		const s = getPeakStatus(overlap, atUtc(7, 30));
		expect(s.state).toBe('active');
		expect(s.block).toEqual({ start: '07:00', end: '08:00' });
		expect(s.minutesUntilEnd).toBe(30);
	});

	it('handles wrap-around windows', () => {
		const wrapCfg = { enabled: true, blocks: [{ start: '22:00', end: '02:00' }] };
		expect(getPeakStatus(wrapCfg, atUtc(23, 0)).state).toBe('active');
		expect(getPeakStatus(wrapCfg, atUtc(1, 0)).state).toBe('active');
		expect(getPeakStatus(wrapCfg, atUtc(21, 55)).state).toBe('soon');
		expect(getPeakStatus(wrapCfg, atUtc(12, 0)).state).toBe('none');
	});

	it('respects a custom soon window', () => {
		expect(getPeakStatus(cfg, atUtc(5, 30), 30).state).toBe('soon');
		expect(getPeakStatus(cfg, atUtc(5, 30), 10).state).toBe('none');
	});
});

describe('formatBlock / formatBlocks', () => {
	it('formats a single block with an en dash', () => {
		expect(formatBlock({ start: '01:00', end: '04:00' })).toBe('01:00–04:00');
		expect(formatBlock({ start: '9:00', end: '10:00' })).toBe('09:00–10:00');
		expect(formatBlock({ start: 'x', end: '10:00' })).toBe('');
	});

	it('joins valid blocks and drops invalid ones', () => {
		expect(
			formatBlocks([
				{ start: '01:00', end: '04:00' },
				{ start: '06:00', end: '10:00' }
			])
		).toBe('01:00–04:00, 06:00–10:00');
		expect(
			formatBlocks([
				{ start: '01:00', end: '04:00' },
				{ start: '05:00', end: '05:00' }
			])
		).toBe('01:00–04:00');
		expect(formatBlocks([])).toBe('');
	});
});

describe('constants', () => {
	it('exposes a sensible default note and lead window', () => {
		expect(DEFAULT_PEAK_NOTE).toMatch(/expensive/i);
		expect(PEAK_SOON_WINDOW_MINUTES).toBe(10);
	});
});
