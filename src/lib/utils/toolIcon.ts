// Turns a user-picked image file into a small, self-contained data URL suitable
// for storing on a tool-server connection (`info.icon` / `meta.icon`).
//
// Icons ride along inside the connection config and the `/api/v1/tools/`
// response, so they must stay TINY: a 64px raster is already 2x for the 16px
// slot the integrations menu renders, and inlining it means the icon costs zero
// extra requests and is cached by whatever already caches those payloads.

export const ICON_MIME_TYPES = [
	'image/png',
	'image/jpeg',
	'image/webp',
	'image/gif',
	'image/svg+xml'
];

export const ICON_ACCEPT = ICON_MIME_TYPES.join(',');

// Rendered at 16px; 64 covers 3x displays with room to spare.
const ICON_PIXEL_SIZE = 64;

// An SVG under this stays vector (crisp at any DPI, and usually smaller than
// the PNG we'd rasterise). Above it, rasterising is the smaller of the two.
const MAX_INLINE_SVG_BYTES = 16 * 1024;

const readAsDataUrl = (file: File): Promise<string> =>
	new Promise((resolve, reject) => {
		const reader = new FileReader();
		reader.onload = () => resolve(String(reader.result ?? ''));
		reader.onerror = () => reject(new Error('Could not read that file'));
		reader.readAsDataURL(file);
	});

const rasterize = (dataUrl: string, size: number): Promise<string> =>
	new Promise((resolve, reject) => {
		const img = new Image();
		img.onload = () => {
			const canvas = document.createElement('canvas');
			canvas.width = size;
			canvas.height = size;

			const ctx = canvas.getContext('2d');
			if (!ctx) {
				reject(new Error('Could not read that image'));
				return;
			}

			// A viewBox-only SVG reports no intrinsic size in some browsers.
			const srcWidth = img.naturalWidth || img.width || size;
			const srcHeight = img.naturalHeight || img.height || size;

			// Contain, not cover: an icon that gets cropped stops reading as itself.
			const scale = Math.min(size / srcWidth, size / srcHeight);
			const width = Math.max(1, Math.round(srcWidth * scale));
			const height = Math.max(1, Math.round(srcHeight * scale));

			ctx.imageSmoothingEnabled = true;
			ctx.imageSmoothingQuality = 'high';
			ctx.drawImage(
				img,
				Math.round((size - width) / 2),
				Math.round((size - height) / 2),
				width,
				height
			);

			resolve(canvas.toDataURL('image/png'));
		};
		img.onerror = () => reject(new Error('Could not read that image'));
		img.src = dataUrl;
	});

export const fileToIconDataUrl = async (
	file: File,
	size: number = ICON_PIXEL_SIZE
): Promise<string> => {
	if (!ICON_MIME_TYPES.includes(file?.type)) {
		throw new Error(`Unsupported file type '${file?.type || 'unknown'}'`);
	}

	const dataUrl = await readAsDataUrl(file);

	if (file.type === 'image/svg+xml' && dataUrl.length <= MAX_INLINE_SVG_BYTES) {
		return dataUrl;
	}

	return rasterize(dataUrl, size);
};
