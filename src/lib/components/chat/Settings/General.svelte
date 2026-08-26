<script lang="ts">
	import { dispatchComponentEvent } from '$lib/utils/componentEvents';
	import { toast } from '$lib/utils/toast';
	import { onMount, getContext } from 'svelte';
	import { getLanguages, changeLanguage } from '$lib/i18n';
	const dispatch = (type: string, detail?: unknown) =>
		dispatchComponentEvent(eventProps, type, detail);

	import { config, fontFamily, models, settings, theme, user } from '$lib/stores';

	const i18n = getContext('i18n');

	import { subscribeToPush, unsubscribeFromPush } from '$lib/apis/push';

	import AdvancedParams from './Advanced/AdvancedParams.svelte';
	import Textarea from '$lib/components/common/Textarea.svelte';
	interface Props {
		saveSettings: Function;
		getModels: Function;
	}

	let { saveSettings, getModels, ...eventProps }: Props & Record<string, unknown> = $props();

	// General
	let themes = ['dark', 'light', 'oled-dark'];
	let selectedTheme = $state('system');
	let selectedFont = $state('system');

	let languages: Awaited<ReturnType<typeof getLanguages>> = $state([]);
	let lang = $state($i18n.language);
	let notificationEnabled = $state(false);
	let pushNotificationEnabled = $state(false);
	let system = $state('');

	// Web Push needs a service worker + a secure context, and on iOS only works
	// once the app is installed to the home screen. Detect rather than fail: the
	// toggle explains why it can't be used instead of erroring on tap.
	let pushSupported = $derived(
		typeof window !== 'undefined' &&
			'serviceWorker' in navigator &&
			'PushManager' in window &&
			window.isSecureContext &&
			!!$config?.features?.webpush_public_key
	);

	let showAdvanced = $state(false);

	const toggleNotification = async () => {
		const permission = await Notification.requestPermission();

		if (permission === 'granted') {
			notificationEnabled = !notificationEnabled;
			saveSettings({ notificationEnabled: notificationEnabled });
		} else {
			toast.error(
				$i18n.t(
					'Response notifications cannot be activated as the website permissions have been denied. Please visit your browser settings to grant the necessary access.'
				)
			);
		}
	};

	// The browser wants applicationServerKey as raw bytes; the server hands out
	// the standard unpadded url-safe base64 form.
	const urlBase64ToUint8Array = (base64String: string) => {
		const padding = '='.repeat((4 - (base64String.length % 4)) % 4);
		const base64 = (base64String + padding).replace(/-/g, '+').replace(/_/g, '/');
		const raw = atob(base64);
		return Uint8Array.from([...raw].map((char) => char.charCodeAt(0)));
	};

	const togglePushNotification = async () => {
		const registration = await navigator.serviceWorker.ready;

		if (pushNotificationEnabled) {
			const subscription = await registration.pushManager.getSubscription();
			if (subscription) {
				await unsubscribeFromPush(localStorage.token, subscription.endpoint).catch(() => null);
				await subscription.unsubscribe();
			}
			pushNotificationEnabled = false;
			saveSettings({ pushNotificationEnabled: false });
			return;
		}

		if ((await Notification.requestPermission()) !== 'granted') {
			toast.error(
				$i18n.t(
					'Response notifications cannot be activated as the website permissions have been denied. Please visit your browser settings to grant the necessary access.'
				)
			);
			return;
		}

		try {
			const subscription =
				(await registration.pushManager.getSubscription()) ??
				(await registration.pushManager.subscribe({
					userVisibleOnly: true,
					applicationServerKey: urlBase64ToUint8Array($config.features.webpush_public_key)
				}));
			await subscribeToPush(localStorage.token, subscription.toJSON());
		} catch (error) {
			toast.error(`${error}`);
			return;
		}

		pushNotificationEnabled = true;
		saveSettings({ pushNotificationEnabled: true });
	};

	let params = $state({
		// Advanced
		stream_response: null,
		stream_delta_chunk_size: null,
		function_calling: null,
		seed: null,
		temperature: null,
		reasoning_effort: null,
		logit_bias: null,
		frequency_penalty: null,
		presence_penalty: null,
		repeat_penalty: null,
		repeat_last_n: null,
		mirostat: null,
		mirostat_eta: null,
		mirostat_tau: null,
		top_k: null,
		top_p: null,
		min_p: null,
		stop: null,
		tfs_z: null,
		num_ctx: null,
		num_batch: null,
		num_keep: null,
		max_tokens: null,
		num_gpu: null
	});

	const saveHandler = async () => {
		const saved = await saveSettings({
			system: system !== '' ? system : undefined,
			params: {
				stream_response: params.stream_response !== null ? params.stream_response : undefined,
				stream_delta_chunk_size:
					params.stream_delta_chunk_size !== null ? params.stream_delta_chunk_size : undefined,
				function_calling: params.function_calling !== null ? params.function_calling : undefined,
				seed: (params.seed !== null ? params.seed : undefined) ?? undefined,
				stop: params.stop ? params.stop.split(',').filter((e) => e) : undefined,
				temperature: params.temperature !== null ? params.temperature : undefined,
				reasoning_effort: params.reasoning_effort !== null ? params.reasoning_effort : undefined,
				logit_bias: params.logit_bias !== null ? params.logit_bias : undefined,
				frequency_penalty: params.frequency_penalty !== null ? params.frequency_penalty : undefined,
				presence_penalty: params.frequency_penalty !== null ? params.frequency_penalty : undefined,
				repeat_penalty: params.frequency_penalty !== null ? params.frequency_penalty : undefined,
				repeat_last_n: params.repeat_last_n !== null ? params.repeat_last_n : undefined,
				mirostat: params.mirostat !== null ? params.mirostat : undefined,
				mirostat_eta: params.mirostat_eta !== null ? params.mirostat_eta : undefined,
				mirostat_tau: params.mirostat_tau !== null ? params.mirostat_tau : undefined,
				top_k: params.top_k !== null ? params.top_k : undefined,
				top_p: params.top_p !== null ? params.top_p : undefined,
				min_p: params.min_p !== null ? params.min_p : undefined,
				tfs_z: params.tfs_z !== null ? params.tfs_z : undefined,
				num_ctx: params.num_ctx !== null ? params.num_ctx : undefined,
				num_batch: params.num_batch !== null ? params.num_batch : undefined,
				num_keep: params.num_keep !== null ? params.num_keep : undefined,
				max_tokens: params.max_tokens !== null ? params.max_tokens : undefined,
				use_mmap: params.use_mmap !== null ? params.use_mmap : undefined,
				use_mlock: params.use_mlock !== null ? params.use_mlock : undefined,
				num_thread: params.num_thread !== null ? params.num_thread : undefined,
				num_gpu: params.num_gpu !== null ? params.num_gpu : undefined,
				think: params.think !== null ? params.think : undefined,
				keep_alive: params.keep_alive !== null ? params.keep_alive : undefined,
				format: params.format !== null ? params.format : undefined
			}
		});
		if (saved !== false) {
			dispatch('save');
		}
	};

	onMount(async () => {
		selectedTheme = localStorage.theme ?? 'system';
		selectedFont = localStorage.font ?? 'system';

		languages = await getLanguages();

		notificationEnabled = $settings.notificationEnabled ?? false;
		pushNotificationEnabled = $settings.pushNotificationEnabled ?? false;
		system = $settings.system ?? '';

		params = { ...params, ...$settings.params };
		params.stop = $settings?.params?.stop ? ($settings?.params?.stop ?? []).join(',') : null;
	});

	const applyTheme = (_theme: string) => {
		let themeToApply = _theme === 'oled-dark' ? 'dark' : _theme === 'her' ? 'light' : _theme;

		if (_theme === 'system') {
			themeToApply = window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
		}

		if (themeToApply === 'dark' && !_theme.includes('oled')) {
			document.documentElement.style.setProperty('--color-gray-800', '#33332E');
			document.documentElement.style.setProperty('--color-gray-850', '#262625');
			document.documentElement.style.setProperty('--color-gray-900', '#1F1F1E');
			document.documentElement.style.setProperty('--color-gray-950', '#191919');
		}

		themes
			.filter((e) => e !== themeToApply)
			.forEach((e) => {
				e.split(' ').forEach((e) => {
					document.documentElement.classList.remove(e);
				});
			});

		themeToApply.split(' ').forEach((e) => {
			document.documentElement.classList.add(e);
		});

		const metaThemeColor = document.querySelector('meta[name="theme-color"]');
		if (metaThemeColor) {
			if (_theme.includes('system')) {
				const systemTheme = window.matchMedia('(prefers-color-scheme: dark)').matches
					? 'dark'
					: 'light';
				metaThemeColor.setAttribute('content', systemTheme === 'light' ? '#FAFAF7' : '#262625');
			} else {
				metaThemeColor.setAttribute(
					'content',
					_theme === 'dark'
						? '#262625'
						: _theme === 'oled-dark'
							? '#000000'
							: _theme === 'her'
								? '#983724'
								: '#FAFAF7'
				);
			}
		}

		if (typeof window !== 'undefined' && window.applyTheme) {
			window.applyTheme();
		}

		if (_theme.includes('oled')) {
			document.documentElement.style.setProperty('--color-gray-800', '#101010');
			document.documentElement.style.setProperty('--color-gray-850', '#050505');
			document.documentElement.style.setProperty('--color-gray-900', '#000000');
			document.documentElement.style.setProperty('--color-gray-950', '#000000');
			document.documentElement.classList.add('dark');
		}

		console.log(_theme);
	};

	const themeChangeHandler = (_theme: string) => {
		theme.set(_theme);
		localStorage.setItem('theme', _theme);
		applyTheme(_theme);
	};

	const fontChangeHandler = (_font: string) => {
		fontFamily.set(_font);
		localStorage.setItem('font', _font);
		document.documentElement.setAttribute('data-font', _font);
	};
