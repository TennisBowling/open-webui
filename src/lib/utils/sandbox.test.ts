import { describe, expect, it } from 'vitest';

import {
	isSandboxHref,
	isImageSandboxFile,
	normalizeSandboxPath,
	resolveSandboxFile,
	sandboxFileContentUrl
} from './sandbox';

// A generated-file descriptor shaped like an entry of message.files.
const f = (workspace_path: string, id = workspace_path) => ({
	id,
	container_workspace: { workspace_path }
});

describe('isSandboxHref', () => {
	it('matches every sandbox: slash variant', () => {
		expect(isSandboxHref('sandbox:/workspace/outputs/a.docx')).toBe(true); // single (canonical)
		expect(isSandboxHref('sandbox://workspace/outputs/a.docx')).toBe(true); // double (authority)
		expect(isSandboxHref('sandbox:///workspace/outputs/a.docx')).toBe(true); // triple (file:// style)
		expect(isSandboxHref('sandbox:workspace/outputs/a.docx')).toBe(true); // no slash
		expect(isSandboxHref('sandbox:/outputs/a.docx')).toBe(true); // no workspace segment
		expect(isSandboxHref('SANDBOX:/workspace/a.docx')).toBe(true); // case-insensitive
	});

	it('does NOT match non-sandbox hrefs', () => {
		// /workspace is a real in-app route — must not be hijacked.
		expect(isSandboxHref('/workspace/models')).toBe(false);
		expect(isSandboxHref('/workspace/outputs/a.docx')).toBe(false);
		expect(isSandboxHref('https://example.com/a.docx')).toBe(false);
		expect(isSandboxHref('http://sandbox.example.com/x')).toBe(false);
		expect(isSandboxHref('mailto:sandbox:@x.com')).toBe(false);
		expect(isSandboxHref('#anchor')).toBe(false);
		expect(isSandboxHref('')).toBe(false);
		expect(isSandboxHref(undefined)).toBe(false);
		expect(isSandboxHref(null)).toBe(false);
	});
});

describe('normalizeSandboxPath', () => {
	it('collapses every slash variant to the same relative path', () => {
		const expected = 'outputs/a.docx';
		expect(normalizeSandboxPath('sandbox:/workspace/outputs/a.docx')).toBe(expected);
		expect(normalizeSandboxPath('sandbox://workspace/outputs/a.docx')).toBe(expected);
		expect(normalizeSandboxPath('sandbox:///workspace/outputs/a.docx')).toBe(expected);
		expect(normalizeSandboxPath('sandbox:workspace/outputs/a.docx')).toBe(expected);
		expect(normalizeSandboxPath('/workspace/outputs/a.docx')).toBe(expected);
		expect(normalizeSandboxPath('workspace/outputs/a.docx')).toBe(expected);
		expect(normalizeSandboxPath('outputs/a.docx')).toBe(expected);
	});

	it('handles the stored relative form unchanged', () => {
		expect(normalizeSandboxPath('outputs/a.docx')).toBe('outputs/a.docx');
		expect(normalizeSandboxPath('inputs/data.csv')).toBe('inputs/data.csv');
	});

	it('decodes percent-encoded segments (spaces, unicode)', () => {
		expect(normalizeSandboxPath('sandbox:/workspace/outputs/my%20file.docx')).toBe(
			'outputs/my file.docx'
		);
	});

	it('keeps the value when percent-decoding is malformed', () => {
		// A lone % is not a valid escape — must not throw, must not drop the path.
		expect(normalizeSandboxPath('sandbox:/workspace/outputs/100%.docx')).toBe('outputs/100%.docx');
	});

	it('does NOT strip a `workspace`-prefixed FILENAME (segment boundary)', () => {
		// Lookahead guards against eating part of a real name.
		expect(normalizeSandboxPath('sandbox:/workspace/workspace_notes.docx')).toBe(
			'workspace_notes.docx'
		);
		expect(normalizeSandboxPath('sandbox:/workspace/workspaces/a.docx')).toBe('workspaces/a.docx');
	});

	it('handles empty / nullish input', () => {
		expect(normalizeSandboxPath('')).toBe('');
		expect(normalizeSandboxPath(undefined)).toBe('');
		expect(normalizeSandboxPath(null)).toBe('');
	});

	it('is a pure PATH normalizer: does NOT strip #/? (those are href-only)', () => {
		// A stored filesystem path can legitimately contain # or ?, so the path
		// normalizer must preserve them; fragment-stripping is the href's job.
		expect(normalizeSandboxPath('outputs/Q#3.docx')).toBe('outputs/Q#3.docx');
		expect(normalizeSandboxPath('sandbox:/workspace/outputs/a%23b.docx')).toBe('outputs/a#b.docx');
	});
});

