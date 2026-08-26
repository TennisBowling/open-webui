<script lang="ts">
	import { getContext } from 'svelte';
	import type { Writable } from 'svelte/store';
	import type { i18n as i18nType } from 'i18next';

	import { getToolArgEntries, getToolEditDiff } from '$lib/utils/toolResults';
	import CodePane from './CodePane.svelte';

	const i18n = getContext<Writable<i18nType>>('i18n');

	interface Props {
		id?: string;
		name?: string;
		argsRaw?: unknown;
	}

	let { id = '', name = '', argsRaw = '' }: Props = $props();

	let diff = $derived(getToolEditDiff(name, argsRaw));
	let entries = $derived(getToolArgEntries(argsRaw));
	// The two halves of the diff are already shown side by side; repeating them
	// as raw arguments below would double the panel's height for no new
	// information.
	let restEntries = $derived(
		diff ? entries.filter((entry) => entry.key !== 'old_string' && entry.key !== 'new_string') : entries
	);
</script>

<div class="space-y-2" {id}>
	{#if diff}
		<div
			class="overflow-hidden rounded-xl border-hairline border-gray-200 bg-white dark:border-gray-800 dark:bg-gray-950/50"
		>
			{#if diff.path}
				<div
					class="truncate border-b-hairline border-gray-200 px-3 py-1.5 font-mono text-[11px] text-gray-500 dark:border-gray-800 dark:text-gray-400"
				>
					{diff.path}
				</div>
			{/if}
			<div class="max-h-[44vh] overflow-auto">
				{#if diff.oldText}
					<!-- Labelled, not colour-only: the two tints are deliberately faint so
					     the code stays the loudest thing in the panel. -->
					<div
						class="border-l-2 border-error-brick/40 bg-error-brick/8 px-3 py-2 dark:bg-error-brick/12"
					>
						<div
							class="mb-1 text-[10px] font-medium uppercase tracking-[0.08em] text-error-brick/70 dark:text-error-brick-dark/70"
						>
							{$i18n.t('Replaced')}
						</div>
						<pre
							class="whitespace-pre-wrap break-words font-mono text-xs leading-relaxed text-gray-700 dark:text-gray-300">{diff.oldText}</pre>
					</div>
				{/if}
				{#if diff.newText}
					<div class="border-l-2 border-success/40 bg-success/8 px-3 py-2 dark:bg-success/12">
						<div
							class="mb-1 text-[10px] font-medium uppercase tracking-[0.08em] text-success/80 dark:text-success-dark/80"
						>
							{$i18n.t('With')}
						</div>
						<pre
							class="whitespace-pre-wrap break-words font-mono text-xs leading-relaxed text-gray-700 dark:text-gray-300">{diff.newText}</pre>
					</div>
				{/if}
			</div>
		</div>
	{/if}

	{#if restEntries.length === 0 && !diff}
		<div
			class="rounded-xl border-hairline border-dashed border-gray-200 px-3 py-5 text-center text-xs text-gray-400 dark:border-gray-800 dark:text-gray-500"
		>
			{$i18n.t('This tool was called without arguments.')}
		</div>
	{:else}
		{@const inlineEntries = restEntries.filter((entry) => entry.kind === 'inline')}
		{@const blockEntries = restEntries.filter((entry) => entry.kind === 'block')}

		{#if inlineEntries.length}
			<dl
				class="divide-y-hairline divide-gray-200/70 overflow-hidden rounded-xl border-hairline border-gray-200 bg-white dark:divide-gray-800 dark:border-gray-800 dark:bg-gray-950/50"
			>
				{#each inlineEntries as entry (entry.key)}
					<div class="flex gap-3 px-3 py-1.5">
						<dt class="w-28 shrink-0 truncate text-xs text-gray-400 dark:text-gray-500">
							{entry.key}
						</dt>
						<dd class="min-w-0 flex-1 break-words font-mono text-xs text-gray-700 dark:text-gray-300">
							{entry.value}
						</dd>
					</div>
				{/each}
			</dl>
		{/if}

		{#each blockEntries as entry (entry.key)}
			<CodePane text={entry.value} label={entry.key} copyable />
		{/each}
	{/if}
</div>
