<script lang="ts">
	import { getContext, untrack } from 'svelte';
	import { toast } from '$lib/utils/toast';

	import { models } from '$lib/stores';
	import {
		createNewAutomation,
		updateAutomationById,
		type Automation
	} from '$lib/apis/automations';

	import Modal from '$lib/components/common/Modal.svelte';
	import Textarea from '$lib/components/common/Textarea.svelte';
	import Tooltip from '$lib/components/common/Tooltip.svelte';
	import XMark from '$lib/components/icons/XMark.svelte';
	import AutomationToolSelector from './AutomationToolSelector.svelte';

	const i18n = getContext('i18n');

	interface Props {
		show?: boolean;
		automation?: Automation | null;
		onSave?: () => void;
	}

	let { show = $bindable(false), automation = null, onSave = () => {} }: Props = $props();

	const WEEKDAYS = [
		{ id: 'MO', label: 'Monday' },
		{ id: 'TU', label: 'Tuesday' },
		{ id: 'WE', label: 'Wednesday' },
		{ id: 'TH', label: 'Thursday' },
		{ id: 'FR', label: 'Friday' },
		{ id: 'SA', label: 'Saturday' },
		{ id: 'SU', label: 'Sunday' }
	];
	const LOCAL_TIMEZONE = Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';

	// Intl.supportedValuesOf is the only complete IANA list a browser exposes;
	// where it's missing (older Safari) the user's own zone is still selectable,
	// which is the one that matters.
	const TIMEZONES: string[] = (() => {
		try {
			return (Intl as any).supportedValuesOf('timeZone');
		} catch (e) {
			return [LOCAL_TIMEZONE, 'UTC'];
		}
	})();

	let title = $state('');
	let prompt = $state('');
	let modelId = $state('');
	let timezone = $state(LOCAL_TIMEZONE);
	let selectedToolIds: string[] = $state([]);
	let features: Record<string, boolean> = $state({});

	let mode = $state('daily'); // daily | weekly | monthly | once | advanced
	let time = $state('09:00');
	let weekday = $state('MO');
	let monthday = $state(1);
	let runAt = $state('');
	let rawRrule = $state('FREQ=DAILY;BYHOUR=9;BYMINUTE=0');

	let saving = $state(false);

	const parseRrule = (rrule: string): Record<string, string> =>
		Object.fromEntries(
			rrule
				.split(';')
				.map((chunk) => chunk.split('='))
				.filter((pair) => pair.length === 2)
				.map(([key, value]) => [key.toUpperCase(), value.toUpperCase()])
		);

	const pad = (value: number) => `${value}`.padStart(2, '0');

	const localInputValue = (epoch: number) => {
		// datetime-local wants the wall clock in the automation's zone, not the
		// browser's — an automation can be scheduled for somewhere else.
		const parts = new Intl.DateTimeFormat('en-CA', {
			timeZone: timezone,
			year: 'numeric',
			month: '2-digit',
			day: '2-digit',
			hour: '2-digit',
			minute: '2-digit',
			hour12: false
		}).formatToParts(new Date(epoch * 1000));
		const get = (type: string) => parts.find((part) => part.type === type)?.value ?? '00';
		return `${get('year')}-${get('month')}-${get('day')}T${get('hour')}:${get('minute')}`;
	};

	// Load the given automation back into the builder. Anything the presets can't
	// express round-trips through the Advanced field rather than being silently
	// rewritten into something simpler.
	const init = () => {
		saving = false;
		title = automation?.title ?? '';
		prompt = automation?.prompt ?? '';
		modelId = automation?.model_id ?? $models?.[0]?.id ?? '';
		timezone = automation?.timezone ?? LOCAL_TIMEZONE;
		selectedToolIds = [...(automation?.tool_ids ?? [])];
		features = { ...(automation?.features ?? {}) };

		if (!automation) {
			mode = 'daily';
			time = '09:00';
			weekday = 'MO';
			monthday = 1;
			runAt = '';
			rawRrule = 'FREQ=DAILY;BYHOUR=9;BYMINUTE=0';
			return;
		}

		if (!automation.rrule) {
			mode = 'once';
			runAt = localInputValue(automation.dtstart);
			return;
		}

		rawRrule = automation.rrule;
		const parts = parseRrule(automation.rrule);
		time = `${pad(parseInt(parts.BYHOUR ?? '9', 10))}:${pad(parseInt(parts.BYMINUTE ?? '0', 10))}`;
		const simple = !parts.INTERVAL && !parts.COUNT && !parts.UNTIL && parts.BYHOUR;

		if (simple && parts.FREQ === 'DAILY' && !parts.BYDAY && !parts.BYMONTHDAY) {
			mode = 'daily';
		} else if (simple && parts.FREQ === 'WEEKLY' && parts.BYDAY?.split(',').length === 1) {
			mode = 'weekly';
			weekday = parts.BYDAY;
		} else if (simple && parts.FREQ === 'MONTHLY' && parts.BYMONTHDAY?.split(',').length === 1) {
			mode = 'monthly';
			monthday = parseInt(parts.BYMONTHDAY, 10);
		} else {
			mode = 'advanced';
		}
	};

	const scheduleFields = () => {
		const [hour, minute] = time.split(':').map((value) => parseInt(value, 10));
		if (mode === 'once') {
			return { run_at: runAt };
		}
		if (mode === 'advanced') {
			return { schedule: rawRrule };
		}
		if (mode === 'weekly') {
			return { schedule: `FREQ=WEEKLY;BYDAY=${weekday};BYHOUR=${hour};BYMINUTE=${minute}` };
		}
		if (mode === 'monthly') {
			return {
				schedule: `FREQ=MONTHLY;BYMONTHDAY=${monthday};BYHOUR=${hour};BYMINUTE=${minute}`
			};
		}
		return { schedule: `FREQ=DAILY;BYHOUR=${hour};BYMINUTE=${minute}` };
	};

	const submitHandler = async () => {
		saving = true;
		const payload = {
			title,
			prompt,
			model_id: modelId,
			timezone,
			tool_ids: selectedToolIds,
			features,
			...scheduleFields()
		};

		const saved = await (
			automation
				? updateAutomationById(localStorage.token, automation.id, payload)
				: createNewAutomation(localStorage.token, payload)
		).catch((error) => {
			// The backend's schedule errors are written to be shown as-is.
			toast.error(`${error}`);
			return null;
		});
		saving = false;

		if (saved) {
			toast.success(automation ? $i18n.t('Automation updated') : $i18n.t('Automation created'));
			show = false;
			onSave();
		}
	};

	// Tracks `show` ONLY: init() reads props and the models store, and letting
	// those re-trigger it would reset the form under the user mid-edit.
	$effect(() => {
		if (show) {
			untrack(() => init());
		}
	});