describe('resolveSandboxFile', () => {
	const files = [
		f('outputs/1_LWHOA_Small_Claims_Filing_Worksheet.docx'),
		f('outputs/2_LWHOA_Trial_Statement_and_Hearing_Script.docx'),
		f('inputs/source.pdf')
	];

	it('THE REPORTED BUG: single-slash sandbox:/workspace link resolves', () => {
		const file = resolveSandboxFile(
			'sandbox:/workspace/outputs/1_LWHOA_Small_Claims_Filing_Worksheet.docx',
			files
		);
		expect(file).toBe(files[0]);
	});

	it('resolves the double/triple/no-slash variants identically', () => {
		const target = files[1];
		const path = 'outputs/2_LWHOA_Trial_Statement_and_Hearing_Script.docx';
		expect(resolveSandboxFile(`sandbox://workspace/${path}`, files)).toBe(target);
		expect(resolveSandboxFile(`sandbox:///workspace/${path}`, files)).toBe(target);
		expect(resolveSandboxFile(`sandbox:workspace/${path}`, files)).toBe(target);
	});

	it('resolves an input file', () => {
		expect(resolveSandboxFile('sandbox:/workspace/inputs/source.pdf', files)).toBe(files[2]);
	});

	it('resolves when the model omits the outputs/ subdirectory', () => {
		expect(
			resolveSandboxFile('sandbox:/workspace/1_LWHOA_Small_Claims_Filing_Worksheet.docx', files)
		).toBe(files[0]);
	});

	it('unique-basename fallback: right name, wrong sub-directory (same namespace)', () => {
		// Model points at outputs/sub/, file is at outputs/, basename unique → resolve.
		expect(
			resolveSandboxFile('sandbox:/workspace/outputs/sub/2_LWHOA_Trial_Statement_and_Hearing_Script.docx', files)
		).toBe(files[1]);
	});

	it('does NOT guess between two files sharing a basename', () => {
		const dup = [f('outputs/report.docx', 'a'), f('archive/report.docx', 'b')];
		// Neither exact path matches → ambiguous basename → null (toast), no wrong guess.
		expect(resolveSandboxFile('sandbox:/workspace/other/report.docx', dup)).toBe(null);
	});

	it('basename fallback does NOT cross top-level namespaces (inputs/ vs outputs/)', () => {
		// An inputs/ link must not silently open an unrelated outputs/ file.
		const only = [f('outputs/data.csv', 'OUT')];
		expect(resolveSandboxFile('sandbox:/workspace/inputs/data.csv', only)).toBe(null);
		// But within the same namespace, a wrong-subdir reference still resolves.
		const sub = [f('outputs/data.csv', 'OUT')];
		expect(resolveSandboxFile('sandbox:/workspace/outputs/sub/data.csv', sub)).toBe(sub[0]);
	});

	it('resolves a multi-version path to the NEWEST descriptor', () => {
		const v1 = { id: 'v1', container_workspace: { workspace_path: 'outputs/r.docx', version: 1 } };
		const v3 = { id: 'v3', container_workspace: { workspace_path: 'outputs/r.docx', version: 3 } };
		const v2 = { id: 'v2', container_workspace: { workspace_path: 'outputs/r.docx', version: 2 } };
		// Order shuffled; highest version wins regardless of position.
		expect(resolveSandboxFile('sandbox:/workspace/outputs/r.docx', [v1, v3, v2])).toBe(v3);
	});

	it('on a version tie prefers the LAST (most recently imported) entry', () => {
		const a = { id: 'a', container_workspace: { workspace_path: 'outputs/r.docx' } };
		const b = { id: 'b', container_workspace: { workspace_path: 'outputs/r.docx' } };
		expect(resolveSandboxFile('sandbox:/workspace/outputs/r.docx', [a, b])).toBe(b);
	});

	it('returns null for non-sandbox hrefs and misses', () => {
		expect(resolveSandboxFile('/workspace/outputs/1_LWHOA_Small_Claims_Filing_Worksheet.docx', files)).toBe(
			null
		);
		expect(resolveSandboxFile('https://x.com/a.docx', files)).toBe(null);
		expect(resolveSandboxFile('sandbox:/workspace/outputs/does_not_exist.docx', files)).toBe(null);
	});

	it('strips a trailing #fragment / ?query from the HREF before matching', () => {
		expect(
			resolveSandboxFile(
				'sandbox:/workspace/outputs/1_LWHOA_Small_Claims_Filing_Worksheet.docx#summary',
				files
			)
		).toBe(files[0]);
		expect(
			resolveSandboxFile(
				'sandbox:/workspace/outputs/2_LWHOA_Trial_Statement_and_Hearing_Script.docx?v=2',
				files
			)
		).toBe(files[1]);
	});

	it('REGRESSION: a file genuinely named with # resolves from a LITERAL # href', () => {
		// Models write the '#' unencoded; the literal path must win.
		const hash = [f('outputs/report#final.docx', 'H')];
		expect(resolveSandboxFile('sandbox:/workspace/outputs/report#final.docx', hash)).toBe(hash[0]);
		// ...and also via the encoded form.
		expect(resolveSandboxFile('sandbox:/workspace/outputs/report%23final.docx', hash)).toBe(hash[0]);
	});

	it('REGRESSION: literal-# href does NOT open a same-prefix sibling', () => {
		// `report#final.docx` is meant; a bare `report` sibling must not be opened.
		const sibs = [f('outputs/report', 'BARE'), f('outputs/report#final.docx', 'HASH')];
		expect(resolveSandboxFile('sandbox:/workspace/outputs/report#final.docx', sibs)).toBe(sibs[1]);
	});

	it('a genuine #fragment still resolves to the un-fragmented file', () => {
		// No file literally named with '#', so the stripped form matches.
		expect(
			resolveSandboxFile('sandbox:/workspace/outputs/1_LWHOA_Small_Claims_Filing_Worksheet.docx#part2', files)
		).toBe(files[0]);
	});

	it('tolerates a nested file.data.container_workspace shape', () => {
		const nested = [{ id: 'n', file: { data: { container_workspace: { workspace_path: 'outputs/x.docx' } } } }];
		expect(resolveSandboxFile('sandbox:/workspace/outputs/x.docx', nested)).toBe(nested[0]);
	});

	it('is null-safe against junk file descriptors', () => {
		const junk = [null, undefined, {}, { container_workspace: null }, f('outputs/ok.docx')];
		expect(resolveSandboxFile('sandbox:/workspace/outputs/ok.docx', junk as any)).toBe(junk[4]);
		expect(resolveSandboxFile('sandbox:/workspace/outputs/ok.docx', null as any)).toBe(null);
	});
});

