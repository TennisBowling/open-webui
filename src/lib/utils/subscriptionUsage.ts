// Display helpers for subscription-provider usage windows (the percentage
// rate-limit windows in the `subscriptionUsage` store). Shared by the token
// panels in Chat.svelte and Placeholder.svelte.

type UsageWindow = {
	id?: string;
	limit_id?: string;
	scope?: string;
	used_percent?: number;
	window_minutes?: number;
	resets_at?: number;
};

// Providers can return several independently metered buckets for one
// connection (for example, `codex` and `codex_bengalfox`). Show the bucket
// identifier in a readable form so identical time windows do not look like
// duplicate bars.
export const formatSubscriptionLimitLabel = (providerName: string, window: UsageWindow): string => {
	const limitId = window?.limit_id?.trim();
	if (!limitId) return providerName || 'Usage';

	const limitName = limitId
		.split(/[_-]+/)
		.filter(Boolean)
		.map((part) => part.charAt(0).toUpperCase() + part.slice(1))
		.join(' ');
	const provider = providerName?.trim();
	if (!provider || limitName.toLowerCase().startsWith(provider.toLowerCase())) return limitName;
	return `${provider} ${limitName}`;
};

// "10080 minutes" reads as nothing; name the common windows and fall back to
// the largest clean unit.
export const formatWindowLabel = (window: UsageWindow): string => {
	const mins = window?.window_minutes;
	if (!mins || mins <= 0) return window?.scope === 'secondary' ? 'Secondary' : 'Usage';
	if (mins % 10080 === 0) return mins === 10080 ? 'Weekly' : `${mins / 10080}w`;
	if (mins % 1440 === 0) return mins === 1440 ? 'Daily' : `${mins / 1440}d`;
	if (mins % 60 === 0) return `${mins / 60}h`;
	return `${mins}m`;
};

export const formatUsedPercent = (window: UsageWindow): string => {
	const percent = window?.used_percent ?? 0;
	// One decimal below 10% so early usage doesn't read as a frozen 0%.
	return percent > 0 && percent < 10 ? `${percent.toFixed(1)}%` : `${Math.round(percent)}%`;
};

export const formatResetsIn = (resetsAt: number | undefined, nowMs: number): string => {
	if (!resetsAt) return '';
	const secs = Math.floor(resetsAt - nowMs / 1000);
	if (secs <= 0) return 'resets soon';
	const d = Math.floor(secs / 86400);
	const h = Math.floor((secs % 86400) / 3600);
	const m = Math.floor((secs % 3600) / 60);
	if (d > 0) return `resets in ${d}d ${h}h`;
	if (h > 0) return `resets in ${h}h ${m}m`;
	return `resets in ${Math.max(1, m)}m`;
};
