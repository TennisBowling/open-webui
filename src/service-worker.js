/// <reference types="@sveltejs/kit" />
/**
 * Shell-only service worker for offline resilience on bad/flaky connections.
 *
 * Strategy (deliberately conservative):
 *  - Precache ONLY the tiny SPA shell document + the cold-load closure at install
 *    (entry + "/" route chunks + eager fonts + brand icons, ~1.1 MB). We NEVER
 *    precache the SvelteKit `build`/`files` arrays wholesale (tens of MB incl.
 *    Pyodide/ONNX wasm; an all-or-nothing install would never finish on bad cell
 *    and the SW would never activate).
 *  - Across deploys, install COPIES byte-identical entries (same URL) from any
 *    previous assets-* cache and only network-fetches the genuine misses. The
 *    cold-load closure is dominated by content-hashed /_app/immutable/* URLs, so
 *    an unchanged chunk keeps its URL → identity is guaranteed and we avoid
 *    re-downloading ~800 KB of JS/CSS on every redeploy.
 *  - All API / socket / streaming / user-file / version-probe traffic passes
 *    straight through to the network (and its own HTTP caching). The SW never
 *    caches authenticated per-user data — that would risk cross-user leakage on a
 *    shared device.
 *  - ONE deliberate exception: model avatar bytes (GET .../api/v1/models/model/
 *    profile/image?id=...&v=<contenthash>) are cache-first in a durable AVATAR_CACHE
 *    that survives deploys (unlike SHELL_CACHE/ASSET_CACHE, which are version-keyed
 *    and pruned every activate). These are model logos keyed by a content-hash URL,
 *    not user data — a changed avatar gets a new URL, so caching it forever is
 *    always correct, and only the genuine immutable response (not an error page,
 *    fallback favicon, or external redirect) is ever stored — see the fetch handler
 *    below for the exact rule. This keeps icons resident through the iOS PWA's
 *    aggressive HTTP-cache eviction and through cookie-expiry windows offline.
 *  - Navigations are CACHE-FIRST: the cached shell is served instantly (zero
 *    network RTT — this is what makes a warm PWA open feel immediate) and a
 *    background refresh keeps the cached copy current. When no cached shell
 *    exists yet (first visit), fall back to network with a short timeout.
 *
 * This preserves the redeploy-forces-fresh contract: a new git HEAD changes
 * `version`, the version poll in +layout.svelte still flips updated.current and
 * hard-reloads, and content-hashed chunk URLs mean a new build is always fetched
 * fresh even while an older SW is still controlling. Serving the shell from
 * cache adds at most ONE stale boot right after a redeploy: the boot-time
 * updated.check() in +layout.svelte sees the new version immediately and the
 * app reloads itself within its early-boot window (see the `updated` subscribe
 * there). We do NOT call skipWaiting() — the app-level version reload stays the
 * single source of truth for activation.
 */
import { version, base } from '$service-worker';

const SHELL_CACHE = `shell-${version}`;
const ASSET_CACHE = `assets-${version}`;

// Durable (NOT version-keyed) cache for model avatar bytes — survives redeploys so
// the iOS PWA never re-fetches logos it already has. Safe because the URL itself
// is content-hashed (`v=<contenthash>` query param), so cache-first is always
// correct; a changed avatar simply gets a new URL. Excluded from the activate-time
// prune below and capped at AVATAR_CACHE_MAX entries (FIFO eviction).
const AVATAR_CACHE = 'avatars-v1';
const AVATAR_CACHE_MAX = 150;

// adapter-static serves index.html for any navigation; caching it lets the shell
// boot offline after one online load.
const SHELL_URL = `${base}/`;

// Cap the first-visit (no cached shell yet) navigation on a stalled connection
// so we fail fast instead of a perpetual white screen (bad cell keeps the
// TCP/TLS socket "open" but bytes never arrive).
const NAV_TIMEOUT_MS = 3500;

// Same-origin paths the SW must NEVER intercept (auth/data/socket/streaming +
// the version probe + user files which are HTTP-cached via Cache-Control +
// backend-rendered pages that are NOT the SPA shell).
const PASS_THROUGH = [
	`${base}/api/`,
	`${base}/openai`,
	`${base}/ollama`,
	`${base}/ws/`, // socket.io transport
	`${base}/files/`,
	`${base}/_app/version.json`,
	`${base}/docs`, // FastAPI swagger UI — serving the SPA shell here would break it
	`${base}/redoc`,
	`${base}/openapi.json`,
	// Backend-rendered top-level navigations that must NEVER get the SPA shell:
	// OAuth login/callback redirects (SSO + tool-server auth flows), health
	// probes, and cached backend files.
	`${base}/oauth/`,
	`${base}/health`,
	`${base}/cache/`
];

