// Pre-measurement of content-visibility message placeholders.
//
// Every chat turn renders inside a `content-visibility: auto` wrapper
// ([data-cv-wrap] in Message.svelte) with a contain-intrinsic-size GUESS of
// 150px. Real turns are routinely 300-1500px, so the first time the reader
// scrolls up past an unrealized turn its placeholder snaps to the true height
// and everything below shifts by the difference — on iOS Safari (no native
// scroll anchoring) that read as the view "jumping around" while scrolling
// back through history. Chat.svelte's scroll-anchoring engine corrects such
// shifts pre-paint, but a correction mid-fling still costs the momentum on
// iOS (any programmatic scrollTop write cancels it).
//
// This sweeper removes the shifts at the SOURCE: during scroll-quiet idle
// moments it walks the unmeasured wrappers bottom-up (the region the reader
// reaches first), briefly forces each chunk to lay out
// (content-visibility: visible), records the real height into
// contain-intrinsic-size, and restores skipping. After the sweep, realization
// is a zero-delta event — scrolling up is seamless with no corrections needed.
// The one-time shifts caused by the sweep's own measuring are corrected by the
// engine before paint (the sweep only runs while the user isn't scrolling, so
// there is no momentum to kill).
//
// Freshness: every measured wrapper is watched by one shared ResizeObserver.
// A skipped wrapper's box IS its placeholder, so the observer firing with a
// height that differs from the stamp means real layout changed — content
// streamed/edited/expanded while realized, or a realization exposed a stale
// stamp — and the stamp is refreshed from the observed box. (Never try to
// infer "is this element realized?" from descendant geometry: children of
// skipped subtrees still report non-zero sizes in Chromium, which is exactly
// the trap that once stamped 150px placeholders as measured heights.)
//
// Stamps are keyed by container width (dataset.cvMeasured = width), so a
// rotation / pane resize / sidebar toggle naturally invalidates everything.

const CHUNK_SIZE_MAX = 12;
const CHUNK_SIZE_MIN = 2;
const CHUNK_BUDGET_MS = 8; // halve the chunk when a forced layout burst exceeds this
const CHUNK_DELAY_MS = 90;
const SCHEDULE_DELAY_MS = 300;
const SCROLL_QUIET_MS = 150;

export type MessageHeightSweeper = {
	/** Request a sweep pass soon (throttled; safe to call from hot paths). */
	schedule: () => void;
	/**
	 * Measure + stamp EVERY unmeasured wrapper synchronously, in one batched
	 * forced layout. For the chat-OPEN settle, while the content is still
	 * opacity-hidden: the async sweep's scroll-quiet gate sees the settle
	 * loop's own per-frame pin writes as "user is scrolling", so it always
	 * deferred until AFTER the reveal — and then each 300ms chunk grew the
	 * placeholders visibly (the "chat jumps around a few times on open"
	 * stutter). Sweeping everything before the reveal makes the settle's
	 * stability check converge on FINAL heights, so nothing moves after.
	 * Bounded by the pagination window (~25 turns rendered at open).
	 */
	sweepAllNow: () => void;
	/** Drop accumulated per-element observations (call on chat switch). */
	reset: () => void;
	destroy: () => void;
};

