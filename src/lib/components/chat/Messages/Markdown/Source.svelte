<script lang="ts">
	interface Props {
		id: any;
		token: any;
		onClick?: Function;
	}

	let { id, token, onClick = () => {} }: Props = $props();

	let attributes: Record<string, string | undefined> = $state({});

	function extractAttributes(input: string): Record<string, string> {
		const regex = /(\w+)="([^"]*)"/g;
		let match;
		let attrs: Record<string, string> = {};

		// Loop through all matches and populate the attributes object
		while ((match = regex.exec(input)) !== null) {
			attrs[match[1]] = match[2];
		}

		return attrs;
	}

	// Helper function to return only the domain from a URL
	function getDomain(url: string): string {
		const domain = url.replace('http://', '').replace('https://', '').split(/[/?#]/)[0];

		if (domain.startsWith('www.')) {
			return domain.slice(4);
		}
		return domain;
	}

	// Helper function to check if text is a URL and return the domain
	function formattedTitle(title: string): string {
		if (title.startsWith('http')) {
			return getDomain(title);
		}

		return title;
	}

	const getDisplayTitle = (title: string) => {
		if (!title) return 'N/A';
		if (title.length > 30) {
			return title.slice(0, 15) + '...' + title.slice(-10);
		}
		return title;
	};

	$effect(() => {
		attributes = extractAttributes(token.text);
	});
</script>

{#if attributes.title !== 'N/A'}
	<button
		class="text-xs font-medium w-fit translate-y-[2px] px-2 py-0.5 dark:bg-white/5 text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200 bg-gray-50 transition rounded-lg"
		onclick={() => {
			onClick(id, attributes.data);
		}}
	>
		<span class="line-clamp-1">
			{getDisplayTitle(
				decodeURIComponent(attributes.title)
					? formattedTitle(decodeURIComponent(attributes.title))
					: ''
			)}
		</span>
	</button>
{/if}
