<script lang="ts">
	import { nonpassive } from '$lib/utils/eventModifiers';

	import { toast } from '$lib/utils/toast';
	import { v4 as uuidv4 } from 'uuid';

	import { beforeNavigate, goto } from '$app/navigation';
	import {
		user,
		chats,
		settings,
		showSettings,
		chatId,
		tags,
		folders as _folders,
		showSidebar,
		showSearch,
		mobile,
		online,
		showArchivedChats,
		pinnedChats,
		scrollPaginationEnabled,
		currentChatPage,
		temporaryChatEnabled,
		channels,
		socket,
		config,
		isApp,
		models,
		selectedFolder,
		WEBUI_NAME,
		bootstrapRevision,
		messageEditingIds,
		automationsUnreadCount
	} from '$lib/stores';
	import { onMount, getContext, tick, onDestroy } from 'svelte';
	import type { Writable } from 'svelte/store';
	import type { i18n as i18nType } from 'i18next';

	const i18n: Writable<i18nType> = getContext('i18n');

	import {
		getChatList,
		getAllTags,
		getPinnedChatList,
		toggleChatPinnedStatusById,
		getChatById,
		updateChatFolderIdById,
		importChat
	} from '$lib/apis/chats';
	import { createNewFolder, getFolders, updateFolderParentIdById } from '$lib/apis/folders';
	import { consumeBootstrap, hasBootstrap } from '$lib/utils/bootstrap';
	import { getTimeRange } from '$lib/utils';
	import { WEBUI_BASE_URL } from '$lib/constants';
	import { readLocalStorageCache, writeLocalStorageCache } from '$lib/utils/cache';
	import {
		offlineChatMeta,
		refreshOfflineChatMeta,
		scheduleOfflinePrefetch,
		requestPersistentStorage,
		isOfflineChatStorageEnabled
	} from '$lib/offline/manager';
	import {
		SIDEBAR_CHANNELS_CACHE_KEY,
		SIDEBAR_FOLDERS_CACHE_KEY,
		SIDEBAR_TAGS_CACHE_KEY,
		SIDEBAR_PINNED_CHATS_CACHE_KEY,
		SIDEBAR_CHATS_CACHE_KEY,
		getSidebarCacheKey as buildSidebarCacheKey
	} from '$lib/constants/cache';

	import ArchivedChatsModal from './ArchivedChatsModal.svelte';
	import UserMenu from './Sidebar/UserMenu.svelte';
	import ChatItem from './Sidebar/ChatItem.svelte';
	import Spinner from '../common/Spinner.svelte';
	import Loader from '../common/Loader.svelte';
	import Folder from '../common/Folder.svelte';
	import Tooltip from '../common/Tooltip.svelte';
	import Folders from './Sidebar/Folders.svelte';
	import { getChannels, createNewChannel } from '$lib/apis/channels';
	import ChannelModal from './Sidebar/ChannelModal.svelte';
	import ChannelItem from './Sidebar/ChannelItem.svelte';
	import PencilSquare from '../icons/PencilSquare.svelte';
	import Search from '../icons/Search.svelte';
	import SearchModal from './SearchModal.svelte';
	import FolderModal from './Sidebar/Folders/FolderModal.svelte';
	import Sidebar from '../icons/Sidebar.svelte';
	import PinnedModelList from './Sidebar/PinnedModelList.svelte';
	import Note from '../icons/Note.svelte';
	import Clock from '../icons/Clock.svelte';
	import { slide } from 'svelte/transition';

	const BREAKPOINT = 768;

	let scrollTop = $state(0);

	let navElement = $state();
	let shiftKey = $state(false);

	let selectedChatId: string | null = $state(null);
	let pendingChatId: string | null = $state(null);
	let activeChatId = $derived(pendingChatId ?? $chatId);
	$effect(() => {
		if (pendingChatId && $chatId === pendingChatId) {
			pendingChatId = null;
		}
	});
	let showPinnedChat = $state(true);

	let showCreateChannel = $state(false);

	// Pagination variables
	let chatListLoading = $state(false);
	let allChatsLoaded = $state(false);

	let showCreateFolderModal = $state(false);

	let folders = $state({});
	let folderRegistry = $state({});

	let newFolderId = null;

	let channelsRefreshPromise: Promise<void> | null = null;
	let chatListRefreshPromise: Promise<void> | null = null;

	// Set true once the sidebar stores have been hydrated (from bootstrap/network/
	// a revalidation) this session. While the socket is connected, push events keep
	// the stores live, so re-opening the sidebar can paint from stores and skip the
	// refetch fan-out (Wire item #6).
	let sidebarHydratedThisSession = false;
	let unsubBootstrapRevision: (() => void) | null = null;

	const getSidebarCacheKey = (name: string) => buildSidebarCacheKey($user?.id, name);

	const buildFolderTree = (folderList: any[]) => {
		const sortedFolderList = [...folderList].sort((a, b) => b.updated_at - a.updated_at);

		folders = {};

		// First pass: Initialize all folder entries
		for (const folder of sortedFolderList) {
			// Ensure folder is added to folders with its data
			folders[folder.id] = { ...(folders[folder.id] || {}), ...folder };

			if (newFolderId && folder.id === newFolderId) {
				folders[folder.id].new = true;
				newFolderId = null;
			}
		}

		// Second pass: Tie child folders to their parents
		for (const folder of sortedFolderList) {
			if (folder.parent_id) {
				// Ensure the parent folder is initialized if it doesn't exist
				if (!folders[folder.parent_id]) {
					folders[folder.parent_id] = {}; // Create a placeholder if not already present
				}

				// Initialize childrenIds array if it doesn't exist and add the current folder id
				folders[folder.parent_id].childrenIds = folders[folder.parent_id].childrenIds
					? [...folders[folder.parent_id].childrenIds, folder.id]
					: [folder.id];

				// Sort the children by updated_at field
				folders[folder.parent_id].childrenIds.sort((a, b) => {
					return folders[b].updated_at - folders[a].updated_at;
				});
			}
		}
	};

	const applyFolderList = (folderList: any[]) => {
		const sortedFolderList = [...folderList].sort((a, b) => b.updated_at - a.updated_at);
		_folders.set(sortedFolderList);
		buildFolderTree(sortedFolderList);
	};

	const hydrateSidebarDataFromCache = () => {
		const cachedChannels = readLocalStorageCache<any[]>(
			SIDEBAR_CHANNELS_CACHE_KEY,
			getSidebarCacheKey('channels')
		);
		if (Array.isArray(cachedChannels)) {
			channels.set(cachedChannels);
		}

		const cachedFolders = readLocalStorageCache<any[]>(
			SIDEBAR_FOLDERS_CACHE_KEY,
			getSidebarCacheKey('folders')
		);
		if (Array.isArray(cachedFolders)) {
			applyFolderList(cachedFolders);
		}

		const cachedTags = readLocalStorageCache<any[]>(
			SIDEBAR_TAGS_CACHE_KEY,
			getSidebarCacheKey('tags')
		);
		if (Array.isArray(cachedTags)) {
			tags.set(cachedTags);
		}

		const cachedPinnedChats = readLocalStorageCache<any[]>(
			SIDEBAR_PINNED_CHATS_CACHE_KEY,
			getSidebarCacheKey('pinned-chats')
		);
		if (Array.isArray(cachedPinnedChats)) {
			pinnedChats.set(cachedPinnedChats);
		}

		const cachedChats = readLocalStorageCache<any[]>(
			SIDEBAR_CHATS_CACHE_KEY,
			getSidebarCacheKey('chats:first-page')
		);
		if (Array.isArray(cachedChats)) {
			chats.set(cachedChats);
		}
	};

	// Apply ONLY the sidebar components present in the pending bootstrap map,
	// without any network fetch. Used when a background bootstrap revalidation
	// (Wire item #4) re-fills the map with just the changed components and bumps
	// bootstrapRevision. Missing components are left untouched (push events / the
	// existing stores keep them current).
	const applyBootstrapSidebarComponents = () => {
		if (hasBootstrap('channels')) {
			const c = consumeBootstrap<any[]>('channels');
			if (Array.isArray(c)) {
				channels.set(c);
				writeLocalStorageCache(SIDEBAR_CHANNELS_CACHE_KEY, getSidebarCacheKey('channels'), c);
			}
		}
		if (hasBootstrap('folders')) {
			const f = consumeBootstrap<any[]>('folders');
			if (Array.isArray(f)) {
				applyFolderList(f);
				writeLocalStorageCache(SIDEBAR_FOLDERS_CACHE_KEY, getSidebarCacheKey('folders'), f);
			}
		}
		if (hasBootstrap('tags')) {
			const t = consumeBootstrap<any[]>('tags');
			if (Array.isArray(t)) {
				tags.set(t);
				writeLocalStorageCache(SIDEBAR_TAGS_CACHE_KEY, getSidebarCacheKey('tags'), t);
			}
		}
		if (hasBootstrap('pinned')) {
			const p = consumeBootstrap<any[]>('pinned');
			if (Array.isArray(p)) {
				const decorated = p.map((c) => ({ ...c, time_range: getTimeRange(c.updated_at) }));
				pinnedChats.set(decorated);
				writeLocalStorageCache(
					SIDEBAR_PINNED_CHATS_CACHE_KEY,
					getSidebarCacheKey('pinned-chats'),
					decorated
				);
			}
		}
		if (hasBootstrap('chats')) {
			const ch = consumeBootstrap<any[]>('chats');
			if (Array.isArray(ch)) {
				const decorated = ch.map((c) => ({ ...c, time_range: getTimeRange(c.updated_at) }));
				chats.set(decorated);
				currentChatPage.set(1);
				scrollPaginationEnabled.set(true);
				writeLocalStorageCache(
					SIDEBAR_CHATS_CACHE_KEY,
					getSidebarCacheKey('chats:first-page'),
					decorated
				);
			}
		}
		sidebarHydratedThisSession = true;

		// Freshly revalidated rows — sweep offline copies against them.
		if (isOfflineChatStorageEnabled() && $user?.id) {
			scheduleOfflinePrefetch(localStorage.token, $user.id, 4000);
		}
	};

	const initFolders = async () => {
		const bootstrapFolders = consumeBootstrap<any[]>('folders');
		if (Array.isArray(bootstrapFolders)) {
			applyFolderList(bootstrapFolders);
			writeLocalStorageCache(
				SIDEBAR_FOLDERS_CACHE_KEY,
				getSidebarCacheKey('folders'),
				bootstrapFolders
			);
			return;
		}
		const folderList = await getFolders(localStorage.token).catch((error) => {
			toast.error(`${error}`);
			return [];
		});

		applyFolderList(folderList);
		writeLocalStorageCache(SIDEBAR_FOLDERS_CACHE_KEY, getSidebarCacheKey('folders'), folderList);
	};

	const createFolder = async ({ name, data }) => {
		if (name === '') {
			toast.error($i18n.t('Folder name cannot be empty.'));
			return;
		}

		const rootFolders = Object.values(folders).filter((folder) => folder.parent_id === null);
		if (rootFolders.find((folder) => folder.name.toLowerCase() === name.toLowerCase())) {
			// If a folder with the same name already exists, append a number to the name
			let i = 1;
			while (
				rootFolders.find((folder) => folder.name.toLowerCase() === `${name} ${i}`.toLowerCase())
			) {
				i++;
			}

			name = `${name} ${i}`;
		}

		// Add a dummy folder to the list to show the user that the folder is being created
		const tempId = uuidv4();
		folders = {
			...folders,
			tempId: {
				id: tempId,
				name: name,
				created_at: Date.now(),
				updated_at: Date.now()
			}
		};

		const res = await createNewFolder(localStorage.token, {
			name,
			data
		}).catch((error) => {
			toast.error(`${error}`);
			return null;
		});

		if (res) {
			// newFolderId = res.id;
			await initFolders();
		}
	};

	const initChannels = async () => {
		if (channelsRefreshPromise) {
			return channelsRefreshPromise;
		}

		channelsRefreshPromise = (async () => {
			const bootstrapChannels = consumeBootstrap<any[]>('channels');
			if (Array.isArray(bootstrapChannels)) {
				await channels.set(bootstrapChannels);
				writeLocalStorageCache(
					SIDEBAR_CHANNELS_CACHE_KEY,
					getSidebarCacheKey('channels'),
					bootstrapChannels
				);
				return;
			}
			const channelList = await getChannels(localStorage.token);
			await channels.set(channelList);
			writeLocalStorageCache(
				SIDEBAR_CHANNELS_CACHE_KEY,
				getSidebarCacheKey('channels'),
				channelList
			);
		})();

		try {
			return await channelsRefreshPromise;
		} finally {
			channelsRefreshPromise = null;
		}
	};

	const initChatList = async () => {
		if (chatListRefreshPromise) {
			return chatListRefreshPromise;
		}

		chatListRefreshPromise = (async () => {
			// Reset pagination variables
			console.log('initChatList');
			const firstPage = 1;
			currentChatPage.set(firstPage);
			allChatsLoaded = false;
			// Deliberately no cache pre-clear here: if this refetch is interrupted
			// (PWA suspended, network drop) a stale cache still paints instantly on
			// next boot; the writes below replace it on success.

			await initFolders();
			await Promise.all([
				(async () => {
					console.log('Init tags');
					const bootstrapTags = consumeBootstrap<any[]>('tags');
					const _tags = Array.isArray(bootstrapTags)
						? bootstrapTags
						: await getAllTags(localStorage.token);
					tags.set(_tags);
					writeLocalStorageCache(SIDEBAR_TAGS_CACHE_KEY, getSidebarCacheKey('tags'), _tags);
				})(),
				(async () => {
					console.log('Init pinned chats');
					const bootstrapPinned = consumeBootstrap<any[]>('pinned');
					const _pinnedChats = Array.isArray(bootstrapPinned)
						? bootstrapPinned.map((c) => ({ ...c, time_range: getTimeRange(c.updated_at) }))
						: await getPinnedChatList(localStorage.token);
					pinnedChats.set(_pinnedChats);
					writeLocalStorageCache(
						SIDEBAR_PINNED_CHATS_CACHE_KEY,
						getSidebarCacheKey('pinned-chats'),
						_pinnedChats
					);
				})(),
				(async () => {
					console.log('Init chat list');
					const bootstrapChats = consumeBootstrap<any[]>('chats');
					const _chats = Array.isArray(bootstrapChats)
						? bootstrapChats.map((c) => ({ ...c, time_range: getTimeRange(c.updated_at) }))
						: await getChatList(localStorage.token, firstPage);
					await chats.set(_chats);
					writeLocalStorageCache(
						SIDEBAR_CHATS_CACHE_KEY,
						getSidebarCacheKey('chats:first-page'),
						_chats
					);
				})()
			]);

			// Enable pagination
			scrollPaginationEnabled.set(true);

			// Sidebar rows are fresh now — the right moment for a stale-only
			// offline prefetch sweep (throttled internally).
			if (isOfflineChatStorageEnabled() && $user?.id) {
				scheduleOfflinePrefetch(localStorage.token, $user.id, 4000);
			}
		})();

		try {
			return await chatListRefreshPromise;
		} finally {
			chatListRefreshPromise = null;
		}
	};

	const loadMoreChats = async () => {
		if (chatListLoading) return;
		chatListLoading = true;
		try {
			// Commit the page number only after the fetch succeeds: a failed page
			// (offline scroll, transient error) must stay retryable on the next
			// intersection without skipping a page or wedging the loader.
			const nextPage = $currentChatPage + 1;
			const newChatList = await getChatList(localStorage.token, nextPage);
			currentChatPage.set(nextPage);

			// once the bottom of the list has been reached (no results) there is no need to continue querying
			allChatsLoaded = newChatList.length === 0;
			await chats.set([...($chats ? $chats : []), ...newChatList]);
		} catch (error) {
			console.error('Failed to load more chats', error);
		} finally {
			chatListLoading = false;
		}
	};

	const importChatHandler = async (items, pinned = false, folderId = null) => {
		console.log('importChatHandler', items, pinned, folderId);
		for (const item of items) {
			console.log(item);
			if (item.chat) {
				await importChat(
					localStorage.token,
					item.chat,
					item?.meta ?? {},
					pinned,
					folderId,
					item?.created_at ?? null,
					item?.updated_at ?? null
				);
			}
		}

		initChatList();
	};

	const inputFilesHandler = async (files) => {
		console.log(files);

		for (const file of files) {
			const reader = new FileReader();
			reader.onload = async (e) => {
				const content = e.target.result;

				try {
					const chatItems = JSON.parse(content);
					importChatHandler(chatItems);
				} catch {
					toast.error($i18n.t(`Invalid file format.`));
				}
			};

			reader.readAsText(file);
		}
	};

	const tagEventHandler = async (type, tagName, chatId) => {
		console.log(type, tagName, chatId);
		if (type === 'delete') {
			initChatList();
		} else if (type === 'add') {
			initChatList();
		}
	};

	let draggedOver = false;

	const onDragOver = (e) => {
		e.preventDefault();

		// Check if a file is being draggedOver.
		if (e.dataTransfer?.types?.includes('Files')) {
			draggedOver = true;
		} else {
			draggedOver = false;
		}
	};

	const onDragLeave = () => {
		draggedOver = false;
	};

	const onDrop = async (e) => {
		e.preventDefault();
		console.log(e); // Log the drop event

		// Perform file drop check and handle it accordingly
		if (e.dataTransfer?.files) {
			const inputFiles = Array.from(e.dataTransfer?.files);

			if (inputFiles && inputFiles.length > 0) {
				console.log(inputFiles); // Log the dropped files
				inputFilesHandler(inputFiles); // Handle the dropped files
			}
		}

		draggedOver = false; // Reset draggedOver status after drop
	};

	let touchstart;
	let touchend;

	function checkDirection() {
		if (!$mobile) return;

		const screenWidth = window.innerWidth;
		const dx = touchend.screenX - touchstart.screenX;
		const dy = Math.abs(touchend.screenY - touchstart.screenY);
		const swipeDistance = Math.abs(dx);
		const minDistance = Math.min(screenWidth / 8, 60);

		// Reject mostly-vertical swipes (scrolls) and short flicks.
		if (swipeDistance < minDistance || dy > swipeDistance) return;

		if (!$showSidebar) {
			// Open: must start near the left edge so we don't hijack normal taps.
			if (touchstart.clientX < 24 && dx > 0) {
				showSidebar.set(true);
			}
		} else {
			// Close: any leftward swipe with sufficient horizontal intent.
			if (dx < 0) {
				showSidebar.set(false);
			}
		}
	}

	const onTouchStart = (e) => {
		touchstart = e.changedTouches[0];
	};

	const onTouchEnd = (e) => {
		touchend = e.changedTouches[0];
		checkDirection();
	};

	// --- Interactive (finger-tracking) drag-to-open/close -------------------
	// Live-follows the finger instead of only reacting to a discrete flick.
	// Falls back to the legacy checkDirection()-based flick above when the
	// user has prefers-reduced-motion enabled.
	const SIDEBAR_DRAG_WIDTH = 260;
	const DRAG_OPEN_FRACTION = 0.4;
	const DRAG_OPEN_VELOCITY = 0.3; // px/ms
	const DRAG_INTENT_THRESHOLD = 10; // px
	const DRAG_SETTLE_MS = 200;

	let reducedMotion = false;

	// Mobile keeps the drawer panel mounted (translated offscreen + hidden +
	// inert) so opening it — by drag or tap — is pure compositor work instead of
	// mounting the whole chat list mid-gesture. Deferred to post-boot idle so
	// the hidden DOM never competes with first paint.
	let sidebarPrerenderReady = $state(false);

	// iOS standalone-PWA: the system back-swipe starts at the same left edge as
	// our open gesture. The edge strip preventDefaults touchstart (which
	// suppresses the system gesture on current iOS), but WebKit has changed this
	// across releases — as a second line of defense, cancel any history-pop
	// navigation that lands while an edge-drag has actually ENGAGED (horizontal
	// intent confirmed), so one swipe can never BOTH navigate back AND move the
	// sidebar. The stamp is cleared on touchcancel — when the OS claims the
	// touch (Android back gesture, iOS if a future WebKit ignores the
	// preventDefault) the system gesture wins and its popstate must go through —
	// and is never set by plain taps, so button/3-button-nav back is unaffected.
	let lastEdgeGestureAt = 0;
	beforeNavigate((navigation) => {
		if (navigation.type !== 'popstate') return;
		if (!$mobile) return;
		if (Date.now() - lastEdgeGestureAt < 700) {
			navigation.cancel();
		}
	});

	// True whenever the sidebar's position is being manually driven (either
	// actively dragged, or animating to its settled position after release).
	// While true the element is force-mounted and its own Svelte `transition:slide`
	// is suppressed so it never fights the manual transform.
	let manualDragControl = $state(false);
	// Set true immediately before manualDragControl is cleared as part of a
	// manually-driven close, so that the Svelte outro transition (which only
	// starts once `manualDragControl` has already flipped to false) still
	// reads a suppressed (0ms) duration instead of replaying the close.
	// Reset once that outro actually finishes (on:outroend below).
	let suppressNextOutro = $state(false);
	let dragActive = false;
	let dragSettling = $state(false);
	let dragIntent = null; // null | 'horizontal' | 'vertical'
	let dragStartX = 0;
	let dragStartY = 0;
	let dragStartTime = 0;
	let dragWasOpen = false;
	let dragX = $state(-SIDEBAR_DRAG_WIDTH);
	let dragSettleTimeout;

	let dragFraction = $derived(
		Math.min(1, Math.max(0, (dragX + SIDEBAR_DRAG_WIDTH) / SIDEBAR_DRAG_WIDTH))
	);

	const beginSidebarDrag = (touch) => {
		if (!$mobile || reducedMotion || dragActive) return;
		if (dragSettleTimeout) {
			clearTimeout(dragSettleTimeout);
			dragSettleTimeout = null;
		}
		dragActive = true;
		dragSettling = false;
		manualDragControl = true;
		dragIntent = null;
		dragStartX = touch.clientX;
		dragStartY = touch.clientY;
		dragStartTime = Date.now();
		dragWasOpen = $showSidebar;
		dragX = dragWasOpen ? 0 : -SIDEBAR_DRAG_WIDTH;
	};

	// Returns true when the caller should preventDefault() (horizontal drag confirmed).
	const updateSidebarDrag = (touch) => {
		if (!dragActive) return false;

		const dx = touch.clientX - dragStartX;
		const dy = touch.clientY - dragStartY;

		if (dragIntent === null) {
			if (Math.abs(dx) < DRAG_INTENT_THRESHOLD && Math.abs(dy) < DRAG_INTENT_THRESHOLD) {
				return false;
			}
			if (Math.abs(dy) >= Math.abs(dx)) {
				// Vertical scroll intent — abandon tracking entirely, let the
				// browser handle native scrolling, don't revisit this touch.
				dragActive = false;
				manualDragControl = false;
				dragIntent = 'vertical';
				return false;
			}
			dragIntent = 'horizontal';
		}

		if (dragIntent !== 'horizontal') return false;

		const base = dragWasOpen ? 0 : -SIDEBAR_DRAG_WIDTH;
		dragX = Math.min(0, Math.max(-SIDEBAR_DRAG_WIDTH, base + dx));
		return true;
	};

	// Clears `manualDragControl` once the manually-driven transform animation
	// has had time to finish. If the panel is ending up closed, this is also
	// the point where the Svelte `{#if}` will flip false and trigger the
	// panel's outro transition — so `suppressNextOutro` must be set *in the
	// same tick* as `manualDragControl` is cleared, not before and not after,
	// otherwise the outro either fires with the wrong duration or (if set too
	// early/late) leaks into an unrelated close/open.
	const finishSidebarSettle = (open) => {
		dragSettleTimeout = setTimeout(() => {
			dragSettling = false;
			if (!open) {
				suppressNextOutro = true;
			}
			manualDragControl = false;
			dragSettleTimeout = null;
		}, DRAG_SETTLE_MS + 20);
	};

	const settleSidebarDrag = (touch) => {
		if (!dragActive) return;

		const horizontal = dragIntent === 'horizontal';
		const dx = touch ? touch.clientX - dragStartX : 0;
		const elapsed = Math.max(1, Date.now() - dragStartTime);
		const velocity = dx / elapsed; // px/ms, positive = rightward

		let open = dragWasOpen;
		if (horizontal) {
			if (dragWasOpen) {
				const closingFraction = Math.max(0, -dx) / SIDEBAR_DRAG_WIDTH;
				open = !(closingFraction > DRAG_OPEN_FRACTION || velocity < -DRAG_OPEN_VELOCITY);
			} else {
				const openingFraction = Math.max(0, dx) / SIDEBAR_DRAG_WIDTH;
				open = openingFraction > DRAG_OPEN_FRACTION || velocity > DRAG_OPEN_VELOCITY;
			}
		}

		dragActive = false;
		dragIntent = null;

		if (!manualDragControl) return;

		dragSettling = true;
		dragX = open ? 0 : -SIDEBAR_DRAG_WIDTH;
		showSidebar.set(open);

		finishSidebarSettle(open);
	};

	const cancelSidebarDrag = () => {
		// Only an ACTIVE drag has anything to cancel. A touchcancel from a touch
		// that never began a drag (e.g. a second resting finger cancelled by the
		// OS during a settle) must not re-settle toward the stale dragWasOpen —
		// that animates the panel against the store for a full width and back.
		if (!dragActive) return;
		dragActive = false;
		dragIntent = null;
		dragSettling = true;
		dragX = dragWasOpen ? 0 : -SIDEBAR_DRAG_WIDTH;

		if (dragSettleTimeout) clearTimeout(dragSettleTimeout);
		finishSidebarSettle(dragWasOpen);
	};

	// Tap-to-close (backdrop click). This is NOT a drag — dx never crosses the
	// intent threshold so `settleSidebarDrag` leaves the sidebar "open" and
	// the actual close previously happened via a bare `showSidebar.set(false)`
	// on the synthesized click, entirely bypassing the manual-transform +
	// suppressed-outro machinery above (and racing the drag-settle timeout
	// from the preceding touchstart/touchend). Route it through the same
	// manually-driven close animation so there is exactly one close motion.
	const closeSidebarAnimated = () => {
		if (!$showSidebar && !manualDragControl) return;
		if (reducedMotion) {
			// Instant close — the subscriber skips animation under reduced motion
			// and the mobile slide duration is 0.
			showSidebar.set(false);
			return;
		}
		if (dragSettleTimeout) clearTimeout(dragSettleTimeout);
		dragActive = false;
		dragIntent = null;
		manualDragControl = true;
		dragWasOpen = true;
		dragSettling = true;
		dragX = -SIDEBAR_DRAG_WIDTH;
		showSidebar.set(false);
		finishSidebarSettle(false);
	};

	// Store-driven open/close on mobile (hamburger tap, tap-a-chat auto-close,
	// search-modal close, …). These previously animated via transition:slide,
	// which animates *width* — a full relayout every frame on a phone. Route
	// them through the same manual-transform machinery as the finger drag so
	// every open/close is a compositor-only translateX.
	const animateSidebarTo = (open) => {
		if (dragSettleTimeout) clearTimeout(dragSettleTimeout);
		dragActive = false;
		dragIntent = null;
		// If the panel has already been painted at a driven transform — mid-settle
		// (retarget), prerendered at the closed position, or currently open — the
		// CSS transition can simply be retargeted from wherever it visually is.
		// Only a fresh mount (open before the idle prerender) needs to paint at
		// the closed position for one frame first, hence the double-rAF: the
		// first rAF runs before that frame paints, the second after.
		const painted = manualDragControl || !open || sidebarPrerenderReady;
		manualDragControl = true;
		dragWasOpen = !open;
		if (painted) {
			dragSettling = true;
			dragX = open ? 0 : -SIDEBAR_DRAG_WIDTH;
		} else {
			dragSettling = false;
			dragX = -SIDEBAR_DRAG_WIDTH;
			requestAnimationFrame(() => {
				requestAnimationFrame(() => {
					// A drag or close that raced in during these frames owns the settle.
					if (!manualDragControl || dragActive || !$showSidebar) return;
					dragSettling = true;
					dragX = 0;
				});
			});
		}
		finishSidebarSettle(open);
	};

	// Edge-strip listeners — open gesture, sidebar currently closed.
	// Start coords tracked here (not via dragStartX/Y) because beginSidebarDrag
	// no-ops under reduced motion but tap-forwarding below must still work.
	let edgeStartX = 0;
	let edgeStartY = 0;
	const onEdgeTouchStart = (e) => {
		// Suppress iOS's history back-swipe, which starts from the same edge. This
		// must happen on touchstart — by first touchmove the system gesture has
		// already been committed. Costs vertical scrolling that starts in the
		// leftmost 24px, which the drag-intent logic previously allowed.
		// (Editable content under the strip needs no special-casing here: the
		// strip is unmounted while a message is in edit mode and display:none'd
		// under keyboard-open, so selection taps never reach this handler.)
		if (e.cancelable) e.preventDefault();
		edgeStartX = e.touches[0].clientX;
		edgeStartY = e.touches[0].clientY;
		beginSidebarDrag(e.touches[0]);
	};
	const onEdgeTouchMove = (e) => {
		if (!dragActive) return;
		if (updateSidebarDrag(e.touches[0])) {
			// Horizontal intent confirmed — arm the popstate guard.
			lastEdgeGestureAt = Date.now();
			e.preventDefault();
		}
	};
	const onEdgeTouchEnd = (e) => {
		const horizontal = dragIntent === 'horizontal';
		lastEdgeGestureAt = horizontal ? Date.now() : 0;
		const touch = e.changedTouches[0];
		settleSidebarDrag(touch);
		// The strip sits above the leftmost 24px of the UI (incl. part of the
		// navbar hamburger) and its touchstart preventDefault suppresses the
		// native click — forward genuine taps to whatever is underneath so that
		// column isn't tap-dead.
		if (!horizontal && touch) {
			const dx = Math.abs(touch.clientX - edgeStartX);
			const dy = Math.abs(touch.clientY - edgeStartY);
			if (dx < DRAG_INTENT_THRESHOLD && dy < DRAG_INTENT_THRESHOLD) {
				const strip = e.currentTarget;
				const under = document
					.elementsFromPoint(touch.clientX, touch.clientY)
					.find((el) => el !== strip);
				if (under instanceof HTMLElement) {
					under.click();
				}
			}
		}
	};
	const onEdgeTouchCancel = () => {
		// The OS claimed this touch (system back gesture) — its navigation must
		// go through, so stand the popstate guard down.
		lastEdgeGestureAt = 0;
		cancelSidebarDrag();
	};

	// Panel/backdrop listeners — close gesture, sidebar currently open.
	const onPanelTouchStart = (e) => {
		if (!$showSidebar || dragActive) return;
		// The iOS history back-swipe also triggers from the left edge while the
		// sidebar is open — suppress it here too (see onEdgeTouchStart). But a
		// preventDefault also kills the tap's synthetic click, and chat rows /
		// folder chevrons start ~8px from the edge — so leave interactive
		// targets alone (for those, the beforeNavigate popstate guard is still
		// armed via the timestamp below).
		if ($mobile && e.touches[0].clientX < 24) {
			lastEdgeGestureAt = Date.now();
			const target = e.target;
			const interactive =
				target instanceof Element && target.closest('a, button, input, textarea, [role="button"]');
			if (e.cancelable && !interactive) {
				e.preventDefault();
			}
		}
		beginSidebarDrag(e.touches[0]);
	};
	const onPanelTouchMove = (e) => {
		if (!dragActive) return;
		if (updateSidebarDrag(e.touches[0])) {
			// Horizontal intent confirmed — arm the popstate guard (a rightward
			// edge-swipe over the open panel is the same system back-swipe zone).
			lastEdgeGestureAt = Date.now();
			e.preventDefault();
		}
	};
	const onPanelTouchEnd = (e) => {
		if (dragIntent === 'horizontal') {
			lastEdgeGestureAt = Date.now();
		}
		settleSidebarDrag(e.changedTouches[0]);
	};
	const onPanelTouchCancel = () => {
		// System claimed the touch — its navigation must go through.
		lastEdgeGestureAt = 0;
		cancelSidebarDrag();
	};

	const onKeyDown = (e) => {
		if (e.key === 'Shift') {
			shiftKey = true;
		}
	};

	const onKeyUp = (e) => {
		if (e.key === 'Shift') {
			shiftKey = false;
		}
	};

	const onFocus = () => {};

	const onBlur = () => {
		shiftKey = false;
		selectedChatId = null;
	};

	let prevShowSidebar = null;

	let unsubscribers = [];
	onMount(async () => {
		showPinnedChat = localStorage?.showPinnedChat ? localStorage.showPinnedChat === 'true' : true;
		channels.set([]);
		_folders.set([]);
		tags.set([]);
		pinnedChats.set([]);
		chats.set(null);
		folders = {};
		hydrateSidebarDataFromCache();
		await showSidebar.set(!$mobile ? localStorage.sidebar === 'true' : false);

		// Offline-chat bookkeeping: load the availability map (cheap, meta-only)
		// so ChatItems/ChatMenu can answer "will this open offline?" and ask for
		// durable storage. The prefetch sweep itself is scheduled where FRESH
		// sidebar rows land (initChatList / bootstrap revalidation) — at mount
		// the rows are the localStorage cache and staleness checks against them
		// would miss server-side updates. Re-sweep when connectivity returns.
		if (isOfflineChatStorageEnabled() && $user?.id) {
			requestPersistentStorage();
			void refreshOfflineChatMeta($user.id);
		} else {
			// Setting off (or account switched to one with it off): a stale map
			// from a previous session/user must not drive availability dimming.
			offlineChatMeta.set(null);
		}
		let wasOnline = $online;

		unsubscribers = [
			online.subscribe((value) => {
				if (value && !wasOnline && isOfflineChatStorageEnabled() && $user?.id) {
					scheduleOfflinePrefetch(localStorage.token, $user.id, 3000);
				}
				wasOnline = value;
			}),
			_folders.subscribe((value) => {
				if (Array.isArray(value)) {
					buildFolderTree(value);
				}
			}),
			mobile.subscribe((value) => {
				// Crossing the breakpoint invalidates any pending outro-suppression
				// (set for a mobile close that, with the always-mounted panel, never
				// runs an outro) — a stale flag would zero the next desktop outro.
				suppressNextOutro = false;

				if ($showSidebar && value) {
					showSidebar.set(false);
				}

				if ($showSidebar && !value) {
					const navElement = document.getElementsByTagName('nav')[0];
					if (navElement) {
						navElement.style['-webkit-app-region'] = 'drag';
					}
				}

				if (!$showSidebar && !value) {
					showSidebar.set(true);
				}
			}),
			showSidebar.subscribe(async (value) => {
				// Animate store-driven toggles on mobile through the manual-transform
				// path. Skipped when the drag machinery already owns the motion toward
				// the SAME target (manualDragControl set BEFORE the store flipped), on
				// the initial subscription fire, and under reduced motion (instant is
				// correct there). A toggle racing into a previous toggle's settle
				// window (target disagrees) RETARGETS the transition instead of being
				// swallowed — otherwise the panel sits dead for ~220ms then pops with
				// no animation.
				const changed = prevShowSidebar !== null && prevShowSidebar !== value;
				prevShowSidebar = value;
				if (changed && $mobile && !reducedMotion && !dragActive) {
					const settleTargetOpen = dragX === 0;
					if (!manualDragControl || settleTargetOpen !== value) {
						animateSidebarTo(value);
					}
				}

				localStorage.sidebar = value;

				// nav element is not available on the first render
				const navElement = document.getElementsByTagName('nav')[0];

				if (navElement) {
					if ($mobile) {
						if (!value) {
							navElement.style['-webkit-app-region'] = 'drag';
						} else {
							navElement.style['-webkit-app-region'] = 'no-drag';
						}
					} else {
						navElement.style['-webkit-app-region'] = 'drag';
					}
				}

				if (value) {
					// Wire item #6: while the socket is connected and the stores were
					// hydrated this session, push events keep them live — paint from the
					// stores and skip the channels+folders+tags+pinned+chats refetch.
					// Refetch only when disconnected (could have missed pushes) or never
					// hydrated.
					if (sidebarHydratedThisSession && ($socket?.connected ?? false)) {
						return;
					}
					// Offline, the refetch fan-out can only fail (error toasts over a
					// perfectly good cached paint). Skip it and leave
					// sidebarHydratedThisSession false — the socket-connect reconcile
					// owns freshness once connectivity returns.
					if (!$online) {
						return;
					}
					try {
						await initChannels();
						await initChatList();
						sidebarHydratedThisSession = true;
					} catch (error) {
						// Transient failure (e.g. connection dropped mid-fan-out): keep
						// the cached paint; the reconnect reconcile refreshes.
						console.error('Sidebar refetch failed', error);
					}
				}
			})
		];

		// Wire item #4: re-consume sidebar components refreshed by a background
		// bootstrap revalidation. The initial rev === 0 fire is skipped (the normal
		// cache hydration + open-triggered init already cover first paint).
		unsubBootstrapRevision = bootstrapRevision.subscribe((rev) => {
			if (rev === 0) return;
			applyBootstrapSidebarComponents();
		});

		window.addEventListener('keydown', onKeyDown);
		window.addEventListener('keyup', onKeyUp);

		reducedMotion =
			typeof window.matchMedia === 'function' &&
			window.matchMedia('(prefers-reduced-motion: reduce)').matches;

		// Mount the hidden mobile drawer once boot is idle, so the first open
		// (drag or tap) never pays the chat-list mount cost mid-gesture.
		{
			const prerender = () => {
				sidebarPrerenderReady = true;
			};
			if (typeof window.requestIdleCallback === 'function') {
				window.requestIdleCallback(prerender, { timeout: 3000 });
			} else {
				// Safari/iOS: no requestIdleCallback.
				setTimeout(prerender, 1200);
			}
		}

		if (reducedMotion) {
			// Reduced-motion fallback: simple discrete flick-to-toggle, no
			// live finger-tracking or transform-driven animation.
			window.addEventListener('touchstart', onTouchStart);
			window.addEventListener('touchend', onTouchEnd);
		}

		window.addEventListener('focus', onFocus);
		window.addEventListener('blur', onBlur);

		const dropZone = document.getElementById('sidebar');

		dropZone?.addEventListener('dragover', onDragOver);
		dropZone?.addEventListener('drop', onDrop);
		dropZone?.addEventListener('dragleave', onDragLeave);
	});

	onDestroy(() => {
		if (unsubscribers && unsubscribers.length > 0) {
			unsubscribers.forEach((unsubscriber) => {
				if (unsubscriber) {
					unsubscriber();
				}
			});
		}

		if (unsubBootstrapRevision) {
			unsubBootstrapRevision();
			unsubBootstrapRevision = null;
		}

		window.removeEventListener('keydown', onKeyDown);
		window.removeEventListener('keyup', onKeyUp);

		if (reducedMotion) {
			window.removeEventListener('touchstart', onTouchStart);
			window.removeEventListener('touchend', onTouchEnd);
		}

		if (dragSettleTimeout) {
			clearTimeout(dragSettleTimeout);
			dragSettleTimeout = null;
		}

		window.removeEventListener('focus', onFocus);
		window.removeEventListener('blur', onBlur);

		const dropZone = document.getElementById('sidebar');

		dropZone?.removeEventListener('dragover', onDragOver);
		dropZone?.removeEventListener('drop', onDrop);
		dropZone?.removeEventListener('dragleave', onDragLeave);
	});

	const newChatHandler = async () => {
		selectedChatId = null;
		pendingChatId = null;
		selectedFolder.set(null);

		if ($user?.role !== 'admin' && $user?.permissions?.chat?.temporary_enforced) {
			await temporaryChatEnabled.set(true);
		} else {
			await temporaryChatEnabled.set(false);
		}

		setTimeout(() => {
			if ($mobile) {
				showSidebar.set(false);
			}
		}, 0);
	};

	const itemClickHandler = async () => {
		selectedChatId = null;
		pendingChatId = null;
		chatId.set('');

		if ($mobile) {
			showSidebar.set(false);
		}

		await tick();
	};

	const isWindows = /Windows/i.test(navigator.userAgent);
