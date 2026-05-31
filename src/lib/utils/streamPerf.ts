type StreamPerfMetric = {
	count: number;
	total: number;
	max: number;
};

type StreamPerfState = {
	metrics: Record<string, StreamPerfMetric>;
	lastLogAt: number;
};

declare global {
	interface Window {
		__owuiStreamPerf?: StreamPerfState;
	}
}

const PERF_FLAG = 'chatStreamPerf';
const LOG_INTERVAL_MS = 5000;

let enabledCache = false;
let lastEnabledCheck = 0;

const isEnabled = () => {
	if (typeof window === 'undefined') return false;

	const now = Date.now();
	if (now - lastEnabledCheck < 1000) return enabledCache;

	lastEnabledCheck = now;
	try {
		enabledCache = window.localStorage?.getItem(PERF_FLAG) === '1';
	} catch {
		enabledCache = false;
	}
	return enabledCache;
};

const getState = (): StreamPerfState | null => {
	if (!isEnabled() || typeof window === 'undefined') return null;
	if (!window.__owuiStreamPerf) {
		window.__owuiStreamPerf = {
			metrics: {},
			lastLogAt: performance.now()
		};
	}
	return window.__owuiStreamPerf;
};

const maybeLog = (state: StreamPerfState) => {
	const now = performance.now();
	if (now - state.lastLogAt < LOG_INTERVAL_MS) return;
	state.lastLogAt = now;

	const rows = Object.entries(state.metrics).map(([name, metric]) => ({
		name,
		count: metric.count,
		totalMs: Number(metric.total.toFixed(2)),
		avgMs: Number((metric.total / Math.max(1, metric.count)).toFixed(3)),
		maxMs: Number(metric.max.toFixed(3))
	}));
	state.metrics = {};

	if (rows.length > 0) {
		console.table(rows);
	}
};

export const streamPerfStart = () => {
	if (!isEnabled() || typeof performance === 'undefined') return 0;
	return performance.now();
};

export const streamPerfEnd = (name: string, startedAt: number, count = 1) => {
	if (!startedAt || typeof performance === 'undefined') return;
	const state = getState();
	if (!state) return;

	const duration = performance.now() - startedAt;
	const metric = state.metrics[name] ?? { count: 0, total: 0, max: 0 };
	metric.count += count;
	metric.total += duration;
	metric.max = Math.max(metric.max, duration);
	state.metrics[name] = metric;
	maybeLog(state);
};

export const streamPerfCount = (name: string, count = 1) => {
	const state = getState();
	if (!state) return;

	const metric = state.metrics[name] ?? { count: 0, total: 0, max: 0 };
	metric.count += count;
	state.metrics[name] = metric;
	maybeLog(state);
};
