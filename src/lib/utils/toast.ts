// Thin wrapper around svelte-sonner's `toast` that collapses duplicate
// notifications.
//
// Why: toasts can be raised from several independent places for the same
// logical event — a client-side handler AND a socket-pushed `notification`
// event, or two component instances that briefly coexist during an SPA route
// transition (a new `Chat.svelte` mounts its `events` socket listener before
// the outgoing one is torn down, so a single server event is handled twice).
// The result is the same message stacked twice.
//
// svelte-sonner already has the primitive to fix this: calling `toast` with an
// `id` that matches a toast currently on screen UPDATES it in place instead of
// adding a second one. So we derive a STABLE id from the toast's type + message
// and inject it whenever the caller didn't supply one. The dedupe is naturally
// scoped to visibility: once the first toast has dismissed and left the store,
// the same id is free again, so genuinely spaced-out repeats (e.g. saving
// twice, a minute apart) still show each time — only rapid/duplicate raises of
// the identical message collapse.
//
// `custom`, `promise`, `loading`, `dismiss`, etc. are passed through untouched:
// they either already manage their own ids or carry non-string payloads.
import { toast as sonnerToast } from 'svelte-sonner';

type ToastData = Record<string, any> | undefined;

const STABLE_METHODS = ['message', 'success', 'info', 'warning'] as const;
type StableMethod = (typeof STABLE_METHODS)[number];

const stableId = (type: string, message: unknown): string => {
	// Ids are compared by strict equality in svelte-sonner, so the raw string is
	// a perfectly good key — no hashing needed. Coerce non-strings defensively.
	const text = typeof message === 'string' ? message : String(message ?? '');
	return `owui-dedupe:${type}:${text}`;
};

// Returns `data` unchanged if the caller already set an explicit id (they want
// to control/update that toast themselves); otherwise injects our stable id.
const withDedupeId = (type: string, message: unknown, data: ToastData): ToastData => {
	if (data && data.id !== undefined && data.id !== null) return data;
	return { ...(data ?? {}), id: stableId(type, message) };
};

const wrappedBase = (message: unknown, data?: ToastData) =>
	(sonnerToast as any)(message, withDedupeId('default', message, data));

// Re-attach every property of the original `toast` (methods + any statics),
// then override the string-message helpers with dedupe-aware versions. Anything
// not in STABLE_METHODS (custom/promise/loading/dismiss/…) keeps its original
// behavior.
const wrapped = Object.assign(wrappedBase, sonnerToast) as typeof sonnerToast;

for (const method of STABLE_METHODS) {
	const original = (sonnerToast as any)[method] as (m: unknown, d?: ToastData) => unknown;
	(wrapped as any)[method] = (message: unknown, data?: ToastData) =>
		original(message, withDedupeId(method, message, data));
}

// `error` deserves the same dedupe but is called constantly with dynamic
// strings; treat it identically to the others (identical error text collapsing
// is a feature, not a bug).
{
	const originalError = (sonnerToast as any).error as (m: unknown, d?: ToastData) => unknown;
	(wrapped as any).error = (message: unknown, data?: ToastData) =>
		originalError(message, withDedupeId('error', message, data));
}

export const toast = wrapped;
