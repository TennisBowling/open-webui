<script lang="ts">
	import { dispatchComponentEvent } from '$lib/utils/componentEvents';
	import { preventDefault } from '$lib/utils/eventModifiers';

	import { config, models, settings, user } from '$lib/stores';
	import { onMount, getContext } from 'svelte';
	import { toast } from '$lib/utils/toast';
	import Tooltip from '$lib/components/common/Tooltip.svelte';
	import { updateUserInfo } from '$lib/apis/users';
	import { getUserPosition } from '$lib/utils';
	import Minus from '$lib/components/icons/Minus.svelte';
	import Plus from '$lib/components/icons/Plus.svelte';
	import Switch from '$lib/components/common/Switch.svelte';
	import ManageImageCompressionModal from './Interface/ManageImageCompressionModal.svelte';
	const dispatch = (type: string, detail?: unknown) =>
		dispatchComponentEvent(eventProps, type, detail);

	const i18n = getContext('i18n');

	interface Props {
		saveSettings: Function;
	}

	let { saveSettings, ...eventProps }: Props & Record<string, unknown> = $props();

	let backgroundImageUrl = $state(null);
	let inputFiles = $state(null);
	let filesInputElement = $state();

	// Addons
	let titleAutoGenerate = $state(true);
	let autoFollowUps = $state(true);
	let autoTags = $state(true);

	let responseAutoCopy = $state(false);
	let widescreenMode = $state(false);
	let splitLargeChunks = false;
	let scrollOnBranchChange = $state(true);
	let userLocation = $state(false);

	// Interface
	let defaultModelId = '';
	let showUsername = $state(false);

	let notificationSound = $state(true);
	let notificationSoundAlways = $state(false);

	let highContrastMode = $state(false);

	let detectArtifacts = $state(true);
	let displayMultiModelResponsesInTabs = $state(false);

	let richTextInput = $state(true);
	let showFormattingToolbar = $state(false);
	let insertPromptAsRichText = $state(false);
	let promptAutocomplete = $state(false);

	let largeTextAsFile = $state(false);

	let insertSuggestionPrompt = $state(false);
	let keepFollowUpPrompts = $state(false);
	let insertFollowUpPrompt = $state(false);

	let regenerateMenu = $state(true);

	let landingPageMode = $state('');
	let chatBubble = $state(true);
	let chatDirection: 'LTR' | 'RTL' | 'auto' = $state('auto');
	let ctrlEnterToSend = $state(false);
	let copyFormatted = $state(false);

	let temporaryChatByDefault = $state(false);
	let chatFadeStreamingText = $state(true);
	let collapseCodeBlocks = $state(false);
	let expandDetails = $state(false);
	let autoExpandReasoningDuringStreaming = $state(false);
	let bundleAgenticSteps = $state(true);
	let agenticStepsAutoExpand = $state(true);
	let showChatTitleInTab = $state(true);


	let imageCompression = $state(false);
	let imageCompressionSize = $state({
		width: '',
		height: ''
	});
	let imageCompressionInChannels = $state(true);

	// chat export
	let stylizedPdfExport = $state(true);

	// Admin - Show Update Available Toast
	let showUpdateToast = $state(true);
	let showChangelog = $state(true);

	let showEmojiInCall = $state(false);
	let voiceInterruption = $state(false);
	let hapticFeedback = $state(false);

	let webSearch = $state(null);

	let iframeSandboxAllowSameOrigin = $state(false);
	let iframeSandboxAllowForms = $state(false);

	let showManageImageCompressionModal = $state(false);

	const toggleLandingPageMode = async () => {
		landingPageMode = landingPageMode === '' ? 'chat' : '';
		saveSettings({ landingPageMode: landingPageMode });
	};

	const toggleUserLocation = async () => {
		if (userLocation) {
			const position = await getUserPosition().catch((error) => {
				toast.error(error.message);
				return null;
			});

			if (position) {
				await updateUserInfo(localStorage.token, { location: position });
				toast.success($i18n.t('User location successfully retrieved.'));
			} else {
				userLocation = false;
			}
		}

		saveSettings({ userLocation });
	};

	const toggleTitleAutoGenerate = async () => {
		saveSettings({
			title: {
				...$settings.title,
				auto: titleAutoGenerate
			}
		});
	};

	const toggleResponseAutoCopy = async () => {
		const permission = await navigator.clipboard
			.readText()
			.then(() => {
				return 'granted';
			})
			.catch(() => {
				return '';
			});

		if (permission === 'granted') {
			saveSettings({ responseAutoCopy: responseAutoCopy });
		} else {
			responseAutoCopy = false;
			toast.error(
				$i18n.t(
					'Clipboard write permission denied. Please check your browser settings to grant the necessary access.'
				)
			);
		}
	};

	const toggleChangeChatDirection = async () => {
		if (chatDirection === 'auto') {
			chatDirection = 'LTR';
		} else if (chatDirection === 'LTR') {
			chatDirection = 'RTL';
		} else if (chatDirection === 'RTL') {
			chatDirection = 'auto';
		}
		saveSettings({ chatDirection });
	};

	const togglectrlEnterToSend = async () => {
		ctrlEnterToSend = !ctrlEnterToSend;
		saveSettings({ ctrlEnterToSend });
	};

	const updateInterfaceHandler = async () => {
		return await saveSettings({
			models: [defaultModelId],
			imageCompressionSize: imageCompressionSize
		});
	};

	const toggleWebSearch = async () => {
		webSearch = webSearch === null ? 'always' : null;
		saveSettings({ webSearch: webSearch });
	};

	onMount(async () => {
		titleAutoGenerate = $settings?.title?.auto ?? true;
		autoTags = $settings?.autoTags ?? true;
		autoFollowUps = $settings?.autoFollowUps ?? true;

		highContrastMode = $settings?.highContrastMode ?? false;

		detectArtifacts = $settings?.detectArtifacts ?? true;
		responseAutoCopy = $settings?.responseAutoCopy ?? false;

		showUsername = $settings?.showUsername ?? false;
		showUpdateToast = $settings?.showUpdateToast ?? true;
		showChangelog = $settings?.showChangelog ?? true;

		showEmojiInCall = $settings?.showEmojiInCall ?? false;
		voiceInterruption = $settings?.voiceInterruption ?? false;

		displayMultiModelResponsesInTabs = $settings?.displayMultiModelResponsesInTabs ?? false;
		chatFadeStreamingText = $settings?.chatFadeStreamingText ?? true;

		richTextInput = $settings?.richTextInput ?? true;
		showFormattingToolbar = $settings?.showFormattingToolbar ?? false;
		insertPromptAsRichText = $settings?.insertPromptAsRichText ?? false;
		promptAutocomplete = $settings?.promptAutocomplete ?? false;

		insertSuggestionPrompt = $settings?.insertSuggestionPrompt ?? false;
		keepFollowUpPrompts = $settings?.keepFollowUpPrompts ?? false;
		insertFollowUpPrompt = $settings?.insertFollowUpPrompt ?? false;

		regenerateMenu = $settings?.regenerateMenu ?? true;

		largeTextAsFile = $settings?.largeTextAsFile ?? false;
		copyFormatted = $settings?.copyFormatted ?? false;

		collapseCodeBlocks = $settings?.collapseCodeBlocks ?? false;
		expandDetails = $settings?.expandDetails ?? false;
		autoExpandReasoningDuringStreaming = $settings?.autoExpandReasoningDuringStreaming ?? false;
		bundleAgenticSteps = $settings?.bundleAgenticSteps ?? true;
		agenticStepsAutoExpand = $settings?.agenticStepsAutoExpand ?? true;

		landingPageMode = $settings?.landingPageMode ?? '';
		chatBubble = $settings?.chatBubble ?? true;
		widescreenMode = $settings?.widescreenMode ?? false;
		splitLargeChunks = $settings?.splitLargeChunks ?? false;
		scrollOnBranchChange = $settings?.scrollOnBranchChange ?? true;

		temporaryChatByDefault = $settings?.temporaryChatByDefault ?? false;
		chatDirection = $settings?.chatDirection ?? 'auto';
		userLocation = $settings?.userLocation ?? false;
		showChatTitleInTab = $settings?.showChatTitleInTab ?? true;

		notificationSound = $settings?.notificationSound ?? true;
		notificationSoundAlways = $settings?.notificationSoundAlways ?? false;

		iframeSandboxAllowSameOrigin = $settings?.iframeSandboxAllowSameOrigin ?? false;
		iframeSandboxAllowForms = $settings?.iframeSandboxAllowForms ?? false;

		stylizedPdfExport = $settings?.stylizedPdfExport ?? true;

		hapticFeedback = $settings?.hapticFeedback ?? false;
		ctrlEnterToSend = $settings?.ctrlEnterToSend ?? false;


		imageCompression = $settings?.imageCompression ?? false;
		imageCompressionSize = $settings?.imageCompressionSize ?? { width: '', height: '' };
		imageCompressionInChannels = $settings?.imageCompressionInChannels ?? true;

		// A user's saved default takes precedence over the instance-wide admin
		// default, matching the new-chat resolution order in Chat.svelte.
		defaultModelId =
			$settings?.models?.find((modelId) => modelId) ??
			$config?.default_models?.split(',').find((modelId) => modelId) ??
			'';

		backgroundImageUrl = $settings?.backgroundImageUrl ?? null;
		webSearch = $settings?.webSearch ?? null;
	});
