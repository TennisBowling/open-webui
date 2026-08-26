<script lang="ts">
	import { getContext, tick, untrack } from 'svelte';
	import type { Writable } from 'svelte/store';
	import type { i18n as i18nType } from 'i18next';
	import { useNodesInitialized, useSvelteFlow } from '@xyflow/svelte';

	import { getChatMessagesOverview } from '$lib/apis/chats';
	import { models, showOverview, user } from '$lib/stores';
	import { buildHistoryGraph } from '$lib/utils/chatHistoryGraph';

	import '@xyflow/svelte/dist/style.css';

	import CustomNode from './Node.svelte';
	import Flow from './Flow.svelte';
	import XMark from '../../icons/XMark.svelte';
	import ArrowLeft from '../../icons/ArrowLeft.svelte';

	const i18n = getContext<Writable<i18nType>>('i18n');
	const { fitView } = useSvelteFlow();
	const nodesInitialized = useNodesInitialized();

	interface Props {
		history: any;
		chatId?: string | null;
		onClose: Function;
		onNodeClick: Function;
	}

	let { history, chatId = null, onClose, onNodeClick }: Props = $props();

	let nodes = $state<any[]>([]);
	let edges = $state<any[]>([]);
	let overviewRows = $state<any[]>([]);
	let layoutDirection = $state<'vertical' | 'horizontal'>('vertical');
	let loading = $state(false);
	let loadError = $state('');
	let branchPointCount = $state(0);
	let orphanCount = $state(0);
	let loadSequence = 0;

	const nodeTypes = { custom: CustomNode };

	const focusNode = async (messageId?: string | null) => {
		if (!messageId || !nodes.some((node) => node.id === messageId)) return;
		await tick();
		await fitView({ nodes: [{ id: messageId }], padding: 0.35, duration: 250 });
	};

	const loadOverview = async (id: string | null) => {
		const sequence = ++loadSequence;
		overviewRows = [];
		loadError = '';
		if (!id || id.startsWith('local:')) {
			loading = false;
			return;
		}

		loading = true;
		try {
			const rows = await getChatMessagesOverview(localStorage.token, id);
			if (sequence !== loadSequence) return;
			overviewRows = Array.isArray(rows) ? rows : [];
		} catch (error: any) {
			if (sequence !== loadSequence) return;
			console.error('Failed to load chat overview', error);
			loadError = error?.detail ?? error?.message ?? `${error}`;
		} finally {
			if (sequence === loadSequence) loading = false;
		}
	};

	const drawFlow = () => {
		const messages: Record<string, any> = {};
		for (const [id, message] of Object.entries(history?.messages ?? {})) {
			messages[id] = { id, ...(message as any) };
		}
		for (const row of overviewRows) {
			if (!row?.id) continue;
			const existing = messages[row.id] ?? {};
			messages[row.id] = {
				...existing,
				...row,
				childrenIds: existing.childrenIds ?? [],
				content: existing._stub ? (row.preview ?? '') : (existing.content ?? row.preview ?? ''),
				preview: row.preview ?? existing.preview ?? '',
				_stub: existing._stub ?? false
			};
		}

		const graph = buildHistoryGraph(messages, history?.currentId);
		branchPointCount = graph.branchPointCount;
		orphanCount = graph.orphanCount;

		const depthOffset = layoutDirection === 'vertical' ? 132 : 280;
		const branchOffset = layoutDirection === 'vertical' ? 280 : 120;
		nodes = graph.nodes.map((node) => ({
			id: node.id,
			type: 'custom',
			data: {
				user: $user,
				message: node.message,
				model: $models.find((model) => model.id === node.message.model),
				direction: layoutDirection,
				isCurrent: node.id === history?.currentId,
				isActivePath: graph.activePathIds.has(node.id),
				orphaned: node.orphaned,
				cyclic: node.cyclic,
				versionIndex: node.versionIndex,
				versionCount: node.versionCount
			},
			position:
				layoutDirection === 'vertical'
					? { x: node.column * branchOffset, y: node.depth * depthOffset }
					: { x: node.depth * depthOffset, y: node.column * branchOffset }
		}));
		edges = graph.edges.map((edge) => ({
			id: `${edge.source}-${edge.target}`,
			source: edge.source,
			target: edge.target,
			selectable: false,
			type: 'smoothstep',
			animated: graph.activePathIds.has(edge.source) && graph.activePathIds.has(edge.target),
			class:
				graph.activePathIds.has(edge.source) && graph.activePathIds.has(edge.target)
					? 'overview-edge-active'
					: 'overview-edge'
		}));
	};

	const setLayoutDirection = async (direction: 'vertical' | 'horizontal') => {
		layoutDirection = direction;
		await tick();
		drawFlow();
		await tick();
		await focusNode(history?.currentId);
	};

	$effect(() => {
		if (nodesInitialized.current) void focusNode(history?.currentId);
	});

	$effect(() => {
		const id = chatId;
		untrack(() => void loadOverview(id));
	});

	$effect(() => {
		// Explicit reads establish the reactive inputs without tying redraws to
		// streaming token content; the graph changes on map/currentId/overview rows.
		history?.currentId;
		Object.keys(history?.messages ?? {}).length;
		overviewRows;
		layoutDirection;
		drawFlow();
	});
