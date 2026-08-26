<script lang="ts">
	import { getContext } from 'svelte';
	import type { Writable } from 'svelte/store';
	import type { i18n as i18nType } from 'i18next';

	import { parseGenericToolResult, type GenericToolResult } from '$lib/utils/toolResults';
	import CodePane from './CodePane.svelte';

	const i18n = getContext<Writable<i18nType>>('i18n');

	interface Props {
		id?: string;
		resultRaw?: unknown;
		errored?: boolean;
	}

	let { id = '', resultRaw = '', errored = false }: Props = $props();

	let parsed: GenericToolResult = $derived(parseGenericToolResult(resultRaw, errored));
</script>

<div {id}>
	{#if parsed.kind === 'empty'}
		<div
			class="rounded-xl border-hairline border-dashed border-gray-200 px-3 py-5 text-center text-xs text-gray-400 dark:border-gray-800 dark:text-gray-500"
		>
			{$i18n.t('This tool returned no output.')}
		</div>
	{:else if parsed.kind === 'error'}
		<div
			class="rounded-xl border-hairline border-error-brick/20 bg-error-brick/5 px-3 py-2.5 dark:bg-error-brick/10"
		>
			<div class="flex gap-2.5 text-error-brick dark:text-error-brick-dark">
				<svg
					xmlns="http://www.w3.org/2000/svg"
					viewBox="0 0 20 20"
					fill="currentColor"
					class="mt-px size-4 shrink-0"
					aria-hidden="true"
				>
					<path
						fill-rule="evenodd"
						d="M10 18a8 8 0 1 0 0-16 8 8 0 0 0 0 16ZM5.5 5.5l9 9-1 1-9-9 1-1Z"
						clip-rule="evenodd"
					/>
				</svg>
				<div class="min-w-0 text-sm leading-snug">{parsed.message}</div>
			</div>
			{#if parsed.details}
				<pre
					class="mt-2 max-h-56 overflow-auto whitespace-pre-wrap break-words border-t-hairline border-error-brick/15 pt-2 font-mono text-xs leading-relaxed text-error-brick/80 dark:text-error-brick-dark/80">{parsed.details}</pre>
			{/if}
		</div>
	{:else if parsed.kind === 'shell'}
		<div class="space-y-2">
			{#if parsed.stdout}
				<CodePane
					text={parsed.stdout}
					label={$i18n.t('stdout')}
					meta={parsed.exitCode === 0 || parsed.exitCode === null
						? ''
						: $i18n.t('exit {{CODE}}', { CODE: parsed.exitCode })}
					copyable
				/>
			{/if}
			{#if parsed.stderr}
				<CodePane text={parsed.stderr} label={$i18n.t('stderr')} tone="error" copyable />
			{/if}
			{#if !parsed.stdout && !parsed.stderr}
				<div
					class="flex items-center justify-between gap-2 rounded-xl border-hairline border-dashed border-gray-200 px-3 py-4 text-xs text-gray-400 dark:border-gray-800 dark:text-gray-500"
				>
					<span>{$i18n.t('No output.')}</span>
					<span class="font-mono">{$i18n.t('exit {{CODE}}', { CODE: parsed.exitCode ?? 0 })}</span>
				</div>
			{/if}
		</div>
	{:else if parsed.kind === 'json'}
		<CodePane text={parsed.text} label="JSON" copyable />
	{:else}
		<CodePane text={parsed.text} copyable={false} />
	{/if}
</div>
