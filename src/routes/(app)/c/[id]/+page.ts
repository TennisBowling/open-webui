import { browser } from '$app/environment';
import { getChatByIdTail } from '$lib/apis/chats';
import { getTaskIdsByChatId } from '$lib/apis';

export const load = ({ params }) => {
	if (browser && localStorage.token) {
		const currentChatId = params.id;

		return {
			chatId: currentChatId,
			chatPromise: getChatByIdTail(localStorage.token, currentChatId).catch(() => null),
			taskResPromise: getTaskIdsByChatId(localStorage.token, currentChatId).catch(() => null)
		};
	}
	return {};
};
