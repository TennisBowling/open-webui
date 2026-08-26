import adapter from '@sveltejs/adapter-static';
import * as child_process from 'node:child_process';
import { vitePreprocess } from '@sveltejs/vite-plugin-svelte';
import fs from 'node:fs';

const repositoryRoot = new URL('./', import.meta.url);
const runGit = (args) =>
	child_process.execFileSync('git', args, {
		cwd: repositoryRoot,
		maxBuffer: 16 * 1024 * 1024
	});

const resolveBuildVersion = () => {
	const explicitVersion = process.env.WEBUI_BUILD_VERSION;
	if (explicitVersion) return explicitVersion;

	try {
		const revision = runGit(['rev-parse', 'HEAD']).toString().trim();
		const worktreeStatus = runGit(['status', '--porcelain=v1', '--untracked-files=no']);
		// Keep non-build commands deterministic. `npm run build` supplies a unique
		// WEBUI_BUILD_VERSION once to every SvelteKit phase via build-frontend.mjs.
		return worktreeStatus.length > 0 ? `${revision}-dirty` : revision;
	} catch {
		// A source archive has no Git worktree to fingerprint. Keep this deterministic
		// across SvelteKit's build phases; deployments that need a unique archive
		// version can provide WEBUI_BUILD_VERSION explicitly.
		try {
			const packageVersion =
				JSON.parse(fs.readFileSync(new URL('./package.json', import.meta.url), 'utf8'))?.version ||
				'unknown';
			return `${packageVersion}-build`;
		} catch {
			return 'unknown-build';
		}
	}
};

/** @type {import('@sveltejs/kit').Config} */
const config = {
	// Consult https://kit.svelte.dev/docs/integrations#preprocessors
	// for more information about preprocessors
	preprocess: vitePreprocess(),
	kit: {
		// adapter-auto only supports some environments, see https://kit.svelte.dev/docs/adapter-auto for a list.
		// If your environment is not supported or you settled on a specific environment, switch out the adapter.
		// See https://kit.svelte.dev/docs/adapters for more information about adapters.
		adapter: adapter({
			pages: 'build',
			assets: 'build',
			fallback: 'index.html'
		}),
		// poll for new version name every 5 minutes (to trigger reload mechanic in +layout.svelte)
		version: {
			name: resolveBuildVersion(),
			// Polling is driven manually in src/routes/+layout.svelte so it can be
			// gated on document visibility (never poll version.json in a hidden/
			// backgrounded PWA tab on a metered link). 0 disables SvelteKit's
			// built-in interval; the redeploy-forces-reload machinery (updated.current
			// -> beforeNavigate hard reload) is unchanged.
			pollInterval: 0
		}
	},
	vitePlugin: {
		dynamicCompileOptions: ({ filename }) =>
			filename.startsWith(new URL('./src/', import.meta.url).pathname) ? { runes: true } : undefined
		// inspector: {
		// 	toggleKeyCombo: 'meta-shift', // Key combination to open the inspector
		// 	holdMode: false, // Enable or disable hold mode
		// 	showToggleButton: 'always', // Show toggle button ('always', 'active', 'never')
		// 	toggleButtonPos: 'bottom-right' // Position of the toggle button
		// }
	},
	onwarn: (warning, handler) => {
		const { code } = warning;
		if (code === 'css-unused-selector') return;

		handler(warning);
	}
};

export default config;
