<script lang="ts">
	import { toast } from '$lib/utils/toast';

	import { onMount, getContext } from 'svelte';

	import { WEBUI_NAME } from '$lib/stores';
	import {
		deleteAutomationById,
		getAutomations,
		runAutomationById,
		toggleAutomationById,
		type Automation
	} from '$lib/apis/automations';

	import ConfirmDialog from '$lib/components/common/ConfirmDialog.svelte';
	import EllipsisHorizontal from '$lib/components/icons/EllipsisHorizontal.svelte';
	import Plus from '$lib/components/icons/Plus.svelte';
	import Search from '$lib/components/icons/Search.svelte';
	import Spinner from '$lib/components/common/Spinner.svelte';
	import Tooltip from '$lib/components/common/Tooltip.svelte';
	import XMark from '$lib/components/icons/XMark.svelte';
	import AutomationMenu from './AutomationMenu.svelte';
	import AutomationModal from './AutomationModal.svelte';
	import RunsModal from './RunsModal.svelte';

	const i18n = getContext('i18n');

	let loaded = $state(false);
	let query = $state('');

	let automations: Automation[] = $state([]);
	let selectedAutomation: Automation | null = $state(null);

	let showEditModal = $state(false);
	let showRunsModal = $state(false);
	let showDeleteConfirm = $state(false);

	const STATUS_COLORS: Record<string, string> = {
		completed: 'bg-green-500',
		running: 'bg-blue-500',
		missed: 'bg-gray-400',
		timeout: 'bg-warning',
		error: 'bg-red-500'
	};

	let filteredItems = $derived(
		automations.filter((automation) => {
			if (query === '') return true;
			const lowerQuery = query.toLowerCase();
			return (
				automation.title.toLowerCase().includes(lowerQuery) ||
				automation.prompt.toLowerCase().includes(lowerQuery)
			);
		})
	);

	// Rendered in the automation's OWN timezone, not the browser's — an
	// automation scheduled for 08:00 in Chicago should read 08:00 wherever the
	// user happens to be looking at it from.
	const formatNextRun = (automation: Automation) =>
		automation.next_run_at
			? new Intl.DateTimeFormat(undefined, {
					dateStyle: 'medium',
					timeStyle: 'short',
					timeZone: automation.timezone
				}).format(new Date(automation.next_run_at * 1000))
			: '';

	const init = async () => {
		automations = (await getAutomations(localStorage.token).catch((error) => {
			toast.error(`${error}`);
			return [];
		})) as Automation[];
	};

	const toggleHandler = async (automation: Automation) => {
		const updated = await toggleAutomationById(localStorage.token, automation.id).catch((error) => {
			toast.error(`${error}`);
			return null;
		});
		if (updated) {
			await init();
		}
	};

	const runHandler = async (automation: Automation) => {
		const res = await runAutomationById(localStorage.token, automation.id).catch((error) => {
			toast.error(`${error}`);
			return null;
		});
		if (res) {
			toast.success($i18n.t('Running “{{title}}” now', { title: automation.title }));
		}
	};

	const deleteHandler = async (automation: Automation) => {
		const res = await deleteAutomationById(localStorage.token, automation.id).catch((error) => {
			toast.error(`${error}`);
			return null;
		});
		if (res) {
			toast.success($i18n.t('Automation deleted'));
			await init();
		}
	};

	onMount(async () => {
		await init();
		loaded = true;
	});
</script>

<svelte:head>
	<title>
		{$i18n.t('Automations')} • {$WEBUI_NAME}
	</title>
</svelte:head>

<AutomationModal
	bind:show={showEditModal}
	automation={selectedAutomation}
	onSave={() => {
		init();
	}}
/>

<RunsModal bind:show={showRunsModal} automation={selectedAutomation} />

<ConfirmDialog
	bind:show={showDeleteConfirm}
	title={$i18n.t('Delete automation?')}
	onconfirm={() => {
		if (selectedAutomation) {
			deleteHandler(selectedAutomation);
		}
	}}
>
	<div class=" text-sm text-gray-500 truncate">
		{$i18n.t('This will delete')}
		<span class="  font-semibold">{selectedAutomation?.title}</span>.
	</div>
</ConfirmDialog>

