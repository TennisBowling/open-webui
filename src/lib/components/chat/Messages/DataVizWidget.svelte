<script lang="ts">
	import { onDestroy, onMount, getContext } from 'svelte';
	import { settings } from '$lib/stores';
	import { copyToClipboard } from '$lib/utils';
	import { toast } from '$lib/utils/toast';

	import Tooltip from '$lib/components/common/Tooltip.svelte';
	import Download from '$lib/components/icons/Download.svelte';
	import ArrowsPointingOut from '$lib/components/icons/ArrowsPointingOut.svelte';

	const i18n = getContext('i18n');

	interface Props {
		title?: string;
		widgetCode?: string;
		loadingMessages?: string[];
		chatId?: string;
		messageId?: string;
		/**
		 * Reload-time persisted overrides from the message: if the original
		 * widget_code (the model's emission) errored at runtime and was
		 * auto-repaired, the backend stored {key(original_widget_code): final_code}
		 * on the message. We compute the same key from our incoming widgetCode and
		 * look it up here.
		 */
		dataVizOverrides?: Record<string, string>;
		/**
		 * True while the show_widget tool call is still in flight (arguments streaming
		 * in, or the backend still verifying/repairing) — i.e. the visualization hasn't
		 * been fully received yet. Drives the loading placeholder so an incomplete
		 * widget shows a tasteful skeleton instead of a blank gap, WITHOUT showing a
		 * perpetual spinner for a call that finished with genuinely empty code.
		 */
		streaming?: boolean;
	}

	let {
		title = 'widget',
		widgetCode = '',
		loadingMessages = [],
		chatId = '',
		messageId = '',
		dataVizOverrides = {},
		streaming = false
	}: Props = $props();

	let iframeElement: HTMLIFrameElement = $state();
	let widgetHeight: number = $state(80);
	let messageIndex = $state(0);
	let messageInterval: ReturnType<typeof setInterval> | null = null;
	let widgetId = `data-viz-${Math.random().toString(36).slice(2, 10)}`;

	// Hard upper bound on the rendered iframe height. Generous so normal tall
	// widgets (big tables, stacked charts) are never clipped; it exists only as a
	// runaway guard against a widget that reports a pathological height.
	const MAX_WIDGET_HEIGHT = 20000;

	// Track the app theme reactively so an already-mounted widget re-themes when
	// the user toggles dark/light (the theme is a documentElement class mutation,
	// not a Svelte store, so we observe it).
	let isDark = $state(
		typeof document !== 'undefined' && document.documentElement.classList.contains('dark')
	);
	let themeObserver: MutationObserver | null = null;

	/**
	 * 64-bit FNV-1a over the UTF-8 bytes, 16 hex chars. MUST stay byte-identical
	 * to the backend `_override_key` (data_viz_tool.py). Chosen over SHA-256 so
	 * it can be computed SYNCHRONOUSLY and WITHOUT crypto.subtle — which is
	 * undefined on insecure (plain-HTTP) origins, where the old async SHA-256
	 * threw and the override silently never applied. We strip NUL first because
	 * the backend persistence layer strips NUL from the stored code and hashes
	 * that normalized form.
	 */
	const fnv1a16 = (str: string): string => {
		const normalized = (str ?? '').replace(/\u0000/g, '');
		let h = 0xcbf29ce484222325n;
		const bytes = new TextEncoder().encode(normalized);
		for (let i = 0; i < bytes.length; i++) {
			h ^= BigInt(bytes[i]);
			h = (h * 0x100000001b3n) & 0xffffffffffffffffn;
		}
		return h.toString(16).padStart(16, '0');
	};

	// Override key + resolved override are pure functions of the inputs, so they
	// recompute synchronously whenever widgetCode or dataVizOverrides change —
	// before the first paint (fixes the reload "flash of broken widget") and
	// without any secure-context dependency.
	let overrideKey = $derived(fnv1a16(widgetCode ?? ''));
	let override = $derived(
		overrideKey && dataVizOverrides && typeof dataVizOverrides[overrideKey] === 'string'
			? dataVizOverrides[overrideKey]
			: undefined
	);

	// What's actually rendered: the persisted repaired code if we have one,
	// otherwise the model's original emission.
	let displayedCode = $derived(override && override.length ? override : (widgetCode ?? ''));

	let trimmedCode = $derived(displayedCode.trimStart());
	let isSvg = $derived(trimmedCode.startsWith('<svg'));

	type RenderState = 'idle' | 'failed';
	let renderState: RenderState = $state('idle');
	let lastErrorMessage = $state('');

	// Whenever the code we display changes (new tool call, or a late-arriving
	// override swapping the broken original for the fix), reset the error state so
	// a stale "Render error" chip from the previous code doesn't linger.
	let renderedCode = $state('');
	$effect(() => {
		if (displayedCode !== renderedCode) {
			renderedCode = displayedCode;
			renderState = 'idle';
			lastErrorMessage = '';
		}
	});

	// SECURITY: widget_code is MODEL-generated (and steerable via prompt injection
	// when the model summarizes untrusted content), so it is untrusted. We must
	// NEVER add `allow-same-origin`: on a srcdoc iframe, `allow-scripts` +
	// `allow-same-origin` runs the frame in the app's own origin, letting injected
	// code read localStorage.token, cookies, and the parent DOM (a full sandbox
	// escape). The app-wide `iframeSandboxAllowSameOrigin` toggle is intended for
	// the user's OWN pasted artifacts (Artifacts/HTMLToken) — a different trust
	// context — so it is deliberately NOT honored here. The frame keeps a null
	// (opaque) origin. `allow-forms` is safe (it can't reach same-origin state) and
	// stays opt-in. The hidden verification iframe (dataVizLiveRender) mirrors this
	// with `allow-scripts` only, so a widget that needs same-origin fails
	// CONSISTENTLY in both the verifier and the visible frame (never a false pass).
	let sandboxAttr = $derived(
		`allow-scripts${($settings?.iframeSandboxAllowForms ?? false) ? ' allow-forms' : ''}`
	);

	const themeVars = (dark: boolean) => {
		const shared = `
			--font-sans: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;
			--font-serif: Georgia, 'Times New Roman', serif;
			--font-mono: ui-monospace, SFMono-Regular, 'SF Mono', Menlo, Consolas, 'Liberation Mono', monospace;
			--border-radius-sm: 4px;
			--border-radius-md: 8px;
			--border-radius-lg: 12px;
			--border-radius-xl: 16px;
		`;

		if (dark) {
			return `
				${shared}

				--color-text-primary: #F0F0EB;
				--color-text-secondary: #BFBFBA;
				--color-text-tertiary: #91918D;
				--color-text-info: #A4B5D6;
				--color-text-danger: #D88577;
				--color-text-success: #9CB07F;
				--color-text-warning: #E2B873;

				--color-bg-primary: transparent;
				--color-bg-secondary: rgba(255,255,255,0.04);
				--color-bg-tertiary: rgba(255,255,255,0.08);

				--color-background-primary: transparent;
				--color-background-secondary: rgba(255,255,255,0.04);
				--color-background-tertiary: rgba(255,255,255,0.08);
				--color-background-info: rgba(164,181,214,0.12);
				--color-background-danger: rgba(216,133,119,0.12);
				--color-background-success: rgba(156,176,127,0.12);
				--color-background-warning: rgba(226,184,115,0.12);

				--color-border-primary: rgba(255,255,255,0.10);
				--color-border-secondary: rgba(255,255,255,0.06);
				--color-border-tertiary: rgba(255,255,255,0.03);
				--color-border-info: rgba(164,181,214,0.30);
				--color-border-danger: rgba(216,133,119,0.30);
				--color-border-success: rgba(156,176,127,0.30);
				--color-border-warning: rgba(226,184,115,0.30);

				--color-accent-primary: #CC785C;
				--color-accent-secondary: #D4A27F;

				--color-success: #9CB07F;
				--color-warning: #E2B873;
				--color-danger: #D88577;

				--p: #F0F0EB;
				--s: #BFBFBA;
				--t: #91918D;
				--bg2: rgba(255,255,255,0.04);
				--b: rgba(255,255,255,0.10);
			`;
		}
		return `
			${shared}

			--color-text-primary: #191919;
			--color-text-secondary: #666663;
			--color-text-tertiary: #91918D;
			--color-text-info: #5A6B8A;
			--color-text-danger: #BF4D43;
			--color-text-success: #5C7048;
			--color-text-warning: #A8783E;

			--color-bg-primary: transparent;
			--color-bg-secondary: rgba(0,0,0,0.04);
			--color-bg-tertiary: rgba(0,0,0,0.08);

			--color-background-primary: transparent;
			--color-background-secondary: rgba(0,0,0,0.04);
			--color-background-tertiary: rgba(0,0,0,0.08);
			--color-background-info: rgba(90,107,138,0.12);
			--color-background-danger: rgba(191,77,67,0.10);
			--color-background-success: rgba(92,112,72,0.10);
			--color-background-warning: rgba(168,120,62,0.10);

			--color-border-primary: rgba(0,0,0,0.10);
			--color-border-secondary: rgba(0,0,0,0.06);
			--color-border-tertiary: rgba(0,0,0,0.03);
			--color-border-info: rgba(90,107,138,0.30);
			--color-border-danger: rgba(191,77,67,0.30);
			--color-border-success: rgba(92,112,72,0.30);
			--color-border-warning: rgba(168,120,62,0.30);

			--color-accent-primary: #CC785C;
			--color-accent-secondary: #D4A27F;

			--color-success: #5C7048;
			--color-warning: #A8783E;
			--color-danger: #BF4D43;

			--p: #191919;
			--s: #666663;
			--t: #91918D;
			--bg2: rgba(0,0,0,0.04);
			--b: rgba(0,0,0,0.10);
		`;
	};

	// The diagram/art prompt modules tell the model these classes are "already
	// loaded in the SVG widget" and forbid it from defining them itself, so the
	// iframe MUST provide them or every SVG diagram renders black/unstyled:
	//   - text:        .t / .ts / .th
	//   - structural:  .box / .node / .arr / .leader
	//   - color ramps: .c-{purple,teal,coral,pink,gray,blue,green,amber,red}
	// Ramp stops and the light/dark stop-selection rules are taken verbatim from
	// the diagram module prompt. Everything is scoped under `svg` so it can't leak
	// into HTML widgets that reuse a class name, and generated per-theme (the
	// iframe bakes one theme) so colors track the app's dark/light toggle. Ramp
	// fills apply only to rect/circle/ellipse/polygon — never <path> (connectors
	// are stroked inline), matching the prompt's contract.
	const svgWidgetStyles = (dark: boolean): string => {
		// stop order: [50, 100, 200, 400, 600, 800, 900]
		const ramps: Record<string, string[]> = {
			purple: ['#EEEDFE', '#CECBF6', '#AFA9EC', '#7F77DD', '#534AB7', '#3C3489', '#26215C'],
			teal: ['#E1F5EE', '#9FE1CB', '#5DCAA5', '#1D9E75', '#0F6E56', '#085041', '#04342C'],
			coral: ['#FAECE7', '#F5C4B3', '#F0997B', '#D85A30', '#993C1D', '#712B13', '#4A1B0C'],
			pink: ['#FBEAF0', '#F4C0D1', '#ED93B1', '#D4537E', '#993556', '#72243E', '#4B1528'],
			gray: ['#F1EFE8', '#D3D1C7', '#B4B2A9', '#888780', '#5F5E5A', '#444441', '#2C2C2A'],
			blue: ['#E6F1FB', '#B5D4F4', '#85B7EB', '#378ADD', '#185FA5', '#0C447C', '#042C53'],
			green: ['#EAF3DE', '#C0DD97', '#97C459', '#639922', '#3B6D11', '#27500A', '#173404'],
			amber: ['#FAEEDA', '#FAC775', '#EF9F27', '#BA7517', '#854F0B', '#633806', '#412402'],
			red: ['#FCEBEB', '#F7C1C1', '#F09595', '#E24B4A', '#A32D2D', '#791F1F', '#501313']
		};
		// light: 50 fill, 600 stroke, 800 title, 600 subtitle
		// dark:  800 fill, 200 stroke, 100 title, 200 subtitle
		const fillI = dark ? 5 : 0;
		const strokeI = dark ? 2 : 4;
		const titleI = dark ? 1 : 5;
		const subI = dark ? 2 : 4;

		let rampCss = '';
		for (const [name, s] of Object.entries(ramps)) {
			rampCss +=
				`svg rect.c-${name}, svg circle.c-${name}, svg ellipse.c-${name}, svg polygon.c-${name},\n` +
				`svg .c-${name} > rect, svg .c-${name} > circle, svg .c-${name} > ellipse, svg .c-${name} > polygon { fill: ${s[fillI]}; stroke: ${s[strokeI]}; }\n` +
				`svg .c-${name} .t, svg .c-${name} .th { fill: ${s[titleI]}; }\n` +
				`svg .c-${name} .ts { fill: ${s[subI]}; }\n`;
		}

		return (
			`svg .t { fill: var(--color-text-primary); font-family: var(--font-sans); font-size: 14px; font-weight: 400; }\n` +
			`svg .ts { fill: var(--color-text-secondary); font-family: var(--font-sans); font-size: 12px; font-weight: 400; }\n` +
			`svg .th { fill: var(--color-text-primary); font-family: var(--font-sans); font-size: 14px; font-weight: 500; }\n` +
			`svg .box { fill: var(--color-background-secondary); stroke: var(--color-border-primary); stroke-width: 0.5; }\n` +
			`svg .node { cursor: pointer; transition: opacity 0.15s ease; }\n` +
			`svg .node:hover { opacity: 0.82; }\n` +
			`svg .arr { fill: none; stroke: var(--color-text-tertiary); stroke-width: 1.5; }\n` +
			`svg .leader { fill: none; stroke: var(--color-text-tertiary); stroke-width: 0.5; stroke-dasharray: 3 3; }\n` +
			rampCss
		);
	};

	// Build the sandboxed iframe document for BOTH HTML fragments and raw SVG.
	// Routing SVG through the same sandboxed iframe (rather than {@html} into the
	// parent DOM) is what closes the SVG XSS hole: the srcdoc iframe has a null
	// origin (sandbox without allow-same-origin), so injected onerror/onload
	// handlers cannot reach the app's localStorage token, cookies, or DOM.
	const buildIframeDoc = (fragment: string, dark: boolean): string => {
		const css = `:root { ${themeVars(dark)} }
html, body {
	margin: 0;
	padding: 0;
	background: transparent;
	color: var(--color-text-primary);
	font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
}
body { overflow: hidden; }
/* Scale a top-level SVG to the widget width and let height follow aspect ratio,
   so a raw-SVG widget behaves like the old inline render but inside the sandbox. */
body > svg { max-width: 100%; height: auto; display: block; }
${svgWidgetStyles(dark)}`;

		const heightScript = `(function () {
	const post = () => {
		const h = Math.max(
			document.documentElement.scrollHeight,
			document.body.scrollHeight
		);
		parent.postMessage({ __dataViz: true, id: ${JSON.stringify(widgetId)}, height: h }, '*');
	};
	const ro = new ResizeObserver(post);
	ro.observe(document.documentElement);
	ro.observe(document.body);
	window.addEventListener('load', post);
	setTimeout(post, 50);
	setTimeout(post, 250);
})();`;

		// Captures the FIRST runtime error (sync throws + unhandled rejections)
		// and reports back to the parent. Once posted, further errors in the same
		// frame load are dropped — the first one is enough.
		const errorScript = `(function () {
	const ID = ${JSON.stringify(widgetId)};
	const truncate = (s, n) => {
		try { s = String(s == null ? '' : s); } catch (e) { s = ''; }
		return s.length > n ? s.slice(0, n) : s;
	};
	const topFrames = (stack) => {
		try {
			return String(stack || '').split('\\n').slice(0, 8).join('\\n');
		} catch (e) { return ''; }
	};
	let posted = false;
	const send = (payload) => {
		if (posted) return;
		posted = true;
		try { parent.postMessage(payload, '*'); } catch (e) {}
	};
	window.addEventListener('error', function (e) {
		// Ignore RESOURCE-load errors (a CDN <script>/<img>/<link> that 404s,
		// times out, or is blocked fires a window 'error' whose target is the
		// element — not window — and which carries no .error object). Those don't
		// mean the widget's code is broken, so reporting them would trigger a
		// needless auto-repair. Only report genuine script runtime errors.
		if (e && e.target && e.target !== window && e.target.tagName) return;
		send({
			__dataVizError: true,
			id: ID,
			msg: truncate((e && e.message) || 'Error', 500),
			line: e && e.lineno,
			col: e && e.colno,
			stack: topFrames(e && e.error && e.error.stack)
		});
	}, true);
	window.addEventListener('unhandledrejection', function (e) {
		const reason = e && e.reason;
		const msg = (reason && reason.message) || (typeof reason === 'string' ? reason : 'Unhandled rejection');
		send({
			__dataVizError: true,
			id: ID,
			msg: 'Unhandled rejection: ' + truncate(msg, 460),
			stack: topFrames(reason && reason.stack)
		});
	});
})();`;

		return `<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<${''}script>${errorScript}</${''}script>
<${''}style>${css}</${''}style>
</head>
<body>
${fragment}
<${''}script>${heightScript}</${''}script>
</body>
</html>`;
	};

	// Rebuilds whenever the displayed code OR the app theme changes (C13: theme
	// toggle now re-themes an already-mounted widget).
	let iframeDoc = $derived(trimmedCode ? buildIframeDoc(trimmedCode, isDark) : '');

	// ───── Iframe message handling ────────────────────────────────────────────

	const handleIframeError = (data: { msg?: string; stack?: string }) => {
		// The visible widget no longer does live verification (that runs in a
		// hidden iframe via dataVizLiveRender). An error here is a reload-time (or
		// post-render) failure of the displayed code — surface a small chip.
		renderState = 'failed';
		lastErrorMessage = String(data?.msg ?? '').slice(0, 240);
	};

	const handleMessage = (event: MessageEvent) => {
		const data = event.data;
		if (!data || typeof data !== 'object' || data.id !== widgetId) return;
		if (data.__dataVizError) {
			handleIframeError(data);
			return;
		}
		if (!data.__dataViz) return;
		if (typeof data.height === 'number' && data.height > 0) {
			widgetHeight = Math.min(Math.max(data.height + 8, 60), MAX_WIDGET_HEIGHT);
		}
	};

	// ───── Misc UI ────────────────────────────────────────────────────────────

	const cycleLoadingMessages = () => {
		if (messageInterval) clearInterval(messageInterval);
		if (!loadingMessages || loadingMessages.length <= 1) return;
		messageInterval = setInterval(() => {
			messageIndex = (messageIndex + 1) % loadingMessages.length;
		}, 2000);
	};

	const handleCopy = async () => {
		const ok = await copyToClipboard(displayedCode || widgetCode);
		if (ok) toast.success($i18n.t('Copying to clipboard was successful!'));
	};

	// A downloaded SVG must be SELF-CONTAINED and readable wherever it's opened.
	// Two hazards: (1) the on-screen render depends on the class + CSS-variable
	// styles that buildIframeDoc injects into the iframe's <style> (svgWidgetStyles
	// + themeVars) — styles the prompt forbids the model from inlining — so the bare
	// fragment would resolve .c-*/.t/.box/.arr to SVG defaults (black) with undefined
	// vars. (2) On screen the widget is transparent and blends into the chat surface,
	// but a file opens on a viewer's own canvas (browsers/editors/docs default to
	// WHITE). So we always bake the LIGHT theme (dark ink) AND paint an explicit white
	// background — a dark-theme export (near-white ink on transparent) would be
	// invisible on a white page. We also guarantee an xmlns so it opens standalone.
	const buildStandaloneSvg = (fragment: string): string => {
		const open = fragment.match(/<svg\b[^>]*>/i);
		if (!open) return fragment;
		let openTag = open[0];
		if (!/\sxmlns\s*=/.test(openTag)) {
			openTag = openTag.replace(/<svg\b/i, '<svg xmlns="http://www.w3.org/2000/svg"');
		}
		const style = `<${''}style>\n:root{${themeVars(false)}}\nsvg{background:#ffffff;}\n${svgWidgetStyles(false)}</${''}style>`;
		// Belt-and-suspenders backdrop: a full-bleed white rect as the FIRST painted
		// element guarantees an opaque background even in renderers that ignore CSS
		// `background` on the SVG root (notably <img> embeds and rasterizers). Sits
		// behind all content; `100%` resolves against the viewBox/viewport either way.
		const bg = '<rect x="0" y="0" width="100%" height="100%" fill="#ffffff"/>';
		const rest = fragment.slice((open.index ?? 0) + open[0].length);
		return `${openTag}\n${style}\n${bg}${rest}`;
	};

	const handleDownload = () => {
		const ext = isSvg ? 'svg' : 'html';
		const content = isSvg ? buildStandaloneSvg(trimmedCode) : iframeDoc;
		const blob = new Blob([content], {
			type: isSvg ? 'image/svg+xml' : 'text/html'
		});
		const url = URL.createObjectURL(blob);
		const a = document.createElement('a');
		a.href = url;
		a.download = `${title || 'widget'}.${ext}`;
		document.body.appendChild(a);
		a.click();
		document.body.removeChild(a);
		URL.revokeObjectURL(url);
	};

	const handleFullscreen = () => {
		const target = iframeElement;
		if (!target) return;
		if (target.requestFullscreen) target.requestFullscreen();
		// @ts-ignore
		else if (target.webkitRequestFullscreen) target.webkitRequestFullscreen();
	};

	// iOS Safari does not expose the Fullscreen API for iframe/non-video elements
	// (document.fullscreenEnabled is false, webkit* is undefined), so a fullscreen
	// button there is a silent dead no-op. Feature-detect once and hide the control
	// where it can't work — the toolbar is now always visible on touch, so we must
	// not present a button that does nothing when tapped.
	const fullscreenSupported =
		typeof document !== 'undefined' &&
		// @ts-ignore - webkitFullscreenEnabled is a legacy vendor-prefixed flag
		!!(document.fullscreenEnabled || document.webkitFullscreenEnabled);

	onMount(() => {
		window.addEventListener('message', handleMessage);
		cycleLoadingMessages();

		// Observe theme changes so the iframe re-themes on dark/light toggle.
		if (typeof MutationObserver !== 'undefined' && typeof document !== 'undefined') {
			themeObserver = new MutationObserver(() => {
				const d = document.documentElement.classList.contains('dark');
				if (d !== isDark) isDark = d;
			});
			themeObserver.observe(document.documentElement, {
				attributes: true,
				attributeFilter: ['class']
			});
		}
	});

	onDestroy(() => {
		window.removeEventListener('message', handleMessage);
		if (messageInterval) clearInterval(messageInterval);
		if (themeObserver) themeObserver.disconnect();
	});

	$effect(() => {
		if (loadingMessages) cycleLoadingMessages();
	});