</script>

<ManageImageCompressionModal
	bind:show={showManageImageCompressionModal}
	size={imageCompressionSize}
	onSave={(size) => {
		saveSettings({ imageCompressionSize: size });
	}}
/>

<form
	id="tab-interface"
	class="flex flex-col h-full justify-between space-y-3 text-sm"
	onsubmit={preventDefault(async () => {
		if ((await updateInterfaceHandler()) !== false) {
			dispatch('save');
		}
	})}
>
	<input
		bind:this={filesInputElement}
		bind:files={inputFiles}
		type="file"
		hidden
		accept="image/*"
		onchange={() => {
			let reader = new FileReader();
			reader.onload = (event) => {
				let originalImageUrl = `${event.target.result}`;

				backgroundImageUrl = originalImageUrl;
				saveSettings({ backgroundImageUrl });
			};

			if (
				inputFiles &&
				inputFiles.length > 0 &&
				['image/gif', 'image/webp', 'image/jpeg', 'image/png'].includes(inputFiles[0]['type'])
			) {
				reader.readAsDataURL(inputFiles[0]);
			} else {
				console.log(`Unsupported File Type '${inputFiles[0]['type']}'.`);
				inputFiles = null;
			}
		}}
	/>

	<div class=" space-y-3 overflow-y-scroll max-h-[28rem] md:max-h-full">
		<div>
			<h1 class=" mb-2 text-sm font-medium">{$i18n.t('UI')}</h1>

			<div>
				<div class=" py-0.5 flex w-full justify-between">
					<div id="high-contrast-mode-label" class=" self-center text-xs">
						{$i18n.t('High Contrast Mode')} ({$i18n.t('Beta')})
					</div>

					<div class="flex items-center gap-2 p-1">
						<Switch
							ariaLabelledbyId="high-contrast-mode-label"
							tooltip={true}
							bind:state={highContrastMode}
							onchange={() => {
								saveSettings({ highContrastMode });
							}}
						/>
					</div>
				</div>
			</div>

			<div>
				<div class=" py-0.5 flex w-full justify-between">
					<div id="use-chat-title-as-tab-title-label" class=" self-center text-xs">
						{$i18n.t('Display chat title in tab')}
					</div>

					<div class="flex items-center gap-2 p-1">
						<Switch
							ariaLabelledbyId="use-chat-title-as-tab-title-label"
							tooltip={true}
							bind:state={showChatTitleInTab}
							onchange={() => {
								saveSettings({ showChatTitleInTab });
							}}
						/>
					</div>
				</div>
			</div>

			<div>
				<div class="py-0.5 flex w-full justify-between">
					<div id="notification-sound-label" class=" self-center text-xs">
						{$i18n.t('Notification Sound')}
					</div>

					<div class="flex items-center gap-2 p-1">
						<Switch
							ariaLabelledbyId="notification-sound-label"
							tooltip={true}
							bind:state={notificationSound}
							onchange={() => {
								saveSettings({ notificationSound });
							}}
						/>
					</div>
				</div>
			</div>

			{#if notificationSound}
				<div>
					<div class=" py-0.5 flex w-full justify-between">
						<div id="play-notification-sound-label" class=" self-center text-xs">
							{$i18n.t('Always Play Notification Sound')}
						</div>

						<div class="flex items-center gap-2 p-1">
							<Switch
								ariaLabelledbyId="play-notification-sound-label"
								tooltip={true}
								bind:state={notificationSoundAlways}
								onchange={() => {
									saveSettings({ notificationSoundAlways });
								}}
							/>
						</div>
					</div>
				</div>
			{/if}

			<div>
				<div id="allow-user-location-label" class=" py-0.5 flex w-full justify-between">
					<div class=" self-center text-xs">{$i18n.t('Allow User Location')}</div>

					<div class="flex items-center gap-2 p-1">
						<Switch
							ariaLabelledbyId="allow-user-location-label"
							tooltip={true}
							bind:state={userLocation}
							onchange={() => {
								toggleUserLocation();
							}}
						/>
					</div>
				</div>
			</div>

			<div>
				<div class=" py-0.5 flex w-full justify-between">
					<div id="haptic-feedback-label" class=" self-center text-xs">
						{$i18n.t('Haptic Feedback')} ({$i18n.t('Android')})
					</div>

					<div class="flex items-center gap-2 p-1">
						<Switch
							ariaLabelledbyId="haptic-feedback-label"
							tooltip={true}
							bind:state={hapticFeedback}
							onchange={() => {
								saveSettings({ hapticFeedback });
							}}
						/>
					</div>
				</div>
			</div>

			<div>
				<div class=" py-0.5 flex w-full justify-between">
					<div id="copy-formatted-label" class=" self-center text-xs">
						{$i18n.t('Copy Formatted Text')}
					</div>

					<div class="flex items-center gap-2 p-1">
						<Switch
							ariaLabelledbyId="copy-formatted-label"
							tooltip={true}
							bind:state={copyFormatted}
							onchange={() => {
								saveSettings({ copyFormatted });
							}}
						/>
					</div>
				</div>
			</div>

			{#if $user?.role === 'admin'}
				<div>
					<div class=" py-0.5 flex w-full justify-between">
						<div id="toast-notifications-label" class=" self-center text-xs">
							{$i18n.t('Toast notifications for new updates')}
						</div>

						<div class="flex items-center gap-2 p-1">
							<Switch
								ariaLabelledbyId="toast-notifications-label"
								tooltip={true}
								bind:state={showUpdateToast}
								onchange={() => {
									saveSettings({ showUpdateToast });
								}}
							/>
						</div>
					</div>
				</div>

				<div>
					<div class=" py-0.5 flex w-full justify-between">
						<div id="whats-new-label" class=" self-center text-xs">
							{$i18n.t(`Show "What's New" modal on login`)}
						</div>

						<div class="flex items-center gap-2 p-1">
							<Switch
								ariaLabelledbyId="whats-new-label"
								tooltip={true}
								bind:state={showChangelog}
								onchange={() => {
									saveSettings({ showChangelog });
								}}
							/>
						</div>
					</div>
				</div>
			{/if}

			<div class=" my-2 text-sm font-medium">{$i18n.t('Chat')}</div>

			<div>
				<div class=" py-0.5 flex w-full justify-between">
					<div id="chat-direction-label" class=" self-center text-xs">
						{$i18n.t('Chat direction')}
					</div>

					<button
						aria-labelledby="chat-direction-label chat-direction-mode"
						class="p-1 px-3 text-xs flex rounded-sm transition"
						onclick={toggleChangeChatDirection}
						type="button"
					>
						<span class="ml-2 self-center" id="chat-direction-mode">
							{chatDirection === 'LTR'
								? $i18n.t('LTR')
								: chatDirection === 'RTL'
									? $i18n.t('RTL')
									: $i18n.t('Auto')}
						</span>
					</button>
				</div>
			</div>

			<div>
				<div class=" py-0.5 flex w-full justify-between">
					<div id="landing-page-mode-label" class=" self-center text-xs">
						{$i18n.t('Landing Page Mode')}
					</div>

					<button
						aria-labelledby="landing-page-mode-label notification-sound-state"
						class="p-1 px-3 text-xs flex rounded-sm transition"
						onclick={() => {
							toggleLandingPageMode();
						}}
						type="button"
					>
						<span class="ml-2 self-center" id="notification-sound-state"
							>{landingPageMode === '' ? $i18n.t('Default') : $i18n.t('Chat')}</span
						>
					</button>
				</div>
			</div>

			<div>
				<div class=" py-0.5 flex w-full justify-between">
					<div id="chat-background-label" class=" self-center text-xs">
						{$i18n.t('Chat Background Image')}
					</div>

					<button
						aria-labelledby="chat-background-label background-image-url-state"
						class="p-1 px-3 text-xs flex rounded-sm transition"
						onclick={() => {
							if (backgroundImageUrl !== null) {
								backgroundImageUrl = null;
								saveSettings({ backgroundImageUrl });
							} else {
								filesInputElement.click();
							}
						}}
						type="button"
					>
						<span class="ml-2 self-center" id="background-image-url-state"
							>{backgroundImageUrl !== null ? $i18n.t('Reset') : $i18n.t('Upload')}</span
						>
					</button>
				</div>
			</div>

			<div>
				<div class=" py-0.5 flex w-full justify-between">
					<div id="chat-bubble-ui-label" class=" self-center text-xs">
						{$i18n.t('Chat Bubble UI')}
					</div>

					<div class="flex items-center gap-2 p-1">
						<Switch
							tooltip={true}
							ariaLabelledbyId="chat-bubble-ui-label"
							bind:state={chatBubble}
							onchange={() => {
								saveSettings({ chatBubble });
							}}
						/>
					</div>
				</div>
			</div>

			{#if !$settings.chatBubble}
				<div>
					<div class=" py-0.5 flex w-full justify-between">
						<div id="chat-bubble-username-label" class=" self-center text-xs">
							{$i18n.t('Display the username instead of You in the Chat')}
						</div>

						<div class="flex items-center gap-2 p-1">
							<Switch
								ariaLabelledbyId="chat-bubble-username-label"
								tooltip={true}
								bind:state={showUsername}
								onchange={() => {
									saveSettings({ showUsername });
								}}
							/>
						</div>
					</div>
				</div>
			{/if}

			<div>
				<div class=" py-0.5 flex w-full justify-between">
					<div id="widescreen-mode-label" class=" self-center text-xs">
						{$i18n.t('Widescreen Mode')}
					</div>

					<div class="flex items-center gap-2 p-1">
						<Switch
							ariaLabelledbyId="widescreen-mode-label"
							tooltip={true}
							bind:state={widescreenMode}
							onchange={() => {
								saveSettings({ widescreenMode });
							}}
						/>
					</div>
				</div>
			</div>

			<div>
				<div class=" py-0.5 flex w-full justify-between">
					<div id="temp-chat-default-label" class=" self-center text-xs">
						{$i18n.t('Temporary Chat by Default')}
					</div>

					<div class="flex items-center gap-2 p-1">
						<Switch
							ariaLabelledbyId="temp-chat-default-label"
							tooltip={true}
							bind:state={temporaryChatByDefault}
							onchange={() => {
								saveSettings({ temporaryChatByDefault });
							}}
						/>
					</div>
				</div>
			</div>

			<div>
				<div class=" py-0.5 flex w-full justify-between">
					<div id="fade-streaming-label" class=" self-center text-xs">
						{$i18n.t('Fade Effect for Streaming Text')}
					</div>

					<div class="flex items-center gap-2 p-1">
						<Switch
							ariaLabelledbyId="fade-streaming-label"
							tooltip={true}
							bind:state={chatFadeStreamingText}
							onchange={() => {
								saveSettings({ chatFadeStreamingText });
							}}
						/>
					</div>
				</div>
			</div>

			<div>
				<div class=" py-0.5 flex w-full justify-between">
					<div id="auto-generation-label" class=" self-center text-xs">
						{$i18n.t('Title Auto-Generation')}
					</div>

					<div class="flex items-center gap-2 p-1">
						<Switch
							ariaLabelledbyId="auto-generation-label"
							tooltip={true}
							bind:state={titleAutoGenerate}
							onchange={() => {
								toggleTitleAutoGenerate();
							}}
						/>
					</div>
				</div>
			</div>

			<div>
				<div class=" py-0.5 flex w-full justify-between">
					<div class=" self-center text-xs" id="follow-up-auto-generation-label">
						{$i18n.t('Follow-Up Auto-Generation')}
					</div>

					<div class="flex items-center gap-2 p-1">
						<Switch
							ariaLabelledbyId="follow-up-auto-generation-label"
							tooltip={true}
							bind:state={autoFollowUps}
							onchange={() => {
								saveSettings({ autoFollowUps });
							}}
						/>
					</div>
				</div>
			</div>

			<div>
				<div class=" py-0.5 flex w-full justify-between">
					<div id="chat-tags-label" class=" self-center text-xs">
						{$i18n.t('Chat Tags Auto-Generation')}
					</div>

					<div class="flex items-center gap-2 p-1">
						<Switch
							ariaLabelledbyId="chat-tags-label"
							tooltip={true}
							bind:state={autoTags}
							onchange={() => {
								saveSettings({ autoTags });
							}}
						/>
					</div>
				</div>
			</div>

			<div>
				<div class=" py-0.5 flex w-full justify-between">
					<div id="auto-copy-label" class=" self-center text-xs">
						{$i18n.t('Auto-Copy Response to Clipboard')}
					</div>

					<div class="flex items-center gap-2 p-1">
						<Switch
							ariaLabelledbyId="auto-copy-label"
							tooltip={true}
							bind:state={responseAutoCopy}
							onchange={() => {
								toggleResponseAutoCopy();
							}}
						/>
					</div>
				</div>
			</div>

			<div>
				<div class=" py-0.5 flex w-full justify-between">
					<div id="insert-suggestion-prompt-label" class=" self-center text-xs">
						{$i18n.t('Insert Suggestion Prompt to Input')}
					</div>

					<div class="flex items-center gap-2 p-1">
						<Switch
							ariaLabelledbyId="insert-suggestion-prompt-label"
							tooltip={true}
							bind:state={insertSuggestionPrompt}
							onchange={() => {
								saveSettings({ insertSuggestionPrompt });
							}}
						/>
					</div>
				</div>
			</div>

			<div>
				<div class=" py-0.5 flex w-full justify-between">
					<div id="keep-follow-up-prompts-label" class=" self-center text-xs">
						{$i18n.t('Keep Follow-Up Prompts in Chat')}
					</div>

					<div class="flex items-center gap-2 p-1">
						<Switch
							ariaLabelledbyId="keep-follow-up-prompts-label"
							tooltip={true}
							bind:state={keepFollowUpPrompts}
							onchange={() => {
								saveSettings({ keepFollowUpPrompts });
							}}
						/>
					</div>
				</div>
			</div>

			<div>
				<div class=" py-0.5 flex w-full justify-between">
					<div id="insert-follow-up-prompt-label" class=" self-center text-xs">
						{$i18n.t('Insert Follow-Up Prompt to Input')}
					</div>

					<div class="flex items-center gap-2 p-1">
						<Switch
							ariaLabelledbyId="insert-follow-up-prompt-label"
							tooltip={true}
							bind:state={insertFollowUpPrompt}
							onchange={() => {
								saveSettings({ insertFollowUpPrompt });
							}}
						/>
					</div>
				</div>
			</div>

			<div>
				<div class=" py-0.5 flex w-full justify-between">
					<div id="regenerate-menu-label" class=" self-center text-xs">
						{$i18n.t('Regenerate Menu')}
					</div>

					<div class="flex items-center gap-2 p-1">
						<Switch
							ariaLabelledbyId="regenerate-menu-label"
							tooltip={true}
							bind:state={regenerateMenu}
							onchange={() => {
								saveSettings({ regenerateMenu });
							}}
						/>
					</div>
				</div>
			</div>

			<div>
				<div class=" py-0.5 flex w-full justify-between">
					<div id="always-collapse-label" class=" self-center text-xs">
						{$i18n.t('Always Collapse Code Blocks')}
					</div>

					<div class="flex items-center gap-2 p-1">
						<Switch
							ariaLabelledbyId="always-collapse-label"
							tooltip={true}
							bind:state={collapseCodeBlocks}
							onchange={() => {
								saveSettings({ collapseCodeBlocks });
							}}
						/>
					</div>
				</div>
			</div>

			<div>
				<div class=" py-0.5 flex w-full justify-between">
					<div id="always-expand-label" class=" self-center text-xs">
						{$i18n.t('Always Expand Details')}
					</div>

					<div class="flex items-center gap-2 p-1">
						<Switch
							ariaLabelledbyId="always-expand-label"
							tooltip={true}
							bind:state={expandDetails}
							onchange={() => {
								saveSettings({ expandDetails });
							}}
						/>
					</div>
				</div>
			</div>

			<div>
				<div class=" py-0.5 flex w-full justify-between">
					<div id="auto-expand-reasoning-streaming-label" class=" self-center text-xs">
						{$i18n.t('Auto-Expand Reasoning During Streaming')}
					</div>

					<div class="flex items-center gap-2 p-1">
						<Switch
							ariaLabelledbyId="auto-expand-reasoning-streaming-label"
							tooltip={true}
							bind:state={autoExpandReasoningDuringStreaming}
							onchange={() => {
								saveSettings({ autoExpandReasoningDuringStreaming });
							}}
						/>
					</div>
				</div>
			</div>

			<div>
				<div class=" py-0.5 flex w-full justify-between">
					<div id="bundle-agentic-steps-label" class=" self-center text-xs">
						{$i18n.t('Bundle Agentic Steps')}
					</div>

					<div class="flex items-center gap-2 p-1">
						<Switch
							ariaLabelledbyId="bundle-agentic-steps-label"
							tooltip={true}
							bind:state={bundleAgenticSteps}
							onchange={() => {
								saveSettings({ bundleAgenticSteps });
							}}
						/>
					</div>
				</div>
			</div>

			{#if bundleAgenticSteps}
				<div>
					<div class=" py-0.5 flex w-full justify-between">
						<div id="agentic-steps-auto-expand-label" class=" self-center text-xs">
							{$i18n.t('Auto-Expand Agentic Steps While Working')}
						</div>

						<div class="flex items-center gap-2 p-1">
							<Switch
								ariaLabelledbyId="agentic-steps-auto-expand-label"
								tooltip={true}
								bind:state={agenticStepsAutoExpand}
								onchange={() => {
									saveSettings({ agenticStepsAutoExpand });
								}}
							/>
						</div>
					</div>
				</div>
			{/if}

			<div>
				<div class=" py-0.5 flex w-full justify-between">
					<div id="keep-followup-prompts-label" class=" self-center text-xs">
						{$i18n.t('Display Multi-model Responses in Tabs')}
					</div>

					<div class="flex items-center gap-2 p-1">
						<Switch
							ariaLabelledbyId="keep-followup-prompts-label"
							tooltip={true}
							bind:state={displayMultiModelResponsesInTabs}
							onchange={() => {
								saveSettings({ displayMultiModelResponsesInTabs });
							}}
						/>
					</div>
				</div>
			</div>

			<div>
				<div class=" py-0.5 flex w-full justify-between">
					<div id="scroll-on-branch-change-label" class=" self-center text-xs">
						{$i18n.t('Scroll On Branch Change')}
					</div>

					<div class="flex items-center gap-2 p-1">
						<Switch
							ariaLabelledbyId="scroll-on-branch-change-label"
							tooltip={true}
							bind:state={scrollOnBranchChange}
							onchange={() => {
								saveSettings({ scrollOnBranchChange });
							}}
						/>
					</div>
				</div>
			</div>

			<div>
				<div class=" py-0.5 flex w-full justify-between">
					<div id="stylized-pdf-export-label" class=" self-center text-xs">
						{$i18n.t('Stylized PDF Export')}
					</div>

					<div class="flex items-center gap-2 p-1">
						<Switch
							ariaLabelledbyId="stylized-pdf-export-label"
							tooltip={true}
							bind:state={stylizedPdfExport}
							onchange={() => {
								saveSettings({ stylizedPdfExport });
							}}
						/>
					</div>
				</div>
			</div>

			<div>
				<div class=" py-0.5 flex w-full justify-between">
					<div id="web-search-in-chat-label" class=" self-center text-xs">
						{$i18n.t('Web Search in Chat')}
					</div>

					<button
						aria-labelledby="web-search-in-chat-label web-search-state"
						class="p-1 px-3 text-xs flex rounded-sm transition"
						onclick={() => {
							toggleWebSearch();
						}}
						type="button"
					>
						<span class="ml-2 self-center" id="web-search-state"
							>{webSearch === 'always' ? $i18n.t('Always') : $i18n.t('Default')}</span
						>
					</button>
				</div>
			</div>

			<div class=" my-2 text-sm font-medium">{$i18n.t('Input')}</div>

			<div>
				<div class=" py-0.5 flex w-full justify-between">
					<div id="enter-key-behavior-label ctrl-enter-to-send-state" class=" self-center text-xs">
						{$i18n.t('Enter Key Behavior')}
					</div>

					<button
						aria-labelledby="enter-key-behavior-label"
						class="p-1 px-3 text-xs flex rounded transition"
						onclick={() => {
							togglectrlEnterToSend();
						}}
						type="button"
					>
						<span class="ml-2 self-center" id="ctrl-enter-to-send-state"
							>{ctrlEnterToSend === true
								? $i18n.t('Ctrl+Enter to Send')
								: $i18n.t('Enter to Send')}</span
						>
					</button>
				</div>
			</div>

			<div>
				<div class=" py-0.5 flex w-full justify-between">
					<div id="rich-input-label" class=" self-center text-xs">
						{$i18n.t('Rich Text Input for Chat')}
					</div>

					<div class="flex items-center gap-2 p-1">
						<Switch
							tooltip={true}
							ariaLabelledbyId="rich-input-label"
							bind:state={richTextInput}
							onchange={() => {
								saveSettings({ richTextInput });
							}}
						/>
					</div>
				</div>
			</div>

			{#if richTextInput}
				<div>
					<div class=" py-0.5 flex w-full justify-between">
						<div id="show-formatting-toolbar-label" class=" self-center text-xs">
							{$i18n.t('Show Formatting Toolbar')}
						</div>

						<div class="flex items-center gap-2 p-1">
							<Switch
								ariaLabelledbyId="show-formatting-toolbar-label"
								tooltip={true}
								bind:state={showFormattingToolbar}
								onchange={() => {
									saveSettings({ showFormattingToolbar });
								}}
							/>
						</div>
					</div>
				</div>

				<div>
					<div class=" py-0.5 flex w-full justify-between">
						<div id="insert-prompt-as-rich-text-label" class=" self-center text-xs">
							{$i18n.t('Insert Prompt as Rich Text')}
						</div>

						<div class="flex items-center gap-2 p-1">
							<Switch
								ariaLabelledbyId="insert-prompt-as-rich-text-label"
								tooltip={true}
								bind:state={insertPromptAsRichText}
								onchange={() => {
									saveSettings({ insertPromptAsRichText });
								}}
							/>
						</div>
					</div>
				</div>

				{#if $config?.features?.enable_autocomplete_generation}
					<div>
						<div class=" py-0.5 flex w-full justify-between">
							<div id="prompt-autocompletion-label" class=" self-center text-xs">
								{$i18n.t('Prompt Autocompletion')}
							</div>

							<div class="flex items-center gap-2 p-1">
								<Switch
									ariaLabelledbyId="prompt-autocompletion-label"
									tooltip={true}
									bind:state={promptAutocomplete}
									onchange={() => {
										saveSettings({ promptAutocomplete });
									}}
								/>
							</div>
						</div>
					</div>
				{/if}
			{/if}

			<div>
				<div class=" py-0.5 flex w-full justify-between">
					<div id="paste-large-label" class=" self-center text-xs">
						{$i18n.t('Paste Large Text as File')}
					</div>

					<div class="flex items-center gap-2 p-1">
						<Switch
							tooltip={true}
							ariaLabelledbyId="paste-large-label"
							bind:state={largeTextAsFile}
							onchange={() => {
								saveSettings({ largeTextAsFile });
							}}
						/>
					</div>
				</div>
			</div>

			<div class=" my-2 text-sm font-medium">{$i18n.t('Artifacts')}</div>

			<div>
				<div class=" py-0.5 flex w-full justify-between">
					<div id="detect-artifacts-label" class=" self-center text-xs">
						{$i18n.t('Detect Artifacts Automatically')}
					</div>

					<div class="flex items-center gap-2 p-1">
						<Switch
							ariaLabelledbyId="detect-artifacts-label"
							tooltip={true}
							bind:state={detectArtifacts}
							onchange={() => {
								saveSettings({ detectArtifacts });
							}}
						/>
					</div>
				</div>
			</div>

			<div>
				<div class=" py-0.5 flex w-full justify-between">
					<div id="iframe-sandbox-allow-same-origin-label" class=" self-center text-xs">
						{$i18n.t('iframe Sandbox Allow Same Origin')}
					</div>

					<div class="flex items-center gap-2 p-1">
						<Switch
							ariaLabelledbyId="iframe-sandbox-allow-same-origin-label"
							tooltip={true}
							bind:state={iframeSandboxAllowSameOrigin}
							onchange={() => {
								saveSettings({ iframeSandboxAllowSameOrigin });
							}}
						/>
					</div>
				</div>
			</div>

			<div>
				<div class=" py-0.5 flex w-full justify-between">
					<div id="iframe-sandbox-allow-forms-label" class=" self-center text-xs">
						{$i18n.t('iframe Sandbox Allow Forms')}
					</div>

					<div class="flex items-center gap-2 p-1">
						<Switch
							ariaLabelledbyId="iframe-sandbox-allow-forms-label"
							tooltip={true}
							bind:state={iframeSandboxAllowForms}
							onchange={() => {
								saveSettings({ iframeSandboxAllowForms });
							}}
						/>
					</div>
				</div>
			</div>

			<div class=" my-2 text-sm font-medium">{$i18n.t('Voice')}</div>

			<div>
				<div class=" py-0.5 flex w-full justify-between">
					<div class=" self-center text-xs" id="allow-voice-interruption-in-call-label">
						{$i18n.t('Allow Voice Interruption in Call')}
					</div>

					<div class="flex items-center gap-2 p-1">
						<Switch
							ariaLabelledbyId="allow-voice-interruption-in-call-label"
							tooltip={true}
							bind:state={voiceInterruption}
							onchange={() => {
								saveSettings({ voiceInterruption });
							}}
						/>
					</div>
				</div>
			</div>

			<div>
				<div class=" py-0.5 flex w-full justify-between">
					<div id="display-emoji-label" class=" self-center text-xs">
						{$i18n.t('Display Emoji in Call')}
					</div>

					<div class="flex items-center gap-2 p-1">
						<Switch
							ariaLabelledbyId="display-emoji-label"
							tooltip={true}
							bind:state={showEmojiInCall}
							onchange={() => {
								saveSettings({ showEmojiInCall });
							}}
						/>
					</div>
				</div>
			</div>

			<div class=" my-2 text-sm font-medium">{$i18n.t('File')}</div>

			<div>
				<div class=" py-0.5 flex w-full justify-between">
					<div id="image-compression-label" class=" self-center text-xs">
						{$i18n.t('Image Compression')}
					</div>

					<div class="flex items-center gap-3 p-1">
						{#if imageCompression}
							<button
								class="text-xs text-gray-700 dark:text-gray-400 underline"
								type="button"
								aria-label={$i18n.t('Open Modal To Manage Image Compression')}
								onclick={() => {
									showManageImageCompressionModal = true;
								}}
							>
								{$i18n.t('Manage')}
							</button>
						{/if}

						<Switch
							ariaLabelledbyId="image-compression-label"
							tooltip={true}
							bind:state={imageCompression}
							onchange={() => {
								saveSettings({ imageCompression });
							}}
						/>
					</div>
				</div>
			</div>

			{#if imageCompression}
				<div>
					<div class=" py-0.5 flex w-full justify-between">
						<div id="image-compression-in-channels-label" class=" self-center text-xs">
							{$i18n.t('Compress Images in Channels')}
						</div>

						<div class="flex items-center gap-2 p-1">
							<Switch
								ariaLabelledbyId="image-compression-in-channels-label"
								tooltip={true}
								bind:state={imageCompressionInChannels}
								onchange={() => {
									saveSettings({ imageCompressionInChannels });
								}}
							/>
						</div>
					</div>
				</div>
			{/if}
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
