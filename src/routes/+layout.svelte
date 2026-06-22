<script>
	import { io } from 'socket.io-client';
	import { spring } from 'svelte/motion';

	let loadingProgress = spring(0, {
		stiffness: 0.05
	});

	import { onMount, tick, setContext, onDestroy } from 'svelte';
	import {
		config,
		user,
		settings,
		theme,
		WEBUI_NAME,
		mobile,
		socket,
		chatId,
		chats,
		currentChatPage,
		tags,
		temporaryChatEnabled,
		isLastActiveTab,
		isApp,
		appInfo,
		toolServers,
		playingNotificationSound,
		tokenUsageGroups
	} from '$lib/stores';
	import { goto } from '$app/navigation';
	import { page } from '$app/stores';
	import { Toaster, toast } from 'svelte-sonner';

	import { executeToolServer, getBackendConfig, getBootstrap } from '$lib/apis';
	import { getSessionUser, userSignOut } from '$lib/apis/auths';
	import {
		BOOTSTRAP_BUNDLE_ETAG_KEY,
		BOOTSTRAP_BUNDLE_BODY_KEY,
		setBootstrapComponents
	} from '$lib/utils/bootstrap';

	import '../tailwind.css';
	import '../app.css';

	import 'tippy.js/dist/tippy.css';

	import { WEBUI_BASE_URL, WEBUI_HOSTNAME } from '$lib/constants';
	import i18n, { initI18n, getLanguages, changeLanguage } from '$lib/i18n';
	import { bestMatchingLanguage } from '$lib/utils';
	import { getAllTags, getChatList } from '$lib/apis/chats';
	import {
		applySidebarEvent,
		refreshSidebarSnapshot,
		SIDEBAR_EVENT_TYPES
	} from '$lib/utils/sidebarSync';
	import { streamPerfCount, streamPerfEnd, streamPerfStart } from '$lib/utils/streamPerf';
	import NotificationToast from '$lib/components/NotificationToast.svelte';
	import AppSidebar from '$lib/components/app/AppSidebar.svelte';

	import { beforeNavigate } from '$app/navigation';
	import { updated } from '$app/state';
	import Spinner from '$lib/components/common/Spinner.svelte';

	// handle frontend updates (https://svelte.dev/docs/kit/configuration#version)
	beforeNavigate(({ willUnload, to }) => {
		if (updated.current && !willUnload && to?.url) {
			location.href = to.url.href;
		}
	});

	setContext('i18n', i18n);

	const SUPPORTS_BROADCAST_CHANNEL =
		typeof window !== 'undefined' && typeof BroadcastChannel !== 'undefined';
	let activeTabBc = SUPPORTS_BROADCAST_CHANNEL ? new BroadcastChannel('active-tab-channel') : null;
	let eventsBc = null;
	// LRU-ish dedup window for events replayed via the owui-events
	// BroadcastChannel. Bounds memory while remaining large enough to
	// cover a streaming response's worth of incremental updates.
	const seenBroadcastEvents = new Set();
	const seenBroadcastOrder = [];
	let primarySessionId = null;
	let suppressBroadcast = false;
	let sidebarReconcileTimer = null;
	let sidebarReconcilePromise = null;
	let hiddenAt = 0;

	let loaded = false;
	let tokenTimer = null;

	let showRefresh = false;

	const BREAKPOINT = 768;
	const SIDEBAR_RECONCILE_AFTER_HIDDEN_MS = 30_000;

	const rememberEvent = (event) => {
		const dataPart = event?.data ?? {};
		const type = dataPart?.type ?? '';
		const messageId = event?.message_id ?? '';
		const eventChatId = event?.chat_id ?? '';
		const data = dataPart?.data ?? {};
		let version = data?.version ?? dataPart?.version ?? event?.version ?? '';
		let id = dataPart?.event_id ?? '';

		if (!id && type === 'chat:delta:batch') {
			const batch = Array.isArray(dataPart?.batch) ? dataPart.batch : [];
			const hasSubagentUpdate = batch.some((b) => b?.data?.type === 'chat:subagent:update');
			if (hasSubagentUpdate) return false;
			const first = batch[0]?.data?.data?.version ?? batch[0]?.data?.version ?? '';
			const last =
				batch[batch.length - 1]?.data?.data?.version ??
				batch[batch.length - 1]?.data?.version ??
				'';
			version = `b:${batch.length}:${first}:${last}`;
		}

		if (!id && type === 'chat:subagent:update') return false;
		if (!id && (messageId || eventChatId)) {
			id = [messageId, eventChatId, type, version].join('|');
		}
		if (!id && SIDEBAR_EVENT_TYPES.has(type) && data?.id) {
			id = [type, data.id, data.updated_at ?? '', dataPart?.emitted_at ?? ''].join('|');
		}
		if (!id) return false;

		if (seenBroadcastEvents.has(id)) return true;
		seenBroadcastEvents.add(id);
		seenBroadcastOrder.push(id);
		if (seenBroadcastOrder.length > 300) {
			const evicted = seenBroadcastOrder.shift();
			if (evicted !== undefined) seenBroadcastEvents.delete(evicted);
		}
		return false;
	};

	const scheduleSidebarReconcile = (reason = 'unknown', delay = 250) => {
		if (typeof localStorage === 'undefined' || !localStorage?.token) return;
		if (sidebarReconcileTimer) clearTimeout(sidebarReconcileTimer);
		sidebarReconcileTimer = setTimeout(async () => {
			if (sidebarReconcilePromise || typeof localStorage === 'undefined' || !localStorage?.token)
				return;
			sidebarReconcilePromise = refreshSidebarSnapshot(localStorage.token, reason).catch((err) => {
				console.error('sidebar reconcile failed', reason, err);
			});
			try {
				await sidebarReconcilePromise;
			} finally {
				sidebarReconcilePromise = null;
			}
		}, delay);
	};

	const parseChatIdFromPath = (pathname = '') => {
		const match = pathname.match(/^\/c\/([^/?#]+)/);
		return match?.[1] ? decodeURIComponent(match[1]) : '';
	};

	const getVisibleChatId = () => {
		const browserPathname = typeof window !== 'undefined' ? window.location.pathname : '';
		const routeChatId =
			parseChatIdFromPath(browserPathname) || parseChatIdFromPath($page.url.pathname);
		if (routeChatId) {
			return routeChatId;
		}

		const currentChatId = $chatId ?? '';
		const isPersistentChatView =
			browserPathname.includes('/c/') || $page.url.pathname.includes('/c/');

		return $temporaryChatEnabled || currentChatId.startsWith('local:') || isPersistentChatView
			? currentChatId
			: '';
	};

	const isWindowFocused = async () => {
		let focused = document.visibilityState === 'visible';

		if (window.electronAPI) {
			const res = await window.electronAPI.send({
				type: 'window:isFocused'
			});

			if (res) {
				focused = Boolean(res.isFocused);
			}
		}

		return focused;
	};

	const setupSocket = async (enableWebsocket) => {
		const _socket = io(`${WEBUI_BASE_URL}` || undefined, {
			reconnection: true,
			reconnectionDelay: 1000,
			reconnectionDelayMax: 5000,
			randomizationFactor: 0.5,
			path: '/ws/socket.io',
			transports: enableWebsocket ? ['websocket'] : ['polling', 'websocket'],
			auth: { token: localStorage.token }
		});

		await socket.set(_socket);

		_socket.on('connect_error', (err) => {
			console.log('connect_error', err);
		});

		_socket.on('connect', () => {
			console.log('connected', _socket.id);
			if (localStorage.getItem('token')) {
				// Emit user-join event with auth token
				_socket.emit('user-join', { auth: { token: localStorage.token } }, (ack) => {
					if (ack && typeof ack === 'object' && 'primary_session_id' in ack) {
						primarySessionId = ack.primary_session_id ?? null;
					} else {
						primarySessionId = null;
					}
					scheduleSidebarReconcile('socket:connect');
				});
			} else {
				console.warn('No token found in localStorage, user-join event not emitted');
			}
		});

		_socket.on('reconnect_attempt', (attempt) => {
			console.log('reconnect_attempt', attempt);
		});

		_socket.on('reconnect_failed', () => {
			console.log('reconnect_failed');
		});

		_socket.on('disconnect', (reason, details) => {
			console.log(`Socket ${_socket.id} disconnected due to ${reason}`);
			if (details) {
				console.log('Additional details:', details);
			}
		});
	};

	const executeTool = async (data, cb) => {
		const toolServer = $settings?.toolServers?.find((server) => server.url === data.server?.url);
		const toolServerData = $toolServers?.find((server) => server.url === data.server?.url);

		console.log('executeTool', data, toolServer);

		if (toolServer) {
			console.log(toolServer);

			let toolServerToken = null;
			const auth_type = toolServer?.auth_type ?? 'bearer';
			if (auth_type === 'bearer') {
				toolServerToken = toolServer?.key;
			} else if (auth_type === 'none') {
				// No authentication
			} else if (auth_type === 'session') {
				toolServerToken = localStorage.token;
			}

			const res = await executeToolServer(
				toolServerToken,
				toolServer.url,
				data?.name,
				data?.params,
				toolServerData
			);

			console.log('executeToolServer', res);
			if (cb) {
				cb(JSON.parse(JSON.stringify(res)));
			}
		} else {
			if (cb) {
				cb(
					JSON.parse(
						JSON.stringify({
							error: 'Tool Server Not Found'
						})
					)
				);
			}
		}
	};

	const STREAM_SCOPED_EVENT_TYPES = new Set([
		'chat:delta',
		'chat:delta:batch',
		'chat:delta:batch2',
		'tool_call:result',
		'chat:subagent:update',
		'browser:frame',
		'chat:stream:sync_required',
		'chat:done',
		'chat:message:error',
		'chat:tasks:cancel'
	]);

	const chatEventHandler = async (event, cb) => {
		const perf = streamPerfStart();
		const eventType = event?.data?.type ?? null;
		const streamScoped = STREAM_SCOPED_EVENT_TYPES.has(eventType);
		const sidebarEvent = eventType && SIDEBAR_EVENT_TYPES.has(eventType);

		// Chat.svelte is the only component that owns live stream rendering. The
		// global layout listener used to recursively unpack stream batches too,
		// doubling async handler work for long generations without mutating chat UI.
		if (streamScoped) {
			streamPerfCount(`layout.stream_ignored.${eventType ?? 'unknown'}`);
			streamPerfEnd('layout.stream_ignored', perf);
			return;
		}

		if (eventType && rememberEvent(event)) {
			streamPerfEnd('layout.event_duplicate', perf);
			return;
		}

		// Re-broadcast to sibling tabs of the same user so the backend can emit
		// stream events to a single elected primary session per user. The
		// non-primary tabs receive the same payload via BroadcastChannel and
		// run it through this handler with suppressBroadcast set, preventing
		// an infinite re-broadcast loop.
		if (
			!suppressBroadcast &&
			eventsBc &&
			primarySessionId &&
			$socket?.id &&
			$socket.id === primarySessionId &&
			!streamScoped &&
			!sidebarEvent
		) {
			try {
				eventsBc.postMessage(event);
			} catch (err) {
				console.error('owui-events broadcast failed', err);
			}
		}

		const visibleChatId = getVisibleChatId();
		const isCurrentChatEvent = Boolean(event.chat_id) && visibleChatId === event.chat_id;
		const windowFocused = await isWindowFocused();

		await tick();
		const type = event?.data?.type ?? null;
		const data = event?.data?.data ?? null;

		// Sidebar-affecting events ALWAYS apply, regardless of which chat is
		// visible or whether the window is focused. The original handler
		// gated chat:title and chat:tags behind the visible-chat check, which
		// meant renaming/tagging the currently-open chat from another tab
		// would silently fail to update the sidebar row in the receiving tab.
		if (type && SIDEBAR_EVENT_TYPES.has(type)) {
			try {
				await applySidebarEvent(type, data, localStorage.token);
			} catch (err) {
				console.error('applySidebarEvent failed', type, err);
			}
		}

		// Push-based replacement for the old 3 s / 30 s `fetchTokenUsage`
		// polling loop (Wire Contract #6 in the network-reduction plan).
		if (type === 'token-usage:update') {
			tokenUsageGroups.set(data?.groups || {});
			return;
		}

		if (!isCurrentChatEvent || !windowFocused) {
			if (type === 'chat:completion') {
				const { done, content, title } = data;

				if (done) {
					if ($settings?.notificationSoundAlways ?? false) {
						playingNotificationSound.set(true);

						const audio = new Audio(`/audio/notification.mp3`);
						audio.play().finally(() => {
							// Ensure the global state is reset after the sound finishes
							playingNotificationSound.set(false);
						});
					}

					if ($isLastActiveTab) {
						if ($settings?.notificationEnabled ?? false) {
							new Notification(`${title} • Open WebUI`, {
								body: content,
								icon: `${WEBUI_BASE_URL}/static/favicon.png`
							});
						}
					}

					toast.custom(NotificationToast, {
						componentProps: {
							onClick: () => {
								goto(`/c/${event.chat_id}`);
							},
							content: content,
							title: title
						},
						duration: 15000,
						unstyled: true
					});
				}
			}
		} else if (data?.session_id === $socket.id) {
			if (type === 'execute:tool') {
				console.log('execute:tool', data);
				executeTool(data, cb);
			} else {
				console.log('chatEventHandler', event);
			}
		}
	};

	const channelEventHandler = async (event) => {
		if (event.data?.type === 'typing') {
			return;
		}

		// check url path
		const channel = $page.url.pathname.includes(`/channels/${event.channel_id}`);
		const windowFocused = await isWindowFocused();

		if ((!channel || !windowFocused) && event?.user?.id !== $user?.id) {
			await tick();
			const type = event?.data?.type ?? null;
			const data = event?.data?.data ?? null;

			if (type === 'message') {
				if ($isLastActiveTab) {
					if ($settings?.notificationEnabled ?? false) {
						new Notification(`${data?.user?.name} (#${event?.channel?.name}) • Open WebUI`, {
							body: data?.content,
							icon: data?.user?.profile_image_url ?? `${WEBUI_BASE_URL}/static/favicon.png`
						});
					}
				}

				toast.custom(NotificationToast, {
					componentProps: {
						onClick: () => {
							goto(`/channels/${event.channel_id}`);
						},
						content: data?.content,
						title: `#${event?.channel?.name}`
					},
					duration: 15000,
					unstyled: true
				});
			}
		}
	};

	const TOKEN_EXPIRY_BUFFER = 60; // seconds
	const checkTokenExpiry = async () => {
		const exp = $user?.expires_at; // token expiry time in unix timestamp
		const now = Math.floor(Date.now() / 1000); // current time in unix timestamp

		if (!exp) {
			// If no expiry time is set, do nothing
			return;
		}

		if (now >= exp - TOKEN_EXPIRY_BUFFER) {
			const res = await userSignOut();
			user.set(null);
			localStorage.removeItem('token');

			location.href = res?.redirect_url ?? '/auth';
		}
	};

	onDestroy(() => {
		activeTabBc?.close();
		eventsBc?.close();
		if (tokenTimer) clearInterval(tokenTimer);
		if (sidebarReconcileTimer) clearTimeout(sidebarReconcileTimer);
		$socket?.off('events', chatEventHandler);
		$socket?.off('events:channel', channelEventHandler);
	});

	onMount(async () => {
		let touchstartY = 0;

		function isNavOrDescendant(el) {
			const nav = document.querySelector('nav'); // change selector if needed
			return nav && (el === nav || nav.contains(el));
		}

		document.addEventListener('touchstart', (e) => {
			if (!isNavOrDescendant(e.target)) return;
			touchstartY = e.touches[0].clientY;
		});

		document.addEventListener('touchmove', (e) => {
			if (!isNavOrDescendant(e.target)) return;
			const touchY = e.touches[0].clientY;
			const touchDiff = touchY - touchstartY;
			if (touchDiff > 50 && window.scrollY === 0) {
				showRefresh = true;
				e.preventDefault();
			}
		});

		document.addEventListener('touchend', (e) => {
			if (!isNavOrDescendant(e.target)) return;
			if (showRefresh) {
				showRefresh = false;
				location.reload();
			}
		});

		if (typeof window !== 'undefined' && window.applyTheme) {
			window.applyTheme();
		}

		if (window?.electronAPI) {
			const info = await window.electronAPI.send({
				type: 'app:info'
			});

			if (info) {
				isApp.set(true);
				appInfo.set(info);

				const data = await window.electronAPI.send({
					type: 'app:data'
				});

				if (data) {
					appData.set(data);
				}
			}
		}

		// Listen for messages on the BroadcastChannel
		if (activeTabBc) {
			activeTabBc.onmessage = (event) => {
				if (event.data === 'active') {
					isLastActiveTab.set(false); // Another tab became active
				}
			};
		}

		if (SUPPORTS_BROADCAST_CHANNEL) {
			eventsBc = new BroadcastChannel('owui-events');
			eventsBc.onmessage = async (msg) => {
				const payload = msg?.data;
				if (!payload) return;
				// The v2 server now sends primary-routed stream events to both the
				// elected primary and the originating socket. The primary still relays
				// over BroadcastChannel for sibling tabs, but the originating tab must
				// ignore that replay or cross-channel ordering can create artificial
				// version gaps and force snapshot catch-up.
				if (payload?.session_id && $socket?.id && payload.session_id === $socket.id) return;
				// Non-primary tabs receive deduped stream events here. Replay
				// them through the same handler the socket would invoke, with
				// the re-broadcast guard set so we don't echo back.
				suppressBroadcast = true;
				try {
					await chatEventHandler(payload, () => {});
				} catch (err) {
					console.error('owui-events replay failed', err);
				} finally {
					suppressBroadcast = false;
				}
			};
		}

		// Set yourself as the last active tab when this tab is focused
		const handleVisibilityChange = () => {
			if (document.visibilityState === 'visible') {
				isLastActiveTab.set(true); // This tab is now the active tab
				activeTabBc?.postMessage('active'); // Notify other tabs that this tab is active
				if (hiddenAt && Date.now() - hiddenAt > SIDEBAR_RECONCILE_AFTER_HIDDEN_MS) {
					scheduleSidebarReconcile('visibility:resume');
				}
				hiddenAt = 0;

				// Check token expiry when the tab becomes active
				checkTokenExpiry();
			} else if (document.visibilityState === 'hidden') {
				hiddenAt = Date.now();
			}
		};

		// Add event listener for visibility state changes
		document.addEventListener('visibilitychange', handleVisibilityChange);

		// Call visibility change handler initially to set state on load
		handleVisibilityChange();

		theme.set(localStorage.theme);

		mobile.set(window.innerWidth < BREAKPOINT);

		const onResize = () => {
			if (window.innerWidth < BREAKPOINT) {
				mobile.set(true);
			} else {
				mobile.set(false);
			}
		};
		window.addEventListener('resize', onResize);

		user.subscribe((value) => {
			if (value) {
				$socket?.off('events', chatEventHandler);
				$socket?.off('events:channel', channelEventHandler);

				$socket?.on('events', chatEventHandler);
				$socket?.on('events:channel', channelEventHandler);

				// Set up the token expiry check
				if (tokenTimer) {
					clearInterval(tokenTimer);
				}
				tokenTimer = setInterval(checkTokenExpiry, 15000);
			} else {
				$socket?.off('events', chatEventHandler);
				$socket?.off('events:channel', channelEventHandler);
			}
		});

		let backendConfig = null;

		// SWR: Load from cache immediately for instant splash screen dismissal
		try {
			const cachedBackendConfig = localStorage.getItem('backendConfig');
			if (cachedBackendConfig) {
				backendConfig = JSON.parse(cachedBackendConfig);
				config.set(backendConfig);
				WEBUI_NAME.set(backendConfig.name);
			}

			const cachedSessionUser = localStorage.getItem('sessionUser');
			if (cachedSessionUser) {
				user.set(JSON.parse(cachedSessionUser));
			}

			if (cachedBackendConfig && (!localStorage.token || cachedSessionUser)) {
				initI18n(localStorage?.locale);
				loaded = true;
				document.getElementById('splash-screen')?.remove();
			}
		} catch (e) {
			console.error('Error parsing cached data', e);
		}

		// Run initialization queries in parallel if we have a token
		try {
			if (localStorage.token) {
				const cachedUserId = (() => {
					try {
						return JSON.parse(localStorage.getItem('sessionUser') ?? 'null')?.id ?? null;
					} catch {
						return null;
					}
				})();
				const bundleEtagKey = BOOTSTRAP_BUNDLE_ETAG_KEY(cachedUserId);
				const bundleBodyKey = BOOTSTRAP_BUNDLE_BODY_KEY(cachedUserId);
				const cachedBundleEtag = localStorage.getItem(bundleEtagKey);

				const bootstrapPromise = getBootstrap(localStorage.token, {
					include: [
						'config',
						'user',
						'settings',
						'models',
						'banners',
						'tools',
						'folders',
						'tags',
						'pinned',
						'chats',
						'channels'
					],
					ifNoneMatch: cachedBundleEtag
				}).catch((error) => {
					console.error('getBootstrap failed', error);
					return null;
				});

				const [bootstrap, _languages] = await Promise.all([
					bootstrapPromise,
					!localStorage.locale ? getLanguages() : Promise.resolve(null)
				]);

				let _config = null;
				let _sessionUser = null;

				if (bootstrap && bootstrap.status === 304) {
					try {
						const cachedBundle = JSON.parse(localStorage.getItem(bundleBodyKey) ?? 'null');
						if (cachedBundle?.components) {
							setBootstrapComponents(cachedBundle.components);
							_config = cachedBundle.components.config ?? null;
							_sessionUser = cachedBundle.components.user ?? null;
						}
					} catch (e) {
						console.error('Failed to read cached bootstrap bundle', e);
					}
				} else if (bootstrap && bootstrap.status === 200) {
					setBootstrapComponents(bootstrap.components);
					_config = bootstrap.components.config ?? null;
					_sessionUser = bootstrap.components.user ?? null;
					try {
						localStorage.setItem(
							bundleBodyKey,
							JSON.stringify({ components: bootstrap.components })
						);
						if (bootstrap.bundle_etag) {
							localStorage.setItem(bundleEtagKey, bootstrap.bundle_etag);
						}
					} catch (e) {
						console.error('Failed to persist bootstrap bundle', e);
					}
				}

				// Fallback when /api/bootstrap is unavailable (backend not yet upgraded).
				if (!bootstrap) {
					const [fallbackConfig, fallbackUser] = await Promise.all([
						getBackendConfig(),
						getSessionUser(localStorage.token).catch((error) => {
							toast.error(`${error}`);
							return null;
						})
					]);
					_config = fallbackConfig;
					_sessionUser = fallbackUser;
				}

				backendConfig = _config;

				if (_config) {
					localStorage.setItem('backendConfig', JSON.stringify(_config));
				}

				if (_sessionUser) {
					localStorage.setItem('sessionUser', JSON.stringify(_sessionUser));
					await user.set(_sessionUser);
				} else {
					localStorage.removeItem('token');
					localStorage.removeItem('sessionUser');
					const currentUrl = `${window.location.pathname}${window.location.search}`;
					if ($page.url.pathname !== '/auth' && !$page.url.pathname.startsWith('/s/')) {
						await goto(`/auth?redirect=${encodeURIComponent(currentUrl)}`);
					}
				}

				initI18n(localStorage?.locale);
				if (!localStorage.locale && _languages) {
					const browserLanguages = navigator.languages
						? navigator.languages
						: [navigator.language || navigator.userLanguage];
					const lang = backendConfig?.default_locale
						? backendConfig.default_locale
						: bestMatchingLanguage(_languages, browserLanguages, 'en-US');
					changeLanguage(lang);
				}
			} else {
				// No token, just get config
				backendConfig = await getBackendConfig();

				if (backendConfig) {
					localStorage.setItem('backendConfig', JSON.stringify(backendConfig));
				}

				initI18n(localStorage?.locale);
				if (!localStorage.locale) {
					const languages = await getLanguages();
					const browserLanguages = navigator.languages
						? navigator.languages
						: [navigator.language || navigator.userLanguage];
					const lang = backendConfig?.default_locale
						? backendConfig.default_locale
						: bestMatchingLanguage(languages, browserLanguages, 'en-US');
					changeLanguage(lang);
				}

				const currentUrl = `${window.location.pathname}${window.location.search}`;
				if ($page.url.pathname !== '/auth' && !$page.url.pathname.startsWith('/s/')) {
					await goto(`/auth?redirect=${encodeURIComponent(currentUrl)}`);
				}
			}
			console.log('Backend config:', backendConfig);
		} catch (error) {
			console.error('Error loading backend config:', error);
			// Initialize i18n even if we didn't get a backend config,
			// so `/error` can show something that's not `undefined`.
			initI18n(localStorage?.locale);
		}

		if (backendConfig) {
			// Save Backend Status to Store
			await config.set(backendConfig);
			await WEBUI_NAME.set(backendConfig.name);

			if ($config) {
				await setupSocket($config.features?.enable_websocket ?? true);
			}
		} else {
			// Redirect to /error when Backend Not Detected
			await goto(`/error`);
		}

		await tick();

		if (
			document.documentElement.classList.contains('her') &&
			document.getElementById('progress-bar')
		) {
			loadingProgress.subscribe((value) => {
				const progressBar = document.getElementById('progress-bar');

				if (progressBar) {
					progressBar.style.width = `${value}%`;
				}
			});

			await loadingProgress.set(100);

			document.getElementById('splash-screen')?.remove();

			const audio = new Audio(`/audio/greeting.mp3`);
			const playAudio = () => {
				audio.play();
				document.removeEventListener('click', playAudio);
			};

			document.addEventListener('click', playAudio);

			loaded = true;
		} else {
			document.getElementById('splash-screen')?.remove();
			loaded = true;
		}

		return () => {
			window.removeEventListener('resize', onResize);
		};
	});
</script>

<svelte:head>
	<title>{$WEBUI_NAME}</title>
	<link crossorigin="anonymous" rel="icon" href="{WEBUI_BASE_URL}/static/favicon.png" />

	<meta name="apple-mobile-web-app-title" content={$WEBUI_NAME} />
	<meta name="description" content={$WEBUI_NAME} />
	<link
		rel="search"
		type="application/opensearchdescription+xml"
		title={$WEBUI_NAME}
		href="/opensearch.xml"
		crossorigin="use-credentials"
	/>
</svelte:head>

{#if showRefresh}
	<div class=" py-5">
		<Spinner className="size-5" />
	</div>
{/if}

{#if loaded}
	{#if $isApp}
		<div class="flex flex-row h-screen">
			<AppSidebar />

			<div class="w-full flex-1 max-w-[calc(100%-4.5rem)]">
				<slot />
			</div>
		</div>
	{:else}
		<slot />
	{/if}
{/if}

<Toaster
	theme={$theme.includes('dark')
		? 'dark'
		: $theme === 'system'
			? window.matchMedia('(prefers-color-scheme: dark)').matches
				? 'dark'
				: 'light'
			: 'light'}
	richColors
	position="top-right"
	closeButton
/>
