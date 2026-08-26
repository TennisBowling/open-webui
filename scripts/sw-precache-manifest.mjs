#!/usr/bin/env node
/**
 * Postbuild: emit build/sw-precache.json — the exact cold-load closure for "/"
 * (entry + the root route's layout/leaf node chain, transitively over STATIC
 * imports only) plus the eager Latin fonts. The service worker precaches this set
 * into the durable Cache API at install, so the ~1.1 MB cold load survives mobile
 * HTTP-cache eviction from the very first visit instead of relying on the volatile
 * browser disk cache and lazy runtime caching.
 *
 * Robust by design: if the SvelteKit manifests can't be parsed (format drift), it
 * falls back to the bootstrap chunks referenced by build/index.html + the fonts,
 * so the SW always gets a valid list. Lazy/dynamic-import chunks (mermaid, pdf,
 * onnx, pyodide, …) are intentionally excluded — they're not on the cold path.
 */
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.join(__dirname, '..');
const BUILD = path.join(ROOT, 'build');
const CLIENT_MANIFEST = path.join(ROOT, '.svelte-kit/output/client/.vite/manifest.json');
const SERVER_MANIFEST = path.join(ROOT, '.svelte-kit/output/server/manifest-full.js');

const EAGER_FONTS = [
	'/assets/fonts/Inter-Variable.latin.woff2',
	'/assets/fonts/Archivo-Variable.latin.woff2',
	'/assets/fonts/InstrumentSerif-Regular.latin.woff2'
];

// Brand / PWA icons referenced by src/app.html and the web manifest. These live
// under a stable (non-hashed) URL and are on the very first paint (favicon +
// splash), so precaching them into the durable Cache API keeps the shell fully
// self-branded offline / on the first flaky load.
const BRAND_ICONS = [
	'/static/favicon.png',
	'/static/favicon-96x96.png',
	'/static/favicon.svg',
	'/static/favicon.ico',
	'/static/apple-touch-icon.png',
	'/static/web-app-manifest-192x192.png',
	'/static/web-app-manifest-512x512.png',
	'/static/splash.png',
	'/static/splash-dark.png'
];

function rootRouteNodeIndices() {
	const s = fs.readFileSync(SERVER_MANIFEST, 'utf8');
	const re =
		/id:\s*"([^"]*)"[\s\S]*?pattern:\s*(\/(?:\\.|[^/\n])*\/)[\s\S]*?page:\s*\{\s*layouts:\s*\[([0-9,\s]*)\][\s\S]*?leaf:\s*(\d+)\s*\}/g;
	let m;
	while ((m = re.exec(s))) {
		const [, , patternSrc, layoutsRaw, leaf] = m;
		const last = patternSrc.lastIndexOf('/');
		let pattern;
		try {
			pattern = new RegExp(patternSrc.slice(1, last), patternSrc.slice(last + 1));
		} catch {
			continue;
		}
		if (pattern.test('/')) {
			const layouts = layoutsRaw
				.split(',')
				.map((x) => x.trim())
				.filter(Boolean)
				.map(Number);
			return [...layouts, Number(leaf)];
		}
	}
	return null;
}

function closureFromManifest() {
	const M = JSON.parse(fs.readFileSync(CLIENT_MANIFEST, 'utf8'));
	const nodes = rootRouteNodeIndices();
	if (!nodes) throw new Error('could not resolve "/" route node chain');
	const startKeys = [
		'.svelte-kit/generated/client-optimized/app.js',
		'node_modules/@sveltejs/kit/src/runtime/client/entry.js',
		...nodes.map((n) => `.svelte-kit/generated/client-optimized/nodes/${n}.js`)
	].filter((k) => M[k]);

	const seen = new Set();
	const files = new Set();
	const q = [...startKeys];
	while (q.length) {
		const k = q.shift();
		if (seen.has(k)) continue;
		seen.add(k);
		const e = M[k];
		if (!e) continue;
		if (e.file) files.add('/' + e.file);
		(e.css || []).forEach((c) => files.add('/' + c));
		(e.imports || []).forEach((i) => !seen.has(i) && q.push(i)); // STATIC imports only
	}
	return files;
}

function bootstrapFromIndexHtml() {
	const html = fs.readFileSync(path.join(BUILD, 'index.html'), 'utf8');
	const files = new Set();
	for (const m of html.matchAll(/\/_app\/immutable\/[^"')\s]+\.(?:js|css)/g)) files.add(m[0]);
	return files;
}

let urls;
try {
	urls = closureFromManifest();
	console.log('[sw-precache] computed cold-load closure from SvelteKit manifests');
} catch (e) {
	console.warn(`[sw-precache] manifest closure failed (${e.message}); falling back to index.html bootstrap`);
	urls = bootstrapFromIndexHtml();
}
EAGER_FONTS.forEach((f) => urls.add(f));
// Only pin brand icons that actually shipped into build/ (avoids a precache entry
// that would 404 and needlessly reject cache.add at install time).
BRAND_ICONS.filter((f) => fs.existsSync(path.join(BUILD, f.replace(/^\//, '')))).forEach((f) =>
	urls.add(f)
);

const list = [...urls].sort();
fs.writeFileSync(path.join(BUILD, 'sw-precache.json'), JSON.stringify(list));

// Report the wire weight (sum of .br where present).
let bytes = 0;
for (const u of list) {
	const p = path.join(BUILD, u.replace(/^\//, ''));
	const br = p + '.br';
	if (fs.existsSync(br)) bytes += fs.statSync(br).size;
	else if (fs.existsSync(p)) bytes += fs.statSync(p).size;
}
console.log(`[sw-precache] build/sw-precache.json: ${list.length} entries, ~${(bytes / 1024).toFixed(0)} KB to precache`);
