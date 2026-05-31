<script lang="ts">
	import hljs from 'highlight.js';
	import { toast } from 'svelte-sonner';
	import { getContext, onMount, onDestroy } from 'svelte';

	import { copyToClipboard, renderMermaidDiagram, renderVegaVisualization } from '$lib/utils';

	import 'highlight.js/styles/github-dark.min.css';

	import CodeEditor from '$lib/components/common/CodeEditor.svelte';
	import SvgPanZoom from '$lib/components/common/SVGPanZoom.svelte';

	import ChevronUpDown from '$lib/components/icons/ChevronUpDown.svelte';

	const i18n = getContext('i18n');

	export let id = '';
	export let edit = true;

	export let onSave = (e) => {};
	export let onUpdate = (e) => {};
	export let onPreview = (e) => {};

	export let save = false;
	export let preview = false;
	export let collapsed = false;

	export let token;
	export let lang = '';
	export let code = '';

	export let className = 'mb-2';
	export let editorClassName = '';
	export let stickyButtonsClassName = 'top-0';

	let codeBlockElement = null;
	let visible = false;
	let observer = null;

	let _code = '';
	$: if (code) {
		updateCode();
	}

	const updateCode = () => {
		_code = code;
	};

	let _token = null;

	let mermaidHtml = null;
	let vegaHtml = null;

	let copied = false;
	let saved = false;

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

	$: if (token) {
		if (JSON.stringify(token) !== JSON.stringify(_token)) {
			_token = token;
		}
	}

	$: if (_token) {
		render();
	}

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
				class="absolute left-0 right-0 py-2.5 pr-3 text-text-300 pl-4.5 text-xs font-medium dark:text-white"
			>
				{lang}
			</div>

			<div
				class="sticky {stickyButtonsClassName} left-0 right-0 py-2 pr-3 flex items-center justify-end w-full z-10 text-xs text-black dark:text-white"
			>
				<div class="flex items-center gap-0.5">
					<button
						class="flex gap-1 items-center bg-none border-none transition rounded-md px-1.5 py-0.5 bg-white/70 dark:bg-gray-800"
						on:click={collapseCodeBlock}
					>
						<div class=" -translate-y-[0.5px]">
							<ChevronUpDown className="size-3" />
						</div>

						<div>
							{collapsed ? $i18n.t('Expand') : $i18n.t('Collapse')}
						</div>
					</button>

					{#if save}
						<button
							class="save-code-button bg-none border-none transition rounded-md px-1.5 py-0.5 bg-white/70 dark:bg-gray-800"
							on:click={saveCode}
						>
							{saved ? $i18n.t('Saved') : $i18n.t('Save')}
						</button>
					{/if}

					<button
						class="copy-code-button bg-none border-none transition rounded-md px-1.5 py-0.5 bg-white/70 dark:bg-gray-800"
						on:click={copyCode}>{copied ? $i18n.t('Copied') : $i18n.t('Copy')}</button
					>

					{#if preview && ['html', 'svg'].includes(lang)}
						<button
							class="flex gap-1 items-center preview-code-button bg-none border-none transition rounded-md px-1.5 py-0.5 bg-white/70 dark:bg-gray-800"
							on:click={previewCode}
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
					{#if edit}
						<CodeEditor
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
					{:else}
						<pre
							class=" hljs p-4 px-5 overflow-x-auto"
							style="border-top-left-radius: 0px; border-top-right-radius: 0px;"><code
								class="language-{lang} rounded-t-none whitespace-pre text-sm"
								>{#if visible}{@html hljs.highlightAuto(code, hljs.getLanguage(lang)?.aliases)
										.value || code}{:else}{code}{/if}</code
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
