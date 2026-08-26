import { describe, expect, it } from 'vitest';
import { DRAG_THRESHOLD_PX, fitRect, isDragGesture, mapClientToImage } from './browserGeometry';

describe('fitRect', () => {
	it('returns null for degenerate boxes', () => {
		expect(fitRect(0, 100, 800, 600)).toBeNull();
		expect(fitRect(100, 0, 800, 600)).toBeNull();
		expect(fitRect(100, 100, 0, 600)).toBeNull();
	});

	it('letterboxes a wide image into a tall container', () => {
		const fit = fitRect(200, 200, 400, 100)!;
		expect(fit).toEqual({ x: 0, y: 75, w: 200, h: 50 });
	});

	it('letterboxes a tall image into a wide container', () => {
		const fit = fitRect(200, 200, 100, 400)!;
		expect(fit).toEqual({ x: 75, y: 0, w: 50, h: 200 });
	});

	it('fills the container when aspect ratios match', () => {
		const fit = fitRect(200, 200, 800, 800)!;
		expect(fit).toEqual({ x: 0, y: 0, w: 200, h: 200 });
	});
});

describe('mapClientToImage', () => {
	const elementRect = { left: 10, top: 20, width: 200, height: 200 } as DOMRect;

	it('maps a point inside the fit rect to image pixels', () => {
		// 400x100 image drawn in a 200x200 box => fit {x:0, y:75, w:200, h:50}
		const fit = fitRect(200, 200, 400, 100)!;
		const pt = mapClientToImage(110, 120, elementRect, fit, 400, 100);
		// client (110,120) -> element (100,100) -> fit (100,25) -> img (200,50)
		expect(pt).toEqual({ x: 200, y: 50 });
	});

	it('rejects clicks in the letterbox', () => {
		// fit for a 400x100 image in a 200x200 box: {x:0, y:75, w:200, h:50}
		// element rect starts at client (10,20), so drawn image spans client
		// y 95..145; anything above or below that is letterbox.
		const fit = fitRect(200, 200, 400, 100)!;
		expect(mapClientToImage(110, 30, elementRect, fit, 400, 100)).toBeNull(); // above image
		expect(mapClientToImage(110, 160, elementRect, fit, 400, 100)).toBeNull(); // below image
	});
});

describe('isDragGesture', () => {
	it('treats short movement as a tap', () => {
		expect(isDragGesture({ x: 10, y: 10 }, { x: 12, y: 13 })).toBe(false);
	});

	it('treats movement beyond the threshold as a drag', () => {
		expect(isDragGesture({ x: 10, y: 10 }, { x: 10, y: 10 + DRAG_THRESHOLD_PX + 1 })).toBe(true);
	});

	it('treats a missing start as a tap', () => {
		expect(isDragGesture(null, { x: 50, y: 50 })).toBe(false);
	});
});