// Durable, non-user-specific static assets served OUTSIDE /_app/immutable/ that
// the fetch handler is allowed to serve from (and refresh into) the version-keyed
// asset cache. Without this, precached fonts/icons would be unreachable — the
// runtime only cache-firsts /_app/immutable/*.
function isCacheableStatic(pathname) {
	const p = base && pathname.startsWith(base) ? pathname.slice(base.length) : pathname;
	if (p.startsWith('/assets/fonts/')) return true;
	if (p.startsWith('/static/') && /\.(png|svg|ico|woff2|webmanifest)$/.test(p)) return true;
	// Root brand PNGs referenced by the shell / components.
	if (/^\/(favicon\.png|user\.png|image-placeholder\.png|marker-[^/]*\.png)$/.test(p)) return true;
	return false;
}

self.addEventListener('install', (event) => {
	event.waitUntil(
		(async () => {
			// Invariant this handler must uphold: activate prunes every cache except
			// this version's (and avatars), so install must NEVER complete with an
			// empty/unbootable shell+closure when a previous version's caches held a
			// working one — otherwise one flaky install right after a redeploy
			// silently destroys the PWA's ability to boot offline until the next
			// fully-healthy online visit. Every network failure below therefore
			// falls back to carrying the previous version's entries forward.

			// Precache the SPA shell fallback so navigations work offline.
			let shellCarriedForward = false;
			try {
				const shell = await caches.open(SHELL_CACHE);
				try {
					await shell.add(new Request(SHELL_URL, { cache: 'reload' }));
				} catch (e) {
					// Network couldn't supply the new shell — keep the previous one.
					shellCarriedForward = await carryForwardShell(shell);
				}
			} catch (e) {
				/* best-effort */
			}
			// Precache the cold-load closure (entry + "/" route chunks + eager fonts +
			// brand icons, ~1.1 MB) into the DURABLE Cache API on the first visit.
			// Mobile browsers evict the HTTP disk cache aggressively, so without this
			// the app re-fetches the whole shell every time; the Cache API copy (esp.
			// with persisted storage / installed to home screen) survives that.
			try {
				const res = await fetch(`${base}/sw-precache.json`, { cache: 'no-cache' });
				if (res.ok) {
					const urls = await res.json();
					await precacheClosure(urls);
					// A carried-forward (old) shell references old chunk URLs that the
					// new manifest no longer lists — carry those forward too, or the
					// activate prune would strand the old shell without its closure.
					if (shellCarriedForward) {
						await carryForwardAllAssets();
					}
				} else {
					await carryForwardAllAssets();
				}
			} catch (e) {
				// Manifest unreachable — whatever shell we ended up with must keep its
				// chunks resolvable offline; content-hashed URLs make this safe.
				await carryForwardAllAssets().catch(() => {});
			}
		})()
	);
});

// Copy the previous deploy's cached shell into the new SHELL_CACHE. Returns
// whether a copy was found (the caller then also carries the old assets
// forward so that shell's chunk URLs stay resolvable).
async function carryForwardShell(shell) {
	try {
		for (const key of await caches.keys()) {
			if (!key.startsWith('shell-') || key === SHELL_CACHE) continue;
			const hit = await (await caches.open(key)).match(SHELL_URL);
			if (hit) {
				await shell.put(SHELL_URL, hit.clone());
				return true;
			}
		}
	} catch (e) {
		/* best-effort */
	}
	return false;
}

// Copy every entry from previous assets-* caches into the new ASSET_CACHE.
// Only used on degraded installs; a later healthy install prunes naturally
// because precacheClosure copies manifest entries only.
async function carryForwardAllAssets() {
	const cache = await caches.open(ASSET_CACHE);
	for (const key of await caches.keys()) {
		if (!key.startsWith('assets-') || key === ASSET_CACHE) continue;
		const old = await caches.open(key);
		for (const req of await old.keys()) {
			if (await cache.match(req)) continue;
			const hit = await old.match(req);
			if (hit) await cache.put(req, hit.clone());
		}
	}
}

