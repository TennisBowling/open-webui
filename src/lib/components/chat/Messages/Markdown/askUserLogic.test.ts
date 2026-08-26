import { describe, expect, it } from 'vitest';

import {
	normalizeQuestions,
	isQuestionAnswered,
	computeAllAnswered,
	optionSelected,
	buildAnswerPayload,
	seedStateFromSource,
	summarizeAnswer,
	type Question
} from './askUserLogic';

// A small builder so the tests read close to the real question shape.
const q = (over: Partial<Question> = {}): Question => ({
	question: 'Pick one?',
	header: '',
	options: [{ label: 'A' }, { label: 'B' }],
	multiSelect: false,
	allowOther: true,
	...over
});

describe('normalizeQuestions', () => {
	it('returns [] for junk input', () => {
		expect(normalizeQuestions(null)).toEqual([]);
		expect(normalizeQuestions(undefined)).toEqual([]);
		expect(normalizeQuestions(42)).toEqual([]);
		expect(normalizeQuestions({})).toEqual([]);
		expect(normalizeQuestions({ questions: 'not json' })).toEqual([]);
	});

	it('reads the {questions:[...]} wrapper and a bare array alike', () => {
		const wrapped = normalizeQuestions({ questions: [{ question: 'Hi?' }] });
		const bare = normalizeQuestions([{ question: 'Hi?' }]);
		expect(wrapped).toHaveLength(1);
		expect(bare).toHaveLength(1);
		expect(wrapped[0].question).toBe('Hi?');
	});

	it('parses a stringified questions array', () => {
		const out = normalizeQuestions({ questions: JSON.stringify([{ question: 'Hi?' }]) });
		expect(out).toHaveLength(1);
		expect(out[0].question).toBe('Hi?');
	});

	it('drops questions with no text and trims', () => {
		const out = normalizeQuestions([{ question: '   ' }, { question: '  Real?  ' }, {}, null]);
		expect(out).toHaveLength(1);
		expect(out[0].question).toBe('Real?');
	});

	it('accepts string options and {label,description} options, dropping blanks', () => {
		const out = normalizeQuestions([
			{
				question: 'Q?',
				options: ['A', { label: ' B ', description: ' desc ' }, { label: '   ' }, 7, '']
			}
		]);
		expect(out[0].options).toEqual([{ label: 'A' }, { label: 'B', description: 'desc' }]);
	});

	it('defaults allowOther true, but forces it true when there are no options', () => {
		expect(normalizeQuestions([{ question: 'Q?' }])[0].allowOther).toBe(true);
		// explicit false is respected only when options exist
		expect(
			normalizeQuestions([{ question: 'Q?', allowOther: false, options: ['A'] }])[0].allowOther
		).toBe(false);
		// free-form: allowOther false is overridden to true (always needs the box)
		expect(
			normalizeQuestions([{ question: 'Q?', allowOther: false, options: [] }])[0].allowOther
		).toBe(true);
	});

	it('coerces multiSelect to a real boolean', () => {
		expect(normalizeQuestions([{ question: 'Q?', multiSelect: 1 }])[0].multiSelect).toBe(true);
		expect(normalizeQuestions([{ question: 'Q?' }])[0].multiSelect).toBe(false);
	});
});

describe('isQuestionAnswered', () => {
	it('is answered with a selected option', () => {
		expect(isQuestionAnswered(q(), ['A'], false, '')).toBe(true);
	});

	it('is unanswered when nothing is picked', () => {
		expect(isQuestionAnswered(q(), [], false, '')).toBe(false);
		expect(isQuestionAnswered(q(), undefined, false, undefined)).toBe(false);
	});

	it('counts an active Other only when it has non-whitespace text', () => {
		expect(isQuestionAnswered(q(), [], true, 'custom')).toBe(true);
		expect(isQuestionAnswered(q(), [], true, '   ')).toBe(false);
		// text present but Other not active → not answered
		expect(isQuestionAnswered(q(), [], false, 'custom')).toBe(false);
	});

	it('ignores Other text when allowOther is false and options exist', () => {
		const noOther = q({ allowOther: false });
		expect(isQuestionAnswered(noOther, [], true, 'custom')).toBe(false);
		expect(isQuestionAnswered(noOther, ['A'], false, '')).toBe(true);
	});

	it('treats a free-form question (no options) as answered with text', () => {
		const free = q({ options: [], allowOther: true });
		expect(isQuestionAnswered(free, [], true, 'typed')).toBe(true);
		expect(isQuestionAnswered(free, [], true, '')).toBe(false);
	});
});

