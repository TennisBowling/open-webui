<script lang="ts">
	import { WEBUI_BASE_URL } from '$lib/constants';
	import { imageFallback } from '$lib/actions/imageFallback';

	interface Props {
		className?: string;
		src?: any;
	}

	let { className = 'size-8', src = `${WEBUI_BASE_URL}/static/favicon.png` }: Props = $props();
</script>

<img
	use:imageFallback={`${WEBUI_BASE_URL}/static/favicon.png`}
	aria-hidden="true"
	src={src === ''
		? `${WEBUI_BASE_URL}/static/favicon.png`
		: src.startsWith(WEBUI_BASE_URL) ||
			  src.startsWith('https://www.gravatar.com/avatar/') ||
			  src.startsWith('data:') ||
			  src.startsWith('/')
			? src
			: `${WEBUI_BASE_URL}/user.png`}
	class=" {className} object-cover rounded-full"
	alt="profile"
	draggable="false"
	loading="lazy"
	decoding="async"
/>
