<script lang="ts" module>
	// Shared across selector instances/remounts: the model list arrives via the
	// boot bootstrap and stays fresh over the socket, so opening the picker
	// shouldn't hit the network every time. Refresh at most once per TTL, in the
	// background (never blocks the dropdown opening).
	let lastModelsRefreshAt = 0;
	const MODELS_REFRESH_TTL_MS = 5 * 60 * 1000;
</script>

<script lang="ts">
	import { DropdownMenu } from 'bits-ui';
	import { marked } from 'marked';
	import type Fuse from 'fuse.js';

	import dayjs from '$lib/dayjs';
	import relativeTime from 'dayjs/plugin/relativeTime';
	dayjs.extend(relativeTime);

	import Spinner from '$lib/components/common/Spinner.svelte';
	import { flyAndScale } from '$lib/utils/transitions';
	import { onMount, getContext, tick } from 'svelte';
	import { goto } from '$app/navigation';

	import { deleteModel, getOllamaVersion, pullModel, unloadModel } from '$lib/apis/ollama';

	import {
		user,
		MODEL_DOWNLOAD_POOL,
		models,
		mobile,
		temporaryChatEnabled,
		settings,
		config,
		modelsLoaded
	} from '$lib/stores';
	import { toast } from '$lib/utils/toast';
	import { capitalizeFirstLetter, sanitizeResponseContent, splitStream } from '$lib/utils';
	import { noAutoKeyboardFocus } from '$lib/utils/menuFocus';
	import { getModels } from '$lib/apis';

	import ChevronDown from '$lib/components/icons/ChevronDown.svelte';
	import Check from '$lib/components/icons/Check.svelte';
	import Search from '$lib/components/icons/Search.svelte';
	import Tooltip from '$lib/components/common/Tooltip.svelte';
	import Switch from '$lib/components/common/Switch.svelte';
	import ChatBubbleOval from '$lib/components/icons/ChatBubbleOval.svelte';

	import ModelItem from './ModelItem.svelte';

	const i18n = getContext('i18n');
	interface Props {
		id?: string;
		value?: string;
		placeholder?: any;
		searchEnabled?: boolean;
		searchPlaceholder?: any;
		items?: {
			label: string;
			value: string;
			model: Model;
			// eslint-disable-next-line @typescript-eslint/no-explicit-any
			[key: string]: any;
		}[];
		className?: string;
		triggerClassName?: string;
		pinModelHandler?: (modelId: string) => void;
		onSelect?: (modelId: string) => void;
		children?: import('svelte').Snippet;
	}

	let {
		id = '',
		value = '',
		placeholder = $i18n.t('Select a model'),
		searchEnabled = true,
		searchPlaceholder = $i18n.t('Search a model'),
		items = [],
		className = 'w-[32rem]',
		triggerClassName = 'text-lg',
		pinModelHandler = () => {},
		onSelect = () => {},
		children
	}: Props = $props();

	let tagsContainerElement = $state();

	let show = $state(false);
	let tags = $state([]);

	let selectedModel = $state('');

	let searchValue = $state('');

	let selectedTag = $state('');
	let selectedConnectionType = $state('');

	let ollamaVersion = $state(null);
	let selectedModelIdx = $state(0);

	let fuse: Fuse<any> | null = $state(null);

	const selectModel = (modelId: string) => {
		onSelect(modelId);
		show = false;
	};

	// fuse.js is lazy-loaded (kept off the cold-load path) when the selector
	// mounts. updateFuse and the filteredItems search both guard on `fuse`, so the
	// list is simply unfiltered for the one tick before it loads — which happens
	// when the dropdown mounts, before the user can type a search.
	const loadFuse = async () => {
		const { default: Fuse } = await import('fuse.js');
		fuse = new Fuse(
			items.map((item) => {
				const _item = {
					...item,
					modelName: item.model?.name,
					tags: (item.model?.tags ?? []).map((tag) => tag.name).join(' '),
					desc: item.model?.info?.meta?.description
				};
				return _item;
			}),
			{
				keys: ['value', 'tags', 'modelName'],
				threshold: 0.4
			}
		);
	};

	const updateFuse = () => {
		if (fuse) {
			fuse.setCollection(
				items.map((item) => {
					const _item = {
						...item,
						modelName: item.model?.name,
						tags: (item.model?.tags ?? []).map((tag) => tag.name).join(' '),
						desc: item.model?.info?.meta?.description
					};
					return _item;
				})
			);
		}
	};

	const resetView = async () => {
		await tick();

		const selectedInFiltered = filteredItems.findIndex((item) => item.value === value);

		if (selectedInFiltered >= 0) {
			// The selected model is visible in the current filter
			selectedModelIdx = selectedInFiltered;
		} else {
			// The selected model is not visible, default to first item in filtered list
			selectedModelIdx = 0;
		}

		await tick();
		const item = document.querySelector(`[data-arrow-selected="true"]`);
		item?.scrollIntoView({ block: 'center', inline: 'nearest', behavior: 'instant' });
	};

	const pullModelHandler = async () => {
		const sanitizedModelTag = searchValue.trim().replace(/^ollama\s+(run|pull)\s+/, '');

		console.log($MODEL_DOWNLOAD_POOL);
		if ($MODEL_DOWNLOAD_POOL[sanitizedModelTag]) {
			toast.error(
				$i18n.t(`Model '{{modelTag}}' is already in queue for downloading.`, {
					modelTag: sanitizedModelTag
				})
			);
			return;
		}
		if (Object.keys($MODEL_DOWNLOAD_POOL).length === 3) {
			toast.error(
				$i18n.t('Maximum of 3 models can be downloaded simultaneously. Please try again later.')
			);
			return;
		}

		const [res, controller] = await pullModel(localStorage.token, sanitizedModelTag, '0').catch(
			(error) => {
				toast.error(`${error}`);
				return null;
			}
		);

		if (res) {
			const reader = res.body
				.pipeThrough(new TextDecoderStream())
				.pipeThrough(splitStream('\n'))
				.getReader();

			MODEL_DOWNLOAD_POOL.set({
				...$MODEL_DOWNLOAD_POOL,
				[sanitizedModelTag]: {
					...$MODEL_DOWNLOAD_POOL[sanitizedModelTag],
					abortController: controller,
					reader,
					done: false
				}
			});

			while (true) {
				try {
					const { value, done } = await reader.read();
					if (done) break;

					let lines = value.split('\n');

					for (const line of lines) {
						if (line !== '') {
							let data = JSON.parse(line);
							console.log(data);
							if (data.error) {
								throw data.error;
							}
							if (data.detail) {
								throw data.detail;
							}

							if (data.status) {
								if (data.digest) {
									let downloadProgress = 0;
									if (data.completed) {
										downloadProgress = Math.round((data.completed / data.total) * 1000) / 10;
									} else {
										downloadProgress = 100;
									}

									MODEL_DOWNLOAD_POOL.set({
										...$MODEL_DOWNLOAD_POOL,
										[sanitizedModelTag]: {
											...$MODEL_DOWNLOAD_POOL[sanitizedModelTag],
											pullProgress: downloadProgress,
											digest: data.digest
										}
									});
								} else {
									toast.success(data.status);

									MODEL_DOWNLOAD_POOL.set({
										...$MODEL_DOWNLOAD_POOL,
										[sanitizedModelTag]: {
											...$MODEL_DOWNLOAD_POOL[sanitizedModelTag],
											done: data.status === 'success'
										}
									});
								}
							}
						}
					}
				} catch (error) {
					console.log(error);
					if (typeof error !== 'string') {
						error = error.message;
					}

					toast.error(`${error}`);
					// opts.callback({ success: false, error, modelName: opts.modelName });
					break;
				}
			}

			if ($MODEL_DOWNLOAD_POOL[sanitizedModelTag].done) {
				toast.success(
					$i18n.t(`Model '{{modelName}}' has been successfully downloaded.`, {
						modelName: sanitizedModelTag
					})
				);

				models.set(
					await getModels(
						localStorage.token,
						$config?.features?.enable_direct_connections && ($settings?.directConnections ?? null)
					)
				);
			} else {
				toast.error($i18n.t('Download canceled'));
			}

			delete $MODEL_DOWNLOAD_POOL[sanitizedModelTag];

			MODEL_DOWNLOAD_POOL.set({
				...$MODEL_DOWNLOAD_POOL
			});
		}
	};

	const setOllamaVersion = async () => {
		ollamaVersion = await getOllamaVersion(localStorage.token).catch((error) => false);
	};

	onMount(async () => {
		loadFuse();
		if (items) {
			tags = items
				.filter((item) => !(item.model?.info?.meta?.hidden ?? false))
				.flatMap((item) => item.model?.tags ?? [])
				.map((tag) => tag.name);

			// Remove duplicates and sort
			tags = Array.from(new Set(tags)).sort((a, b) => a.localeCompare(b));
		}
	});

	const cancelModelPullHandler = async (model: string) => {
		const { reader, abortController } = $MODEL_DOWNLOAD_POOL[model];
		if (abortController) {
			abortController.abort();
		}
		if (reader) {
			await reader.cancel();
			delete $MODEL_DOWNLOAD_POOL[model];
			MODEL_DOWNLOAD_POOL.set({
				...$MODEL_DOWNLOAD_POOL
			});
			await deleteModel(localStorage.token, model);
			toast.success($i18n.t('{{model}} download has been canceled', { model: model }));
		}
	};

	const unloadModelHandler = async (model: string) => {
		const res = await unloadModel(localStorage.token, model).catch((error) => {
			toast.error($i18n.t('Error unloading model: {{error}}', { error }));
		});

		if (res) {
			toast.success($i18n.t('Model unloaded successfully'));
			models.set(
				await getModels(
					localStorage.token,
					$config?.features?.enable_direct_connections && ($settings?.directConnections ?? null)
				)
			);
		}
	};
	$effect(() => {
		selectedModel = items.find((item) => item.value === value) ?? '';
	});
	$effect(() => {
		if (items) {
			updateFuse();
		}
	});
	let filteredItems = $derived(
		(searchValue && fuse
			? fuse
					.search(searchValue)
					.map((e) => {
						return e.item;
					})
					.filter((item) => {
						if (selectedTag === '') {
							return true;
						}
						return (item.model?.tags ?? []).map((tag) => tag.name).includes(selectedTag);
					})
					.filter((item) => {
						if (selectedConnectionType === '') {
							return true;
						} else if (selectedConnectionType === 'local') {
							return item.model?.connection_type === 'local';
						} else if (selectedConnectionType === 'external') {
							return item.model?.connection_type === 'external';
						} else if (selectedConnectionType === 'direct') {
							return item.model?.direct;
						}
					})
			: items
					.filter((item) => {
						if (selectedTag === '') {
							return true;
						}
						return (item.model?.tags ?? []).map((tag) => tag.name).includes(selectedTag);
					})
					.filter((item) => {
						if (selectedConnectionType === '') {
							return true;
						} else if (selectedConnectionType === 'local') {
							return item.model?.connection_type === 'local';
						} else if (selectedConnectionType === 'external') {
							return item.model?.connection_type === 'external';
						} else if (selectedConnectionType === 'direct') {
							return item.model?.direct;
						}
					})
		).filter((item) => !(item.model?.info?.meta?.hidden ?? false))
	);
	$effect(() => {
		if (selectedTag || selectedConnectionType) {
			resetView();
		} else {
			resetView();
		}
	});
	// Probe the Ollama version once per mount (gates the admin "pull model" row),
	// not on every open — it can't change mid-session and the probe 404s
	// pointlessly when Ollama isn't configured.
	$effect(() => {
		if (show && ollamaVersion === null) {
			ollamaVersion = false;
			setOllamaVersion();
		}
	});
