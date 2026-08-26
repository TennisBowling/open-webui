<script lang="ts">
	import { getContext, onMount } from 'svelte';
	import { toast } from '$lib/utils/toast';

	import { settings } from '$lib/stores';
	import Modal from '$lib/components/common/Modal.svelte';
	import Switch from '$lib/components/common/Switch.svelte';
	import XMark from '$lib/components/icons/XMark.svelte';
	import Spinner from '$lib/components/common/Spinner.svelte';
	import { getVideoConfig, type VideoConfig } from '$lib/apis/videos';

	const i18n = getContext('i18n');

	interface Props {
		show?: boolean;
		onSubmit: (spec: {
			sourceType: 'url' | 'upload';
			url?: string;
			file?: File;
			fps: number;
			quality: string;
			start: number | null;
			end: number | null;
			audio: boolean;
		}) => void;
	}

	let { show = $bindable(false), onSubmit }: Props = $props();

	let cfg = $state<VideoConfig | null>(null);
	let loading = $state(false);

	let url = $state('');
	let pickedFile = $state<File | null>(null);
	let fileInputEl = $state<HTMLInputElement | null>(null);

	let fps = $state(1);
	let quality = $state('720p');
	let audio = $state(true);
	let startText = $state('');
	let endText = $state('');

	const urlIngestEnabled = $derived(cfg?.url_ingest_enabled ?? true);

	/**
	 * Accepts `90`, `1:30`, or `1:02:03`. People copy timestamps out of a video
	 * player far more often than they count raw seconds, so refusing the colon
	 * form would make the common case the awkward one.
	 */
	const parseTime = (raw: string): number | null => {
		const text = (raw ?? '').trim();
		if (!text) return null;
		if (!/^[0-9:.]+$/.test(text)) return NaN;
		const parts = text.split(':');
		if (parts.length > 3) return NaN;
		let seconds = 0;
		for (const part of parts) {
			if (part === '') return NaN;
			const value = Number(part);
			if (!Number.isFinite(value) || value < 0) return NaN;
			seconds = seconds * 60 + value;
		}
		return seconds;
	};

	const startSeconds = $derived(parseTime(startText));
	const endSeconds = $derived(parseTime(endText));
	const startInvalid = $derived(Number.isNaN(startSeconds as number));
	const endInvalid = $derived(Number.isNaN(endSeconds as number));
	const rangeInverted = $derived(
		startSeconds !== null &&
			endSeconds !== null &&
			!startInvalid &&
			!endInvalid &&
			(endSeconds as number) <= (startSeconds as number)
	);

	// Only a trimmed span has a knowable length before the backend probes the
	// source, so the estimate stays quiet for full-length clips rather than
	// inventing a duration.
	const trimmedSpan = $derived(
		startSeconds !== null && endSeconds !== null && !rangeInverted && !startInvalid && !endInvalid
			? (endSeconds as number) - (startSeconds as number)
			: null
	);

	// Mirrors the server-side constants measured against Gemini via OpenRouter
	// (66 tokens/frame, ~25 tokens/sec of audio).
	const estimate = $derived.by(() => {
		if (trimmedSpan === null || trimmedSpan <= 0) return null;
		const frames = Math.max(Math.round(trimmedSpan * fps), 1);
		const tokens = frames * 66 + (audio ? Math.round(trimmedSpan * 25) : 0);
		return { frames, tokens };
	});

	const warnSeconds = $derived(cfg?.warn_duration_seconds ?? 600);
	const overWarn = $derived(trimmedSpan !== null && trimmedSpan > warnSeconds);

	const formatTokens = (n: number) =>
		n >= 1000 ? `${(n / 1000).toFixed(n >= 10000 ? 0 : 1)}k` : `${n}`;

	const formatDuration = (s: number) => {
		const total = Math.round(s);
		const h = Math.floor(total / 3600);
		const m = Math.floor((total % 3600) / 60);
		const sec = total % 60;
		return h > 0
			? `${h}:${String(m).padStart(2, '0')}:${String(sec).padStart(2, '0')}`
			: `${m}:${String(sec).padStart(2, '0')}`;
	};

	const loadConfig = async () => {
		loading = true;
		try {
			cfg = await getVideoConfig(localStorage.token);
			fps = cfg.default_fps ?? 1;
			quality = cfg.default_quality ?? '720p';
			audio = cfg.default_audio ?? true;
		} catch (e) {
			// Non-fatal: the built-in defaults below already match the shipped
			// server defaults, so the dialog still works without /config.
			console.warn('video config load failed', e);
		} finally {
			loading = false;
		}
	};

	$effect(() => {
		if (show && !cfg && !loading) {
			void loadConfig();
		}
	});

	const reset = () => {
		url = '';
		pickedFile = null;
		startText = '';
		endText = '';
		if (fileInputEl) fileInputEl.value = '';
	};

	const handleFileChange = (event: Event) => {
		const input = event.target as HTMLInputElement;
		const file = input.files?.[0];
		if (file) {
			pickedFile = file;
			url = '';
		}
	};

	const submitHandler = () => {
		if (startInvalid || endInvalid) {
			toast.error($i18n.t('Enter times as seconds or mm:ss.'));
			return;
		}
		if (rangeInverted) {
			toast.error($i18n.t('End time must be after the start time.'));
			return;
		}

		if (pickedFile) {
			onSubmit({
				sourceType: 'upload',
				file: pickedFile,
				fps,
				quality,
				start: startSeconds,
				end: endSeconds,
				audio
			});
		} else {
			const trimmed = url.trim();
			if (!trimmed) {
				toast.error($i18n.t('Paste a video link or choose a file.'));
				return;
			}
			onSubmit({
				sourceType: 'url',
				url: trimmed,
				fps,
				quality,
				start: startSeconds,
				end: endSeconds,
				audio
			});
		}

		show = false;
		reset();
	};

	const highContrast = $derived($settings?.highContrastMode ?? false);
	const inputClass =
		'w-full rounded-lg py-1.5 px-3 text-sm bg-gray-50 dark:bg-gray-900 dark:text-gray-100 outline-hidden border-hairline border-gray-200 dark:border-gray-700';
