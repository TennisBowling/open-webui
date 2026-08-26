<script lang="ts" module>
	// Lazy chunks kept off the first-message-render path. Each loader caches its
	// promise so repeated renders hand {#await} the SAME promise — a fresh
	// promise per render would re-enter the pending branch (visible churn while
	// streaming) even though the module itself is cached.
	let _codeBlockPromise: Promise<any> | null = null;
	const loadCodeBlock = () =>
		(_codeBlockPromise ??= import('$lib/components/chat/Messages/CodeBlock.svelte'));
	let _dataVizPromise: Promise<any> | null = null;
	const loadDataVizWidget = () =>
		(_dataVizPromise ??= import('$lib/components/chat/Messages/DataVizWidget.svelte'));
	let _subagentBlockPromise: Promise<any> | null = null;
	const loadSubagentBlock = () => (_subagentBlockPromise ??= import('./SubagentBlock.svelte'));
	// Lazy reasoning body fetcher (dynamic import also breaks the cycle
	// ReasoningText → Markdown → MarkdownTokens).
	let _reasoningTextPromise: Promise<any> | null = null;
	const loadReasoningText = () => (_reasoningTextPromise ??= import('../ReasoningText.svelte'));
</script>

<script lang="ts">
	import MarkdownTokens from './MarkdownTokens.svelte';
	import DOMPurify from 'dompurify';
	import { onMount, getContext } from 'svelte';
	const i18n = getContext('i18n');

	import fileSaver from 'file-saver';
	const { saveAs } = fileSaver;

	import { marked, type Token } from 'marked';
	import { unescapeHtml } from '$lib/utils';
	import { getCachedMarkdownTokens, setCachedMarkdownTokens } from '../Markdown.svelte';

	import { WEBUI_BASE_URL } from '$lib/constants';

	import MarkdownInlineTokens from '$lib/components/chat/Messages/Markdown/MarkdownInlineTokens.svelte';
	import KatexRenderer from './KatexRenderer.svelte';
	import AlertRenderer, { alertComponent } from './AlertRenderer.svelte';
	import Collapsible from '$lib/components/common/Collapsible.svelte';
	import Tooltip from '$lib/components/common/Tooltip.svelte';
	import Download from '$lib/components/icons/Download.svelte';

	import Source from './Source.svelte';
	import { settings } from '$lib/stores';
	import HtmlToken from './HTMLToken.svelte';

	const parseToolCallArguments = (raw: unknown): Record<string, any> => {
		if (raw == null) return {};
		try {
			let value: any = raw;
			if (typeof value === 'string') {
				value = unescapeHtml(value);
				value = JSON.parse(value);
			}
			// `function.arguments` is a JSON string per the OpenAI tool-call spec,
			// so the backend's `json.dumps(tool_arguments)` double-encodes it.
			if (typeof value === 'string') {
				value = JSON.parse(value);
			}
			return value && typeof value === 'object' ? value : {};
		} catch (err) {
			console.warn('Failed to parse tool_call arguments', err);
			return {};
		}
	};

	const normalizeReasoningDetailsText = (raw: unknown) => {
		const text = typeof raw === 'string' ? unescapeHtml(raw) : String(raw ?? '');
		return text
			.split('\n')
			.map((line) => line.replace(/^\s*>\s?/, ''))
			.join('\n')
			.trim();
	};

	interface Props {
		id: string;
		tokens: Token[];
		top?: boolean;
		done?: boolean;
		chatId?: string;
		messageId?: string;
		dataVizOverrides?: Record<string, string>;
		sandboxFiles?: any[];
		save?: boolean;
		preview?: boolean;
		editCodeBlock?: boolean;
		topPadding?: boolean;
		onSave?: Function;
		onUpdate?: Function;
		onPreview?: Function;
		onTaskClick?: Function;
		onSourceClick?: Function;
	}

	let {
		id,
		tokens,
		top = true,
		done = true,
		chatId = '',
		messageId = '',
		dataVizOverrides = {},
		sandboxFiles = [],
		save = false,
		preview = false,
		editCodeBlock = true,
		topPadding = false,
		onSave = () => {},
		onUpdate = () => {},
		onPreview = () => {},
		onTaskClick = () => {},
		onSourceClick = () => {}
	}: Props = $props();

	const headerComponent = (depth: number) => {
		return 'h' + depth;
	};

	// Reuse the module-level markdown token cache from Markdown.svelte for the
	// secondary/nested lex calls below (e.g. reasoning/details sub-content) so
	// re-renders of an unchanged nested block don't re-run a full marked.lexer
	// pass. Only cache once the message is `done`, matching the top-level
	// Markdown.svelte behavior (streaming content changes every render, so
	// caching it would just churn the LRU without benefit).
	const lexWithCache = (text: string) => {
		if (done) {
			const cached = getCachedMarkdownTokens(text);
			if (cached) return cached;
			const lexed = marked.lexer(text);
			setCachedMarkdownTokens(text, lexed);
			return lexed;
		}
		return marked.lexer(text);
	};

	const exportTableToCSVHandler = (token, tokenIdx = 0) => {
		console.log('Exporting table to CSV');

		// Extract header row text and escape for CSV.
		const header = token.header.map((headerCell) => `"${headerCell.text.replace(/"/g, '""')}"`);

		// Create an array for rows that will hold the mapped cell text.
		const rows = token.rows.map((row) =>
			row.map((cell) => {
				// Map tokens into a single text
				const cellContent = cell.tokens.map((token) => token.text).join('');
				// Escape double quotes and wrap the content in double quotes
				return `"${cellContent.replace(/"/g, '""')}"`;
			})
		);

		// Combine header and rows
		const csvData = [header, ...rows];

		// Join the rows using commas (,) as the separator and rows using newline (\n).
		const csvContent = csvData.map((row) => row.join(',')).join('\n');

		// Log rows and CSV content to ensure everything is correct.
		console.log(csvData);
		console.log(csvContent);

		// To handle Unicode characters, you need to prefix the data with a BOM:
		const bom = '\uFEFF'; // BOM for UTF-8

		// Create a new Blob prefixed with the BOM to ensure proper Unicode encoding.
		const blob = new Blob([bom + csvContent], { type: 'text/csv;charset=UTF-8' });

		// Use FileSaver.js's saveAs function to save the generated CSV file.
		saveAs(blob, `table-${id}-${tokenIdx}.csv`);
	};
