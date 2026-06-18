<script lang="ts">
	import { getContext, createEventDispatcher } from 'svelte';
	import { config, settings, mobile } from '$lib/stores';
	import { formatFileSize } from '$lib/utils';

	import Tooltip from '$lib/components/common/Tooltip.svelte';
	import Sparkles from '$lib/components/icons/Sparkles.svelte';
	import Bolt from '$lib/components/icons/Bolt.svelte';

	const i18n = getContext('i18n');
	const dispatch = createEventDispatcher();

	// `fullQuality === true` ⇒ pinned (no compression). Anything else
	// (false/undefined/missing) ⇒ optimized. Mirrors the backend's
	// `f.fullQuality === true` semantics so the UI can never disagree with
	// what actually gets sent.
	export let fullQuality: boolean = false;
	export let size: number | null = null;
	// `compact` is icon-only, for tiny thumbnails (e.g. message-edit, ~56px).
	export let compact: boolean = false;
	// Disable interaction while an image is still uploading.
	export let disabled: boolean = false;

	$: pinned = fullQuality === true;

	// Match the rest of the composer: controls hide until hover unless the user
	// is on mobile / high-contrast (no reliable hover), or the state is the
	// consequential one (pinned), which should always announce itself.
	$: alwaysVisible =
		pinned || $mobile || ($settings?.highContrastMode ?? false);

	$: enabledGlobally = $config?.file?.image_provider_compression?.enabled ?? false;
	$: sizeLabel = typeof size === 'number' && size > 0 ? formatFileSize(size) : '';

	const toggle = () => {
		if (disabled) return;
		dispatch('toggle', { fullQuality: !pinned });
	};
</script>

{#if enabledGlobally}
	<Tooltip
		content={pinned
			? $i18n.t(
					'Sending at full quality — larger images may be rejected by some providers. Click to optimize.'
				)
			: $i18n.t('Optimized for delivery. Click to send at full quality.')}
	>
		<button
			type="button"
			class="flex items-center gap-1 rounded-full shadow-sm transition-colors {compact
				? 'p-1'
				: 'px-1.5 py-1'} {pinned
				? 'bg-amber-400/95 hover:bg-amber-400 text-amber-950'
				: 'bg-white/90 hover:bg-white text-gray-700 dark:bg-gray-800/90 dark:text-gray-200 dark:hover:bg-gray-800'} {alwaysVisible
				? ''
				: 'opacity-0 group-hover:opacity-100'} {disabled ? 'cursor-default' : ''}"
			aria-pressed={pinned}
			aria-label={pinned
				? $i18n.t('Image quality: full. Click to optimize.')
				: $i18n.t('Image quality: optimized. Click to send at full quality.')}
			on:click|stopPropagation={toggle}
		>
			{#if pinned}
				<Bolt className="size-3.5" strokeWidth="2" />
				{#if !compact}
					<span class="text-[11px] font-medium leading-none pr-0.5">
						{$i18n.t('Full quality')}
					</span>
				{/if}
			{:else}
				<Sparkles className="size-3.5" strokeWidth="1.75" />
				{#if !compact && sizeLabel}
					<span class="text-[11px] font-medium leading-none pr-0.5">{sizeLabel}</span>
				{/if}
			{/if}
		</button>
	</Tooltip>
{/if}
