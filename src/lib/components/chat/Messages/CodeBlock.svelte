<script lang="ts">
	import hljs from 'highlight.js/lib/common';
	import { toast } from '$lib/utils/toast';
	import { getContext, onMount, onDestroy } from 'svelte';

	import { copyToClipboard, renderMermaidDiagram, renderVegaVisualization } from '$lib/utils';

	import 'highlight.js/styles/github-dark.min.css';

	import SvgPanZoom from '$lib/components/common/SVGPanZoom.svelte';

	// CodeEditor pulls in the entire CodeMirror stack. Load it lazily so a
	// transcript of read-only code blocks never mounts an editor (or imports
	// CodeMirror) until the user explicitly clicks Edit.
	let codeEditorPromise = null;
	const loadCodeEditor = () => {
		if (!codeEditorPromise) {
			codeEditorPromise = import('$lib/components/common/CodeEditor.svelte');
		}
		return codeEditorPromise;
	};

	import ChevronUpDown from '$lib/components/icons/ChevronUpDown.svelte';

	const i18n = getContext('i18n');

	interface Props {
		id?: string;
		edit?: boolean;
		onSave?: any;
		onUpdate?: any;
		onPreview?: any;
		save?: boolean;
		preview?: boolean;
		collapsed?: boolean;
		token: any;
		lang?: string;
		code?: string;
		className?: string;
		editorClassName?: string;
		stickyButtonsClassName?: string;
	}

	let {
		id = '',
		edit = true,
		onSave = (e) => {},
		onUpdate = (e) => {},
		onPreview = (e) => {},
		save = false,
		preview = false,
		collapsed = $bindable(false),
		token,
		lang = '',
		code = $bindable(''),
		className = 'mb-2',
		editorClassName = '',
		stickyButtonsClassName = 'top-0'
	}: Props = $props();

	let codeBlockElement = $state(null);
	let visible = $state(false);
	let observer = null;
	let editing = $state(false);

	let _code = $state('');

	const updateCode = () => {
		_code = code;
	};

	let _token = $state(null);

	let mermaidHtml = $state(null);
	let vegaHtml = $state(null);

	let copied = $state(false);
	let saved = $state(false);

	const collapseCodeBlock = () => {
		collapsed = !collapsed;
	};

	const saveCode = () => {
		saved = true;

		code = _code;
		onSave(code);

		setTimeout(() => {
			saved = false;
		}, 1000);
	};

	const copyCode = async () => {
		copied = true;
		await copyToClipboard(_code);

		setTimeout(() => {
			copied = false;
		}, 1000);
	};

	const previewCode = () => {
		onPreview(code);
	};

	const render = async () => {
		onUpdate(token);
		if (lang === 'mermaid' && (token?.raw ?? '').slice(-4).includes('```')) {
			try {
				mermaidHtml = await renderMermaidDiagram(code);
			} catch (error) {
				console.error('Failed to render mermaid diagram:', error);
				const errorMsg = error instanceof Error ? error.message : String(error);
				toast.error($i18n.t('Failed to render diagram') + `: ${errorMsg}`);
				mermaidHtml = null;
			}
		} else if (
			(lang === 'vega' || lang === 'vega-lite') &&
			(token?.raw ?? '').slice(-4).includes('```')
		) {
			try {
				vegaHtml = await renderVegaVisualization(code);
			} catch (error) {
				console.error('Failed to render Vega visualization:', error);
				const errorMsg = error instanceof Error ? error.message : String(error);
				toast.error($i18n.t('Failed to render diagram') + `: ${errorMsg}`);
				vegaHtml = null;
			}
		}
	};

	// Cheap change-detection: `code` and `lang` are derived from the token and
	// are what actually drive rendering. Comparing them (plus raw length to catch
	// trailing-fence changes) avoids the previous double `JSON.stringify(token)`
	// of a growing token on every reactive pass -- O(token) per pass during
	// streaming -> O(token^2) over the block's life.
	let _tokenSig = $state('');

	// Memoized syntax highlighting. Recomputes ONLY when the visible code or its
	// language changes -- not on every reactive pass. Prefer `hljs.highlight` with
	// the known language (much cheaper than `highlightAuto`'s language detection,
	// which previously ran in the template on every re-render of a growing code
	// block -> O(code^2) over the stream). Falls back to auto-detect only when the
	// language is unknown.
	let highlightedHtml = $state('');
	let _lastHighlightKey = $state('');

	onMount(async () => {
		observer = new IntersectionObserver((entries) => {
			if (entries[0].isIntersecting) {
				visible = true;
				observer.disconnect();
			}
		});

		if (codeBlockElement) {
			observer.observe(codeBlockElement);
		}

		if (token) {
			onUpdate(token);
		}
	});

	onDestroy(() => {
		if (observer) {
			observer.disconnect();
		}
	});
	$effect(() => {
		if (code) {
			updateCode();
		}
	});
	$effect(() => {
		if (token) {
			const sig = `${lang} ${(code ?? '').length} ${(token?.raw ?? '').length}`;
			if (sig !== _tokenSig) {
				_tokenSig = sig;
				_token = token;
			}
		}
	});
	$effect(() => {
		if (_token) {
			render();
		}
	});
	$effect(() => {
		if (visible && code) {
			const key = `${lang} ${code}`;
			if (key !== _lastHighlightKey) {
				_lastHighlightKey = key;
				try {
					const known = lang && hljs.getLanguage(lang);
					highlightedHtml = known
						? hljs.highlight(code, { language: lang, ignoreIllegal: true }).value
						: hljs.highlightAuto(code).value || code;
				} catch {
					highlightedHtml = code;
				}
			}
		}
	});
