<script lang="ts">
	import { dispatchComponentEvent } from '$lib/utils/componentEvents';
	import { preventDefault } from '$lib/utils/eventModifiers';

	import { toast } from '$lib/utils/toast';
	import { onDestroy, onMount, getContext } from 'svelte';
	const dispatch = (type: string, detail?: unknown) =>
		dispatchComponentEvent(eventProps, type, detail);

	import { getBackendConfig } from '$lib/apis';
	import {
		getAudioConfig,
		updateAudioConfig,
		previewOpenRouterSpeech,
		getModels as _getModels,
		getVoices as _getVoices,
		type AvailableTTSModel
	} from '$lib/apis/audio';
	import { config } from '$lib/stores';

	import Spinner from '$lib/components/common/Spinner.svelte';
	import SensitiveInput from '$lib/components/common/SensitiveInput.svelte';

	import { TTS_RESPONSE_SPLIT } from '$lib/types';

	import type { Writable } from 'svelte/store';
	import type { i18n as i18nType } from 'i18next';
	import Textarea from '$lib/components/common/Textarea.svelte';

	const i18n = getContext<Writable<i18nType>>('i18n');

	interface Props {
		saveHandler: () => void;
	}

	let { saveHandler, ...eventProps }: Props & Record<string, unknown> = $props();

	// Audio
	let TTS_OPENAI_API_BASE_URL = $state('');
	let TTS_OPENAI_API_KEY = $state('');
	let TTS_OPENROUTER_API_KEY = $state('');
	let TTS_API_KEY = $state('');
	let TTS_ENGINE = $state('');
	let TTS_MODEL = $state('');
	let TTS_VOICE = $state('');
	let TTS_OPENAI_PARAMS = $state('');
	let TTS_SPLIT_ON: TTS_RESPONSE_SPLIT = $state(TTS_RESPONSE_SPLIT.PUNCTUATION);
	let TTS_AZURE_SPEECH_REGION = $state('');
	let TTS_AZURE_SPEECH_BASE_URL = $state('');
	let TTS_AZURE_SPEECH_OUTPUT_FORMAT = $state('');

	let STT_OPENAI_API_BASE_URL = $state('');
	let STT_OPENAI_API_KEY = $state('');
	let STT_OPENROUTER_API_KEY = $state('');
	let STT_OPENROUTER_TEMPERATURE: number | null = $state(null);
	let STT_ENGINE = $state('');
	let STT_MODEL = $state('');
	let STT_SUPPORTED_CONTENT_TYPES = $state('');
	let STT_WHISPER_MODEL = $state('');
	let STT_AZURE_API_KEY = $state('');
	let STT_AZURE_REGION = $state('');
	let STT_AZURE_LOCALES = $state('');
	let STT_AZURE_BASE_URL = $state('');
	let STT_AZURE_MAX_SPEAKERS = $state('');
	let STT_DEEPGRAM_API_KEY = $state('');

	let STT_WHISPER_MODEL_LOADING = $state(false);
	let STT_MODELS_LOADING = $state(false);
	let STT_MODELS_ERROR = $state('');
	let TTS_MODELS_LOADING = $state(false);
	let TTS_MODELS_ERROR = $state('');
	let TTS_PREVIEW_LOADING = $state(false);
	let TTS_PREVIEW_PLAYING = $state(false);
	let TTS_PREVIEW_AUDIO: HTMLAudioElement | null = null;
	let TTS_PREVIEW_URL = '';

	type TTSVoice = {
		id?: string;
		name: string;
		voiceURI?: string;
		localService?: boolean;
	};

	let voices: TTSVoice[] = $state([]);
	let models: AvailableTTSModel[] = $state([]);
	let sttModels: AvailableTTSModel[] = $state([]);
	let selectedOpenRouterModel = $derived(models.find((model) => model.id === TTS_MODEL));
	let selectedOpenRouterSTTModel = $derived(sttModels.find((model) => model.id === STT_MODEL));

	const syncOpenRouterVoices = (preserveCustomVoice = true) => {
		const model = models.find((candidate) => candidate.id === TTS_MODEL);
		voices = (model?.voices ?? []).map((voice) => ({
			id: voice,
			name: formatOpenRouterVoice(voice)
		}));

		if (voices.length > 0 && !voices.some((voice) => voice.id === TTS_VOICE)) {
			TTS_VOICE = voices[0]?.id ?? '';
		} else if (voices.length === 0 && !preserveCustomVoice) {
			TTS_VOICE = '';
		}
	};

	const getModels = async (refresh = false) => {
		if (TTS_ENGINE === '') {
			models = [];
		} else {
			TTS_MODELS_LOADING = TTS_ENGINE === 'openrouter';
			TTS_MODELS_ERROR = '';

			const res = await _getModels(
				localStorage.token,
				TTS_ENGINE === 'openrouter' ? 'openrouter' : undefined,
				refresh
			).catch((e) => {
				TTS_MODELS_ERROR = `${e}`;
				toast.error(`${e}`);
			});

			if (res) {
				models = res.models;

				if (TTS_ENGINE === 'openrouter') {
					if (!models.some((model) => model.id === TTS_MODEL)) {
						TTS_MODEL = models[0]?.id ?? '';
					}
					syncOpenRouterVoices();

					if (refresh) {
						toast.success($i18n.t('OpenRouter model catalog refreshed'));
					}
				}
			}

			TTS_MODELS_LOADING = false;
		}
	};

	const syncOpenRouterSTTSettings = () => {
		if (!selectedOpenRouterSTTModel?.supported_parameters?.includes('temperature')) {
			STT_OPENROUTER_TEMPERATURE = null;
		}
	};

	const getSTTModels = async (refresh = false) => {
		if (STT_ENGINE !== 'openrouter') {
			sttModels = [];
			STT_MODELS_ERROR = '';
			return;
		}

		STT_MODELS_LOADING = true;
		STT_MODELS_ERROR = '';

		const res = await _getModels(localStorage.token, 'openrouter-stt', refresh).catch((e) => {
			STT_MODELS_ERROR = `${e}`;
			toast.error(`${e}`);
		});

		if (res) {
			sttModels = res.models;
			if (!sttModels.some((model) => model.id === STT_MODEL)) {
				STT_MODEL = sttModels[0]?.id ?? '';
			}
			syncOpenRouterSTTSettings();

			if (refresh) {
				toast.success($i18n.t('OpenRouter transcription model catalog refreshed'));
			}
		}

		STT_MODELS_LOADING = false;
	};

	const onSTTEngineChange = async () => {
		sttModels = [];
		STT_MODELS_ERROR = '';
		STT_OPENROUTER_TEMPERATURE = null;

		if (STT_ENGINE === 'openrouter') {
			STT_MODEL = '';
			await getSTTModels();
		} else if (STT_ENGINE === 'openai') {
			STT_MODEL = 'whisper-1';
		} else if (STT_ENGINE !== '') {
			STT_MODEL = '';
		}
	};

	const getVoices = async () => {
		if (TTS_ENGINE === '') {
			// Capped: iOS/PWA contexts can leave getVoices() empty forever, which
			// would spin this 100ms interval for the page's lifetime.
			let voicesAttempts = 0;
			const getVoicesLoop = setInterval(() => {
				voices = speechSynthesis.getVoices();
				voicesAttempts += 1;

				if (voices.length > 0 || voicesAttempts >= 30) {
					clearInterval(getVoicesLoop);
					voices.sort((a, b) => a.name.localeCompare(b.name, $i18n.resolvedLanguage));
				}
			}, 100);
		} else if (TTS_ENGINE === 'openrouter') {
			syncOpenRouterVoices();
		} else {
			const res = await _getVoices(localStorage.token).catch((e) => {
				toast.error(`${e}`);
			});

			if (res) {
				console.log(res);
				voices = res.voices;
				voices.sort((a, b) => a.name.localeCompare(b.name, $i18n.resolvedLanguage));
			}
		}
	};

	const onTTSEngineChange = async () => {
		models = [];
		voices = [];
		TTS_MODELS_ERROR = '';

		if (TTS_ENGINE === 'openrouter') {
			TTS_MODEL = '';
			TTS_VOICE = '';
			await getModels();
			return;
		}

		if (TTS_ENGINE === 'openai') {
			TTS_MODEL = 'tts-1';
			TTS_VOICE = 'alloy';
		} else {
			TTS_MODEL = '';
			TTS_VOICE = '';
		}

		await updateConfigHandler();
		await getModels();
		await getVoices();
	};

	const formatOpenRouterVoice = (voice: string) => {
		const kokoroVoice = voice.match(/^([abefhijpz])([fm])_(.+)$/);
		if (kokoroVoice) {
			const languages: Record<string, string> = {
				a: 'American English',
				b: 'British English',
				e: 'Spanish',
				f: 'French',
				h: 'Hindi',
				i: 'Italian',
				j: 'Japanese',
				p: 'Portuguese',
				z: 'Chinese'
			};
			const name = kokoroVoice[3]
				.split('_')
				.map((part) => part.charAt(0).toUpperCase() + part.slice(1))
				.join(' ');
			return `${name} · ${languages[kokoroVoice[1]]} · ${kokoroVoice[2] === 'f' ? 'Female' : 'Male'}`;
		}

		const deepgramVoice = voice.match(/^aura-2-(.+)-([a-z]{2})$/);
		if (deepgramVoice) {
			return `${deepgramVoice[1].charAt(0).toUpperCase() + deepgramVoice[1].slice(1)} · ${deepgramVoice[2].toUpperCase()}`;
		}

		return voice
			.split(/[_-]/)
			.map((part) => part.charAt(0).toUpperCase() + part.slice(1))
			.join(' ');
	};

	const formatOpenRouterPrice = (value?: string) => {
		if (!value || value === '0') {
			return $i18n.t('Free');
		}
		if (value.startsWith('$')) {
			return value;
		}

		const pricePerMillion = Number(value) * 1_000_000;
		if (!Number.isFinite(pricePerMillion)) {
			return value;
		}

		return `$${pricePerMillion.toLocaleString(undefined, {
			maximumFractionDigits: 4
		})} / 1M tokens`;
	};

	const stopOpenRouterPreview = () => {
		TTS_PREVIEW_AUDIO?.pause();
		TTS_PREVIEW_AUDIO = null;
		TTS_PREVIEW_PLAYING = false;

		if (TTS_PREVIEW_URL) {
			URL.revokeObjectURL(TTS_PREVIEW_URL);
			TTS_PREVIEW_URL = '';
		}
	};

	const previewOpenRouterVoice = async () => {
		if (TTS_PREVIEW_PLAYING) {
			stopOpenRouterPreview();
			return;
		}

		TTS_PREVIEW_LOADING = true;
		const res = await previewOpenRouterSpeech(localStorage.token, {
			API_KEY: TTS_OPENROUTER_API_KEY,
			MODEL: TTS_MODEL,
			VOICE: TTS_VOICE,
			INPUT: $i18n.t('Hello! This is how read aloud will sound.')
		}).catch((e) => {
			toast.error(`${e}`);
			return null;
		});
		TTS_PREVIEW_LOADING = false;

		if (!res) {
			return;
		}

		stopOpenRouterPreview();
		TTS_PREVIEW_URL = URL.createObjectURL(await res.blob());
		TTS_PREVIEW_AUDIO = new Audio(TTS_PREVIEW_URL);
		TTS_PREVIEW_AUDIO.onended = stopOpenRouterPreview;
		TTS_PREVIEW_AUDIO.onerror = () => {
			toast.error($i18n.t('Unable to play the voice preview'));
			stopOpenRouterPreview();
		};

		try {
			await TTS_PREVIEW_AUDIO.play();
			TTS_PREVIEW_PLAYING = true;
		} catch (e) {
			toast.error(`${e}`);
			stopOpenRouterPreview();
		}
	};

	const updateConfigHandler = async () => {
		let openaiParams = {};
		try {
			openaiParams = TTS_OPENAI_PARAMS ? JSON.parse(TTS_OPENAI_PARAMS) : {};
			TTS_OPENAI_PARAMS = JSON.stringify(openaiParams, null, 2);
		} catch (e) {
			toast.error($i18n.t('Invalid JSON format for Parameters'));
			return;
		}

		const res = await updateAudioConfig(localStorage.token, {
			tts: {
				OPENAI_API_BASE_URL: TTS_OPENAI_API_BASE_URL,
				OPENAI_API_KEY: TTS_OPENAI_API_KEY,
				OPENAI_PARAMS: openaiParams,
				OPENROUTER_API_KEY: TTS_OPENROUTER_API_KEY,
				API_KEY: TTS_API_KEY,
				ENGINE: TTS_ENGINE,
				MODEL: TTS_MODEL,
				VOICE: TTS_VOICE,
				AZURE_SPEECH_REGION: TTS_AZURE_SPEECH_REGION,
				AZURE_SPEECH_BASE_URL: TTS_AZURE_SPEECH_BASE_URL,
				AZURE_SPEECH_OUTPUT_FORMAT: TTS_AZURE_SPEECH_OUTPUT_FORMAT,
				SPLIT_ON: TTS_SPLIT_ON
			},
			stt: {
				OPENAI_API_BASE_URL: STT_OPENAI_API_BASE_URL,
				OPENAI_API_KEY: STT_OPENAI_API_KEY,
				OPENROUTER_API_KEY: STT_OPENROUTER_API_KEY,
				OPENROUTER_TEMPERATURE: STT_OPENROUTER_TEMPERATURE,
				ENGINE: STT_ENGINE,
				MODEL: STT_MODEL,
				SUPPORTED_CONTENT_TYPES: STT_SUPPORTED_CONTENT_TYPES.split(',')
					.map((contentType) => contentType.trim())
					.filter(Boolean),
				WHISPER_MODEL: STT_WHISPER_MODEL,
				DEEPGRAM_API_KEY: STT_DEEPGRAM_API_KEY,
				AZURE_API_KEY: STT_AZURE_API_KEY,
				AZURE_REGION: STT_AZURE_REGION,
				AZURE_LOCALES: STT_AZURE_LOCALES,
				AZURE_BASE_URL: STT_AZURE_BASE_URL,
				AZURE_MAX_SPEAKERS: STT_AZURE_MAX_SPEAKERS
			}
		});

		if (res) {
			saveHandler();
			config.set(await getBackendConfig());
		}
	};

	const sttModelUpdateHandler = async () => {
		STT_WHISPER_MODEL_LOADING = true;
		await updateConfigHandler();
		STT_WHISPER_MODEL_LOADING = false;
	};

	onMount(async () => {
		const res = await getAudioConfig(localStorage.token);

		if (res) {
			console.log(res);
			TTS_OPENAI_API_BASE_URL = res.tts.OPENAI_API_BASE_URL;
			TTS_OPENAI_API_KEY = res.tts.OPENAI_API_KEY;
			TTS_OPENAI_PARAMS = JSON.stringify(res?.tts?.OPENAI_PARAMS ?? '', null, 2);
			TTS_OPENROUTER_API_KEY = res.tts.OPENROUTER_API_KEY ?? '';
			TTS_API_KEY = res.tts.API_KEY;

			TTS_ENGINE = res.tts.ENGINE;
			TTS_MODEL = res.tts.MODEL;
			TTS_VOICE = res.tts.VOICE;

			TTS_SPLIT_ON = res.tts.SPLIT_ON || TTS_RESPONSE_SPLIT.PUNCTUATION;

			TTS_AZURE_SPEECH_REGION = res.tts.AZURE_SPEECH_REGION;
			TTS_AZURE_SPEECH_BASE_URL = res.tts.AZURE_SPEECH_BASE_URL;
			TTS_AZURE_SPEECH_OUTPUT_FORMAT = res.tts.AZURE_SPEECH_OUTPUT_FORMAT;

			STT_OPENAI_API_BASE_URL = res.stt.OPENAI_API_BASE_URL;
			STT_OPENAI_API_KEY = res.stt.OPENAI_API_KEY;
			STT_OPENROUTER_API_KEY = res.stt.OPENROUTER_API_KEY ?? '';
			STT_OPENROUTER_TEMPERATURE = res.stt.OPENROUTER_TEMPERATURE ?? null;

			STT_ENGINE = res.stt.ENGINE;
			STT_MODEL = res.stt.MODEL;
			STT_SUPPORTED_CONTENT_TYPES = (res?.stt?.SUPPORTED_CONTENT_TYPES ?? []).join(',');
			STT_WHISPER_MODEL = res.stt.WHISPER_MODEL;
			STT_AZURE_API_KEY = res.stt.AZURE_API_KEY;
			STT_AZURE_REGION = res.stt.AZURE_REGION;
			STT_AZURE_LOCALES = res.stt.AZURE_LOCALES;
			STT_AZURE_BASE_URL = res.stt.AZURE_BASE_URL;
			STT_AZURE_MAX_SPEAKERS = res.stt.AZURE_MAX_SPEAKERS;
			STT_DEEPGRAM_API_KEY = res.stt.DEEPGRAM_API_KEY;
		}

		await Promise.all([getModels(), getSTTModels()]);
		await getVoices();
	});

	onDestroy(stopOpenRouterPreview);