</script>

<Modal size="md" bind:show>
	<div>
		<div class="flex justify-between dark:text-gray-100 px-5 pt-4 pb-1">
			<div class="text-lg font-medium self-center">
				{automation ? $i18n.t('Edit Automation') : $i18n.t('New Automation')}
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

		<div class="flex flex-col md:flex-row w-full px-5 pb-4 md:space-x-4 dark:text-gray-200">
			<form
				class="flex flex-col w-full gap-3"
				onsubmit={(e) => {
					e.preventDefault();
					submitHandler();
				}}
			>
				<div>
					<div class=" text-xs font-medium text-gray-600 dark:text-gray-400 mb-1">
						{$i18n.t('Title')}
					</div>
					<input
						class="w-full text-sm bg-transparent outline-hidden border-hairline border-gray-100 dark:border-gray-850 rounded-lg px-3 py-2"
						bind:value={title}
						placeholder={$i18n.t('Morning news digest')}
						required
					/>
				</div>

				<div>
					<div class=" text-xs font-medium text-gray-600 dark:text-gray-400 mb-1">
						{$i18n.t('Prompt')}
					</div>
					<Textarea
						className="w-full text-sm bg-transparent outline-hidden border-hairline border-gray-100 dark:border-gray-850 rounded-lg px-3 py-2 resize-vertical"
						bind:value={prompt}
						rows="4"
						placeholder={$i18n.t('Search the web for today’s AI news and write a short digest.')}
					/>
					<div class=" text-xs text-gray-500 dark:text-gray-500 mt-1">
						{$i18n.t(
							'Runs verbatim in a fresh chat with no memory of anything else. Write it as a standalone instruction — leave the timing to the schedule below.'
						)}
					</div>
				</div>

				<div>
					<div class=" text-xs font-medium text-gray-600 dark:text-gray-400 mb-1">
						{$i18n.t('Schedule')}
					</div>
					<div class="flex flex-wrap gap-2 items-center">
						<select
							class="text-sm bg-transparent outline-hidden border-hairline border-gray-100 dark:border-gray-850 rounded-lg px-2 py-1.5"
							bind:value={mode}
						>
							<option value="daily">{$i18n.t('Daily')}</option>
							<option value="weekly">{$i18n.t('Weekly')}</option>
							<option value="monthly">{$i18n.t('Monthly')}</option>
							<option value="once">{$i18n.t('Once')}</option>
							<option value="advanced">{$i18n.t('Advanced')}</option>
						</select>

						{#if mode === 'weekly'}
							<select
								class="text-sm bg-transparent outline-hidden border-hairline border-gray-100 dark:border-gray-850 rounded-lg px-2 py-1.5"
								bind:value={weekday}
							>
								{#each WEEKDAYS as day (day.id)}
									<option value={day.id}>{$i18n.t(day.label)}</option>
								{/each}
							</select>
						{/if}

						{#if mode === 'monthly'}
							<input
								class="w-20 text-sm bg-transparent outline-hidden border-hairline border-gray-100 dark:border-gray-850 rounded-lg px-2 py-1.5"
								type="number"
								min="1"
								max="28"
								bind:value={monthday}
							/>
						{/if}

						{#if mode === 'once'}
							<input
								class="text-sm bg-transparent outline-hidden border-hairline border-gray-100 dark:border-gray-850 rounded-lg px-2 py-1.5"
								type="datetime-local"
								bind:value={runAt}
								required
							/>
						{:else if mode === 'advanced'}
							<Tooltip content={$i18n.t('An iCal RRULE body, e.g. FREQ=DAILY;BYHOUR=8;BYMINUTE=0')}>
								<input
									class="w-72 text-sm bg-transparent outline-hidden border-hairline border-gray-100 dark:border-gray-850 rounded-lg px-2 py-1.5"
									bind:value={rawRrule}
									placeholder="FREQ=DAILY;BYHOUR=8;BYMINUTE=0"
									required
								/>
							</Tooltip>
						{:else}
							<input
								class="text-sm bg-transparent outline-hidden border-hairline border-gray-100 dark:border-gray-850 rounded-lg px-2 py-1.5"
								type="time"
								bind:value={time}
								required
							/>
						{/if}
					</div>
					<div class=" text-xs text-gray-500 dark:text-gray-500 mt-1">
						{$i18n.t('Automations can run at most once per hour.')}
					</div>
				</div>

				<div class="flex flex-wrap gap-3">
					<div class="flex-1 min-w-40">
						<div class=" text-xs font-medium text-gray-600 dark:text-gray-400 mb-1">
							{$i18n.t('Timezone')}
						</div>
						<select
							class="w-full text-sm bg-transparent outline-hidden border-hairline border-gray-100 dark:border-gray-850 rounded-lg px-2 py-1.5"
							bind:value={timezone}
						>
							{#each TIMEZONES as zone (zone)}
								<option value={zone}>{zone}</option>
							{/each}
						</select>
					</div>

					<div class="flex-1 min-w-40">
						<div class=" text-xs font-medium text-gray-600 dark:text-gray-400 mb-1">
							{$i18n.t('Model')}
						</div>
						<select
							class="w-full text-sm bg-transparent outline-hidden border-hairline border-gray-100 dark:border-gray-850 rounded-lg px-2 py-1.5"
							bind:value={modelId}
							required
						>
							{#each $models as model (model.id)}
								<option value={model.id}>{model.name}</option>
							{/each}
						</select>
					</div>
				</div>

				<AutomationToolSelector bind:selectedToolIds bind:features />

				<div class="flex justify-end pt-1">
					<button
						class="px-3.5 py-1.5 text-sm font-medium bg-book-cloth hover:bg-kraft text-white transition-colors duration-200 ease-paper rounded-full disabled:opacity-50"
						type="submit"
						disabled={saving}
					>
						{$i18n.t('Save')}
					</button>
				</div>
			</form>
		</div>
	</div>
</Modal>