</script>

<div>
	<div
		class="relative {className} flex flex-col rounded-xl border-hairline border-gray-300 dark:border-gray-700 my-0.5"
		dir="ltr"
		bind:this={codeBlockElement}
	>
		{#if lang === 'mermaid'}
			{#if mermaidHtml}
				<SvgPanZoom
					className=" rounded-xl max-h-fit overflow-hidden"
					svg={mermaidHtml}
					content={_token.text}
				/>
			{:else}
				<pre class="mermaid">{code}</pre>
			{/if}
		{:else if lang === 'vega' || lang === 'vega-lite'}
			{#if vegaHtml}
				<SvgPanZoom
					className="rounded-xl max-h-fit overflow-hidden"
					svg={vegaHtml}
					content={_token.text}
				/>
			{:else}
				<pre class="vega">{code}</pre>
			{/if}
		{:else}
			<div
				class="absolute left-0 right-0 py-2.5 pr-3 text-gray-500 pl-4.5 text-xs font-medium dark:text-gray-400"
			>
				{lang}
			</div>

			<div
				class="sticky {stickyButtonsClassName} left-0 right-0 py-2 pr-3 flex items-center justify-end w-full z-10 text-xs text-black dark:text-white"
			>
				<div class="flex items-center gap-0.5">
					<button
						class="flex gap-1 items-center bg-none border-none transition rounded-md px-1.5 py-0.5 max-md:py-1.5 bg-white/70 dark:bg-gray-800 hover:bg-white dark:hover:bg-gray-700"
						data-anchor-on-click
						onclick={collapseCodeBlock}
					>
						<div class=" -translate-y-[0.5px]">
							<ChevronUpDown className="size-3" />
						</div>

						<div>
							{collapsed ? $i18n.t('Expand') : $i18n.t('Collapse')}
						</div>
					</button>

					{#if save && !edit && !editing}
						<button
							class="edit-code-button bg-none border-none transition rounded-md px-1.5 py-0.5 max-md:py-1.5 bg-white/70 dark:bg-gray-800 hover:bg-white dark:hover:bg-gray-700"
							data-anchor-on-click
							onclick={() => (editing = true)}
						>
							{$i18n.t('Edit')}
						</button>
					{/if}

					{#if save && (edit || editing)}
						<button
							class="save-code-button bg-none border-none transition rounded-md px-1.5 py-0.5 max-md:py-1.5 bg-white/70 dark:bg-gray-800 hover:bg-white dark:hover:bg-gray-700"
							onclick={saveCode}
						>
							{saved ? $i18n.t('Saved') : $i18n.t('Save')}
						</button>
					{/if}

					<button
						class="copy-code-button bg-none border-none transition rounded-md px-1.5 py-0.5 max-md:py-1.5 bg-white/70 dark:bg-gray-800 hover:bg-white dark:hover:bg-gray-700"
						onclick={copyCode}>{copied ? $i18n.t('Copied') : $i18n.t('Copy')}</button
					>

					{#if preview && ['html', 'svg'].includes(lang)}
						<button
							class="flex gap-1 items-center preview-code-button bg-none border-none transition rounded-md px-1.5 py-0.5 max-md:py-1.5 bg-white/70 dark:bg-gray-800 hover:bg-white dark:hover:bg-gray-700"
							onclick={previewCode}
						>
							<div>
								{$i18n.t('Preview')}
							</div>
						</button>
					{/if}
				</div>
			</div>

			<div
				class="language-{lang} rounded-t-xl -mt-9 bg-manilla/60 dark:bg-manilla-dark {editorClassName
					? editorClassName
					: 'rounded-b-xl'} overflow-hidden"
			>
				<div class=" pt-8 bg-manilla/60 dark:bg-manilla-dark"></div>

				{#if !collapsed}
					{#if edit || editing}
						{#await loadCodeEditor() then m}
							<m.default
								value={code}
								{id}
								{lang}
								onSave={() => {
									saveCode();
								}}
								onChange={(value) => {
									_code = value;
								}}
							/>
						{/await}
					{:else}
						<pre
							class=" hljs p-4 px-5 overflow-x-auto"
							style="border-top-left-radius: 0px; border-top-right-radius: 0px;"><code
								class="language-{lang} rounded-t-none whitespace-pre text-sm"
								>{#if visible}{@html highlightedHtml || code}{:else}{code}{/if}</code
							></pre>
					{/if}
				{:else}
					<div
						class="bg-manilla/60 dark:bg-manilla-dark dark:text-white rounded-b-xl! pt-0.5 pb-3 px-4 flex flex-col gap-2 text-xs"
					>
						<span class="text-gray-500 italic">
							{$i18n.t('{{COUNT}} hidden lines', {
								COUNT: code.split('\n').length
							})}
						</span>
					</div>
				{/if}
			</div>
		{/if}
	</div>
</div>
