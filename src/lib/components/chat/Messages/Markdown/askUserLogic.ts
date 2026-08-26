// Pure decision logic for the ask_user question card (AskUserBlock.svelte).
//
// These helpers are deliberately kept free of any Svelte / DOM dependency so
// they can be unit-tested directly AND — just as importantly — so the component
// can call them while still referencing the reactive state variables at the
// call site. Svelte 4 only tracks variables that appear *textually* in a
// reactive statement (`$:`) or a markup expression; it does NOT look inside a
// helper's body. The original component hid `selected` / `otherOn` / `otherText`
// inside `questionAnswered()` / `isOptionSelected()`, so neither the Submit
// gate nor the option highlight ever repainted when the user interacted. The
// fix is to pass that state in as explicit arguments (see AskUserBlock.svelte),
// which makes the dependency visible to the compiler.

export type Option = { label: string; description?: string };

export type Question = {
	question: string;
	header?: string;
	options: Option[];
	multiSelect: boolean;
	allowOther: boolean;
};

// The persisted shape for one question's answer/draft.
export type AnswerEntry = { selected: string[]; other: string };

// Local form state, keyed by question index.
export type SelectedMap = Record<number, string[]>;
export type OtherTextMap = Record<number, string>;
export type OtherOnMap = Record<number, boolean>;

/**
 * Normalize the raw `{questions: [...]}` (or bare array) argument the model
 * produced into a clean, fully-typed `Question[]`. Tolerant of strings, missing
 * optional fields, string-or-object options, and double-encoded arrays — the
 * backend `_coerce_questions` is equally permissive, so the two must agree.
 */
export const normalizeQuestions = (raw: any): Question[] => {
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
					if (typeof o === 'string') {
						const label = o.trim();
						return label ? { label } : null;
					}
					if (o && typeof o === 'object' && `${o.label ?? ''}`.trim())
						return { label: `${o.label}`.trim(), description: `${o.description ?? ''}`.trim() };
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

/**
 * True when a single question has a usable answer given the current local
 * state: at least one selected option, OR an active "Other" / free-form box
 * with non-whitespace text.
 */
export const isQuestionAnswered = (
	q: Question,
	selected: string[] | undefined,
	otherOn: boolean,
	otherText: string | undefined
): boolean => {
	const sel = Array.isArray(selected) ? selected : [];
	if (sel.length > 0) return true;
	if ((q.allowOther || q.options.length === 0) && otherOn && (otherText ?? '').trim().length > 0)
		return true;
	return false;
};

/**
 * The Submit gate: every question must be answered, and there must be at least
 * one question. Callers MUST pass `selected` / `otherOn` / `otherText` so the
 * reactive statement that invokes this tracks them.
 */
export const computeAllAnswered = (
	questions: Question[],
	selected: SelectedMap,
	otherOn: OtherOnMap,
	otherText: OtherTextMap
): boolean =>
	questions.length > 0 &&
	questions.every((q, i) => isQuestionAnswered(q, selected[i], !!otherOn[i], otherText[i]));

/** Whether a given option label is currently selected for question `qi`. */
export const optionSelected = (selected: SelectedMap, qi: number, label: string): boolean =>
	(selected[qi] ?? []).includes(label);

/**
 * Build the `{ "<qIndex>": { selected, other } }` payload from local state.
 * `other` is only included when the "Other"/free-form box is active, and is
 * trimmed.
 */
export const buildAnswerPayload = (
	questions: Question[],
	selected: SelectedMap,
	otherOn: OtherOnMap,
	otherText: OtherTextMap
): Record<string, AnswerEntry> => {
	const out: Record<string, AnswerEntry> = {};
	questions.forEach((q, i) => {
		const sel = Array.isArray(selected[i]) ? selected[i] : [];
		const other = otherOn[i] ? (otherText[i] ?? '').trim() : '';
		out[String(i)] = { selected: sel, other };
	});
	return out;
};

/**
 * Derive local form state from a durable source (a submitted answer or a saved
 * draft, both shaped `{ "<qIndex>": { selected, other } }`). Used to restore
 * partial selections after a reload. Pure — returns fresh maps.
 */
export const seedStateFromSource = (
	questions: Question[],
	source: any
): { selected: SelectedMap; otherText: OtherTextMap; otherOn: OtherOnMap } => {
	const selected: SelectedMap = {};
	const otherText: OtherTextMap = {};
	const otherOn: OtherOnMap = {};
	const src = source && typeof source === 'object' ? source : {};
	questions.forEach((q, i) => {
		const entry = src[String(i)] ?? src[i] ?? {};
		const sel = Array.isArray(entry?.selected)
			? entry.selected.filter((s: any) => typeof s === 'string')
			: [];
		selected[i] = sel;
		const other = typeof entry?.other === 'string' ? entry.other : '';
		otherText[i] = other;
		// Light up the "Other"/free-form row if there's restored text, or there
		// are no options at all (a pure free-form question is always text-mode).
		otherOn[i] = other.length > 0 || q.options.length === 0;
	});
	return { selected, otherText, otherOn };
};

/**
 * Render the locked, read-only summary line for question `qi` from a submitted
 * answer. `otherLabel` / `noAnswerLabel` are passed in so i18n stays in the
 * component.
 */
export const summarizeAnswer = (
	submittedAnswer: any,
	qi: number,
	otherLabel: string,
	noAnswerLabel: string
): string => {
	const src = submittedAnswer && typeof submittedAnswer === 'object' ? submittedAnswer : {};
	const entry = src[String(qi)] ?? src[qi] ?? {};
	const picks: string[] = [];
	if (Array.isArray(entry?.selected))
		picks.push(...entry.selected.filter((s: any) => `${s ?? ''}`.trim()).map((s: any) => `${s}`));
	if (entry?.other && `${entry.other}`.trim()) picks.push(`${otherLabel}: ${entry.other}`);
	return picks.length ? picks.join(', ') : noAnswerLabel;
};