</script>

<Modal bind:show size="sm">
	<div class="flex flex-col h-full">
		<div class="flex justify-between items-center dark:text-gray-100 px-5 pt-4 pb-1.5">
			<h1 class="text-lg font-medium self-center font-primary">
				{$i18n.t('Add Video')}
			</h1>
			<button
				class="self-center"
				aria-label={$i18n.t('Close modal')}
				onclick={() => {
					show = false;
					reset();
				}}
			>
				<XMark className="size-5" />
			</button>
		</div>

		<div class="px-5 pb-4">
			<form
				onsubmit={(e) => {
					e.preventDefault();
					submitHandler();
				}}
			>
				{#if urlIngestEnabled}
					<div class="mb-1">
						<label
							for="video-url"
							class={`text-xs ${highContrast ? 'text-gray-800 dark:text-gray-100' : 'text-gray-500'}`}
							>{$i18n.t('Video link')}</label
						>
					</div>
					<input
						id="video-url"
						class={inputClass}
						type="text"
						bind:value={url}
						oninput={() => {
							if (url) pickedFile = null;
						}}
						placeholder={'https://x.com/… · youtube.com/… · tiktok.com/…'}
						autocomplete="off"
						disabled={!!pickedFile}
					/>
					<p class="text-[10px] italic text-gray-500 dark:text-gray-400 mt-1 px-1">
						{$i18n.t('YouTube, X/Twitter, TikTok and most other video sites.')}
					</p>

					<div class="flex items-center gap-3 my-3">
						<div class="flex-1 h-px bg-gray-100 dark:bg-gray-800"></div>
						<span class="text-[10px] uppercase tracking-wide text-gray-400 dark:text-gray-500"
							>{$i18n.t('or')}</span
						>
						<div class="flex-1 h-px bg-gray-100 dark:bg-gray-800"></div>
					</div>
				{/if}

				<input
					bind:this={fileInputEl}
					id="video-file-input"
					type="file"
					accept="video/*,.mp4,.mkv,.mov,.webm,.avi,.m4v,.mpeg,.mpg,.wmv,.flv"
					onchange={handleFileChange}
					class="hidden"
				/>

				{#if pickedFile}
					<div
						class="flex items-center justify-between gap-2 rounded-lg px-3 py-2 bg-gray-50 dark:bg-gray-900 border-hairline border-gray-200 dark:border-gray-700"
					>
						<div class="min-w-0">
							<div class="text-sm truncate dark:text-gray-100">{pickedFile.name}</div>
							<div class="text-[10px] text-gray-500 dark:text-gray-400">
								{(pickedFile.size / 1048576).toFixed(1)} MB
							</div>
						</div>
						<button
							type="button"
							class="text-xs text-gray-500 hover:text-gray-800 dark:hover:text-gray-200 shrink-0"
							onclick={() => {
								pickedFile = null;
								if (fileInputEl) fileInputEl.value = '';
							}}
						>
							{$i18n.t('Remove')}
						</button>
					</div>
				{:else}
					<button
						type="button"
						class="w-full rounded-lg py-2 text-sm bg-gray-50 dark:bg-gray-900 hover:bg-gray-100 dark:hover:bg-gray-800 border-hairline border-gray-200 dark:border-gray-700 dark:text-gray-100 transition"
						onclick={() => fileInputEl?.click()}
					>
						{$i18n.t('Choose a video file')}
					</button>
				{/if}

				<div class="grid grid-cols-2 gap-3 mt-4">
					<div>
						<label
							for="video-start"
							class="block text-xs font-medium text-gray-700 dark:text-gray-300 mb-1 px-1"
							>{$i18n.t('Start')}</label
						>
						<input
							id="video-start"
							class={inputClass}
							class:!border-red-400={startInvalid}
							type="text"
							bind:value={startText}
							placeholder={$i18n.t('start')}
							autocomplete="off"
						/>
					</div>
					<div>
						<label
							for="video-end"
							class="block text-xs font-medium text-gray-700 dark:text-gray-300 mb-1 px-1"
							>{$i18n.t('End')}</label
						>
						<input
							id="video-end"
							class={inputClass}
							class:!border-red-400={endInvalid || rangeInverted}
							type="text"
							bind:value={endText}
							placeholder={$i18n.t('end')}
							autocomplete="off"
						/>
					</div>
				</div>
				<p class="text-[10px] italic text-gray-500 dark:text-gray-400 mt-1 px-1">
					{$i18n.t('Seconds or mm:ss. Leave empty for the whole video.')}
				</p>

				<div class="grid grid-cols-2 gap-3 mt-3">
					<div>
						<label
							for="video-fps"
							class="block text-xs font-medium text-gray-700 dark:text-gray-300 mb-1 px-1"
							>{$i18n.t('Frame rate')}</label
						>
						<select id="video-fps" bind:value={fps} class={inputClass}>
							<option value={0.2}>{$i18n.t('0.2 fps (1 every 5s)')}</option>
							<option value={0.5}>{$i18n.t('0.5 fps (1 every 2s)')}</option>
							<option value={1}>{$i18n.t('1 fps (recommended)')}</option>
							<option value={2}>{$i18n.t('2 fps')}</option>
							<option value={4}>{$i18n.t('4 fps')}</option>
						</select>
					</div>
					<div>
						<label
							for="video-quality"
							class="block text-xs font-medium text-gray-700 dark:text-gray-300 mb-1 px-1"
							>{$i18n.t('Quality')}</label
						>
						<select id="video-quality" bind:value={quality} class={inputClass}>
							{#each cfg?.qualities ?? ['360p', '480p', '720p', '1080p', 'source'] as q}
								<option value={q}>{q === 'source' ? $i18n.t('Original') : q}</option>
							{/each}
						</select>
					</div>
				</div>

				<div class="flex items-start justify-between gap-3 mt-3 px-1">
					<div class="min-w-0">
						<div class="text-xs font-medium text-gray-700 dark:text-gray-300">
							{$i18n.t('Include audio')}
						</div>
						<p class="text-[10px] italic text-gray-500 dark:text-gray-400 mt-0.5">
							{$i18n.t('The model can hear speech and narration. Costs extra tokens.')}
						</p>
					</div>
					<div class="shrink-0 pt-0.5">
						<Switch bind:state={audio} />
					</div>
				</div>

				{#if estimate}
					<div
						class="mt-3 rounded-lg px-3 py-2 text-[11px] {overWarn
							? 'bg-amber-50 dark:bg-amber-900/20 text-amber-800 dark:text-amber-200'
							: 'bg-gray-50 dark:bg-gray-900 text-gray-600 dark:text-gray-300'}"
					>
						{$i18n.t('~{{frames}} frames · ~{{tokens}} tokens for {{span}}', {
							frames: estimate.frames,
							tokens: formatTokens(estimate.tokens),
							span: formatDuration(trimmedSpan ?? 0)
						})}
						{#if overWarn}
							<div class="mt-1 font-medium">
								{$i18n.t('That is a long clip — it will use a lot of context.')}
							</div>
						{/if}
					</div>
				{/if}

				<div class="flex justify-end gap-2 pt-4">
					<button
						class="px-3.5 py-1.5 text-sm font-medium bg-book-cloth hover:bg-kraft text-white transition-colors duration-200 ease-paper rounded-full disabled:opacity-50"
						type="submit"
						disabled={loading || (!pickedFile && !url.trim())}
					>
						{#if loading}
							<Spinner className="size-3.5" />
						{:else}
							{$i18n.t('Add')}
						{/if}
					</button>
				</div>
			</form>
		</div>
	</div>
</Modal>
