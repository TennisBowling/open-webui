<script lang="ts">
	import { WEBUI_BASE_URL } from '$lib/constants';
	import { onDestroy } from 'svelte';

	import { settings } from '$lib/stores';
	import { getFileObjectUrlById, revokeFileObjectUrlById } from '$lib/apis/files';
	import ImagePreview from './ImagePreview.svelte';
	import XMark from '$lib/components/icons/XMark.svelte';
	import Spinner from './Spinner.svelte';
	import Photo from '$lib/components/icons/Photo.svelte';
	import ArrowPath from '$lib/components/icons/ArrowPath.svelte';
	import { getContext } from 'svelte';

	export let src = '';
	export let alt = '';
	// Some callers (e.g. tool-result galleries) pass an id for keying; accept it
	// so Svelte doesn't warn about an unknown prop. Not otherwise required.
	export let id = '';

	export let className = ` w-full ${($settings?.highContrastMode ?? false) ? '' : 'outline-hidden focus:outline-hidden'}`;

	export let imageClassName = 'rounded-lg';

	export let dismissible = false;
	export let onDismiss = () => {};

	const i18n = getContext('i18n');

	// Match BOTH a root-relative `/api/v1/files/{id}/content` and a fully
	// qualified `http(s)://host/api/v1/files/{id}/content` (the API base is
	// origin-prefixed in dev). `[^/?#]+` keeps the id segment intact.
	const LOCAL_FILE_RE = /(?:^|\/)api\/v1\/files\/([^/?#]+)\/content(?:[/?#]|$)/;

	type Resolved =
		| { kind: 'passthrough'; url: string }
		| { kind: 'localFile'; fileId: string };

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

	let displaySrc = '';
	let loading = false;
	let errored = false;
	let activeFileId = ''; // the local file id currently being shown (for retry/stale-guard)
	let abortController: AbortController | null = null;
	let resolveToken = 0; // monotonic; guards against stale async resolutions

	const resolve = async (value: string) => {
		const token = ++resolveToken;

		// Tear down any in-flight fetch for a previous src.
		if (abortController) {
			abortController.abort();
			abortController = null;
		}

		const classified = classifySrc(value);

		if (classified.kind === 'passthrough') {
			activeFileId = '';
			errored = false;
			loading = false;
			displaySrc = classified.url;
			return;
		}

		// localFile → Bearer-fetch into an object URL.
		activeFileId = classified.fileId;
		errored = false;
		loading = true;
		displaySrc = '';

		const controller = new AbortController();
		abortController = controller;

		try {
			const token_str =
				typeof localStorage !== 'undefined' ? (localStorage.token ?? '') : '';
			const objectUrl = await getFileObjectUrlById(
				token_str,
				classified.fileId,
				controller.signal
			);
			// A newer resolve() started while we awaited → drop this result.
			if (token !== resolveToken) return;
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

	const retry = () => {
		if (activeFileId) {
			// Drop any cached failure so the refetch actually hits the network.
			revokeFileObjectUrlById(activeFileId);
		}
		resolve(src);
	};

	// Re-resolve whenever the src prop changes.
	$: resolve(src);

	onDestroy(() => {
		resolveToken++;
		if (abortController) {
			abortController.abort();
			abortController = null;
		}
	});

	let showImagePreview = false;
</script>

<ImagePreview bind:show={showImagePreview} src={displaySrc} {alt} />

<div class=" relative group w-fit flex items-center">
	{#if loading}
		<div
			class="flex items-center justify-center {imageClassName} bg-gray-100 dark:bg-gray-850 min-w-16 min-h-16 aspect-square"
		>
			<Spinner className="size-5" />
		</div>
	{:else if errored}
		<button
			type="button"
			class="flex flex-col items-center justify-center gap-1 {imageClassName} bg-gray-100 dark:bg-gray-850 text-gray-400 dark:text-gray-500 min-w-16 min-h-16 aspect-square p-2"
			on:click={retry}
			aria-label={$i18n.t('Image failed to load. Click to retry.')}
		>
			<Photo className="size-5" />
			<div class="flex items-center gap-0.5 text-[10px] leading-tight">
				<ArrowPath className="size-3" />
				<span>{$i18n.t('Retry')}</span>
			</div>
		</button>
	{:else}
		<button
			class={className}
			on:click={() => {
				showImagePreview = true;
			}}
			aria-label={$i18n.t('Show image preview')}
			type="button"
		>
			<img
				src={displaySrc}
				{alt}
				class={imageClassName}
				draggable="false"
				data-cy="image"
				on:error={() => {
					// Backstop: a passthrough/legacy URL that fails to load (expired,
					// 404) still shows the real fallback instead of the native glyph.
					if (displaySrc) errored = true;
				}}
			/>
		</button>
	{/if}

	{#if dismissible}
		<div class=" absolute -top-1 -right-1">
			<button
				aria-label={$i18n.t('Remove image')}
				class=" bg-white text-black border border-white rounded-full group-hover:visible invisible transition"
				type="button"
				on:click={() => {
					onDismiss();
				}}
			>
				<XMark className={'size-4'} />
			</button>
		</div>
	{/if}
</div>
