<script lang="ts">
	import { getContext, onMount } from 'svelte';
	import { slide } from 'svelte/transition';
	import { toast } from 'svelte-sonner';

	import { chatId, questionStates } from '$lib/stores';
	import { patchChat } from '$lib/apis/chats';
	import Spinner from '$lib/components/common/Spinner.svelte';

	const i18n: any = getContext('i18n');

	// `attributes` is parsed by Collapsible.svelte from the ask_user tool call.
	// Keys we read:
	//   tool_call_id      — primary key into questionStates + the durable answer write
	//   arguments         — JSON of {questions: [...]}, the question definitions
	//   result            — the tool result string (present once answered/skipped)
	//   done              — "true" once the parent tool result lands (card locks)
	//   message_terminated — "true" if the generation ended (Stop) — orphan guard
	//   chat_id           — the chat we write the answer into
	export let attributes: Record<string, any> = {};

	const decodeHtml = (value: unknown) => {
		if (typeof value !== 'string') return value ?? '';
		if (typeof document === 'undefined') return value;
		const textarea = document.createElement('textarea');
		textarea.innerHTML = value;
		return textarea.value;
	};

	const parseArgs = (raw: unknown): Record<string, any> => {
		try {
			let value: any = decodeHtml(raw);
			if (typeof value === 'string') value = JSON.parse(value);
			// Backend/client projections can double-encode function.arguments.
			if (typeof value === 'string') value = JSON.parse(value);
			return value && typeof value === 'object' ? value : {};
		} catch {
			return {};
		}
	};

	type Option = { label: string; description?: string };
	type Question = {
		question: string;
		header?: string;
		options: Option[];
		multiSelect: boolean;
		allowOther: boolean;
	};

	const normalizeQuestions = (raw: any): Question[] => {
		let list = raw?.questions ?? raw;
		if (typeof list === 'string') {
			try {
				list = JSON.parse(list);
			} catch {
				list = [];
			}
		}
		if (!Array.isArray(list)) return [];
		return list
			.map((q: any): Question | null => {
				if (!q || typeof q !== 'object') return null;
				const question = `${q.question ?? ''}`.trim();
				if (!question) return null;
				const rawOptions = Array.isArray(q.options) ? q.options : [];
				const options: Option[] = rawOptions
					.map((o: any): Option | null => {
						if (typeof o === 'string') return { label: o };
						if (o && typeof o === 'object' && `${o.label ?? ''}`.trim())
							return { label: `${o.label}`.trim(), description: o.description ?? '' };
						return null;
					})
					.filter((o: Option | null): o is Option => o != null);
				return {
					question,
					header: `${q.header ?? ''}`.trim(),
					options,
					multiSelect: !!q.multiSelect,
					// Free-form questions (no options) always take text input.
					allowOther: q.allowOther !== false || options.length === 0
				};
			})
			.filter((q: Question | null): q is Question => q != null);
	};

	$: toolCallId = (attributes?.tool_call_id || '') as string;
	$: questions = normalizeQuestions(parseArgs(attributes?.arguments));
	$: answeredByResult = String(attributes?.done) === 'true';
	$: messageTerminated = String(attributes?.message_terminated) === 'true';

	// The durable per-question state for this tool call (draft + submitted answer).
	$: storeEntry = $questionStates?.[toolCallId];
	$: submittedAnswer =
		storeEntry?.answer && typeof storeEntry.answer === 'object' ? storeEntry.answer : null;
	$: wasSkipped = !!storeEntry?.skipped;

	// Locked = the question is resolved. Either the tool result landed (answer is
	// in the message now), or the durable store records a submit/skip.
	$: locked = answeredByResult || submittedAnswer != null || wasSkipped;

	// Orphaned = generation ended without an answer (e.g. user pressed Stop while
	// the card was waiting). Disable the form; it can no longer resume anything.
	$: orphaned = !locked && messageTerminated;

	// --- local form state, keyed by question index ---
	// selected[i] = array of chosen labels; other[i] = free-text; otherOn[i] =
	// whether the "Other" radio/row is the active choice for a single-select.
	let selected: Record<number, string[]> = {};
	let otherText: Record<number, string> = {};
	let otherOn: Record<number, boolean> = {};
	let submitting = false;
	let seededFor = '';

	// Seed local state from the durable draft / submitted answer exactly once per
	// tool call (and re-seed if the tool call id changes, e.g. switching chats).
	// This is what restores partial selections after a reload mid-answering.
	$: if (toolCallId && seededFor !== toolCallId && questions.length) {
		seedFromStore();
	}

	const seedFromStore = () => {
		const source = submittedAnswer ?? storeEntry?.draft ?? {};
		const nextSelected: Record<number, string[]> = {};
		const nextOther: Record<number, string> = {};
		const nextOtherOn: Record<number, boolean> = {};
		questions.forEach((q, i) => {
			const entry = (source as any)?.[String(i)] ?? (source as any)?.[i] ?? {};
			const sel = Array.isArray(entry?.selected)
				? entry.selected.filter((s: any) => typeof s === 'string')
				: [];
			nextSelected[i] = sel;
			nextOther[i] = typeof entry?.other === 'string' ? entry.other : '';
			nextOtherOn[i] = !!(nextOther[i] && nextOther[i].length) || q.options.length === 0;
		});
		selected = nextSelected;
		otherText = nextOther;
		otherOn = nextOtherOn;
		seededFor = toolCallId;
	};

	// --- selection handlers (interactive state only; persistence is debounced) ---
	const toggleOption = (qi: number, label: string) => {
		if (locked || orphaned) return;
		const q = questions[qi];
		if (q.multiSelect) {
			const cur = new Set(selected[qi] ?? []);
			if (cur.has(label)) cur.delete(label);
			else cur.add(label);
			selected = { ...selected, [qi]: [...cur] };
		} else {
			selected = { ...selected, [qi]: [label] };
			otherOn = { ...otherOn, [qi]: false };
		}
		scheduleDraftSave();
	};

	const selectOther = (qi: number) => {
		if (locked || orphaned) return;
		const q = questions[qi];
		if (!q.multiSelect) {
			selected = { ...selected, [qi]: [] };
		}
		otherOn = { ...otherOn, [qi]: true };
		scheduleDraftSave();
	};

	const onOtherInput = (qi: number, value: string) => {
		otherText = { ...otherText, [qi]: value };
		if (value && !otherOn[qi]) otherOn = { ...otherOn, [qi]: true };
		scheduleDraftSave();
	};

	const handleOtherInputEvent = (qi: number, e: Event) => {
		onOtherInput(qi, (e.target as HTMLInputElement).value);
	};

	const isOptionSelected = (qi: number, label: string) => (selected[qi] ?? []).includes(label);

	// A question counts as answered when it has at least one selected option, or
	// an active "Other" with non-empty text, or (free-form) non-empty text.
	const questionAnswered = (qi: number): boolean => {
		const q = questions[qi];
		const sel = selected[qi] ?? [];
		if (sel.length > 0) return true;
		if ((q.allowOther || q.options.length === 0) && otherOn[qi] && (otherText[qi] ?? '').trim())
			return true;
		return false;
	};

	$: allAnswered = questions.length > 0 && questions.every((_, i) => questionAnswered(i));

	// Build the {qIndex: {selected, other}} payload from local state.
	const buildAnswerPayload = (): Record<string, { selected: string[]; other: string }> => {
		const out: Record<string, { selected: string[]; other: string }> = {};
		questions.forEach((q, i) => {
			const sel = selected[i] ?? [];
			const other = otherOn[i] ? (otherText[i] ?? '').trim() : '';
			out[String(i)] = { selected: sel, other };
		});
		return out;
	};

	// --- durable persistence via the set_question_state patch op ---
	const isServerChat = () => {
		const cid = $chatId;
		return typeof cid === 'string' && cid && !cid.startsWith('local:');
	};

	const writeState = async (patch: Record<string, unknown>) => {
		// Optimistically update the store so the card reflects the change instantly
		// and a reload (or another tab's load) sees consistent state.
		questionStates.update((s) => ({
			...s,
			[toolCallId]: { ...(s[toolCallId] ?? {}), ...patch }
		}));
		if (!isServerChat() || !toolCallId) return; // temp/local: socket path handles it
		try {
			await patchChat(localStorage.token, $chatId as string, [
				{ op: 'set_question_state', tool_call_id: toolCallId, patch }
			]);
		} catch (err) {
			console.warn('ask_user: failed to persist question state', err);
		}
	};

	let draftTimer: ReturnType<typeof setTimeout> | null = null;
	const scheduleDraftSave = () => {
		if (locked || orphaned) return;
		if (draftTimer) clearTimeout(draftTimer);
		draftTimer = setTimeout(() => {
			draftTimer = null;
			void writeState({ draft: buildAnswerPayload() });
		}, 400);
	};

	const submit = async () => {
		if (locked || orphaned || submitting) return;
		if (!allAnswered) {
			toast.error($i18n.t('Please answer every question before submitting.'));
			return;
		}
		if (draftTimer) {
			clearTimeout(draftTimer);
			draftTimer = null;
		}
		submitting = true;
		const answer = buildAnswerPayload();
		await writeState({ answer, submitted_at: Math.floor(Date.now() / 1000) });
		submitting = false;
	};

	const skip = async () => {
		if (locked || orphaned || submitting) return;
		if (draftTimer) {
			clearTimeout(draftTimer);
			draftTimer = null;
		}
		submitting = true;
		await writeState({ skipped: true, submitted_at: Math.floor(Date.now() / 1000) });
		submitting = false;
	};

	// For the locked summary view: pull the chosen answer for a question from the
	// submitted store answer (preferred — structured) so reload shows real picks.
	const summaryFor = (qi: number): string => {
		const entry = (submittedAnswer as any)?.[String(qi)] ?? (submittedAnswer as any)?.[qi] ?? {};
		const picks: string[] = [];
		if (Array.isArray(entry?.selected)) picks.push(...entry.selected.filter((s: any) => !!s));
		if (entry?.other && `${entry.other}`.trim()) picks.push(`${$i18n.t('Other')}: ${entry.other}`);
		return picks.length ? picks.join(', ') : $i18n.t('(no answer)');
	};
