<script lang="ts">
	import { getContext } from 'svelte';
	const i18n = getContext('i18n');

	import Modal from '$lib/components/common/Modal.svelte';
	import AccessControl from './AccessControl.svelte';
	import XMark from '$lib/components/icons/XMark.svelte';

	interface Props {
		show?: boolean;
		accessControl?: any;
		accessRoles?: any;
		allowPublic?: boolean;
		onChange?: any;
	}

	let {
		show = $bindable(false),
		accessControl = $bindable({}),
		accessRoles = ['read'],
		allowPublic = true,
		onChange = () => {}
	}: Props = $props();
</script>

<Modal size="sm" bind:show>
	<div>
		<div class=" flex justify-between dark:text-gray-100 px-5 pt-3 pb-1">
			<div class=" text-lg font-medium self-center font-primary">
				{$i18n.t('Access Control')}
			</div>
			<button
				class="self-center p-0.5 rounded-lg text-gray-500 hover:text-gray-700 dark:text-gray-400 dark:hover:text-gray-200 hover:bg-gray-100 dark:hover:bg-gray-800 transition"
				onclick={() => {
					show = false;
				}}
			>
				<XMark className={'size-5'} />
			</button>
		</div>

		<div class="w-full px-5 pb-4 dark:text-white">
			<AccessControl bind:accessControl {onChange} {accessRoles} {allowPublic} />
		</div>
	</div>
</Modal>
