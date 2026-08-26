// ROUND-2 adversarial verification of the PREVIEW-REACTIVE invariant on the
// PATCHED FilePreview.svelte / OutputFileModal.svelte.
//
// Models the load-bearing reactive graph of BOTH patched components with the
// REAL svelte compiler + runtime, mounts headless, drives the scenarios, and
// COUNTS getFileById invocations to catch a refetch loop / storm / stale apply.
//
// FilePreview graph (patched lines 15-20,32,47-75):
//   $: item = $previewFile;  $: file = item?.file ?? null;  $: fileId = item?.id ?? file?.id;
//   loadFile: targetId=fileId; if(!targetId)return; if(item?.file&&item.file.id===targetId)return;
//             res=await get(targetId); if(res&&res.id===targetId&&(item?.id??item?.file?.id)===targetId){
//               item={...item,file:res}; previewFile.set(item); }
//   $: item, loadFile();
//
// OutputFileModal graph (patched lines 15-21,35,38-61):
//   export let item; export let show=false;
//   $: file=item?.file??null; $: fileId=item?.id??file?.id;
//   loadFile: targetId=fileId; if(!show||!targetId)return; if(item?.file&&item.file.id===targetId)return;
//             res=await get(targetId); if(res&&res.id===targetId&&(item?.id??item?.file?.id)===targetId){
//               item={...item,file:res}; }   // NOTE: no previewFile.set
//   $: show, item, loadFile();
//
// Run: node backend/open_webui/test/util/test_adv2_preview.mjs

import { compile } from 'svelte/compiler';
import * as internal from 'svelte/internal';
import { writeFileSync, rmSync } from 'node:fs';
import { join } from 'node:path';

// ---- minimal headless DOM ----
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

// ----- FilePreview core (store-driven, with previewFile.set inside loadFile) -----
const FP_SRC = `
<script>
  export let previewFile;
  export let getFileById;
  export let onRender;
  let item = null;
  let loading = false;
  $: item = $previewFile;
  $: file = item?.file ?? null;
  $: fileId = item?.id ?? file?.id;
  $: contentType = (file?.meta?.content_type ?? item?.content_type ?? '').toLowerCase();
  $: name = item?.name ?? file?.meta?.name ?? file?.filename ?? 'file';
  $: textPreview = (file?.data?.content ?? '').trim();
  const loadFile = async () => {
    const targetId = fileId;
    if (!targetId) return;
    if (item?.file && item.file.id === targetId) return;
    loading = true;
    const res = await getFileById(targetId).catch(() => null);
    if (res && res.id === targetId && (item?.id ?? item?.file?.id) === targetId) {
      item = { ...item, file: res };
      previewFile.set(item);
    }
    loading = false;
  };
  $: item, loadFile();
  $: {
    const isImage = contentType.startsWith('image/');
    const isPdf = contentType === 'application/pdf' || name.toLowerCase().endsWith('.pdf');
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

// ----- OutputFileModal core (props item+show, NO previewFile.set) -----
const OFM_SRC = `
<script>
  export let item;
  export let show = false;
  export let getFileById;
  export let onRender;
  let loading = false;
  $: file = item?.file ?? null;
  $: fileId = item?.id ?? file?.id;
  $: contentType = (file?.meta?.content_type ?? item?.content_type ?? '').toLowerCase();
  $: name = item?.name ?? file?.meta?.name ?? file?.filename ?? 'file';
  $: textPreview = (file?.data?.content ?? '').trim();
  const loadFile = async () => {
    const targetId = fileId;
    if (!show || !targetId) return;
    if (item?.file && item.file.id === targetId) return;
    loading = true;
    const res = await getFileById(targetId).catch(() => null);
    if (res && res.id === targetId && (item?.id ?? item?.file?.id) === targetId) {
      item = { ...item, file: res };
    }
    loading = false;
  };
  $: show, item, loadFile();
  $: {
    const isImage = contentType.startsWith('image/');
    const isPdf = contentType === 'application/pdf' || name.toLowerCase().endsWith('.pdf');
    let state;
    if (loading) state = 'spinner';
    else if (isPdf) state = 'frame';
    else if (isImage) state = 'image';
    else if (textPreview) state = 'text';
    else state = 'fallback';
    if (onRender) onRender({ state, text: textPreview, fileId, hasFile: !!file, loading, show });
  }