</script>

<DropdownMenu.Root
	bind:open={show}
	onOpenChange={async (open) => {
		if (open && Date.now() - lastModelsRefreshAt > MODELS_REFRESH_TTL_MS) {
			lastModelsRefreshAt = Date.now();
			// Background refresh — the picker renders instantly from $models
			// (seeded by bootstrap, kept fresh elsewhere); don't block open on RTT.
			getModels(
				localStorage.token,
				$config?.features?.enable_direct_connections && ($settings?.directConnections ?? null)
			)
				.then((freshModels) => {
					if (freshModels?.length) {
						models.set(freshModels);
					}
				})
				.catch(() => {});
		}
		searchValue = '';
		if (!$mobile) {
			window.setTimeout(() => document.getElementById('model-search-input')?.focus(), 0);
		}

		resetView();
	}}
	closeFocus={false}
>
	<DropdownMenu.Trigger
		class="relative w-full {($settings?.highContrastMode ?? false)
			? ''
			: 'outline-hidden focus:outline-hidden'}"
		aria-label={placeholder}
		id="model-selector-{id}-button"
	>
		<div
			class="flex w-full text-left px-0.5 bg-transparent truncate {triggerClassName} justify-between {($settings?.highContrastMode ??
			false)
				? 'dark:placeholder-gray-100 placeholder-gray-800'
				: 'placeholder-gray-400'}"
		>
			{#if selectedModel}
				{selectedModel.label}
			{:else if !$modelsLoaded}
				{$i18n.t('Loading...')}
			{:else if value}
				{value}
			{:else}
				{placeholder}
			{/if}
			<ChevronDown className=" self-center ml-2 size-3" strokeWidth="2.5" />
		</div>
	</DropdownMenu.Trigger>

	<DropdownMenu.Content
		class=" z-40 {$mobile
			? `w-full`
			: `${className}`} max-w-[calc(100vw-1rem)] justify-start rounded-2xl  bg-white dark:bg-gray-850 dark:text-white shadow-lg  outline-hidden"
		transition={flyAndScale}
		side={$mobile ? 'bottom' : 'bottom-start'}
		sideOffset={2}
		alignOffset={-1}
	>
		{#if children}{@render children()}{:else}
			{#if searchEnabled}
				<div class="flex items-center gap-2.5 px-5.5 mt-3.5 mb-1.5">
					<Search className="size-4" strokeWidth="2.5" />

					<input
						id="model-search-input"
						bind:value={searchValue}
						use:noAutoKeyboardFocus
						class="w-full text-sm bg-transparent outline-hidden"
						placeholder={searchPlaceholder}
						autocomplete="off"
						aria-label={$i18n.t('Search In Models')}
						onkeydown={(e) => {
							if (e.code === 'Enter' && filteredItems.length > 0) {
								selectModel(filteredItems[selectedModelIdx].value);
								return; // dont need to scroll on selection
							} else if (e.code === 'ArrowDown') {
								e.stopPropagation();
								selectedModelIdx = Math.min(selectedModelIdx + 1, filteredItems.length - 1);
							} else if (e.code === 'ArrowUp') {
								e.stopPropagation();
								selectedModelIdx = Math.max(selectedModelIdx - 1, 0);
							} else {
								// if the user types something, reset to the top selection.
								selectedModelIdx = 0;
							}

							const item = document.querySelector(`[data-arrow-selected="true"]`);
							item?.scrollIntoView({ block: 'center', inline: 'nearest', behavior: 'instant' });
						}}
					/>
				</div>
			{/if}

			<div class="px-4">
				{#if tags && items.filter((item) => !(item.model?.info?.meta?.hidden ?? false)).length > 0}
					<div
						class=" flex w-full bg-white dark:bg-gray-850 overflow-x-auto scrollbar-none font-medium mb-0.5"
						onwheel={(e) => {
							if (e.deltaY !== 0) {
								e.preventDefault();
								e.currentTarget.scrollLeft += e.deltaY;
							}
						}}
					>
						<div
							class="flex gap-1 w-fit text-center text-sm rounded-full bg-transparent px-1.5 whitespace-nowrap"
							bind:this={tagsContainerElement}
						>
							{#if items.find((item) => item.model?.connection_type === 'local') || items.find((item) => item.model?.connection_type === 'external') || items.find((item) => item.model?.direct) || tags.length > 0}
								<button
									class="min-w-fit outline-none px-1.5 py-0.5 max-md:py-2 max-md:px-2.5 {selectedTag ===
										'' && selectedConnectionType === ''
										? ''
										: 'text-gray-300 dark:text-gray-600 hover:text-gray-700 dark:hover:text-white'} transition capitalize"
									aria-pressed={selectedTag === '' && selectedConnectionType === ''}
									onclick={() => {
										selectedConnectionType = '';
										selectedTag = '';
									}}
								>
									{$i18n.t('All')}
								</button>
							{/if}

							{#if items.find((item) => item.model?.connection_type === 'local')}
								<button
									class="min-w-fit outline-none px-1.5 py-0.5 max-md:py-2 max-md:px-2.5 {selectedConnectionType ===
									'local'
										? ''
										: 'text-gray-300 dark:text-gray-600 hover:text-gray-700 dark:hover:text-white'} transition capitalize"
									aria-pressed={selectedConnectionType === 'local'}
									onclick={() => {
										selectedTag = '';
										selectedConnectionType = 'local';
									}}
								>
									{$i18n.t('Local')}
								</button>
							{/if}

							{#if items.find((item) => item.model?.connection_type === 'external')}
								<button
									class="min-w-fit outline-none px-1.5 py-0.5 max-md:py-2 max-md:px-2.5 {selectedConnectionType ===
									'external'
										? ''
										: 'text-gray-300 dark:text-gray-600 hover:text-gray-700 dark:hover:text-white'} transition capitalize"
									aria-pressed={selectedConnectionType === 'external'}
									onclick={() => {
										selectedTag = '';
										selectedConnectionType = 'external';
									}}
								>
									{$i18n.t('External')}
								</button>
							{/if}

							{#if items.find((item) => item.model?.direct)}
								<button
									class="min-w-fit outline-none px-1.5 py-0.5 max-md:py-2 max-md:px-2.5 {selectedConnectionType ===
									'direct'
										? ''
										: 'text-gray-300 dark:text-gray-600 hover:text-gray-700 dark:hover:text-white'} transition capitalize"
									aria-pressed={selectedConnectionType === 'direct'}
									onclick={() => {
										selectedTag = '';
										selectedConnectionType = 'direct';
									}}
								>
									{$i18n.t('Direct')}
								</button>
							{/if}

							{#each tags as tag}
								<Tooltip content={tag} touch={!$mobile}>
									<button
										class="min-w-fit outline-none px-1.5 py-0.5 max-md:py-2 max-md:px-2.5 {selectedTag ===
										tag
											? ''
											: 'text-gray-300 dark:text-gray-600 hover:text-gray-700 dark:hover:text-white'} transition capitalize"
										aria-pressed={selectedTag === tag}
										onclick={() => {
											selectedConnectionType = '';
											selectedTag = tag;
										}}
									>
										{tag.length > 16 ? `${tag.slice(0, 16)}...` : tag}
									</button>
								</Tooltip>
							{/each}
						</div>
					</div>
				{/if}
			</div>

			<div class="px-2.5 model-picker-list overflow-y-auto group relative">
				{#each filteredItems as item, index}
					<ModelItem
						{selectedModelIdx}
						{item}
						{index}
						{value}
						{pinModelHandler}
						{unloadModelHandler}
						onClick={() => {
							selectedModelIdx = index;
							selectModel(item.value);
						}}
					/>
				{:else}
					<div class="">
						<div class="block px-3 py-2 text-sm text-gray-700 dark:text-gray-100">
							{$i18n.t('No results found')}
						</div>
					</div>
				{/each}

				{#if !(searchValue.trim() in $MODEL_DOWNLOAD_POOL) && searchValue && ollamaVersion && $user?.role === 'admin'}
					<Tooltip
						content={$i18n.t(`Pull "{{searchValue}}" from Ollama.com`, {
							searchValue: searchValue
						})}
						placement="top-start"
						touch={!$mobile}
					>
						<button
							class="flex w-full font-medium line-clamp-1 select-none items-center rounded-button py-2 pl-3 pr-1.5 text-sm text-gray-700 dark:text-gray-100 outline-hidden transition-all duration-75 hover:bg-gray-100 dark:hover:bg-gray-800 rounded-xl cursor-pointer data-highlighted:bg-muted"
							onclick={() => {
								pullModelHandler();
							}}
						>
							<div class=" truncate">
								{$i18n.t(`Pull "{{searchValue}}" from Ollama.com`, { searchValue: searchValue })}
							</div>
						</button>
					</Tooltip>
				{/if}

				{#each Object.keys($MODEL_DOWNLOAD_POOL) as model}
					<div
						class="flex w-full justify-between font-medium select-none rounded-button py-2 pl-3 pr-1.5 text-sm text-gray-700 dark:text-gray-100 outline-hidden transition-all duration-75 rounded-xl cursor-pointer data-highlighted:bg-muted"
					>
						<div class="flex">
							<div class="mr-2.5 translate-y-0.5">
								<Spinner />
							</div>

							<div class="flex flex-col self-start">
								<div class="flex gap-1">
									<div class="line-clamp-1">
										Downloading "{model}"
									</div>

									<div class="shrink-0">
										{'pullProgress' in $MODEL_DOWNLOAD_POOL[model]
											? `(${$MODEL_DOWNLOAD_POOL[model].pullProgress}%)`
											: ''}
									</div>
								</div>

								{#if 'digest' in $MODEL_DOWNLOAD_POOL[model] && $MODEL_DOWNLOAD_POOL[model].digest}
									<div class="-mt-1 h-fit text-xs text-gray-500 dark:text-gray-500 line-clamp-1">
										{$MODEL_DOWNLOAD_POOL[model].digest}
									</div>
								{/if}
							</div>
						</div>

						<div class="mr-2 ml-1 translate-y-0.5">
							<Tooltip content={$i18n.t('Cancel')} touch={!$mobile}>
								<button
									class="text-gray-800 dark:text-gray-100"
									onclick={() => {
										cancelModelPullHandler(model);
									}}
								>
									<svg
										class="w-4 h-4 text-gray-800 dark:text-white"
										aria-hidden="true"
										xmlns="http://www.w3.org/2000/svg"
										width="24"
										height="24"
										fill="currentColor"
										viewBox="0 0 24 24"
									>
										<path
											stroke="currentColor"
											stroke-linecap="round"
											stroke-linejoin="round"
											stroke-width="2"
											d="M6 18 17.94 6M18 18 6.06 6"
										/>
									</svg>
								</button>
							</Tooltip>
						</div>
					</div>
				{/each}
			</div>

			<div class="mb-2.5"></div>

			<div class="hidden w-[42rem]"></div>
			<div class="hidden w-[32rem]"></div>
		{/if}
	</DropdownMenu.Content>
</DropdownMenu.Root>
