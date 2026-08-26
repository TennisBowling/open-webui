<script lang="ts">
	// Lightweight, dependency-free stand-in for RichTextInput, rendered while the
	// real (tiptap/prosemirror-based) editor is being dynamically imported — see
	// MessageInput.svelte's `richTextInputLoader`. Also used permanently if the

	interface Props {
		// dynamic import ever fails, so typing/sending never breaks.
		value?: string;
		placeholder?: string;
		id?: string;
		onFocusChange?: (focused: boolean) => void;
		onkeydown?: (event: KeyboardEvent) => void;
		onpaste?: (event: ClipboardEvent) => void;
	}

	let {
		value = $bindable(''),
		placeholder = '',
		id = 'chat-input',
		onFocusChange = () => {},
		onkeydown,
		onpaste
	}: Props = $props();

	let textareaElement: HTMLTextAreaElement = $state();

	export const focus = () => {
		textareaElement?.focus();
	};
</script>

<textarea
	bind:this={textareaElement}
	{id}
	class="scrollbar-hidden rtl:text-right ltr:text-left bg-transparent dark:text-gray-100 outline-hidden w-full resize-none h-fit max-h-96 overflow-auto"
	style="field-sizing: content;"
	rows="1"
	{placeholder}
	bind:value
	onfocus={() => onFocusChange(true)}
	onblur={() => onFocusChange(false)}
	{onkeydown}
	{onpaste}></textarea>
