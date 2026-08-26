// ADV3 — NO-UNRELATED-REGRESSION invariant (frontend half).
//
// Owns: the frontend mergeMessageFiles / fileContentKey helpers in Chat.svelte
// (which replaced the THREE inline dedup loops) must NOT have broken
// NON-container file handling, AND must stay in PARITY with the OLD inline
// dedup key so reloaded lists match. Also checks the ResponseMessage keyed-each
// null-key collision concern.
//
// This test EXTRACTS the two helpers verbatim from the REAL Chat.svelte source
// (so it can never drift from what ships) and runs them in plain node. No svelte
// runtime needed — these are pure functions.
//
// Run: node backend/open_webui/test/util/test_adv3_frontend_parity.mjs

import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const __dirname = dirname(fileURLToPath(import.meta.url));
const REPO = join(__dirname, '..', '..', '..', '..'); // backend/open_webui/test/util -> repo root
const CHAT = join(REPO, 'src', 'lib', 'components', 'chat', 'Chat.svelte');
const src = readFileSync(CHAT, 'utf8');

// Hard guard: the source must contain NO literal NUL byte (only the \u0000
// escape). A stray NUL would corrupt the bundle.
if (src.indexOf('\u0000') !== -1) {
	throw new Error('Chat.svelte contains a LITERAL NUL byte — must use the \\u0000 escape');
}

// ---- extract the two helpers VERBATIM from the real Chat.svelte source ----
// The source has TS annotations; pull the exact function block and strip the
// type annotations so plain node can evaluate it. This can never drift from
// what ships because it reads the shipping file at runtime.
function loadHelpers() {
	const startKey = src.indexOf('const fileContentKey =');
	const startMerge = src.indexOf('const mergeMessageFiles =');
	const endMerge = src.indexOf('const openGeneratedFilePreview', startMerge);
	if (startKey < 0 || startMerge < 0 || endMerge < 0)
		throw new Error('helper anchors not found in Chat.svelte');
	let block = src.slice(startKey, endMerge);
	// strip TS type annotations the helpers use: ": any", ": string | null",
	// "<string>", and param ": any[]".
	block = block
		.replace(/: any\[\]/g, '')
		.replace(/: string \| null/g, '')
		.replace(/: any/g, '')
		.replace(/new Set<string>\(\)/g, 'new Set()');
	// eslint-disable-next-line no-new-func
	const factory = new Function(`${block}; return { fileContentKey, mergeMessageFiles };`);
	return factory();
}

const { fileContentKey: contentKey, mergeMessageFiles: merge } = loadHelpers();

// ---- the OLD inline dedup key (pre-patch), for parity comparison ----
// socket {type:"files"} & event_files sites used: id ?? url ?? content ?? JSON.stringify
// tool_call:result site used: url ?? content ?? JSON.stringify  (NO id first)
function oldMergeWithIdFallback(existing, incoming) {
	const seen = new Set((existing ?? []).map((f) => f?.id ?? f?.url ?? f?.content ?? JSON.stringify(f)));
	const next = [...(existing ?? [])];
	for (const f of incoming ?? []) {
		const k = f?.id ?? f?.url ?? f?.content ?? JSON.stringify(f);
		if (seen.has(k)) continue;
		seen.add(k);
		next.push(f);
	}
	return next;
}

let failed = 0;
function check(name, fn) {
	try {
		fn();
		console.log(`PASS ${name}`);
	} catch (e) {
		failed++;
		console.log(`FAIL ${name}: ${e && e.message ? e.message : e}`);
	}
}
function assert(cond, msg) {
	if (!cond) throw new Error(msg || 'assertion failed');
}
function eq(a, b, msg) {
	assert(JSON.stringify(a) === JSON.stringify(b), `${msg || ''} :: ${JSON.stringify(a)} !== ${JSON.stringify(b)}`);
}

