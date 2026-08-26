// Scroll-stability helpers for in-place message editing.
//
// Editing a message must NOT yank the reader's viewport. Two native behaviors
// fight this and are neutralized here:
//   1. Auto-growing a <textarea> by zeroing its height then reading scrollHeight
//      momentarily collapses the element. The forced reflow can clamp
//      #messages-container.scrollTop upward (a visible per-keystroke jump,
//      worst on Safari which has no scroll-anchoring to compensate).
//   2. Swapping rendered markdown for a tall edit textarea on edit-entry shifts
//      everything below the edited message.
//
// Both are corrected by measuring a stable anchor and restoring it after the
// layout change. These anchors are UNCONDITIONAL: entering edit is an explicit
// act on a specific message, so the edited message owns the viewport for the
// duration — Chat.svelte disengages `autoScroll` the moment any message enters
// edit mode (via the messageEditingIds store), so there is no bottom-pin to
// defer to. (An earlier version no-op'd these anchors when the reader was near
// the bottom to avoid fighting the pin — but editing the LAST message while
// sitting at the bottom is the most common edit of all, and the pin then
// rearranged the view under the editor. That deference was the bug.)
// All corrections here measure the anchor's ACTUAL position and fix only the
// residual, so they compose with the container's scroll-anchoring engine and
// native overflow-anchor without double-correcting.
//
// The no-yank rule above is a DESKTOP rule. Once an on-screen keyboard takes
// the bottom ~40% of the screen, the pre-edit position is the wrong place for
// the edit box (the taller textarea runs straight under the keyboard), so
// `placeEditBoxForKeyboard` supersedes the entry anchor the moment a keyboard
// is involved and top-aligns the edit box in the shrunk viewport instead.

import { isKeyboardOpen } from './keyboardViewport';

const NEAR_BOTTOM_PX = 64; // mirrors Chat.svelte AUTO_SCROLL_REENGAGE_PX
// Keep re-asserting the keyboard edit placement this long: the keyboard-open
// geometry (--app-height, container height) keeps changing for several frames
// while iOS animates the keyboard in. Mirrors Chat.svelte's bottom-pin settle.
const EDIT_PLACE_SETTLE_MS = 450;
// If no keyboard materializes this long after edit entry, none is coming
// (desktop / hardware keyboard) and the entry anchor's no-yank result stands.
const EDIT_PLACE_ARM_TIMEOUT_MS = 1500;
// Breathing room between the container's top edge and the edit box.
const EDIT_PLACE_TOP_PAD_PX = 8;

const getMessagesContainer = (): HTMLElement | null =>
	typeof document === 'undefined' ? null : document.getElementById('messages-container');

const isNearBottom = (el: HTMLElement): boolean =>
	el.scrollHeight - el.scrollTop - el.clientHeight <= NEAR_BOTTOM_PX;

/**
 * Auto-grow an edit textarea to fit its content without letting the brief
 * height collapse clamp the chat scroll position. Restores the pre-resize
 * scrollTop whenever the collapse moved it.
 */
export const autoGrowEditTextarea = (ta: HTMLTextAreaElement | null | undefined): void => {
	if (!ta) return;
	const container = getMessagesContainer();
	const beforeTop = container?.scrollTop;

	ta.style.height = '';
	ta.style.height = `${ta.scrollHeight}px`;

	if (container && beforeTop !== undefined && container.scrollTop !== beforeTop) {
		container.scrollTop = beforeTop;
	}
};

/**
 * Capture the edited message's current on-screen position before edit-entry
 * mutates its height (markdown -> textarea swap, focus, growth). Returns a
 * `restore()` to call after the DOM settles that holds the message at the same
 * screen position.
 */
export const captureEditEntryAnchor = (messageId: string): (() => void) => {
	const container = getMessagesContainer();
	const anchorEl =
		typeof document === 'undefined' ? null : document.getElementById(`message-${messageId}`);
	const beforeTop = anchorEl?.getBoundingClientRect().top ?? 0;

	return () => {
		if (!container || !anchorEl) return;
		const delta = anchorEl.getBoundingClientRect().top - beforeTop;
		if (delta !== 0) container.scrollTop += delta;
	};
};

