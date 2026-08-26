#!/usr/bin/env node
/**
 * Build-time font subsetting.
 *
 * The UI ships variable TTFs (Inter 804 KB, Archivo 652 KB, Vazirmatn 241 KB,
 * InstrumentSerif 69 KB) with no `unicode-range`, so a cold paint pulls the full
 * fonts — brutal on a low-bandwidth link. This pass uses `pyftsubset` (fontTools)
 * to emit small woff2 subsets, split into a Latin "core" face (eager, covers
 * English + Western/Central-European Latin) and an "ext" face (Greek / Cyrillic /
 * Vietnamese, fetched on demand via non-overlapping unicode-range). Vazirmatn is
 * subset to its Arabic range with all layout features kept for correct shaping.
 *
 * The variable weight axis is preserved (no --instance), so `font-weight: 100 900`
 * still works. Idempotent: regenerates a subset only when the source TTF is newer
 * than the output woff2, so it adds ~0 to incremental builds.
 *
 * Wired into the `prebuild` npm script. Requires `pyftsubset` on PATH and the
 * Python `brotli` module (for woff2 output). If pyftsubset is missing the build
 * still succeeds — app.css references the woff2 files, so commit the generated
 * output (this script is reproducibility, not a hard build dep).
 */
import { execFileSync } from 'node:child_process';
import { statSync, existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const __dirname = dirname(fileURLToPath(import.meta.url));
const FONT_DIR = join(__dirname, '..', 'static', 'assets', 'fonts');

// Non-overlapping unicode ranges. `latin` is the eager core (merges Latin-1 +
// Latin Extended-A/B so all common European locales render without a 2nd fetch);
// `ext` is everything else those Latin fonts cover; `arabic` is Vazirmatn's job.
const RANGES = {
	latin:
		'U+0000-00FF,U+0131,U+0152-0153,U+02BB-02BC,U+02C6,U+02DA,U+02DC,U+0300-0304,' +
		'U+0308-0309,U+0323,U+0329,U+2000-206F,U+2074,U+20AC,U+2122,U+2191,U+2193,' +
		'U+2212,U+2215,U+FEFF,U+FFFD,U+0100-024F',
	ext:
		'U+0250-02AF,U+0370-03FF,U+0400-052F,U+0590-05FF,U+1E00-1EFF,U+1F00-1FFF,' +
		'U+2C60-2C7F,U+A720-A7FF,U+FB00-FB06',
	arabic: 'U+0600-06FF,U+0750-077F,U+08A0-08FF,U+200C-200F,U+FB50-FDFF,U+FE70-FEFF'
};

const FONTS = [
	{ src: 'Inter-Variable.ttf', subsets: ['latin', 'ext'] },
	{ src: 'Archivo-Variable.ttf', subsets: ['latin', 'ext'] },
	{ src: 'InstrumentSerif-Regular.ttf', subsets: ['latin'] },
	{ src: 'Vazirmatn-Variable.ttf', subsets: ['arabic'], layoutAll: true }
];

const fmt = (n) => `${(n / 1024).toFixed(1)} KB`;

let generated = 0;
let skipped = 0;
let totalIn = 0;
let totalOut = 0;

for (const font of FONTS) {
	const input = join(FONT_DIR, font.src);
	if (!existsSync(input)) {
		console.warn(`[subset-fonts] missing source, skipping: ${font.src}`);
		continue;
	}
	const inStat = statSync(input);
	const base = font.src.replace(/\.ttf$/i, '');
	for (const subset of font.subsets) {
		const out = join(FONT_DIR, `${base}.${subset}.woff2`);
		if (existsSync(out) && statSync(out).mtimeMs >= inStat.mtimeMs) {
			skipped++;
			totalOut += statSync(out).size;
			continue;
		}
		const args = [
			input,
			`--unicodes=${RANGES[subset]}`,
			'--flavor=woff2',
			`--output-file=${out}`
		];
		// Arabic needs the full GSUB/GPOS feature set for init/medi/fina shaping.
		if (font.layoutAll) args.push("--layout-features=*");
		try {
			execFileSync('pyftsubset', args, { stdio: 'pipe' });
		} catch (e) {
			console.error(
				`[subset-fonts] pyftsubset failed for ${font.src} (${subset}). ` +
					`Is pyftsubset + python 'brotli' installed? Keeping any existing woff2.`
			);
			console.error(String(e.stderr || e.message || e));
			continue;
		}
		const outSize = statSync(out).size;
		totalIn += inStat.size;
		totalOut += outSize;
		generated++;
		console.log(
			`[subset-fonts] ${font.src} → ${base}.${subset}.woff2  ` +
				`${fmt(inStat.size)} → ${fmt(outSize)}`
		);
	}
}

console.log(
	`[subset-fonts] done: ${generated} generated, ${skipped} up-to-date.` +
		(generated ? `  new output total ${fmt(totalOut)}.` : '')
);
