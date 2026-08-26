<script lang="ts">
	import { getContext, tick } from 'svelte';
	import Spinner from '$lib/components/common/Spinner.svelte';
	import { captureEditEntryAnchor, placeEditBoxForKeyboard } from '$lib/utils/editScroll';
	const i18n: any = getContext('i18n');

	// One rewind boundary sitting BETWEEN two rendered blocks. `cutIndex` is the
	// index of the first block to DISCARD — i.e. the rewind keeps content_blocks
	// before this point, injects the typed message there, and resumes inline as a
	// new sibling branch. See ContentRenderer / WorkingBlock for placement and

	interface Props {
		// utils/retryLastRequest.ts::getRewindContext for the slice semantics.
		messageId: string;
		cutIndex: number;
		active?: boolean;
		disabled?: boolean;
		// Rendered flush under a WorkingBlock divider (the group's tail cut or the
		// before-next-item cut right after a group): drop the full-width line so
		// two divider rows don't stack — just the small centered pill.
		bare?: boolean;
		// State lives in the parent (only one composer open per message at a time).
		onActivate?: (cutIndex: number) => void;
		onCancel?: () => void;
		// Resolves true once the rewind has actually committed its branch and
		// started generating. While it is pending this composer shows a working
		// state; on false it stays open so the typed redirect is never lost.
		onSubmit?: (cutIndex: number, text: string) => Promise<boolean> | boolean;
	}

	let {
		messageId,
		cutIndex,
		active = false,
		disabled = false,
		bare = false,
		onActivate = () => {},
		onCancel = () => {},
		onSubmit = () => {}
	}: Props = $props();

	let text = $state('');
	let submitting = $state(false);

	const autofocus = (node: HTMLTextAreaElement) => {
		node.focus({ preventScroll: true });
	};

	// Same "edit box" entry behavior as every other in-message editor (top-level
	// message edit, steer edit): anchor the on-screen position across the
	// pill->textarea swap, then top-align in the viewport once a mobile keyboard
	// shows up. Routed through the shared editScroll.ts helpers, edge-triggered
	// off `active` since activation is driven by the parent (only one composer
	// open per message) rather than a local click handler.
	const placementId = () => `${messageId}-rewind-${cutIndex}`;
	let wasActive = $state(false);
	$effect(() => {
		if (active && !wasActive) {
			const restoreAnchor = captureEditEntryAnchor(messageId);
			tick()
				.then(() => tick())
				.then(() => {
					restoreAnchor();
					placeEditBoxForKeyboard(placementId());
				});
		}
		wasActive = active;
	});

	$effect(() => {
		if (!active) {
			// Reset the draft whenever this composer closes so re-opening starts clean.
			text = '';
			submitting = false;
		}
	});

	// Committing a rewind is several round-trips (hydrate reasoning context,
	// PATCH the sibling row, POST the completion) — seconds on a weak mobile
	// link. Hold the composer open and visibly working for that whole window
	// instead of closing it on click: a composer that vanishes with nothing
	// else changing on screen is indistinguishable from a no-op, which is
	// exactly how a failed rewind used to present.
	const submit = async () => {
		if (submitting) return;
		submitting = true;
		try {
			// Empty text is allowed — it degrades to a pure rewind+regenerate from the cut.
			const committed = await onSubmit(cutIndex, text.trim());
			// On success the parent closes this composer, and the `!active` effect
			// above resets `submitting`. On failure we stay open with the draft
			// intact so the user can retry without retyping.
			if (committed === false) submitting = false;
		} catch {
			submitting = false;
		}
	};

	const onKeydown = (e: KeyboardEvent) => {
		if (submitting) return;
		if (e.key === 'Enter' && !e.shiftKey) {
			e.preventDefault();
			submit();
		} else if (e.key === 'Escape') {
			e.preventDefault();
			onCancel();
		}
	};
</script>

