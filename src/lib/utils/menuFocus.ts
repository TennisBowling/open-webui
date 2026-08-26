// Every bits-ui / melt dropdown wraps its surface in a focus-trap, and
// focus-trap's default initial target is the first *tabbable* node inside that
// surface. Several of our menus lead with a search field (the model picker, the
// emoji picker, the regenerate menu), so on a touch device merely *opening* the
// menu summoned the on-screen keyboard.
//
// On iOS that is not a cosmetic annoyance, it cascades:
//   1. the keyboard opens, so keyboardViewport.ts sets html.keyboard-open;
//   2. typing mode collapses #chat-navbar to a slim shade — and the navbar owns
//      the model picker's trigger, i.e. the popper's anchor element;
//   3. floating-ui re-measures a now-0x0 anchor and flings the menu to the
//      top-left corner, detached from anything;
//   4. the keyboard eats the bottom half of the screen, and the reflow between
//      pointerdown and click makes iOS drop the synthesized click — so the
//      first tap outside only dismisses the keyboard and a second is needed to
//      actually close the menu.
//
// The fix is to let the trap activate (it must keep a valid focus target) but
// bounce that *automatic* focus off the text field onto the menu container
// itself. The container is already tabindex="-1" and is one of the trap's own
// containers, so focus-trap stays satisfied and never yanks focus back. A
// deliberate tap or keypress on the field still focuses it and still opens the
// keyboard — searching a long model list on a phone keeps working.
//
// Pointer-coarse only: on desktop the auto-focused search field is exactly the
// behaviour you want, and there is no on-screen keyboard to summon.

const SURFACE_SELECTOR =
	'[role="menu"],[role="dialog"],[role="listbox"],[data-menu-content],[data-melt-popover-content]';

const isTouchPrimary = () =>
	typeof window !== 'undefined' &&
	typeof window.matchMedia === 'function' &&
	window.matchMedia('(pointer: coarse)').matches;

/**
 * Svelte action for a text field that sits inside a popper surface: suppresses
 * the focus-trap's automatic focus on touch devices without blocking the user's
 * own taps. No-op on pointer-fine devices.
 */
export const noAutoKeyboardFocus = (node: HTMLInputElement | HTMLTextAreaElement) => {
	if (!isTouchPrimary()) return {};

	// Set by the user's own gesture (tap / physical key) so we can tell a
	// deliberate focus from the trap's automatic one. The action is destroyed
	// with the surface, so this resets every time the menu reopens.
	let userInitiated = false;

	const markUserInitiated = () => {
		userInitiated = true;
	};

	const onFocus = () => {
		if (userInitiated) return;
		const surface = node.closest<HTMLElement>(SURFACE_SELECTOR);
		// Fall back to blurring when there is no enclosing surface to park focus
		// on — better a focusless menu than an unwanted keyboard.
		if (surface) {
			surface.focus({ preventScroll: true });
		} else {
			node.blur();
		}
	};

	// Capture phase: the flag has to be set before the browser dispatches focus.
	node.addEventListener('pointerdown', markUserInitiated, true);
	node.addEventListener('mousedown', markUserInitiated, true);
	node.addEventListener('keydown', markUserInitiated, true);
	node.addEventListener('focus', onFocus);

	return {
		destroy() {
			node.removeEventListener('pointerdown', markUserInitiated, true);
			node.removeEventListener('mousedown', markUserInitiated, true);
			node.removeEventListener('keydown', markUserInitiated, true);
			node.removeEventListener('focus', onFocus);
		}
	};
};