export const createMessageHeightSweeper = (): MessageHeightSweeper => {
	if (typeof document === 'undefined' || typeof ResizeObserver === 'undefined') {
		return { schedule: () => {}, sweepAllNow: () => {}, reset: () => {}, destroy: () => {} };
	}

	let destroyed = false;
	let scheduleId: ReturnType<typeof setTimeout> | null = null;
	let chunkId: ReturnType<typeof setTimeout> | null = null;
	let lastScrollAt = 0;
	// Adaptive: heavy turns (huge code blocks/tables) make a 12-element forced
	// layout burst too expensive — shrink toward CHUNK_SIZE_MIN when a burst
	// blows the budget, creep back up when bursts are cheap.
	let chunkSize = CHUNK_SIZE_MAX;

	// Non-bubbling scroll still propagates in the capture phase, so one
	// document-level listener sees the messages container (and everything else
	// — over-triggering only delays the sweep, never breaks it).
	const onAnyScroll = () => {
		lastScrollAt = performance.now();
	};
	document.addEventListener('scroll', onAnyScroll, { capture: true, passive: true });

	const stamp = (el: HTMLElement, height: number, widthStamp: string) => {
		const next = `auto ${height}px`;
		if (el.style.containIntrinsicSize !== next) {
			el.style.containIntrinsicSize = next;
		}
		el.dataset.cvMeasured = widthStamp;
	};

	// Shared freshness observer for already-measured wrappers (see header).
	// Restamping a SKIPPED wrapper is a no-op (its box already equals the
	// stamp), and restamping a realized one doesn't change its box — so this
	// can't feed back into itself.
	const freshnessObserver = new ResizeObserver((entries) => {
		if (destroyed) return;
		const container = document.getElementById('messages-container');
		if (!container) return;
		const widthStamp = String(container.clientWidth);
		for (const entry of entries) {
			const el = entry.target as HTMLElement;
			const height = Math.round(
				entry.borderBoxSize?.[0]?.blockSize ?? entry.contentRect.height
			);
			if (height > 0 && el.isConnected) {
				stamp(el, height, widthStamp);
			}
		}
	});

	const runChunk = () => {
		chunkId = null;
		if (destroyed) return;

		const container = document.getElementById('messages-container');
		if (!container) return;
		const width = container.clientWidth;
		if (width === 0) {
			// Transiently unlaid-out (mid-transition) — retry, don't abandon.
			chunkId = setTimeout(runChunk, 500);
			return;
		}

		// Never force layouts while the user (or the streaming pin) is actively
		// scrolling — retry once things settle.
		if (performance.now() - lastScrollAt < SCROLL_QUIET_MS) {
			chunkId = setTimeout(runChunk, SCROLL_QUIET_MS + 60);
			return;
		}

		const widthStamp = String(width);
		const wraps = container.querySelectorAll<HTMLElement>('[data-cv-wrap]');
		const toMeasure: HTMLElement[] = [];
		// Bottom-up: the reader scrolls up into recent history first.
		for (let i = wraps.length - 1; i >= 0 && toMeasure.length < chunkSize; i--) {
			const el = wraps[i];
			if (el.dataset.cvMeasured !== widthStamp) {
				toMeasure.push(el);
			}
		}

		if (toMeasure.length > 0) {
			// Batched: all writes, one forced layout on first read, all writes.
			// Forcing `visible` on a realized wrapper changes nothing; on a
			// skipped one it forces real layout, so offsetHeight is exact for
			// both — no skip-state detection needed.
			const burstStart = performance.now();
			for (const el of toMeasure) el.style.contentVisibility = 'visible';
			const heights = toMeasure.map((el) => el.offsetHeight);
			toMeasure.forEach((el, i) => {
				el.style.contentVisibility = 'auto';
				stamp(el, heights[i], widthStamp);
				freshnessObserver.observe(el);
			});
			const burstMs = performance.now() - burstStart;
			if (burstMs > CHUNK_BUDGET_MS) {
				chunkSize = Math.max(CHUNK_SIZE_MIN, Math.floor(chunkSize / 2));
			} else if (chunkSize < CHUNK_SIZE_MAX) {
				chunkSize += 1;
			}
			// More may remain — keep chipping away.
			chunkId = setTimeout(runChunk, CHUNK_DELAY_MS);
		}
	};

	const schedule = () => {
		if (destroyed || scheduleId !== null || chunkId !== null) return;
		scheduleId = setTimeout(() => {
			scheduleId = null;
			runChunk();
		}, SCHEDULE_DELAY_MS);
	};

	const sweepAllNow = () => {
		if (destroyed) return;
		const container = document.getElementById('messages-container');
		if (!container) return;
		const width = container.clientWidth;
		if (width === 0) return;
		const widthStamp = String(width);
		const wraps = container.querySelectorAll<HTMLElement>('[data-cv-wrap]');
		const toMeasure: HTMLElement[] = [];
		for (const el of wraps) {
			if (el.dataset.cvMeasured !== widthStamp) toMeasure.push(el);
		}
		if (toMeasure.length === 0) return;
		// Same batched write→read→write shape as runChunk: one forced layout
		// for the whole set (the content is hidden during the open settle, so
		// this can't paint anything).
		for (const el of toMeasure) el.style.contentVisibility = 'visible';
		const heights = toMeasure.map((el) => el.offsetHeight);
		toMeasure.forEach((el, i) => {
			el.style.contentVisibility = 'auto';
			stamp(el, heights[i], widthStamp);
			freshnessObserver.observe(el);
		});
	};

	// Chat switch: drop every per-element observation (the old chat's wrappers
	// are detached; without this the observation set grows across every chat
	// visited until GC collects the nodes). The new chat's wrappers re-enroll
	// as the sweep measures them.
	const reset = () => {
		freshnessObserver.disconnect();
	};

	const destroy = () => {
		destroyed = true;
		if (scheduleId !== null) clearTimeout(scheduleId);
		if (chunkId !== null) clearTimeout(chunkId);
		freshnessObserver.disconnect();
		document.removeEventListener('scroll', onAnyScroll, { capture: true } as EventListenerOptions);
	};

	return { schedule, sweepAllNow, reset, destroy };
};
