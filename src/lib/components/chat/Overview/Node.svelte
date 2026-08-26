<script lang="ts">
	import { WEBUI_BASE_URL } from '$lib/constants';
	import { Handle, Position } from '@xyflow/svelte';

	import ProfileImage from '../Messages/ProfileImage.svelte';
	import Tooltip from '$lib/components/common/Tooltip.svelte';

	interface OverviewNodeData {
		message?: any;
		user?: any;
		model?: any;
		direction?: 'vertical' | 'horizontal';
		isCurrent?: boolean;
		isActivePath?: boolean;
		orphaned?: boolean;
		cyclic?: boolean;
		versionIndex?: number;
		versionCount?: number;
	}

	interface Props {
		data: OverviewNodeData;
	}

	let { data = $bindable() }: Props = $props();

	const stringContent = (value: any): string => {
		if (typeof value === 'string') return value;
		if (Array.isArray(value)) {
			return value
				.map((part) => (typeof part === 'string' ? part : (part?.text ?? part?.content ?? '')))
				.filter(Boolean)
				.join(' ');
		}
		return '';
	};

	let preview = $derived(
		stringContent(data?.message?.preview || data?.message?.content).trim() ||
			(data?.message?._stub ? 'Saved version' : 'Empty message')
	);
	let vertical = $derived(data?.direction !== 'horizontal');
</script>

<Tooltip content={preview} className="w-full" allowHTML={false}>
	<div
		class="group w-60 cursor-pointer rounded-xl border bg-white px-3.5 py-3 shadow-sm transition hover:-translate-y-0.5 hover:shadow-md dark:bg-gray-900 {data?.isCurrent
			? 'border-book-cloth ring-2 ring-book-cloth/20 dark:border-kraft'
			: data?.isActivePath
				? 'border-kraft/60 dark:border-kraft/50'
				: 'border-gray-200 dark:border-gray-700'}"
	>
		<div class="flex items-start gap-2.5">
			<ProfileImage
				src={data?.message?.role === 'user'
					? (data?.user?.profile_image_url ?? `${WEBUI_BASE_URL}/user.png`)
					: (data?.model?.info?.meta?.profile_image_url ?? '')}
				className="size-5 shrink-0"
			/>
			<div class="min-w-0 flex-1">
				<div class="flex items-center justify-between gap-2">
					<div class="truncate text-xs font-medium text-gray-900 dark:text-gray-100">
						{data?.message?.role === 'user'
							? (data?.user?.name ?? 'You')
							: (data?.model?.name ?? data?.message?.model ?? 'Assistant')}
					</div>
					{#if (data?.versionCount ?? 0) > 1}
						<span
							class="shrink-0 rounded-full bg-gray-100 px-1.5 py-0.5 text-[10px] font-medium text-gray-600 dark:bg-gray-800 dark:text-gray-300"
						>
							{data.versionIndex ?? 1}/{data.versionCount ?? 1}
						</span>
					{/if}
				</div>
				<div class="mt-1 line-clamp-2 min-h-8 text-xs leading-4 text-gray-500 dark:text-gray-400">
					{preview}
				</div>
				{#if data?.orphaned || data?.cyclic}
					<div class="mt-1 text-[10px] font-medium text-amber-700 dark:text-amber-300">
						Preserved · ancestry unavailable
					</div>
				{:else if data?.isCurrent}
					<div class="mt-1 text-[10px] font-medium text-book-cloth dark:text-kraft">
						Current branch
					</div>
				{/if}
			</div>
		</div>
		<Handle
			type="target"
			position={vertical ? Position.Top : Position.Left}
			class="!size-2 !border-0 !bg-gray-400 dark:!bg-gray-600"
		/>
		<Handle
			type="source"
			position={vertical ? Position.Bottom : Position.Right}
			class="!size-2 !border-0 !bg-gray-400 dark:!bg-gray-600"
		/>
	</div>
</Tooltip>
