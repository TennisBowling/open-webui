// True when focusing an input summons an on-screen keyboard (touch devices —
// includes wide iPads that the width-based `$mobile` store misses).
// Programmatic composer `.focus()` calls are gated on this app-wide: every
// surprise keyboard pop-up on iOS/Android traces back to an ungated call made
// inside a tap's gesture context. Only flows where typing is the user's
// unavoidable next step should focus the composer on these devices.
export const isOnScreenKeyboardDevice = (): boolean =>
	typeof window !== 'undefined' && ('ontouchstart' in window || navigator.maxTouchPoints > 0);
