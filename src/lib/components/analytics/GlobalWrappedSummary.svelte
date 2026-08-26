<script lang="ts">
	import { getContext, onMount } from 'svelte';
	import {
		getGlobalWrapped,
		getGlobalModelUsage,
		formatTokenCount,
		type GlobalWrappedSummary,
		type ModelUsage
	} from '$lib/apis/analytics';

	const i18n = getContext('i18n');

	interface Props {
		year?: number | undefined;
	}

	let { year = undefined }: Props = $props();

	let loading = $state(true);
	let error: string | null = $state(null);
	let wrapped: GlobalWrappedSummary | null = $state(null);
	let modelUsage: ModelUsage[] = $state([]);

	onMount(async () => {
		await loadGlobalData();
	});

	async function loadGlobalData() {
		loading = true;
		error = null;

		try {
			const token = localStorage.getItem('token');
			if (!token) {
				error = 'Not authenticated';
				loading = false;
				return;
			}

			const [wrappedData, modelData] = await Promise.all([
				getGlobalWrapped(token, year),
				getGlobalModelUsage(token, 10)
			]);

			wrapped = wrappedData;
			modelUsage = modelData;
		} catch (e) {
			error = 'Error loading global data';
			console.error(e);
		}

		loading = false;
	}

	function getModelDisplayName(modelId: string): string {
		const parts = modelId.split('/');
		return parts[parts.length - 1] || modelId;
	}

	$effect(() => {
		if (year !== undefined) {
			loadGlobalData();
		}
	});
</script>

