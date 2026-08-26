<script lang="ts">
	import { preventDefault } from '$lib/utils/eventModifiers';

	import { onMount, onDestroy, getContext } from 'svelte';
	import { toast } from '$lib/utils/toast';

	import {
		getChatEmbeddingConfig,
		updateChatEmbeddingConfig,
		verifyChatEmbeddingConnection,
		getChatEmbeddingStats,
		rebuildChatEmbeddings,
		retryFailedChatEmbeddings
	} from '$lib/apis';

	import Switch from '$lib/components/common/Switch.svelte';
	import Tooltip from '$lib/components/common/Tooltip.svelte';
	import Spinner from '$lib/components/common/Spinner.svelte';
	import ConfirmDialog from '$lib/components/common/ConfirmDialog.svelte';

	const i18n = getContext('i18n');

	interface Props {
		saveHandler: Function;
	}

	let { saveHandler }: Props = $props();

	// Config
	let enabled = $state(true);
	let embedUrl = $state('');
	let embedModel = $state('');
	let embedDim: number | null = $state(null);
	let sweepInterval = $state(120);
	let textBatch = $state(16);

	// Verify state
	let verifying = $state(false);
	let verifyResult: { ok: boolean; message: string } | null = $state(null);

	// Stats
	let stats: { embedded: number; failed: number; pending: number; rebuilding?: boolean } | null =
		$state(null);
	let statsLoading = $state(false);

	// Rebuild
	let showRebuildConfirm = $state(false);
	let rebuilding = $state(false);
	let pollTimer: ReturnType<typeof setInterval> | null = null;

	const submitHandler = async () => {
		const res = await updateChatEmbeddingConfig(localStorage.token, {
			ENABLE_CHAT_SEMANTIC_SEARCH: enabled,
			CHAT_EMBED_URL: (embedUrl ?? '').trim().replace(/\/+$/, ''),
			CHAT_EMBED_MODEL: (embedModel ?? '').trim(),
			CHAT_EMBED_SWEEP_INTERVAL: Math.max(10, Number(sweepInterval) || 120),
			CHAT_EMBED_TEXT_BATCH: Math.max(1, Math.min(128, Number(textBatch) || 16))
		}).catch((err) => {
			toast.error(`${err?.detail ?? err}`);
			return null;
		});

		if (res) {
			embedUrl = res.CHAT_EMBED_URL ?? embedUrl;
			embedModel = res.CHAT_EMBED_MODEL ?? embedModel;
			embedDim = res.CHAT_EMBED_DIM ?? embedDim;
			sweepInterval = res.CHAT_EMBED_SWEEP_INTERVAL ?? sweepInterval;
			textBatch = res.CHAT_EMBED_TEXT_BATCH ?? textBatch;
			toast.success($i18n.t('Settings saved successfully'));
		}
	};

	const verifyHandler = async () => {
		verifying = true;
		verifyResult = null;
		try {
			// Probe the value currently in the box (before saving) so the admin can
			// validate a new URL before committing it.
			const res = await verifyChatEmbeddingConnection(localStorage.token, {
				CHAT_EMBED_URL: (embedUrl ?? '').trim().replace(/\/+$/, ''),
				CHAT_EMBED_MODEL: (embedModel ?? '').trim()
			});
			if (res?.status) {
				if (res.dim_matches) {
					verifyResult = {
						ok: true,
						message: $i18n.t('Connected — returned a {{DIM}}-dim vector.', { DIM: res.dim })
					};
					toast.success($i18n.t('Embedder connection verified'));
				} else {
					// Reachable, but the vector width won't fit the stored index — a rebuild
					// after a schema/dim change would be required.
					verifyResult = {
						ok: false,
						message: $i18n.t(
							'Reachable, but returned {{DIM}}-dim vectors (expected {{EXPECTED}}). This model is incompatible with the stored index.',
							{ DIM: res.dim, EXPECTED: res.expected_dim }
						)
					};
					toast.error($i18n.t('Embedder dimension mismatch'));
				}
			}
		} catch (err) {
			verifyResult = { ok: false, message: `${err}` };
			toast.error(`${err}`);
		} finally {
			verifying = false;
		}
	};

	const refreshStats = async () => {
		statsLoading = true;
		const res = await getChatEmbeddingStats(localStorage.token).catch(() => null);
		statsLoading = false;
		if (res) {
			stats = res;
			// Poll for live progress only while a manual rebuild sweep is actually
			// running (backend flag). Gating on pending>0 instead would poll forever
			// (every 4s, an expensive count) whenever the embedder is unreachable.
			if (res.rebuilding) {
				rebuilding = true;
				startPolling();
			} else {
				rebuilding = false;
				stopPolling();
			}
		}
	};

	const startPolling = () => {
		if (pollTimer) return;
		pollTimer = setInterval(() => {
			// A backgrounded admin tab shouldn't keep firing the (expensive) count
			// query — the next visible tick catches up.
			if (!document.hidden) {
				refreshStats();
			}
		}, 4000);
	};

	const stopPolling = () => {
		if (pollTimer) {
			clearInterval(pollTimer);
			pollTimer = null;
		}
	};

	const retryFailedHandler = async () => {
		const res = await retryFailedChatEmbeddings(localStorage.token).catch((err) => {
			toast.error(`${err?.detail ?? err}`);
			return null;
		});
		if (res?.status) {
			rebuilding = true;
			toast.success(
				$i18n.t('Retrying {{COUNT}} failed embeddings in the background.', {
					COUNT: res.cleared
				})
			);
			await refreshStats();
			startPolling();
		}
	};

	const rebuildHandler = async () => {
		const res = await rebuildChatEmbeddings(localStorage.token).catch((err) => {
			toast.error(`${err?.detail ?? err}`);
			return null;
		});
		if (res?.status) {
			rebuilding = true;
			toast.success(
				$i18n.t('Cleared {{COUNT}} embeddings — re-embedding in the background.', {
					COUNT: res.deleted
				})
			);
			await refreshStats();
			startPolling();
		}
	};

	onMount(async () => {
		const res = await getChatEmbeddingConfig(localStorage.token).catch(() => null);
		if (res) {
			enabled = res.ENABLE_CHAT_SEMANTIC_SEARCH ?? true;
			embedUrl = res.CHAT_EMBED_URL ?? '';
			embedModel = res.CHAT_EMBED_MODEL ?? '';
			embedDim = res.CHAT_EMBED_DIM ?? null;
			sweepInterval = res.CHAT_EMBED_SWEEP_INTERVAL ?? 120;
			textBatch = res.CHAT_EMBED_TEXT_BATCH ?? 16;
		}
		await refreshStats();
	});

	onDestroy(() => {
		stopPolling();
	});
