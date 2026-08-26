// Adversarial verification of the PREVIEW invariant (FilePreview.svelte /
// OutputFileModal.svelte): "never get stuck on a stale or wrong-file state ...
// across EVERY open/close/reopen/... sequence with NO full-tab reload."
//
// This test compiles a component whose <script> reactive graph is byte-for-byte
// the load-bearing part of FilePreview.svelte (lines 18-20, 32, 47-71) with the
// REAL Svelte 4 compiler, mounts it headless on the REAL svelte/internal runtime,
// and drives the previewFile store.
//
// FINDING (VIOLATION): re-opening the SAME file id while the preview is already
// open — by re-clicking the same OutputFileItem, which calls
// previewFile.set(message.files[i]) with the SAME un-hydrated raw descriptor
// (loadFile never hydrates message.files, only a throwaway store copy) — drops
// the preview to the "No inline preview available" fallback and STICKS there.
//
// Why: `$: item = $previewFile` re-runs (object ref => dirty) so `$: file =
// item?.file ?? null` recomputes to null; but `$: fileId = item?.id ?? file?.id`
// recomputes to the SAME string 'A', and Svelte's $$invalidate gates make_dirty
// on safe_not_equal(old,new) — safe_not_equal('A','A') === false — so fileId is
// NOT marked dirty and the `$: if (fileId) loadFile()` block does NOT re-run.
// Nothing refetches; render falls through to the fallback branch.
//
// Run: node backend/open_webui/test/util/test_adv_preview.mjs   (from repo root,
// or any cwd inside the open-webui project so bare `svelte/internal` resolves).

import { compile } from 'svelte/compiler';
import * as internal from 'svelte/internal';
import { writeFileSync, rmSync } from 'node:fs';
import { join } from 'node:path';

// ---- minimal headless DOM so Svelte can mount ----
function makeNode(tag = 'div') {
	const node = {
		nodeName: tag, childNodes: [], parentNode: null, firstChild: null, style: {},
		appendChild(c) { c.parentNode = node; node.childNodes.push(c); node.firstChild = node.childNodes[0]; return c; },
		insertBefore(c, ref) { c.parentNode = node; const i = ref ? node.childNodes.indexOf(ref) : -1; if (i < 0) node.childNodes.push(c); else node.childNodes.splice(i, 0, c); node.firstChild = node.childNodes[0]; return c; },
		removeChild(c) { const i = node.childNodes.indexOf(c); if (i >= 0) node.childNodes.splice(i, 1); node.firstChild = node.childNodes[0] || null; return c; },
		setAttribute() {}, removeAttribute() {}, addEventListener() {}, removeEventListener() {},
		set textContent(v) { node._t = v; }, get textContent() { return node._t; },
		set nodeValue(v) { node._t = v; }, get nodeValue() { return node._t; },
		set data(v) { node._t = v; }, get data() { return node._t; },
		cloneNode() { return makeNode(tag); }
	};
	return node;
}
globalThis.document = {
	createElement: (t) => makeNode(t),
	createElementNS: (ns, t) => makeNode(t),
	createTextNode: (t) => { const n = makeNode('#text'); n._t = t; return n; },
	createComment: (t) => { const n = makeNode('#comment'); n._t = t; return n; },
	head: makeNode('head')
};

// Reactive graph copied from the PATCHED FilePreview.svelte (load-bearing lines).
const SRC = `
<script>
  export let previewFile;     // store, mirrors $lib/stores previewFile
  export let getFileById;     // async (id) => record | null  (mirrors apis/files)
  export let onRender;        // test probe
  let item = null;
  let loading = false;
  $: item = $previewFile;
  $: file = item?.file ?? null;
  $: fileId = item?.id ?? file?.id;
  $: textPreview = (file?.data?.content ?? '').trim();
  const loadFile = async () => {
    const targetId = fileId;
    if (!targetId) return;
    if (item?.file && item.file.id === targetId) return;   // already hydrated for this id
    loading = true;
    const res = await getFileById(targetId).catch(() => null);
    // apply only if it's the record we asked for AND still the shown id
    if (res && res.id === targetId && (item?.id ?? item?.file?.id) === targetId) {
      item = { ...item, file: res };
      previewFile.set(item);
    }
    loading = false;
  };
  $: item, loadFile();                                     // re-run on item identity
  // render-decision probe mirroring the template's branch order:
  $: {
    const ct = (file?.meta?.content_type ?? item?.content_type ?? '').toLowerCase();
    const name = item?.name ?? file?.meta?.name ?? file?.filename ?? 'file';
    const isImage = ct.startsWith('image/');
    const isPdf = ct === 'application/pdf' || name.toLowerCase().endsWith('.pdf');
    let state;
    if (loading && !file) state = 'spinner';
    else if (isPdf) state = 'frame';
    else if (isImage) state = 'image';
    else if (textPreview) state = 'text';
    else state = 'fallback';
    if (onRender) onRender({ state, text: textPreview, fileId, hasFile: !!file, loading });
  }
</script>
<div>{fileId}</div>
`;

const { js } = compile(SRC, { generate: 'dom', name: 'PreviewCore' });
const tmp = join(process.cwd(), '_adv_preview_cmp.mjs');
writeFileSync(tmp, js.code);

let failures = 0;
function check(cond, msg) {
	if (cond) console.log('ok: ' + msg);
	else { console.error('FAIL: ' + msg); failures++; }
}