</script>

<ArchivedChatsModal
	bind:show={$showArchivedChats}
	onUpdate={async () => {
		await initChatList();
	}}
/>

<ChannelModal
	bind:show={showCreateChannel}
	onSubmit={async ({ name, access_control }) => {
		const res = await createNewChannel(localStorage.token, {
			name: name,
			access_control: access_control
		}).catch((error) => {
			toast.error(`${error}`);
			return null;
		});

		if (res) {
			$socket.emit('join-channels', { auth: { token: $user?.token } });
			await initChannels();
			showCreateChannel = false;
		}
	}}
/>

<FolderModal
	bind:show={showCreateFolderModal}
	onSubmit={async (folder) => {
		await createFolder(folder);
		showCreateFolderModal = false;
	}}
/>

<!-- svelte-ignore a11y_no_static_element_interactions -->

{#if $showSidebar || manualDragControl}
	<button
		type="button"
		aria-label={$i18n.t('Close Sidebar')}
		class=" {$isApp
			? ' ml-[4.5rem] md:ml-0'
			: ''} fixed md:hidden z-40 top-0 right-0 left-0 bottom-0 bg-black/60 w-full min-h-screen h-screen flex justify-center overflow-hidden overscroll-contain cursor-default"
		style={manualDragControl
			? `opacity: ${dragFraction}; transition: opacity ${dragSettling ? DRAG_SETTLE_MS : 0}ms ease-out;`
			: ''}
		onclick={() => {
			if ($mobile) {
				closeSidebarAnimated();
			} else {
				showSidebar.set(false);
			}
		}}
		use:nonpassive={['touchstart', () => onPanelTouchStart]}
		use:nonpassive={['touchmove', () => onPanelTouchMove]}
		ontouchend={onPanelTouchEnd}
		ontouchcancel={onPanelTouchCancel}
	></button>
{/if}

