<script lang="ts">
	import { createEventDispatcher, getContext, onMount } from 'svelte';
	import { toast } from 'svelte-sonner';

	import Switch from '$lib/components/common/Switch.svelte';
	import Spinner from '$lib/components/common/Spinner.svelte';
	import { getContainerConfig, setContainerConfig } from '$lib/apis/configs';

	const i18n = getContext('i18n');
	const dispatch = createEventDispatcher();

	let loading = true;
	let saving = false;
	let containerConfig = {
		ENABLE_CONTAINER_WORKSPACE_SYNC: false,
		CONTAINER_DATA_ROOT: '/mnt/microns/openllm-containers',
		CONTAINER_MCP_SERVER_ID: '',
		CONTAINER_SYSTEM_PROMPT:
			'Container workspace is enabled for this chat.\n' +
			'Use /workspace/inputs for uploaded files and /workspace/outputs for files the user should receive.\n' +
			'Files in /workspace/outputs persist across turns; when the user asks to modify a previous output, edit the existing file there instead of creating an unrelated copy.\n' +
			'When you create files for the user, save them under /workspace/outputs. Open WebUI will attach generated files automatically. If you mention a generated file, refer to its filename or use a sandbox:/workspace/... link only for files that actually exist.'
	};

	const saveHandler = async () => {
		saving = true;
		const res = await setContainerConfig(localStorage.token, containerConfig).catch((error) => {
			toast.error(error?.detail ?? error ?? $i18n.t('Failed to save container settings'));
			return null;
		});

		if (res) {
			containerConfig = res;
			dispatch('save');
		}
		saving = false;
	};

	onMount(async () => {
		const res = await getContainerConfig(localStorage.token).catch((error) => {
			toast.error(error?.detail ?? error ?? $i18n.t('Failed to load container settings'));
			return null;
		});
		if (res) {
			containerConfig = res;
		}
		loading = false;
	});
</script>

<form class="flex flex-col h-full justify-between text-sm" on:submit|preventDefault={saveHandler}>
	<div class="overflow-y-scroll scrollbar-hidden h-full">
		{#if !loading}
			<div class="mb-3">
				<div class="mb-2.5 text-base font-medium">{$i18n.t('Container')}</div>
				<hr class="border-gray-100 dark:border-gray-850 my-2" />

				<div class="mb-3 flex w-full justify-between items-center gap-4">
					<div>
						<div class="text-sm font-medium">{$i18n.t('Workspace Sync')}</div>
						<div class="text-xs text-gray-500 mt-0.5">
							{$i18n.t('Only syncs files when the selected MCP external tool server is active for the message.')}
						</div>
					</div>
					<Switch bind:state={containerConfig.ENABLE_CONTAINER_WORKSPACE_SYNC} />
				</div>

				<div class="mb-3">
					<label class="text-xs text-gray-500" for="container-data-root">
						{$i18n.t('Container Data Root')}
					</label>
					<input
						id="container-data-root"
						class="w-full mt-1 text-sm bg-transparent outline-hidden border border-gray-100 dark:border-gray-800 rounded-lg px-3 py-2"
						type="text"
						bind:value={containerConfig.CONTAINER_DATA_ROOT}
						placeholder="/mnt/microns/openllm-containers"
						autocomplete="off"
					/>
					<div class="text-xs text-gray-500 mt-1">
						{$i18n.t('Must match CAM_DATA_ROOT used by container-agent-middleware.')}
					</div>
				</div>

				<div class="mb-3">
					<label class="text-xs text-gray-500" for="container-mcp-server-id">
						{$i18n.t('Container MCP Server ID')}
					</label>
					<input
						id="container-mcp-server-id"
						class="w-full mt-1 text-sm bg-transparent outline-hidden border border-gray-100 dark:border-gray-800 rounded-lg px-3 py-2"
						type="text"
						bind:value={containerConfig.CONTAINER_MCP_SERVER_ID}
						placeholder="container-agent"
						autocomplete="off"
					/>
					<div class="text-xs text-gray-500 mt-1">
						{$i18n.t('This is the ID from Admin Settings → External Tools. That MCP connection should send')}
						<code class="px-1">X-Chat-Id: {'{{'}CHAT_ID{'}}'}</code>.
					</div>
				</div>

				<div class="mb-3">
					<label class="text-xs text-gray-500" for="container-system-prompt">
						{$i18n.t('Append to System Prompt')}
					</label>
					<textarea
						id="container-system-prompt"
						class="w-full mt-1 text-sm bg-transparent outline-hidden border border-gray-100 dark:border-gray-800 rounded-lg px-3 py-2 min-h-36 resize-y"
						bind:value={containerConfig.CONTAINER_SYSTEM_PROMPT}
						placeholder={$i18n.t('Instructions appended only when the container MCP server is active')}
					/>
					<div class="text-xs text-gray-500 mt-1">
						{$i18n.t('This text is appended to the system prompt only for messages using the configured container MCP server. Open WebUI also appends the current input/output file list automatically.')}
					</div>
				</div>
			</div>
		{:else}
			<div class="flex h-full justify-center">
				<div class="my-auto"><Spinner className="size-6" /></div>
			</div>
		{/if}
	</div>

	<div class="flex justify-end pt-3 text-sm font-medium">
		<button
			class="px-3.5 py-1.5 text-sm font-medium bg-book-cloth hover:bg-kraft text-white dark:bg-book-cloth dark:text-white dark:hover:bg-kraft transition-colors duration-200 ease-paper rounded-full flex items-center gap-2 {saving ? 'cursor-not-allowed opacity-80' : ''}"
			type="submit"
			disabled={loading || saving}
		>
			{$i18n.t('Save')}
			{#if saving}<Spinner />{/if}
		</button>
	</div>
</form>