function writable(val) {
	const subs = new Set();
	const s = {
		subscribe(f) { subs.add(f); f(val); return () => subs.delete(f); },
		set(v) { val = v; subs.forEach((f) => f(val)); },
		update(fn) { s.set(fn(val)); }
	};
	return s;
}
const settle = async () => {
	for (let i = 0; i < 12; i++) { await Promise.resolve(); await new Promise((r) => setTimeout(r, 0)); try { internal.flush(); } catch {} }
};

try {
	const { default: PreviewCore } = await import('file://' + tmp + '?t=' + Date.now());

	const backend = {
		A: { id: 'A', meta: { name: 'a.txt', content_type: 'text/plain' }, data: { content: 'AAA' } },
		C: { id: 'C', meta: { name: 'c.txt', content_type: 'text/plain' }, data: { content: 'CCC' } }
		// 'B' intentionally absent -> getFileById('B') => null (404 path)
	};
	const rawA = () => ({ id: 'A', name: 'a.txt', content_type: 'text/plain' });
	const rawB = () => ({ id: 'B', name: 'b.txt', content_type: 'text/plain' });

	// =========================================================================
	// PRIMARY FINDING: same-id re-open while already open loses content + sticks.
	// =========================================================================
	{
		const store = writable(null);
		let r = null;
		new PreviewCore({ target: makeNode('body'), props: { previewFile: store, getFileById: async (id) => backend[id] ?? null, onRender: (x) => (r = { ...x }) } });

		const aItem = rawA(); // the SAME message.files[i] object the list holds
		store.set(aItem);
		await settle();
		check(r && r.state === 'text' && r.text === 'AAA', 'PRIMARY: first open of A renders content');

		// User re-clicks the SAME OutputFileItem -> previewFile.set(SAME raw item).
		// message.files is never hydrated, so this is still un-hydrated (.file absent).
		store.set(aItem);
		await settle();
		console.log('   after same-id re-open:', JSON.stringify(r));
		// EXPECTED-IF-HOLDS: still 'text'/'AAA'. ACTUAL (violation): 'fallback'.
		check(
			r && r.state === 'text' && r.text === 'AAA',
			'PRIMARY: same-id re-open keeps content (must NOT drop to fallback w/o reload)'
		);

		// And it STICKS: re-clicking A again does not recover (fileId never changes).
		store.set(rawA());
		await settle();
		check(
			r && r.state === 'text' && r.text === 'AAA',
			'PRIMARY: repeated same-id re-clicks self-heal (stuck state must not persist)'
		);

		// It only recovers by opening a DIFFERENT file (proving the stick is id-gated).
		store.set({ id: 'C', name: 'c.txt', content_type: 'text/plain' });
		await settle();
		check(r && r.state === 'text' && r.text === 'CCC', 'PRIMARY: opening a different file recovers');
	}

	// =========================================================================
	// Sanity sequences that the patch DOES handle (to keep the finding honest).
	// =========================================================================
	// open A -> open B(dead) -> reopen A -> reopen B : each transition changes
	// fileId, so loadFile re-runs; these are fine.
	{
		const store = writable(null);
		let r = null;
		new PreviewCore({ target: makeNode('body'), props: { previewFile: store, getFileById: async (id) => backend[id] ?? null, onRender: (x) => (r = { ...x }) } });
		store.set(rawA()); await settle();
		check(r.state === 'text' && r.text === 'AAA', 'SEQ: A loads');
		store.set(rawB()); await settle();
		check(r.state === 'fallback' && r.loading === false, 'SEQ: B(dead) -> fallback, not stuck spinner');
		store.set(rawA()); await settle();
		check(r.state === 'text' && r.text === 'AAA', 'SEQ: reopen A self-heals (fileId B->A changed)');
		store.set(rawB()); await settle();
		check(r.state === 'fallback', 'SEQ: reopen B fallback again');
	}

	// in-flight swap A->C: stale A result must not clobber C.
	// SECOND FINDING: the apply guard `(item?.id ?? item?.file?.id) === fileId`
	// (line 62) compares the LIVE reactive item/fileId, NOT the id that was
	// actually fetched. loadFile is a single instance-scope closure, so `fileId`
	// and `item` read their CURRENT values at await-resolve time. After swapping
	// to C, item.id===fileId==='C' is trivially true, so the stale A fetch's
	// result (res = A's record) is written onto item C: the panel then shows file
	// C's name/metadata with file A's CONTENT -> a wrong-file render.
	{
		const store = writable(null);
		let r = null;
		const gate = { A: null, C: null };
		const getFileById = (id) => new Promise((resolve) => { gate[id] = () => resolve(backend[id] ?? null); });
		new PreviewCore({ target: makeNode('body'), props: { previewFile: store, getFileById, onRender: (x) => (r = { ...x }) } });
		store.set(rawA()); await settle();          // A fetch in flight (gate.A pending)
		store.set({ id: 'C', name: 'c.txt', content_type: 'text/plain' }); await settle(); // C in flight
		gate.A && gate.A(); await settle();          // resolve stale A while showing C
		console.log('   after stale-A resolve while showing C:', JSON.stringify(r));
		// VIOLATION: r.fileId === 'C' but r.text === 'AAA' (A's content under C).
		check(
			!(r.fileId === 'C' && r.text === 'AAA'),
			'SWAP: stale A result must NOT be applied as content for the now-shown C (wrong-file render)'
		);
		gate.C && gate.C(); await settle();
		check(r.state === 'text' && r.text === 'CCC', 'SWAP: C eventually shows its own content');
	}
} finally {
	rmSync(tmp, { force: true });
}

console.log('\n' + (failures ? `VIOLATIONS FOUND: ${failures} assertion(s) failed` : 'no violations'));
process.exitCode = failures ? 1 : 0;
