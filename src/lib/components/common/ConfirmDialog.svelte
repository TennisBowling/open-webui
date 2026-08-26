<script lang="ts">
	import { dispatchComponentEvent } from '$lib/utils/componentEvents';
	import DOMPurify from 'dompurify';

	import { getContext, onDestroy, tick } from 'svelte';
	import * as FocusTrap from 'focus-trap';

	const i18n = getContext('i18n');
	const dispatch = (type: string, detail?: unknown) =>
		dispatchComponentEvent(eventProps, type, detail);

	import { fade } from 'svelte/transition';
	import { flyAndScale } from '$lib/utils/transitions';
	import { marked } from 'marked';

	interface Props {
		title?: string;
		message?: string;
		cancelLabel?: any;
		confirmLabel?: any;
		onConfirm?: any;
		input?: boolean;
		inputPlaceholder?: string;
		inputValue?: string;
		show?: boolean;
		children?: import('svelte').Snippet;
	}

	let {
		title = '',
		message = '',
		cancelLabel = $i18n.t('Cancel'),
		confirmLabel = $i18n.t('Confirm'),
		onConfirm = () => {},
		input = false,
		inputPlaceholder = '',
		inputValue = $bindable(''),
		show = $bindable(false),
		children,
		...eventProps
	}: Props & Record<string, unknown> = $props();

	let modalElement = $state(null);

	let focusTrap: FocusTrap.FocusTrap | null = $state(null);

	const init = () => {
		inputValue = '';
	};

	const handleKeyDown = (event: KeyboardEvent) => {
		if (event.key === 'Escape') {
			console.log('Escape');
			show = false;
		}

		if (event.key === 'Enter') {
			console.log('Enter');
			confirmHandler();
		}
	};

	const confirmHandler = async () => {
		show = false;
		await tick();
		await onConfirm();
		dispatch('confirm', inputValue);
	};

	onDestroy(() => {
		show = false;
		if (focusTrap) {
			focusTrap.deactivate();
		}
		if (modalElement?.parentNode === document.body) {
			document.body.removeChild(modalElement);
		}
		if (!document.querySelector('[data-modal-root]')) {
			document.body.style.overflow = '';
		}
	});
	$effect(() => {
		if (show) {
			init();
		}
	});
	$effect(() => {
		if (!show || !modalElement) return;

		const element = modalElement;
		document.body.appendChild(element);
		// preventScroll: focus-trap returns focus to the element that was focused
		// before the dialog opened (e.g. the message's Delete button) when it
		// deactivates. Without preventScroll that .focus() scroll-into-views the
		// trigger — which, for message-delete, jumps the viewport BEFORE
		// deleteMessage captures its scroll anchor, so the careful anchor restore
		// then preserves the wrong (jumped) spot. The same applies on activate.
		const trap = FocusTrap.createFocusTrap(element, { preventScroll: true });
		focusTrap = trap;
		trap.activate();

		window.addEventListener('keydown', handleKeyDown);
		document.body.style.overflow = 'hidden';

		return () => {
			trap.deactivate();
			if (focusTrap === trap) {
				focusTrap = null;
			}
			window.removeEventListener('keydown', handleKeyDown);
			const hasAnotherOpenModal = [...document.querySelectorAll('[data-modal-root]')].some(
				(node) => node !== element
			);
			document.body.style.overflow = hasAnotherOpenModal ? 'hidden' : '';
		};
	});
</script>

{#if show}
	<!-- svelte-ignore a11y_click_events_have_key_events -->
	<!-- svelte-ignore a11y_no_static_element_interactions -->
	<div
		bind:this={modalElement}
		data-modal-root
		aria-modal="true"
		role="dialog"
		tabindex="-1"
		class=" fixed top-0 right-0 left-0 bottom-0 bg-[#191919]/30 dark:bg-[#0F0F0F]/60 w-full h-screen max-h-[100dvh] flex justify-center z-99999999 overflow-hidden overscroll-contain"
		in:fade={{ duration: 10 }}
		onmousedown={() => {
			show = false;
		}}
	>
		<div
			class=" m-auto max-w-full w-[32rem] mx-2 bg-white dark:bg-gray-850 rounded-2xl max-h-[100dvh] shadow-md border-hairline border-gray-200 dark:border-gray-700 flex flex-col overflow-hidden"
			in:flyAndScale
			onmousedown={(e) => {
				e.stopPropagation();
			}}
		>
			<div class="px-7 py-6 flex flex-col min-h-0">
				<div class=" text-lg font-medium dark:text-gray-200 mb-2.5">
					{#if title !== ''}
						{title}
					{:else}
						{$i18n.t('Confirm your action')}
					{/if}
				</div>

				<div class="flex-1 min-h-0 overflow-y-auto">
					{#if children}{@render children()}{:else}
						<div class=" text-sm text-gray-500 flex-1">
							{#if message !== ''}
								{@const html = DOMPurify.sanitize(marked.parse(message))}
								{@html html}
							{:else}
								{$i18n.t('This action cannot be undone. Do you wish to continue?')}
							{/if}

							{#if input}
								<textarea
									bind:value={inputValue}
									placeholder={inputPlaceholder ? inputPlaceholder : $i18n.t('Enter your message')}
									class="w-full mt-2 rounded-lg px-3.5 py-2 text-sm border-hairline border-gray-300 dark:border-gray-700 bg-white dark:bg-gray-900 dark:text-gray-300 outline-hidden resize-none focus-visible:ring-2 focus-visible:ring-book-cloth/40 focus-visible:border-book-cloth"
									rows="3"
									required></textarea>
							{/if}
						</div>
					{/if}
				</div>

				<div class="mt-6 flex justify-between gap-2 shrink-0">
					<button
						class="text-sm bg-gray-100 hover:bg-gray-200 text-gray-800 dark:bg-gray-800 dark:hover:bg-gray-700 dark:text-white font-medium w-full py-2 rounded-full transition-colors duration-200 ease-paper"
						onclick={() => {
							show = false;
							dispatch('cancel');
						}}
						type="button"
					>
						{cancelLabel}
					</button>
					<button
						class="text-sm bg-book-cloth hover:bg-kraft text-white font-medium w-full py-2 rounded-full transition-colors duration-200 ease-paper"
						onclick={() => {
							confirmHandler();
						}}
						type="button"
					>
						{confirmLabel}
					</button>
				</div>
			</div>
		</div>
	</div>
{/if}

<style>
	.modal-content {
		animation: scaleUp 0.1s ease-out forwards;
	}

	@keyframes scaleUp {
		from {
			transform: scale(0.985);
			opacity: 0;
		}
		to {
			transform: scale(1);
			opacity: 1;
		}
	}
</style>
