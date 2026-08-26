<script lang="ts">
	import DOMPurify from 'dompurify';

	import { onDestroy, onMount } from 'svelte';
	import { mobile } from '$lib/stores';

	import tippy from 'tippy.js';

	// Tippy.js touch prop: true|false|'hold'|['hold', delayMs].

	interface Props {
		elementId?: string;
		as?: string;
		className?: string;
		placement?: string;
		content?: any;
		// eslint-disable-next-line @typescript-eslint/no-explicit-any
		touch?: any;
		theme?: string;
		offset?: any;
		allowHTML?: boolean;
		tippyOptions?: any;
		interactive?: boolean;
		clickToStick?: boolean;
		onClick?: (event?: MouseEvent) => void;
		children?: import('svelte').Snippet;
		tooltip?: import('svelte').Snippet;
	}

	let {
		elementId = '',
		as = 'div',
		className = 'flex',
		placement = 'top',
		content = `I'm a tooltip!`,
		touch = true,
		theme = '',
		offset = [0, 4],
		allowHTML = true,
		tippyOptions = {},
		interactive = false,
		clickToStick = false,
		onClick = () => {},
		children,
		tooltip
	}: Props = $props();

	let tooltipElement = $state();
	let tooltipInstance = $state();
	let isSticky = $state(false);

	function handleClick(event) {
		if (clickToStick && tooltipInstance) {
			if (isSticky) {
				isSticky = false;
				tooltipInstance.hide();
			} else {
				isSticky = true;
				tooltipInstance.show();
			}
		}
		onClick(event);
	}

	function handleDocumentClick(event) {
		if (!isSticky || !tooltipInstance) return;
		if (tooltipElement && tooltipElement.contains(event.target)) return;
		if (tooltipInstance.popper && tooltipInstance.popper.contains(event.target)) return;
		isSticky = false;
		tooltipInstance.hide();
	}

	// On mobile, tooltips intercept taps and need a second tap to dismiss —
	// disable them by default. Callers that explicitly want long-press tooltips
	// can pass touch="hold". EXCEPTION: `clickToStick` tooltips are an explicit
	// tap-to-open / tap-to-close interaction (e.g. the per-message usage/eye
	// popover), not a hover hint — keep touch enabled or tippy's manual .show()
	// is a no-op on touch devices and the tap does nothing.
	let effectiveTouch = $derived($mobile && touch !== 'hold' && !clickToStick ? false : touch);

	$effect(() => {
		if (tooltipInstance) {
			tooltipInstance.setProps({ touch: effectiveTouch });
		}
	});

	$effect(() => {
		if (tooltipElement && (content || elementId)) {
			let tooltipContent = null;

			if (elementId) {
				tooltipContent = document.getElementById(`${elementId}`);
			} else {
				tooltipContent = DOMPurify.sanitize(content);
			}

			if (tooltipInstance) {
				tooltipInstance.setContent(tooltipContent);
			} else {
				if (content) {
					tooltipInstance = tippy(tooltipElement, {
						content: tooltipContent,
						placement: placement,
						allowHTML: allowHTML,
						touch: effectiveTouch,
						...(theme !== '' ? { theme } : { theme: 'claude' }),
						arrow: false,
						offset: offset,
						delay: [400, 100],
						...(interactive ? { interactive: true } : {}),
						...(clickToStick
							? {
									interactive: true,
									hideOnClick: false,
									onHide: () => {
										if (isSticky) return false;
									}
								}
							: {}),
						...tippyOptions
					});
				}
			}
		} else if (tooltipInstance && content === '') {
			if (tooltipInstance) {
				tooltipInstance.destroy();
			}
		}
	});

	onMount(() => {
		if (clickToStick) {
			document.addEventListener('click', handleDocumentClick);
		}
	});

	onDestroy(() => {
		if (clickToStick) {
			document.removeEventListener('click', handleDocumentClick);
		}
		if (tooltipInstance) {
			tooltipInstance.destroy();
		}
	});
</script>

<!-- svelte-ignore a11y_no_static_element_interactions -->
<svelte:element this={as} bind:this={tooltipElement} class={className} onclick={handleClick}>
	{@render children?.()}
</svelte:element>

{@render tooltip?.()}
