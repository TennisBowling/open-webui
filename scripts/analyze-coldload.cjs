const fs = require('fs'), path = require('path');
const BUILD = 'build';
const closure = JSON.parse(fs.readFileSync(path.join(BUILD, 'sw-precache.json'), 'utf8')).filter((u) => u.endsWith('.js'));
const brOf = (rel) => {
	const b = path.join(BUILD, rel) + '.br', p = path.join(BUILD, rel);
	return fs.existsSync(b) ? fs.statSync(b).size : fs.existsSync(p) ? fs.statSync(p).size : 0;
};
function bucket(src) {
	let m = src.match(/node_modules\/(@[^/]+\/[^/]+|[^/]+)/);
	if (m) return 'npm:' + m[1];
	if (src.includes('Chat.svelte')) return 'app:Chat.svelte';
	if (src.includes('RichTextInput') || src.includes('prosemirror') || src.includes('tiptap')) return 'app:editor(RichTextInput)';
	if (/components\/chat\/Messages/.test(src)) return 'app:Messages/*';
	if (/components\/chat/.test(src)) return 'app:components/chat/*';
	if (/components\/layout/.test(src)) return 'app:components/layout/*';
	if (/components\/common/.test(src)) return 'app:components/common/*';
	if (/components\/admin/.test(src)) return 'app:components/admin/*';
	if (/components/.test(src)) return 'app:components/*';
	if (/src\/lib\/(utils|apis|stores|i18n|workers)/.test(src)) return 'app:' + src.match(/src\/lib\/(\w+)/)[1];
	if (/svelte\/src\/(runtime|internal)|\.svelte-kit\/generated|@sveltejs\/kit/.test(src)) return 'svelte+kit runtime';
	return 'other';
}
const agg = {};
let totalBr = 0, chunks = 0, noMap = 0;
for (const u of closure) {
	const rel = u.replace(/^\//, '');
	const br = brOf(rel); totalBr += br; chunks++;
	const mp = path.join(BUILD, rel) + '.map';
	if (!fs.existsSync(mp)) { noMap++; agg['(no map)'] = (agg['(no map)'] || 0) + br; continue; }
	let map; try { map = JSON.parse(fs.readFileSync(mp, 'utf8')); } catch { continue; }
	const srcs = map.sources || [], cont = map.sourcesContent || [];
	let tot = 0; const sizes = srcs.map((s, i) => { const l = (cont[i] || '').length; tot += l; return l; });
	if (!tot) { agg['(empty map)'] = (agg['(empty map)'] || 0) + br; continue; }
	srcs.forEach((s, i) => { agg[bucket(s)] = (agg[bucket(s)] || 0) + br * sizes[i] / tot; });
}
const rows = Object.entries(agg).sort((a, b) => b[1] - a[1]);
console.log(`COLD-LOAD JS: ${chunks} chunks, ${(totalBr / 1024).toFixed(0)} KB brotli total\n`);
console.log('attributed by source (brotli KB, proportional to original size):');
let shown = 0;
for (const [k, v] of rows) { if (v < 1536) continue; console.log(`  ${(v / 1024).toFixed(0).padStart(4)} KB  ${k}`); shown += v; }
console.log(`  ---- ${(shown / 1024).toFixed(0)} KB shown, ${((totalBr - shown) / 1024).toFixed(0)} KB in <1.5KB buckets`);