<div class="w-full">
	{#if loading}
		<div class="flex items-center justify-center py-24">
			<div class="flex flex-col items-center gap-4">
				<div
					class="w-12 h-12 border-2 border-book-cloth border-t-transparent rounded-full animate-spin"
				></div>
				<div class="text-sm text-gray-500 dark:text-gray-400 animate-pulse">
					Loading global data…
				</div>
			</div>
		</div>
	{:else if error}
		<div
			class="text-center py-16 rounded-2xl border-hairline border-error-brick/20 bg-error-brick/10"
		>
			<div class="text-error-brick dark:text-error-brick-dark font-semibold text-lg mb-1">
				Something went wrong
			</div>
			<div class="text-sm text-error-brick dark:text-error-brick-dark">{error}</div>
		</div>
	{:else if wrapped}
		<!-- Hero Stats -->
		<div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4 mb-10">
			<!-- Total Tokens -->
			<div
				class="group relative bg-gray-50 dark:bg-gray-850 border-hairline border-gray-200 dark:border-gray-800 rounded-2xl p-6 overflow-hidden transition-colors duration-200 ease-paper hover:border-book-cloth/40"
			>
				<div
					class="absolute top-0 right-0 p-3 text-book-cloth/25 dark:text-kraft/25 group-hover:text-book-cloth/50 dark:group-hover:text-kraft/50 transition-colors duration-200"
				>
					<svg
						xmlns="http://www.w3.org/2000/svg"
						viewBox="0 0 24 24"
						fill="currentColor"
						class="w-11 h-11"
					>
						<path
							fill-rule="evenodd"
							d="M2.25 12c0-5.385 4.365-9.75 9.75-9.75s9.75 4.365 9.75 9.75-4.365 9.75-9.75 9.75S2.25 17.385 2.25 12Zm11.378-3.917c-.89-.777-2.366-.777-3.255 0a.75.75 0 0 1-.988-1.129c1.454-1.272 3.776-1.272 5.23 0 1.513 1.324 1.513 3.518 0 4.842a3.75 3.75 0 0 1-.837.552c-.676.328-1.028.774-1.028 1.152v.75a.75.75 0 0 1-1.5 0v-.75c0-1.279 1.06-2.107 1.875-2.502.182-.088.351-.199.503-.331.83-.727.83-1.857 0-2.584ZM12 18a.75.75 0 1 0 0-1.5.75.75 0 0 0 0 1.5Z"
							clip-rule="evenodd"
						/>
					</svg>
				</div>
				<div class="text-xs text-gray-500 dark:text-gray-400 uppercase tracking-wide mb-2">
					{$i18n.t('Global request tokens')}
				</div>
				<div class="font-display text-5xl md:text-6xl leading-none text-gray-900 dark:text-white">
					{formatTokenCount(wrapped.total_tokens)}
				</div>
			</div>

			<!-- Active Users -->
			<div
				class="group relative bg-gray-50 dark:bg-gray-850 border-hairline border-gray-200 dark:border-gray-800 rounded-2xl p-6 overflow-hidden transition-colors duration-200 ease-paper hover:border-book-cloth/40"
			>
				<div
					class="absolute top-0 right-0 p-3 text-book-cloth/25 dark:text-kraft/25 group-hover:text-book-cloth/50 dark:group-hover:text-kraft/50 transition-colors duration-200"
				>
					<svg
						xmlns="http://www.w3.org/2000/svg"
						viewBox="0 0 24 24"
						fill="currentColor"
						class="w-11 h-11"
					>
						<path
							fill-rule="evenodd"
							d="M7.5 6a4.5 4.5 0 1 1 9 0 4.5 4.5 0 0 1-9 0ZM3.751 20.105a8.25 8.25 0 0 1 16.498 0 .75.75 0 0 1-.437.695A18.683 18.683 0 0 1 12 22.5c-2.786 0-5.433-.608-7.812-1.7a.75.75 0 0 1-.437-.695Z"
							clip-rule="evenodd"
						/>
					</svg>
				</div>
				<div class="text-xs text-gray-500 dark:text-gray-400 uppercase tracking-wide mb-2">
					{$i18n.t('Active users')}
				</div>
				<div class="font-display text-5xl md:text-6xl leading-none text-gray-900 dark:text-white">
					{wrapped.total_users_active.toLocaleString()}
				</div>
			</div>

			<!-- Conversations -->
			<div
				class="group relative bg-gray-50 dark:bg-gray-850 border-hairline border-gray-200 dark:border-gray-800 rounded-2xl p-6 overflow-hidden transition-colors duration-200 ease-paper hover:border-book-cloth/40"
			>
				<div
					class="absolute top-0 right-0 p-3 text-book-cloth/25 dark:text-kraft/25 group-hover:text-book-cloth/50 dark:group-hover:text-kraft/50 transition-colors duration-200"
				>
					<svg
						xmlns="http://www.w3.org/2000/svg"
						viewBox="0 0 24 24"
						fill="currentColor"
						class="w-11 h-11"
					>
						<path
							fill-rule="evenodd"
							d="M4.804 21.644A6.707 6.707 0 0 0 6 21.75a6.721 6.721 0 0 0 3.583-1.029c.774.182 1.584.279 2.417.279 5.322 0 9.75-3.97 9.75-9 0-5.03-4.428-9-9.75-9s-9.75 3.97-9.75 9c0 2.409 1.025 4.587 2.674 6.192.232.226.277.428.254.543a3.73 3.73 0 0 1-.814 1.686.75.75 0 0 0 .44 1.223ZM8.25 10.875a1.125 1.125 0 1 0 0 2.25 1.125 1.125 0 0 0 0-2.25ZM10.875 12a1.125 1.125 0 1 1 2.25 0 1.125 1.125 0 0 1-2.25 0Zm4.875-1.125a1.125 1.125 0 1 0 0 2.25 1.125 1.125 0 0 0 0-2.25Z"
							clip-rule="evenodd"
						/>
					</svg>
				</div>
				<div class="text-xs text-gray-500 dark:text-gray-400 uppercase tracking-wide mb-2">
					{$i18n.t('Conversations')}
				</div>
				<div class="font-display text-5xl md:text-6xl leading-none text-gray-900 dark:text-white">
					{wrapped.total_conversations.toLocaleString()}
				</div>
			</div>

			<!-- Messages -->
			<div
				class="group relative bg-gray-50 dark:bg-gray-850 border-hairline border-gray-200 dark:border-gray-800 rounded-2xl p-6 overflow-hidden transition-colors duration-200 ease-paper hover:border-book-cloth/40"
			>
				<div
					class="absolute top-0 right-0 p-3 text-book-cloth/25 dark:text-kraft/25 group-hover:text-book-cloth/50 dark:group-hover:text-kraft/50 transition-colors duration-200"
				>
					<svg
						xmlns="http://www.w3.org/2000/svg"
						viewBox="0 0 24 24"
						fill="currentColor"
						class="w-11 h-11"
					>
						<path
							fill-rule="evenodd"
							d="M4.848 2.771A49.144 49.144 0 0 1 12 2.25c2.43 0 4.817.178 7.152.52 1.978.292 3.348 2.024 3.348 3.97v6.02c0 1.946-1.37 3.678-3.348 3.97a48.901 48.901 0 0 1-3.476.383.39.39 0 0 0-.297.17l-2.755 4.133a.75.75 0 0 1-1.248 0l-2.755-4.133a.39.39 0 0 0-.297-.17 48.9 48.9 0 0 1-3.476-.384c-1.978-.29-3.348-2.024-3.348-3.97V6.741c0-1.946 1.37-3.68 3.348-3.97ZM6.75 8.25a.75.75 0 0 1 .75-.75h9a.75.75 0 0 1 0 1.5h-9a.75.75 0 0 1-.75-.75Zm.75 2.25a.75.75 0 0 0 0 1.5H12a.75.75 0 0 0 0-1.5H7.5Z"
							clip-rule="evenodd"
						/>
					</svg>
				</div>
				<div class="text-xs text-gray-500 dark:text-gray-400 uppercase tracking-wide mb-2">
					{$i18n.t('Messages')}
				</div>
				<div class="font-display text-5xl md:text-6xl leading-none text-gray-900 dark:text-white">
					{wrapped.total_messages.toLocaleString()}
				</div>
			</div>
		</div>

		<!-- Busiest Day -->
		{#if wrapped.busiest_day}
			<div class="mb-10">
				<div
					class="bg-manilla/30 dark:bg-gray-850 border-hairline border-gray-200 dark:border-gray-800 rounded-2xl p-8"
				>
					<div class="flex items-center justify-between mb-6">
						<div>
							<h4 class="text-xs text-gray-500 dark:text-gray-400 uppercase tracking-wide mb-1">
								{$i18n.t('Busiest day')}
							</h4>
							<div class="font-display text-3xl text-gray-900 dark:text-white">
								{wrapped.busiest_day.day_of_week}
							</div>
						</div>
						<div
							class="p-3 rounded-xl bg-book-cloth/15 border-hairline border-book-cloth/20 text-book-cloth dark:text-kraft"
						>
							<svg
								xmlns="http://www.w3.org/2000/svg"
								viewBox="0 0 24 24"
								fill="currentColor"
								class="size-6"
							>
								<path
									fill-rule="evenodd"
									d="M12.963 2.286a.75.75 0 0 0-1.071-.136 9.742 9.742 0 0 0-3.539 6.176 7.547 7.547 0 0 1-1.705-1.715.75.75 0 0 0-1.152-.082A9 9 0 1 0 15.68 4.534a7.46 7.46 0 0 1-2.717-2.248ZM15.75 14.25a3.75 3.75 0 1 1-7.313-1.172c.628.465 1.35.81 2.133 1a5.99 5.99 0 0 1 1.925-3.546 3.75 3.75 0 0 1 3.255 3.718Z"
									clip-rule="evenodd"
								/>
							</svg>
						</div>
					</div>

					<div class="grid grid-cols-1 md:grid-cols-2 gap-8">
						<div
							class="flex justify-between items-end border-b-hairline border-gray-200 dark:border-gray-800 pb-2"
						>
							<span class="text-xs text-gray-500 dark:text-gray-400 uppercase">Date</span>
							<span class="text-sm font-medium text-gray-900 dark:text-white">
								{new Date(wrapped.busiest_day.date).toLocaleDateString(undefined, {
									year: 'numeric',
									month: 'long',
									day: 'numeric'
								})}
							</span>
						</div>
						<div
							class="flex justify-between items-end border-b-hairline border-gray-200 dark:border-gray-800 pb-2"
						>
							<span class="text-xs text-gray-500 dark:text-gray-400 uppercase"
								>{$i18n.t('Total tokens')}</span
							>
							<span class="text-sm font-medium text-book-cloth dark:text-kraft">
								{formatTokenCount(wrapped.busiest_day.tokens)}
							</span>
						</div>
					</div>
				</div>
			</div>
		{/if}

		<!-- Model Leaderboard -->
		{#if modelUsage.length > 0}
			<div>
				<div class="flex items-center gap-3 mb-6">
					<span class="w-2 h-2 rounded-full bg-book-cloth"></span>
					<h3 class="text-base font-semibold text-gray-900 dark:text-white">
						{$i18n.t('Model leaderboard')}
					</h3>
					<div class="h-px flex-1 bg-gray-200 dark:bg-gray-800"></div>
				</div>

				<div class="space-y-3">
					{#each modelUsage as model, index}
						<div
							class="group bg-gray-50 dark:bg-gray-850 border-hairline border-gray-200 dark:border-gray-800 rounded-2xl p-4 transition-colors duration-200 ease-paper hover:border-book-cloth/40"
						>
							<div class="flex items-center gap-6">
								<div
									class="font-display text-4xl w-12 text-center {index < 3
										? 'text-book-cloth dark:text-kraft'
										: 'text-gray-400 dark:text-gray-600'}"
								>
									#{index + 1}
								</div>

								<div class="flex-1 min-w-0">
									<div class="flex items-center justify-between mb-2">
										<div class="font-semibold text-gray-900 dark:text-white truncate text-base">
											{getModelDisplayName(model.model_id)}
										</div>
										<div class="text-sm font-medium text-book-cloth dark:text-kraft">
											{model.percentage.toFixed(1)}%
										</div>
									</div>

									<div class="h-2 bg-gray-100 dark:bg-gray-800 w-full overflow-hidden rounded-full">
										<div
											class="h-full bg-book-cloth transition-all duration-1000 ease-out rounded-full"
											style="width: {model.percentage}%"
										></div>
									</div>

									<div class="flex justify-between mt-2 text-xs text-gray-500 dark:text-gray-400">
										<span>{model.conversation_count.toLocaleString()} Conversations</span>
										<span>{formatTokenCount(model.total_tokens)} Tokens</span>
									</div>
								</div>
							</div>
						</div>
					{/each}
				</div>
			</div>
		{/if}

		<!-- Top Models from wrapped -->
		{#if wrapped.top_models && wrapped.top_models.length > 0}
			<!-- Already showing model leaderboard above -->
		{/if}
	{:else}
		<div
			class="text-center py-16 rounded-2xl border-hairline border-gray-200 dark:border-gray-800 bg-gray-50 dark:bg-gray-850"
		>
			<div class="text-sm text-gray-500 dark:text-gray-400">No global data available.</div>
		</div>
	{/if}
</div>
