// iOS Safari (browser tab AND standalone PWA) never resizes the layout
// viewport for the on-screen keyboard — it ignores
// interactive-widget=resizes-content, 100dvh stays full-height, and env(
// safe-area-inset-bottom) keeps its home-indicator value even though the
// keyboard is covering it. Instead, on focus Safari *pans* the page to
// reveal the input, which scrolls the navbar (and its model selector)
// offscreen and leaves the composer floating above a dead safe-area gap.
//
// This module tracks window.visualViewport and, while a keyboard is open:
//  - publishes the truly visible height as --app-height on <html> and adds
//    the `keyboard-open` class (app.css shrinks screen-sized containers and
//    zeroes bottom safe-area padding under that class), so the header stays
//    put, the composer sits flush on the keyboard, and the message list is
//    the only thing that scrolls;
//  - publishes the visible area's top edge as --app-offset-top (app.css
//    shifts <body> down by it). Safari's focus-reveal pan can leave the
//    visual viewport offset below the layout viewport's top, and with the
//    document unscrollable (html is overflow-hidden and the app is sized to
//    the visible area) window.scrollTo(0,0) is a same-position no-op that
//    never clears that offset — so the app used to render offsetTop px above
//    the visible area, floating the composer above the keyboard until a
//    manual drag panned the viewport back. Following the pan instead of
//    fighting it keeps the app glued to the visible pixels wherever iOS
//    leaves the viewport. Genuine layout-viewport scroll (scrollY) IS
//    programmatically resettable and still gets reset; the published offset
//    folds it in for any frame where the reset hasn't landed.
//
// Android Chrome honors interactive-widget=resizes-content (innerHeight and
// visualViewport shrink together, so the measured inset stays ~0) and is
// deliberately untouched by this. A `keyboard-viewport` CustomEvent is
// dispatched on open/close transitions so the chat can re-pin its
// bottom-anchored scroll position.
//
// State invariant: keyboard-open ⇔ (visual viewport shrunk past the
// threshold) AND (an editable element is focused). An on-screen keyboard
// cannot be up without a focused editable, so requiring both means a missed
// visualViewport event can no longer strand the UI in typing mode: focus
// events re-drive the measurement, and a watchdog re-measures even when every
// event is missed. Previously the class was cleared exclusively by a
// visualViewport resize — if iOS dropped that event (focused node swapped out
// mid-render, app backgrounded, ...) the collapsed navbar/token-panel state
// persisted until a full reload.
//
// The measurement is DRIVEN, not awaited. iOS reports keyboard geometry on its
// own schedule: events can arrive once and early (before the keyboard has
// finished moving), or not until the next touch, and frame callbacks can go
// unserved while Safari animates the keyboard in and pans the page. Any of
// those leaves the app in its pre-keyboard layout — full height, navbar and
// typing-mode exit pushed above the visible band, composer floating over dead
// background — until an unrelated gesture happens to shake a measurement
// loose, which is exactly the "drag the page and it snaps into place" bug this
// file keeps re-earning. So: focus arms a burst of re-measures across the
// animation, a watchdog runs for as long as an editable is focused (not merely
// while a keyboard has been detected), input events re-measure because typing
// moves the QuickType bar, and every scheduled measurement has a timer
// fallback behind its animation frame.

