<script lang="ts" module>
	import { marked, type Token } from 'marked';

	type AlertType = 'NOTE' | 'TIP' | 'IMPORTANT' | 'WARNING' | 'CAUTION';

	interface AlertTheme {
		border: string;
		text: string;
		icon: ComponentType;
	}

	export interface AlertData {
		type: AlertType;
		text: string;
		tokens: Token[];
	}

	// Warm-palette alert accents (see src/lib/utils/statusColors.ts philosophy):
	// five distinguishable on-brand hues instead of default-Tailwind neon.
	const alertStyles: Record<AlertType, AlertTheme> = {
		NOTE: {
			border: 'border-gray-400 dark:border-gray-500',
			text: 'text-gray-500 dark:text-gray-400',
			icon: Info
		},
		TIP: {
			border: 'border-success dark:border-success-dark',
			text: 'text-success dark:text-success-dark',
			icon: LightBulb
		},
		IMPORTANT: {
			border: 'border-book-cloth',
			text: 'text-book-cloth dark:text-kraft',
			icon: Star
		},
		WARNING: {
			border: 'border-warning dark:border-warning-dark',
			text: 'text-warning dark:text-warning-dark',
			icon: ArrowRightCircle
		},
		CAUTION: {
			border: 'border-error-brick dark:border-error-brick-dark',
			text: 'text-error-brick dark:text-error-brick-dark',
			icon: Bolt
		}
	};

	export function alertComponent(token: Token): AlertData | false {
		const regExpStr = `^(?:\\[!(NOTE|TIP|IMPORTANT|WARNING|CAUTION)\\])\\s*?\n*`;
		const regExp = new RegExp(regExpStr);
		const matches = token.text?.match(regExp);

		if (matches && matches.length) {
			const alertType = matches[1] as AlertType;
			const newText = token.text.replace(regExp, '');
			const newTokens = marked.lexer(newText);
			return {
				type: alertType,
				text: newText,
				tokens: newTokens
			};
		}
		return false;
	}
</script>

<script lang="ts">
	import Info from '$lib/components/icons/Info.svelte';
	import Star from '$lib/components/icons/Star.svelte';
	import LightBulb from '$lib/components/icons/LightBulb.svelte';
	import Bolt from '$lib/components/icons/Bolt.svelte';
	import ArrowRightCircle from '$lib/components/icons/ArrowRightCircle.svelte';
	import MarkdownTokens from './MarkdownTokens.svelte';
	import type { ComponentType } from 'svelte';

	interface Props {
		token: Token;
		alert: AlertData;
		id?: string;
		tokenIdx?: number;
		sandboxFiles?: any[];
		onTaskClick?: ((event: MouseEvent) => void) | undefined;
		onSourceClick?: ((event: MouseEvent) => void) | undefined;
	}

	let {
		token,
		alert,
		id = '',
		tokenIdx = 0,
		sandboxFiles = [],
		onTaskClick = undefined,
		onSourceClick = undefined
	}: Props = $props();

	const SvelteComponent = $derived(alertStyles[alert.type].icon);
</script>

<!--

Renders the following Markdown as alerts:

> [!NOTE]
> Example note

> [!TIP]
> Example tip

> [!IMPORTANT]
> Example important

> [!CAUTION]
> Example caution

> [!WARNING]
> Example warning

-->
<div class={`border-l-4 pl-2.5 ${alertStyles[alert.type].border} my-0.5`}>
	<div class="{alertStyles[alert.type].text} items-center flex gap-1 py-1.5">
		<SvelteComponent className="inline-block size-4" />
		<span class=" font-medium">{alert.type}</span>
	</div>
	<div class="pb-2">
		<MarkdownTokens
			id={`${id}-${tokenIdx}`}
			tokens={alert.tokens}
			{sandboxFiles}
			{onTaskClick}
			{onSourceClick}
		/>
	</div>
</div>
