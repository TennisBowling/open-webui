import { WEBUI_BASE_URL } from '$lib/constants';

// Svelte action: on `error`, swap the img's src to a fallback exactly once so a
// broken avatar (offline, 401/5xx, unreachable external URL, failed CORS
// redirect, ...) never renders as a broken-image glyph. Guards against an
// infinite loop if the fallback itself fails to load.
export function imageFallback(
	node: HTMLImageElement,
	fallback: string = `${WEBUI_BASE_URL}/static/favicon.png`
) {
	let swapped = false;

	const onError = () => {
		if (swapped) return;
		swapped = true;
		node.removeEventListener('error', onError);
		node.src = fallback;
	};

	node.addEventListener('error', onError);

	return {
		destroy() {
			node.removeEventListener('error', onError);
		}
	};
}
