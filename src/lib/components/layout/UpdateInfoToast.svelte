<script lang="ts">
	import { dispatchComponentEvent } from '$lib/utils/componentEvents';
	import { getContext } from 'svelte';

	const dispatch = (type: string, detail?: unknown) =>
		dispatchComponentEvent(eventProps, type, detail);
	const i18n = getContext('i18n');

	import { WEBUI_VERSION } from '$lib/constants';
	import XMark from '../icons/XMark.svelte';

	let {
		version = {
			current: WEBUI_VERSION,
			latest: WEBUI_VERSION
		},
		...eventProps
	} = $props();
</script>

<div
	class="flex items-start bg-manilla/40 dark:bg-manilla-dark border-hairline border-book-cloth/30 dark:border-book-cloth/40 text-gray-800 dark:text-gray-100 rounded-lg px-3.5 py-3 text-xs max-w-80 pr-2 w-full shadow-lg"
>
	<div class="flex-1 font-medium">
		{$i18n.t(`A new version (v{{LATEST_VERSION}}) is now available.`, {
			LATEST_VERSION: version.latest
		})}

		<a
			href="https://github.com/open-webui/open-webui/releases"
			target="_blank"
			class="underline text-book-cloth hover:text-kraft"
		>
			{$i18n.t('Update for the latest features and improvements.')}</a
		>
	</div>

	<div class=" shrink-0 pr-1">
		<button
			class=" text-gray-400 hover:text-gray-700 dark:hover:text-gray-200 transition"
			onclick={() => {
				dispatch('close');
			}}
		>
			<XMark />
		</button>
	</div>
</div>
