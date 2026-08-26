import { WEBUI_API_BASE_URL } from '$lib/constants';

export const subscribeToPush = async (token: string, subscription: PushSubscriptionJSON) => {
	let error = null;

	const res = await fetch(`${WEBUI_API_BASE_URL}/push/subscribe`, {
		method: 'POST',
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			authorization: `Bearer ${token}`
		},
		body: JSON.stringify({
			endpoint: subscription.endpoint,
			keys: subscription.keys
		})
	})
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json();
		})
		.catch((err) => {
			error = err.detail;
			console.error(err);
			return null;
		});

	if (error) {
		throw error;
	}

	return res;
};

export const unsubscribeFromPush = async (token: string, endpoint: string) => {
	let error = null;

	const res = await fetch(`${WEBUI_API_BASE_URL}/push/unsubscribe`, {
		method: 'POST',
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			authorization: `Bearer ${token}`
		},
		body: JSON.stringify({
			endpoint
		})
	})
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json();
		})
		.catch((err) => {
			error = err.detail;
			console.error(err);
			return null;
		});

	if (error) {
		throw error;
	}

	return res;
};