</script>

<!-- {JSON.stringify(tokens)} -->
{#each tokens as token, tokenIdx (tokenIdx)}
	{#if token.type === 'hr'}
		<hr class=" border-gray-100 dark:border-gray-850" />
	{:else if token.type === 'heading'}
		<svelte:element this={headerComponent(token.depth)} dir="auto">
			<MarkdownInlineTokens
				id={`${id}-${tokenIdx}-h`}
				tokens={token.tokens}
				{done}
				{sandboxFiles}
				{onSourceClick}
			/>
		</svelte:element>
	{:else if token.type === 'code'}
		{#if token.raw.includes('```')}
			{#await loadCodeBlock() then CodeBlock}
				<CodeBlock.default
					id={`${id}-${tokenIdx}`}
					collapsed={$settings?.collapseCodeBlocks ?? false}
					{token}
					lang={token?.lang ?? ''}
					code={token?.text ?? ''}
					{save}
					{preview}
					edit={editCodeBlock}
					stickyButtonsClassName={topPadding ? 'top-7' : 'top-0'}
					onSave={(value) => {
						onSave({
							raw: token.raw,
							oldContent: token.text,
							newContent: value
						});
					}}
					{onUpdate}
					{onPreview}
				/>
			{/await}
		{:else}
			{token.text}
		{/if}
	{:else if token.type === 'table'}
		<div class="relative w-full group mb-2">
			<div class="scrollbar-hidden relative overflow-x-auto max-w-full">
				<table
					class=" w-full text-sm text-left text-gray-500 dark:text-gray-400 max-w-full rounded-xl"
				>
					<thead
						class="text-xs text-gray-700 uppercase bg-gray-50 dark:bg-gray-850 dark:text-gray-400 border-none"
					>
						<tr class="">
							{#each token.header as header, headerIdx}
								<th
									scope="col"
									class="px-2.5! py-2! cursor-pointer border-b-hairline border-gray-100! dark:border-gray-850!"
									style={token.align[headerIdx] ? '' : `text-align: ${token.align[headerIdx]}`}
								>
									<div class="gap-1.5 text-left">
										<div class="shrink-0 break-normal">
											<MarkdownInlineTokens
												id={`${id}-${tokenIdx}-header-${headerIdx}`}
												tokens={header.tokens}
												{done}
												{sandboxFiles}
												{onSourceClick}
											/>
										</div>
									</div>
								</th>
							{/each}
						</tr>
					</thead>
					<tbody>
						{#each token.rows as row, rowIdx}
							<tr class="bg-white dark:bg-gray-900 text-xs">
								{#each row ?? [] as cell, cellIdx}
									<td
										class="px-3! py-2! text-gray-900 dark:text-white w-max {token.rows.length -
											1 ===
										rowIdx
											? ''
											: 'border-b-hairline border-gray-100! dark:border-gray-850!'}"
										style={token.align[cellIdx] ? `text-align: ${token.align[cellIdx]}` : ''}
									>
										<div class="break-normal">
											<MarkdownInlineTokens
												id={`${id}-${tokenIdx}-row-${rowIdx}-${cellIdx}`}
												tokens={cell.tokens}
												{done}
												{sandboxFiles}
												{onSourceClick}
											/>
										</div>
									</td>
								{/each}
							</tr>
						{/each}
					</tbody>
				</table>
			</div>

			<div class=" absolute top-1 right-1.5 z-20 invisible group-hover:visible">
				<Tooltip content={$i18n.t('Export to CSV')}>
					<button
						class="tap-target p-1 max-md:p-2 rounded-lg bg-transparent transition"
						onclick={(e) => {
							e.stopPropagation();
							exportTableToCSVHandler(token, tokenIdx);
						}}
					>
						<Download className=" size-3.5" strokeWidth="1.5" />
					</button>
				</Tooltip>
			</div>
		</div>
	{:else if token.type === 'blockquote'}
		{@const alert = alertComponent(token)}
		{#if alert}
			<AlertRenderer {id} {tokenIdx} {token} {alert} {sandboxFiles} {onTaskClick} {onSourceClick} />
		{:else}
			<blockquote dir="auto">
				<MarkdownTokens
					id={`${id}-${tokenIdx}`}
					tokens={token.tokens}
					{done}
					{editCodeBlock}
					{chatId}
					{messageId}
					{dataVizOverrides}
					{sandboxFiles}
					{onTaskClick}
					{onSourceClick}
				/>
			</blockquote>
		{/if}
	{:else if token.type === 'list'}
		{#if token.ordered}
			<ol start={token.start || 1} dir="auto">
				{#each token.items as item, itemIdx}
					<li class="text-start">
						{#if item?.task}
							<input
								class=" translate-y-[1px] -translate-x-1"
								type="checkbox"
								checked={item.checked}
								onchange={(e) => {
									onTaskClick({
										id: id,
										token: token,
										tokenIdx: tokenIdx,
										item: item,
										itemIdx: itemIdx,
										checked: e.target.checked
									});
								}}
							/>
						{/if}

						<MarkdownTokens
							id={`${id}-${tokenIdx}-${itemIdx}`}
							tokens={item.tokens}
							top={token.loose}
							{done}
							{editCodeBlock}
							{chatId}
							{messageId}
							{dataVizOverrides}
							{sandboxFiles}
							{onTaskClick}
							{onSourceClick}
						/>
					</li>
				{/each}
			</ol>
		{:else}
			<ul dir="auto" class="">
				{#each token.items as item, itemIdx}
					<li class="text-start {item?.task ? 'flex -translate-x-6.5 gap-3 ' : ''}">
						{#if item?.task}
							<input
								class=""
								type="checkbox"
								checked={item.checked}
								onchange={(e) => {
									onTaskClick({
										id: id,
										token: token,
										tokenIdx: tokenIdx,
										item: item,
										itemIdx: itemIdx,
										checked: e.target.checked
									});
								}}
							/>

							<div>
								<MarkdownTokens
									id={`${id}-${tokenIdx}-${itemIdx}`}
									tokens={item.tokens}
									top={token.loose}
									{done}
									{editCodeBlock}
									{chatId}
									{messageId}
									{dataVizOverrides}
									{sandboxFiles}
									{onTaskClick}
									{onSourceClick}
								/>
							</div>
						{:else}
							<MarkdownTokens
								id={`${id}-${tokenIdx}-${itemIdx}`}
								tokens={item.tokens}
								top={token.loose}
								{done}
								{editCodeBlock}
								{chatId}
								{messageId}
								{dataVizOverrides}
								{sandboxFiles}
								{onTaskClick}
								{onSourceClick}
							/>
						{/if}
					</li>
				{/each}
			</ul>
		{/if}
	{:else if token.type === 'details' && token?.attributes?.type === 'tool_calls' && token?.attributes?.name === 'show_widget'}
		{@const args = parseToolCallArguments(token.attributes.arguments)}
		{#await loadDataVizWidget() then DataVizWidget}
			<DataVizWidget.default
				title={args.title ?? 'widget'}
				widgetCode={args.widget_code ?? ''}
				loadingMessages={Array.isArray(args.loading_messages) ? args.loading_messages : []}
				{chatId}
				{messageId}
				{dataVizOverrides}
			/>
		{/await}
	{:else if token.type === 'details' && token?.attributes?.type === 'subagent_launch'}
		{#await loadSubagentBlock() then SubagentBlock}
			<SubagentBlock.default
				attributes={token.attributes}
				parentChatId={chatId}
				parentMessageId={messageId}
				{sandboxFiles}
			/>
		{/await}
	{:else if token.type === 'details'}
		{@const isReasoning = token?.attributes?.type === 'reasoning'}
		{@const reasoningId = isReasoning ? `${id}-${tokenIdx}` : ''}
		<Collapsible
			title={token.summary}
			id={reasoningId}
			reasoningKey={reasoningId}
			open={isReasoning ? undefined : ($settings?.expandDetails ?? false)}
			attributes={token?.attributes}
			className="w-full space-y-1"
			dir="auto"
		>
			{#snippet content()}
				<div class=" mb-1.5">
					{#if token?.attributes?.type === 'reasoning'}
						<div class="text-sm text-gray-600 dark:text-gray-400">
							{#if token?.attributes?.content_lazy === 'true'}
								<!-- Server withheld the reasoning text (lazy stub). This slot
								     only mounts while the block is expanded, so the fetch fires
								     exactly on first expand. Attr-embedded ids win so subagent
								     transcripts (another chat's blocks) fetch from the right row. -->
								{#await loadReasoningText() then ReasoningText}
									<ReasoningText.default
										id={`${id}-${tokenIdx}-d`}
										chatId={token?.attributes?.chat_id || chatId}
										messageId={token?.attributes?.message_id || messageId}
										contentRef={token?.attributes?.content_ref ?? ''}
									/>
								{/await}
							{:else}
								<MarkdownTokens
									id={`${id}-${tokenIdx}-d`}
									tokens={lexWithCache(normalizeReasoningDetailsText(token.text))}
									attributes={token?.attributes}
									{done}
									{editCodeBlock}
									{chatId}
									{messageId}
									{dataVizOverrides}
									{sandboxFiles}
									{onTaskClick}
									{onSourceClick}
								/>
							{/if}
						</div>
					{:else}
						<MarkdownTokens
							id={`${id}-${tokenIdx}-d`}
							tokens={lexWithCache(token.text)}
							attributes={token?.attributes}
							{done}
							{editCodeBlock}
							{chatId}
							{messageId}
							{dataVizOverrides}
							{sandboxFiles}
							{onTaskClick}
							{onSourceClick}
						/>
					{/if}
				</div>
			{/snippet}
		</Collapsible>
	{:else if token.type === 'html'}
		<HtmlToken {id} {token} {onSourceClick} />
	{:else if token.type === 'iframe'}
		<iframe
			src="{WEBUI_BASE_URL}/api/v1/files/{token.fileId}/content"
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
	{:else if token.type === 'paragraph'}
		<p dir="auto">
			<MarkdownInlineTokens
				id={`${id}-${tokenIdx}-p`}
				tokens={token.tokens ?? []}
				{done}
				{sandboxFiles}
				{onSourceClick}
			/>
		</p>
	{:else if token.type === 'text'}
		{#if top}
			<p>
				{#if token.tokens}
					<MarkdownInlineTokens
						id={`${id}-${tokenIdx}-t`}
						tokens={token.tokens}
						{done}
						{sandboxFiles}
						{onSourceClick}
					/>
				{:else}
					{unescapeHtml(token.text)}
				{/if}
			</p>
		{:else if token.tokens}
			<MarkdownInlineTokens
				id={`${id}-${tokenIdx}-p`}
				tokens={token.tokens ?? []}
				{done}
				{sandboxFiles}
				{onSourceClick}
			/>
		{:else}
			{unescapeHtml(token.text)}
		{/if}
	{:else if token.type === 'inlineKatex'}
		{#if token.text}
			<KatexRenderer content={token.text} displayMode={token?.displayMode ?? false} />
		{/if}
	{:else if token.type === 'blockKatex'}
		{#if token.text}
			<KatexRenderer content={token.text} displayMode={token?.displayMode ?? false} />
		{/if}
	{:else if token.type === 'space'}
		<div class="my-2"></div>
	{:else}
		{console.log('Unknown token', token)}
	{/if}
{/each}
