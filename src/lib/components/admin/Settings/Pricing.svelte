<script lang="ts">
	import { dispatchComponentEvent } from '$lib/utils/componentEvents';
	import { self } from '$lib/utils/eventModifiers';

	import { getContext, onMount } from 'svelte';
	import type { Writable } from 'svelte/store';
	import { toast } from '$lib/utils/toast';

	import {
		getPricingCatalog,
		getPricingOverrides,
		upsertPricingOverride,
		deletePricingOverride,
		syncPricing,
		formatTokenCount,
		formatCost,
		type PricingCatalogRow,
		type PricingOverrideRow,
		type ResolvedModelStatus
	} from '$lib/apis/analytics';

	const i18n =
		getContext<Writable<{ t: (key: string, options?: Record<string, unknown>) => string }>>('i18n');

	const eventProps: Record<string, unknown> = $props();
	const dispatch = (type: string, detail?: unknown) =>
		dispatchComponentEvent(eventProps, type, detail);

	let loading = $state(true);
	let syncing = $state(false);
	let catalog: PricingCatalogRow[] = $state([]);
	let overrides: PricingOverrideRow[] = [];
	let resolution: ResolvedModelStatus[] = $state([]);
	let syncedAt: number | null = $state(null);

	let search = $state('');

	// Edit modal state
	let editing: ResolvedModelStatus | null = $state(null);
	let editMode: 'alias' | 'manual' | 'zero' = $state('alias');
	let editAliasSlug = $state('');
	let editPromptRate: number | null = $state(null);
	let editCompletionRate: number | null = $state(null);
	let editCacheReadRate: number | null = $state(null);
	let editNote = $state('');
	let catalogSearch = $state('');
	let saving = $state(false);

	onMount(load);

	async function load() {
		loading = true;
		try {
			const token = localStorage.getItem('token');
			if (!token) return;
			const [cat, ovr] = await Promise.all([getPricingCatalog(token), getPricingOverrides(token)]);
			catalog = cat.catalog ?? [];
			syncedAt = cat.synced_at;
			overrides = ovr.overrides ?? [];
			resolution = ovr.resolution ?? [];
		} catch (e) {
			console.error(e);
			toast.error($i18n.t('Failed to load pricing'));
		} finally {
			loading = false;
		}
	}

	async function doSync() {
		syncing = true;
		try {
			const token = localStorage.getItem('token');
			if (!token) return;
			const res = await syncPricing(token);
			if (res?.status === 'ok') {
				toast.success(
					$i18n.t('Synced {{count}} models from OpenRouter', { count: res.synced_count })
				);
				await load();
			} else {
				toast.error($i18n.t('Sync failed') + (res?.error ? `: ${res.error}` : ''));
			}
		} catch (e) {
			toast.error($i18n.t('Sync failed'));
		} finally {
			syncing = false;
		}
	}

	function overrideFor(modelId: string): PricingOverrideRow | undefined {
		return overrides.find((o) => o.model_id === modelId);
	}

	function openEdit(row: ResolvedModelStatus) {
		editing = row;
		const ov = overrideFor(row.model_id);
		if (ov) {
			editMode = (ov.mode as any) ?? 'alias';
			editAliasSlug = ov.alias_slug ?? '';
			editPromptRate = ov.prompt_rate;
			editCompletionRate = ov.completion_rate;
			editCacheReadRate = ov.cache_read_rate;
			editNote = ov.note ?? '';
		} else {
			// Smart default: if the model_id literally matches a catalog slug, prefill alias.
			editMode = 'alias';
			editAliasSlug = catalog.some((c) => c.slug === row.model_id) ? row.model_id : '';
			editPromptRate = null;
			editCompletionRate = null;
			editCacheReadRate = null;
			editNote = '';
		}
		catalogSearch = '';
	}

	function closeEdit() {
		editing = null;
	}

	async function saveEdit() {
		if (!editing) return;
		if (editMode === 'alias' && !editAliasSlug) {
			toast.error($i18n.t('Select a catalog model to alias to'));
			return;
		}
		saving = true;
		try {
			const token = localStorage.getItem('token');
			if (!token) return;
			await upsertPricingOverride(token, {
				model_id: editing.model_id,
				mode: editMode,
				alias_slug: editMode === 'alias' ? editAliasSlug : null,
				prompt_rate: editMode === 'manual' ? editPromptRate : null,
				completion_rate: editMode === 'manual' ? editCompletionRate : null,
				cache_read_rate: editMode === 'manual' ? editCacheReadRate : null,
				note: editNote || null
			});
			toast.success($i18n.t('Mapping saved'));
			closeEdit();
			await load();
			dispatch('save');
		} catch (e: any) {
			toast.error(e?.detail || $i18n.t('Failed to save mapping'));
		} finally {
			saving = false;
		}
	}

	async function clearOverride(modelId: string) {
		try {
			const token = localStorage.getItem('token');
			if (!token) return;
			await deletePricingOverride(token, modelId);
			toast.success($i18n.t('Mapping removed'));
			await load();
			dispatch('save');
		} catch (e) {
			toast.error($i18n.t('Failed to remove mapping'));
		}
	}

	function sourceLabel(row: ResolvedModelStatus): string {
		if (!row.priced) return $i18n.t('Unmapped');
		switch (row.rate_source) {
			case 'override_alias':
				return $i18n.t('Alias');
			case 'override_manual':
				return $i18n.t('Manual');
			case 'override_zero':
				return $i18n.t('Free ($0)');
			case 'catalog':
				return $i18n.t('Catalog');
			default:
				return $i18n.t('Embedded');
		}
	}

	// per-million display helper for the tiny per-token rates
	function perM(rate: number | null | undefined): string {
		if (rate == null) return '—';
		return '$' + (rate * 1_000_000).toLocaleString(undefined, { maximumFractionDigits: 2 });
	}

	let unmapped = $derived(resolution.filter((r) => !r.priced));
	let filteredResolution = $derived(
		resolution
			.filter((r) => {
				const q = search.trim().toLowerCase();
				return !q || r.model_id.toLowerCase().includes(q);
			})
			.sort((a, b) => b.total_tokens - a.total_tokens)
	);
	let filteredCatalog = $derived(
		catalog
			.filter((c) => {
				const q = catalogSearch.trim().toLowerCase();
				return (
					!q || c.slug.toLowerCase().includes(q) || (c.model_name ?? '').toLowerCase().includes(q)
				);
			})
			.slice(0, 60)
	);