// REAL non-container shapes (mirror middleware.py).
const mcpImage = (url) => ({ type: 'image', url });
const openapiDatauri = (content) => ({ type: 'data', content });
const userUpload = (id, name = 'r.pdf') => ({ type: 'file', id, url: `/api/v1/files/${id}`, name });
const container = (id, ws, sha, name = 'out.csv') => ({
	type: 'file',
	id,
	name,
	container_workspace: { workspace_path: ws, sha256: sha }
});

// --- 0: non-container files never produce a content key ---------------------
check('noncontainer_content_key_null', () => {
	assert(contentKey(mcpImage('https://cdn/a.png')) === null, 'mcp image key must be null');
	assert(contentKey(openapiDatauri('data:text/csv;base64,YWJj')) === null, 'datauri key null');
	assert(contentKey(userUpload('u1')) === null, 'upload key null');
	// container produces the cw\u0000..\u0000.. key
	assert(
		contentKey(container('c1', 'outputs/out.csv', 'a'.repeat(64))) ===
			`cw\u0000outputs/out.csv\u0000${'a'.repeat(64)}`,
		'container key shape'
	);
});

// --- 1: distinct accumulate (parity with backend) ---------------------------
check('distinct_accumulate', () => {
	const out = merge([], [mcpImage('https://cdn/a.png'), mcpImage('https://cdn/b.png')]);
	eq(out.map((f) => f.url), ['https://cdn/a.png', 'https://cdn/b.png'], 'two distinct images');
	const out2 = merge([], [userUpload('u1'), userUpload('u2')]);
	eq(out2.map((f) => f.id), ['u1', 'u2'], 'two distinct uploads');
});

// --- 2: same-id dedups ------------------------------------------------------
check('same_id_dedups', () => {
	const u = userUpload('u1');
	const out = merge([u], [{ ...u }]);
	eq(out.length, 1, 'same id deduped');
});

// --- 3: no-id file uses url fallback (frontend-only behavior, documented) ----
//   The frontend id-fallback chain is id ?? url ?? content ?? JSON.stringify.
//   So two no-id SAME-url images dedup on the FRONTEND (by url). This is the
//   SAME as the old inline loop — parity preserved, NOT a new regression. (The
//   backend keeps both because it has no url fallback; the persisted list is
//   the authority and on reload the frontend re-dedups by url to the same view.)
check('noid_sameurl_dedups_by_url_parity', () => {
	const img = mcpImage('https://cdn/same.png');
	const out = merge([], [img, { ...img }]);
	eq(out.length, 1, 'frontend dedups no-id same-url by url fallback');
	// parity: old inline loop did the same
	const old = oldMergeWithIdFallback([], [img, { ...img }]);
	eq(out.length, old.length, 'matches old inline dedup count');
});

// --- 4: no-id DISTINCT-url images both kept ---------------------------------
check('noid_distinct_url_kept', () => {
	const out = merge([], [mcpImage('https://cdn/a.png'), mcpImage('https://cdn/b.png')]);
	eq(out.length, 2, 'distinct-url no-id images both kept');
});

// --- 5: no-id no-url data-uri uses content fallback -------------------------
check('noid_nourl_datauri_content_fallback', () => {
	const a = openapiDatauri('data:text/csv;base64,AAAA');
	const b = openapiDatauri('data:text/csv;base64,BBBB');
	const out = merge([], [a, b]);
	eq(out.length, 2, 'distinct datauri content both kept');
	// same content dedups (content fallback)
	const out2 = merge([], [a, { ...a }]);
	eq(out2.length, 1, 'same datauri content deduped by content fallback');
});

// --- 6: container content-key dedup with fresh id (the core fix) ------------
check('container_content_dedup_fresh_id', () => {
	const sha = 'a'.repeat(64);
	const v1 = container('file-A', 'outputs/r.csv', sha);
	const v2 = container('file-B', 'outputs/r.csv', sha); // re-import, new id
	const out = merge([v1], [v2]);
	eq(out.length, 1, 'container same content deduped despite fresh id');
	eq(out[0].id, 'file-A', 'existing kept');
});