describe('computeAllAnswered', () => {
	it('is false when there are no questions', () => {
		expect(computeAllAnswered([], {}, {}, {})).toBe(false);
	});

	// This is the exact bug the user reported: every question answered via the
	// "Other" box (custom text), Submit must enable.
	it('enables Submit when every question is answered via Other text', () => {
		const questions = [
			q({ question: 'Q0?' }),
			q({ question: 'Q1?' }),
			q({ question: 'Q2?', options: [] })
		];
		const selected = { 0: [], 1: [], 2: [] };
		const otherOn = { 0: true, 1: true, 2: true };
		const otherText = { 0: 'one', 1: 'two', 2: 'three' };
		expect(computeAllAnswered(questions, selected, otherOn, otherText)).toBe(true);
	});

	it('stays false if any single question is blank', () => {
		const questions = [q({ question: 'Q0?' }), q({ question: 'Q1?' })];
		expect(
			computeAllAnswered(questions, { 0: ['A'], 1: [] }, { 0: false, 1: false }, { 0: '', 1: '' })
		).toBe(false);
	});

	it('handles a mix of selected options and Other text', () => {
		const questions = [q({ question: 'Q0?' }), q({ question: 'Q1?' })];
		expect(
			computeAllAnswered(questions, { 0: ['A'], 1: [] }, { 0: false, 1: true }, { 0: '', 1: 'x' })
		).toBe(true);
	});

	it('multiSelect with two picks counts as answered', () => {
		const questions = [q({ multiSelect: true })];
		expect(computeAllAnswered(questions, { 0: ['A', 'B'] }, { 0: false }, { 0: '' })).toBe(true);
	});
});

describe('optionSelected', () => {
	it('reflects the current selection', () => {
		expect(optionSelected({ 0: ['A'] }, 0, 'A')).toBe(true);
		expect(optionSelected({ 0: ['A'] }, 0, 'B')).toBe(false);
		expect(optionSelected({}, 0, 'A')).toBe(false);
	});
});

describe('buildAnswerPayload', () => {
	it('keys by stringified index and trims Other text', () => {
		const questions = [q({ question: 'Q0?' }), q({ question: 'Q1?' })];
		const payload = buildAnswerPayload(
			questions,
			{ 0: ['A'], 1: [] },
			{ 0: false, 1: true },
			{ 0: 'ignored', 1: '  trimmed  ' }
		);
		expect(payload).toEqual({
			'0': { selected: ['A'], other: '' },
			'1': { selected: [], other: 'trimmed' }
		});
	});

	it('omits Other text when the Other row is not active', () => {
		const payload = buildAnswerPayload([q()], { 0: [] }, { 0: false }, { 0: 'typed-but-off' });
		expect(payload['0']).toEqual({ selected: [], other: '' });
	});
});

describe('seedStateFromSource', () => {
	it('round-trips a saved draft back into local form state', () => {
		const questions = [q({ question: 'Q0?' }), q({ question: 'Q1?' })];
		const source = { '0': { selected: ['A'], other: '' }, '1': { selected: [], other: 'hello' } };
		const seeded = seedStateFromSource(questions, source);
		expect(seeded.selected).toEqual({ 0: ['A'], 1: [] });
		expect(seeded.otherText).toEqual({ 0: '', 1: 'hello' });
		// otherOn lights up where there is restored text
		expect(seeded.otherOn).toEqual({ 0: false, 1: true });
	});

	it('turns Other on for a free-form (no-options) question even with no text', () => {
		const seeded = seedStateFromSource([q({ options: [] })], {});
		expect(seeded.otherOn).toEqual({ 0: true });
	});

	it('is safe against missing / malformed source entries', () => {
		const seeded = seedStateFromSource([q()], { '0': { selected: 'not-an-array', other: 5 } });
		expect(seeded.selected).toEqual({ 0: [] });
		expect(seeded.otherText).toEqual({ 0: '' });
	});

	it('accepts a null source without throwing', () => {
		const seeded = seedStateFromSource([q()], null);
		expect(seeded.selected).toEqual({ 0: [] });
	});
});

describe('summarizeAnswer', () => {
	it('joins selected picks and an Other entry', () => {
		const ans = { '0': { selected: ['A', 'B'], other: 'C' } };
		expect(summarizeAnswer(ans, 0, 'Other', '(no answer)')).toBe('A, B, Other: C');
	});

	it('falls back to the no-answer label when empty', () => {
		expect(summarizeAnswer({ '0': { selected: [], other: '' } }, 0, 'Other', '(none)')).toBe(
			'(none)'
		);
		expect(summarizeAnswer(null, 0, 'Other', '(none)')).toBe('(none)');
	});

	it('drops blank selected entries', () => {
		const ans = { '0': { selected: ['', '  ', 'Real'], other: '' } };
		expect(summarizeAnswer(ans, 0, 'Other', '(none)')).toBe('Real');
	});
});
