type EventHandler<T extends Event = Event> = (this: EventTarget, event: T) => unknown;

export const preventDefault = <T extends Event>(handler?: EventHandler<T>) =>
	function (this: EventTarget, event: T) {
		event.preventDefault();
		return handler?.call(this, event);
	};

export const stopPropagation = <T extends Event>(handler?: EventHandler<T>) =>
	function (this: EventTarget, event: T) {
		event.stopPropagation();
		return handler?.call(this, event);
	};

export const self = <T extends Event>(handler?: EventHandler<T>) =>
	function (this: EventTarget, event: T) {
		if (event.target === this) {
			return handler?.call(this, event);
		}
	};

type ListenerActionOptions = [event: string, handler: () => EventListener | null | undefined];

const listenerAction = (
	node: HTMLElement,
	[event, handlerFactory]: ListenerActionOptions,
	passive: boolean
) => {
	const handler = handlerFactory?.();
	if (!handler) return;

	node.addEventListener(event, handler, { passive });
	return {
		destroy: () => node.removeEventListener(event, handler)
	};
};

export const passive = (node: HTMLElement, options: ListenerActionOptions) =>
	listenerAction(node, options, true);

export const nonpassive = (node: HTMLElement, options: ListenerActionOptions) =>
	listenerAction(node, options, false);
