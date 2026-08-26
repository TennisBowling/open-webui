<script lang="ts">
	import { stopPropagation } from '$lib/utils/eventModifiers';

	import { getContext } from 'svelte';
	import { getModelEndpoints } from '$lib/apis/openai';
	import Switch from '$lib/components/common/Switch.svelte';

	const i18n = getContext('i18n');

	interface Props {
		baseModelId?: string;
		providerOnly?: string[];
		providerOrder?: string[];
	}

	let {
		baseModelId = '',
		providerOnly = $bindable([]),
		providerOrder = $bindable([])
	}: Props = $props();

	let endpoints: any[] = $state([]);
	let loading = $state(false);
	let enabled = $state(false);

	$effect(() => {
		enabled = providerOnly.length > 0;
	});

	const fetchEndpoints = async (modelId: string) => {
		if (!modelId) {
			endpoints = [];
			return;
		}
		loading = true;
		try {
			const res = await getModelEndpoints(localStorage.token, modelId);
			endpoints = res?.data?.endpoints ?? [];
		} catch (e) {
			console.error('Failed to fetch endpoints:', e);
			endpoints = [];
		} finally {
			loading = false;
		}
	};

	$effect(() => {
		fetchEndpoints(baseModelId);
	});

	// OpenRouter now lists service tiers (flex/priority) as separate endpoints,
	// distinguished only by a tag suffix (e.g. "google-vertex/global/flex").
	// The picker operates on base (tier-less) tags: tiers are selected per-request
	// via service_tier, and base slugs in provider.only/order still match the
	// tier endpoint of the chosen tier (verified against the live API).
	const SERVICE_TIERS = ['flex', 'priority'];

	const tierOf = (tag: string): string | null => {
		const segs = tag.split('/');
		const last = segs[segs.length - 1];
		return segs.length > 1 && SERVICE_TIERS.includes(last) ? last : null;
	};

	const baseTagOf = (tag: string): string => {
		const tier = tierOf(tag);
		return tier ? tag.slice(0, -(tier.length + 1)) : tag;
	};

	// Variant/region segments after the provider slug (e.g. "global", "int4").
	const variantOf = (baseTag: string): string => {
		const segs = baseTag.split('/');
		return segs.length > 1 ? segs.slice(1).join('/') : '';
	};

	type EndpointGroup = {
		baseTag: string;
		main: any; // default-tier endpoint (or cheapest tier endpoint if none)
		tiers: Record<string, any>;
		tierOnly: boolean; // no default-tier endpoint exists for this provider
	};

	let groups: EndpointGroup[] = $state([]);

	const groupEndpoints = (eps: any[]): EndpointGroup[] => {
		const byBase = new Map<string, EndpointGroup>();
		for (const e of eps) {
			const base = baseTagOf(e.tag);
			if (!byBase.has(base)) {
				byBase.set(base, { baseTag: base, main: null, tiers: {}, tierOnly: false });
			}
			const g = byBase.get(base);
			const tier = tierOf(e.tag);
			if (tier) {
				g.tiers[tier] = e;
			} else {
				g.main = e;
			}
		}
		for (const g of byBase.values()) {
			if (!g.main) {
				g.tierOnly = true;
				g.main = g.tiers.flex ?? g.tiers.priority;
			}
		}
		return [...byBase.values()];
	};

	$effect(() => {
		groups = groupEndpoints(endpoints);
	});

	const handleToggle = () => {
		if (!enabled) {
			providerOnly = [];
			providerOrder = [];
		}
	};

	const toggleProvider = (baseTag: string) => {
		if (providerOnly.includes(baseTag)) {
			// Also sweep any legacy tier-suffixed tags of the same base so
			// unchecking a provider never leaves phantom tier entries behind.
			providerOnly = providerOnly.filter((t) => t !== baseTag && baseTagOf(t) !== baseTag);
			providerOrder = providerOrder.filter((t) => t !== baseTag && baseTagOf(t) !== baseTag);
		} else {
			providerOnly = [...providerOnly, baseTag];
			providerOrder = [...providerOrder, baseTag];
		}
	};

	const removeFromOrder = (tag: string) => {
		providerOnly = providerOnly.filter((t) => t !== tag);
		providerOrder = providerOrder.filter((t) => t !== tag);
	};

	const moveUp = (idx: number) => {
		if (idx <= 0) return;
		const newOrder = [...providerOrder];
		[newOrder[idx - 1], newOrder[idx]] = [newOrder[idx], newOrder[idx - 1]];
		providerOrder = newOrder;
	};

	const moveDown = (idx: number) => {
		if (idx >= providerOrder.length - 1) return;
		const newOrder = [...providerOrder];
		[newOrder[idx], newOrder[idx + 1]] = [newOrder[idx + 1], newOrder[idx]];
		providerOrder = newOrder;
	};

	const formatPrice = (priceStr: string) => {
		const price = parseFloat(priceStr);
		if (isNaN(price)) return '-';
		const perMillion = price * 1_000_000;
		if (perMillion < 0.01) return '<$0.01';
		return `$${perMillion.toFixed(2)}`;
	};

	// Label for an entry in the saved order list. Handles base tags (normal
	// case), legacy tier-suffixed tags, and tags whose endpoint disappeared.
	const orderLabel = (tag: string): string => {
		const group = groups.find((g) => g.baseTag === tag);
		if (group) {
			const variant = variantOf(group.baseTag);
			return variant ? `${group.main?.provider_name} · ${variant}` : group.main?.provider_name;
		}
		const endpoint = endpoints.find((e) => e.tag === tag);
		if (endpoint) {
			const tier = tierOf(tag);
			return tier ? `${endpoint.provider_name} · ${tier}` : endpoint.provider_name;
		}
		return tag;
	};