</script>

<ConfirmDialog
	bind:show={showRebuildConfirm}
	title={$i18n.t('Rebuild all embeddings?')}
	message={$i18n.t(
		'This deletes every stored chat embedding and re-embeds all messages from scratch using the current embedder + model. Semantic search stays available (degrading to keyword-only for not-yet-embedded messages) while it runs. This can take a while on large histories.'
	)}
	confirmLabel={$i18n.t('Rebuild')}
	onconfirm={rebuildHandler}
/>

<form
	class="flex flex-col h-full justify-between space-y-3 text-sm"
	onsubmit={preventDefault(async () => {
		await submitHandler();
		saveHandler();
	})}
>
	<div class=" space-y-3 overflow-y-scroll scrollbar-hidden h-full">
		<div class="mb-3">
			<div class=" mb-2.5 text-base font-medium">{$i18n.t('Chat Search Embeddings')}</div>

			<hr class=" border-gray-100 dark:border-gray-850 my-2" />

			<div class="text-xs text-gray-500 dark:text-gray-400 mb-3">
				{$i18n.t(
					'Semantic chat search embeds your messages with an external embedding model and blends the results with keyword search. Configure the embedder here.'
				)}
			</div>

			<div class="mb-2.5 flex w-full justify-between">
				<div class=" self-center text-xs font-medium">
					{$i18n.t('Enable Semantic Search')}
				</div>
				<div class="flex items-center relative">
					<Switch bind:state={enabled} />
				</div>
			</div>

			<hr class=" border-gray-100 dark:border-gray-850 my-3" />

			<!-- Embedder URL + verify -->
			<div class="mb-2.5 flex w-full flex-col">
				<div class=" self-center text-xs font-medium mb-1 w-full">
					<Tooltip
						content={$i18n.t('Base URL of the embedding server (e.g. http://127.0.0.1:8085).')}
					>
						{$i18n.t('Embedder URL')}
					</Tooltip>
				</div>
				<div class="flex gap-2">
					<input
						class="w-full rounded-lg py-2 px-4 text-sm bg-gray-50 dark:text-gray-300 dark:bg-gray-850 outline-hidden"
						placeholder={$i18n.t('Enter embedder URL')}
						bind:value={embedUrl}
					/>
					<button
						type="button"
						class="shrink-0 px-3.5 py-1.5 text-sm font-medium rounded-lg bg-gray-50 dark:bg-gray-850 hover:bg-gray-100 dark:hover:bg-gray-800 transition flex items-center gap-1.5 disabled:opacity-50"
						onclick={verifyHandler}
						disabled={verifying || !(embedUrl ?? '').trim()}
					>
						{#if verifying}
							<Spinner className="size-3.5" />
						{/if}
						{$i18n.t('Verify')}
					</button>
				</div>
				{#if verifyResult}
					<div
						class="mt-1.5 text-xs {verifyResult.ok
							? 'text-green-600 dark:text-green-400'
							: 'text-red-600 dark:text-red-400'}"
					>
						{verifyResult.message}
					</div>
				{/if}
			</div>

			<!-- Model name -->
			<div class="mb-2.5 flex w-full flex-col">
				<div class=" self-center text-xs font-medium mb-1 w-full">
					<Tooltip content={$i18n.t('Model name sent with embedding requests.')}>
						{$i18n.t('Embedding Model')}
					</Tooltip>
				</div>
				<input
					class="w-full rounded-lg py-2 px-4 text-sm bg-gray-50 dark:text-gray-300 dark:bg-gray-850 outline-hidden"
					placeholder={$i18n.t('Enter embedding model name')}
					bind:value={embedModel}
				/>
			</div>

			{#if embedDim != null}
				<div class="mb-2.5 text-xs text-gray-500 dark:text-gray-400">
					{$i18n.t('Vector dimension')}: <span class="font-mono">{embedDim}</span>
					<span class="opacity-70"
						>— {$i18n.t('fixed by the database index; a different width needs a rebuild.')}</span
					>
				</div>
			{/if}

			<hr class=" border-gray-100 dark:border-gray-850 my-3" />

			<!-- Deferred batching -->
			<div
				class="mb-1.5 text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wide"
			>
				{$i18n.t('Deferred Batching')}
			</div>

			<div class="text-xs text-gray-500 dark:text-gray-400 mb-2.5">
				{$i18n.t(
					'New messages are not embedded immediately — they accumulate and get embedded together on a schedule, one request at a time, to go easy on the inference server.'
				)}
			</div>

			<div class="mb-2.5 flex w-full justify-between">
				<div class="self-center text-xs font-medium">
					<Tooltip
						content={$i18n.t(
							'How often the deferred embedding pass runs. Raise it to accumulate more messages per pass; minimum 10 seconds.'
						)}
					>
						{$i18n.t('Sweep interval (seconds)')}
					</Tooltip>
				</div>
				<div class="flex items-center relative">
					<input
						type="number"
						min="10"
						step="1"
						class="w-24 rounded-lg py-1.5 px-3 text-sm bg-gray-50 dark:text-gray-300 dark:bg-gray-850 outline-hidden text-right"
						bind:value={sweepInterval}
					/>
				</div>
			</div>

			<div class="mb-2.5 flex w-full justify-between">
				<div class="self-center text-xs font-medium">
					<Tooltip
						content={$i18n.t(
							'How many messages go into each embedding request during a pass (1–128). Each message still gets its own vector; requests are sent strictly one at a time.'
						)}
					>
						{$i18n.t('Messages per request')}
					</Tooltip>
				</div>
				<div class="flex items-center relative">
					<input
						type="number"
						min="1"
						max="128"
						step="1"
						class="w-24 rounded-lg py-1.5 px-3 text-sm bg-gray-50 dark:text-gray-300 dark:bg-gray-850 outline-hidden text-right"
						bind:value={textBatch}
					/>
				</div>
			</div>

			<hr class=" border-gray-100 dark:border-gray-850 my-3" />

			<!-- Stats -->
			<div class="mb-1.5 flex items-center justify-between">
				<div class="text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wide">
					{$i18n.t('Index Status')}
				</div>
				<button
					type="button"
					class="text-xs text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-white flex items-center gap-1"
					onclick={refreshStats}
				>
					{#if statsLoading}
						<Spinner className="size-3" />
					{/if}
					{$i18n.t('Refresh')}
				</button>
			</div>

			<div class="grid grid-cols-3 gap-2 mb-2.5">
				<div class="rounded-lg bg-gray-50 dark:bg-gray-850 p-3 text-center">
					<div class="text-lg font-semibold">
						{(stats?.embedded ?? 0).toLocaleString()}
					</div>
					<div class="text-xs text-gray-500 dark:text-gray-400">{$i18n.t('Embedded')}</div>
				</div>
				<div class="rounded-lg bg-gray-50 dark:bg-gray-850 p-3 text-center">
					<div class="text-lg font-semibold">
						{(stats?.pending ?? 0).toLocaleString()}
					</div>
					<div class="text-xs text-gray-500 dark:text-gray-400">{$i18n.t('Pending')}</div>
				</div>
				<div class="rounded-lg bg-gray-50 dark:bg-gray-850 p-3 text-center">
					<div class="text-lg font-semibold">
						{(stats?.failed ?? 0).toLocaleString()}
					</div>
					<div class="text-xs text-gray-500 dark:text-gray-400">{$i18n.t('Failed')}</div>
				</div>
			</div>

			{#if rebuilding || stats?.rebuilding}
				<div class="mb-2.5 text-xs text-gray-500 dark:text-gray-400 flex items-center gap-1.5">
					<Spinner className="size-3" />
					{$i18n.t('Rebuilding — embedding pending messages in the background…')}
				</div>
			{/if}

			{#if (stats?.failed ?? 0) > 0}
				<div class="mb-2.5 flex w-full justify-between items-center">
					<div class="self-center text-xs font-medium max-w-[70%]">
						<Tooltip
							content={$i18n.t(
								'Re-attempt the messages that previously failed to embed. Use after fixing or upgrading the embedder.'
							)}
						>
							{$i18n.t('Retry failed embeddings')}
						</Tooltip>
					</div>
					<button
						type="button"
						class="shrink-0 px-3.5 py-1.5 text-sm font-medium rounded-lg bg-gray-50 dark:bg-gray-850 hover:bg-gray-100 dark:hover:bg-gray-800 transition disabled:opacity-50"
						onclick={retryFailedHandler}
						disabled={rebuilding}
					>
						{$i18n.t('Retry failed')}
					</button>
				</div>
			{/if}

			<div class="mb-2.5 flex w-full justify-between items-center">
				<div class="self-center text-xs font-medium max-w-[70%]">
					<Tooltip
						content={$i18n.t(
							'Delete all stored embeddings and re-embed every message. Use after changing the embedding model.'
						)}
					>
						{$i18n.t('Rebuild all embeddings')}
					</Tooltip>
				</div>
				<button
					type="button"
					class="shrink-0 px-3.5 py-1.5 text-sm font-medium rounded-lg text-red-600 dark:text-red-400 bg-red-500/10 hover:bg-red-500/20 transition disabled:opacity-50"
					onclick={() => (showRebuildConfirm = true)}
					disabled={rebuilding}
				>
					{$i18n.t('Rebuild')}
				</button>
			</div>
		</div>
	</div>

	<div class="flex justify-end pt-3 text-sm font-medium">
		<button
			class="px-3.5 py-1.5 text-sm font-medium bg-book-cloth hover:bg-kraft text-white dark:bg-book-cloth dark:text-white dark:hover:bg-kraft transition-colors duration-200 ease-paper rounded-full"
			type="submit"
		>
			{$i18n.t('Save')}
		</button>
	</div>
</form>
