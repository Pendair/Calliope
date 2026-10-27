/** Opt-in Comfy Router picks. The value is what the generate APIs send as router_model. */

export const ROUTER_VIDEO = 'minimax/minimax-h3';
export const ROUTER_IMAGE = 'bfl/flux-3-image';

export const ROUTER_VIDEO_VALUE = `router:${ROUTER_VIDEO}`;
export const ROUTER_IMAGE_VALUE = `router:${ROUTER_IMAGE}`;

export function routerModelOf(value: unknown): string | null {
	if (typeof value !== 'string' || !value.startsWith('router:')) return null;
	const model = value.slice('router:'.length);
	return model === ROUTER_VIDEO || model === ROUTER_IMAGE ? model : null;
}
