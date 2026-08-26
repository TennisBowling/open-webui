<script lang="ts">
	import { getContext } from 'svelte';
	const i18n = getContext('i18n');

	import StatusItem from './StatusHistory/StatusItem.svelte';
	interface Props {
		statusHistory?: any;
		expand?: boolean;
	}

	let { statusHistory = [], expand = false }: Props = $props();

	let showHistory = $state(true);

	$effect(() => {
		if (expand) {
			showHistory = true;
		} else {
			showHistory = false;
		}
	});

	let history = $state([]);
	let status = $state(null);
	const EXPANDED_STATUS_LIMIT = 50;

	$effect(() => {
		history = Array.isArray(statusHistory) ? statusHistory : [];
	});
	$effect(() => {
		status = history.length > 0 ? history.at(-1) : null;
	});
	let expandedHistory = $derived(
		showHistory
			? history.slice(
					Math.max(0, history.length - EXPANDED_STATUS_LIMIT),
					Math.max(0, history.length - 1)
				)
			: []
	);
</script>

{#if history && history.length > 0}
	{#if status?.hidden !== true}
		<div class="text-sm flex flex-col w-full">
			{#if showHistory}
				<div class="flex flex-row">
					{#if history.length > 1}
						<div class="w-full">
							{#each expandedHistory as status}
								{#if status}
									<div class="flex items-stretch gap-2 mb-1">
										<div class="flex flex-col items-center px-1">
											<div class="pt-3 mb-1.5">
												<span
													class="relative flex size-1.5 rounded-full justify-center items-center"
												>
													<span
														class="relative inline-flex size-1.5 rounded-full bg-gray-500 dark:bg-gray-300"
													></span>
												</span>
											</div>

											<div class="w-[0.5px] h-[calc(100%-14px)] bg-gray-300 dark:bg-gray-700"></div>
										</div>

										<StatusItem {status} done={true} />
									</div>
								{/if}
							{/each}
						</div>
					{/if}
				</div>
			{/if}

			<button
				class="w-full"
				onclick={() => {
					showHistory = !showHistory;
				}}
			>
				<div class="flex items-start gap-2">
					{#if history.length > 1}
						<div class="pt-3 px-1">
							<span class="relative flex size-1.5 rounded-full justify-center items-center">
								{#if status?.done === false}
									<span
										class="absolute inline-flex h-full w-full animate-ping rounded-full bg-gray-500 dark:bg-gray-300 opacity-75"
									></span>
								{/if}
								<span
									class="relative inline-flex size-1.5 rounded-full bg-gray-500 dark:bg-gray-300"
								></span>
							</span>
						</div>
					{/if}
					<StatusItem {status} />
				</div>
			</button>
		</div>
	{/if}
{/if}
