import { AUDIO_API_BASE_URL } from '$lib/constants';

export const getAudioConfig = async (token: string) => {
	let error = null;

	const res = await fetch(`${AUDIO_API_BASE_URL}/config`, {
		method: 'GET',
		headers: {
			'Content-Type': 'application/json',
			Authorization: `Bearer ${token}`
		}
	})
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json();
		})
		.catch((err) => {
			console.error(err);
			error = err.detail;
			return null;
		});

	if (error) {
		throw error;
	}

	return res;
};

type AudioConfigForm = {
	tts: {
		OPENAI_API_BASE_URL: string;
		OPENAI_API_KEY: string;
		OPENAI_PARAMS?: Record<string, unknown>;
		OPENROUTER_API_KEY: string;
		API_KEY: string;
		ENGINE: string;
		MODEL: string;
		VOICE: string;
		SPLIT_ON: string;
		AZURE_SPEECH_REGION: string;
		AZURE_SPEECH_BASE_URL: string;
		AZURE_SPEECH_OUTPUT_FORMAT: string;
	};
	stt: {
		OPENAI_API_BASE_URL: string;
		OPENAI_API_KEY: string;
		OPENROUTER_API_KEY: string;
		OPENROUTER_TEMPERATURE: number | null;
		ENGINE: string;
		MODEL: string;
		SUPPORTED_CONTENT_TYPES: string[];
		WHISPER_MODEL: string;
		DEEPGRAM_API_KEY: string;
		AZURE_API_KEY: string;
		AZURE_REGION: string;
		AZURE_LOCALES: string;
		AZURE_BASE_URL: string;
		AZURE_MAX_SPEAKERS: string;
	};
};

export const updateAudioConfig = async (token: string, payload: AudioConfigForm) => {
	let error = null;

	const res = await fetch(`${AUDIO_API_BASE_URL}/config/update`, {
		method: 'POST',
		headers: {
			'Content-Type': 'application/json',
			Authorization: `Bearer ${token}`
		},
		body: JSON.stringify({
			...payload
		})
	})
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json();
		})
		.catch((err) => {
			console.error(err);
			error = err.detail;
			return null;
		});

	if (error) {
		throw error;
	}

	return res;
};

export const previewOpenRouterSpeech = async (
	token: string,
	payload: {
		API_KEY: string;
		MODEL: string;
		VOICE: string;
		INPUT: string;
	}
) => {
	let error = null;

	const res = await fetch(`${AUDIO_API_BASE_URL}/openrouter/preview`, {
		method: 'POST',
		headers: {
			'Content-Type': 'application/json',
			Authorization: `Bearer ${token}`
		},
		body: JSON.stringify(payload)
	})
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res;
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

export const transcribeAudio = async (token: string, file: File, language?: string) => {
	const data = new FormData();
	data.append('file', file);
	if (language) {
		data.append('language', language);
	}

	let error = null;
	const res = await fetch(`${AUDIO_API_BASE_URL}/transcriptions`, {
		method: 'POST',
		headers: {
			Accept: 'application/json',
			authorization: `Bearer ${token}`
		},
		body: data
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

export const synthesizeOpenAISpeech = async (
	token: string = '',
	speaker: string = 'alloy',
	text: string = '',
	model?: string
) => {
	let error = null;

	const res = await fetch(`${AUDIO_API_BASE_URL}/speech`, {
		method: 'POST',
		headers: {
			Authorization: `Bearer ${token}`,
			'Content-Type': 'application/json'
		},
		body: JSON.stringify({
			input: text,
			voice: speaker,
			...(model && { model })
		})
	})
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res;
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

export type AvailableTTSModel = {
	id: string;
	name?: string;
	description?: string;
	context_length?: number;
	pricing?: {
		prompt?: string;
		completion?: string;
	};
	voices?: string[];
	supported_parameters?: string[];
};

interface AvailableModelsResponse {
	models: AvailableTTSModel[];
}

export const getModels = async (
	token: string = '',
	engine?: string,
	refresh: boolean = false
): Promise<AvailableModelsResponse> => {
	let error = null;
	const query = new URLSearchParams();
	if (engine) query.set('engine', engine);
	if (refresh) query.set('refresh', 'true');
	const queryString = query.size ? `?${query.toString()}` : '';

	const res = await fetch(`${AUDIO_API_BASE_URL}/models${queryString}`, {
		method: 'GET',
		headers: {
			'Content-Type': 'application/json',
			Authorization: `Bearer ${token}`
		}
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

export const getVoices = async (token: string = '', engine?: string, model?: string) => {
	let error = null;
	const query = new URLSearchParams();
	if (engine) query.set('engine', engine);
	if (model) query.set('model', model);
	const queryString = query.size ? `?${query.toString()}` : '';

	const res = await fetch(`${AUDIO_API_BASE_URL}/voices${queryString}`, {
		method: 'GET',
		headers: {
			'Content-Type': 'application/json',
			Authorization: `Bearer ${token}`
		}
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