{#if loaded}
	<div class="flex flex-col gap-1 px-1 mt-1.5 mb-3">
		<div class="flex justify-between items-center">
			<div class="flex items-center md:self-center text-xl font-medium px-0.5 gap-2 shrink-0">
				<div>
					{$i18n.t('Automations')}
				</div>

				<div class="text-lg font-medium text-gray-500 dark:text-gray-500">
					{filteredItems.length}
				</div>
			</div>

			<div class="flex w-full justify-end gap-1.5">
				<button
					class=" px-2 py-1.5 max-md:p-2.5 max-md:min-w-9 max-md:min-h-9 max-md:justify-center rounded-full bg-book-cloth hover:bg-kraft text-white transition-colors duration-200 ease-paper font-medium text-sm flex items-center"
					type="button"
					onclick={() => {
						selectedAutomation = null;
						showEditModal = true;
					}}
				>
					<Plus className="size-3" strokeWidth="2.5" />

					<div class=" hidden md:block md:ml-1 text-xs">{$i18n.t('New Automation')}</div>
				</button>
			</div>
		</div>
	</div>

	<div
		class="py-2 bg-white dark:bg-gray-900 rounded-2xl border-hairline border-gray-100 dark:border-gray-850"
	>
		<div class=" flex w-full space-x-2 py-0.5 px-3.5 pb-2">
			<div class="flex flex-1">
				<div class=" self-center ml-1 mr-3">
					<Search className="size-3.5" />
				</div>
				<input
					class=" w-full text-sm pr-4 py-1 rounded-r-xl outline-hidden bg-transparent"
					bind:value={query}
					placeholder={$i18n.t('Search Automations')}
				/>
				{#if query}
					<div class="self-center pl-1.5 bg-transparent">
						<button
							class="p-0.5 max-md:p-2 rounded-full text-gray-500 hover:text-gray-700 hover:bg-gray-100 dark:text-gray-400 dark:hover:text-gray-200 dark:hover:bg-gray-850 transition"
							onclick={() => {
								query = '';
							}}
						>
							<XMark className="size-3 max-md:size-4" strokeWidth="2" />
						</button>
					</div>
				{/if}
			</div>
		</div>

		{#if filteredItems.length !== 0}
			<div class=" my-2 gap-2 grid px-3 lg:grid-cols-2">
				{#each filteredItems as automation (automation.id)}
					<div
						class=" flex space-x-4 text-left w-full px-3 py-2.5 dark:hover:bg-gray-850/50 hover:bg-gray-50 transition rounded-2xl"
					>
						<button
							class=" flex flex-1 space-x-3.5 cursor-pointer w-full text-left min-w-0"
							type="button"
							onclick={() => {
								selectedAutomation = automation;
								showEditModal = true;
							}}
						>
							<div class="flex items-center text-left min-w-0">
								<div class=" flex-1 self-center min-w-0">
									<div class="flex items-center gap-2">
										{#if automation.last_run_status}
											<Tooltip content={$i18n.t(automation.last_run_status)}>
												<div
													class="size-2 rounded-full shrink-0 {STATUS_COLORS[
														automation.last_run_status
													] ?? 'bg-gray-400'}"
												></div>
											</Tooltip>
										{/if}
										<div class="line-clamp-1 text-sm">
											{automation.title}
										</div>
										{#if !automation.active}
											<span
												class="px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide rounded-full bg-gray-100 dark:bg-gray-850 text-gray-500 dark:text-gray-400 shrink-0"
											>
												{$i18n.t('Paused')}
											</span>
										{/if}
									</div>

									<div class="px-0.5">
										<div class="text-xs text-gray-500 line-clamp-1">
											{automation.schedule_text}{automation.next_run_at
												? ` · ${$i18n.t('next')} ${formatNextRun(automation)}`
												: ''}
										</div>
									</div>
								</div>
							</div>
						</button>
						<div class="flex flex-row gap-0.5 max-md:gap-1 self-center">
							<AutomationMenu
								active={automation.active}
								editHandler={() => {
									selectedAutomation = automation;
									showEditModal = true;
								}}
								toggleHandler={() => {
									toggleHandler(automation);
								}}
								runHandler={() => {
									runHandler(automation);
								}}
								runsHandler={() => {
									selectedAutomation = automation;
									showRunsModal = true;
								}}
								deleteHandler={() => {
									selectedAutomation = automation;
									showDeleteConfirm = true;
								}}
								onClose={() => {}}
							>
								<button
									class="self-center w-fit text-sm p-1.5 max-md:p-2 text-gray-500 hover:text-gray-700 dark:text-gray-400 dark:hover:text-gray-200 hover:bg-black/5 dark:hover:bg-white/5 rounded-lg transition"
									type="button"
								>
									<EllipsisHorizontal className="size-5" />
								</button>
							</AutomationMenu>
						</div>
					</div>
				{/each}
			</div>
		{:else}
			<div class=" w-full h-full flex flex-col justify-center items-center my-16 mb-24">
				<div class="max-w-md text-center">
					<div class=" text-3xl mb-3">🕗</div>
					<div class=" text-lg font-medium mb-1">{$i18n.t('No automations yet')}</div>
					<div class=" text-gray-500 text-center text-xs">
						{$i18n.t(
							'Schedule a prompt to run on its own — or just ask the model to set one up for you.'
						)}
					</div>
				</div>
			</div>
		{/if}
	</div>
{:else}
	<div class="w-full h-full flex justify-center items-center">
		<Spinner className="size-5" />
	</div>
{/if}
