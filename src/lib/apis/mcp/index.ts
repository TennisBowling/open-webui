import { WEBUI_API_BASE_URL } from '$lib/constants';

const authHeaders = (token: string) => ({
	'Content-Type': 'application/json',
	Authorization: `Bearer ${token}`
});

const handle = async (res: Response) => {
	if (!res.ok) throw await res.json().catch(() => ({ detail: res.statusText }));
	return res.json();
};

export const getMCPConnections = async (token: string) =>
	fetch(`${WEBUI_API_BASE_URL}/mcp/connections`, {
		method: 'GET',
		headers: authHeaders(token)
	}).then(handle);

export const createMCPConnection = async (token: string, connection: object) =>
	fetch(`${WEBUI_API_BASE_URL}/mcp/connections`, {
		method: 'POST',
		headers: authHeaders(token),
		body: JSON.stringify(connection)
	}).then(handle);

export const updateMCPConnection = async (token: string, id: string, connection: object) =>
	fetch(`${WEBUI_API_BASE_URL}/mcp/connections/${id}`, {
		method: 'PATCH',
		headers: authHeaders(token),
		body: JSON.stringify(connection)
	}).then(handle);

export const deleteMCPConnection = async (token: string, id: string) =>
	fetch(`${WEBUI_API_BASE_URL}/mcp/connections/${id}`, {
		method: 'DELETE',
		headers: authHeaders(token)
	}).then(handle);

export const verifyMCPConnection = async (token: string, id: string) =>
	fetch(`${WEBUI_API_BASE_URL}/mcp/connections/${id}/verify`, {
		method: 'POST',
		headers: authHeaders(token)
	}).then(handle);

export const startMCPConnectionOAuth = async (token: string, id: string) =>
	fetch(`${WEBUI_API_BASE_URL}/mcp/connections/${id}/oauth/start`, {
		method: 'POST',
		headers: authHeaders(token)
	}).then(handle);

export const disconnectMCPConnectionOAuth = async (token: string, id: string) =>
	fetch(`${WEBUI_API_BASE_URL}/mcp/connections/${id}/oauth/disconnect`, {
		method: 'POST',
		headers: authHeaders(token)
	}).then(handle);

export const getMCPConnectionTemplates = async (token: string) =>
	fetch(`${WEBUI_API_BASE_URL}/mcp/templates`, {
		method: 'GET',
		headers: authHeaders(token)
	}).then(handle);

export const discoverMCP = async (token: string, url: string) =>
	fetch(`${WEBUI_API_BASE_URL}/mcp/discover`, {
		method: 'POST',
		headers: authHeaders(token),
		body: JSON.stringify({ url })
	}).then(handle);