</script>

<div class="flex flex-col h-full justify-between text-sm" id="tab-general">
	<div class="  overflow-y-scroll max-h-[28rem] md:max-h-full">
		<div class="">
			<div class=" mb-1 text-sm font-medium">{$i18n.t('WebUI Settings')}</div>

			<div class="flex w-full justify-between">
				<div class=" self-center text-xs font-medium">{$i18n.t('Theme')}</div>
				<div class="flex items-center relative">
					<select
						class="dark:bg-gray-900 w-fit pr-8 rounded-sm py-2 px-2 text-xs bg-transparent text-right {$settings.highContrastMode
							? ''
							: 'outline-hidden'}"
						bind:value={selectedTheme}
						placeholder={$i18n.t('Select a theme')}
						onchange={() => themeChangeHandler(selectedTheme)}
					>
						<option value="system">⚙️ {$i18n.t('System')}</option>
						<option value="dark">🌑 {$i18n.t('Dark')}</option>
						<option value="oled-dark">🌃 {$i18n.t('OLED Dark')}</option>
						<option value="light">☀️ {$i18n.t('Light')}</option>
						<option value="her">🌷 Her</option>
						<!-- <option value="rose-pine dark">🪻 {$i18n.t('Rosé Pine')}</option>
						<option value="rose-pine-dawn light">🌷 {$i18n.t('Rosé Pine Dawn')}</option> -->
					</select>
				</div>
			</div>

			<div class="flex w-full justify-between">
				<div class=" self-center text-xs font-medium">{$i18n.t('Font')}</div>
				<div class="flex items-center relative">
					<select
						class="dark:bg-gray-900 w-fit pr-8 rounded-sm py-2 px-2 text-xs bg-transparent text-right {$settings.highContrastMode
							? ''
							: 'outline-hidden'}"
						bind:value={selectedFont}
						placeholder={$i18n.t('Select a font')}
						onchange={() => fontChangeHandler(selectedFont)}
					>
						<option value="system">{$i18n.t('System (SF Pro on macOS)')}</option>
						<option value="inter">Inter</option>
						<option value="mona">Mona Sans</option>
					</select>
				</div>
			</div>

			<div class=" flex w-full justify-between">
				<div class=" self-center text-xs font-medium">{$i18n.t('Language')}</div>
				<div class="flex items-center relative">
					<select
						class="dark:bg-gray-900 w-fit pr-8 rounded-sm py-2 px-2 text-xs bg-transparent text-right {$settings.highContrastMode
							? ''
							: 'outline-hidden'}"
						bind:value={lang}
						placeholder={$i18n.t('Select a language')}
						onchange={(e) => {
							changeLanguage(lang);
						}}
					>
						{#each languages as language}
							<option value={language['code']}>{language['title']}</option>
						{/each}
					</select>
				</div>
			</div>
			{#if $i18n.language === 'en-US' && !($config?.license_metadata ?? false)}
				<div
					class="mb-2 text-xs {($settings?.highContrastMode ?? false)
						? 'text-gray-800 dark:text-gray-100'
						: 'text-gray-400 dark:text-gray-500'}"
				>
					Couldn't find your language?
					<a
						class="font-medium underline {($settings?.highContrastMode ?? false)
							? 'text-gray-700 dark:text-gray-200'
							: 'text-gray-300'}"
						href="https://github.com/open-webui/open-webui/blob/main/docs/CONTRIBUTING.md#-translations-and-internationalization"
						target="_blank"
					>
						Help us translate Open WebUI!
					</a>
				</div>
			{/if}

			<div>
				<div class=" py-0.5 flex w-full justify-between">
					<div class=" self-center text-xs font-medium">{$i18n.t('Notifications')}</div>

					<button
						class="py-2 px-3 text-xs flex rounded-sm transition"
						onclick={() => {
							toggleNotification();
						}}
						type="button"
					>
						{#if notificationEnabled === true}
							<span class="ml-2 self-center">{$i18n.t('On')}</span>
						{:else}
							<span class="ml-2 self-center">{$i18n.t('Off')}</span>
						{/if}
					</button>
				</div>
			</div>

			{#if $config?.features?.enable_automations}
				<div>
					<div class=" py-0.5 flex w-full justify-between">
						<div class=" self-center text-xs font-medium">
							{$i18n.t('Push Notifications')}
						</div>

						{#if pushSupported}
							<button
								class="py-2 px-3 text-xs flex rounded-sm transition"
								onclick={() => {
									togglePushNotification();
								}}
								type="button"
							>
								{#if pushNotificationEnabled === true}
									<span class="ml-2 self-center">{$i18n.t('On')}</span>
								{:else}
									<span class="ml-2 self-center">{$i18n.t('Off')}</span>
								{/if}
							</button>
						{:else}
							<span class="py-2 px-3 text-xs self-center text-gray-500">
								{$i18n.t('Unavailable')}
							</span>
						{/if}
					</div>
					<div class=" text-xs text-gray-500 dark:text-gray-500">
						{#if pushSupported}
							{$i18n.t('Get notified when an automation finishes, even with the app closed.')}
						{:else}
							{$i18n.t(
								'Push needs an HTTPS connection. On iPhone and iPad, add this app to your Home Screen first.'
							)}
						{/if}
					</div>
				</div>
			{/if}
		</div>

		{#if $user?.role === 'admin' || (($user?.permissions.chat?.controls ?? true) && ($user?.permissions.chat?.system_prompt ?? true))}
			<hr class="border-gray-100/50 dark:border-gray-850 my-3" />

			<div>
				<div class=" my-2.5 text-sm font-medium">{$i18n.t('System Prompt')}</div>
				<Textarea
					bind:value={system}
					className={'w-full text-sm outline-hidden resize-vertical' +
						($settings.highContrastMode
							? ' p-2.5 border-2 border-gray-300 dark:border-gray-700 rounded-lg bg-transparent text-gray-900 dark:text-gray-100 focus:ring-1 focus:ring-book-cloth focus:border-book-cloth overflow-y-hidden'
							: '  dark:text-gray-300 ')}
					rows="4"
					placeholder={$i18n.t('Enter system prompt here')}
				/>
			</div>
		{/if}

		{#if $user?.role === 'admin' || (($user?.permissions.chat?.controls ?? true) && ($user?.permissions.chat?.params ?? true))}
			<div class="mt-2 space-y-3 pr-1.5">
				<div class="flex justify-between items-center text-sm">
					<div class="  font-medium">{$i18n.t('Advanced Parameters')}</div>
					<button
						class=" text-xs font-medium {($settings?.highContrastMode ?? false)
							? 'text-gray-800 dark:text-gray-100'
							: 'text-gray-400 dark:text-gray-500'}"
						type="button"
						onclick={() => {
							showAdvanced = !showAdvanced;
						}}>{showAdvanced ? $i18n.t('Hide') : $i18n.t('Show')}</button
					>
				</div>

				{#if showAdvanced}
					<AdvancedParams admin={$user?.role === 'admin'} bind:params />
				{/if}
			</div>
		{/if}
	</div>

	<div class="flex justify-end pt-3 text-sm font-medium">
		<button
			class="px-3.5 py-1.5 text-sm font-medium bg-book-cloth hover:bg-kraft text-white dark:bg-book-cloth dark:text-white dark:hover:bg-kraft transition-colors duration-200 ease-paper rounded-full"
			onclick={() => {
				saveHandler();
			}}
		>
			{$i18n.t('Save')}
		</button>
	</div>
</div>
