import type { BootstrapInclude } from '$lib/apis';

type ComponentMap = Partial<Record<BootstrapInclude, unknown>>;

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
