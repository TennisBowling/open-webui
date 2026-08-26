#!/usr/bin/env node
/**
 * Postbuild brotli-11 precompression for immutable JS/CSS.
 *
 * The server otherwise brotli-compresses every static chunk on the fly at quality
 * 4 (and burns that CPU per request on the single worker). Precompressing the
 * content-hashed, immutable JS/CSS to quality-11 `.br` siblings lets the static
 * handler (FrontendStaticFiles._precompressed_response) serve the smaller payload
 * directly — measurable on a cold load over a bad cell link.
 *
 * Scoped strictly to build/_app/immutable/*.{js,css}; the ~50 MB of .map files are
 * skipped (devtools-only, never on the cold path). Idempotent: recompresses a file
 * only when its source is newer than the existing .br. Wired into `postbuild`.
 */
import { readdirSync, statSync, existsSync, readFileSync, writeFileSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { brotliCompressSync, constants } from 'node:zlib';

const __dirname = dirname(fileURLToPath(import.meta.url));
const ROOT = join(__dirname, '..', 'build', '_app', 'immutable');

if (!existsSync(ROOT)) {
	console.warn('[precompress] build/_app/immutable not found — run after build. Skipping.');
	process.exit(0);
}

const EXTS = ['.js', '.css'];
let count = 0;
let inBytes = 0;
let outBytes = 0;

function walk(dir) {
	for (const entry of readdirSync(dir, { withFileTypes: true })) {
		const full = join(dir, entry.name);
		if (entry.isDirectory()) {
			walk(full);
			continue;
		}
		if (!EXTS.some((e) => entry.name.endsWith(e))) continue; // skips .map etc.
		const br = full + '.br';
		const srcStat = statSync(full);
		if (existsSync(br) && statSync(br).mtimeMs >= srcStat.mtimeMs) continue;
		const buf = readFileSync(full);
		const compressed = brotliCompressSync(buf, {
			params: {
				[constants.BROTLI_PARAM_QUALITY]: 11,
				[constants.BROTLI_PARAM_SIZE_HINT]: buf.length
			}
		});
		writeFileSync(br, compressed);
		count++;
		inBytes += buf.length;
		outBytes += compressed.length;
	}
}

walk(ROOT);
const kb = (n) => `${(n / 1024).toFixed(0)} KB`;
console.log(
	count
		? `[precompress] brotli-11: ${count} files, ${kb(inBytes)} → ${kb(outBytes)}`
		: '[precompress] all .br up-to-date.'
);