</script>
<div>{fileId}</div>
`;

const fpTmp = join(process.cwd(), '_adv2_fp_cmp.mjs');
const ofmTmp = join(process.cwd(), '_adv2_ofm_cmp.mjs');
writeFileSync(fpTmp, compile(FP_SRC, { generate: 'dom', name: 'FPCore' }).js.code);
writeFileSync(ofmTmp, compile(OFM_SRC, { generate: 'dom', name: 'OFMCore' }).js.code);

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
	for (let i = 0; i < 16; i++) { await Promise.resolve(); await new Promise((r) => setTimeout(r, 0)); try { internal.flush(); } catch {} }
};

const backend = {
	A: { id: 'A', meta: { name: 'a.txt', content_type: 'text/plain' }, data: { content: 'AAA' } },
	C: { id: 'C', meta: { name: 'c.txt', content_type: 'text/plain' }, data: { content: 'CCC' } }
	// 'B' absent => 404 path
};
const rawA = () => ({ id: 'A', name: 'a.txt', content_type: 'text/plain' });
const rawB = () => ({ id: 'B', name: 'b.txt', content_type: 'text/plain' });
const rawC = () => ({ id: 'C', name: 'c.txt', content_type: 'text/plain' });

try {
	const { default: FPCore } = await import('file://' + fpTmp + '?t=' + Date.now());
	const { default: OFMCore } = await import('file://' + ofmTmp + '?t=' + Date.now());

	// =====================================================================
	// FILEPREVIEW
	// =====================================================================
	console.log('\n=== FilePreview ===');

	// --- FP1: no refetch loop. previewFile.set(item) inside loadFile re-fires
	//     $: item=$previewFile -> $: item,loadFile(); the early-return must stop it.
	{
		let calls = 0;
		const store = writable(null);
		let r = null;
		new FPCore({ target: makeNode('body'), props: {
			previewFile: store,
			getFileById: async (id) => { calls++; return backend[id] ?? null; },
			onRender: (x) => (r = { ...x })
		} });
		store.set(rawA());
		await settle();
		check(r && r.state === 'text' && r.text === 'AAA', 'FP1: first open renders content');
		check(calls === 1, `FP1: exactly ONE fetch for one open (no loop) [calls=${calls}]`);
	}

	// --- FP2: same-id re-open refetches once and keeps content (V1 stays fixed).
	{
		let calls = 0;
		const store = writable(null);
		let r = null;
		new FPCore({ target: makeNode('body'), props: {
			previewFile: store,
			getFileById: async (id) => { calls++; return backend[id] ?? null; },
			onRender: (x) => (r = { ...x })
		} });
		store.set(rawA()); await settle();
		check(calls === 1, `FP2: open A -> 1 fetch [calls=${calls}]`);
		store.set(rawA()); await settle();   // re-click SAME id, raw descriptor
		check(r && r.state === 'text' && r.text === 'AAA', 'FP2/V1: same-id re-open keeps content (not fallback)');
		check(calls === 2, `FP2: same-id re-open refetches exactly once more [calls=${calls}]`);
		store.set(rawA()); await settle();   // again
		check(r && r.state === 'text' && r.text === 'AAA', 'FP2/V1: third re-click still content');
		check(calls === 3, `FP2: third re-click -> one more fetch [calls=${calls}]`);
	}

	// --- FP3: close (previewFile=null) early-returns; reopen same id refetches once.
	{
		let calls = 0;
		const store = writable(null);
		let r = null;
		new FPCore({ target: makeNode('body'), props: {
			previewFile: store,
			getFileById: async (id) => { calls++; return backend[id] ?? null; },
			onRender: (x) => (r = { ...x })
		} });
		store.set(rawA()); await settle();
		const afterOpen = calls;
		store.set(null); await settle();      // close()
		check(calls === afterOpen, `FP3: close triggers NO fetch [calls=${calls}]`);
		check(r && (r.fileId === undefined || r.fileId === null), 'FP3: close clears fileId');
		store.set(rawA()); await settle();    // reopen same id
		check(r && r.state === 'text' && r.text === 'AAA', 'FP3: reopen-after-close renders content');
		check(calls === afterOpen + 1, `FP3: reopen-after-close = exactly one more fetch [calls=${calls}]`);
	}

	// --- FP4 (V2): in-flight swap A->C; stale A result must NOT clobber C.
	{
		let calls = 0;
		const gate = {};
		const store = writable(null);
		let r = null;
		const getFileById = (id) => { calls++; return new Promise((resolve) => { gate[id] = () => resolve(backend[id] ?? null); }); };
		new FPCore({ target: makeNode('body'), props: { previewFile: store, getFileById, onRender: (x) => (r = { ...x }) } });
		store.set(rawA()); await settle();   // A fetch in flight
		store.set(rawC()); await settle();   // switch to C; C fetch in flight
		gate.A && gate.A(); await settle();  // resolve stale A while showing C
		check(!(r.fileId === 'C' && r.text === 'AAA'), 'FP4/V2: stale A result NOT applied onto C (no wrong-file)');
		gate.C && gate.C(); await settle();
		check(r && r.state === 'text' && r.text === 'CCC', 'FP4/V2: C eventually shows its own content');
	}

	// --- FP5: rapid triple-click same item (overlapping fetches), gated resolves.
	{
		let calls = 0;
		const pending = [];
		const store = writable(null);
		let r = null;
		const getFileById = (id) => { calls++; return new Promise((resolve) => pending.push(() => resolve(backend[id] ?? null))); };
		new FPCore({ target: makeNode('body'), props: { previewFile: store, getFileById, onRender: (x) => (r = { ...x }) } });
		store.set(rawA()); await settle();
		store.set(rawA()); await settle();
		store.set(rawA()); await settle();
		// resolve all in order
		while (pending.length) { pending.shift()(); await settle(); }
		check(r && r.state === 'text' && r.text === 'AAA', 'FP5: rapid triple-click same item -> correct content');
		check(r.fileId === 'A', 'FP5: final fileId is A');
	}

	// --- FP6: A->B->A fast with overlapping fetches; final must be A's content.
	{
		const gate = {};
		const store = writable(null);
		let r = null;
		const getFileById = (id) => new Promise((resolve) => { (gate[id] = gate[id] || []).push(() => resolve(backend[id] ?? null)); });
		new FPCore({ target: makeNode('body'), props: { previewFile: store, getFileById, onRender: (x) => (r = { ...x }) } });
		store.set(rawA()); await settle();
		store.set(rawB()); await settle();
		store.set(rawA()); await settle();
		// resolve out of order: B(dead) first, then both A fetches
		(gate.B || []).forEach((g) => g()); await settle();
		(gate.A || []).forEach((g) => g()); await settle();
		check(r && r.fileId === 'A' && r.text === 'AAA', 'FP6: A->B->A overlapping -> final content is A (no wrong-file)');
	}

	// =====================================================================
	// OUTPUTFILEMODAL  ($: show, item, loadFile())
	// =====================================================================
	console.log('\n=== OutputFileModal ===');

	// --- OFM1: open (show=true) -> 1 fetch -> content; no loop (no previewFile.set).
	{
		let calls = 0;
		let r = null;
		const cmp = new OFMCore({ target: makeNode('body'), props: {
			item: rawA(), show: false,
			getFileById: async (id) => { calls++; return backend[id] ?? null; },
			onRender: (x) => (r = { ...x })
		} });
		await settle();
		check(calls === 0, `OFM1: show=false -> NO fetch [calls=${calls}]`);
		cmp.$set({ show: true }); await settle();
		check(r && r.state === 'text' && r.text === 'AAA', 'OFM1: open renders content');
		check(calls === 1, `OFM1: open = exactly one fetch [calls=${calls}]`);
	}

	// --- OFM2: open/close(show=false)/reopen-same-item. After hydration item has
	//     .file so reopen must NOT refetch (early-return holds). Content correct.
	{
		let calls = 0;
		let r = null;
		const cmp = new OFMCore({ target: makeNode('body'), props: {
			item: rawA(), show: true,
			getFileById: async (id) => { calls++; return backend[id] ?? null; },
			onRender: (x) => (r = { ...x })
		} });
		await settle();
		check(calls === 1 && r.text === 'AAA', `OFM2: open A -> 1 fetch + content [calls=${calls}]`);
		cmp.$set({ show: false }); await settle();    // close
		check(calls === 1, `OFM2: close -> no fetch [calls=${calls}]`);
		cmp.$set({ show: true }); await settle();      // reopen SAME hydrated item object
		check(r && r.state === 'text' && r.text === 'AAA', 'OFM2: reopen-same-hydrated-item keeps content');
		check(calls === 1, `OFM2: reopen hydrated item -> NO refetch (early-return) [calls=${calls}]`);
	}

	// --- OFM2b: reopen with a FRESH RAW descriptor (re-click rebuilds the item)
	//     must refetch once (the realistic re-click path), content correct.
	{
		let calls = 0;
		let r = null;
		const cmp = new OFMCore({ target: makeNode('body'), props: {
			item: rawA(), show: true,
			getFileById: async (id) => { calls++; return backend[id] ?? null; },
			onRender: (x) => (r = { ...x })
		} });
		await settle();
		check(calls === 1, `OFM2b: open -> 1 fetch [calls=${calls}]`);
		cmp.$set({ show: false }); await settle();
		cmp.$set({ item: rawA(), show: true }); await settle();   // re-click: new raw item
		check(r && r.text === 'AAA', 'OFM2b: reopen-raw keeps content');
		check(calls === 2, `OFM2b: reopen-raw refetches once [calls=${calls}]`);
	}

	// --- OFM3: open different item while shown -> refetch new, correct content.
	{
		let calls = 0;
		let r = null;
		const cmp = new OFMCore({ target: makeNode('body'), props: {
			item: rawA(), show: true,
			getFileById: async (id) => { calls++; return backend[id] ?? null; },
			onRender: (x) => (r = { ...x })
		} });
		await settle();
		check(r.text === 'AAA', 'OFM3: A content');
		cmp.$set({ item: rawC() }); await settle();    // switch file while open
		check(r && r.state === 'text' && r.text === 'CCC', 'OFM3: switch to C -> C content');
		check(calls === 2, `OFM3: switch file = one more fetch [calls=${calls}]`);
	}

	// --- OFM4: in-flight swap A->C (item prop) while show stays true; stale A
	//     must NOT clobber C content.
	{
		const gate = {};
		let r = null;
		const getFileById = (id) => new Promise((resolve) => { gate[id] = () => resolve(backend[id] ?? null); });
		const cmp = new OFMCore({ target: makeNode('body'), props: { item: rawA(), show: true, getFileById, onRender: (x) => (r = { ...x }) } });
		await settle();                          // A in flight
		cmp.$set({ item: rawC() }); await settle();  // C in flight
		gate.A && gate.A(); await settle();      // resolve stale A while showing C
		check(!(r.fileId === 'C' && r.text === 'AAA'), 'OFM4: stale A not applied onto C');
		gate.C && gate.C(); await settle();
		check(r && r.text === 'CCC', 'OFM4: C eventually shows its own content');
	}

	// --- OFM5: show-toggle storm: false->true->false->true rapidly w/ raw items.
	{
		let calls = 0;
		let r = null;
		const cmp = new OFMCore({ target: makeNode('body'), props: {
			item: rawA(), show: false,
			getFileById: async (id) => { calls++; return backend[id] ?? null; },
			onRender: (x) => (r = { ...x })
		} });
		await settle();
		cmp.$set({ show: true }); await settle();
		const c1 = calls;
		check(c1 === 1 && r.text === 'AAA', `OFM5: first open 1 fetch [calls=${c1}]`);
		// toggle without changing item identity (item already hydrated now) -> no refetch
		cmp.$set({ show: false }); await settle();
		cmp.$set({ show: true }); await settle();
		check(calls === c1, `OFM5: toggle with hydrated item -> no extra fetch [calls=${calls}]`);
		check(r && r.text === 'AAA' && r.show === true, 'OFM5: content correct after toggles');
	}
} finally {
	rmSync(fpTmp, { force: true });
	rmSync(ofmTmp, { force: true });
}

console.log('\n' + (failures ? `VIOLATIONS FOUND: ${failures} assertion(s) failed` : 'ALL PREVIEW-REACTIVE CHECKS HOLD'));
process.exitCode = failures ? 1 : 0;