describe('sandboxFileContentUrl', () => {
	it('builds an authenticated content URL from id', () => {
		expect(sandboxFileContentUrl({ id: 'abc' }, '/api/v1')).toBe('/api/v1/files/abc/content');
	});
	it('falls back to a nested file.id', () => {
		expect(sandboxFileContentUrl({ file: { id: 'xyz' } }, '/api/v1')).toBe(
			'/api/v1/files/xyz/content'
		);
	});
	it('returns empty string when there is no id', () => {
		expect(sandboxFileContentUrl({}, '/api/v1')).toBe('');
		expect(sandboxFileContentUrl(null, '/api/v1')).toBe('');
	});
});

describe('isImageSandboxFile', () => {
	it('detects images by content type', () => {
		expect(isImageSandboxFile({ meta: { content_type: 'image/png' } })).toBe(true);
		expect(isImageSandboxFile({ file: { meta: { content_type: 'image/jpeg' } } })).toBe(true);
	});
	it('detects images by extension on name or workspace_path', () => {
		expect(isImageSandboxFile({ name: 'chart.PNG' })).toBe(true);
		expect(
			isImageSandboxFile({ container_workspace: { workspace_path: 'outputs/diagram.svg' } })
		).toBe(true);
	});
	it('returns false for non-image media', () => {
		expect(isImageSandboxFile({ container_workspace: { workspace_path: 'outputs/report.pdf' } })).toBe(
			false
		);
		expect(isImageSandboxFile({ name: 'rec.mp3', meta: { content_type: 'audio/mpeg' } })).toBe(false);
		expect(isImageSandboxFile({ name: 'doc.docx' })).toBe(false);
		expect(isImageSandboxFile({})).toBe(false);
		expect(isImageSandboxFile(null)).toBe(false);
	});
});