// Populate ASSET_CACHE from the precache list, reusing byte-identical entries from
// previous deploys' assets-* caches so redeploys only download the genuine misses.
async function precacheClosure(urls) {
	const cache = await caches.open(ASSET_CACHE);
	const keys = await caches.keys();
	const oldCaches = await Promise.all(
		keys.filter((k) => k.startsWith('assets-') && k !== ASSET_CACHE).map((k) => caches.open(k))
	);

	const misses = [];
	await Promise.all(
		urls.map(async (u) => {
			const req = new Request(u);
			if (await cache.match(req)) return; // already present in the new cache
			// Same URL ⇒ same bytes (content-hashed chunks; pinned vendored fonts/icons
			// only change under a new name) → safe to carry forward without a refetch.
			for (const oc of oldCaches) {
				const hit = await oc.match(req);
				if (hit) {
					await cache.put(req, hit.clone());
					return;
				}
			}
			misses.push(u);
		})
	);
	// Fetch only what we couldn't carry forward; tolerate individual failures so
	// install always completes and activates.
	await Promise.allSettled(misses.map((u) => cache.add(u)));
}

self.addEventListener('activate', (event) => {
	event.waitUntil(
		(async () => {
			// Runs only AFTER install's waitUntil resolved, so the copy-forward above
			// has already read the old assets-* caches before we prune them here.
			for (const key of await caches.keys()) {
				if (key !== SHELL_CACHE && key !== ASSET_CACHE && key !== AVATAR_CACHE) {
					await caches.delete(key);
				}
			}
			await self.clients.claim();
		})()
	);
});

self.addEventListener('fetch', (event) => {
	const { request } = event;
	if (request.method !== 'GET') return;

	const url = new URL(request.url);
	if (url.origin !== self.location.origin) return; // cross-origin: leave alone
	if (request.headers.has('range')) return; // media range requests: pass through

	// Model avatars: cache-first, ahead of the general PASS_THROUGH-for-/api/ rule
	// below (this is the one deliberate exception — see the top-of-file comment).
	if (url.pathname === `${base}/api/v1/models/model/profile/image`) {
		event.respondWith(handleAvatarRequest(event));
		return;
	}

	if (PASS_THROUGH.some((p) => url.pathname.startsWith(p))) return;

	// Content-hashed immutable assets → cache-first (safe; never user-specific).
	if (url.pathname.startsWith(`${base}/_app/immutable/`)) {
		event.respondWith(
			caches.open(ASSET_CACHE).then(async (cache) => {
				const hit = await cache.match(request);
				if (hit) return hit;
				const res = await fetch(request);
				if (res.ok) cache.put(request, res.clone());
				return res;
			})
		);
		return;
	}

	// Durable static assets (fonts, brand icons) → stale-while-revalidate so the
	// precached copies are actually served (and kept fresh) on flaky links.
	if (isCacheableStatic(url.pathname)) {
		event.respondWith(
			(async () => {
				const cache = await caches.open(ASSET_CACHE);
				const hit = await cache.match(request);
				const network = fetch(request)
					.then((res) => {
						if (res.ok) cache.put(request, res.clone());
						return res;
					})
					.catch(() => null);
				if (hit) {
					event.waitUntil(network); // refresh in the background
					return hit;
				}
				return (await network) || Response.error();
			})()
		);
		return;
	}

	// Navigations → cache-first (instant boot), background refresh keeps it fresh.
	if (request.mode === 'navigate') {
		event.respondWith(handleNavigate(event));
		return;
	}

	// Everything else: default to the network.
});

// Cache-first for model avatars, keyed by the full request URL (the `v=<hash>`
// query param changes whenever the avatar changes, so a cache hit is always the
// current image).
async function handleAvatarRequest(event) {
	const request = event.request;
	const cache = await caches.open(AVATAR_CACHE);
	const hit = await cache.match(request);
	if (hit) return hit;

	const res = await fetch(request);
	// Only persist the genuine avatar response, never an error body, the
	// cookie-outage favicon fallback, or an external-URL redirect target. The
	// backend marks exactly the real, contenthash-addressed avatar response
	// `Cache-Control: private, max-age=31536000, immutable` — the favicon
	// fallback is max-age=300 and an external redirect is max-age=3600, so
	// checking for `immutable` is a precise, cheap discriminator.
	if (res.ok && (res.headers.get('cache-control') || '').includes('immutable')) {
		const putAndTrim = cache
			.put(request, res.clone())
			.then(() => trimAvatarCache(cache))
			.catch(() => {});
		event.waitUntil(putAndTrim);
	}
	return res;
}

