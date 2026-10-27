import type { Edge } from '@xyflow/svelte';
import type { CanvasNode, Scene } from '$lib/api';

/** Files that are a clip's current output. An artifact card with one of
 * these paths is the same video the clip card already shows. */
export function currentClipPaths(scenes: Scene[]): Set<string> {
	const paths = new Set<string>();
	for (const scene of scenes) {
		for (const clip of scene.clips ?? []) {
			if (clip.clip_path) paths.add(clip.clip_path);
		}
	}
	return paths;
}

/** Live #N.M mention plus the scene heading. Null when the scenes query
 * has not loaded that clip yet — the card then keeps its stored title. */
export function clipPresentation(
	scenes: Scene[],
	clipId: number,
): { mention: string; subtitle: string } | null {
	for (const scene of scenes) {
		const clip = scene.clips?.find((c) => c.id === clipId);
		if (!clip) continue;
		const heading = (scene.heading ?? '').trim() || `Scene ${scene.order_index}`;
		return {
			mention: `#${scene.order_index}.${clip.order_index}`,
			subtitle: heading,
		};
	}
	return null;
}

/** Cast lines from the scene's characters and location to each of its clips.
 * Derived from the project, not stored as canvas_edge rows. Items, scenes,
 * artifacts, and tombstoned cards are skipped. */
export function storyEdges(nodes: CanvasNode[], scenes: Scene[]): Edge[] {
	const flowId = new Map<string, string>();
	for (const node of nodes) {
		if (node.type !== 'entity' || node.entity_type == null || node.entity_id == null) continue;
		flowId.set(`${node.entity_type}:${node.entity_id}`, `cn-${node.id}`);
	}
	const edges: Edge[] = [];
	for (const scene of scenes) {
		const sources: Array<[string, string]> = [];
		for (const characterId of scene.character_ids ?? []) {
			sources.push([`character:${characterId}`, `c${characterId}`]);
		}
		if (scene.location_id != null) {
			sources.push([`location:${scene.location_id}`, `l${scene.location_id}`]);
		}
		for (const clip of scene.clips ?? []) {
			const target = flowId.get(`clip:${clip.id}`);
			if (!target) continue;
			for (const [key, tag] of sources) {
				const source = flowId.get(key);
				if (!source) continue;
				edges.push({
					id: `cast-${tag}-clip${clip.id}`,
					source,
					target,
					sourceHandle: 'asset',
					targetHandle: 'clip',
					type: 'smoothstep',
					deletable: false,
					selectable: false,
					focusable: false,
					interactionWidth: 0,
					style: 'stroke: #8b87c9; stroke-width: 1.25px',
				});
			}
		}
	}
	return edges;
}
