<script lang="ts">
	import { getContext, untrack } from 'svelte';
	import { goto } from '$app/navigation';

	import { automationsUnreadCount } from '$lib/stores';
	import {
		getAutomationRuns,
		markRunsRead,
		type Automation,
		type AutomationRun
	} from '$lib/apis/automations';

	import Modal from '$lib/components/common/Modal.svelte';
	import Spinner from '$lib/components/common/Spinner.svelte';
	import XMark from '$lib/components/icons/XMark.svelte';

	const i18n = getContext('i18n');

	interface Props {
		show?: boolean;
		automation?: Automation | null;
	}

	let { show = $bindable(false), automation = null }: Props = $props();

	let runs: AutomationRun[] | null = $state(null);

	const STATUS_COLORS: Record<string, string> = {
		completed: 'bg-green-500',
		running: 'bg-blue-500',
		missed: 'bg-gray-400',
		timeout: 'bg-warning',
		error: 'bg-red-500'
	};

	const formatTime = (epoch: number) =>
		new Intl.DateTimeFormat(undefined, {
			dateStyle: 'medium',
			timeStyle: 'short',
			timeZone: automation?.timezone
		}).format(new Date(epoch * 1000));

	const init = async () => {
		runs = null;
		if (!automation) {
			return;
		}
		runs = await getAutomationRuns(localStorage.token, automation.id).catch(() => []);

		// Opening the history IS reading it — clear the badge here rather than
		// making the user find a separate "mark read" affordance.
		await markRunsRead(localStorage.token).catch(() => null);
		automationsUnreadCount.set(0);
	};

	$effect(() => {
		if (show) {
			untrack(() => init());
		}
	});
</script>

<Modal size="sm" bind:show>
	<div>
		<div class="flex justify-between dark:text-gray-100 px-5 pt-4 pb-1">
			<div class="text-lg font-medium self-center">
				{$i18n.t('Run history')}
			</div>
			<button
				class="self-center"
				onclick={() => {
					show = false;
				}}
				type="button"
				aria-label={$i18n.t('Close')}
			>
				<XMark className="size-4" />
			</button>
		</div>

		<div class="px-5 pb-5 dark:text-gray-200">
			{#if runs === null}
				<div class="flex justify-center py-10">
					<Spinner className="size-5" />
				</div>
			{:else if runs.length === 0}
				<div class=" text-sm text-gray-500 dark:text-gray-500 py-8 text-center">
					{$i18n.t('This automation hasn’t run yet.')}
				</div>
			{:else}
				<div class="flex flex-col">
					{#each runs as run (run.id)}
						<button
							class="flex gap-3 items-start text-left w-full px-3 py-2.5 rounded-xl hover:bg-gray-50 dark:hover:bg-gray-850/50 transition disabled:cursor-default"
							type="button"
							disabled={!run.chat_id}
							onclick={() => {
								if (run.chat_id) {
									show = false;
									goto(`/c/${run.chat_id}`);
								}
							}}
						>
							<div
								class="size-2 rounded-full mt-1.5 shrink-0 {STATUS_COLORS[run.status] ??
									'bg-gray-400'}"
							></div>
							<div class="flex-1 min-w-0">
								<div class="text-sm">{formatTime(run.started_at)}</div>
								<div class=" text-xs text-gray-500 dark:text-gray-500 line-clamp-2">
									{run.error || run.preview || $i18n.t(run.status)}
								</div>
							</div>
						</button>
					{/each}
				</div>
			{/if}
		</div>
	</div>
</Modal>
