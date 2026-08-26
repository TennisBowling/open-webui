<script lang="ts">
	import { preventDefault } from '$lib/utils/eventModifiers';

	import { getContext, onDestroy } from 'svelte';
	import { slide } from 'svelte/transition';
	import { toast } from '$lib/utils/toast';

	import { chatId, questionStates } from '$lib/stores';
	import { patchChat } from '$lib/apis/chats';
	import Spinner from '$lib/components/common/Spinner.svelte';
	import {
		normalizeQuestions,
		computeAllAnswered,
		optionSelected,
		buildAnswerPayload as buildPayload,
		seedStateFromSource,
		summarizeAnswer,
		type SelectedMap,
		type OtherTextMap,
		type OtherOnMap
	} from './askUserLogic';

	const i18n: any = getContext('i18n');

	// `attributes` is parsed by Collapsible.svelte from the ask_user tool call.
	// Keys we read:
	//   tool_call_id      — primary key into questionStates + the durable answer write
	//   arguments         — JSON of {questions: [...]}, the question definitions
	//   result            — the tool result string (present once answered/skipped)
	//   done              — "true" once the parent tool result lands (card locks)
	//   message_terminated — "true" if the generation ended (Stop) — orphan guard

	interface Props {
		//   chat_id           — the chat we write the answer into
		attributes?: Record<string, any>;
	}

	let { attributes = {} }: Props = $props();

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

	// --- local form state, keyed by question index ---
	// selected[i] = array of chosen labels; otherText[i] = free-text; otherOn[i] =
	// whether the "Other" / free-form row is the active choice.
	let selected: SelectedMap = $state({});
	let otherText: OtherTextMap = $state({});
	let otherOn: OtherOnMap = $state({});
	let submitting = $state(false);
	// Set the moment the user interacts; gates the auto-seed below so we never
	// clobber in-progress input with (re-)hydrated durable state.
	let userTouched = $state(false);
	let seedKey = $state('');

	const applySeed = (source: any) => {
		const seeded = seedStateFromSource(questions, source ?? {});
		selected = seeded.selected;
		otherText = seeded.otherText;
		otherOn = seeded.otherOn;
	};

	// --- selection handlers (interactive state only; persistence is debounced) ---
	const toggleOption = (qi: number, label: string) => {
		if (locked || orphaned) return;
		userTouched = true;
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
		userTouched = true;
		const q = questions[qi];
		if (!q.multiSelect) {
			selected = { ...selected, [qi]: [] };
		}
		otherOn = { ...otherOn, [qi]: true };
		scheduleDraftSave();
	};

	const onOtherInput = (qi: number, value: string) => {
		if (locked || orphaned) return;
		userTouched = true;
		otherText = { ...otherText, [qi]: value };
		if (value && !otherOn[qi]) otherOn = { ...otherOn, [qi]: true };
		scheduleDraftSave();
	};

	const handleOtherInputEvent = (qi: number, e: Event) => {
		onOtherInput(qi, (e.target as HTMLInputElement).value);
	};

	// Build the {qIndex: {selected, other}} payload from local state.
	const buildAnswerPayload = () => buildPayload(questions, selected, otherOn, otherText);

	const isServerChat = (cid: string) =>
		typeof cid === 'string' && !!cid && !cid.startsWith('local:');

	const applyOptimistic = (patch: Record<string, unknown>) => {
		// Reflect the change in the local store so the card updates instantly and a
		// reload / another tab sees consistent state.
		questionStates.update((s) => ({
			...s,
			[toolCallId]: { ...(s[toolCallId] ?? {}), ...patch }
		}));
	};

	const persist = async (patch: Record<string, unknown>) => {
		await patchChat(localStorage.token, cardChatId, [
			{ op: 'set_question_state', tool_call_id: toolCallId, patch }
		]);
	};

	// Draft writes are non-critical: optimistic + fire-and-forget. Losing a draft
	// on a transient failure is harmless (it's re-derived from local state). We do
	// keep a handle on the in-flight draft so a terminal submit can wait for it.
	let draftInFlight: Promise<unknown> | null = null;
	const writeDraft = (patch: Record<string, unknown>) => {
		applyOptimistic(patch);
		if (!isServerChat(cardChatId) || !toolCallId) return; // temp/local: socket path handles it
		const p = persist(patch).catch((err) => console.warn('ask_user: failed to persist draft', err));
		draftInFlight = p;
		p.finally(() => {
			if (draftInFlight === p) draftInFlight = null;
		});
	};

	// Terminal writes (answer / skip) MUST reach the durable blob before we lock
	// the card — that blob is the only thing the blocked server-side generation
	// polls. Locking optimistically before the write would, on a failed write,
	// hide the form, drop the answer, and hang the generation until timeout. So:
	// persist first; only on success apply the optimistic lock. Throws on failure
	// (caller keeps the card interactive and surfaces an error).
	const writeTerminal = async (patch: Record<string, unknown>) => {
		if (isServerChat(cardChatId) && toolCallId) {
			// A debounced draft write may still be in flight. Let it settle FIRST so
			// its (older) read-modify-write of the shared question_states blob can't
			// land AFTER ours and clobber the answer with a stale snapshot.
			if (draftInFlight) {
				try {
					await draftInFlight;
				} catch {
					/* a failed draft is non-fatal; still persist the answer */
				}
			}
			await persist(patch);
		}
		applyOptimistic(patch);
	};

	let draftTimer: ReturnType<typeof setTimeout> | null = null;
	const scheduleDraftSave = () => {
		if (locked || orphaned) return;
		if (draftTimer) clearTimeout(draftTimer);
		draftTimer = setTimeout(() => {
			draftTimer = null;
			// Re-check at fire time: the card may have locked (answered in another
			// tab / result landed) or been orphaned during the debounce window.
			if (locked || orphaned) return;
			writeDraft({ draft: buildAnswerPayload() });
		}, 400);
	};

	// Pending debounced draft must not outlive the card; otherwise it fires after
	// unmount and writes into whatever chat is active by then.
	onDestroy(() => {
		if (draftTimer) {
			clearTimeout(draftTimer);
			draftTimer = null;
		}
	});

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
		try {
			await writeTerminal({
				answer: buildAnswerPayload(),
				submitted_at: Math.floor(Date.now() / 1000)
			});
		} catch (err) {
			console.warn('ask_user: failed to submit answer', err);
			toast.error($i18n.t('Could not submit your answer. Please try again.'));
		} finally {
			submitting = false;
		}
	};

	const skip = async () => {
		if (locked || orphaned || submitting) return;
		if (draftTimer) {
			clearTimeout(draftTimer);
			draftTimer = null;
		}
		submitting = true;
		try {
			await writeTerminal({ skipped: true, submitted_at: Math.floor(Date.now() / 1000) });
		} catch (err) {
			console.warn('ask_user: failed to skip question', err);
			toast.error($i18n.t('Could not skip the question. Please try again.'));
		} finally {
			submitting = false;
		}
	};
	let toolCallId = $derived((attributes?.tool_call_id || '') as string);
	let questions = $derived(normalizeQuestions(parseArgs(attributes?.arguments)));
	let answeredByResult = $derived(String(attributes?.done) === 'true');
	let messageTerminated = $derived(String(attributes?.message_terminated) === 'true');
	// The durable per-question state for this tool call (draft + submitted answer).
	let storeEntry = $derived($questionStates?.[toolCallId]);
	let submittedAnswer = $derived(
		storeEntry?.answer && typeof storeEntry.answer === 'object' ? storeEntry.answer : null
	);
	let wasSkipped = $derived(!!storeEntry?.skipped);
	// Locked = the question is resolved. Either the tool result landed (answer is
	// in the message now), or the durable store records a submit/skip.
	let locked = $derived(answeredByResult || submittedAnswer != null || wasSkipped);
	// Orphaned = generation ended without an answer (e.g. user pressed Stop while
	// the card was waiting). Disable the form; it can no longer resume anything.
	let orphaned = $derived(!locked && messageTerminated);
	// The durable source to restore from (a submitted answer wins over a draft).
	let seedSource = $derived(submittedAnswer ?? storeEntry?.draft ?? null);
	// Reset interaction state when the card identity changes (e.g. switching
	// chats) so the next card seeds fresh from its own durable state.
	$effect(() => {
		if (toolCallId !== seedKey) {
			seedKey = toolCallId;
			userTouched = false;
		}
	});
	// (Re)seed local form state from the durable source until the user touches
	// the form. Re-running (rather than seeding exactly once) restores partial
	// selections after a reload even if the questionStates store hydrates a tick
	// AFTER the card first mounts — the one-shot seed used to miss that and show
	// an empty form. `userTouched` guarantees we stop before clobbering input.
	$effect(() => {
		if (toolCallId && questions.length && !userTouched) {
			applySeed(seedSource);
		}
	});
	// The Submit gate. Passing the state maps as explicit arguments is what makes
	// Svelte track them: a bare `questions.every(i => questionAnswered(i))` would
	// only re-run when `questions` changed, never when the user filled an answer.
	let allAnswered = $derived(computeAllAnswered(questions, selected, otherOn, otherText));
	// --- durable persistence via the set_question_state patch op ---
	// Always target the card's OWN chat (captured from the tool-call attributes),
	// never the global $chatId store: a debounced write can fire after the user
	// has navigated to a different chat, and $chatId would by then point at the
	// wrong blob. Falls back to $chatId only if the attribute is somehow absent.
	let cardChatId = $derived((attributes?.chat_id || $chatId || '') as string);