</script>

<form
	class="flex flex-col h-full justify-between space-y-3 text-sm"
	onsubmit={preventDefault(async () => {
		await updateConfigHandler();
		dispatch('save');
	})}
>
	<div class=" space-y-3 overflow-y-scroll scrollbar-hidden h-full">
		<div class="flex flex-col gap-3">
			<div>
				<div class=" mb-2.5 text-base font-medium">{$i18n.t('Speech-to-Text')}</div>

				<hr class=" border-gray-100 dark:border-gray-850 my-2" />

				{#if STT_ENGINE !== 'web'}
					<div class="mb-2">
						<div class=" mb-1.5 text-xs font-medium">{$i18n.t('Supported MIME Types')}</div>
						<div class="flex w-full">
							<div class="flex-1">
								<input
									class="w-full rounded-lg py-2 px-4 text-sm bg-gray-50 dark:text-gray-300 dark:bg-gray-850 outline-hidden"
									bind:value={STT_SUPPORTED_CONTENT_TYPES}
									placeholder={$i18n.t(
										'e.g., audio/wav,audio/mpeg,video/* (leave blank for defaults)'
									)}
								/>
							</div>
						</div>
					</div>
				{/if}

				<div class="mb-2 py-0.5 flex w-full justify-between">
					<div class=" self-center text-xs font-medium">{$i18n.t('Speech-to-Text Engine')}</div>
					<div class="flex items-center relative">
						<select
							class="dark:bg-gray-900 cursor-pointer w-fit pr-8 rounded-lg px-2 p-1 text-xs bg-transparent outline-hidden text-right"
							bind:value={STT_ENGINE}
							placeholder={$i18n.t('Select an engine')}
							onchange={onSTTEngineChange}
						>
							<option value="">{$i18n.t('Whisper (Local)')}</option>
							<option value="openrouter">{$i18n.t('OpenRouter')}</option>
							<option value="openai">{$i18n.t('OpenAI')}</option>
							<option value="web">{$i18n.t('Web API')}</option>
							<option value="deepgram">{$i18n.t('Deepgram')}</option>
							<option value="azure">{$i18n.t('Azure AI Speech')}</option>
						</select>
					</div>
				</div>

				{#if STT_ENGINE === 'openrouter'}
					<div
						class="mb-3 overflow-hidden rounded-xl border border-gray-200 bg-gray-50/70 dark:border-gray-800 dark:bg-gray-850/70"
					>
						<div class="flex items-start justify-between gap-3 px-4 pt-4">
							<div>
								<div class="flex flex-wrap items-center gap-2">
									<div class="text-sm font-medium">{$i18n.t('OpenRouter Speech-to-Text')}</div>
									<span
										class="rounded-full bg-emerald-100 px-2 py-0.5 text-[10px] font-medium text-emerald-700 dark:bg-emerald-950 dark:text-emerald-300"
									>
										{$i18n.t('Live catalog')}
									</span>
								</div>
								<div class="mt-1 text-xs leading-5 text-gray-500 dark:text-gray-400">
									{$i18n.t(
										'Record from the message box and transcribe through the OpenRouter model you choose.'
									)}
								</div>
							</div>
							<div class="flex shrink-0 flex-col items-end gap-1">
								<a
									class="text-xs font-medium text-gray-600 hover:underline dark:text-gray-300"
									href="https://openrouter.ai/settings/keys"
									target="_blank"
									rel="noreferrer"
								>
									{$i18n.t('Manage keys')}
								</a>
								<a
									class="text-[11px] text-gray-400 hover:underline dark:text-gray-500"
									href="https://openrouter.ai/docs/guides/overview/multimodal/stt"
									target="_blank"
									rel="noreferrer"
								>
									{$i18n.t('OpenRouter docs')}
								</a>
							</div>
						</div>

						<div class="space-y-3 px-4 pb-4 pt-3">
							<div>
								<label class="mb-1.5 block text-xs font-medium" for="openrouter-stt-key">
									{$i18n.t('OpenRouter API Key')}
								</label>
								<SensitiveInput
									id="openrouter-stt-key"
									type="password"
									placeholder="sk-or-v1-…"
									bind:value={STT_OPENROUTER_API_KEY}
									outerClassName="flex w-full rounded-lg border border-gray-200 bg-white px-3 py-2 dark:border-gray-700 dark:bg-gray-900"
									inputClassName="w-full bg-transparent text-sm outline-hidden"
									showButtonClassName="pl-2 text-gray-500 transition hover:text-gray-800 dark:hover:text-gray-200"
								/>
								<div class="mt-1.5 text-[11px] text-gray-400 dark:text-gray-500">
									{$i18n.t(
										'The key stays on the server. Recorded audio is sent from this app directly to OpenRouter.'
									)}
								</div>
							</div>

							<div>
								<div class="mb-1.5 flex items-center justify-between gap-2">
									<label class="text-xs font-medium" for="openrouter-stt-model">
										{$i18n.t('Transcription model')}
									</label>
									<button
										class="flex items-center gap-1 text-[11px] font-medium text-gray-500 transition hover:text-gray-900 disabled:cursor-wait disabled:opacity-60 dark:hover:text-gray-100"
										type="button"
										disabled={STT_MODELS_LOADING}
										onclick={() => getSTTModels(true)}
									>
										{#if STT_MODELS_LOADING}
											<Spinner className="size-3" />
										{/if}
										{$i18n.t('Refresh')}
									</button>
								</div>
								{#if STT_MODELS_ERROR}
									<input
										id="openrouter-stt-model"
										class="w-full rounded-lg border border-gray-200 bg-white px-3 py-2.5 text-sm outline-hidden transition focus:border-gray-400 dark:border-gray-700 dark:bg-gray-900 dark:text-gray-200"
										bind:value={STT_MODEL}
										placeholder="openai/whisper-large-v3"
										required
									/>
								{:else}
									<select
										id="openrouter-stt-model"
										class="w-full rounded-lg border border-gray-200 bg-white px-3 py-2.5 text-sm outline-hidden transition focus:border-gray-400 dark:border-gray-700 dark:bg-gray-900 dark:text-gray-200"
										bind:value={STT_MODEL}
										disabled={STT_MODELS_LOADING || sttModels.length === 0}
										required
										onchange={syncOpenRouterSTTSettings}
									>
										{#if sttModels.length === 0}
											<option value="">
												{STT_MODELS_LOADING
													? $i18n.t('Loading transcription models…')
													: $i18n.t('No transcription models available')}
											</option>
										{/if}
										{#each sttModels as model}
											<option value={model.id}>{model.name ?? model.id}</option>
										{/each}
									</select>
								{/if}
							</div>

							{#if STT_MODELS_ERROR}
								<div
									class="rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700 dark:border-red-900 dark:bg-red-950/40 dark:text-red-300"
								>
									{STT_MODELS_ERROR}
									<div class="mt-1 text-red-600/80 dark:text-red-300/80">
										{$i18n.t('You can still enter an OpenRouter model slug manually above.')}
									</div>
								</div>
							{:else if selectedOpenRouterSTTModel}
								<div
									class="rounded-lg border border-gray-100 bg-white px-3 py-3 dark:border-gray-800 dark:bg-gray-900"
								>
									<div class="flex flex-wrap items-center justify-between gap-2">
										<div>
											<div class="text-xs font-medium text-gray-700 dark:text-gray-200">
												{selectedOpenRouterSTTModel.id}
											</div>
											<div class="mt-0.5 text-[11px] text-gray-400 dark:text-gray-500">
												{formatOpenRouterPrice(selectedOpenRouterSTTModel.pricing?.prompt)}
												{$i18n.t('audio input')}
												{#if selectedOpenRouterSTTModel.pricing?.completion && selectedOpenRouterSTTModel.pricing.completion !== '0'}
													· {formatOpenRouterPrice(selectedOpenRouterSTTModel.pricing.completion)}
													{$i18n.t('text output')}
												{/if}
											</div>
										</div>

										{#if selectedOpenRouterSTTModel.supported_parameters?.includes('temperature')}
											<label
												class="flex items-center gap-2 text-[11px] text-gray-500 dark:text-gray-400"
											>
												<span>{$i18n.t('Temperature')}</span>
												<select
													class="rounded-md border border-gray-200 bg-gray-50 px-2 py-1 text-xs text-gray-700 outline-hidden dark:border-gray-700 dark:bg-gray-850 dark:text-gray-200"
													bind:value={STT_OPENROUTER_TEMPERATURE}
												>
													<option value={null}>{$i18n.t('Automatic')}</option>
													<option value={0}>0</option>
													<option value={0.2}>0.2</option>
													<option value={0.5}>0.5</option>
													<option value={1}>1</option>
												</select>
											</label>
										{/if}
									</div>
									{#if selectedOpenRouterSTTModel.description}
										<div class="mt-1.5 text-xs leading-5 text-gray-500 dark:text-gray-400">
											{selectedOpenRouterSTTModel.description}
										</div>
									{/if}
								</div>
							{/if}

							<div class="text-[11px] leading-4 text-gray-400 dark:text-gray-500">
								{$i18n.t(
									'Each user’s Speech-to-Text language is forwarded as a hint; when unset, OpenRouter detects it automatically.'
								)}
							</div>
						</div>
					</div>
				{:else if STT_ENGINE === 'openai'}
					<div>
						<div class="mt-1 flex gap-2 mb-1">
							<input
								class="flex-1 w-full bg-transparent outline-hidden"
								placeholder={$i18n.t('API Base URL')}
								bind:value={STT_OPENAI_API_BASE_URL}
								required
							/>

							<SensitiveInput placeholder={$i18n.t('API Key')} bind:value={STT_OPENAI_API_KEY} />
						</div>
					</div>

					<hr class="border-gray-100 dark:border-gray-850 my-2" />

					<div>
						<div class=" mb-1.5 text-xs font-medium">{$i18n.t('STT Model')}</div>
						<div class="flex w-full">
							<div class="flex-1">
								<input
									list="model-list"
									class="w-full rounded-lg py-2 px-4 text-sm bg-gray-50 dark:text-gray-300 dark:bg-gray-850 outline-hidden"
									bind:value={STT_MODEL}
									placeholder={$i18n.t('Select a model')}
								/>

								<datalist id="model-list">
									<option value="whisper-1"></option>
								</datalist>
							</div>
						</div>
					</div>
				{:else if STT_ENGINE === 'deepgram'}
					<div>
						<div class="mt-1 flex gap-2 mb-1">
							<SensitiveInput placeholder={$i18n.t('API Key')} bind:value={STT_DEEPGRAM_API_KEY} />
						</div>
					</div>

					<hr class="border-gray-100 dark:border-gray-850 my-2" />

					<div>
						<div class=" mb-1.5 text-xs font-medium">{$i18n.t('STT Model')}</div>
						<div class="flex w-full">
							<div class="flex-1">
								<input
									class="w-full rounded-lg py-2 px-4 text-sm bg-gray-50 dark:text-gray-300 dark:bg-gray-850 outline-hidden"
									bind:value={STT_MODEL}
									placeholder={$i18n.t('Select a model (optional)')}
								/>
							</div>
						</div>
						<div class="mt-2 mb-1 text-xs text-gray-400 dark:text-gray-500">
							{$i18n.t('Leave model field empty to use the default model.')}
							<a
								class=" hover:underline dark:text-gray-200 text-gray-800"
								href="https://developers.deepgram.com/docs/models"
								target="_blank"
							>
								{$i18n.t('Click here to see available models.')}
							</a>
						</div>
					</div>
				{:else if STT_ENGINE === 'azure'}
					<div>
						<div class="mt-1 flex gap-2 mb-1">
							<SensitiveInput
								placeholder={$i18n.t('API Key')}
								bind:value={STT_AZURE_API_KEY}
								required
							/>
						</div>

						<hr class="border-gray-100 dark:border-gray-850 my-2" />

						<div>
							<div class=" mb-1.5 text-xs font-medium">{$i18n.t('Azure Region')}</div>
							<div class="flex w-full">
								<div class="flex-1">
									<input
										class="w-full rounded-lg py-2 px-4 text-sm bg-gray-50 dark:text-gray-300 dark:bg-gray-850 outline-hidden"
										bind:value={STT_AZURE_REGION}
										placeholder={$i18n.t('e.g., westus (leave blank for eastus)')}
									/>
								</div>
							</div>
						</div>

						<div>
							<div class=" mb-1.5 text-xs font-medium">{$i18n.t('Language Locales')}</div>
							<div class="flex w-full">
								<div class="flex-1">
									<input
										class="w-full rounded-lg py-2 px-4 text-sm bg-gray-50 dark:text-gray-300 dark:bg-gray-850 outline-hidden"
										bind:value={STT_AZURE_LOCALES}
										placeholder={$i18n.t('e.g., en-US,ja-JP (leave blank for auto-detect)')}
									/>
								</div>
							</div>
						</div>

						<div>
							<div class=" mb-1.5 text-xs font-medium">{$i18n.t('Endpoint URL')}</div>
							<div class="flex w-full">
								<div class="flex-1">
									<input
										class="w-full rounded-lg py-2 px-4 text-sm bg-gray-50 dark:text-gray-300 dark:bg-gray-850 outline-hidden"
										bind:value={STT_AZURE_BASE_URL}
										placeholder={$i18n.t('(leave blank for to use commercial endpoint)')}
									/>
								</div>
							</div>
						</div>

						<div>
							<div class=" mb-1.5 text-xs font-medium">{$i18n.t('Max Speakers')}</div>
							<div class="flex w-full">
								<div class="flex-1">
									<input
										class="w-full rounded-lg py-2 px-4 text-sm bg-gray-50 dark:text-gray-300 dark:bg-gray-850 outline-hidden"
										bind:value={STT_AZURE_MAX_SPEAKERS}
										placeholder={$i18n.t('e.g., 3, 4, 5 (leave blank for default)')}
									/>
								</div>
							</div>
						</div>
					</div>
				{:else if STT_ENGINE === ''}
					<div>
						<div class=" mb-1.5 text-xs font-medium">{$i18n.t('STT Model')}</div>

						<div class="flex w-full">
							<div class="flex-1 mr-2">
								<input
									class="w-full rounded-lg py-2 px-4 text-sm bg-gray-50 dark:text-gray-300 dark:bg-gray-850 outline-hidden"
									placeholder={$i18n.t('Set whisper model')}
									bind:value={STT_WHISPER_MODEL}
								/>
							</div>

							<button
								class="px-2.5 bg-gray-50 hover:bg-gray-100 text-gray-800 dark:bg-gray-850 dark:hover:bg-gray-800 dark:text-gray-100 rounded-lg transition"
								onclick={() => {
									sttModelUpdateHandler();
								}}
								disabled={STT_WHISPER_MODEL_LOADING}
							>
								{#if STT_WHISPER_MODEL_LOADING}
									<div class="self-center">
										<Spinner />
									</div>
								{:else}
									<svg
										xmlns="http://www.w3.org/2000/svg"
										viewBox="0 0 16 16"
										fill="currentColor"
										class="w-4 h-4"
									>
										<path
											d="M8.75 2.75a.75.75 0 0 0-1.5 0v5.69L5.03 6.22a.75.75 0 0 0-1.06 1.06l3.5 3.5a.75.75 0 0 0 1.06 0l3.5-3.5a.75.75 0 0 0-1.06-1.06L8.75 8.44V2.75Z"
										/>
										<path
											d="M3.5 9.75a.75.75 0 0 0-1.5 0v1.5A2.75 2.75 0 0 0 4.75 14h6.5A2.75 2.75 0 0 0 14 11.25v-1.5a.75.75 0 0 0-1.5 0v1.5c0 .69-.56 1.25-1.25 1.25h-6.5c-.69 0-1.25-.56-1.25-1.25v-1.5Z"
										/>
									</svg>
								{/if}
							</button>
						</div>

						<div class="mt-2 mb-1 text-xs text-gray-400 dark:text-gray-500">
							{$i18n.t(`{{WEBUI_NAME}} uses faster-whisper internally.`)}

							<a
								class=" hover:underline dark:text-gray-200 text-gray-800"
								href="https://github.com/SYSTRAN/faster-whisper"
								target="_blank"
							>
								{$i18n.t(
									`Click here to learn more about faster-whisper and see the available models.`
								)}
							</a>
						</div>
					</div>
				{/if}
			</div>

			<div>
				<div class=" mb-2.5 text-base font-medium">{$i18n.t('Text-to-Speech')}</div>

				<hr class=" border-gray-100 dark:border-gray-850 my-2" />

				<div class="mb-2 py-0.5 flex w-full justify-between">
					<div class=" self-center text-xs font-medium">{$i18n.t('Text-to-Speech Engine')}</div>
					<div class="flex items-center relative">
						<select
							class=" dark:bg-gray-900 w-fit pr-8 cursor-pointer rounded-lg px-2 p-1 text-xs bg-transparent outline-hidden text-right"
							bind:value={TTS_ENGINE}
							placeholder={$i18n.t('Select a mode')}
							onchange={onTTSEngineChange}
						>
							<option value="">{$i18n.t('Web API')}</option>
							<option value="transformers">{$i18n.t('Transformers')} ({$i18n.t('Local')})</option>
							<option value="openrouter">{$i18n.t('OpenRouter')}</option>
							<option value="openai">{$i18n.t('OpenAI')}</option>
							<option value="elevenlabs">{$i18n.t('ElevenLabs')}</option>
							<option value="azure">{$i18n.t('Azure AI Speech')}</option>
						</select>
					</div>
				</div>

				{#if TTS_ENGINE === 'openrouter'}
					<div
						class="mb-3 overflow-hidden rounded-xl border border-gray-200 bg-gray-50/70 dark:border-gray-800 dark:bg-gray-850/70"
					>
						<div class="flex items-start justify-between gap-3 px-4 pt-4">
							<div>
								<div class="flex items-center gap-2">
									<div class="text-sm font-medium">{$i18n.t('OpenRouter Text-to-Speech')}</div>
									<span
										class="rounded-full bg-emerald-100 px-2 py-0.5 text-[10px] font-medium text-emerald-700 dark:bg-emerald-950 dark:text-emerald-300"
									>
										{$i18n.t('Live catalog')}
									</span>
								</div>
								<div class="mt-1 text-xs leading-5 text-gray-500 dark:text-gray-400">
									{$i18n.t(
										'Choose from every speech model and voice currently available through OpenRouter.'
									)}
								</div>
							</div>
							<a
								class="shrink-0 text-xs font-medium text-gray-600 hover:underline dark:text-gray-300"
								href="https://openrouter.ai/settings/keys"
								target="_blank"
								rel="noreferrer"
							>
								{$i18n.t('Manage keys')}
							</a>
						</div>

						<div class="px-4 pb-4 pt-3">
							<label class="mb-1.5 block text-xs font-medium" for="openrouter-tts-key">
								{$i18n.t('OpenRouter API Key')}
							</label>
							<SensitiveInput
								id="openrouter-tts-key"
								type="password"
								placeholder="sk-or-v1-…"
								bind:value={TTS_OPENROUTER_API_KEY}
								outerClassName="flex w-full rounded-lg border border-gray-200 bg-white px-3 py-2 dark:border-gray-700 dark:bg-gray-900"
								inputClassName="w-full bg-transparent text-sm outline-hidden"
								showButtonClassName="pl-2 text-gray-500 transition hover:text-gray-800 dark:hover:text-gray-200"
							/>
							<div class="mt-1.5 text-[11px] text-gray-400 dark:text-gray-500">
								{$i18n.t(
									'The key stays on the server. Browsers send read-aloud requests only to this app.'
								)}
							</div>
						</div>
					</div>
				{:else if TTS_ENGINE === 'openai'}
					<div>
						<div class="mt-1 flex gap-2 mb-1">
							<input
								class="flex-1 w-full bg-transparent outline-hidden"
								placeholder={$i18n.t('API Base URL')}
								bind:value={TTS_OPENAI_API_BASE_URL}
								required
							/>

							<SensitiveInput placeholder={$i18n.t('API Key')} bind:value={TTS_OPENAI_API_KEY} />
						</div>
					</div>
				{:else if TTS_ENGINE === 'elevenlabs'}
					<div>
						<div class="mt-1 flex gap-2 mb-1">
							<input
								class="flex-1 w-full rounded-lg py-2 pl-4 text-sm bg-gray-50 dark:text-gray-300 dark:bg-gray-850 outline-hidden"
								placeholder={$i18n.t('API Key')}
								bind:value={TTS_API_KEY}
								required
							/>
						</div>
					</div>
				{:else if TTS_ENGINE === 'azure'}
					<div>
						<div class="mt-1 flex gap-2 mb-1">
							<SensitiveInput placeholder={$i18n.t('API Key')} bind:value={TTS_API_KEY} required />
						</div>

						<hr class="border-gray-100 dark:border-gray-850 my-2" />

						<div>
							<div class=" mb-1.5 text-xs font-medium">{$i18n.t('Azure Region')}</div>
							<div class="flex w-full">
								<div class="flex-1">
									<input
										class="w-full rounded-lg py-2 px-4 text-sm bg-gray-50 dark:text-gray-300 dark:bg-gray-850 outline-hidden"
										bind:value={TTS_AZURE_SPEECH_REGION}
										placeholder={$i18n.t('e.g., westus (leave blank for eastus)')}
									/>
								</div>
							</div>
						</div>

						<div>
							<div class=" mb-1.5 text-xs font-medium">{$i18n.t('Endpoint URL')}</div>
							<div class="flex w-full">
								<div class="flex-1">
									<input
										class="w-full rounded-lg py-2 px-4 text-sm bg-gray-50 dark:text-gray-300 dark:bg-gray-850 outline-hidden"
										bind:value={TTS_AZURE_SPEECH_BASE_URL}
										placeholder={$i18n.t('(leave blank for to use commercial endpoint)')}
									/>
								</div>
							</div>
						</div>
					</div>
				{/if}

				<div class="mb-2">
					{#if TTS_ENGINE === ''}
						<div>
							<div class=" mb-1.5 text-xs font-medium">{$i18n.t('TTS Voice')}</div>
							<div class="flex w-full">
								<div class="flex-1">
									<select
										class="w-full rounded-lg py-2 px-4 text-sm bg-gray-50 dark:text-gray-300 dark:bg-gray-850 outline-hidden"
										bind:value={TTS_VOICE}
									>
										<option value="" selected={TTS_VOICE !== ''}>{$i18n.t('Default')}</option>
										{#each voices as voice}
											<option
												value={voice.voiceURI}
												class="bg-gray-100 dark:bg-gray-700"
												selected={TTS_VOICE === voice.voiceURI}>{voice.name}</option
											>
										{/each}
									</select>
								</div>
							</div>
						</div>
					{:else if TTS_ENGINE === 'transformers'}
						<div>
							<div class=" mb-1.5 text-xs font-medium">{$i18n.t('TTS Model')}</div>
							<div class="flex w-full">
								<div class="flex-1">
									<input
										list="model-list"
										class="w-full rounded-lg py-2 px-4 text-sm bg-gray-50 dark:text-gray-300 dark:bg-gray-850 outline-hidden"
										bind:value={TTS_MODEL}
										placeholder={$i18n.t('CMU ARCTIC speaker embedding name')}
									/>

									<datalist id="model-list">
										<option value="tts-1"></option>
									</datalist>
								</div>
							</div>
							<div class="mt-2 mb-1 text-xs text-gray-400 dark:text-gray-500">
								{$i18n.t(`{{WEBUI_NAME}} uses SpeechT5 and CMU Arctic speaker embeddings.`)}

								To learn more about SpeechT5,

								<a
									class=" hover:underline dark:text-gray-200 text-gray-800"
									href="https://github.com/microsoft/SpeechT5"
									target="_blank"
								>
									{$i18n.t(`click here`, {
										name: 'SpeechT5'
									})}.
								</a>
								To see the available CMU Arctic speaker embeddings,
								<a
									class=" hover:underline dark:text-gray-200 text-gray-800"
									href="https://huggingface.co/datasets/Matthijs/cmu-arctic-xvectors"
									target="_blank"
								>
									{$i18n.t(`click here`)}.
								</a>
							</div>
						</div>
					{:else if TTS_ENGINE === 'openrouter'}
						<div class="space-y-3">
							<div class="grid grid-cols-1 gap-3 sm:grid-cols-2">
								<div>
									<div class="mb-1.5 flex items-center justify-between gap-2">
										<label class="text-xs font-medium" for="openrouter-tts-model">
											{$i18n.t('Speech model')}
										</label>
										<button
											class="flex items-center gap-1 text-[11px] font-medium text-gray-500 transition hover:text-gray-900 disabled:cursor-wait disabled:opacity-60 dark:hover:text-gray-100"
											type="button"
											disabled={TTS_MODELS_LOADING}
											onclick={() => getModels(true)}
										>
											{#if TTS_MODELS_LOADING}
												<Spinner className="size-3" />
											{/if}
											{$i18n.t('Refresh')}
										</button>
									</div>
									<select
										id="openrouter-tts-model"
										class="w-full rounded-lg border border-gray-200 bg-gray-50 px-3 py-2.5 text-sm outline-hidden transition focus:border-gray-400 dark:border-gray-700 dark:bg-gray-850 dark:text-gray-200"
										bind:value={TTS_MODEL}
										disabled={TTS_MODELS_LOADING || models.length === 0}
										required
										onchange={() => syncOpenRouterVoices(false)}
									>
										{#if models.length === 0}
											<option value="">
												{TTS_MODELS_LOADING
													? $i18n.t('Loading speech models…')
													: $i18n.t('No speech models available')}
											</option>
										{/if}
										{#each models as model}
											<option value={model.id}>{model.name ?? model.id}</option>
										{/each}
									</select>
								</div>

								<div>
									<label class="mb-1.5 block text-xs font-medium" for="openrouter-tts-voice">
										{$i18n.t('Voice')}
									</label>
									{#if voices.length > 0}
										<select
											id="openrouter-tts-voice"
											class="w-full rounded-lg border border-gray-200 bg-gray-50 px-3 py-2.5 text-sm outline-hidden transition focus:border-gray-400 dark:border-gray-700 dark:bg-gray-850 dark:text-gray-200"
											bind:value={TTS_VOICE}
											required
										>
											{#each voices as voice}
												<option value={voice.id}>{voice.name}</option>
											{/each}
										</select>
									{:else}
										<input
											id="openrouter-tts-voice"
											class="w-full rounded-lg border border-gray-200 bg-gray-50 px-3 py-2.5 text-sm outline-hidden transition focus:border-gray-400 dark:border-gray-700 dark:bg-gray-850 dark:text-gray-200"
											bind:value={TTS_VOICE}
											placeholder={$i18n.t('Enter a provider voice ID')}
											required
										/>
									{/if}
								</div>
							</div>

							{#if TTS_MODELS_ERROR}
								<div
									class="rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700 dark:border-red-900 dark:bg-red-950/40 dark:text-red-300"
								>
									{TTS_MODELS_ERROR}
								</div>
							{:else if selectedOpenRouterModel}
								<div
									class="rounded-lg border border-gray-100 bg-white px-3 py-3 dark:border-gray-800 dark:bg-gray-900"
								>
									<div class="flex flex-wrap items-center gap-x-3 gap-y-1">
										<div class="text-xs font-medium text-gray-700 dark:text-gray-200">
											{selectedOpenRouterModel.id}
										</div>
										<div class="text-[11px] text-gray-400 dark:text-gray-500">
											{formatOpenRouterPrice(selectedOpenRouterModel.pricing?.prompt)}
											{$i18n.t('input')}
											{#if selectedOpenRouterModel.pricing?.completion && selectedOpenRouterModel.pricing.completion !== '0'}
												· {formatOpenRouterPrice(selectedOpenRouterModel.pricing.completion)}
												{$i18n.t('output')}
											{/if}
											· {selectedOpenRouterModel.voices?.length ?? 0}
											{$i18n.t('voices')}
										</div>
									</div>
									{#if selectedOpenRouterModel.description}
										<div class="mt-1.5 text-xs leading-5 text-gray-500 dark:text-gray-400">
											{selectedOpenRouterModel.description}
										</div>
									{/if}
								</div>
							{/if}

							<div class="flex items-center justify-between gap-3">
								<div class="text-[11px] text-gray-400 dark:text-gray-500">
									{$i18n.t('Preview sends one short, billable request through OpenRouter.')}
								</div>
								<button
									class="flex shrink-0 items-center gap-1.5 rounded-full border border-gray-200 bg-white px-3 py-1.5 text-xs font-medium text-gray-700 transition hover:bg-gray-100 disabled:cursor-not-allowed disabled:opacity-50 dark:border-gray-700 dark:bg-gray-900 dark:text-gray-200 dark:hover:bg-gray-800"
									type="button"
									disabled={TTS_PREVIEW_LOADING ||
										!TTS_OPENROUTER_API_KEY ||
										!TTS_MODEL ||
										!TTS_VOICE}
									onclick={previewOpenRouterVoice}
								>
									{#if TTS_PREVIEW_LOADING}
										<Spinner className="size-3" />
										{$i18n.t('Generating…')}
									{:else if TTS_PREVIEW_PLAYING}
										{$i18n.t('Stop preview')}
									{:else}
										{$i18n.t('Preview voice')}
									{/if}
								</button>
							</div>
						</div>
					{:else if TTS_ENGINE === 'openai'}
						<div class=" flex gap-2">
							<div class="w-full">
								<div class=" mb-1.5 text-xs font-medium">{$i18n.t('TTS Voice')}</div>
								<div class="flex w-full">
									<div class="flex-1">
										<input
											list="voice-list"
											class="w-full rounded-lg py-2 px-4 text-sm bg-gray-50 dark:text-gray-300 dark:bg-gray-850 outline-hidden"
											bind:value={TTS_VOICE}
											placeholder={$i18n.t('Select a voice')}
										/>

										<datalist id="voice-list">
											{#each voices as voice}
												<option value={voice.id}>{voice.name}</option>
											{/each}
										</datalist>
									</div>
								</div>
							</div>
							<div class="w-full">
								<div class=" mb-1.5 text-xs font-medium">{$i18n.t('TTS Model')}</div>
								<div class="flex w-full">
									<div class="flex-1">
										<input
											list="tts-model-list"
											class="w-full rounded-lg py-2 px-4 text-sm bg-gray-50 dark:text-gray-300 dark:bg-gray-850 outline-hidden"
											bind:value={TTS_MODEL}
											placeholder={$i18n.t('Select a model')}
										/>

										<datalist id="tts-model-list">
											{#each models as model}
												<option value={model.id} class="bg-gray-50 dark:bg-gray-700"></option>
											{/each}
										</datalist>
									</div>
								</div>
							</div>
						</div>

						<div class="mt-2 mb-1 text-xs text-gray-400 dark:text-gray-500">
							<div class="w-full">
								<div class=" mb-1.5 text-xs font-medium">{$i18n.t('Additional Parameters')}</div>
								<div class="flex w-full">
									<div class="flex-1">
										<Textarea
											className="w-full rounded-lg py-2 px-4 text-sm bg-gray-50 dark:text-gray-300 dark:bg-gray-850 outline-hidden"
											bind:value={TTS_OPENAI_PARAMS}
											placeholder={$i18n.t('Enter additional parameters in JSON format')}
											minSize={100}
										/>
									</div>
								</div>
							</div>
						</div>
					{:else if TTS_ENGINE === 'elevenlabs'}
						<div class=" flex gap-2">
							<div class="w-full">
								<div class=" mb-1.5 text-xs font-medium">{$i18n.t('TTS Voice')}</div>
								<div class="flex w-full">
									<div class="flex-1">
										<input
											list="voice-list"
											class="w-full rounded-lg py-2 px-4 text-sm bg-gray-50 dark:text-gray-300 dark:bg-gray-850 outline-hidden"
											bind:value={TTS_VOICE}
											placeholder={$i18n.t('Select a voice')}
										/>

										<datalist id="voice-list">
											{#each voices as voice}
												<option value={voice.id}>{voice.name}</option>
											{/each}
										</datalist>
									</div>
								</div>
							</div>
							<div class="w-full">
								<div class=" mb-1.5 text-xs font-medium">{$i18n.t('TTS Model')}</div>
								<div class="flex w-full">
									<div class="flex-1">
										<input
											list="tts-model-list"
											class="w-full rounded-lg py-2 px-4 text-sm bg-gray-50 dark:text-gray-300 dark:bg-gray-850 outline-hidden"
											bind:value={TTS_MODEL}
											placeholder={$i18n.t('Select a model')}
										/>

										<datalist id="tts-model-list">
											{#each models as model}
												<option value={model.id} class="bg-gray-50 dark:bg-gray-700"></option>
											{/each}
										</datalist>
									</div>
								</div>
							</div>
						</div>
					{:else if TTS_ENGINE === 'azure'}
						<div class=" flex gap-2">
							<div class="w-full">
								<div class=" mb-1.5 text-xs font-medium">{$i18n.t('TTS Voice')}</div>
								<div class="flex w-full">
									<div class="flex-1">
										<input
											list="voice-list"
											class="w-full rounded-lg py-2 px-4 text-sm bg-gray-50 dark:text-gray-300 dark:bg-gray-850 outline-hidden"
											bind:value={TTS_VOICE}
											placeholder={$i18n.t('Select a voice')}
										/>

										<datalist id="voice-list">
											{#each voices as voice}
												<option value={voice.id}>{voice.name}</option>
											{/each}
										</datalist>
									</div>
								</div>
							</div>
							<div class="w-full">
								<div class=" mb-1.5 text-xs font-medium">
									{$i18n.t('Output format')}
									<a
										href="https://learn.microsoft.com/en-us/azure/ai-services/speech-service/rest-text-to-speech?tabs=streaming#audio-outputs"
										target="_blank"
									>
										<small>{$i18n.t('Available list')}</small>
									</a>
								</div>
								<div class="flex w-full">
									<div class="flex-1">
										<input
											list="tts-model-list"
											class="w-full rounded-lg py-2 px-4 text-sm bg-gray-50 dark:text-gray-300 dark:bg-gray-850 outline-hidden"
											bind:value={TTS_AZURE_SPEECH_OUTPUT_FORMAT}
											placeholder={$i18n.t('Select an output format')}
										/>
									</div>
								</div>
							</div>
						</div>
					{/if}
				</div>

				<div class="pt-0.5 flex w-full justify-between">
					<div class="self-center text-xs font-medium">{$i18n.t('Response splitting')}</div>
					<div class="flex items-center relative">
						<select
							class="dark:bg-gray-900 w-fit pr-8 cursor-pointer rounded-lg px-2 p-1 text-xs bg-transparent outline-hidden text-right"
							aria-label={$i18n.t('Select how to split message text for TTS requests')}
							bind:value={TTS_SPLIT_ON}
						>
							{#each Object.values(TTS_RESPONSE_SPLIT) as split}
								<option value={split}
									>{$i18n.t(split.charAt(0).toUpperCase() + split.slice(1))}</option
								>
							{/each}
						</select>
					</div>
				</div>
				<div class="mt-2 mb-1 text-xs text-gray-400 dark:text-gray-500">
					{$i18n.t(
						"Control how message text is split for TTS requests. 'Punctuation' splits into sentences, 'paragraphs' splits into paragraphs, and 'none' keeps the message as a single string."
					)}
				</div>
			</div>
		</div>
	</div>
	<div class="flex justify-end text-sm font-medium">
		<button
			class="px-3.5 py-1.5 text-sm font-medium bg-book-cloth hover:bg-kraft text-white dark:bg-book-cloth dark:text-white dark:hover:bg-kraft transition-colors duration-200 ease-paper rounded-full"
			type="submit"
		>
			{$i18n.t('Save')}
		</button>
	</div>
</form>