// Must be high enough to reject a PHANTOM inset. iOS regularly leaves
// visualViewport.height short by roughly an accessory-bar height (~44-55px)
// with nothing actually covering the screen — after the keyboard is dismissed by
// destroying its focused element (opening another chat from the sidebar is the
// common way in), and whenever a hardware keyboard shows only the shortcuts bar.
// At the old 50px that counted as "keyboard up": the navbar collapsed to its
// slim shade and the app was sized ~55px short of the viewport, leaving a dead
// strip of page background below it, until a real keyboard open/close cycle
// reset the measurement.
//
// 120px sits clear of both ends: every real software keyboard is bigger (the
// smallest, an iPhone SE in landscape, is ~162px; iPhone portrait with QuickType
// is ~336px) and every accessory/shortcuts bar is smaller. Below the threshold
// nothing is published at all, so the app just stays at its natural full height
// — which is the right answer for an inset that isn't really there.
const KEYBOARD_MIN_INSET_PX = 120;
// Armed whenever an editable is focused OR the keyboard is up — i.e. across the
// whole window in which iOS can change the viewport under us. It is the backstop
// for every way the browser can fail to tell us: a dropped visualViewport event,
// a frame callback that never runs while Safari animates the keyboard in, or
// geometry that is simply stale until the next interaction. 300ms is fast enough
// that a missed transition heals before it reads as a bug and cheap enough to run
// continuously — a measurement is a handful of property reads and writes nothing
// unless a value actually changed.
const WATCHDOG_INTERVAL_MS = 300;
// Backstop for a requestAnimationFrame that never runs (see update()).
const MEASURE_FALLBACK_MS = 120;
// Explicit re-measures spanning the keyboard's open animation. iOS emits its
// viewport events on its own schedule — sometimes only once, before the keyboard
// has finished moving, sometimes not until the next touch — so the app drives the
// transition itself instead of waiting to be told about it.
const FOCUS_BURST_MS = [50, 120, 250, 400, 600, 850, 1150, 1500];
const DISMISS_TAP_MAX_MOVEMENT_PX = 12;
const DISMISS_TAP_MAX_DURATION_MS = 600;

type KeyboardViewportInstance = {
	isOpen: () => boolean;
	dismiss: () => void;
};

let instance: KeyboardViewportInstance | null = null;

/** True while the on-screen keyboard is considered open. */
export const isKeyboardOpen = (): boolean => instance?.isOpen() ?? false;

/**
 * Actively dismiss the on-screen keyboard and typing mode: blurs the focused
 * editable (which closes the keyboard) and force-reconciles the state so the
 * UI restores even if the browser never delivers the closing viewport events.
 * Safe to call any time; no-op when nothing is open.
 */
export const dismissKeyboard = (): void => {
	instance?.dismiss();
};

const getFocusedEditable = (): HTMLElement | null => {
	let el = document.activeElement as HTMLElement | null;
	// Descend through shadow roots to the actually-focused node — the host
	// document only sees the shadow HOST as activeElement.
	while (el?.shadowRoot?.activeElement) {
		el = el.shadowRoot.activeElement as HTMLElement;
	}
	if (!el || el === document.body || el === document.documentElement) return null;
	// Focus inside an iframe is opaque from here (activeElement is the IFRAME
	// element itself). It may well hold a focused text field (data-viz widgets
	// do), so assume editable and let the viewport-inset half of the invariant
	// decide — otherwise a keyboard summoned from inside an iframe would never
	// engage the shrink-to-visible layout.
	if (el.tagName === 'IFRAME') return el;
	if (el.isContentEditable) return el;
	const tag = el.tagName;
	if (tag === 'TEXTAREA') return el;
	if (tag === 'INPUT') {
		const type = (el as HTMLInputElement).type;
		// Excluded types either take no keyboard at all or summon iOS wheel
		// pickers (date/time family) — typing mode is wrong for a picker even
		// though it also shrinks the visual viewport.
		return [
			'button',
			'checkbox',
			'radio',
			'range',
			'submit',
			'reset',
			'file',
			'color',
			'image',
			'date',
			'datetime-local',
			'month',
			'week',
			'time'
		].includes(type)
			? null
			: el;
	}
	return null;
};

