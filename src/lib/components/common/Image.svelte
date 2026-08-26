<script lang="ts">
	import { WEBUI_BASE_URL } from '$lib/constants';
	import { onDestroy } from 'svelte';

	import { settings, sharedContext } from '$lib/stores';
	import { getFileObjectUrlById, revokeFileObjectUrlById } from '$lib/apis/files';
	import ImagePreview from './ImagePreview.svelte';
	import XMark from '$lib/components/icons/XMark.svelte';
	import Spinner from './Spinner.svelte';
	import Photo from '$lib/components/icons/Photo.svelte';
	import ArrowPath from '$lib/components/icons/ArrowPath.svelte';
	import {
		cacheImageDims,
		fetchFileImageDims,
		fileDimsKey,
		fitInsideBox,
		getCachedImageDims,
		rememberLoadedImage,
		urlDimsKey,
		type ImageDims
	} from '$lib/utils/imageDimensions';
	import { getContext } from 'svelte';

	// Some callers (e.g. tool-result galleries) pass an id for keying; accept it

	interface Props {
		src?: string;
		alt?: string;
		// so Svelte doesn't warn about an unknown prop. Not otherwise required.
		id?: string;
		className?: any;
		imageClassName?: string;
		dismissible?: boolean;
		onDismiss?: any;
	}

	let {
		src = '',
		alt = '',
		id = '',
		className = ` w-full ${($settings?.highContrastMode ?? false) ? '' : 'outline-hidden focus:outline-hidden'}`,
		imageClassName = 'rounded-lg',
		dismissible = false,
		onDismiss = () => {}
	}: Props = $props();

	const i18n = getContext('i18n');

	// Match BOTH a root-relative `/api/v1/files/{id}/content` and a fully
	// qualified `http(s)://host/api/v1/files/{id}/content` (the API base is
	// origin-prefixed in dev). `[^/?#]+` keeps the id segment intact.
	const LOCAL_FILE_RE = /(?:^|\/)api\/v1\/files\/([^/?#]+)\/content(?:[/?#]|$)/;

	type Resolved = { kind: 'passthrough'; url: string } | { kind: 'localFile'; fileId: string };

	const classifySrc = (value: string): Resolved => {
		const s = (value ?? '').trim();
		if (!s) return { kind: 'passthrough', url: '' };

		// data: and blob: URLs are self-contained — never fetch them (generated
		// images, tool-result images, live browser frames, object URLs).
		if (s.startsWith('data:') || s.startsWith('blob:')) {
			return { kind: 'passthrough', url: s };
		}

		const match = s.match(LOCAL_FILE_RE);
		if (match) {
			return { kind: 'localFile', fileId: match[1] };
		}

		// Root-relative non-file asset (e.g. /user.png) → prefix the base URL,
		// exactly as before. Absolute external URLs pass through untouched.
		if (s.startsWith('/')) {
			return { kind: 'passthrough', url: `${WEBUI_BASE_URL}${s}` };
		}
		return { kind: 'passthrough', url: s };
	};

	let displaySrc = $state('');
	let loading = $state(false);
	let errored = $state(false);
	// One silent refetch per file id on <img> error (heals LRU-evicted object
	// URLs); afterwards the manual retry button takes over.
	let autoRetryFileId = $state('');
	let activeFileId = $state(''); // the local file id currently being shown (for retry/stale-guard)
	let isLocalFile = $state(false); // whether the current src resolves to a Bearer-fetched file
	let abortController: AbortController | null = null;
	let resolveToken = 0; // monotonic; guards against stale async resolutions

	// Inline display only needs a thumbnail, not the full-res original. Retina
	// (dpr>2) gets a 1024px thumbnail so it still looks crisp; everything else
	// 768px. Full-res is fetched lazily only when the lightbox opens / on
	// download (getFileObjectUrlById with no width). The two are cached under
	// separate keys, so opening the lightbox never evicts the thumbnail.
	const inlineWidth =
		typeof window !== 'undefined' && (window.devicePixelRatio ?? 1) > 2 ? 1024 : 768;

	// Lazy-load gate: chats open scrolled to the bottom, so the visible/latest
	// images resolve immediately while off-screen ones (the 600px rootMargin
	// prefetch aside) never Bearer-fetch until the user scrolls near them. This
	// is what keeps a 20-photo chat from pulling tens of MB on open — the JS
	// fetch design bypasses native loading="lazy".
	let inView = $state(false);

	// ---- Reserved box (layout-shift prevention) ----------------------------
	// The intrinsic size of the variant we are about to show, applied to the
	// <img> as width/height ATTRIBUTES. Attributes (not inline styles) are the
	// right lever: they are presentational hints, so `size-14`, `w-full` and
	// friends still win, while the browser derives `aspect-ratio` from them and
	// reserves the exact final box before a single byte has arrived. See
	// utils/imageDimensions.ts for where the numbers come from.
	let reservedDims = $state<ImageDims | null>(null);
	// Remembers the size of the last image THIS instance showed. Sources that
	// swap `src` rapidly with same-shaped frames (the live browser view, which
	// streams data: URLs) then never shift, even though data: URLs are not
	// cacheable by key.
	let lastShownDims: ImageDims | null = null;

	const localVariantKey = (fileId: string) => fileDimsKey(fileId, inlineWidth);

	/** Best size we can name for `value` right now, without touching the network. */
	const reservationFor = (value: string): ImageDims | null => {
		const classified = classifySrc(value);
		if (classified.kind === 'localFile') {
			// Exact size of the thumbnail variant, measured on a previous view.
			const exact = getCachedImageDims(localVariantKey(classified.fileId));
			if (exact) return exact;
			// Otherwise derive it from the original's size the same way the
			// server's thumbnailer will.
			const original = getCachedImageDims(fileDimsKey(classified.fileId));
			return original ? fitInsideBox(original, inlineWidth) : null;
		}
		return getCachedImageDims(urlDimsKey(classified.url)) ?? lastShownDims;
	};

	/** Intrinsic size of a URL, decoded off-DOM. Null when it can't be loaded. */
	const measureOffscreen = (url: string): Promise<ImageDims | null> =>
		new Promise((resolve) => {
			if (typeof window === 'undefined') return resolve(null);
			const probe = new window.Image();
			probe.onload = () =>
				resolve(
					probe.naturalWidth > 0 && probe.naturalHeight > 0
						? { width: probe.naturalWidth, height: probe.naturalHeight }
						: null
				);
			probe.onerror = () => resolve(null);
			probe.src = url;
		});

	const applyLoadedDims = (img: HTMLImageElement) => {
		// A load event for a src we have already moved on from would file the
		// wrong size under the new src's cache key.
		if (img.getAttribute('src') !== displaySrc) return;
		const classified = classifySrc(src);
		const dims = rememberLoadedImage(
			img,
			classified.kind === 'localFile'
				? [localVariantKey(classified.fileId)]
				: [urlDimsKey(classified.url)]
		);
		if (dims) {
			// The decoded bytes are the authority — adopt them so the reserved box
			// and the rendered box are the same box from here on.
			reservedDims = dims;
			lastShownDims = dims;
		}
	};

	const lazyObserve = (node: HTMLElement) => {
		if (typeof IntersectionObserver === 'undefined') {
			inView = true; // no support → behave as before (resolve immediately)
			return;
		}
		const observer = new IntersectionObserver(
			(entries) => {
				if (entries.some((e) => e.isIntersecting)) {
					inView = true;
					observer.disconnect();
				}
			},
			{ rootMargin: '600px' }
		);
		observer.observe(node);
		return {
			destroy() {
				observer.disconnect();
			}
		};
	};

	const resolve = async (value: string) => {
		const token = ++resolveToken;

		// Tear down any in-flight fetch for a previous src.
		if (abortController) {
			abortController.abort();
			abortController = null;
		}
		// A new src invalidates any previously fetched full-res copy.
		fullSrc = '';
		fullFileId = '';

		const classified = classifySrc(value);

		if (classified.kind === 'passthrough') {
			activeFileId = '';
			isLocalFile = false;
			errored = false;
			loading = false;
			// Self-contained bytes (generated images, tool-result galleries, live
			// browser frames) have no server-side size to ask for, so measure them
			// off-DOM before mounting: decoding a data:/blob: URL is local and
			// near-instant, and it means the <img> appears at its final size
			// instead of growing from zero under the reader. Remote URLs are
			// deliberately NOT pre-decoded — that would trade a layout shift for a
			// blank box during the whole download, and they self-heal from the
			// dimension cache on the next view.
			const url = classified.url;
			if (url && !reservedDims && (url.startsWith('data:') || url.startsWith('blob:'))) {
				const dims = await measureOffscreen(url);
				if (token !== resolveToken) return;
				if (dims) {
					reservedDims = dims;
					lastShownDims = dims;
				}
			}
			displaySrc = url;
			return;
		}

		// localFile → Bearer-fetch a THUMBNAIL into an object URL.
		activeFileId = classified.fileId;
		isLocalFile = true;
		errored = false;
		loading = true;
		displaySrc = '';

		const controller = new AbortController();
		abortController = controller;

		try {
			const token_str = typeof localStorage !== 'undefined' ? (localStorage.token ?? '') : '';
			const objectUrl = await getFileObjectUrlById(
				token_str,
				classified.fileId,
				controller.signal,
				$sharedContext.shareId,
				inlineWidth
			);
			// A newer resolve() started while we awaited → drop this result.
			if (token !== resolveToken) return;
			if (!reservedDims) {
				// No size from the cache or the server (old backend, offline, a
				// format PIL can't read). The bytes are in hand now, so measure
				// them locally and reveal the image and its box in the SAME
				// update — that way it can't grow out of zero afterwards, and the
				// size is cached for every future view of this chat.
				const dims = await measureOffscreen(objectUrl);
				if (token !== resolveToken) return;
				if (dims) {
					reservedDims = dims;
					lastShownDims = dims;
					cacheImageDims(localVariantKey(classified.fileId), dims);
				}
			}
			displaySrc = objectUrl;
			loading = false;
		} catch (err: any) {
			if (token !== resolveToken || err?.name === 'AbortError') {
				return; // superseded or intentionally aborted — not an error state
			}
			console.error('Image load failed:', err);
			errored = true;
			loading = false;
		}
	};

	// Full-resolution copy, fetched lazily only when the lightbox opens.
	let fullSrc = $state('');
	let fullFileId = '';
	let fullLoading = false;

	const loadFullResolution = async () => {
		if (!isLocalFile || !activeFileId) return;
		if (fullSrc && fullFileId === activeFileId) return;

		const fileId = activeFileId;
		fullLoading = true;
		try {
			const token_str = typeof localStorage !== 'undefined' ? (localStorage.token ?? '') : '';
			// No width → the untouched original bytes (its own cache key).
			const objectUrl = await getFileObjectUrlById(
				token_str,
				fileId,
				undefined,
				$sharedContext.shareId
			);
			if (fileId !== activeFileId) return; // src changed while awaiting
			fullSrc = objectUrl;
			fullFileId = fileId;
		} catch (err: any) {
			// Fall back to the already-shown thumbnail in the lightbox.
			console.error('Full-resolution image load failed:', err);
		} finally {
			fullLoading = false;
		}
	};

	const openPreview = () => {
		showImagePreview = true;
		void loadFullResolution();
	};

	const retry = () => {
		if (activeFileId) {
			// Drop any cached failure (thumbnail + full-res) so the refetch
			// actually hits the network.
			revokeFileObjectUrlById(activeFileId);
		}
		resolve(src);
	};

	// Re-resolve whenever the src prop changes, but only once the image is near
	// the viewport (lazy gate). `inView` flipping true re-runs this and kicks
	// off the first fetch.
	$effect(() => {
		if (inView) resolve(src);
	});

	// Reserve the box as early as possible — deliberately NOT gated on `inView`.
	// An off-screen image whose height is unknown makes the whole document
	// shorter than it really is, so every scroll position below it is wrong and
	// the turn heights the sweeper stamps (messageHeights.ts) are wrong too.
	// Asking for the size costs one batched JSON row; the bytes still wait for
	// the lazy gate.
	$effect(() => {
		const value = src;
		const known = reservationFor(value);
		if (known) {
			reservedDims = known;
			return;
		}
		reservedDims = null;

		const classified = classifySrc(value);
		if (classified.kind !== 'localFile') return;

		void fetchFileImageDims(classified.fileId, $sharedContext.shareId).then((dims) => {
			// A newer src (or a size learned from the decoded bytes meanwhile)
			// takes precedence over this answer.
			if (!dims || value !== src || reservedDims) return;
			reservedDims = fitInsideBox(dims, inlineWidth);
		});
	});

	onDestroy(() => {
		resolveToken++;
		if (abortController) {
			abortController.abort();
			abortController = null;
		}
	});

	let showImagePreview = $state(false);

	// Prefer the full-res copy in the lightbox once it has loaded; otherwise the
	// thumbnail (or a passthrough url) is shown immediately and upgraded in place.
	let previewSrc = $derived(fullSrc || displaySrc);
</script>

<ImagePreview bind:show={showImagePreview} src={previewSrc} {alt} />

<div class=" relative group w-fit flex items-center" use:lazyObserve>
	{#if errored}
		<button
			type="button"
			class="flex flex-col items-center justify-center gap-1 {imageClassName} bg-gray-100 dark:bg-gray-850 text-gray-400 dark:text-gray-500 min-w-16 min-h-16 aspect-square p-2"
			onclick={retry}
			aria-label={$i18n.t('Image failed to load. Click to retry.')}
		>
			<Photo className="size-5" />
			<div class="flex items-center gap-0.5 text-[10px] leading-tight">
				<ArrowPath className="size-3" />
				<span>{$i18n.t('Retry')}</span>
			</div>
		</button>
	{:else}
		<!-- The <img> stays mounted through loading so its reserved box (below)
		     is the SAME box the loaded image ends up in — the spinner floats on
		     top of it instead of being a differently sized placeholder that has
		     to be swapped out (which is a layout shift, and with it a jump in
		     everyone's scroll position). Only when the size is still unknown do
		     we fall back to the old in-flow 64px chip. -->
		<button
			class={className}
			onclick={openPreview}
			aria-label={$i18n.t('Show image preview')}
			type="button"
			disabled={!displaySrc}
		>
			<img
				src={displaySrc || undefined}
				{alt}
				width={reservedDims?.width}
				height={reservedDims?.height}
				class={imageClassName}
				style={displaySrc ? undefined : 'visibility: hidden;'}
				draggable="false"
				decoding="async"
				data-cy="image"
				onload={(event) => applyLoadedDims(event.currentTarget as HTMLImageElement)}
				onerror={() => {
					// A cached object URL may have been revoked by the LRU eviction in
					// getFileObjectUrlById after this <img> resolved it; refetch once so
					// an evicted-then-redecoded image heals silently.
					if (isLocalFile && activeFileId && autoRetryFileId !== activeFileId) {
						autoRetryFileId = activeFileId;
						revokeFileObjectUrlById(activeFileId);
						resolve(src);
						return;
					}
					// Backstop: a passthrough/legacy URL that fails to load (expired,
					// 404) still shows the real fallback instead of the native glyph.
					if (displaySrc) errored = true;
				}}
			/>
		</button>

		{#if loading}
			{#if reservedDims}
				<div
					class="absolute inset-0 flex items-center justify-center {imageClassName} bg-gray-100 dark:bg-gray-850"
				>
					<Spinner className="size-5" />
				</div>
			{:else}
				<div
					class="flex items-center justify-center {imageClassName} bg-gray-100 dark:bg-gray-850 min-w-16 min-h-16 aspect-square"
				>
					<Spinner className="size-5" />
				</div>
			{/if}
		{/if}
	{/if}

	{#if dismissible}
		<div class=" absolute -top-1 -right-1">
			<button
				aria-label={$i18n.t('Remove image')}
				class=" bg-white text-black border-hairline border-white rounded-full max-md:p-1 group-hover:visible invisible transition"
				type="button"
				onclick={() => {
					onDismiss();
				}}
			>
				<XMark className={'size-4'} />
			</button>
		</div>
	{/if}
</div>