{#if active}
	<div class="my-2 flex justify-center" dir="ltr">
		<div
			class="message-edit-box w-full max-w-[80%] rounded-2xl border-hairline border-gray-100 dark:border-gray-800 bg-gray-50 dark:bg-gray-850 p-2.5 shadow-sm"
		>
			<div
				class="flex items-center justify-between mb-1.5 text-[11px] text-gray-500 dark:text-gray-400 px-0.5"
			>
				<div class="flex items-center gap-1">
					<svg
						xmlns="http://www.w3.org/2000/svg"
						viewBox="0 0 16 16"
						fill="currentColor"
						class="size-3"
					>
						<path
							d="M8 3.5a4.5 4.5 0 1 1-4.39 5.5H2.06a6 6 0 1 0 1.4-5.06V2.5a.75.75 0 0 0-1.5 0v3.25c0 .41.34.75.75.75H6a.75.75 0 0 0 0-1.5H4.6A4.49 4.49 0 0 1 8 3.5Z"
						/>
					</svg>
					<span>{$i18n.t('Rewind & insert here')}</span>
				</div>
				<button
					type="button"
					class="p-0.5 max-md:p-2 rounded hover:bg-gray-100 dark:hover:bg-gray-800"
					aria-label={$i18n.t('Cancel')}
					onclick={onCancel}
				>
					<svg
						xmlns="http://www.w3.org/2000/svg"
						viewBox="0 0 20 20"
						fill="currentColor"
						class="size-3.5"
					>
						<path
							d="M6.28 5.22a.75.75 0 0 0-1.06 1.06L8.94 10l-3.72 3.72a.75.75 0 1 0 1.06 1.06L10 11.06l3.72 3.72a.75.75 0 1 0 1.06-1.06L11.06 10l3.72-3.72a.75.75 0 0 0-1.06-1.06L10 8.94 6.28 5.22Z"
						/>
					</svg>
				</button>
			</div>

			<textarea
				id="message-edit-{placementId()}"
				use:autofocus
				bind:value={text}
				onkeydown={onKeydown}
				disabled={submitting}
				rows="2"
				placeholder={$i18n.t(
					'Type a message to redirect from here… (or leave empty to just rewind)'
				)}
				class="w-full resize-none bg-transparent text-sm text-gray-800 dark:text-gray-100 placeholder-gray-400 dark:placeholder-gray-500 outline-none px-1.5 py-1"
			></textarea>

			<div class="flex items-center justify-between mt-1 px-0.5">
				<span class="text-[10px] text-gray-400 dark:text-gray-500">
					{$i18n.t('Enter to send · Esc to cancel')}
				</span>
				<button
					type="button"
					class="inline-flex items-center gap-1.5 text-xs font-medium px-3 py-1 rounded-full bg-book-cloth hover:bg-kraft text-white transition-colors duration-200 ease-paper disabled:opacity-60"
					disabled={submitting}
					onclick={submit}
				>
					{#if submitting}
						<Spinner className="size-3" />
						{$i18n.t('Rewinding…')}
					{:else}
						{text.trim() ? $i18n.t('Send') : $i18n.t('Rewind here')}
					{/if}
				</button>
			</div>
		</div>
	</div>
{:else}
	<div
		class="group/rw relative flex items-center justify-center select-none {bare
			? 'py-0.5 -mt-1.5'
			: 'py-1'}"
		dir="ltr"
	>
		{#if !bare}
			<!-- Persistent faint divider so the cut point is discoverable WITHOUT hover;
			     it brightens and the label expands on hover. -->
			<div
				class="absolute inset-x-0 top-1/2 -translate-y-1/2 h-px bg-gray-100 dark:bg-gray-800/80 group-hover/rw:bg-gray-300 dark:group-hover/rw:bg-gray-600 transition-colors duration-150"
			></div>
		{/if}
		<button
			type="button"
			{disabled}
			onclick={() => onActivate(cutIndex)}
			class="relative z-10 inline-flex items-center text-[10px] font-medium text-gray-400 dark:text-gray-500 bg-white dark:bg-gray-900 border-hairline border-gray-200 dark:border-gray-700 rounded-full px-1.5 py-0.5 shadow-sm hover:text-gray-700 dark:hover:text-gray-200 hover:border-gray-400 dark:hover:border-gray-500 transition disabled:hidden"
			title={$i18n.t('Rewind & insert here')}
		>
			<svg
				xmlns="http://www.w3.org/2000/svg"
				viewBox="0 0 16 16"
				fill="currentColor"
				class="size-3"
			>
				<path
					d="M8 3.5a4.5 4.5 0 1 1-4.39 5.5H2.06a6 6 0 1 0 1.4-5.06V2.5a.75.75 0 0 0-1.5 0v3.25c0 .41.34.75.75.75H6a.75.75 0 0 0 0-1.5H4.6A4.49 4.49 0 0 1 8 3.5Z"
				/>
			</svg>
			<!-- Horizontal-only reveal: a hidden→inline swap put a ~15px text line
			     box next to the 12px icon, so the pill (and the whole row, and the
			     chat above the input) grew a couple px on hover and everything
			     shifted. max-width animation + leading-none keeps the pill's
			     height identical with or without the label. -->
			<span
				class="max-w-0 overflow-hidden whitespace-nowrap leading-none opacity-0 transition-all duration-150 group-hover/rw:ml-1 group-hover/rw:max-w-40 group-hover/rw:opacity-100"
				>{$i18n.t('Rewind & insert here')}</span
			>
		</button>
	</div>
{/if}