{#if $mobile && !$showSidebar && $messageEditingIds.size === 0}
	<!-- Left-edge strip: touch target for the drag-to-open gesture when the
	     sidebar is closed. Kept narrow so it doesn't intercept normal taps
	     or horizontally-scrollable content (e.g. code blocks) elsewhere.
	     touch-action:none + the touchstart preventDefault suppress iOS's
	     history back-swipe, which starts from this same edge.
	     Stands down entirely while a message is in edit mode (it hit-tests
	     ABOVE the edit textarea, so it would swallow selection/caret taps in
	     the leftmost 24px) and while the on-screen keyboard is up (same
	     problem for the composer — hidden via #sidebar-edge-strip CSS). -->
	<div
		id="sidebar-edge-strip"
		class="fixed top-0 left-0 h-full w-6 z-50"
		style="touch-action: none;"
		use:nonpassive={['touchstart', () => onEdgeTouchStart]}
		use:nonpassive={['touchmove', () => onEdgeTouchMove]}
		ontouchend={onEdgeTouchEnd}
		ontouchcancel={onEdgeTouchCancel}
	></div>
{/if}

<SearchModal
	bind:show={$showSearch}
	onClose={() => {
		if ($mobile) {
			showSidebar.set(false);
		}
	}}
/>

<button
	id="sidebar-new-chat-button"
	class="hidden"
	onclick={() => {
		goto('/');
		newChatHandler();
	}}
></button>

{#if !$mobile && !$showSidebar}
	<div
		class=" py-2 px-1.5 flex flex-col justify-between text-black dark:text-white hover:bg-gray-50/50 dark:hover:bg-gray-950/50 h-full border-e-hairline border-gray-50 dark:border-gray-850 z-10 transition-all"
		id="sidebar"
	>
		<button
			class="flex flex-col flex-1 {isWindows ? 'cursor-pointer' : 'cursor-[e-resize]'}"
			onclick={async () => {
				showSidebar.set(!$showSidebar);
			}}
		>
			<div class="pb-1.5">
				<Tooltip
					content={$showSidebar ? $i18n.t('Close Sidebar') : $i18n.t('Open Sidebar')}
					placement="right"
				>
					<button
						class="flex rounded-xl hover:bg-gray-100 dark:hover:bg-gray-850 transition group {isWindows
							? 'cursor-pointer'
							: 'cursor-[e-resize]'}"
						aria-label={$showSidebar ? $i18n.t('Close Sidebar') : $i18n.t('Open Sidebar')}
					>
						<div class=" self-center flex items-center justify-center size-9">
							<img
								crossorigin="anonymous"
								src="{WEBUI_BASE_URL}/static/favicon.png"
								class="sidebar-new-chat-icon size-6 rounded-full group-hover:hidden"
								alt=""
							/>

							<Sidebar className="size-5 hidden group-hover:flex" />
						</div>
					</button>
				</Tooltip>
			</div>

			<div>
				<div class="">
					<Tooltip content={$i18n.t('New Chat')} placement="right">
						<a
							class=" cursor-pointer flex rounded-xl hover:bg-gray-100 dark:hover:bg-gray-850 transition group"
							href="/"
							draggable="false"
							onclick={async (e) => {
								e.stopImmediatePropagation();
								e.preventDefault();

								goto('/');
								newChatHandler();
							}}
							aria-label={$i18n.t('New Chat')}
						>
							<div class=" self-center flex items-center justify-center size-9">
								<PencilSquare className="size-4.5" strokeWidth="2" />
							</div>
						</a>
					</Tooltip>
				</div>

				<div class="">
					<Tooltip content={$i18n.t('Search')} placement="right">
						<button
							class=" cursor-pointer flex rounded-xl hover:bg-gray-100 dark:hover:bg-gray-850 transition group"
							onclick={(e) => {
								e.stopImmediatePropagation();
								e.preventDefault();

								showSearch.set(true);
							}}
							draggable="false"
							aria-label={$i18n.t('Search')}
						>
							<div class=" self-center flex items-center justify-center size-9">
								<Search className="size-4.5" strokeWidth="2" />
							</div>
						</button>
					</Tooltip>
				</div>

				{#if ($config?.features?.enable_notes ?? false) && ($user?.role === 'admin' || ($user?.permissions?.features?.notes ?? true))}
					<div class="">
						<Tooltip content={$i18n.t('Notes')} placement="right">
							<a
								class=" cursor-pointer flex rounded-xl hover:bg-gray-100 dark:hover:bg-gray-850 transition group"
								href="/notes"
								onclick={async (e) => {
									e.stopImmediatePropagation();
									e.preventDefault();

									goto('/notes');
									itemClickHandler();
								}}
								draggable="false"
								aria-label={$i18n.t('Notes')}
							>
								<div class=" self-center flex items-center justify-center size-9">
									<Note className="size-4.5" strokeWidth="2" />
								</div>
							</a>
						</Tooltip>
					</div>
				{/if}

				{#if $config?.features?.enable_automations ?? false}
					<div class="">
						<Tooltip content={$i18n.t('Automations')} placement="right">
							<a
								class=" cursor-pointer flex rounded-xl hover:bg-gray-100 dark:hover:bg-gray-850 transition group"
								href="/automations"
								onclick={async (e) => {
									e.stopImmediatePropagation();
									e.preventDefault();

									goto('/automations');
									itemClickHandler();
								}}
								draggable="false"
								aria-label={$i18n.t('Automations')}
							>
								<div class=" self-center flex items-center justify-center size-9 relative">
									<Clock className="size-4.5" strokeWidth="2" />
									{#if $automationsUnreadCount > 0}
										<div
											class="absolute top-1.5 right-1.5 size-2 rounded-full bg-book-cloth ring-2 ring-white dark:ring-gray-950"
										></div>
									{/if}
								</div>
							</a>
						</Tooltip>
					</div>
				{/if}

				{#if $user?.role === 'admin' || $user?.permissions?.workspace?.models || $user?.permissions?.workspace?.prompts || $user?.permissions?.workspace?.tools}
					<div class="">
						<Tooltip content={$i18n.t('Workspace')} placement="right">
							<a
								class=" cursor-pointer flex rounded-xl hover:bg-gray-100 dark:hover:bg-gray-850 transition group"
								href="/workspace"
								onclick={async (e) => {
									e.stopImmediatePropagation();
									e.preventDefault();

									goto('/workspace');
									itemClickHandler();
								}}
								aria-label={$i18n.t('Workspace')}
								draggable="false"
							>
								<div class=" self-center flex items-center justify-center size-9">
									<svg
										xmlns="http://www.w3.org/2000/svg"
										fill="none"
										viewBox="0 0 24 24"
										stroke-width="2"
										stroke="currentColor"
										class="size-4.5"
									>
										<path
											stroke-linecap="round"
											stroke-linejoin="round"
											d="M13.5 16.875h3.375m0 0h3.375m-3.375 0V13.5m0 3.375v3.375M6 10.5h2.25a2.25 2.25 0 0 0 2.25-2.25V6a2.25 2.25 0 0 0-2.25-2.25H6A2.25 2.25 0 0 0 3.75 6v2.25A2.25 2.25 0 0 0 6 10.5Zm0 9.75h2.25A2.25 2.25 0 0 0 10.5 18v-2.25a2.25 2.25 0 0 0-2.25-2.25H6a2.25 2.25 0 0 0-2.25 2.25V18A2.25 2.25 0 0 0 6 20.25Zm9.75-9.75H18a2.25 2.25 0 0 0 2.25-2.25V6A2.25 2.25 0 0 0 18 3.75h-2.25A2.25 2.25 0 0 0 13.5 6v2.25a2.25 2.25 0 0 0 2.25 2.25Z"
										/>
									</svg>
								</div>
							</a>
						</Tooltip>
					</div>
				{/if}
			</div>
		</button>

		<div>
			<div>
				<div class=" py-0.5">
					{#if $user !== undefined && $user !== null}
						<UserMenu
							role={$user?.role}
							onshow={(e) => {
								if (e.detail === 'archived-chat') {
									showArchivedChats.set(true);
								}
							}}
						>
							<div
								class=" cursor-pointer flex rounded-xl hover:bg-gray-100 dark:hover:bg-gray-850 transition group"
							>
								<div class=" self-center flex items-center justify-center size-9">
									<img
										src={$user?.profile_image_url}
										class=" size-6 object-cover rounded-full"
										alt={$i18n.t('Open User Profile Menu')}
										aria-label={$i18n.t('Open User Profile Menu')}
									/>
								</div>
							</div>
						</UserMenu>
					{/if}
				</div>
			</div>
		</div>
	</div>
{/if}

{#if $showSidebar || manualDragControl || ($mobile && sidebarPrerenderReady)}
	<div
		bind:this={navElement}
		id="sidebar"
		class="h-screen max-h-[100dvh] min-h-screen select-none {$showSidebar ||
		manualDragControl ||
		$mobile
			? 'bg-gray-50 dark:bg-gray-950 z-50'
			: ' bg-transparent z-0 '} {$isApp
			? `ml-[4.5rem] md:ml-0 `
			: ' md:transition-all md:duration-300 '} shrink-0 text-gray-900 dark:text-gray-200 text-sm fixed top-0 left-0 overflow-x-hidden
        "
		style={manualDragControl
			? `transform: translateX(${dragX}px); transition: transform ${dragSettling ? DRAG_SETTLE_MS : 0}ms ease-out; will-change: transform;`
			: $mobile && !$showSidebar
				? `transform: translateX(-${SIDEBAR_DRAG_WIDTH}px); visibility: hidden;`
				: ''}
		inert={$mobile && !$showSidebar && !manualDragControl ? true : undefined}
		transition:slide={{
			duration: $mobile || manualDragControl || suppressNextOutro ? 0 : 250,
			axis: 'x'
		}}
		onoutroend={() => {
			suppressNextOutro = false;
		}}
		data-state={$showSidebar}
		use:nonpassive={['touchstart', () => onPanelTouchStart]}
		use:nonpassive={['touchmove', () => onPanelTouchMove]}
		ontouchend={onPanelTouchEnd}
		ontouchcancel={onPanelTouchCancel}
	>
		<div
			class=" my-auto flex flex-col justify-between h-screen max-h-[100dvh] w-[260px] overflow-x-hidden scrollbar-hidden z-50 {$showSidebar ||
			manualDragControl
				? ''
				: 'invisible'}"
		>
			<div
				class="sidebar px-2 pt-2 pb-1.5 flex justify-between space-x-1 text-gray-600 dark:text-gray-400 sticky top-0 z-10 -mb-3"
			>
				<a
					class="flex items-center rounded-xl size-8.5 h-full justify-center hover:bg-gray-100/50 dark:hover:bg-gray-850/50 transition no-drag-region"
					href="/"
					draggable="false"
					onclick={newChatHandler}
				>
					<img
						crossorigin="anonymous"
						src="{WEBUI_BASE_URL}/static/favicon.png"
						class="sidebar-new-chat-icon size-6 rounded-full"
						alt=""
					/>
				</a>

				<a href="/" class="flex flex-1 px-1.5 items-center gap-1.5" onclick={newChatHandler}>
					<div class=" self-center font-medium text-gray-850 dark:text-white font-primary">
						{$WEBUI_NAME}
					</div>

					{#if !$online}
						<Tooltip content={$i18n.t('Offline — showing saved data.')}>
							<span
								class="text-xs bg-red-500/15 text-red-600 dark:text-red-400 px-2 py-0.5 rounded-full font-medium"
							>
								{$i18n.t('Offline')}
							</span>
						</Tooltip>
					{/if}
				</a>
				<Tooltip
					content={$showSidebar ? $i18n.t('Close Sidebar') : $i18n.t('Open Sidebar')}
					placement="bottom"
				>
					<button
						class="flex rounded-xl size-8.5 justify-center items-center hover:bg-gray-100/50 dark:hover:bg-gray-850/50 transition {isWindows
							? 'cursor-pointer'
							: 'cursor-[w-resize]'}"
						onclick={() => {
							showSidebar.set(!$showSidebar);
						}}
						aria-label={$showSidebar ? $i18n.t('Close Sidebar') : $i18n.t('Open Sidebar')}
					>
						<div class=" self-center p-1.5">
							<Sidebar />
						</div>
					</button>
				</Tooltip>

				<div
					class="{scrollTop > 0
						? 'visible'
						: 'invisible'} sidebar-bg-gradient-to-b bg-linear-to-b from-gray-50 dark:from-gray-950 to-transparent from-50% pointer-events-none absolute inset-0 -z-10 -mb-6"
				></div>
			</div>

			<div
				id="sidebar-scroll-container"
				class="relative flex flex-col flex-1 overflow-y-auto scrollbar-hidden pt-3 pb-3"
				onscroll={(e) => {
					if (e.target.scrollTop === 0) {
						scrollTop = 0;
					} else {
						scrollTop = e.target.scrollTop;
					}
				}}
			>
				<div class="pb-1.5">
					<div class="px-[7px] flex justify-center text-gray-800 dark:text-gray-200">
						<a
							id="sidebar-new-chat-button"
							class="grow flex items-center space-x-3 rounded-xl px-2.5 py-2 hover:bg-manilla/20 dark:hover:bg-manilla-dark/50 transition outline-none"
							href="/"
							draggable="false"
							onclick={newChatHandler}
							aria-label={$i18n.t('New Chat')}
						>
							<div class="self-center">
								<PencilSquare className=" size-4.5" strokeWidth="2" />
							</div>

							<div class="self-center text-sm font-primary">{$i18n.t('New Chat')}</div>
						</a>
					</div>

					<div class="px-[7px] flex justify-center text-gray-800 dark:text-gray-200">
						<button
							id="sidebar-search-button"
							class="grow flex items-center space-x-3 rounded-xl px-2.5 py-2 hover:bg-manilla/20 dark:hover:bg-manilla-dark/50 transition outline-none"
							onclick={() => {
								showSearch.set(true);
							}}
							draggable="false"
							aria-label={$i18n.t('Search')}
						>
							<div class="self-center">
								<Search strokeWidth="2" className="size-4.5" />
							</div>

							<div class="self-center text-sm font-primary">{$i18n.t('Search')}</div>
						</button>
					</div>

					{#if ($config?.features?.enable_notes ?? false) && ($user?.role === 'admin' || ($user?.permissions?.features?.notes ?? true))}
						<div class="px-[7px] flex justify-center text-gray-800 dark:text-gray-200">
							<a
								id="sidebar-notes-button"
								class="grow flex items-center space-x-3 rounded-xl px-2.5 py-2 hover:bg-manilla/20 dark:hover:bg-manilla-dark/50 transition"
								href="/notes"
								onclick={itemClickHandler}
								draggable="false"
								aria-label={$i18n.t('Notes')}
							>
								<div class="self-center">
									<Note className="size-4.5" strokeWidth="2" />
								</div>

								<div class="self-center text-sm font-primary">{$i18n.t('Notes')}</div>
							</a>
						</div>
					{/if}

					{#if $config?.features?.enable_automations ?? false}
						<div class="px-[7px] flex justify-center text-gray-800 dark:text-gray-200">
							<a
								id="sidebar-automations-button"
								class="grow flex items-center space-x-3 rounded-xl px-2.5 py-2 hover:bg-manilla/20 dark:hover:bg-manilla-dark/50 transition"
								href="/automations"
								onclick={itemClickHandler}
								draggable="false"
								aria-label={$i18n.t('Automations')}
							>
								<div class="self-center">
									<Clock className="size-4.5" strokeWidth="2" />
								</div>

								<div class="self-center text-sm font-primary">{$i18n.t('Automations')}</div>

								{#if $automationsUnreadCount > 0}
									<div class="ml-auto size-2 rounded-full bg-book-cloth"></div>
								{/if}
							</a>
						</div>
					{/if}

					{#if $user?.role === 'admin' || $user?.permissions?.workspace?.models || $user?.permissions?.workspace?.prompts || $user?.permissions?.workspace?.tools}
						<div class="px-[7px] flex justify-center text-gray-800 dark:text-gray-200">
							<a
								id="sidebar-workspace-button"
								class="grow flex items-center space-x-3 rounded-xl px-2.5 py-2 hover:bg-manilla/20 dark:hover:bg-manilla-dark/50 transition"
								href="/workspace"
								onclick={itemClickHandler}
								draggable="false"
								aria-label={$i18n.t('Workspace')}
							>
								<div class="self-center">
									<svg
										xmlns="http://www.w3.org/2000/svg"
										fill="none"
										viewBox="0 0 24 24"
										stroke-width="2"
										stroke="currentColor"
										class="size-4.5"
									>
										<path
											stroke-linecap="round"
											stroke-linejoin="round"
											d="M13.5 16.875h3.375m0 0h3.375m-3.375 0V13.5m0 3.375v3.375M6 10.5h2.25a2.25 2.25 0 0 0 2.25-2.25V6a2.25 2.25 0 0 0-2.25-2.25H6A2.25 2.25 0 0 0 3.75 6v2.25A2.25 2.25 0 0 0 6 10.5Zm0 9.75h2.25A2.25 2.25 0 0 0 10.5 18v-2.25a2.25 2.25 0 0 0-2.25-2.25H6a2.25 2.25 0 0 0-2.25 2.25V18A2.25 2.25 0 0 0 6 20.25Zm9.75-9.75H18a2.25 2.25 0 0 0 2.25-2.25V6A2.25 2.25 0 0 0 18 3.75h-2.25A2.25 2.25 0 0 0 13.5 6v2.25a2.25 2.25 0 0 0 2.25 2.25Z"
										/>
									</svg>
								</div>

								<div class="self-center text-sm font-primary">{$i18n.t('Workspace')}</div>
							</a>
						</div>
					{/if}
				</div>

				{#if ($models ?? []).length > 0 && ($settings?.pinnedModels ?? []).length > 0}
					<PinnedModelList bind:selectedChatId bind:pendingChatId {shiftKey} />
				{/if}

				{#if $config?.features?.enable_channels && ($user?.role === 'admin' || $channels.length > 0)}
					<Folder
						className="px-2 mt-0.5"
						name={$i18n.t('Channels')}
						chevron={false}
						dragAndDrop={false}
						onAdd={async () => {
							if ($user?.role === 'admin') {
								await tick();

								setTimeout(() => {
									showCreateChannel = true;
								}, 0);
							}
						}}
						onAddLabel={$i18n.t('Create Channel')}
					>
						{#each $channels as channel}
							<ChannelItem
								{channel}
								onUpdate={async () => {
									await initChannels();
								}}
							/>
						{/each}
					</Folder>
				{/if}

				{#if folders}
					<Folder
						className="px-2 mt-0.5"
						name={$i18n.t('Folders')}
						chevron={false}
						onAdd={() => {
							showCreateFolderModal = true;
						}}
						onAddLabel={$i18n.t('New Folder')}
						ondrop={async (e) => {
							const { type, id, item } = e.detail;

							if (type === 'folder') {
								if (folders[id].parent_id === null) {
									return;
								}

								const res = await updateFolderParentIdById(localStorage.token, id, null).catch(
									(error) => {
										toast.error(`${error}`);
										return null;
									}
								);

								if (res) {
									await initFolders();
								}
							}
						}}
					>
						<Folders
							bind:folderRegistry
							{folders}
							{shiftKey}
							{activeChatId}
							onDelete={(folderId) => {
								selectedFolder.set(null);
								initChatList();
							}}
							onactivate={(e) => {
								pendingChatId = e.detail;
								selectedChatId = null;
							}}
							onupdate={() => {
								initChatList();
							}}
							onimport={(e) => {
								const { folderId, items } = e.detail;
								importChatHandler(items, false, folderId);
							}}
							onchange={async () => {
								initChatList();
							}}
						/>
					</Folder>
				{/if}

				<Folder
					className="px-2 mt-0.5"
					name={$i18n.t('Chats')}
					chevron={false}
					onchange={async (e) => {
						selectedFolder.set(null);
					}}
					onimport={(e) => {
						importChatHandler(e.detail);
					}}
					ondrop={async (e) => {
						const { type, id, item } = e.detail;

						if (type === 'chat') {
							let chat = await getChatById(localStorage.token, id).catch((error) => {
								return null;
							});
							if (!chat && item) {
								chat = await importChat(
									localStorage.token,
									item.chat,
									item?.meta ?? {},
									false,
									null,
									item?.created_at ?? null,
									item?.updated_at ?? null
								);
							}

							if (chat) {
								console.log(chat);
								if (chat.folder_id) {
									const res = await updateChatFolderIdById(localStorage.token, chat.id, null).catch(
										(error) => {
											toast.error(`${error}`);
											return null;
										}
									);

									folderRegistry[chat.folder_id]?.setFolderItems();
								}

								if (chat.pinned) {
									const res = await toggleChatPinnedStatusById(localStorage.token, chat.id);
								}

								initChatList();
							}
						} else if (type === 'folder') {
							if (folders[id].parent_id === null) {
								return;
							}

							const res = await updateFolderParentIdById(localStorage.token, id, null).catch(
								(error) => {
									toast.error(`${error}`);
									return null;
								}
							);

							if (res) {
								await initFolders();
							}
						}
					}}
				>
					{#if $pinnedChats.length > 0}
						<div class="mb-1">
							<div class="flex flex-col space-y-1 rounded-xl">
								<Folder
									buttonClassName=" text-gray-500"
									bind:open={showPinnedChat}
									onchange={(e) => {
										localStorage.setItem('showPinnedChat', e.detail);
										console.log(e.detail);
									}}
									onimport={(e) => {
										importChatHandler(e.detail, true);
									}}
									ondrop={async (e) => {
										const { type, id, item } = e.detail;

										if (type === 'chat') {
											let chat = await getChatById(localStorage.token, id).catch((error) => {
												return null;
											});
											if (!chat && item) {
												chat = await importChat(
													localStorage.token,
													item.chat,
													item?.meta ?? {},
													false,
													null,
													item?.created_at ?? null,
													item?.updated_at ?? null
												);
											}

											if (chat) {
												console.log(chat);
												if (chat.folder_id) {
													const res = await updateChatFolderIdById(
														localStorage.token,
														chat.id,
														null
													).catch((error) => {
														toast.error(`${error}`);
														return null;
													});
												}

												if (!chat.pinned) {
													const res = await toggleChatPinnedStatusById(localStorage.token, chat.id);
												}

												initChatList();
											}
										}
									}}
									name={$i18n.t('Pinned')}
								>
									<div
										class="ml-3 pl-1 flex flex-col overflow-y-auto scrollbar-hidden border-s border-gray-100 dark:border-gray-900 text-gray-900 dark:text-gray-200"
									>
										{#each $pinnedChats as chat, idx (`pinned-chat-${chat?.id ?? idx}`)}
											<ChatItem
												className=""
												id={chat.id}
												title={chat.title}
												{shiftKey}
												active={activeChatId === chat.id}
												selected={selectedChatId === chat.id}
												onactivate={(e) => {
													pendingChatId = e.detail;
													selectedChatId = null;
												}}
												onselect={() => {
													selectedChatId = chat.id;
												}}
												onunselect={() => {
													selectedChatId = null;
												}}
												onchange={async () => {
													initChatList();
												}}
												ontag={(e) => {
													const { type, name } = e.detail;
													tagEventHandler(type, name, chat.id);
												}}
											/>
										{/each}
									</div>
								</Folder>
							</div>
						</div>
					{/if}

					<div class=" flex-1 flex flex-col overflow-y-auto scrollbar-hidden">
						<div class="pt-1.5">
							{#if $chats}
								{#each $chats as chat, idx (`chat-${chat?.id ?? idx}`)}
									{#if idx === 0 || (idx > 0 && chat.time_range !== $chats[idx - 1].time_range)}
										<div
											class="w-full pl-2.5 text-xs text-gray-500 dark:text-gray-500 font-medium {idx ===
											0
												? ''
												: 'pt-5'} pb-1.5"
										>
											{$i18n.t(chat.time_range)}
											<!-- localisation keys for time_range to be recognized from the i18next parser (so they don't get automatically removed):
							{$i18n.t('Today')}
							{$i18n.t('Yesterday')}
							{$i18n.t('Previous 7 days')}
							{$i18n.t('Previous 30 days')}
							{$i18n.t('January')}
							{$i18n.t('February')}
							{$i18n.t('March')}
							{$i18n.t('April')}
							{$i18n.t('May')}
							{$i18n.t('June')}
							{$i18n.t('July')}
							{$i18n.t('August')}
							{$i18n.t('September')}
							{$i18n.t('October')}
							{$i18n.t('November')}
							{$i18n.t('December')}
							-->
										</div>
									{/if}

									<ChatItem
										className=""
										id={chat.id}
										title={chat.title}
										{shiftKey}
										active={activeChatId === chat.id}
										selected={selectedChatId === chat.id}
										onactivate={(e) => {
											pendingChatId = e.detail;
											selectedChatId = null;
										}}
										onselect={() => {
											selectedChatId = chat.id;
										}}
										onunselect={() => {
											selectedChatId = null;
										}}
										onchange={async () => {
											initChatList();
										}}
										ontag={(e) => {
											const { type, name } = e.detail;
											tagEventHandler(type, name, chat.id);
										}}
									/>
								{/each}

								{#if $scrollPaginationEnabled && !allChatsLoaded}
									<Loader
										onvisible={(e) => {
											if (!chatListLoading) {
												loadMoreChats();
											}
										}}
									>
										<div
											class="w-full flex justify-center py-1 text-xs animate-pulse items-center gap-2"
										>
											<Spinner className=" size-4" />
											<div class=" ">{$i18n.t('Loading...')}</div>
										</div>
									</Loader>
								{/if}
							{:else}
								<div
									class="w-full flex justify-center py-1 text-xs animate-pulse items-center gap-2"
								>
									<Spinner className=" size-4" />
									<div class=" ">{$i18n.t('Loading...')}</div>
								</div>
							{/if}
						</div>
					</div>
				</Folder>
			</div>

			<div class="px-1.5 pt-1.5 pb-2 pb-safe sticky bottom-0 z-10 -mt-3 sidebar">
				<div
					class=" sidebar-bg-gradient-to-t bg-linear-to-t from-gray-50 dark:from-gray-950 to-transparent from-50% pointer-events-none absolute inset-0 -z-10 -mt-6"
				></div>
				<div class="flex flex-col font-primary">
					{#if $user !== undefined && $user !== null}
						<UserMenu
							role={$user?.role}
							onshow={(e) => {
								if (e.detail === 'archived-chat') {
									showArchivedChats.set(true);
								}
							}}
						>
							<div
								class=" flex items-center rounded-2xl py-2 px-1.5 w-full hover:bg-gray-100/50 dark:hover:bg-gray-850/50 transition"
							>
								<div class=" self-center mr-3">
									<img
										src={$user?.profile_image_url}
										class=" size-6 object-cover rounded-full"
										alt={$i18n.t('Open User Profile Menu')}
										aria-label={$i18n.t('Open User Profile Menu')}
									/>
								</div>
								<div class=" self-center font-medium">{$user?.name}</div>
							</div>
						</UserMenu>
					{/if}
				</div>
			</div>
		</div>
	</div>
{/if}