// FIFO size cap so a long-lived install never grows AVATAR_CACHE unbounded.
// cache.keys() returns entries in insertion order, so the oldest is first.
async function trimAvatarCache(cache) {
	const keys = await cache.keys();
	if (keys.length <= AVATAR_CACHE_MAX) return;
	const excess = keys.length - AVATAR_CACHE_MAX;
	for (let i = 0; i < excess; i++) {
		await cache.delete(keys[i]);
	}
}

async function handleNavigate(event) {
	const request = event.request;

	// A navigation whose last path segment has an extension (/static/foo.txt,
	// /manifest.json, …) is a file the backend serves, not an SPA route — never
	// substitute the shell for those.
	const pathname = new URL(request.url).pathname;
	const lastSegment = pathname.split('/').pop() || '';
	if (lastSegment.includes('.')) {
		return fetch(request);
	}

	// Cache-first: the single biggest lever for a warm PWA open. The shell is a
	// pure SPA bootstrap document (adapter-static fallback — identical for every
	// route), so serving the cached copy costs nothing in correctness: all data
	// is fetched by the app itself, and a redeploy is caught by the boot-time
	// version check (see +layout.svelte) which reloads within its early-boot
	// window while the background refresh below has already updated this cache.
	const cache = await caches.open(SHELL_CACHE);
	const cached = await cache.match(SHELL_URL);
	if (cached) {
		event.waitUntil(refreshShell().catch(() => {}));
		return cached;
	}

	// First visit (no cached shell yet): network with a stall timeout.
	const controller = new AbortController();
	const timer = setTimeout(() => controller.abort(), NAV_TIMEOUT_MS);
	try {
		const res = await fetch(request, { signal: controller.signal });
		clearTimeout(timer);
		// Seed the shell cache, but only from a real HTML shell — never an error
		// page or a redirect.
		if (res.ok && (res.headers.get('content-type') || '').includes('text/html')) {
			cache.put(SHELL_URL, res.clone());
		}
		return res;
	} catch {
		clearTimeout(timer);
		return Response.error();
	}
}

async function refreshShell() {
	const res = await fetch(new Request(SHELL_URL, { cache: 'reload' }));
	if (res.ok && (res.headers.get('content-type') || '').includes('text/html')) {
		const cache = await caches.open(SHELL_CACHE);
		await cache.put(SHELL_URL, res.clone());
	}
}

// The stale-boot guard in +layout.svelte asks us to refresh the cached shell
// (and waits for the reply) before reloading — otherwise its reload would be
// served the same stale cached shell it is trying to escape.
self.addEventListener('message', (event) => {
	const data = event.data;
	if (!data || data.type !== 'refresh-shell') return;
	const reply = () => {
		try {
			event.ports?.[0]?.postMessage({ type: 'shell-refreshed' });
		} catch (e) {
			/* port gone — nothing to do */
		}
	};
	event.waitUntil(refreshShell().then(reply, reply));
});

// --- Web Push (automation run notifications) ---------------------------------
// Purely additive: nothing above (precache, PASS_THROUGH, the cache-first
// navigation path) participates in or is affected by these handlers.

// ALWAYS call showNotification, even on a malformed or bodyless push: iOS
// revokes the site's push permission after a couple of "silent" pushes (a push
// event that ends without showing a notification), which would kill the feature
// on exactly the platform that needs it most.
self.addEventListener('push', (event) => {
	let payload = {};
	try {
		payload = event.data?.json() ?? {};
	} catch (e) {
		payload = { body: event.data?.text() ?? '' };
	}

	event.waitUntil(
		self.registration.showNotification(payload.title || 'Open WebUI', {
			body: payload.body || '',
			icon: `${base}/static/favicon.png`,
			badge: `${base}/static/favicon.png`,
			tag: payload.tag || undefined,
			data: { url: payload.url || `${base}/` }
		})
	);
});

self.addEventListener('notificationclick', (event) => {
	event.notification.close();
	const url = event.notification.data?.url || `${base}/`;

	event.waitUntil(
		(async () => {
			// Prefer an existing tab: opening a second window on top of a running
			// app is the classic push-notification annoyance.
			const clients = await self.clients.matchAll({
				type: 'window',
				includeUncontrolled: true
			});
			for (const client of clients) {
				if (new URL(client.url).origin === self.location.origin) {
					await client.focus();
					if ('navigate' in client) {
						await client.navigate(url).catch(() => {});
					}
					return;
				}
			}
			await self.clients.openWindow(url);
		})()
	);
});