export const initKeyboardViewport = (): (() => void) => {
	const vv = window.visualViewport;
	const isTouch = 'ontouchstart' in window || navigator.maxTouchPoints > 0;
	if (!vv || !isTouch) return () => {};

	const root = document.documentElement;
	let keyboardOpen = false;
	let lastAppHeight = '';
	let lastAppOffset = '';
	let rafId: number | null = null;
	let fallbackId: ReturnType<typeof setTimeout> | null = null;
	let watchdogId: ReturnType<typeof setInterval> | null = null;
	let dismissFallbackId: ReturnType<typeof setTimeout> | null = null;
	let burstIds: ReturnType<typeof setTimeout>[] = [];
	// The layout viewport's height with nothing focused — see measure(), signal 2.
	let baselineInnerHeight = window.innerHeight;
	// Excluding Android rather than requiring an Apple vendor string: signal 2 is
	// CONFIRMED to be the one that fires on the reporter's iPhone, so the test
	// must not be able to accidentally exclude it (vendor strings vary by
	// browser and wrapper; "not Android" cannot).
	const isAndroid = /Android/i.test(navigator.userAgent ?? '');

	// The watchdog is armed for the whole "a keyboard could be up" window, not
	// just once one has been detected: the failure this module exists to prevent
	// is a MISSED transition, and the one that hurts is the opening one — the app
	// keeps its full height while iOS pans the page to reveal the composer, so
	// the navbar (and its typing-mode exit) sits above the visible band and the
	// composer floats over a strip of dead background until something happens to
	// re-measure. Waiting for the user to drag the page is not a heal.
	const syncWatchdog = (editableFocused: boolean) => {
		const wanted = keyboardOpen || editableFocused;
		if (wanted && watchdogId === null) {
			watchdogId = setInterval(update, WATCHDOG_INTERVAL_MS);
		} else if (!wanted && watchdogId !== null) {
			clearInterval(watchdogId);
			watchdogId = null;
		}
	};

	const clearBurst = () => {
		for (const id of burstIds) clearTimeout(id);
		burstIds = [];
	};

	const setState = (open: boolean, inset: number) => {
		if (open === keyboardOpen) return;
		keyboardOpen = open;
		root.classList.toggle('keyboard-open', open);
		// Typing mode owns the page's gestures: only real scrollers move (see
		// bindDragContainment). Safe to call before the const initializes — every
		// path into setState goes through a rAF'd measure().
		bindDragContainment(open);
		if (!open) {
			root.style.removeProperty('--app-height');
			root.style.removeProperty('--app-offset-top');
			lastAppHeight = '';
			lastAppOffset = '';
		}
		window.dispatchEvent(new CustomEvent('keyboard-viewport', { detail: { open, inset } }));
	};

	const measure = () => {
		// Whichever of the two schedulers got here first wins; drop the other so a
		// stale one can't re-run this immediately or keep update() coalescing.
		if (rafId !== null) {
			cancelAnimationFrame(rafId);
			rafId = null;
		}
		if (fallbackId !== null) {
			clearTimeout(fallbackId);
			fallbackId = null;
		}

		const focused = getFocusedEditable();
		const zoomed = vv.scale > 1;
		const innerHeight = window.innerHeight;

		// While nothing is focused there is no keyboard, so whatever the layout
		// viewport measures right now is this device/orientation's full height.
		// Remembering it is what lets the SECOND signal below work at all.
		if (!focused && !zoomed) baselineInnerHeight = innerHeight;

		// Signal 1 — the classic iOS overlay keyboard: the layout viewport keeps
		// its full height and only the VISUAL viewport shrinks.
		// A shrunken visual viewport at scale > 1 is pinch-zoom, not a keyboard.
		const insetFromViewport = zoomed ? 0 : Math.round(innerHeight - vv.height);
		// Signal 2 — a keyboard that took its space out of the LAYOUT viewport
		// (interactive-widget=resizes-content honoured, and whatever iOS is doing
		// in a standalone PWA when it reports the two heights in lockstep). Signal
		// 1 is blind to it: both numbers shrink together, so their difference is
		// ~0 and the app concludes there is no keyboard while sitting behind one.
		// Measured against the remembered unfocused height instead.
		//
		// Everywhere except Android. Android Chrome resizes the layout for its
		// keyboard by design and lays out correctly as a result — it has never
		// needed typing mode and collapsing its chrome now would be an unasked-for
		// change. Its URL bar also shows and hides on scroll, which moves
		// innerHeight by something not far off the threshold.
		const layoutShrink =
			zoomed || isAndroid || !baselineInnerHeight
				? 0
				: Math.round(baselineInnerHeight - innerHeight);

		const inset = Math.max(insetFromViewport, layoutShrink);
		const open = inset > KEYBOARD_MIN_INSET_PX && focused !== null;
		let visibleHeightChanged = false;

		if (open) {
			// Whichever viewport is smaller bounds what is actually on screen —
			// correct for both signals (with signal 1 the visual viewport is the
			// short one; with signal 2 they agree, or vv is the stale one).
			// Only touch the style when the value actually changed — Safari fires
			// a stream of visualViewport events during the keyboard animation and
			// an unconditional write forces a style recalc for each one.
			const appHeight = `${Math.round(Math.min(vv.height, innerHeight))}px`;
			if (appHeight !== lastAppHeight) {
				root.style.setProperty('--app-height', appHeight);
				lastAppHeight = appHeight;
				visibleHeightChanged = true;
			}
			// Layout-viewport scroll is the one half of Safari's focus pan that a
			// programmatic scroll CAN undo; reset it before reading the offset so
			// the value published below is post-reset (scrollTo is synchronous).
			if (window.scrollY !== 0) {
				window.scrollTo(0, 0);
			}
			// The visual-viewport half of the pan is not resettable from script —
			// follow it instead: track the visible area's top edge (scrollY stays in
			// the sum for any frame where the reset above was ignored)
			// and let app.css shift <body> onto the visible pixels.
			const appOffset = `${Math.max(0, Math.round(window.scrollY + vv.offsetTop))}px`;
			if (appOffset !== lastAppOffset) {
				root.style.setProperty('--app-offset-top', appOffset);
				lastAppOffset = appOffset;
			}
		}

		setState(open, inset);

		// The body glue above deliberately neutralizes iOS's native pan-to-reveal
		// (the pan moves the visible band, the glue moves the app with it — net
		// zero), so keeping the focused element on screen is now this module's
		// job. Whenever the visible height changes while a keyboard is up (open
		// transition, keyboard grow animation frames, QuickType bar toggles),
		// minimally scroll the focused element's scroll ancestors to reveal it.
		// Runs AFTER setState so layout already reflects the keyboard-open class
		// and any deliberate placement done by keyboard-viewport listeners
		// (editScroll.ts top-aligns the message-edit box) — `nearest` is a no-op
		// for anything already visible, so it never fights those.
		if (open && visibleHeightChanged) {
			focused?.scrollIntoView({ block: 'nearest', inline: 'nearest' });
		}

		syncWatchdog(focused !== null);
	};

	// Coalesce the event storm (visualViewport resize+scroll and window scroll
	// all fire per animation frame during the keyboard transition, and our own
	// scrollTo above re-triggers scroll) into at most one measure per frame.
	const update = () => {
		if (rafId === null) {
			rafId = requestAnimationFrame(measure);
		}
		// rAF alone is not a guarantee here. Frame callbacks can go unserved while
		// Safari animates the keyboard in and pans the page — and because rafId
		// stays armed until its callback runs, every later event would coalesce
		// into a frame that never comes, leaving the app in its pre-keyboard layout
		// until an unrelated interaction finally shook a frame loose (the "drag the
		// page and it snaps into typing mode" bug). A timer can't be starved by the
		// same thing; whichever fires first cancels the other.
		if (fallbackId === null) {
			fallbackId = setTimeout(measure, MEASURE_FALLBACK_MS);
		}
	};

	// Re-measure across the keyboard's whole opening animation rather than only
	// when the browser volunteers an event. Re-armed on every focus change, so
	// hopping between inputs (composer -> message edit box) re-drives it too.
	const startFocusBurst = () => {
		clearBurst();
		burstIds = FOCUS_BURST_MS.map((ms) => setTimeout(update, ms));
	};

	const dismiss = () => {
		const active = document.activeElement as HTMLElement | null;
		if (
			active &&
			(active.tagName === 'INPUT' || active.tagName === 'TEXTAREA' || active.isContentEditable)
		) {
			active.blur();
		}
		update();
		// Belt-and-braces: if the closing viewport events never arrive, the
		// re-measure above (and this delayed one) still observe "no editable
		// focused" and clear the state directly.
		if (dismissFallbackId !== null) clearTimeout(dismissFallbackId);
		dismissFallbackId = setTimeout(() => {
			dismissFallbackId = null;
			update();
		}, 350);
	};

	vv.addEventListener('resize', update);
	vv.addEventListener('scroll', update);
	// Safari pans the layout viewport itself on focus in some configurations.
	window.addEventListener('scroll', update, { passive: true });
	// Orientation changes and (on browsers that resize the layout viewport for
	// the keyboard) the keyboard itself land here rather than on visualViewport.
	window.addEventListener('resize', update);
	// Focus transitions re-drive the measurement: focusout with no successor
	// editable means the keyboard is closing whether or not the viewport
	// events arrive (focusout fires before the sibling focusin, and both run
	// before our rAF'd measure — so an input-to-input hop never flickers).
	// A focus change is also the one moment we KNOW a keyboard transition is
	// about to happen, so it additionally arms the burst of re-measures that
	// carries the app through the animation without depending on iOS events.
	const onFocusChange = () => {
		update();
		startFocusBurst();
	};
	document.addEventListener('focusin', onFocusChange, true);
	document.addEventListener('focusout', onFocusChange, true);
	// Typing itself changes the keyboard's geometry: iOS swaps the QuickType /
	// AutoFill accessory bar in and out as soon as there is text, moving the top
	// of the keyboard by ~40px with no guarantee of a viewport event. Measuring on
	// input keeps the app's height in step with the keystroke that caused it
	// instead of a fifth of a second later, which is the difference between the
	// conversation resizing and the conversation visibly re-adjusting itself.
	document.addEventListener('input', update, { capture: true, passive: true });
	// Returning from the app switcher / bfcache: iOS may have closed the
	// keyboard while hidden without emitting anything.
	const onVisible = () => {
		if (!document.hidden) update();
	};
	document.addEventListener('visibilitychange', onVisible);
	window.addEventListener('pageshow', update);

	// Tap-to-dismiss. Standalone iOS PWAs have no Safari "Done" bar, and iOS
	// does NOT blur a focused input when you tap static content — so once the
	// keyboard (and with it the app's typing mode) is up, there is no obvious
	// way out. Make the natural gestures work: a tap on the conversation, or
	// on the slim strip where the navbar was, drops the keyboard and restores
	// the full UI.
	//
	// Detection is pointer-based tap synthesis (primary pointer, <12px travel,
	// <600ms) instead of `click`: iOS suppresses click on scroll-container
	// touches often enough that the old click-only path made the navbar shade
	// feel dead. Scrolls still never dismiss (iOS fires pointercancel when a
	// drag becomes a scroll, and the travel guard catches the rest); long-press
	// text selection is protected by the duration guard. Scoped to explicit
	// dismiss zones so taps in the composer, menus, modals, or sidebar never
	// yank the keyboard.
	let tapStart: { x: number; y: number; t: number; inZone: boolean } | null = null;
	const onPointerDown = (e: PointerEvent) => {
		if (!e.isPrimary) {
			tapStart = null;
			return;
		}
		const target = e.target as HTMLElement | null;
		tapStart = {
			x: e.clientX,
			y: e.clientY,
			t: performance.now(),
			inZone: Boolean(target?.closest?.('#messages-container, #chat-navbar'))
		};
	};
	const onPointerUp = (e: PointerEvent) => {
		const start = tapStart;
		tapStart = null;
		// Any released gesture is also a re-measure point, whatever it was for. On
		// iOS a touch is often what finally makes the viewport report its real
		// geometry, and the end of one is the moment the user is looking for the
		// layout to be right. The watchdog would get there within its interval;
		// this makes it the same frame.
		update();
		if (!keyboardOpen || !start || !start.inZone || !e.isPrimary) return;
		if (performance.now() - start.t > DISMISS_TAP_MAX_DURATION_MS) return;
		if (Math.hypot(e.clientX - start.x, e.clientY - start.y) > DISMISS_TAP_MAX_MOVEMENT_PX) return;
		// Don't dismiss when the tap is interacting with an editable (double-tap
		// word selection and caret taps in a message-edit textarea fire here —
		// blurring would kill native selection), moving focus INTO an editable
		// (e.g. an input inside the messages area, like an ask-user card),
		// inside a surface that opted out via data-kb-keep (edit-mode toolbars),
		// or on an INTERACTIVE element (links, copy buttons, expand toggles):
		// blurring on pointerup starts the keyboard-close reflow BEFORE the
		// browser synthesizes the click, which on iOS can retarget or drop the
		// click entirely — the tap must perform its action, and content
		// interaction isn't a dismissal request. (The navbar .kb-expand button
		// dismisses via its own on:click, unaffected by this exemption.)
		// Blank-space taps on the conversation still dismiss.
		const target = e.target as HTMLElement | null;
		if (
			target &&
			target.closest(
				'input, textarea, [contenteditable="true"], [contenteditable=""], [data-kb-keep], a, button, [role="button"], summary, label, select, audio, video'
			) !== null
		) {
			return;
		}
		dismiss();
	};
	const onPointerCancel = () => {
		tapStart = null;
	};
	document.addEventListener('pointerdown', onPointerDown, true);
	document.addEventListener('pointerup', onPointerUp, true);
	document.addEventListener('pointercancel', onPointerCancel, true);

	// ---- Drag containment (typing mode only) --------------------------------
	// With the keyboard up the document has nothing to scroll: <html> is
	// overflow-hidden and the app is sized to the visible band. A drag that
	// starts on a surface with no scrollable ancestor — the composer's chrome,
	// the clearance strip under it, the gutters beside the bubble, the navbar
	// shade — therefore has nothing to move, and iOS answers it by panning the
	// VISUAL viewport instead: the app slides under the finger, the
	// --app-offset-top glue follows the pan (by design — see measure()), and
	// the whole thing snaps back when the finger lifts. It reads as the page
	// wobbling whenever you touch near the input.
	//
	// Cancel the browser default for exactly those drags, so the conversation
	// is the only thing that moves while typing. A gesture is left alone the
	// moment it has something real to scroll: the message list, a long draft
	// inside #chat-input-container, the horizontal toolbar rail, an open menu,
	// modal or sidebar. preventDefault only suppresses the browser's own pan —
	// app-level touchmove handlers (the sidebar's swipe-to-close pan) still run
	// and still work.
	let drag: { x: number; y: number; blocked: boolean | null } | null = null;

	// Is there a scroll container between the touch target and the app root that
	// can take a gesture on this axis? Position within the scroller deliberately
	// does NOT matter: a list that is merely AT its end keeps its native bounce
	// (bounded by its own overscroll-behavior), which is the iOS feel we want.
	const hasScrollerOnAxis = (target: Element | null, horizontal: boolean): boolean => {
		let node: Element | null = target;
		while (node && node !== root) {
			if (node instanceof HTMLElement) {
				const style = getComputedStyle(node);
				const overflow = horizontal ? style.overflowX : style.overflowY;
				if (overflow === 'auto' || overflow === 'scroll') {
					const slack = horizontal
						? node.scrollWidth - node.clientWidth
						: node.scrollHeight - node.clientHeight;
					if (slack > 1) return true;
				}
			}
			node = node.parentElement;
		}
		return false;
	};

	// The verdict has to be reached on the first move that has any direction at
	// all (later preventDefault calls are ignored once a pan has begun), and the
	// first move is a 1-2px sample — far too noisy to read as an axis. Treating
	// an ambiguous sample as "the dominant axis" is how a horizontal flick along
	// the composer's toolbar rail got cancelled outright whenever its first pixel
	// happened to travel down: the whole gesture was already decided. So only a
	// clearly one-directional sample is taken at its word; anything diagonal
	// defers to either axis having somewhere to go.
	const gestureHasScroller = (target: Element | null, dx: number, dy: number): boolean => {
		const ax = Math.abs(dx);
		const ay = Math.abs(dy);
		if (ax >= ay * 2) return hasScrollerOnAxis(target, true);
		if (ay >= ax * 2) return hasScrollerOnAxis(target, false);
		return hasScrollerOnAxis(target, true) || hasScrollerOnAxis(target, false);
	};

	const onTouchStart = (e: TouchEvent) => {
		drag =
			e.touches.length === 1
				? { x: e.touches[0].clientX, y: e.touches[0].clientY, blocked: null }
				: null;
	};

	const onTouchMove = (e: TouchEvent) => {
		if (!keyboardOpen || !drag) return;
		// Pinch-zoom and other multi-touch gestures are never ours to cancel.
		if (e.touches.length !== 1) {
			drag = null;
			return;
		}
		if (drag.blocked === null) {
			const dx = e.touches[0].clientX - drag.x;
			const dy = e.touches[0].clientY - drag.y;
			// Decide on the FIRST move that has a direction at all. Waiting for a
			// larger threshold would be too late: once iOS has begun a pan, later
			// preventDefault calls are ignored (the event stops being cancelable).
			if (dx === 0 && dy === 0) return;
			// Never fight a native selection drag — the handles produce touchmoves
			// over an editable that has nothing to scroll, which would otherwise
			// look exactly like a dead drag.
			const selection = window.getSelection();
			const selecting = Boolean(selection && !selection.isCollapsed && selection.rangeCount > 0);
			drag.blocked = !selecting && !gestureHasScroller(e.target as Element | null, dx, dy);
		}
		if (drag.blocked && e.cancelable) e.preventDefault();
	};

	const onTouchEnd = () => {
		drag = null;
	};

	// Registered only while the keyboard is up. A permanently non-passive
	// document-level touchmove listener would put the handler on the critical
	// path of every scroll in the app, streaming included.
	const bindDragContainment = (bind: boolean) => {
		if (bind) {
			document.addEventListener('touchstart', onTouchStart, { capture: true, passive: true });
			document.addEventListener('touchmove', onTouchMove, { capture: true, passive: false });
			document.addEventListener('touchend', onTouchEnd, { capture: true, passive: true });
			document.addEventListener('touchcancel', onTouchEnd, { capture: true, passive: true });
		} else {
			drag = null;
			document.removeEventListener('touchstart', onTouchStart, true);
			document.removeEventListener('touchmove', onTouchMove, true);
			document.removeEventListener('touchend', onTouchEnd, true);
			document.removeEventListener('touchcancel', onTouchEnd, true);
		}
	};

	instance = {
		isOpen: () => keyboardOpen,
		dismiss
	};

	return () => {
		if (rafId !== null) cancelAnimationFrame(rafId);
		if (fallbackId !== null) clearTimeout(fallbackId);
		if (watchdogId !== null) clearInterval(watchdogId);
		if (dismissFallbackId !== null) clearTimeout(dismissFallbackId);
		clearBurst();
		vv.removeEventListener('resize', update);
		vv.removeEventListener('scroll', update);
		window.removeEventListener('scroll', update);
		window.removeEventListener('resize', update);
		document.removeEventListener('focusin', onFocusChange, true);
		document.removeEventListener('focusout', onFocusChange, true);
		document.removeEventListener('input', update, true);
		document.removeEventListener('visibilitychange', onVisible);
		window.removeEventListener('pageshow', update);
		document.removeEventListener('pointerdown', onPointerDown, true);
		document.removeEventListener('pointerup', onPointerUp, true);
		document.removeEventListener('pointercancel', onPointerCancel, true);
		bindDragContainment(false);
		root.classList.remove('keyboard-open');
		root.style.removeProperty('--app-height');
		root.style.removeProperty('--app-offset-top');
		instance = null;
	};
};
