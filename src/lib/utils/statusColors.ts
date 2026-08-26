/**
 * Warm semantic status colors — the single source of truth for how status
 * (info / success / warning / error / muted) is colored across the app.
 *
 * The design system is a warm "ink on paper" theme: default-Tailwind neon hues
 * (blue/green/yellow/red/…) clash with it. These maps translate each semantic
 * status into the warm palette:
 *   - error   → error-brick   (#BF4D43 / dark #D88577)   — matches Error.svelte
 *   - warning → warm ochre     (warning / warning-dark)  — from the hljs palette
 *   - success → warm sage      (success / success-dark)  — from the hljs palette
 *   - info    → book-cloth      (the brand accent)
 *   - muted   → warm gray
 *
 * Consume these instead of hardcoding hue classes so Badge, Banner, the admin
 * banner editor, and any future status surface stay in lockstep.
 */

export type StatusType = 'info' | 'success' | 'warning' | 'error' | 'muted';

/** Soft filled chip/badge: tinted background + on-tint text. */
export const statusChipClass: Record<StatusType, string> = {
	info: 'bg-book-cloth/15 text-book-cloth dark:bg-book-cloth/20 dark:text-kraft',
	success: 'bg-success/15 text-success dark:bg-success/20 dark:text-success-dark',
	warning: 'bg-warning/15 text-warning dark:bg-warning/20 dark:text-warning-dark',
	error: 'bg-error-brick/15 text-error-brick dark:bg-error-brick/20 dark:text-error-brick-dark',
	muted: 'bg-gray-500/15 text-gray-600 dark:bg-gray-500/25 dark:text-gray-300'
};

/** Just the foreground/text color for a status (e.g. an inline label or icon). */
export const statusTextClass: Record<StatusType, string> = {
	info: 'text-book-cloth dark:text-kraft',
	success: 'text-success dark:text-success-dark',
	warning: 'text-warning dark:text-warning-dark',
	error: 'text-error-brick dark:text-error-brick-dark',
	muted: 'text-gray-500 dark:text-gray-400'
};

/** Soft notice panel: tinted background + hairline tinted border. */
export const statusPanelClass: Record<StatusType, string> = {
	info: 'bg-book-cloth/10 border-hairline border-book-cloth/25',
	success: 'bg-success/10 border-hairline border-success/25',
	warning: 'bg-warning/10 border-hairline border-warning/25',
	error: 'bg-error-brick/10 border-hairline border-error-brick/20',
	muted: 'bg-gray-500/10 border-hairline border-gray-300 dark:border-gray-700'
};

/** Solid fill (e.g. a status dot / progress bar). */
export const statusSolidClass: Record<StatusType, string> = {
	info: 'bg-book-cloth',
	success: 'bg-success dark:bg-success-dark',
	warning: 'bg-warning dark:bg-warning-dark',
	error: 'bg-error-brick',
	muted: 'bg-gray-400 dark:bg-gray-500'
};

const normalize = (type: string | undefined): StatusType => {
	const t = (type ?? '').toLowerCase();
	return (['info', 'success', 'warning', 'error', 'muted'] as const).includes(t as StatusType)
		? (t as StatusType)
		: 'info';
};

export const chipClass = (type?: string) => statusChipClass[normalize(type)];
export const textClass = (type?: string) => statusTextClass[normalize(type)];
export const panelClass = (type?: string) => statusPanelClass[normalize(type)];
export const solidClass = (type?: string) => statusSolidClass[normalize(type)];
