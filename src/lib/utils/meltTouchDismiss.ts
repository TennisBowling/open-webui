// Workaround for a state leak in melt-ui's `useInteractOutside`
// (@melt-ui/svelte 0.76.2, vendored under bits-ui — every DropdownMenu, Popover
// and Select in the app runs it).
//
// How melt decides an "outside" tap should close a surface:
//   pointerdown (document, capture) → isPointerDown = true, and
//     isPointerDownInside = true when the target is inside the surface
//   pointerup (document, capture) → for pointerType 'touch' it does NOT decide
//     immediately; it registers a ONE-SHOT `click` listener and returns, because
//     touch devices fire click well after the finger lifts. That handler is the
//     only thing that ever calls resetPointerState().
//
// A touch *scroll* inside the surface breaks the cycle: the gesture starts
// inside (isPointerDownInside = true), but a drag produces no click — and on iOS
// it usually ends in pointercancel, so melt's pointerup handler may not run at
// all. Either way resetPointerState() never happens and isPointerDownInside is
// left stuck true. The next tap outside is then evaluated as "the pointer went
// down inside", so the surface refuses to close; that tap's real click finally
// runs the stale one-shot handler, which resets the state, and only a THIRD
// gesture closes the menu.
//
// Net effect on a phone: scroll the model list, tap away — nothing happens; tap
// away again — now it closes.
//
// Fix: when a touch gesture that began inside a melt surface ends as a drag (so
// no click is coming), replay the pair of events melt needs to complete its
// state machine — a `pointerup`, which makes it arm the one-shot click listener,
// then the `click` that listener is waiting for. Both are dispatched ON THE
// SURFACE ROOT, so melt's own isValidEvent() sees a target inside the surface,
// declines to treat it as an outside interaction, and falls straight through to
// resetPointerState() — exactly the missing half. Replaying the pointerup as
// well (not just the click) is what covers the iOS path, where the scroll ends
// in pointercancel and melt's pointerup handler never ran, so there is no
// pending click listener to satisfy in the first place.
//
// Both events are dispatched with bubbles:false, so they reach capture-phase
// listeners along the path (melt's) and nothing else: no ancestor handler and no
// item button can mistake them for a real tap and select a model. When melt's
// state is already clean the replay is a no-op — isPointerDown is false, so
// nothing closes.

const SURFACE_SELECTOR =
	'[data-melt-menu-id],[role="menu"],[data-menu-content],[data-melt-popover-content],[role="listbox"]';

// Chrome/Safari suppress the synthesized click once a touch travels past their
// slop radius; ~10px is the conventional threshold and is what melt is
// implicitly relying on.
const TAP_SLOP_PX = 10;

export const initMeltTouchDismiss = (): (() => void) => {
	if (typeof window === 'undefined') return () => {};
	const isTouch = 'ontouchstart' in window || navigator.maxTouchPoints > 0;
	if (!isTouch) return () => {};

	let start: { x: number; y: number; surface: HTMLElement } | null = null;

	const onPointerDown = (e: PointerEvent) => {
		start = null;
		if (!e.isPrimary || e.pointerType !== 'touch') return;
		const target = e.target as HTMLElement | null;
		const surface = target?.closest?.<HTMLElement>(SURFACE_SELECTOR);
		if (!surface) return;
		start = { x: e.clientX, y: e.clientY, surface };
	};

	const finish = (e: PointerEvent) => {
		const gesture = start;
		start = null;
		if (!gesture || !e.isPrimary) return;
		const dragged =
			e.type === 'pointercancel' ||
			Math.hypot(e.clientX - gesture.x, e.clientY - gesture.y) > TAP_SLOP_PX;
		if (!dragged) return; // a real tap: the browser's own click will do the job
		if (!gesture.surface.isConnected) return;
		const { clientX, clientY } = e;
		// Deferred so this lands after any handling the browser is still doing for
		// the real gesture (and after melt's own pointerup handler, when there was
		// one), rather than interleaving with it.
		setTimeout(() => {
			const surface = gesture.surface;
			if (!surface.isConnected) return;
			surface.dispatchEvent(
				new PointerEvent('pointerup', {
					bubbles: false,
					cancelable: true,
					composed: true,
					pointerId: -1,
					pointerType: 'touch',
					isPrimary: true,
					clientX,
					clientY
				})
			);
			surface.dispatchEvent(
				new MouseEvent('click', {
					bubbles: false,
					cancelable: true,
					composed: true,
					clientX,
					clientY
				})
			);
		}, 0);
	};

	document.addEventListener('pointerdown', onPointerDown, true);
	document.addEventListener('pointerup', finish, true);
	document.addEventListener('pointercancel', finish, true);

	return () => {
		document.removeEventListener('pointerdown', onPointerDown, true);
		document.removeEventListener('pointerup', finish, true);
		document.removeEventListener('pointercancel', finish, true);
	};
};
