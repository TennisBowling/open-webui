<script lang="ts">
	import { onDestroy, onMount, tick } from 'svelte';

	interface Props {
		// Props
		src?: string | null; // URL or raw HTML (auto-detected)
		title?: string;
		initialHeight?: number | null; // initial height in px, null = auto
		iframeClassName?: string;
		args?: any;
		allowScripts?: boolean;
		allowForms?: boolean;
		allowSameOrigin?: boolean; // set to true only when you trust the content
		allowPopups?: boolean;
		allowDownloads?: boolean;
		referrerPolicy?: HTMLIFrameElement['referrerPolicy'];
		allowFullscreen?: boolean;
	}

	let {
		src = null,
		title = 'Embedded Content',
		initialHeight = null,
		iframeClassName = 'w-full rounded-2xl',
		args = null,
		allowScripts = true,
		allowForms = false,
		allowSameOrigin = false,
		allowPopups = false,
		allowDownloads = true,
		referrerPolicy = 'strict-origin-when-cross-origin',
		allowFullscreen = true
	}: Props = $props();

	let iframe: HTMLIFrameElement | null = $state(null);
	let iframeSrc: string | null = $state(null);
	let iframeDoc: string | null = $state(null);

	const setIframeSrc = async () => {
		await tick();
		if (isUrl) {
			iframeSrc = src as string;
			iframeDoc = null;
		} else {
			iframeDoc = await processHtmlForDeps(src as string);
			iframeSrc = null;
		}
	};

	// Alpine directives detection
	const alpineDirectives = [
		'x-data',
		'x-init',
		'x-show',
		'x-bind',
		'x-on',
		'x-text',
		'x-html',
		'x-model',
		'x-modelable',
		'x-ref',
		'x-for',
		'x-if',
		'x-effect',
		'x-transition',
		'x-cloak',
		'x-ignore',
		'x-teleport',
		'x-id'
	];

	async function processHtmlForDeps(html: string): Promise<string> {
		if (!allowSameOrigin) return html;

		const scriptTags: string[] = [];

		// --- Alpine.js detection & injection ---
		const hasAlpineDirectives = alpineDirectives.some((dir) => html.includes(dir));
		if (hasAlpineDirectives) {
			try {
				const { default: alpineCode } = await import('alpinejs/dist/cdn.min.js?raw');
				const alpineBlob = new Blob([alpineCode], { type: 'text/javascript' });
				const alpineUrl = URL.createObjectURL(alpineBlob);
				const alpineTag = `<script src="${alpineUrl}" defer><\/script>`;
				scriptTags.push(alpineTag);
			} catch (error) {
				console.error('Error processing Alpine for iframe:', error);
			}
		}

		// --- Chart.js detection & injection ---
		const chartJsDirectives = ['new Chart(', 'Chart.'];
		const hasChartJsDirectives = chartJsDirectives.some((dir) => html.includes(dir));
		if (hasChartJsDirectives) {
			try {
				// import chartUrl from 'chart.js/auto?url';
				const { default: Chart } = await import('chart.js/auto');
				(window as any).Chart = Chart;

				const chartTag = `<script>
window.Chart = parent.Chart; // Chart previously assigned on parent
<\/script>`;
				scriptTags.push(chartTag);
			} catch (error) {
				console.error('Error processing Chart.js for iframe:', error);
			}
		}

		// If nothing to inject, return original HTML
		if (scriptTags.length === 0) return html;

		const tags = scriptTags.join('\n');

		// Prefer injecting into <head>, then before </body>, otherwise prepend
		if (html.includes('</head>')) {
			return html.replace('</head>', `${tags}\n</head>`);
		}
		if (html.includes('</body>')) {
			return html.replace('</body>', `${tags}\n</body>`);
		}
		return `${tags}\n${html}`;
	}

	// Try to measure same-origin content safely
	function resizeSameOrigin() {
		if (!iframe) return;
		try {
			const doc = iframe.contentDocument || iframe.contentWindow?.document;
			if (!doc) return;
			const h = Math.max(doc.documentElement?.scrollHeight ?? 0, doc.body?.scrollHeight ?? 0);
			if (h > 0) iframe.style.height = h + 20 + 'px';
		} catch {
			// Cross-origin → rely on postMessage from inside the iframe
		}
	}

	// Handle height messages from the iframe (we also verify the sender)
	function onMessage(e: MessageEvent) {
		if (!iframe || e.source !== iframe.contentWindow) return;
		const data = e.data as { type?: string; height?: number };
		if (data?.type === 'iframe:height' && typeof data.height === 'number') {
			iframe.style.height = Math.max(0, data.height) + 'px';
		}
	}

	// When the iframe loads, try same-origin resize (cross-origin will noop)
	const onLoad = async () => {
		requestAnimationFrame(resizeSameOrigin);

		// if arguments are provided, inject them into the iframe window
		if (args && iframe?.contentWindow) {
			(iframe.contentWindow as any).args = args;
		}
	};

	// Ensure event listener bound only while component lives
	onMount(() => {
		window.addEventListener('message', onMessage);
	});

	onDestroy(() => {
		window.removeEventListener('message', onMessage);
	});
	// Derived: build sandbox attribute from flags
	let sandbox = $derived(
		[
			allowScripts && 'allow-scripts',
			allowForms && 'allow-forms',
			allowSameOrigin && 'allow-same-origin',
			allowPopups && 'allow-popups',
			allowDownloads && 'allow-downloads'
		]
			.filter(Boolean)
			.join(' ') || undefined
	);
	// Detect URL vs raw HTML and prep src/srcdoc
	let isUrl = $derived(typeof src === 'string' && /^(https?:)?\/\//i.test(src));
	$effect(() => {
		if (src) {
			setIframeSrc();
		}
	});
</script>

{#if iframeDoc}
	<iframe
		bind:this={iframe}
		srcdoc={iframeDoc}
		{title}
		class={iframeClassName}
		style={`${initialHeight ? `height:${initialHeight}px;` : ''}`}
		width="100%"
		frameborder="0"
		{sandbox}
		{allowFullscreen}
		onload={onLoad}
	></iframe>
{:else if iframeSrc}
	<iframe
		bind:this={iframe}
		src={iframeSrc}
		{title}
		class={iframeClassName}
		style={`${initialHeight ? `height:${initialHeight}px;` : ''}`}
		width="100%"
		frameborder="0"
		{sandbox}
		referrerpolicy={referrerPolicy}
		{allowFullscreen}
		onload={onLoad}
	></iframe>
{/if}
