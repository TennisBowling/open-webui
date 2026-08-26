// Peak-hours helpers.
//
// A model can advertise "peak hours" — windows of high demand, configured by an
// admin per-model and stored on `model.info.meta.peak_hours`. The UI surfaces a
// soft, non-blocking heads-up while the current time falls inside a window (or is
// about to). Everything here is computed against UTC so it stays timezone-proof:
// the admin enters UTC times, and we compare against the browser clock's UTC
// components. No server round-trip is involved.

import { readable } from 'svelte/store';

export interface PeakBlock {
	/** Window start, "HH:MM" in 24h UTC. */
	start: string;
	/** Window end (exclusive), "HH:MM" in 24h UTC. */
	end: string;
}

export interface PeakHoursConfig {
	enabled?: boolean;
	blocks?: PeakBlock[];
	/** Optional admin note shown alongside the notice. */
	note?: string;
}

export type PeakState = 'active' | 'soon' | 'none';

export interface PeakStatus {
	state: PeakState;
	/** The active window ('active') or the upcoming window ('soon'); null otherwise. */
	block: PeakBlock | null;
	/** Minutes until the upcoming window starts ('soon' only). */
	minutesUntilStart: number | null;
	/** Minutes until the active window ends ('active' only). */
	minutesUntilEnd: number | null;
}

/** Default note when an admin enables peak hours without writing their own. */
export const DEFAULT_PEAK_NOTE = 'This model is more expensive during peak hours.';

/** How early (in minutes) we warn that a window is about to start. */
export const PEAK_SOON_WINDOW_MINUTES = 10;

const MINUTES_PER_DAY = 24 * 60;
const HHMM_RE = /^(\d{1,2}):(\d{2})(?::\d{2})?$/;

/**
 * Parse a "HH:MM" (UTC, 24h) string into minutes-from-midnight [0, 1439].
 * Returns null for anything malformed or out of range.
 */
export function parseHHMM(value: unknown): number | null {
	if (typeof value !== 'string') return null;
	const match = HHMM_RE.exec(value.trim());
	if (!match) return null;
	const hours = Number(match[1]);
	const minutes = Number(match[2]);
	if (!Number.isInteger(hours) || !Number.isInteger(minutes)) return null;
	if (hours < 0 || hours > 23 || minutes < 0 || minutes > 59) return null;
	return hours * 60 + minutes;
}

/** Format minutes-from-midnight back into a zero-padded "HH:MM" string. */
export function minutesToHHMM(totalMinutes: number): string {
	const wrapped = ((Math.floor(totalMinutes) % MINUTES_PER_DAY) + MINUTES_PER_DAY) % MINUTES_PER_DAY;
	const hours = Math.floor(wrapped / 60);
	const minutes = wrapped % 60;
	return `${String(hours).padStart(2, '0')}:${String(minutes).padStart(2, '0')}`;
}

/** Minutes-from-midnight for a Date, in UTC. */
export function utcMinutesOfDay(date: Date): number {
	return date.getUTCHours() * 60 + date.getUTCMinutes();
}

/**
 * A block is valid when both ends parse and are not equal. Equal endpoints are
 * rejected because they're ambiguous (zero-length vs. all-day); the editor
 * prevents saving them.
 */
export function isValidBlock(block: PeakBlock | null | undefined): boolean {
	if (!block) return false;
	const start = parseHHMM(block.start);
	const end = parseHHMM(block.end);
	return start !== null && end !== null && start !== end;
}

/**
 * Is minute-of-day `t` inside `block`? `end` is exclusive. Windows that wrap past
 * midnight (start > end, e.g. 22:00–02:00) are handled. Invalid blocks are never
 * "within".
 */
export function isWithinBlock(t: number, block: PeakBlock): boolean {
	const start = parseHHMM(block.start);
	const end = parseHHMM(block.end);
	if (start === null || end === null || start === end) return false;
	if (start < end) {
		return t >= start && t < end;
	}
	// Wraps past midnight.
	return t >= start || t < end;
}

/** Minutes from `t` forward to the block's start (0..1439). Infinity if invalid. */
export function minutesUntilStart(t: number, block: PeakBlock): number {
	const start = parseHHMM(block.start);
	if (start === null) return Infinity;
	return (((start - t) % MINUTES_PER_DAY) + MINUTES_PER_DAY) % MINUTES_PER_DAY;
}