</script>

<div class="flex flex-col h-full justify-between text-sm">
	<div class="overflow-y-scroll max-h-[28rem] lg:max-h-full">
		<div class="mb-4 flex items-center justify-between gap-3">
			<div>
				<div class="text-base font-medium">{$i18n.t('Model Pricing')}</div>
				<div class="text-xs text-gray-500">
					{#if syncedAt}
						{$i18n.t('Catalog')}: {catalog.length}
						{$i18n.t('models')} · {$i18n.t('synced')}
						{new Date(syncedAt * 1000).toLocaleString()}
					{:else}
						{$i18n.t('Catalog not synced yet')}
					{/if}
				</div>
			</div>
			<button
				class="rounded-lg bg-gray-100 px-3 py-1.5 text-xs font-medium hover:bg-gray-200 disabled:opacity-50 dark:bg-gray-800 dark:hover:bg-gray-700"
				onclick={doSync}
				disabled={syncing}
			>
				{syncing ? $i18n.t('Syncing…') : $i18n.t('Sync catalog now')}
			</button>
		</div>

		{#if loading}
			<div class="py-10 text-center text-gray-400">{$i18n.t('Loading…')}</div>
		{:else}
			<!-- Unmapped worklist -->
			{#if unmapped.length > 0}
				<div class="mb-5 rounded-xl border-hairline border-warning/25 bg-warning/10 p-4">
					<div class="mb-2 text-sm font-semibold text-warning dark:text-warning-dark">
						{unmapped.length}
						{$i18n.t('models need a price mapping')}
					</div>
					<div class="flex flex-wrap gap-2">
						{#each unmapped.slice(0, 12) as r}
							<button
								class="rounded-lg bg-white px-2.5 py-1 text-xs font-medium text-warning shadow-sm hover:bg-warning/10 dark:bg-gray-900 dark:text-warning-dark"
								onclick={() => openEdit(r)}
							>
								{r.model_id} · {formatTokenCount(r.total_tokens)}
							</button>
						{/each}
					</div>
				</div>
			{/if}

			<input
				bind:value={search}
				placeholder={$i18n.t('Search model id')}
				class="mb-3 w-full rounded-lg border-hairline border-gray-200 bg-gray-50 px-3 py-2 text-sm outline-hidden dark:border-gray-800 dark:bg-gray-850"
			/>

			<div class="overflow-x-auto">
				<table class="w-full text-left text-xs">
					<thead class="text-[11px] uppercase tracking-wide text-gray-500">
						<tr>
							<th class="py-2 pr-2">{$i18n.t('Model')}</th>
							<th class="px-2 py-2 text-right">{$i18n.t('Tokens')}</th>
							<th class="px-2 py-2">{$i18n.t('Source')}</th>
							<th class="px-2 py-2 text-right">{$i18n.t('In / Cache / Out')} ($/M)</th>
							<th class="px-2 py-2"></th>
						</tr>
					</thead>
					<tbody class="divide-y divide-gray-100 dark:divide-gray-800">
						{#each filteredResolution as r}
							<tr class="hover:bg-gray-50 dark:hover:bg-gray-950/50">
								<td class="py-2 pr-2 font-mono">{r.model_id}</td>
								<td class="px-2 py-2 text-right">{formatTokenCount(r.total_tokens)}</td>
								<td class="px-2 py-2">
									{#if r.priced}
										<span
											class="rounded-full bg-success/10 px-2 py-0.5 text-[10px] font-semibold text-success dark:text-success-dark"
										>
											{sourceLabel(r)}
										</span>
									{:else}
										<span
											class="rounded-full bg-warning/10 px-2 py-0.5 text-[10px] font-semibold text-warning dark:text-warning-dark"
										>
											{$i18n.t('Unmapped')}
										</span>
									{/if}
								</td>
								<td class="px-2 py-2 text-right font-mono text-gray-500">
									{#if r.effective_rate}
										{perM(r.effective_rate.prompt)} / {perM(r.effective_rate.cache_read)} / {perM(
											r.effective_rate.completion
										)}
									{:else}
										—
									{/if}
								</td>
								<td class="px-2 py-2 text-right whitespace-nowrap">
									<button
										class="text-book-cloth hover:underline dark:text-kraft"
										onclick={() => openEdit(r)}
									>
										{$i18n.t('Map')}
									</button>
									{#if overrideFor(r.model_id)}
										<button
											class="ml-2 text-gray-400 hover:text-error-brick dark:hover:text-error-brick-dark"
											onclick={() => clearOverride(r.model_id)}
										>
											{$i18n.t('Clear')}
										</button>
									{/if}
								</td>
							</tr>
						{/each}
					</tbody>
				</table>
			</div>
		{/if}
	</div>
</div>

<!-- Edit modal -->
{#if editing}
	<div
		class="fixed inset-0 z-[9999] flex items-center justify-center bg-[#191919]/30 dark:bg-[#0F0F0F]/60 p-4"
		onclick={self(closeEdit)}
	>
		<div class="w-full max-w-md rounded-2xl bg-white p-6 shadow-xl dark:bg-gray-900">
			<div class="mb-1 text-lg font-semibold">{$i18n.t('Map pricing')}</div>
			<div class="mb-4 break-all font-mono text-xs text-gray-500">{editing.model_id}</div>

			<div class="mb-4 flex gap-2">
				<button
					class="flex-1 rounded-lg px-3 py-2 text-xs font-medium {editMode === 'alias'
						? 'bg-book-cloth text-white'
						: 'bg-gray-100 dark:bg-gray-800'}"
					onclick={() => (editMode = 'alias')}>{$i18n.t('Alias to OpenRouter')}</button
				>
				<button
					class="flex-1 rounded-lg px-3 py-2 text-xs font-medium {editMode === 'manual'
						? 'bg-book-cloth text-white'
						: 'bg-gray-100 dark:bg-gray-800'}"
					onclick={() => (editMode = 'manual')}>{$i18n.t('Manual rates')}</button
				>
				<button
					class="flex-1 rounded-lg px-3 py-2 text-xs font-medium {editMode === 'zero'
						? 'bg-book-cloth text-white'
						: 'bg-gray-100 dark:bg-gray-800'}"
					onclick={() => (editMode = 'zero')}>{$i18n.t('Free ($0)')}</button
				>
			</div>

			{#if editMode === 'alias'}
				<input
					bind:value={catalogSearch}
					placeholder={$i18n.t('Search OpenRouter models')}
					class="mb-2 w-full rounded-lg border-hairline border-gray-200 bg-gray-50 px-3 py-2 text-sm outline-hidden dark:border-gray-800 dark:bg-gray-850"
				/>
				<div
					class="max-h-48 overflow-y-auto rounded-lg border-hairline border-gray-200 dark:border-gray-800"
				>
					{#each filteredCatalog as c}
						<button
							class="flex w-full items-center justify-between gap-2 px-3 py-2 text-left text-xs hover:bg-gray-50 dark:hover:bg-gray-800 {editAliasSlug ===
							c.slug
								? 'bg-book-cloth/15 dark:bg-book-cloth/20'
								: ''}"
							onclick={() => (editAliasSlug = c.slug)}
						>
							<span class="truncate font-mono">{c.slug}</span>
							<span class="shrink-0 text-gray-400"
								>{perM(c.prompt_rate)} / {perM(c.completion_rate)} {$i18n.t('per M')}</span
							>
						</button>
					{/each}
				</div>
				{#if editAliasSlug}
					<div class="mt-2 text-xs text-gray-500">
						{$i18n.t('Aliased to')}: <span class="font-mono">{editAliasSlug}</span>
					</div>
				{/if}
			{:else if editMode === 'manual'}
				<div class="space-y-2">
					<label class="block text-xs text-gray-500">
						{$i18n.t('Prompt rate (per token USD)')}
						<input
							type="number"
							step="any"
							bind:value={editPromptRate}
							class="mt-1 w-full rounded-lg border-hairline border-gray-200 bg-white px-3 py-2 dark:border-gray-800 dark:bg-gray-950"
						/>
					</label>
					<label class="block text-xs text-gray-500">
						{$i18n.t('Cache-read rate (per token USD)')}
						<input
							type="number"
							step="any"
							bind:value={editCacheReadRate}
							class="mt-1 w-full rounded-lg border-hairline border-gray-200 bg-white px-3 py-2 dark:border-gray-800 dark:bg-gray-950"
						/>
					</label>
					<label class="block text-xs text-gray-500">
						{$i18n.t('Completion rate (per token USD)')}
						<input
							type="number"
							step="any"
							bind:value={editCompletionRate}
							class="mt-1 w-full rounded-lg border-hairline border-gray-200 bg-white px-3 py-2 dark:border-gray-800 dark:bg-gray-950"
						/>
					</label>
				</div>
			{:else}
				<div class="rounded-lg bg-gray-50 p-3 text-xs text-gray-500 dark:bg-gray-950/60">
					{$i18n.t('This model will be counted as $0 (e.g. a free or self-hosted local model).')}
				</div>
			{/if}

			<input
				bind:value={editNote}
				placeholder={$i18n.t('Note (optional)')}
				class="mt-3 w-full rounded-lg border-hairline border-gray-200 bg-white px-3 py-2 text-xs outline-none dark:border-gray-800 dark:bg-gray-950"
			/>

			<div class="mt-5 flex justify-end gap-2">
				<button
					class="rounded-lg px-4 py-2 text-sm hover:bg-gray-100 dark:hover:bg-gray-800"
					onclick={closeEdit}
				>
					{$i18n.t('Cancel')}
				</button>
				<button
					class="rounded-lg bg-book-cloth hover:bg-kraft px-4 py-2 text-sm font-medium text-white transition-colors duration-200 ease-paper disabled:opacity-50"
					onclick={saveEdit}
					disabled={saving}
				>
					{saving ? $i18n.t('Saving…') : $i18n.t('Save')}
				</button>
			</div>
		</div>
	</div>
{/if}