/**
 * On-screen-keyboard placement for in-place message editing: align the edit
 * box's top with the top of the visible conversation area.
 *
 * The predecessor of this function tried to keep the edited message at its
 * pre-keyboard screen position through the shrink — but a screen position
 * measured against the full-height layout usually doesn't EXIST in the shrunk
 * viewport, so the (taller) edit textarea ended up stranded under the
 * keyboard. An explicit edit tap on mobile means "give me room to edit";
 * top-alignment is the placement that maximizes it and is fully predictable.
 *
 * Call right after the entry anchor restore. Covers both keyboard timelines:
 * already up at edit entry (focus hop from the composer — no keyboard-viewport
 * transition will fire), or arriving ~100-300ms after the entry `focus()`
 * (armed for the next open transition; expires quietly on desktop / hardware
 * keyboards where none comes, leaving the entry anchor's no-yank result).
 * Re-asserts through the keyboard's grow animation, then stands down. A
 * pointer gesture cancels immediately — the user owns the viewport (same
 * gesture-wins invariant as Chat.svelte's disengageAutoScroll).
 *
 * Singleton: a new edit replaces any pending placement, so two edits in quick
 * succession can't both adjust the container's scrollTop and fight.
 */
let cancelActiveEditPlacement: (() => void) | null = null;

export const placeEditBoxForKeyboard = (messageId: string): void => {
	if (typeof window === 'undefined') return;
	cancelActiveEditPlacement?.();

	let rafId: number | null = null;
	let armId: number | undefined;

	const cancel = () => {
		if (cancelActiveEditPlacement === cancel) cancelActiveEditPlacement = null;
		if (armId !== undefined) window.clearTimeout(armId);
		window.removeEventListener('keyboard-viewport', onKeyboardEvent);
		window.removeEventListener('pointerdown', cancel, true);
		if (rafId !== null) cancelAnimationFrame(rafId);
	};

	const align = (): boolean => {
		const container = getMessagesContainer();
		// The textarea disappearing means the edit was saved or cancelled.
		const editBox = document
			.getElementById(`message-edit-${messageId}`)
			?.closest('.message-edit-box');
		if (!container || !editBox) return false;
		const delta =
			editBox.getBoundingClientRect().top -
			(container.getBoundingClientRect().top + EDIT_PLACE_TOP_PAD_PX);
		// scrollTop clamps at the ends natively: a bottom-of-chat edit gets as
		// close to the top as the content below it allows.
		if (delta !== 0) container.scrollTop += delta;
		return true;
	};

	const settle = () => {
		const start = performance.now();
		const step = () => {
			rafId = null;
			if (!align() || performance.now() - start >= EDIT_PLACE_SETTLE_MS) {
				cancel();
				return;
			}
			rafId = requestAnimationFrame(step);
		};
		step();
	};

	const onKeyboardEvent = (event: Event) => {
		if (!(event as CustomEvent).detail?.open) return;
		window.removeEventListener('keyboard-viewport', onKeyboardEvent);
		if (armId !== undefined) window.clearTimeout(armId);
		settle();
	};

	cancelActiveEditPlacement = cancel;
	window.addEventListener('pointerdown', cancel, true);

	if (isKeyboardOpen()) {
		settle();
	} else {
		window.addEventListener('keyboard-viewport', onKeyboardEvent);
		armId = window.setTimeout(cancel, EDIT_PLACE_ARM_TIMEOUT_MS);
	}
};

/**
 * Should a one-shot "scroll the new/selected turn into view" run? Only when the
 * reader is already at the bottom (a fresh submit, or branch-switching while
 * tailing). When they've scrolled up to read/edit, suppress it so we don't yank
 * them away from where they're looking.
 */
export const shouldScrollIntoViewOnRender = (): boolean => {
	const container = getMessagesContainer();
	return container ? isNearBottom(container) : true;
};
