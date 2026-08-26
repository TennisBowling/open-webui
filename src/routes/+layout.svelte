<script lang="ts">
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
		online,
		chatId,
		chats,
		currentChatPage,
		tags,
		temporaryChatEnabled,
		isLastActiveTab,
		isApp,
		appInfo,
		appData,
		toolServers,
		playingNotificationSound,
		tokenUsageGroups,
		subscriptionUsage,
		bootstrapRevision,
		automationsUnreadCount
	} from '$lib/stores';
	import { goto, onNavigate } from '$app/navigation';
	import { page } from '$app/state';
	import { Toaster } from 'svelte-sonner';
	import { toast } from '$lib/utils/toast';

	import { initKeyboardViewport } from '$lib/utils/keyboardViewport';
	import { initMeltTouchDismiss } from '$lib/utils/meltTouchDismiss';
	import { executeToolServer, getBackendConfig, getBootstrap } from '$lib/apis';
	import { getSessionUser, userSignOut } from '$lib/apis/auths';
	import {
		BOOTSTRAP_BUNDLE_ETAG_KEY,
		setBootstrapComponents,
		readBootstrapCache,
		writeBootstrapCache,
		buildBootstrapEtagsHeader,
		applyUnchangedFromCache
	} from '$lib/utils/bootstrap';

	import '../tailwind.css';
	import '../app.css';

	import 'tippy.js/dist/tippy.css';

	import { WEBUI_BASE_URL, WEBUI_HOSTNAME } from '$lib/constants';
	import i18next from 'i18next';
	import i18n, { initI18n, getLanguages, changeLanguage } from '$lib/i18n';
	import { bestMatchingLanguage } from '$lib/utils';
	import { getAllTags, getChatList } from '$lib/apis/chats';
	import { getUnreadRunCount } from '$lib/apis/automations';
	import {
		applySidebarEvent,
		refreshSidebarSnapshot,
		SIDEBAR_EVENT_TYPES,
		sidebarReconcileClean
	} from '$lib/utils/sidebarSync';
	import { streamPerfCount, streamPerfEnd, streamPerfStart } from '$lib/utils/streamPerf';
	import NotificationToast from '$lib/components/NotificationToast.svelte';
	import AppSidebar from '$lib/components/app/AppSidebar.svelte';

	import { beforeNavigate } from '$app/navigation';
	import { updated } from '$app/state';
	import Spinner from '$lib/components/common/Spinner.svelte';
	interface Props {
		children?: import('svelte').Snippet;
	}

	let { children }: Props = $props();

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

	// Socket-connect reconcile-storm guards (Wire item #1).
	let firstSocketConnect = true;
	let socketDisconnectedAt = 0;
	let lastSidebarReconcileAt = 0;
	// Set true once a boot bootstrap delivered fresh sidebar components, so the
	// first socket connect can skip the (now-redundant) reconcile.
	let bootstrapDeliveredSidebar = false;

	let loaded = $state(false);
	let tokenTimer = null;
	let versionPollTimer = null;

	let showRefresh = $state(false);

	const BREAKPOINT = 768;
	const SIDEBAR_RECONCILE_AFTER_HIDDEN_MS = 30_000;
	// Dedupe window between the socket-reconnect and visibility-resume reconciles.
	const SIDEBAR_RECONCILE_DEDUPE_MS = 5_000;
	const VERSION_POLL_INTERVAL_MS = 300_000;

	const BOOTSTRAP_INCLUDE = [
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
		'channels',
		'subscription_usage'
	];

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

	// Dedupes the socket-reconnect and visibility-resume reconciles: if either
	// ran within SIDEBAR_RECONCILE_DEDUPE_MS, skip the other (Wire item #1c).
	const maybeScheduleSidebarReconcile = (reason = 'unknown') => {
		const now = Date.now();
		if (now - lastSidebarReconcileAt < SIDEBAR_RECONCILE_DEDUPE_MS) return;
		lastSidebarReconcileAt = now;
		scheduleSidebarReconcile(reason);
	};

	const parseChatIdFromPath = (pathname = '') => {
		const match = pathname.match(/^\/c\/([^/?#]+)/);
		return match?.[1] ? decodeURIComponent(match[1]) : '';
	};

	const getVisibleChatId = () => {
		const browserPathname = typeof window !== 'undefined' ? window.location.pathname : '';
		const routeChatId =
			parseChatIdFromPath(browserPathname) || parseChatIdFromPath(page.url.pathname);
		if (routeChatId) {
			return routeChatId;
		}

		const currentChatId = $chatId ?? '';
		const isPersistentChatView =
			browserPathname.includes('/c/') || page.url.pathname.includes('/c/');

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

		_socket.off('events', chatEventHandler);
		_socket.off('events:channel', channelEventHandler);
		_socket.on('events', chatEventHandler);
		_socket.on('events:channel', channelEventHandler);

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

					// Wire item #1: avoid a reconcile storm.
					if (firstSocketConnect) {
						firstSocketConnect = false;
						// (a) The boot bootstrap already delivered fresh sidebar
						// components — the first connect's reconcile would be redundant.
						if (bootstrapDeliveredSidebar) {
							lastSidebarReconcileAt = Date.now();
							sidebarReconcileClean.set(true);
							return;
						}
						maybeScheduleSidebarReconcile('socket:connect');
						return;
					}

					// (b) Reconnect: only reconcile if we were actually down long
					// enough that push events could have been missed.
					const downFor = socketDisconnectedAt ? Date.now() - socketDisconnectedAt : 0;
					socketDisconnectedAt = 0;
					if (downFor < SIDEBAR_RECONCILE_AFTER_HIDDEN_MS) {
						// Blip short enough that the existing heuristic already treats
						// push events as reliable over that span — no reconcile is
						// scheduled, so restore the "clean" signal immediately rather
						// than leaving the offline cache-tier gate blocked forever.
						sidebarReconcileClean.set(true);
						return;
					}
					maybeScheduleSidebarReconcile('socket:reconnect');
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
			// Stamp the disconnect so the next connect can measure downtime and only
			// reconcile after a real outage (Wire item #1b).
			socketDisconnectedAt = Date.now();
			// The sidebar's cached updated_at window can no longer be trusted as
			// freshness proof until a reconcile confirms nothing was missed —
			// gates Chat.svelte's zero-network cache-serve tier (WS-A).
			sidebarReconcileClean.set(false);
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
		'chat:tasks:cancel',
		// Cross-device prompt sync: Chat.svelte owns the surgical user-bubble
		// insert; the global handler must defer (no rebroadcast/dedup) just like
		// the other stream-scoped types.
		'chat:user-message'
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
		// Data-frugal wire: the backend sends only the groups whose payload
		// CHANGED since its last push (each carried in full), plus `names`, the
		// authoritative catalog of current group names — merge changed groups in
		// by name and drop anything no longer in the catalog (deleted groups).
		// Subscription-provider usage push: the backend only emits on actual
		// change and the snapshot is tiny (a few windows per flagged
		// connection), so the wire carries the whole authoritative map —
		// full replace, no merge protocol.
		if (type === 'subscription-usage:update') {
			subscriptionUsage.set(
				data?.subscriptions && typeof data.subscriptions === 'object' ? data.subscriptions : {}
			);
			return;
		}

		// Automation runs are never "the current chat" (they fire into their own
		// hidden chat), so this branch sits ahead of the focus/visible-chat gate
		// that chat:completion lives behind — a scheduled run finishing while the
		// user reads something else is exactly the case worth telling them about.
		if (type === 'automation:completed') {
			automationsUnreadCount.update((count) => count + 1);

			if ($isLastActiveTab && ($settings?.notificationEnabled ?? false)) {
				new Notification(`${data?.title} • ${$WEBUI_NAME}`, {
					body: data?.content,
					icon: `${WEBUI_BASE_URL}/static/favicon.png`
				});
			}

			toast.custom(NotificationToast, {
				componentProps: {
					onClick: () => {
						goto(data?.chat_id ? `/c/${data.chat_id}` : '/automations');
					},
					content: data?.content,
					title: data?.title
				},
				duration: 15000,
				unstyled: true
			});
			return;
		}

		if (type === 'token-usage:update') {
			const pushedGroups = data?.groups && typeof data.groups === 'object' ? data.groups : {};
			const names = Array.isArray(data?.names) ? data.names : null;
			tokenUsageGroups.update((current) => {
				const next = { ...(current ?? {}) };
				if (names) {
					for (const name of Object.keys(next)) {
						if (!names.includes(name)) delete next[name];
					}
				}
				for (const [name, group] of Object.entries(pushedGroups)) {
					next[name] = group;
				}
				return next;
			});
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
							new Notification(`${title} • ${$WEBUI_NAME}`, {
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
		const channel = page.url.pathname.includes(`/channels/${event.channel_id}`);
		const windowFocused = await isWindowFocused();

		if ((!channel || !windowFocused) && event?.user?.id !== $user?.id) {
			await tick();
			const type = event?.data?.type ?? null;
			const data = event?.data?.data ?? null;

			if (type === 'message') {
				if ($isLastActiveTab) {
					if ($settings?.notificationEnabled ?? false) {
						new Notification(`${data?.user?.name} (#${event?.channel?.name}) • ${$WEBUI_NAME}`, {
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

	let teardownKeyboardViewport = null;
	let teardownMeltTouchDismiss = null;
	let teardownPreloadErrorHandler = null;

	onDestroy(() => {
		teardownKeyboardViewport?.();
		teardownMeltTouchDismiss?.();
		teardownPreloadErrorHandler?.();
		activeTabBc?.close();
		eventsBc?.close();
		if (tokenTimer) clearInterval(tokenTimer);
		if (versionPollTimer) clearInterval(versionPollTimer);
		if (sidebarReconcileTimer) clearTimeout(sidebarReconcileTimer);
		$socket?.off('events', chatEventHandler);
		$socket?.off('events:channel', channelEventHandler);
	});

	// Subtle native-feel motion on route changes. Independent of the PTR
	// (pull-to-refresh) touch handling below — do not merge with it.
	// Skipped on mobile: startViewTransition snapshots the full DOM twice, which
	// costs a visible frame hitch on phones for chat-sized pages — and it fires
	// on popstate too, compounding the sidebar edge-gesture stutter.
	onNavigate((navigation) => {
		if (!document.startViewTransition) return;
		if ($mobile) return;
		if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;

		return new Promise((resolve) => {
			document.startViewTransition(async () => {
				resolve();
				await navigation.complete;
			});
		});
	});

	onMount(async () => {
		teardownKeyboardViewport = initKeyboardViewport();
		teardownMeltTouchDismiss = initMeltTouchDismiss();

		let touchstartY = 0;

		// Scope PTR arming to the sidebar's own region only. `window.scrollY` is
		// not a usable "at the top" signal in this app — the outer <html> has
		// overflow-y: hidden and all real scrolling happens in inner containers
		// — and `document.querySelector('nav')` would grab whichever <nav>
		// happens to be first in document order (e.g. the chat Navbar), letting
		// the gesture arm from unrelated parts of the UI. `#sidebar` is the
		// sidebar's own container (see Sidebar.svelte), so anchor to that.
		function isNavOrDescendant(el) {
			const sidebarEl = document.getElementById('sidebar');
			return sidebarEl && (el === sidebarEl || sidebarEl.contains(el));
		}

		// A permanently-installed non-passive touchmove on `document` disables the
		// compositor's fast scroll path for EVERY touch scroll in the app — iOS
		// must round-trip each move through the main thread just in case we call
		// preventDefault. Instead, the non-passive listener exists only for the
		// duration of a single-finger touch that (a) started inside the sidebar
		// and (b) started with the sidebar's scroll region at the top — the only
		// state PTR can arm from. All other touches never see a cancelable
		// listener.
		//
		// The end/cancel handlers are attached to the gesture's TARGET node, not
		// document: touch events keep firing at the original target for the whole
		// gesture even if that node is detached mid-pull (e.g. a socket event
		// re-buckets the chat row), but then they no longer bubble to document —
		// a document-level touchend would never run and the armed non-passive
		// touchmove plus the spinner would leak into the next unrelated touch.
		let ptrMoveArmed = false;
		let ptrTarget = null;
		let ptrTouchId = null;

		const onPtrTouchMove = (e) => {
			const t = Array.from(e.touches).find((touch) => touch.identifier === ptrTouchId);
			if (!t) return;
			const touchDiff = t.clientY - touchstartY;
			if (touchDiff > 50) {
				showRefresh = true;
				if (e.cancelable) e.preventDefault();
			}
		};

		const disarmPtr = () => {
			if (!ptrMoveArmed) return;
			ptrMoveArmed = false;
			ptrTouchId = null;
			document.removeEventListener('touchmove', onPtrTouchMove);
			if (ptrTarget) {
				ptrTarget.removeEventListener('touchend', onPtrTouchEnd);
				ptrTarget.removeEventListener('touchcancel', onPtrTouchCancel);
				ptrTarget = null;
			}
		};

		const onPtrTouchEnd = async (e) => {
			if (!Array.from(e.changedTouches).some((t) => t.identifier === ptrTouchId)) return;
			const wasArmed = ptrMoveArmed;
			disarmPtr();
			if (!wasArmed || !showRefresh) return;
			const minSpinnerMs = 500;
			const startedAt = Date.now();
			try {
				if (!localStorage?.token) throw new Error('missing token');
				await refreshSidebarSnapshot(localStorage.token, 'ptr:refresh');
				const elapsed = Date.now() - startedAt;
				if (elapsed < minSpinnerMs) {
					await new Promise((resolve) => setTimeout(resolve, minSpinnerMs - elapsed));
				}
				showRefresh = false;
			} catch (err) {
				console.error('pull-to-refresh soft refresh failed, falling back to reload', err);
				location.reload();
			}
		};

		const onPtrTouchCancel = () => {
			disarmPtr();
			// A cancelled pull must not leave the spinner armed for the next tap.
			showRefresh = false;
		};

		document.addEventListener(
			'touchstart',
			(e) => {
				if (ptrMoveArmed) {
					// A second finger landed mid-pull — this is a multi-touch gesture,
					// not a refresh pull. Stand down entirely.
					disarmPtr();
					showRefresh = false;
					return;
				}
				if (e.touches.length !== 1) return;
				if (!isNavOrDescendant(e.target)) return;
				// Only arm when the sidebar list can't scroll further up — a pull
				// mid-list is a scroll, not a refresh gesture.
				const scroller = document.getElementById('sidebar-scroll-container');
				if (scroller && scroller.scrollTop > 0) return;
				const touch = e.changedTouches[0];
				touchstartY = touch.clientY;
				ptrTouchId = touch.identifier;
				ptrTarget = e.target;
				ptrMoveArmed = true;
				ptrTarget.addEventListener('touchend', onPtrTouchEnd);
				ptrTarget.addEventListener('touchcancel', onPtrTouchCancel);
				document.addEventListener('touchmove', onPtrTouchMove, { passive: false });
			},
			{ passive: true }
		);

		if (typeof window !== 'undefined' && window.applyTheme) {
			window.applyTheme();
		}

		// Ask the browser to keep our storage durable. On a flaky mobile link the
		// service worker's Cache API is the difference between an instant load and
		// re-pulling ~1.1 MB of app shell; without this, mobile browsers evict it
		// "best-effort" under storage pressure. Granted automatically once the app
		// has engagement / is installed to the home screen. No-op where unsupported;
		// never blocks startup.
		try {
			if (navigator?.storage?.persist && navigator?.storage?.persisted) {
				if (!(await navigator.storage.persisted())) {
					// Eviction detection: storage is no longer persistent (browser may
					// have evicted our shell). Re-request persistence. Deliberately NO
					// automatic re-warm of caches here — that would burn metered data.
					await navigator.storage.persist();
				}
			}
			if (navigator?.storage?.estimate) {
				navigator.storage
					.estimate()
					.then((est) => console.log('storage estimate', est))
					.catch(() => {});
			}
		} catch (e) {
			// ignore — durability is best-effort
		}

		// Manual version polling (Wire item #10): only while the tab is visible, so
		// a backgrounded PWA on a metered link never fetches version.json. The
		// redeploy-forces-reload path (updated.current -> beforeNavigate) is intact.
		versionPollTimer = setInterval(() => {
			if (!document.hidden) {
				try {
					updated
						.check()
						.then((stale) => stale && ensureFreshShellOnce())
						.catch(() => {});
				} catch (e) {
					// non-fatal
				}
			}
		}, VERSION_POLL_INTERVAL_MS);

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

		// Stale-boot guard. Navigations are served cache-first by the service
		// worker (that's what makes a warm open instant), so the first boot after
		// a redeploy can run the previous build. Detect it via the version probe
		// and reload once — but only inside the early-boot window, never while
		// the user is typing, and never twice in quick succession (a broken
		// version.json must not reload-loop).
		const STALE_BOOT_WINDOW_MS = 15_000;
		const STALE_BOOT_RELOAD_GUARD_KEY = 'staleBootReloadAt';
		const bootAt = Date.now();

		const refreshShellViaSW = () =>
			new Promise((resolve) => {
				const ctrl = navigator.serviceWorker?.controller;
				if (!ctrl) return resolve(false);
				const channel = new MessageChannel();
				const timer = setTimeout(() => resolve(false), 5000);
				channel.port1.onmessage = () => {
					clearTimeout(timer);
					resolve(true);
				};
				try {
					ctrl.postMessage({ type: 'refresh-shell' }, [channel.port2]);
				} catch (e) {
					clearTimeout(timer);
					resolve(false);
				}
			});

		// Mid-session update detection (resume checks / 5-min poll): re-cache the
		// shell once so the beforeNavigate hard-reload lands on the new build
		// directly instead of bouncing through a second stale boot.
		let shellRefreshRequested = false;
		const ensureFreshShellOnce = () => {
			if (shellRefreshRequested) return;
			shellRefreshRequested = true;
			refreshShellViaSW();
		};

		const isUserTyping = () => {
			const active = document.activeElement;
			return Boolean(
				active &&
					(active.tagName === 'TEXTAREA' || active.tagName === 'INPUT' || active.isContentEditable)
			);
		};

		// Vite emits this when an already-open tab asks for a lazy chunk from the
		// build it booted with after a deploy has replaced those hashed files.
		// Without a handler, the import promise rejects invisibly (for example,
		// clicking Settings simply closes the user menu and renders nothing).
		// Refresh the SW's cache-first shell before reloading so the replacement
		// page and its chunk graph come from the same build.
		const handleVitePreloadError = async (event) => {
			event.preventDefault();
			console.warn('Lazy frontend chunk is stale; reloading the current build', event.payload);

			// A background lazy import must never eat an in-progress prompt. The
			// next explicit action/navigation will retry, while the normal version
			// update path keeps preparing the fresh shell in the background.
			if (isUserTyping()) {
				ensureFreshShellOnce();
				return;
			}

			await refreshShellViaSW();
			location.reload();
		};
		window.addEventListener('vite:preloadError', handleVitePreloadError);
		teardownPreloadErrorHandler = () => {
			window.removeEventListener('vite:preloadError', handleVitePreloadError);
			teardownPreloadErrorHandler = null;
		};

		const checkStaleBoot = async () => {
			let stale = false;
			try {
				stale = await updated.check();
			} catch (e) {
				return; // offline / version probe unavailable — nothing to do
			}
			if (!stale) return;
			if (Date.now() - bootAt > STALE_BOOT_WINDOW_MS) return; // beforeNavigate handles it
			if (isUserTyping()) return;
			let lastReloadAt = 0;
			try {
				lastReloadAt = Number(sessionStorage.getItem(STALE_BOOT_RELOAD_GUARD_KEY) || 0);
			} catch (e) {
				/* sessionStorage unavailable — handled below */
			}
			if (Date.now() - lastReloadAt < 60_000) return;
			// The SW must re-cache the fresh shell BEFORE we reload, or the reload
			// gets served the same stale shell we're escaping.
			await refreshShellViaSW();
			// The SW round-trip can take seconds on a slow link — the user may have
			// started typing meanwhile; a reload now would eat their draft.
			if (isUserTyping()) return;
			try {
				sessionStorage.setItem(STALE_BOOT_RELOAD_GUARD_KEY, String(Date.now()));
			} catch (e) {
				// Can't persist the loop guard (storage-blocked webview) — an auto
				// reload here could loop forever on a persistently-stale shell.
				// Skip; the beforeNavigate update path still picks up the new build.
				return;
			}
			location.reload();
		};

		// The boot-time stale check below owns the first version probe; resume
		// events after that go through handleVisibilityChange as before.
		let bootVersionCheckDone = false;

		// Set yourself as the last active tab when this tab is focused
		const handleVisibilityChange = () => {
			if (document.visibilityState === 'visible') {
				isLastActiveTab.set(true); // This tab is now the active tab
				activeTabBc?.postMessage('active'); // Notify other tabs that this tab is active
				if (hiddenAt && Date.now() - hiddenAt > SIDEBAR_RECONCILE_AFTER_HIDDEN_MS) {
					maybeScheduleSidebarReconcile('visibility:resume');
				}
				hiddenAt = 0;

				// Returning from background (iOS freezes PWA JS entirely): the socket is
				// often dead but its reconnect timer hasn't fired yet — and if we paused
				// reconnection while offline, nothing else will revive it. Probe now so
				// live updates resume within the first moments of the app being visible.
				if (localStorage.token && $socket && !$socket.connected) {
					$socket.connect();
				}

				// Manual version poll (svelte.config pollInterval is 0): check once on
				// resume so a redeploy still forces a reload without polling while
				// hidden. The initial synthetic call skips this — checkStaleBoot owns
				// the boot-time probe (and reacts to it).
				if (bootVersionCheckDone) {
					try {
						updated
							.check()
							.then((stale) => stale && ensureFreshShellOnce())
							.catch(() => {});
					} catch (e) {
						// non-fatal
					}
				}

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

		// Boot-time version probe + stale-boot reload (fire-and-forget; never
		// blocks boot). Runs regardless of outcome so resume-time checks resume.
		checkStaleBoot().finally(() => {
			bootVersionCheckDone = true;
		});

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

		const onOnline = () => {
			online.set(true);
			// Resume the socket promptly instead of waiting for the next scheduled
			// reconnect attempt (or forever, if we paused the loop while offline).
			if ($socket && !$socket.connected) {
				$socket.connect();
			}
		};
		const onOffline = () => {
			online.set(false);
			// A dead link means every reconnect attempt (~1-5s apart, indefinitely)
			// is a wasted radio wake-up. Pause the loop; onOnline/visibility resume it.
			// Only when already disconnected — navigator.onLine has false positives,
			// and a still-healthy connection should be left alone.
			if ($socket && !$socket.connected) {
				$socket.disconnect();
			}
		};
		window.addEventListener('online', onOnline);
		window.addEventListener('offline', onOffline);

		user.subscribe((value) => {
			if (value) {
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
				if (i18next?.options?.interpolation?.defaultVariables)
					i18next.options.interpolation.defaultVariables.WEBUI_NAME = backendConfig.name;
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
				const cachedBundleEtag = localStorage.getItem(bundleEtagKey);
				const cachedBundle = readBootstrapCache(cachedUserId);

				// Stale-while-revalidate boot (Wire item #4): if a cached bundle exists,
				// fill the pending component map synchronously so the child layouts
				// (app layout + Sidebar) consume cached components on their first mount
				// instead of each doing its own per-component network fetch. The network
				// bootstrap below then revalidates and re-applies ONLY changed components.
				let bootstrapCachePrefilled = false;
				if (cachedBundle?.components && Object.keys(cachedBundle.components).length > 0) {
					setBootstrapComponents(cachedBundle.components);
					bootstrapCachePrefilled = true;
					// bootstrapDeliveredSidebar (which lets the first socket connect skip
					// its reconcile, Wire item #1a) is NOT set here: at this point the
					// components are an unvalidated localStorage cache. It is set below
					// only once the server confirms them (304) or replaces them (200) —
					// an offline boot must leave it false so the eventual reconnect
					// reconciles the stale rows instead of trusting them as fresh.
				}

				// Contract 1: send the per-component etags we already have cached so the
				// server can omit unchanged component bodies from a 200 response.
				const bootstrapEtags = buildBootstrapEtagsHeader(
					cachedBundle?.components_etags,
					BOOTSTRAP_INCLUDE
				);

				const bootstrapPromise = getBootstrap(localStorage.token, {
					include: BOOTSTRAP_INCLUDE,
					ifNoneMatch: cachedBundleEtag,
					bootstrapEtags
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
					// Whole bundle unchanged — the prefilled cache stands, now
					// server-validated, so the first connect's reconcile is redundant.
					if (!bootstrapCachePrefilled && cachedBundle?.components) {
						setBootstrapComponents(cachedBundle.components);
					}
					if (
						cachedBundle?.components &&
						('chats' in cachedBundle.components ||
							'folders' in cachedBundle.components ||
							'channels' in cachedBundle.components)
					) {
						bootstrapDeliveredSidebar = true;
					}
					_config = cachedBundle?.components?.config ?? null;
					_sessionUser = cachedBundle?.components?.user ?? null;
				} else if (bootstrap && bootstrap.status === 200) {
					// Reconstruct the full component set: changed bodies from the response +
					// unchanged bodies from cache. Any unchanged component missing from cache
					// forces one full (header-less) re-fetch (Contract 1).
					let { components: fullComponents, missing } = applyUnchangedFromCache(
						bootstrap,
						cachedBundle
					);
					let etags = bootstrap.components_etags;
					let changedComponents = bootstrap.components;
					if (missing.length > 0) {
						const full = await getBootstrap(localStorage.token, {
							include: BOOTSTRAP_INCLUDE
						}).catch(() => null);
						if (full && full.status === 200) {
							fullComponents = full.components;
							etags = full.components_etags;
							changedComponents = full.components;
						}
					}

					// Re-fill pending with ONLY the changed components so already-mounted
					// children re-consume just those (via the bootstrapRevision bump below).
					setBootstrapComponents(changedComponents);

					// Key the cache by the *fetched* user id, not the pre-fetch one: on a
					// fresh login sessionUser isn't in localStorage yet, so cachedUserId is
					// null and writing under the anon key made the NEXT boot (which resolves
					// the real id) miss the cache and re-download the full bundle once.
					const resolvedUserId = fullComponents.user?.id ?? cachedUserId;
					writeBootstrapCache(resolvedUserId, {
						components: fullComponents,
						components_etags: etags ?? {}
					});
					if (bootstrap.bundle_etag) {
						localStorage.setItem(BOOTSTRAP_BUNDLE_ETAG_KEY(resolvedUserId), bootstrap.bundle_etag);
					}

					// fullComponents = fresh bodies + cache entries the server confirmed
					// unchanged — everything in it is server-validated, so the first
					// connect's reconcile is redundant if the sidebar set is present.
					if (
						'chats' in fullComponents ||
						'folders' in fullComponents ||
						'channels' in fullComponents
					) {
						bootstrapDeliveredSidebar = true;
					}

					_config = fullComponents.config ?? null;
					_sessionUser = fullComponents.user ?? null;

					// If the child layouts already mounted (warm boot painted from cache),
					// nudge them to re-consume the freshly-changed components.
					if (bootstrapCachePrefilled) {
						bootstrapRevision.update((n) => n + 1);
					}
				}

				// Only an explicit auth rejection may sign the user out below. A
				// transient failure (network drop mid-boot, 5xx during a partial
				// outage) must keep the cached session: wiping the token would also
				// kill every future offline boot until the user can log in again.
				let authRejected = bootstrap?.status === 401;

				// Fallback when /api/bootstrap is unavailable (backend not yet upgraded) or
				// when a 304 arrived but the cached body was evicted (don't wrongly sign out).
				if (!bootstrap || (bootstrap.status === 304 && !_sessionUser)) {
					const [fallbackConfig, fallbackUser] = await Promise.all([
						getBackendConfig(),
						getSessionUser(localStorage.token).catch((error) => {
							if (error?.status === 401 || error?.status === 403) {
								authRejected = true;
							} else if (navigator.onLine !== false) {
								// Suppress the toast when we already know we're offline — the
								// failure is expected/unactionable there. Real server errors
								// while online must still surface.
								toast.error(`${error?.detail ?? error}`);
							}
							return null;
						})
					]);
					_config = fallbackConfig ?? _config;
					_sessionUser = fallbackUser ?? _sessionUser;
				}

				// Keep the cached config if the network delivered none (e.g. a 401 during
				// revalidation) so we don't fall through to /error before the /auth
				// redirect below runs the existing sign-out flow (Wire item #4).
				backendConfig = _config ?? backendConfig;

				if (_config) {
					localStorage.setItem('backendConfig', JSON.stringify(_config));
				}

				if (_sessionUser) {
					localStorage.setItem('sessionUser', JSON.stringify(_sessionUser));
					await user.set(_sessionUser);
				} else if (!authRejected && cachedUserId) {
					// Couldn't revalidate the session, but nothing rejected it either —
					// keep the cached session (already in the user store from the SWR
					// block above); the next successful bootstrap revalidates or 401s.
				} else {
					localStorage.removeItem('token');
					localStorage.removeItem('sessionUser');
					const currentUrl = `${window.location.pathname}${window.location.search}`;
					if (page.url.pathname !== '/auth' && !page.url.pathname.startsWith('/s/')) {
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
				if (page.url.pathname !== '/auth' && !page.url.pathname.startsWith('/s/')) {
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
			if (i18next?.options?.interpolation?.defaultVariables)
				i18next.options.interpolation.defaultVariables.WEBUI_NAME = backendConfig.name;

			if ($config) {
				await setupSocket($config.features?.enable_websocket ?? true);
			}

			// Seed the automations badge once per boot. Kept off the bootstrap
			// bundle deliberately: it is a volatile counter, and putting volatile
			// fields in a cached bootstrap component is what makes a warm open
			// render stale numbers.
			if ($config?.features?.enable_automations && localStorage.token) {
				automationsUnreadCount.set(
					await getUnreadRunCount(localStorage.token).catch(() => 0)
				);
			}
		} else {
			// Redirect to /error when Backend Not Detected. If we already know
			// we're offline, flag it so the error page can show a friendlier,
			// actionable "you're offline" message instead of a generic error.
			await goto(navigator.onLine === false ? `/error?offline=1` : `/error`);
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
			window.removeEventListener('online', onOnline);
			window.removeEventListener('offline', onOffline);
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
	<div class="fixed top-0 inset-x-0 z-999 flex justify-center pt-safe py-2 pointer-events-none">
		<Spinner className="size-5" />
	</div>
{/if}

{#if loaded}
	{#if $isApp}
		<div class="flex flex-row h-screen">
			<AppSidebar />

			<div class="w-full flex-1 max-w-[calc(100%-4.5rem)]">
				{@render children?.()}
			</div>
		</div>
	{:else}
		{@render children?.()}
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