/** Minutes from `t` forward to the block's end (1..1440). Infinity if invalid. */
export function minutesUntilEnd(t: number, block: PeakBlock): number {
	const end = parseHHMM(block.end);
	if (end === null) return Infinity;
	const delta = (((end - t) % MINUTES_PER_DAY) + MINUTES_PER_DAY) % MINUTES_PER_DAY;
	// `t` is only ever inside a window when this is called for an active block, so
	// the distance to the (exclusive) end is strictly positive; normalise a 0 to a
	// full day rather than reporting "ends now".
	return delta === 0 ? MINUTES_PER_DAY : delta;
}

/**
 * Read a model's effective peak-hours config, or null when there's nothing to
 * show. Filters out invalid blocks and respects an explicit `enabled: false`.
 */
export function getPeakHoursConfig(model: any): PeakHoursConfig | null {
	const cfg = model?.info?.meta?.peak_hours;
	if (!cfg || typeof cfg !== 'object') return null;
	if (cfg.enabled === false) return null;
	const blocks = Array.isArray(cfg.blocks) ? cfg.blocks.filter(isValidBlock) : [];
	if (blocks.length === 0) return null;
	return {
		enabled: true,
		blocks,
		note: typeof cfg.note === 'string' ? cfg.note : undefined
	};
}

/**
 * Classify `date` against a config: inside a window ('active'), within
 * `soonWindow` minutes of one starting ('soon'), or neither ('none'). 'active'
 * always wins over 'soon'. Among ties, the soonest-ending active window and the
 * soonest-starting upcoming window are chosen for determinism.
 */
export function getPeakStatus(
	cfg: PeakHoursConfig | null,
	date: Date,
	soonWindow: number = PEAK_SOON_WINDOW_MINUTES
): PeakStatus {
	const none: PeakStatus = {
		state: 'none',
		block: null,
		minutesUntilStart: null,
		minutesUntilEnd: null
	};
	if (!cfg || !Array.isArray(cfg.blocks) || cfg.blocks.length === 0) return none;

	const t = utcMinutesOfDay(date);

	let activeBlock: PeakBlock | null = null;
	let activeEnd = Infinity;
	let soonBlock: PeakBlock | null = null;
	let soonStart = Infinity;

	for (const block of cfg.blocks) {
		if (!isValidBlock(block)) continue;
		if (isWithinBlock(t, block)) {
			const end = minutesUntilEnd(t, block);
			if (end < activeEnd) {
				activeEnd = end;
				activeBlock = block;
			}
		} else {
			const until = minutesUntilStart(t, block);
			if (until > 0 && until <= soonWindow && until < soonStart) {
				soonStart = until;
				soonBlock = block;
			}
		}
	}

	if (activeBlock) {
		return {
			state: 'active',
			block: activeBlock,
			minutesUntilStart: null,
			minutesUntilEnd: activeEnd
		};
	}
	if (soonBlock) {
		return {
			state: 'soon',
			block: soonBlock,
			minutesUntilStart: soonStart,
			minutesUntilEnd: null
		};
	}
	return none;
}

/** Format one block as "HH:MM–HH:MM" (en dash), normalising the times. */
export function formatBlock(block: PeakBlock): string {
	const start = parseHHMM(block.start);
	const end = parseHHMM(block.end);
	if (start === null || end === null) return '';
	return `${minutesToHHMM(start)}–${minutesToHHMM(end)}`;
}

/** Format a list of blocks as "01:00–04:00, 06:00–10:00" (does not append "UTC"). */
export function formatBlocks(blocks: PeakBlock[]): string {
	return (blocks ?? [])
		.filter(isValidBlock)
		.map(formatBlock)
		.filter(Boolean)
		.join(', ');
}

/**
 * A shared clock that ticks every 30s. The interval only runs while something is
 * subscribed (Svelte `readable` semantics), so it's free in tests/SSR. Components
 * read `$peakClock` to recompute peak status as time passes without a reload.
 */
export const peakClock = readable(new Date(), (set) => {
	set(new Date());
	const interval = setInterval(() => set(new Date()), 30_000);
	return () => clearInterval(interval);
});
