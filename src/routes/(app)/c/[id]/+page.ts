import { browser } from '$app/environment';
import { getChatByIdTail } from '$lib/apis/chats';
import { getChat as getOfflineChat } from '$lib/offline/chatStore';

// sidebarSync's currentUserId is module-private — same one-liner inlined.
const currentUserId = (): string | null => {
	try {
		return JSON.parse(localStorage.getItem('sessionUser') ?? 'null')?.id ?? null;
	} catch {
		return null;
	}
};

export const load = ({ params }) => {
	if (browser && localStorage.token) {
		const currentChatId = params.id;

		// Local-first open: the IDB copy is read ONCE (~1-5ms) and exposed on its
		// own promise so Chat.svelte can PAINT from it immediately, while the
		// network request (sharing the same read for its If-None-Match) revalidates
		// in the background. Unchanged chat → 304 (~300 bytes) and getChatByIdTail
		// substitutes the stored copy; changed chat → manifest delta or full tail.
		const localEntryPromise = (async () => {
			if (currentChatId.startsWith('local:')) return null;
			const userId = currentUserId();
			return userId ? await getOfflineChat(userId, currentChatId).catch(() => null) : null;
		})();

		return {
			chatId: currentChatId,
			localEntryPromise,
			// Task ids + active streams ride INSIDE the open response now
			// (meta.active, or proven-absent by a 304) — no separate request.
			chatPromise: (async () => {
				const etagEntry = await localEntryPromise;
				return getChatByIdTail(localStorage.token, currentChatId, 25, { etagEntry });
			})().catch(() => null)
		};
	}
	return {};
};