</script>

{#if loading || endpoints.length > 0}
	<div class="my-2">
		<div class="px-4 py-3 bg-gray-50 dark:bg-gray-950 rounded-2xl">
			<div class="flex w-full justify-between items-center">
				<div class="self-center text-sm font-semibold">{$i18n.t('Provider Routing')}</div>
				<div class="flex items-center gap-2">
					{#if loading}
						<span class="text-xs text-gray-400">{$i18n.t('Loading...')}</span>
					{/if}
					{#if endpoints.length > 0}
						<Switch bind:state={enabled} onchange={handleToggle} />
					{/if}
				</div>
			</div>
			<div class="mt-1 text-xs text-gray-500 dark:text-gray-500 mb-3">
				{$i18n.t(
					'Select and order which OpenRouter providers handle requests for this model. Flex/priority prices apply when that service tier is requested.'
				)}
			</div>

			{#if !loading && endpoints.length === 0}
				<div class="text-xs text-gray-400 dark:text-gray-500 italic">
					{baseModelId
						? 'No providers found for this model (may not be an OpenRouter model).'
						: 'No base model selected.'}
				</div>
			{:else if endpoints.length > 0}
				<div class="overflow-x-auto">
					<table class="w-full text-xs">
						<thead>
							<tr
								class="text-left text-gray-500 dark:text-gray-400 border-b border-gray-200 dark:border-gray-800"
							>
								<th class="pb-1.5 pr-2 w-6"></th>
								<th class="pb-1.5 pr-3">{$i18n.t('Provider')}</th>
								<th class="pb-1.5 pr-3 text-right">{$i18n.t('Prompt')}</th>
								<th class="pb-1.5 pr-3 text-right">{$i18n.t('Completion')}</th>
								<th class="pb-1.5 pr-3 text-right">{$i18n.t('Throughput')}</th>
								<th class="pb-1.5 pr-3 text-right">{$i18n.t('Latency')}</th>
								<th class="pb-1.5 text-right">{$i18n.t('Uptime')}</th>
							</tr>
						</thead>
						<tbody>
							{#each groups as group (group.baseTag)}
								{@const endpoint = group.main}
								<tr
									class="border-b border-gray-100 dark:border-gray-800/50 hover:bg-gray-100 dark:hover:bg-gray-900 transition cursor-pointer"
									onclick={() => toggleProvider(group.baseTag)}
								>
									<td class="py-1.5 pr-2 align-top">
										<input
											type="checkbox"
											checked={providerOnly.includes(group.baseTag)}
											onclick={stopPropagation(() => toggleProvider(group.baseTag))}
											class="cursor-pointer"
										/>
									</td>
									<td
										class="py-1.5 pr-3 font-medium text-gray-700 dark:text-gray-300 whitespace-nowrap"
									>
										<div class="flex items-center gap-1.5">
											{endpoint.provider_name}
											{#if variantOf(group.baseTag)}
												<span class="font-normal text-gray-400 dark:text-gray-500">
													{variantOf(group.baseTag)}
												</span>
											{/if}
											{#if group.tierOnly}
												<span class="font-normal text-gray-400 dark:text-gray-500 italic">
													({Object.keys(group.tiers).join('/')}
													{$i18n.t('only')})
												</span>
											{/if}
										</div>
										{#if !group.tierOnly && (group.tiers.flex || group.tiers.priority)}
											<div
												class="mt-0.5 font-normal text-[10px] leading-tight text-gray-400 dark:text-gray-500"
											>
												{#if group.tiers.flex}
													{$i18n.t('Flex')}
													{formatPrice(group.tiers.flex.pricing?.prompt ?? '0')}/{formatPrice(
														group.tiers.flex.pricing?.completion ?? '0'
													)}
												{/if}
												{#if group.tiers.flex && group.tiers.priority}
													<span class="mx-0.5">·</span>
												{/if}
												{#if group.tiers.priority}
													{$i18n.t('Priority')}
													{formatPrice(group.tiers.priority.pricing?.prompt ?? '0')}/{formatPrice(
														group.tiers.priority.pricing?.completion ?? '0'
													)}
												{/if}
											</div>
										{/if}
									</td>
									<td
										class="py-1.5 pr-3 text-right text-gray-500 dark:text-gray-400 whitespace-nowrap align-top"
									>
										{formatPrice(endpoint.pricing?.prompt ?? '0')}/M
									</td>
									<td
										class="py-1.5 pr-3 text-right text-gray-500 dark:text-gray-400 whitespace-nowrap align-top"
									>
										{formatPrice(endpoint.pricing?.completion ?? '0')}/M
									</td>
									<td
										class="py-1.5 pr-3 text-right text-gray-500 dark:text-gray-400 whitespace-nowrap align-top"
									>
										{endpoint.throughput_last_30m?.p50 ?? '-'} tok/s
									</td>
									<td
										class="py-1.5 pr-3 text-right text-gray-500 dark:text-gray-400 whitespace-nowrap align-top"
									>
										{endpoint.latency_last_30m?.p50
											? `${(endpoint.latency_last_30m.p50 / 1000).toFixed(1)}s`
											: '-'}
									</td>
									<td class="py-1.5 text-right whitespace-nowrap align-top">
										<span
											class={endpoint.uptime_last_30m >= 99
												? 'text-success dark:text-success-dark'
												: endpoint.uptime_last_30m >= 95
													? 'text-warning dark:text-warning-dark'
													: 'text-error-brick dark:text-error-brick-dark'}
										>
											{endpoint.uptime_last_30m?.toFixed(1) ?? '-'}%
										</span>
									</td>
								</tr>
							{/each}
						</tbody>
					</table>
				</div>

				{#if providerOrder.length > 0}
					<div class="mt-3 pt-3 border-t border-gray-200 dark:border-gray-800">
						<div class="text-xs font-semibold text-gray-600 dark:text-gray-400 mb-2">
							{$i18n.t('Provider Order')}
						</div>
						<div class="flex flex-col gap-1">
							{#each providerOrder as tag, idx}
								<div
									class="flex items-center justify-between px-2.5 py-1.5 bg-white dark:bg-gray-900 rounded-lg border-hairline border-gray-200 dark:border-gray-800"
								>
									<div class="flex items-center gap-2">
										<span class="text-xs text-gray-400 w-4 text-center">{idx + 1}</span>
										<span class="text-xs font-medium text-gray-700 dark:text-gray-300">
											{orderLabel(tag)}
										</span>
									</div>
									<div class="flex items-center gap-0.5">
										<button
											type="button"
											class="p-0.5 text-gray-400 hover:text-gray-600 dark:hover:text-gray-200 disabled:opacity-30 transition"
											disabled={idx === 0}
											onclick={() => moveUp(idx)}
										>
											<svg
												xmlns="http://www.w3.org/2000/svg"
												viewBox="0 0 16 16"
												fill="currentColor"
												class="size-3.5"
											>
												<path
													fill-rule="evenodd"
													d="M8 14a.75.75 0 0 1-.75-.75V4.56L4.03 7.78a.75.75 0 0 1-1.06-1.06l4.5-4.5a.75.75 0 0 1 1.06 0l4.5 4.5a.75.75 0 0 1-1.06 1.06L8.75 4.56v8.69A.75.75 0 0 1 8 14Z"
													clip-rule="evenodd"
												/>
											</svg>
										</button>
										<button
											type="button"
											class="p-0.5 text-gray-400 hover:text-gray-600 dark:hover:text-gray-200 disabled:opacity-30 transition"
											disabled={idx === providerOrder.length - 1}
											onclick={() => moveDown(idx)}
										>
											<svg
												xmlns="http://www.w3.org/2000/svg"
												viewBox="0 0 16 16"
												fill="currentColor"
												class="size-3.5"
											>
												<path
													fill-rule="evenodd"
													d="M8 2a.75.75 0 0 1 .75.75v8.69l3.22-3.22a.75.75 0 1 1 1.06 1.06l-4.5 4.5a.75.75 0 0 1-1.06 0l-4.5-4.5a.75.75 0 0 1 1.06-1.06l3.22 3.22V2.75A.75.75 0 0 1 8 2Z"
													clip-rule="evenodd"
												/>
											</svg>
										</button>
										<button
											type="button"
											class="p-0.5 text-gray-400 hover:text-error-brick dark:hover:text-error-brick-dark transition"
											aria-label={$i18n.t('Remove')}
											onclick={() => removeFromOrder(tag)}
										>
											<svg
												xmlns="http://www.w3.org/2000/svg"
												viewBox="0 0 16 16"
												fill="currentColor"
												class="size-3.5"
											>
												<path
													d="M5.28 4.22a.75.75 0 0 0-1.06 1.06L6.94 8l-2.72 2.72a.75.75 0 1 0 1.06 1.06L8 9.06l2.72 2.72a.75.75 0 1 0 1.06-1.06L9.06 8l2.72-2.72a.75.75 0 0 0-1.06-1.06L8 6.94 5.28 4.22Z"
												/>
											</svg>
										</button>
									</div>
								</div>
							{/each}
						</div>
					</div>
				{/if}
			{/if}
		</div>
	</div>
{/if}
