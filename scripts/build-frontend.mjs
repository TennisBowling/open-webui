import childProcess from 'node:child_process';
import fs from 'node:fs';
import process from 'node:process';
import { fileURLToPath } from 'node:url';

const repositoryRoot = new URL('../', import.meta.url);

const resolveBuildVersion = () => {
	if (process.env.WEBUI_BUILD_VERSION) return process.env.WEBUI_BUILD_VERSION;

	try {
		const git = (args) =>
			childProcess.execFileSync('git', args, {
				cwd: repositoryRoot,
				encoding: 'utf8'
			});
		const revision = git(['rev-parse', 'HEAD']).trim();
		const dirty = git(['status', '--porcelain=v1', '--untracked-files=normal']).trim();
		return dirty ? `${revision}-dirty-${Date.now().toString(36)}` : revision;
	} catch {
		try {
			const packageVersion =
				JSON.parse(fs.readFileSync(new URL('../package.json', import.meta.url), 'utf8'))?.version ||
				'unknown';
			return `${packageVersion}-build-${Date.now().toString(36)}`;
		} catch {
			return `unknown-build-${Date.now().toString(36)}`;
		}
	}
};

const viteCli = fileURLToPath(new URL('../node_modules/vite/bin/vite.js', import.meta.url));
const result = childProcess.spawnSync(process.execPath, [viteCli, 'build'], {
	cwd: repositoryRoot,
	env: {
		...process.env,
		WEBUI_BUILD_VERSION: resolveBuildVersion()
	},
	stdio: 'inherit'
});

if (result.error) throw result.error;
if (result.signal) process.kill(process.pid, result.signal);
process.exit(result.status ?? 1);
