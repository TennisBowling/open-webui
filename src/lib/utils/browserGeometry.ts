export interface Rect {
	x: number;
	y: number;
	w: number;
	h: number;
}

/**
 * Pure helpers for mapping panel gestures onto daemon viewport coordinates.
 *
 * The verification image renders with `object-contain`, so the drawn image can
 * be letterboxed inside its element. Mapping through the element box alone (the
 * old code) sent letterbox clicks to the wrong page coordinates — and the
 * daemon clamps out-of-range points to the viewport edge, so a near-edge click
 * landed nowhere near the cursor. Every mapping now goes through the FIT rect.
 */

/** The drawn-image rect inside an object-contain element box, or null. */
export const fitRect = (
	containerW: number,
	containerH: number,
	imgW: number,
	imgH: number
): Rect | null => {
	if (containerW <= 0 || containerH <= 0 || imgW <= 0 || imgH <= 0) return null;
	const scale = Math.min(containerW / imgW, containerH / imgH);
	const w = imgW * scale;
	const h = imgH * scale;
	return { x: (containerW - w) / 2, y: (containerH - h) / 2, w, h };
};

/**
 * Map a client-space pointer event to image-pixel coordinates (which equal the
 * daemon's viewport CSS pixels, since screenshots are 1:1 with the viewport).
 * Returns null for clicks in the letterbox (outside the drawn image).
 */
export const mapClientToImage = (
	clientX: number,
	clientY: number,
	elementRect: DOMRect,
	fit: Rect,
	naturalWidth: number,
	naturalHeight: number
): { x: number; y: number } | null => {
	const px = clientX - elementRect.left;
	const py = clientY - elementRect.top;
	if (px < fit.x || py < fit.y || px > fit.x + fit.w || py > fit.y + fit.h) return null;
	return {
		x: ((px - fit.x) / fit.w) * naturalWidth,
		y: ((py - fit.y) / fit.h) * naturalHeight
	};
};

export const DRAG_THRESHOLD_PX = 6;

/** A pointer sequence longer than the threshold is a drag; shorter is a tap. */
export const isDragGesture = (
	start: { x: number; y: number } | null,
	end: { x: number; y: number },
	threshold: number = DRAG_THRESHOLD_PX
): boolean => {
	if (!start) return false;
	return Math.hypot(end.x - start.x, end.y - start.y) >= threshold;
};
