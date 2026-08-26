<script lang="ts">
	import { getContext } from 'svelte';
	import { getChatMessageCompaction } from '$lib/apis/chats';
	import Spinner from '$lib/components/common/Spinner.svelte';

	const i18n: any = getContext('i18n');

	// The transcript marker for one conversation compaction (see
	// backend/open_webui/utils/COMPACTION.md §8). Everything BEFORE this point in
	// the conversation was replaced, in the outbound payload only, by a summary —
	// nothing was deleted, so the blocks above still render normally and their
	// tool results still expand.
	//
	// Renders as a divider like the rewind boundary, and expands to the full
	// <compacted_context> the model actually received. Because we author that
	// summary ourselves it is fully human-readable — the point being that you can
	// read it and verify what was preserved.

	interface Props {
		block: {
			compacted_at?: string;
			narrative?: string;
			covers?: number;
			tokens?: number;
			context_length?: number;
		};
		chatId?: string;
		messageId?: string;
		blockIndex?: number;
	}

	let { block, chatId = '', messageId = '', blockIndex = 0 }: Props = $props();

	let expanded = $state(false);
	let loading = $state(false);
	let envelope = $state('');
	let envelopeSource = $state('');
	let loadError = $state('');

	const compactNumber = (n: number): string => {
		if (!Number.isFinite(n) || n <= 0) return '';
		if (n < 1000) return `${n}`;
		if (n < 1_000_000) return `${(n / 1000).toFixed(1).replace(/\.0$/, '')}k`;
		return `${(n / 1_000_000).toFixed(1).replace(/\.0$/, '')}M`;
	};

	// `compacted_at` is a persisted UTC stamp (never recomputed — the outbound
	// bytes depend on it), rendered in the reader's local time.
	let clock = $derived.by(() => {
		const raw = block?.compacted_at;
		if (!raw) return '';
		const d = new Date(raw);
		if (Number.isNaN(d.getTime())) return '';
		return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
	});

	let summary = $derived.by(() => {
		const parts: string[] = [];
		const covers = Number(block?.covers ?? 0);
		if (covers > 0) {
			parts.push($i18n.t('{{count}} messages', { count: covers }));
		}
		const tokens = compactNumber(Number(block?.tokens ?? 0));
		if (tokens) parts.push($i18n.t('{{tokens}} tokens', { tokens }));
		if (clock) parts.push(clock);
		return parts.join(' · ');
	});

	const toggle = async () => {
		expanded = !expanded;
		if (!expanded || envelope || loading) return;
		if (!chatId || !messageId || chatId.startsWith('local:')) {
			// No durable row to re-render from; the narrative below is still shown.
			return;
		}
		loading = true;
		loadError = '';
		try {
			const res = await getChatMessageCompaction(localStorage.token, chatId, messageId, blockIndex);
			envelope = res?.compacted_context ?? '';
			envelopeSource = res?.source ?? '';
		} catch (e) {
			loadError = $i18n.t('Could not load the compacted context');
			console.error(e);
		} finally {
			loading = false;
		}
	};

	// The narrative is always available on the block itself, so the panel is
	// never empty even when the envelope fetch fails or the chat is local-only.
	let fallbackText = $derived(block?.narrative ?? '');

	// Say plainly which of the three this is. "Sent" is a stored copy of the
	// wire payload; "rendered" is a reconstruction for anchors written before the
	// capture existed, and can differ from what actually went out.
	let provenance = $derived.by(() => {
		if (!envelope) {
			return $i18n.t('Summary only — the full request payload was not recorded.');
		}
		if (envelopeSource === 'sent') {
			return $i18n.t('Exactly what was sent to the model.');
		}
		return $i18n.t(
			'Reconstructed from the conversation — the exact payload was not recorded, so this may differ slightly from what was sent.'
		);
	});
</script>

<div class="my-2.5" dir="ltr">
	<div class="group/cp relative flex items-center justify-center py-1 select-none">
		<div
			class="absolute inset-x-0 top-1/2 -translate-y-1/2 h-px bg-gray-100 dark:bg-gray-800/80 group-hover/cp:bg-gray-300 dark:group-hover/cp:bg-gray-600 transition-colors duration-150"
		></div>
		<button
			type="button"
			onclick={toggle}
			aria-expanded={expanded}
			class="relative z-10 inline-flex items-center gap-1.5 text-[10px] font-medium text-gray-400 dark:text-gray-500 bg-white dark:bg-gray-900 px-2 py-0.5 hover:text-gray-700 dark:hover:text-gray-200 transition"
			title={$i18n.t('Context compacted')}
		>
			<svg
				xmlns="http://www.w3.org/2000/svg"
				viewBox="0 0 16 16"
				fill="currentColor"
				class="size-3 shrink-0"
				aria-hidden="true"
			>
				<path
					d="M2.75 2a.75.75 0 0 0 0 1.5h10.5a.75.75 0 0 0 0-1.5H2.75Zm2 3.5a.75.75 0 0 0 0 1.5h6.5a.75.75 0 0 0 0-1.5h-6.5Zm-2 3.5a.75.75 0 0 0 0 1.5h10.5a.75.75 0 0 0 0-1.5H2.75Zm2 3.5a.75.75 0 0 0 0 1.5h6.5a.75.75 0 0 0 0-1.5h-6.5Z"
				/>
			</svg>
			<span class="truncate">
				{$i18n.t('Context compacted')}{summary ? ` · ${summary}` : ''}
			</span>
			<svg
				xmlns="http://www.w3.org/2000/svg"
				viewBox="0 0 20 20"
				fill="currentColor"
				class="size-3 shrink-0 transition-transform duration-150 {expanded ? 'rotate-180' : ''}"
				aria-hidden="true"
			>
				<path
					fill-rule="evenodd"
					d="M5.22 8.22a.75.75 0 0 1 1.06 0L10 11.94l3.72-3.72a.75.75 0 1 1 1.06 1.06l-4.25 4.25a.75.75 0 0 1-1.06 0L5.22 9.28a.75.75 0 0 1 0-1.06Z"
					clip-rule="evenodd"
				/>
			</svg>
		</button>
	</div>

	{#if expanded}
		<div
			class="mt-1.5 rounded-2xl border-hairline border-gray-200 dark:border-gray-800 bg-gray-50/60 dark:bg-gray-850/40 p-3"
		>
			<p class="text-[11px] leading-4 text-gray-500 dark:text-gray-400 mb-2">
				{$i18n.t(
					'Earlier messages were replaced with this summary in the request sent to the model. Nothing was deleted — the messages above still show their full content.'
				)}
			</p>
			{#if loading}
				<div class="flex items-center gap-2 text-xs text-gray-500 dark:text-gray-400">
					<Spinner className="size-3" />
					{$i18n.t('Loading')}
				</div>
			{:else}
				{#if loadError}
					<p class="text-[11px] text-gray-500 dark:text-gray-400 mb-2">{loadError}</p>
				{:else}
					<p class="text-[11px] leading-4 text-gray-400 dark:text-gray-500 mb-2 italic">
						{provenance}
					</p>
				{/if}
				<pre
					class="whitespace-pre-wrap break-words text-[11px] leading-[1.45] font-mono text-gray-700 dark:text-gray-200 max-h-[28rem] overflow-y-auto m-0">{envelope ||
						fallbackText}</pre>
			{/if}
		</div>
	{/if}
</div>