</script>

<div
	class="my-2 rounded-2xl border-hairline border-gray-200 dark:border-gray-800 bg-white/60 dark:bg-gray-900/40 ask-user-block"
	data-tool-call-id={toolCallId}
>
	<div
		class="flex items-center gap-2 px-3.5 py-2.5 border-b-hairline border-gray-100 dark:border-gray-800/70"
	>
		<span
			class="shrink-0 inline-flex items-center justify-center size-5 text-gray-500 dark:text-gray-400"
		>
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
					{@const summaryLine = summarizeAnswer(
						submittedAnswer,
						qi,
						$i18n.t('Other'),
						$i18n.t('(no answer)')
					)}
					<div class="space-y-1">
						<div class="text-sm text-gray-700 dark:text-gray-300">{q.question}</div>
						<div class="text-sm font-medium text-gray-900 dark:text-gray-100">
							{#if submittedAnswer}
								{summaryLine}
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
					class="text-xs rounded-lg px-2.5 py-2 bg-warning/10 text-warning dark:text-warning-dark"
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
							{@const isSel = optionSelected(selected, qi, opt.label)}
							<button
								type="button"
								class="w-full text-left flex items-start gap-2.5 rounded-xl border px-3 py-2 transition-colors
									{isSel
									? 'border-book-cloth bg-book-cloth/10 dark:border-book-cloth dark:bg-book-cloth/15'
									: 'border-gray-200 hover:border-gray-300 dark:border-gray-700 dark:hover:border-gray-600'}
									{orphaned ? 'opacity-60 cursor-not-allowed' : ''}"
								disabled={orphaned}
								onclick={preventDefault(() => toggleOption(qi, opt.label))}
							>
								<!-- selection indicator: circle (single) / square (multi) -->
								<span
									class="shrink-0 mt-0.5 inline-flex items-center justify-center size-4 border-2 {q.multiSelect
										? 'rounded'
										: 'rounded-full'} {isSel
										? 'border-book-cloth bg-book-cloth text-white'
										: 'border-gray-300 dark:border-gray-600'}"
								>
									{#if isSel}
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
									? 'border-book-cloth bg-book-cloth/10 dark:border-book-cloth dark:bg-book-cloth/15'
									: 'border-gray-200 dark:border-gray-700'}"
							>
								{#if q.options.length > 0}
									<button
										type="button"
										class="flex items-center gap-2.5 w-full text-left {orphaned
											? 'opacity-60 cursor-not-allowed'
											: ''}"
										disabled={orphaned}
										onclick={preventDefault(() => selectOther(qi))}
									>
										<span
											class="shrink-0 inline-flex items-center justify-center size-4 border-2 {q.multiSelect
												? 'rounded'
												: 'rounded-full'} {otherOn[qi]
												? 'border-book-cloth bg-book-cloth text-white'
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
										<span class="text-sm text-gray-700 dark:text-gray-200">{$i18n.t('Other')}</span>
									</button>
								{/if}
								<input
									type="text"
									class="w-full bg-transparent text-sm outline-none placeholder-gray-400 dark:placeholder-gray-500 text-gray-900 dark:text-gray-100 {q
										.options.length > 0
										? 'mt-1.5'
										: ''}"
									placeholder={q.options.length === 0
										? $i18n.t('Type your answer…')
										: $i18n.t('Type something…')}
									disabled={orphaned}
									value={otherText[qi] ?? ''}
									oninput={(e) => handleOtherInputEvent(qi, e)}
									onfocus={() => selectOther(qi)}
								/>
							</div>
						{/if}
					</div>
				</div>
			{/each}

			<div class="flex items-center justify-end gap-2 pt-1">
				<button
					type="button"
					class="text-xs px-3 py-1.5 max-md:py-2.5 rounded-lg text-gray-500 hover:text-gray-700 dark:text-gray-400 dark:hover:text-gray-200 transition-colors disabled:opacity-50"
					disabled={orphaned || submitting}
					onclick={preventDefault(skip)}
				>
					{$i18n.t('Skip')}
				</button>
				<button
					type="button"
					class="text-sm font-medium px-4 py-1.5 max-md:py-2.5 rounded-lg bg-book-cloth hover:bg-kraft text-white transition-colors duration-200 ease-paper disabled:opacity-40 disabled:cursor-not-allowed"
					disabled={!allAnswered || orphaned || submitting}
					onclick={preventDefault(submit)}
				>
					{$i18n.t('Submit')}
				</button>
			</div>
		</div>
	{/if}
</div>
