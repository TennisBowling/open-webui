<script lang="ts">
	import MarkdownInlineTokens from './MarkdownInlineTokens.svelte';
	import DOMPurify from 'dompurify';
	import { toast } from '$lib/utils/toast';

	import type { Token } from 'marked';
	import { getContext } from 'svelte';

	const i18n = getContext('i18n');

	import { WEBUI_API_BASE_URL } from '$lib/constants';
	import { copyToClipboard, unescapeHtml } from '$lib/utils';
	import {
		isSandboxHref,
		isImageSandboxFile,
		resolveSandboxFile,
		sandboxFileContentUrl,
		fileContentUrl
	} from '$lib/utils/sandbox';

	import Image from '$lib/components/common/Image.svelte';
	import KatexRenderer from './KatexRenderer.svelte';
	import Source from './Source.svelte';
	import HtmlToken from './HTMLToken.svelte';
	import TextToken from './MarkdownInlineTokens/TextToken.svelte';
	import CodespanToken from './MarkdownInlineTokens/CodespanToken.svelte';
	import MentionToken from './MarkdownInlineTokens/MentionToken.svelte';
	import { openFilePreview, sharedContext } from '$lib/stores';

	interface Props {
		id: string;
		done?: boolean;
		tokens: Token[];
		sandboxFiles?: any[];
		onSourceClick?: Function;
	}

	let { id, done = true, tokens, sandboxFiles = [], onSourceClick = () => {} }: Props = $props();

	let shareId = $derived($sharedContext.shareId);

	const openSandboxFile = (file: any) => {
		openFilePreview(file, sandboxFiles);
	};

	const handleSandboxClick = (href: string) => {
		const file = resolveSandboxFile(href, sandboxFiles);
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
				class="underline text-book-cloth dark:text-kraft hover:text-kraft dark:hover:text-book-cloth"
				title={token.title}
				onclick={() => handleSandboxClick(token.href)}
			>
				{#if token.tokens}
					<MarkdownInlineTokens
						id={`${id}-a`}
						tokens={token.tokens}
						{sandboxFiles}
						{onSourceClick}
						{done}
					/>
				{:else}
					{token.text}
				{/if}
			</button>
		{:else if token.tokens}
			<a href={token.href} target="_blank" rel="nofollow" title={token.title}>
				<MarkdownInlineTokens
					id={`${id}-a`}
					tokens={token.tokens}
					{sandboxFiles}
					{onSourceClick}
					{done}
				/>
			</a>
		{:else}
			<a href={token.href} target="_blank" rel="nofollow" title={token.title}>{token.text}</a>
		{/if}
	{:else if token.type === 'image'}
		{#if isSandboxHref(token.href)}
			{@const sandboxImage = resolveSandboxFile(token.href, sandboxFiles)}
			{#if sandboxImage && isImageSandboxFile(sandboxImage)}
				<Image
					src={sandboxFileContentUrl(sandboxImage, WEBUI_API_BASE_URL, shareId)}
					alt={token.text}
				/>
			{:else if sandboxImage}
				<!-- Image syntax pointing at a non-image generated file (pdf/audio/docx):
				     open it in the preview panel instead of a broken <img>. -->
				<button
					type="button"
					class="underline text-book-cloth dark:text-kraft hover:text-kraft dark:hover:text-book-cloth"
					onclick={() => openSandboxFile(sandboxImage)}
				>
					{token.text || sandboxImage?.name || sandboxImage?.file?.meta?.name || 'file'}
				</button>
			{:else}
				<!-- Unresolved sandbox file: graceful toast, same as the link path —
				     never hand the non-loadable sandbox: scheme to <img>. -->
				<button
					type="button"
					class="underline text-book-cloth dark:text-kraft hover:text-kraft dark:hover:text-book-cloth"
					onclick={() => handleSandboxClick(token.href)}
				>
					{token.text || 'file'}
				</button>
			{/if}
		{:else}
			<Image src={token.href} alt={token.text} />
		{/if}
	{:else if token.type === 'strong'}
		<strong
			><MarkdownInlineTokens
				id={`${id}-strong`}
				tokens={token.tokens}
				{sandboxFiles}
				{onSourceClick}
			/></strong
		>
	{:else if token.type === 'em'}
		<em
			><MarkdownInlineTokens
				id={`${id}-em`}
				tokens={token.tokens}
				{sandboxFiles}
				{onSourceClick}
			/></em
		>
	{:else if token.type === 'codespan'}
		<CodespanToken {token} {done} />
	{:else if token.type === 'br'}
		<br />
	{:else if token.type === 'del'}
		<del
			><MarkdownInlineTokens
				id={`${id}-del`}
				tokens={token.tokens}
				{sandboxFiles}
				{onSourceClick}
			/></del
		>
	{:else if token.type === 'inlineKatex'}
		{#if token.text}
			<KatexRenderer content={token.text} displayMode={false} />
		{/if}
	{:else if token.type === 'iframe'}
		<iframe
			src={fileContentUrl(token.fileId, WEBUI_API_BASE_URL, { shareId })}
			title={token.fileId}
			width="100%"
			frameborder="0"
			onload={(event) => {
				const frame = event.currentTarget;
				try {
					const bodyHeight = frame.contentDocument?.body?.scrollHeight;
					if (bodyHeight) frame.style.height = `${bodyHeight + 20}px`;
				} catch {
					// Cross-origin frames are intentionally not introspectable.
				}
			}}
		></iframe>
	{:else if token.type === 'mention'}
		<MentionToken {token} />
	{:else if token.type === 'text'}
		<TextToken {token} {done} />
	{/if}
{/each}
