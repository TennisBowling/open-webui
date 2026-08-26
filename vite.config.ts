import { sveltekit } from '@sveltejs/kit/vite';
import tailwindcss from '@tailwindcss/vite';
import { defineConfig } from 'vite';

import { viteStaticCopy } from 'vite-plugin-static-copy';

export default defineConfig({
	plugins: [
		tailwindcss(),
		sveltekit(),
		viteStaticCopy({
			targets: [
				{
					src: 'node_modules/onnxruntime-web/dist/*.jsep.*',

					dest: 'wasm'
				}
			]
		})
	],
	define: {
		APP_VERSION: JSON.stringify(process.env.npm_package_version),
		APP_BUILD_HASH: JSON.stringify(process.env.APP_BUILD_HASH || 'dev-build')
	},
	build: {
		// 'hidden' still emits maps for error reporting but stops advertising them
		// via //# sourceMappingURL in prod chunks — no ~51 MB of maps offered to
		// clients on metered links.
		sourcemap: 'hidden'
	},
	worker: {
		format: 'es'
	},
	ssr: {
		noExternal: [],
		external: []
	},
	optimizeDeps: {
		exclude: ['vega', 'vega-lite']
	}
});