// --- 7: PARITY — for the FIRST two sites (socket/event_files), the new merge
//   must produce the SAME result as the old id-first inline loop for any
//   non-container input (the content-key path only ADDS dedups for container
//   files, which the old loop also deduped by id when id was stable). ----------
check('parity_with_old_inline_for_noncontainer', () => {
	const existing = [mcpImage('https://cdn/x.png'), userUpload('u9')];
	const incoming = [
		mcpImage('https://cdn/y.png'),
		openapiDatauri('data:application/pdf;base64,ZZ'),
		userUpload('u9'), // id repeat
		userUpload('u10')
	];
	const neu = merge(existing, incoming);
	const old = oldMergeWithIdFallback(existing, incoming);
	eq(
		neu.map((f) => f.id ?? f.url ?? f.content),
		old.map((f) => f.id ?? f.url ?? f.content),
		'new merge identical to old inline for non-container'
	);
});

// --- 8: container files are STRICTLY better than old (old kept fresh-id dup,
//   new collapses by content). Documents the intended improvement. ------------
check('container_improvement_over_old', () => {
	const sha = 'b'.repeat(64);
	const v1 = container('id-1', 'outputs/p.png', sha);
	const v2 = container('id-2', 'outputs/p.png', sha); // fresh id, same content
	const neu = merge([v1], [v2]);
	const old = oldMergeWithIdFallback([v1], [v2]);
	eq(neu.length, 1, 'new collapses fresh-id container dup');
	eq(old.length, 2, 'old (id-only) kept the dup — the bug the fix closes');
});

// --- 9: ResponseMessage keyed-each: (file?.id ?? file?.url ?? file). Verify no
//   NULL-key collision risk for the real shapes. A user upload w/o id keys on
//   url; a no-url-no-id item keys on the object itself (unique per object). ----
check('keyed_each_no_null_key_collision', () => {
	const keyOf = (file) => file?.id ?? file?.url ?? file;
	const files = [
		userUpload('u1'), // keys on id
		mcpImage('https://cdn/a.png'), // no id -> keys on url
		openapiDatauri('data:x') // no id, no url -> keys on the object ref
	];
	const keys = files.map(keyOf);
	// id, url, and the object itself — all distinct, none undefined/null.
	assert(keys[0] === 'u1', 'upload keys on id');
	assert(keys[1] === 'https://cdn/a.png', 'image keys on url');
	assert(keys[2] === files[2], 'no-id-no-url keys on object ref (unique)');
	// no two keys are === (object refs are unique; id/url distinct)
	assert(new Set(keys).size === 3, 'all three keys distinct');
	// Even TWO no-id-no-url items get distinct keys (object identity), so the
	// each block never collapses two real cards under one key.
	const a = openapiDatauri('data:x');
	const b = openapiDatauri('data:x'); // same content, different object
	assert(keyOf(a) !== keyOf(b), 'two same-content no-id-no-url items key distinctly');
});

// --- 10: the cw\u0000 key uses the ESCAPE, not a literal NUL, AND a plain url
//   that equals the key string can't collide (non-container -> content_key null).
check('content_key_url_no_collision', () => {
	const sha = 'a'.repeat(64);
	const weird = `cw\u0000outputs/out.csv\u0000${sha}`;
	const img = mcpImage(weird); // url == the cw key string, but NO container_workspace
	const cont = container('fc', 'outputs/out.csv', sha);
	assert(contentKey(cont) === weird, 'container key equals the weird url string');
	assert(contentKey(img) === null, 'non-container url never becomes a content key');
	const out = merge([], [img, cont]);
	eq(out.length, 2, 'no collapse between a plain url and an identical content key');
});

if (failed) {
	console.error(`\n${failed} FRONTEND PARITY CHECK(S) FAILED`);
	process.exit(1);
}
console.log('\nall frontend parity checks passed');