</script>

<div class="group relative w-full my-2">
	{#if trimmedCode}
		<iframe
			bind:this={iframeElement}
			{title}
			srcdoc={iframeDoc}
			class="w-full block"
			style="border:0; height:{widgetHeight}px; background:transparent;"
			sandbox={sandboxAttr}
			referrerpolicy="strict-origin-when-cross-origin"
		></iframe>
	{:else if streaming}
		<!-- Visualization still streaming in: a tasteful reserved-height skeleton
		     (matches the iframe's initial height, so no layout jump on swap) with
		     the model's cycling loading messages, or a neutral fallback label. -->
		<div
			class="h-20 w-full rounded-lg border border-gray-100 dark:border-gray-850 bg-gray-50/70 dark:bg-gray-900/40 flex items-center justify-center px-4 animate-pulse select-none"
			aria-live="polite"
		>
			<span class="text-xs text-gray-400 dark:text-gray-500 text-center truncate">
				{loadingMessages.length > 0
					? loadingMessages[messageIndex]
					: $i18n.t('Preparing visualization…')}
			</span>
		</div>
	{/if}

	{#if trimmedCode && renderState === 'failed'}
		<div
			class="absolute top-1 left-1 z-10 flex items-center gap-1.5 px-2 py-1 rounded-md text-xs font-medium border-hairline bg-manilla/60 dark:bg-manilla-dark border-gray-300 dark:border-gray-700 text-gray-700 dark:text-gray-200 backdrop-blur-sm"
		>
			<Tooltip content={lastErrorMessage}>
				<span class="text-gray-600 dark:text-gray-400">{$i18n.t('Render error')}</span>
			</Tooltip>
		</div>
	{/if}

	{#if trimmedCode}
		<!-- Controls: hidden-until-hover on pointer-precise devices, but ALWAYS
		     visible on touch (Tailwind v4 gates group-hover behind
		     @media(hover:hover), so a hover-only toolbar is invisible on mobile). -->
		<div
			class="absolute top-1 right-1 flex gap-1 opacity-100 [@media(hover:hover)]:opacity-0 group-hover:opacity-100 transition-opacity duration-150 pointer-events-none"
		>
			<Tooltip content={$i18n.t('Copy')}>
				<button
					onclick={handleCopy}
					type="button"
					class="tap-target pointer-events-auto bg-white/80 dark:bg-gray-900/80 hover:bg-white dark:hover:bg-gray-900 text-gray-700 dark:text-gray-200 rounded-md p-1.5 max-md:p-2 shadow-sm backdrop-blur-sm"
					aria-label={$i18n.t('Copy')}
				>
					<svg
						xmlns="http://www.w3.org/2000/svg"
						viewBox="0 0 24 24"
						fill="none"
						stroke="currentColor"
						stroke-width="1.75"
						class="size-3.5"
					>
						<path
							d="M9 5H7a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V7a2 2 0 0 0-2-2h-2M9 5a2 2 0 0 0 2 2h2a2 2 0 0 0 2-2M9 5a2 2 0 0 1 2-2h2a2 2 0 0 1 2 2"
						/>
					</svg>
				</button>
			</Tooltip>
			<Tooltip content={$i18n.t('Download')}>
				<button
					onclick={handleDownload}
					type="button"
					class="tap-target pointer-events-auto bg-white/80 dark:bg-gray-900/80 hover:bg-white dark:hover:bg-gray-900 text-gray-700 dark:text-gray-200 rounded-md p-1.5 max-md:p-2 shadow-sm backdrop-blur-sm"
					aria-label={$i18n.t('Download')}
				>
					<Download className="size-3.5" />
				</button>
			</Tooltip>
			{#if fullscreenSupported}
				<Tooltip content={$i18n.t('Open in full screen')}>
					<button
						onclick={handleFullscreen}
						type="button"
						class="tap-target pointer-events-auto bg-white/80 dark:bg-gray-900/80 hover:bg-white dark:hover:bg-gray-900 text-gray-700 dark:text-gray-200 rounded-md p-1.5 max-md:p-2 shadow-sm backdrop-blur-sm"
						aria-label={$i18n.t('Open in full screen')}
					>
						<ArrowsPointingOut className="size-3.5" />
					</button>
				</Tooltip>
			{/if}
		</div>
	{/if}
</div>
