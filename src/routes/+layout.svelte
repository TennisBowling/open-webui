<script>
	import { io } from 'socket.io-client';
	import { spring } from 'svelte/motion';
	import PyodideWorker from '$lib/workers/pyodide.worker?worker';

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
	import { applySidebarEvent, SIDEBAR_EVENT_TYPES } from '$lib/utils/sidebarSync';
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

	const bc = new BroadcastChannel('active-tab-channel');

	const SUPPORTS_BROADCAST_CHANNEL = typeof BroadcastChannel !== 'undefined';
	let eventsBc = null;
	// LRU-ish dedup window for events replayed via the owui-events
	// BroadcastChannel. Bounds memory while remaining large enough to
	// cover a streaming response's worth of incremental updates.
	const seenBroadcastEvents = new Set();
	const seenBroadcastOrder = [];
	let primarySessionId = null;
	let suppressBroadcast = false;

	let loaded = false;
	let tokenTimer = null;

	let showRefresh = false;

	const BREAKPOINT = 768;

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

	const executePythonAsWorker = async (id, code, cb) => {
		let result = null;
		let stdout = null;
		let stderr = null;

		let executing = true;
		let packages = [
			/\bimport\s+requests\b|\bfrom\s+requests\b/.test(code) ? 'requests' : null,
			/\bimport\s+bs4\b|\bfrom\s+bs4\b/.test(code) ? 'beautifulsoup4' : null,
			/\bimport\s+numpy\b|\bfrom\s+numpy\b/.test(code) ? 'numpy' : null,
			/\bimport\s+pandas\b|\bfrom\s+pandas\b/.test(code) ? 'pandas' : null,
			/\bimport\s+matplotlib\b|\bfrom\s+matplotlib\b/.test(code) ? 'matplotlib' : null,
			/\bimport\s+seaborn\b|\bfrom\s+seaborn\b/.test(code) ? 'seaborn' : null,
			/\bimport\s+sklearn\b|\bfrom\s+sklearn\b/.test(code) ? 'scikit-learn' : null,
			/\bimport\s+scipy\b|\bfrom\s+scipy\b/.test(code) ? 'scipy' : null,
			/\bimport\s+re\b|\bfrom\s+re\b/.test(code) ? 'regex' : null,
			/\bimport\s+seaborn\b|\bfrom\s+seaborn\b/.test(code) ? 'seaborn' : null,
			/\bimport\s+sympy\b|\bfrom\s+sympy\b/.test(code) ? 'sympy' : null,
			/\bimport\s+tiktoken\b|\bfrom\s+tiktoken\b/.test(code) ? 'tiktoken' : null,
			/\bimport\s+pytz\b|\bfrom\s+pytz\b/.test(code) ? 'pytz' : null
		].filter(Boolean);

		const pyodideWorker = new PyodideWorker();

		pyodideWorker.postMessage({
			id: id,
			code: code,
			packages: packages
		});

		setTimeout(() => {
			if (executing) {
				executing = false;
				stderr = 'Execution Time Limit Exceeded';
				pyodideWorker.terminate();

				if (cb) {
					cb(
						JSON.parse(
							JSON.stringify(
								{
									stdout: stdout,
									stderr: stderr,
									result: result
								},
								(_key, value) => (typeof value === 'bigint' ? value.toString() : value)
							)
						)
					);
				}
			}
		}, 60000);

		pyodideWorker.onmessage = (event) => {
			console.log('pyodideWorker.onmessage', event);
			const { id, ...data } = event.data;

			console.log(id, data);

			data['stdout'] && (stdout = data['stdout']);
			data['stderr'] && (stderr = data['stderr']);
			data['result'] && (result = data['result']);

			if (cb) {
				cb(
					JSON.parse(
						JSON.stringify(
							{
								stdout: stdout,
								stderr: stderr,
								result: result
							},
							(_key, value) => (typeof value === 'bigint' ? value.toString() : value)
						)
					)
				);
			}

			executing = false;
		};

		pyodideWorker.onerror = (event) => {
			console.log('pyodideWorker.onerror', event);

			if (cb) {
				cb(
					JSON.parse(
						JSON.stringify(
							{
								stdout: stdout,
								stderr: stderr,
								result: result
							},
							(_key, value) => (typeof value === 'bigint' ? value.toString() : value)
						)
					)
				);
			}
			executing = false;
		};
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
		'tool_call:result',
		'chat:subagent:update',
		'chat:done',
		'chat:message:error',
		'chat:tasks:cancel'
	]);

	const chatEventHandler = async (event, cb) => {
		const eventType = event?.data?.type ?? null;
		const streamScoped = STREAM_SCOPED_EVENT_TYPES.has(eventType);
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
			!streamScoped
		) {
			try {
				eventsBc.postMessage(event);
			} catch (err) {
				console.error('owui-events broadcast failed', err);
			}
		}

		// Stream v2 batching: the backend coalesces consecutive chat:delta /
		// tool_call:result emits per asyncio tick into a single envelope to
		// reduce socket I/O at high concurrency. Unpack inline so downstream
		// handlers (and stores) see each inner event as if it had arrived
		// individually. Re-broadcast (above) forwards the outer batch so
		// non-primary tabs unpack the same way here.
		if (eventType === 'chat:delta:batch') {
			const batch = Array.isArray(event?.data?.batch) ? event.data.batch : [];
			const prevSuppress = suppressBroadcast;
			// Don't re-broadcast each inner event individually — the outer
			// batch was already forwarded above (or this IS a replay from
			// the BroadcastChannel and suppress is already on).
			suppressBroadcast = true;
			try {
				for (const inner of batch) {
					if (!inner || typeof inner !== 'object') continue;
					try {
						await chatEventHandler(
							{
								chat_id: inner.chat_id ?? event.chat_id,
								message_id: inner.message_id ?? event.message_id,
								data: inner.data
							},
							cb
						);
					} catch (err) {
						console.error('chat:delta:batch inner dispatch failed', err);
					}
				}
			} finally {
				suppressBroadcast = prevSuppress;
			}
			return;
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
			if (type === 'execute:python') {
				console.log('execute:python', data);
				executePythonAsWorker(data.id, data.code, cb);
			} else if (type === 'execute:tool') {
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
		bc.onmessage = (event) => {
			if (event.data === 'active') {
				isLastActiveTab.set(false); // Another tab became active
			}
		};

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
				// Defensive dedup: if the backend briefly has two primary
				// sessions during the election race (e.g. old primary
				// disconnecting at the same instant a new tab connects),
				// the same event can arrive on this channel twice. Drop
				// repeats so non-primary tabs don't double-apply stream
				// updates. Identifier covers message id, event type, and
				// the per-message version when present.
				try {
					const dataPart = payload?.data ?? {};
					const messageId = payload?.message_id ?? '';
					const chatId = payload?.chat_id ?? '';
					const type = dataPart?.type ?? '';
					let version = dataPart?.data?.version ?? dataPart?.version ?? payload?.version ?? '';
					// For batched envelopes the outer type doesn't carry a
					// version; derive a scoped key from the inner batch's
					// first+last versions (plus length) so distinct batches
					// don't collide on the same message id. chat:subagent:update
					// inner events don't have a top-level version field — the
					// inner_event nested inside MAY have one for chat:delta /
					// chat:done, but for status/error events there is none and
					// distinct batches could collide. Skip dedup entirely when
					// the batch contains a chat:subagent:update entry: the
					// server-side 0.5s throttle already coalesces these and
					// queueSubagentUpdate is idempotent for repeated state.
					if (type === 'chat:delta:batch') {
						const batch = Array.isArray(dataPart?.batch) ? dataPart.batch : [];
						const hasSubagentUpdate = batch.some((b) => b?.data?.type === 'chat:subagent:update');
						if (hasSubagentUpdate) {
							// Bypass dedup — replay every batch as-is.
							suppressBroadcast = true;
							try {
								await chatEventHandler(payload, () => {});
							} catch (err) {
								console.error('owui-events replay failed', err);
							} finally {
								suppressBroadcast = false;
							}
							return;
						}
						const first = batch[0]?.data?.data?.version ?? batch[0]?.data?.version ?? '';
						const last =
							batch[batch.length - 1]?.data?.data?.version ??
							batch[batch.length - 1]?.data?.version ??
							'';
						version = `b:${batch.length}:${first}:${last}`;
					}
					// Only dedup when we have at least a message/chat id to
					// scope by — otherwise distinct events with no
					// identifying fields would all collapse onto one entry.
					// Skip chat:subagent:update entirely — these envelopes
					// don't carry a per-emit version (the throttle key is
					// subagent_id, set server-side) and queueSubagentUpdate
					// is idempotent for repeated state, so a rare duplicate
					// during the primary-election race is harmless.
					if (type === 'chat:subagent:update') {
						// fall through to replay without recording dedup id
					} else if (messageId || chatId) {
						const id = [messageId, chatId, type, version].join('|');
						if (seenBroadcastEvents.has(id)) {
							return;
						}
						seenBroadcastEvents.add(id);
						seenBroadcastOrder.push(id);
						if (seenBroadcastOrder.length > 200) {
							const evicted = seenBroadcastOrder.shift();
							if (evicted !== undefined) {
								seenBroadcastEvents.delete(evicted);
							}
						}
					}
				} catch (err) {
					// Identifier construction must never break replay.
					console.error('owui-events dedup id failed', err);
				}
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
				bc.postMessage('active'); // Notify other tabs that this tab is active

				// Check token expiry when the tab becomes active
				checkTokenExpiry();
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
