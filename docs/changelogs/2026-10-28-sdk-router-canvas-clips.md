# Calliope 1.6.2 — Comfy SDK, Router, and clip cards

Release **v1.6.2**, 2026-10-28. Setup for the two Comfy paths is on the wiki: [Comfy SDK and Router](https://github.com/benjiyaya/Calliope/wiki/Comfy-SDK-and-Router).

## Local workflows go through the Comfy SDK

A workflow job is still a row in Calliope's queue. After the graph is patched, the worker submits it with `comfy-sdk` to `comfy-api-proxy`, which fronts your ComfyUI.

- ComfyUI stays on port **8188** (**Settings → Comfy → Base URL**).
- The proxy listens on port **8189** (**Comfy API URL**, already the default). Start it with ComfyUI already running:

```bash
pip install comfy-api-proxy
comfy-api-proxy --comfyui http://127.0.0.1:8188 --port 8189
```

- Role tags, prompt profiles, and strict mode are unchanged. The SDK does not rewrite prompts.
- Image, video, and audio paths on the form are uploaded as SDK assets. A path that is a folder or a missing file fails before the job is submitted.
- H3 latent continue-from-previous still uploads the latent to port 8188. The proxy then forwards that filename.
- Cancel still interrupts ComfyUI on 8188 so the GPU stops, and the worker also cancels the SDK job.
- Dry-run still writes a placeholder and never opens an SDK client. If the proxy is down, the job fails and names both URLs.

## Comfy Router is optional

Router calls `https://api.comfy.org`. It does not use the proxy. Leave it off and every generate stays on your imported workflows.

1. Settings → Comfy → save a **Comfy API key**. The field clears and the placeholder says the key is saved. A later save with the field left blank keeps the key.
2. Turn on **Use Comfy API provider (Router)**. The key alone does not show the new choices.

Then the pickers gain two rows:

| Where | Choice | What it sends |
|---|---|---|
| Project → Video, and the canvas `@` menu | **Comfy Router · MiniMax H3** | The clip prompt, up to 9 reference images, and a duration from 4 to 15 seconds |
| Project → Assets | **Comfy Router · FLUX 3** | That character, environment, or item's image prompt |

Partner result URLs expire quickly, so the worker downloads the file into your assets folder immediately. The clip, the asset card, and the queue use that local file. A Router job with the switch off, or with no key, fails closed. It does not fall through to a local workflow.

The agent can run MiniMax H3 through Router only when your message asked for it. Render approval is unchanged.

A demo that only shows Router cannot enter an Open Weights track. The default path is the SDK plus your own ComfyUI.

## Project canvases show each clip

A project canvas used to show one card per scene, and that card played the scene's first ready clip. Each clip is now its own card, in the column between items and scenes.

- The card is labeled with the mention the agent already understands (`#3.2`) and the scene heading under it. Reordering a scene or a clip updates that label. Dragging a card does not get undone by the refresh.
- Lines run from the scene's characters and from its location to every clip of that scene. Items are not on those lines. The lines are drawn from the cast, not stored, so they follow a cast edit. You cannot draw or delete them on the board.
- The scene card keeps the environment still. The clip card shows that clip's video, with a first frame when there is no poster.
- A finished clip render stays on the clip card. The board does not add a second, unconnected card for the same file. An older take with a different file stays on the board, with no line.
- A sandbox canvas has no story cast, so it gets neither clip cards nor those lines.

While the canvas chat is open, a finished agent tool refreshes the board and the scene list, so a new clip or a cast edit shows up without a reload.

## The agent keeps the workflow you already picked

A swarm turn used to forget the workflow you chose on the previous card. Answering "yes, generate" made it ask you to pick the workflow again. The session now remembers that choice until you ask to switch workflows.