</script>

<div class="relative h-full w-full overflow-hidden bg-white dark:bg-gray-850">
	<div
		class="absolute inset-x-0 top-0 z-50 flex items-center justify-between border-b border-gray-100 bg-white/90 px-3 py-3 backdrop-blur dark:border-gray-800 dark:bg-gray-850/90 dark:text-gray-100"
	>
		<div class="flex min-w-0 items-center gap-2.5">
			<button
				type="button"
				class="rounded-lg p-2 text-gray-500 transition hover:bg-gray-100 hover:text-gray-900 dark:text-gray-400 dark:hover:bg-gray-800 dark:hover:text-white"
				aria-label={$i18n.t('Back')}
				onclick={() => showOverview.set(false)}
			>
				<ArrowLeft className="size-3.5" />
			</button>
			<div class="min-w-0">
				<div class="truncate font-primary text-base font-medium">{$i18n.t('Chat Overview')}</div>
				<div class="text-[11px] text-gray-500 dark:text-gray-400">
					{nodes.length}
					{$i18n.t('messages')} · {branchPointCount}
					{$i18n.t('branches')}
				</div>
			</div>
		</div>
		<button
			type="button"
			class="rounded-full p-2 text-gray-500 transition hover:bg-gray-100 hover:text-gray-700 dark:text-gray-400 dark:hover:bg-gray-800 dark:hover:text-gray-200"
			aria-label={$i18n.t('Close')}
			onclick={() => {
				onClose();
				showOverview.set(false);
			}}
		>
			<XMark className="size-3.5" />
		</button>
	</div>

	{#if nodes.length > 0}
		<Flow
			{nodes}
			{nodeTypes}
			{edges}
			{layoutDirection}
			{setLayoutDirection}
			onnodeclick={(event: any) => {
				const node = event.detail?.node;
				if (!node) return;
				onNodeClick({ ...event.detail, node });
				void focusNode(node.id);
			}}
		/>
	{:else if loading}
		<div class="flex h-full items-center justify-center px-6 text-sm text-gray-500">
			{$i18n.t('Loading...')}
		</div>
	{:else}
		<div class="flex h-full items-center justify-center px-6 text-center text-sm text-gray-500">
			<div>
				<div class="font-medium text-gray-700 dark:text-gray-200">
					{$i18n.t('No message history yet')}
				</div>
				{#if loadError}
					<button type="button" class="mt-2 underline" onclick={() => void loadOverview(chatId)}>
						{$i18n.t('Retry')}
					</button>
				{/if}
			</div>
		</div>
	{/if}

	{#if orphanCount > 0}
		<div
			class="pointer-events-none absolute bottom-3 left-1/2 z-40 -translate-x-1/2 rounded-full border border-amber-200 bg-amber-50/95 px-3 py-1.5 text-[11px] text-amber-800 shadow-sm dark:border-amber-900 dark:bg-amber-950/90 dark:text-amber-200"
		>
			{orphanCount}
			{$i18n.t('preserved disconnected messages')}
		</div>
	{/if}
</div>
