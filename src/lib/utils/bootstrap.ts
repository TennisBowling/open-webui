import type { BootstrapInclude } from '$lib/apis';

type ComponentMap = Partial<Record<BootstrapInclude, unknown>>;
type ComponentEtags = Partial<Record<BootstrapInclude, string>>;

let pending: ComponentMap = {};

export const setBootstrapComponents = (components: ComponentMap | null | undefined) => {
	if (!components) return;
	pending = { ...pending, ...components };
};

export const peekBootstrap = <T>(name: BootstrapInclude): T | undefined => {
	return pending[name] as T | undefined;
};

export const consumeBootstrap = <T>(name: BootstrapInclude): T | undefined => {
	if (!(name in pending)) return undefined;
	const v = pending[name];
	delete pending[name];
	return v as T;
};

export const hasBootstrap = (name: BootstrapInclude): boolean => name in pending;

export const clearBootstrap = () => {
	pending = {};
};

export const BOOTSTRAP_BUNDLE_ETAG_KEY = (userId: string | null | undefined) =>
	`bootstrap:bundle_etag:${userId ?? 'anon'}`;

export const BOOTSTRAP_BUNDLE_BODY_KEY = (userId: string | null | undefined) =>
	`bootstrap:bundle_body:${userId ?? 'anon'}`;

// Separate bundle etag for the sidebar-subset reconcile request. The server's
// bundle_etag is computed over the *requested* include set, so a 5-component
// sidebar reconcile produces a different bundle etag than the 11-component boot
// bundle — they must not share an If-None-Match key. Per-component etags ARE
// include-independent, so both flows share BOOTSTRAP_BUNDLE_BODY_KEY.
export const BOOTSTRAP_SIDEBAR_ETAG_KEY = (userId: string | null | undefined) =>
	`bootstrap:sidebar_etag:${userId ?? 'anon'}`;

export type BootstrapCacheEntry = {
	components: ComponentMap;
	components_etags: ComponentEtags;
};

export const readBootstrapCache = (
	userId: string | null | undefined
): BootstrapCacheEntry | null => {
	try {
		const raw = localStorage.getItem(BOOTSTRAP_BUNDLE_BODY_KEY(userId));
		if (!raw) return null;
		const parsed = JSON.parse(raw);
		if (!parsed || typeof parsed !== 'object' || !parsed.components) return null;
		return {
			components: parsed.components ?? {},
			components_etags: parsed.components_etags ?? {}
		};
	} catch (e) {
		console.error('Failed to read cached bootstrap bundle', e);
		return null;
	}
};

export const writeBootstrapCache = (
	userId: string | null | undefined,
	entry: BootstrapCacheEntry
) => {
	try {
		localStorage.setItem(
			BOOTSTRAP_BUNDLE_BODY_KEY(userId),
			JSON.stringify({
				components: entry.components ?? {},
				components_etags: entry.components_etags ?? {}
			})
		);
	} catch (e) {
		console.error('Failed to persist bootstrap bundle', e);
	}
};

// Merge freshly-refreshed components into the shared bundle cache (used by the
// sidebar reconcile so the boot's instant-paint cache + per-component etags
// stay current for the untouched components).
export const mergeBootstrapCacheComponents = (
	userId: string | null | undefined,
	components: ComponentMap,
	componentsEtags: ComponentEtags
) => {
	const cur = readBootstrapCache(userId) ?? { components: {}, components_etags: {} };
	writeBootstrapCache(userId, {
		components: { ...cur.components, ...(components ?? {}) },
		components_etags: { ...cur.components_etags, ...(componentsEtags ?? {}) }
	});
};

// Build the compact JSON x-bootstrap-etags header value (Contract 1) from the
// cached per-component etags, limited to the components being requested. Returns
// null when there's nothing to send (server then behaves exactly as today).
export const buildBootstrapEtagsHeader = (
	etags: ComponentEtags | null | undefined,
	names: BootstrapInclude[]
): string | null => {
	if (!etags) return null;
	const map: Record<string, string> = {};
	for (const name of names) {
		const et = etags[name];
		if (typeof et === 'string' && et) map[name] = et;
	}
	if (Object.keys(map).length === 0) return null;
	return JSON.stringify(map);
};

// Given a 200 bootstrap response (which omits components the client already has,
// listing them under `unchanged`), reconstruct the full component set by filling
// the unchanged ones from cache. Any unchanged component missing from cache is
// reported in `missing` so the caller can re-issue a full (header-less) request.
export const applyUnchangedFromCache = (
	response: { components?: ComponentMap; unchanged?: string[] },
	cached: BootstrapCacheEntry | null
): { components: ComponentMap; missing: BootstrapInclude[] } => {
	const components: ComponentMap = { ...(response.components ?? {}) };
	const missing: BootstrapInclude[] = [];
	const unchanged = Array.isArray(response.unchanged) ? response.unchanged : [];
	for (const name of unchanged) {
		if (cached?.components && name in cached.components) {
			components[name as BootstrapInclude] = (cached.components as ComponentMap)[
				name as BootstrapInclude
			];
		} else {
			missing.push(name as BootstrapInclude);
		}
	}
	return { components, missing };
};
