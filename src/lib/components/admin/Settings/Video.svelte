<script lang="ts">
	import { preventDefault } from '$lib/utils/eventModifiers';

	import { getContext, onMount } from 'svelte';
	import { toast } from '$lib/utils/toast';

	import Switch from '$lib/components/common/Switch.svelte';
	import Tooltip from '$lib/components/common/Tooltip.svelte';
	import { getVideoConfig, updateVideoConfig } from '$lib/apis/videos';

	const i18n = getContext('i18n');

	interface Props {
		saveHandler: Function;
	}

	let { saveHandler }: Props = $props();

	let enabled = $state(true);
	let urlIngestEnabled = $state(true);
	let defaultFps = $state(1);
	let defaultQuality = $state('720p');
	let defaultAudio = $state(true);
	let maxSourceSizeMb = $state(2048);
	let warnDurationSeconds = $state(600);
	let qualities = $state<string[]>(['360p', '480p', '720p', '1080p', 'source']);

	const submitHandler = async () => {
		try {
			await updateVideoConfig(localStorage.token, {
				enabled,
				url_ingest_enabled: urlIngestEnabled,
				default_fps: Number(defaultFps),
				default_quality: defaultQuality,
				default_audio: defaultAudio,
				max_source_size_mb: Number(maxSourceSizeMb),
				warn_duration_seconds: Number(warnDurationSeconds)
			});
			toast.success($i18n.t('Settings saved successfully'));
		} catch (e) {
			toast.error(`${e}`);
		}
	};

	onMount(async () => {
		try {
			const res = await getVideoConfig(localStorage.token);
			if (res) {
				enabled = res.enabled;
				urlIngestEnabled = res.url_ingest_enabled;
				defaultFps = res.default_fps;
				defaultQuality = res.default_quality;
				defaultAudio = res.default_audio;
				maxSourceSizeMb = res.max_source_size_mb;
				warnDurationSeconds = res.warn_duration_seconds;
				qualities = res.qualities ?? qualities;
			}
		} catch (e) {
			toast.error(`${e}`);
		}
	});

	const inputClass =
		'w-full rounded-lg py-1.5 px-3 text-sm bg-gray-50 dark:bg-gray-900 dark:text-gray-100 outline-hidden border-hairline border-gray-200 dark:border-gray-700';
</script>

<form
	class="flex flex-col h-full justify-between space-y-3 text-sm"
	onsubmit={preventDefault(async () => {
		await submitHandler();
		saveHandler();
	})}
>
	<div class=" space-y-3 overflow-y-scroll scrollbar-hidden h-full">
		<div class="mb-3">
			<div class=" mb-2.5 text-base font-medium">{$i18n.t('Video')}</div>

			<hr class=" border-gray-100 dark:border-gray-850 my-2" />

			<div class="mb-2.5 flex w-full justify-between">
				<div class=" self-center text-xs font-medium">
					{$i18n.t('Enable Video Input')}
				</div>
				<div class="flex items-center relative">
					<Switch bind:state={enabled} />
				</div>
			</div>

			<div class="mb-2.5 flex w-full justify-between">
				<div class=" self-center text-xs font-medium">
					<Tooltip
						content={$i18n.t(
							'Allow downloading videos from pasted links (YouTube, X/Twitter, TikTok and others). Turn off to allow uploads only.'
						)}
					>
						{$i18n.t('Allow Video Links')}
					</Tooltip>
				</div>
				<div class="flex items-center relative">
					<Switch bind:state={urlIngestEnabled} />
				</div>
			</div>

			<div class=" mb-2.5 text-sm font-medium">{$i18n.t('Defaults')}</div>
			<p class="text-xs text-gray-500 dark:text-gray-400 mb-2.5">
				{$i18n.t(
					'Pre-filled in the video dialog. Users can override these per video; the built-in view_video tool always uses 1 fps at 720p.'
				)}
			</p>

			<div class="grid grid-cols-2 gap-3 mb-2.5">
				<div>
					<div class="mb-1 text-xs font-medium">{$i18n.t('Frame rate')}</div>
					<select bind:value={defaultFps} class={inputClass}>
						<option value={0.2}>{$i18n.t('0.2 fps (1 every 5s)')}</option>
						<option value={0.5}>{$i18n.t('0.5 fps (1 every 2s)')}</option>
						<option value={1}>{$i18n.t('1 fps (recommended)')}</option>
						<option value={2}>{$i18n.t('2 fps')}</option>
						<option value={4}>{$i18n.t('4 fps')}</option>
					</select>
				</div>
				<div>
					<div class="mb-1 text-xs font-medium">{$i18n.t('Quality')}</div>
					<select bind:value={defaultQuality} class={inputClass}>
						{#each qualities as q}
							<option value={q}>{q === 'source' ? $i18n.t('Original') : q}</option>
						{/each}
					</select>
				</div>
			</div>

			<div class="mb-2.5 flex w-full justify-between">
				<div class=" self-center text-xs font-medium">
					<Tooltip
						content={$i18n.t(
							'Keep the audio track so the model can hear speech and narration. Costs roughly 25 extra tokens per second of video.'
						)}
					>
						{$i18n.t('Include Audio By Default')}
					</Tooltip>
				</div>
				<div class="flex items-center relative">
					<Switch bind:state={defaultAudio} />
				</div>
			</div>

			<hr class=" border-gray-100 dark:border-gray-850 my-2" />

			<div class="grid grid-cols-2 gap-3 mb-2.5">
				<div>
					<div class="mb-1 text-xs font-medium">
						<Tooltip
							content={$i18n.t(
								'Limit on the downloaded source file, before processing. Guards server disk and bandwidth.'
							)}
						>
							{$i18n.t('Max Source Size (MB)')}
						</Tooltip>
					</div>
					<input class={inputClass} type="number" min="1" bind:value={maxSourceSizeMb} />
				</div>
				<div>
					<div class="mb-1 text-xs font-medium">
						<Tooltip
							content={$i18n.t(
								'The video dialog warns past this length. It is advisory only and never blocks a send.'
							)}
						>
							{$i18n.t('Warn Above Duration (seconds)')}
						</Tooltip>
					</div>
					<input class={inputClass} type="number" min="0" bind:value={warnDurationSeconds} />
				</div>
			</div>

			<p class="text-xs text-gray-500 dark:text-gray-400">
				{$i18n.t(
					'Video-capable models are detected automatically from the provider’s input modalities. Use the Video capability on a workspace model to force it on or off.'
				)}
			</p>
		</div>
	</div>

	<div class="flex justify-end pt-3 text-sm font-medium">
		<button
			class="px-3.5 py-1.5 text-sm font-medium bg-book-cloth hover:bg-kraft text-white transition-colors duration-200 ease-paper rounded-full"
			type="submit"
		>
			{$i18n.t('Save')}
		</button>
	</div>
</form>
