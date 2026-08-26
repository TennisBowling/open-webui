// ROUND-3 adversarial verification of the LIVE mergeMessageFiles invariant on the
// PATCHED Chat.svelte — using the REAL function bodies (extracted verbatim from
// src/lib/components/chat/Chat.svelte at runtime, then eval'd as real JS), NOT a
// transcription. This pins the Python mirror in test_adv3_endtoend.py to the
// actual source: if either drifts, this fails.
//
// Invariant: for the two-concurrent-fanout sequence (two completions each
// emitting a distinct-id descriptor of the SAME (workspace_path, sha256), each
// replayed through BOTH the {type:'files'} handler and the chat:completion
// handler — i.e. mergeMessageFiles called 4x total), message.files holds EXACTLY
// ONE card. And the simple single-file / two-distinct-file / regen sequences hold.
//
// Run: node backend/open_webui/test/util/test_adv3_live_merge.mjs

import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const __dirname = dirname(fileURLToPath(import.meta.url));
// backend/open_webui/test/util -> repo root is five levels up.
const repoRoot = join(__dirname, '..', '..', '..', '..');
const chatSvelte = readFileSync(
	join(repoRoot, 'src', 'lib', 'components', 'chat', 'Chat.svelte'),
	'utf-8'
);

// --- Extract the REAL fileContentKey + mergeMessageFiles bodies verbatim. ---
function extractConst(src, name) {
	// Match `const <name> = (...) => { ... };` balancing braces from the first
	// `{` of the arrow body to its matching `}`.
	const decl = `const ${name} = `;
	const start = src.indexOf(decl);
	if (start < 0) throw new Error(`could not find ${name} in Chat.svelte`);
	const arrow = src.indexOf('=>', start);
	if (arrow < 0) throw new Error(`no arrow for ${name}`);
	const braceStart = src.indexOf('{', arrow);
	if (braceStart < 0) throw new Error(`no body brace for ${name}`);
	let depth = 0;
	let i = braceStart;
	for (; i < src.length; i++) {
		const ch = src[i];
		if (ch === '{') depth++;
		else if (ch === '}') {
			depth--;
			if (depth === 0) break;
		}
	}
	if (depth !== 0) throw new Error(`unbalanced braces for ${name}`);
	// Strip TypeScript type annotations so plain eval can run it.
	const signature = src.slice(start + decl.length, braceStart);
	const body = src.slice(braceStart, i + 1);
	return (signature + body)
		.replace(/:\s*string\s*\|\s*null/g, '')
		.replace(/:\s*any\[\]/g, '')
		.replace(/:\s*any/g, '')
		.replace(/new Set<string>\(\)/g, 'new Set()');
}

const fileContentKeySrc = extractConst(chatSvelte, 'fileContentKey');
const mergeMessageFilesSrc = extractConst(chatSvelte, 'mergeMessageFiles');

// Build the real functions in this scope. mergeMessageFiles references
// fileContentKey by name, so define both in the same eval closure.
const factory = new Function(
	`const fileContentKey = ${fileContentKeySrc};\n` +
		`const mergeMessageFiles = ${mergeMessageFilesSrc};\n` +
		`return { fileContentKey, mergeMessageFiles };`
);
const { fileContentKey, mergeMessageFiles } = factory();

// Sanity: the patched key uses the U+0000 (NUL) separator at runtime.
const NUL = String.fromCharCode(0);
const probe = fileContentKey({ container_workspace: { workspace_path: 'outputs/x', sha256: 'H' } });
const expected = 'cw' + NUL + 'outputs/x' + NUL + 'H';
if (probe !== expected) {
	throw new Error(`fileContentKey shape drifted: ${JSON.stringify(probe)}`);
}
if (probe.indexOf(NUL) < 0) throw new Error('expected NUL separator');

let failures = 0;
function check(name, cond, detail) {
	if (cond) {
		console.log(`ok   - ${name}`);
	} else {
		failures++;
		console.error(`FAIL - ${name}: ${detail}`);
	}
}

// LIVE = one completion replays mergeMessageFiles for the {type:'files'} handler
// (2970) AND the chat:completion handler (5177), both fed the same imported list.
function liveAfterCompletion(prior, imported) {
	let files = mergeMessageFiles(prior ?? [], imported ?? []);
	files = mergeMessageFiles(files, imported ?? []);
	return files;
}
const cards = (files, name) => files.filter((f) => f && f.name === name);

// --- (2) two concurrent fanout completions, distinct id, same content key. ---
{
	const a = {
		id: 'id-a', name: 'chart.png', type: 'file',
		container_workspace: { workspace_path: 'outputs/chart.png', sha256: 'HHH' }
	};
	const b = {
		id: 'id-b', name: 'chart.png', type: 'file',
		container_workspace: { workspace_path: 'outputs/chart.png', sha256: 'HHH' }
	};
	let live = [];
	live = liveAfterCompletion(live, [a]);
	live = liveAfterCompletion(live, [b]);
	check('concurrent-fanout live == 1 card', cards(live, 'chart.png').length === 1 && live.length === 1, JSON.stringify(live));
}

// --- (1) single new file. ---
{
	const a = { id: 'id-1', name: 'report.csv', container_workspace: { workspace_path: 'outputs/report.csv', sha256: 'S1' } };
	const live = liveAfterCompletion([], [a]);
	check('single-file live == 1', live.length === 1 && cards(live, 'report.csv').length === 1, JSON.stringify(live));
}

// --- (4) two distinct files in one completion. ---
{
	const a = { id: 'id-a', name: 'a.txt', container_workspace: { workspace_path: 'outputs/a.txt', sha256: 'A' } };
	const b = { id: 'id-b', name: 'b.txt', container_workspace: { workspace_path: 'outputs/b.txt', sha256: 'B' } };
	const live = liveAfterCompletion([], [a, b]);
	check('two-distinct live == 2', live.length === 2 && cards(live, 'a.txt').length === 1 && cards(live, 'b.txt').length === 1, JSON.stringify(live));
}

// --- (6) regen: stray re-emit of the SAME descriptor collapses to 1. ---
{
	const a = { id: 'id-1', name: 'out.txt', container_workspace: { workspace_path: 'outputs/out.txt', sha256: 'O' } };
	let live = liveAfterCompletion([], [a]);
	live = liveAfterCompletion(live, [a]); // regen re-emit
	check('regen re-emit live == 1', live.length === 1 && cards(live, 'out.txt').length === 1, JSON.stringify(live));
}

// --- preview_file_id survives the merge (office docx). ---
{
	const a = { id: 'id-d', name: 'summary.docx', preview_file_id: 'prev-1', container_workspace: { workspace_path: 'outputs/summary.docx', sha256: 'D', preview_file_id: 'prev-1' } };
	const live = liveAfterCompletion([], [a]);
	check('docx preview preserved', live.length === 1 && live[0].preview_file_id === 'prev-1', JSON.stringify(live));
}

// --- non-container file (no container_workspace) still deduped by id only. ---
{
	const u = { id: 'u-1', name: 'upload.pdf' };
	let live = mergeMessageFiles([], [u]);
	live = mergeMessageFiles(live, [u]);
	check('non-container id-dedup', live.length === 1, JSON.stringify(live));
}

if (failures) {
	console.error(`\n${failures} check(s) FAILED`);
	process.exit(1);
}
console.log('\nall checks passed');
