type ComponentEventCallback = (event: CustomEvent<unknown>) => unknown;

export const dispatchComponentEvent = (
	props: Record<string, unknown>,
	type: string,
	detail?: unknown
) => {
	const callback = props[`on${type}`];
	if (typeof callback !== 'function') return true;

	const event = new CustomEvent(type, { detail, cancelable: true });
	const result = (callback as ComponentEventCallback)(event);
	return !event.defaultPrevented && result !== false;
};