</script>

<div
	class="my-2 rounded-2xl border border-gray-200 dark:border-gray-800 bg-white/60 dark:bg-gray-900/40 ask-user-block"
	data-tool-call-id={toolCallId}
>
	<div class="flex items-center gap-2 px-3.5 py-2.5 border-b border-gray-100 dark:border-gray-800/70">
		<span class="shrink-0 inline-flex items-center justify-center size-5 text-gray-500 dark:text-gray-400">
			{#if submitting}
				<Spinner className="size-4" />
			{:else}
				<!-- chat-bubble question icon -->
				<svg
					xmlns="http://www.w3.org/2000/svg"
					viewBox="0 0 24 24"
					fill="none"
					stroke="currentColor"
					stroke-width="2"
					stroke-linecap="round"
					stroke-linejoin="round"
					class="size-4"
				>
					<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" />
					<path d="M9.5 9a2.5 2.5 0 1 1 3.6 2.2c-.6.3-1.1.9-1.1 1.6" />
					<path d="M12 16h.01" />
				</svg>
			{/if}
		</span>
		<div class="flex-1 min-w-0">
			<div class="text-sm font-medium text-gray-700 dark:text-gray-200">
				{#if locked}
					{$i18n.t('Your answer')}
				{:else if orphaned}
					{$i18n.t('Question (unanswered)')}
				{:else}
					{$i18n.t('A question for you')}
				{/if}
			</div>
		</div>
	</div>

	{#if locked}
		<!-- Locked, read-only summary of what was chosen (or skipped). -->
		<div class="px-3.5 py-3 space-y-3">
			{#if wasSkipped}
				<div class="text-sm text-gray-500 dark:text-gray-400 italic">
					{$i18n.t('You skipped this question.')}
				</div>
			{:else}
				{#each questions as q, qi}
					<div class="space-y-1">
						<div class="text-sm text-gray-700 dark:text-gray-300">{q.question}</div>
						<div class="text-sm font-medium text-gray-900 dark:text-gray-100">
							{#if submittedAnswer}
								{summaryFor(qi)}
							{:else}
								{$i18n.t('Answered')}
							{/if}
						</div>
					</div>
				{/each}
			{/if}
		</div>
	{:else}
		<!-- Interactive form -->
		<div class="px-3.5 py-3 space-y-4" transition:slide={{ duration: 150 }}>
			{#if orphaned}
				<div
					class="text-xs rounded-lg px-2.5 py-2 bg-amber-50 text-amber-700 dark:bg-amber-950/40 dark:text-amber-400"
				>
					{$i18n.t('Generation stopped before this was answered.')}
				</div>
			{/if}

			{#each questions as q, qi}
				<div class="space-y-2">
					<div class="flex items-baseline gap-2">
						{#if q.header}
							<span
								class="shrink-0 text-[10px] uppercase tracking-wide font-semibold px-1.5 py-0.5 rounded bg-gray-100 text-gray-500 dark:bg-gray-800 dark:text-gray-400"
							>
								{q.header}
							</span>
						{/if}
						<div class="text-sm font-medium text-gray-800 dark:text-gray-100">{q.question}</div>
					</div>

					<div class="space-y-1.5">
						{#each q.options as opt}
							<button
								type="button"
								class="w-full text-left flex items-start gap-2.5 rounded-xl border px-3 py-2 transition-colors
									{isOptionSelected(qi, opt.label)
									? 'border-blue-500 bg-blue-50 dark:border-blue-500 dark:bg-blue-950/30'
									: 'border-gray-200 hover:border-gray-300 dark:border-gray-700 dark:hover:border-gray-600'}
									{orphaned ? 'opacity-60 cursor-not-allowed' : ''}"
								disabled={orphaned}
								on:click|preventDefault={() => toggleOption(qi, opt.label)}
							>
								<!-- selection indicator: circle (single) / square (multi) -->
								<span
									class="shrink-0 mt-0.5 inline-flex items-center justify-center size-4 border-2 {q.multiSelect
										? 'rounded'
										: 'rounded-full'} {isOptionSelected(qi, opt.label)
										? 'border-blue-500 bg-blue-500 text-white'
										: 'border-gray-300 dark:border-gray-600'}"
								>
									{#if isOptionSelected(qi, opt.label)}
										<svg viewBox="0 0 12 12" fill="none" class="size-3">
											<path
												d="M2.5 6.5l2.5 2.5 4.5-5"
												stroke="currentColor"
												stroke-width="2"
												stroke-linecap="round"
												stroke-linejoin="round"
											/>
										</svg>
									{/if}
								</span>
								<span class="min-w-0">
									<span class="block text-sm text-gray-800 dark:text-gray-100">{opt.label}</span>
									{#if opt.description}
										<span class="block text-xs text-gray-500 dark:text-gray-400 mt-0.5"
											>{opt.description}</span
										>
									{/if}
								</span>
							</button>
						{/each}

						{#if q.allowOther || q.options.length === 0}
							<div
								class="rounded-xl border px-3 py-2 transition-colors {otherOn[qi] &&
								q.options.length > 0
									? 'border-blue-500 bg-blue-50 dark:border-blue-500 dark:bg-blue-950/30'
									: 'border-gray-200 dark:border-gray-700'}"
							>
								{#if q.options.length > 0}
									<button
										type="button"
										class="flex items-center gap-2.5 w-full text-left {orphaned
											? 'opacity-60 cursor-not-allowed'
											: ''}"
										disabled={orphaned}
										on:click|preventDefault={() => selectOther(qi)}
									>
										<span
											class="shrink-0 inline-flex items-center justify-center size-4 border-2 {q.multiSelect
												? 'rounded'
												: 'rounded-full'} {otherOn[qi]
												? 'border-blue-500 bg-blue-500 text-white'
												: 'border-gray-300 dark:border-gray-600'}"
										>
											{#if otherOn[qi]}
												<svg viewBox="0 0 12 12" fill="none" class="size-3">
													<path
														d="M2.5 6.5l2.5 2.5 4.5-5"
														stroke="currentColor"
														stroke-width="2"
														stroke-linecap="round"
														stroke-linejoin="round"
													/>
												</svg>
											{/if}
										</span>
										<span class="text-sm text-gray-700 dark:text-gray-200"
											>{$i18n.t('Other')}</span
										>
									</button>
								{/if}
								<input
									type="text"
									class="w-full bg-transparent text-sm outline-none placeholder-gray-400 dark:placeholder-gray-500 text-gray-900 dark:text-gray-100 {q.options
										.length > 0
										? 'mt-1.5'
										: ''}"
									placeholder={q.options.length === 0
										? $i18n.t('Type your answer…')
										: $i18n.t('Type something…')}
									disabled={orphaned}
									value={otherText[qi] ?? ''}
									on:input={(e) => handleOtherInputEvent(qi, e)}
									on:focus={() => selectOther(qi)}
								/>
							</div>
						{/if}
					</div>
				</div>
			{/each}

			<div class="flex items-center justify-end gap-2 pt-1">
				<button
					type="button"
					class="text-xs px-3 py-1.5 rounded-lg text-gray-500 hover:text-gray-700 dark:text-gray-400 dark:hover:text-gray-200 transition-colors disabled:opacity-50"
					disabled={orphaned || submitting}
					on:click|preventDefault={skip}
				>
					{$i18n.t('Skip')}
				</button>
				<button
					type="button"
					class="text-sm font-medium px-4 py-1.5 rounded-lg bg-gray-900 text-white hover:bg-black dark:bg-white dark:text-gray-900 dark:hover:bg-gray-100 transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
					disabled={!allAnswered || orphaned || submitting}
					on:click|preventDefault={submit}
				>
					{$i18n.t('Submit')}
				</button>
			</div>
		</div>
	{/if}
</div>
