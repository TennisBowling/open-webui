<script lang="ts">
	import DOMPurify from 'dompurify';
	import { toast } from 'svelte-sonner';

	import type { Token } from 'marked';
	import { getContext } from 'svelte';

	const i18n = getContext('i18n');

	import { WEBUI_BASE_URL } from '$lib/constants';
	import { copyToClipboard, unescapeHtml } from '$lib/utils';

	import Image from '$lib/components/common/Image.svelte';
	import KatexRenderer from './KatexRenderer.svelte';
	import Source from './Source.svelte';
	import HtmlToken from './HTMLToken.svelte';
	import TextToken from './MarkdownInlineTokens/TextToken.svelte';
	import CodespanToken from './MarkdownInlineTokens/CodespanToken.svelte';
	import MentionToken from './MarkdownInlineTokens/MentionToken.svelte';
	import {
		previewFile,
		showArtifacts,
		showCallOverlay,
		showControls,
		showEmbeds,
		showFilePreview,
		showOverview
	} from '$lib/stores';

	export let id: string;
	export let done = true;
	export let tokens: Token[];
	export let sandboxFiles: any[] = [];
	export let onSourceClick: Function = () => {};

	const normalizeSandboxPath = (value: string) => {
		try {
			value = decodeURIComponent(value);
		} catch {
			// keep original
		}
		return value
			.replace(/^sandbox:\/\/workspace\/?/, '')
			.replace(/^\/workspace\/?/, '')
			.replace(/^\/+/, '');
	};

	const isSandboxHref = (href: string) =>
		href?.startsWith('sandbox:/workspace') || href?.startsWith('sandbox://workspace');

	const resolveSandboxFile = (href: string) => {
		if (!isSandboxHref(href)) return null;
		const rel = normalizeSandboxPath(href);
		const candidates = new Set([rel]);
		if (rel && !rel.startsWith('outputs/')) candidates.add(`outputs/${rel}`);

		return (sandboxFiles ?? []).find((file) => {
			const workspacePath = normalizeSandboxPath(file?.container_workspace?.workspace_path ?? '');
			return candidates.has(workspacePath);
		});
	};

	const openSandboxFile = (file: any) => {
		previewFile.set(file);
		showOverview.set(false);
		showArtifacts.set(false);
		showEmbeds.set(false);
		showCallOverlay.set(false);
		showFilePreview.set(true);
		showControls.set(true);
	};

	const handleSandboxClick = (href: string) => {
		const file = resolveSandboxFile(href);
		if (file) {
			openSandboxFile(file);
			return;
		}
		toast.error('This sandbox file is not available as a preview yet.');
	};
</script>

{#each tokens as token}
	{#if token.type === 'escape'}
		{unescapeHtml(token.text)}
	{:else if token.type === 'html'}
		<HtmlToken {id} {token} {onSourceClick} />
	{:else if token.type === 'link'}
		{#if isSandboxHref(token.href)}
			<button
				type="button"
				class="underline text-blue-600 dark:text-blue-400 hover:text-blue-800 dark:hover:text-blue-300"
				title={token.title}
				on:click={() => handleSandboxClick(token.href)}
			>
				{#if token.tokens}
					<svelte:self id={`${id}-a`} tokens={token.tokens} {sandboxFiles} {onSourceClick} {done} />
				{:else}
					{token.text}
				{/if}
			</button>
		{:else if token.tokens}
			<a href={token.href} target="_blank" rel="nofollow" title={token.title}>
				<svelte:self id={`${id}-a`} tokens={token.tokens} {sandboxFiles} {onSourceClick} {done} />
			</a>
		{:else}
			<a href={token.href} target="_blank" rel="nofollow" title={token.title}>{token.text}</a>
		{/if}
	{:else if token.type === 'image'}
		<Image src={token.href} alt={token.text} />
	{:else if token.type === 'strong'}
		<strong><svelte:self id={`${id}-strong`} tokens={token.tokens} {sandboxFiles} {onSourceClick} /></strong>
	{:else if token.type === 'em'}
		<em><svelte:self id={`${id}-em`} tokens={token.tokens} {sandboxFiles} {onSourceClick} /></em>
	{:else if token.type === 'codespan'}
		<CodespanToken {token} {done} />
	{:else if token.type === 'br'}
		<br />
	{:else if token.type === 'del'}
		<del><svelte:self id={`${id}-del`} tokens={token.tokens} {sandboxFiles} {onSourceClick} /></del>
	{:else if token.type === 'inlineKatex'}
		{#if token.text}
			<KatexRenderer content={token.text} displayMode={false} />
		{/if}
	{:else if token.type === 'iframe'}
		<iframe
			src="{WEBUI_BASE_URL}/api/v1/files/{token.fileId}/content"
			title={token.fileId}
			width="100%"
			frameborder="0"
			onload="this.style.height=(this.contentWindow.document.body.scrollHeight+20)+'px';"
		></iframe>
	{:else if token.type === 'mention'}
		<MentionToken {token} />
	{:else if token.type === 'text'}
		<TextToken {token} {done} />
	{/if}
{/each}
