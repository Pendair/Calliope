# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Calliope is a local-first story-to-video studio: a FastAPI backend (`calliope-backend/`, Python 3.11+, SQLite) plus a SvelteKit SPA (`calliope-web/`, Svelte 5, Tailwind, adapter-static). It drafts story/script with an OpenAI-compatible LLM, renders assets and shot clips by driving the user's ComfyUI over HTTP, and stitches the film with ffmpeg. Windows is the main platform (the `.bat` scripts, the `.venv\Scripts\` paths).

## Commands

First-time setup: `setup.bat`, which creates `calliope-backend/.venv`, runs `pip install -e ".[dev]"` and `npm install`. It is safe to re-run.

Run: `start.bat` (two console windows) or `start-bg.bat` (logs go to `logs/`); stop with `stop.bat`. The backend is on `127.0.0.1:8247` and the frontend on `127.0.0.1:5173`. Vite proxies `/api` to the backend.

Backend (from `calliope-backend/`):
```bash
.venv/Scripts/python -m calliope.main --host 127.0.0.1 --port 8247
.venv/Scripts/python -m pytest                                   # all tests (asyncio_mode=auto)
.venv/Scripts/python -m pytest tests/test_clips.py::test_name -q # single test
.venv/Scripts/ruff check src tests                               # line-length 100, rules E,F,I,N,W
.venv/Scripts/python scripts/export_contracts.py                 # regenerate contracts.json
.venv/Scripts/python -m PyInstaller calliope.spec --noconfirm    # packaged build (entry: launcher.py)
```

Frontend (from `calliope-web/`):
```bash
npm run dev
npm run check    # svelte-kit sync + svelte-check (type check)
npm run lint     # prettier --check + eslint
npm test         # scripts/check-storage-ssr.mjs + scripts/check-carry-values.mjs (regression guards)
npm run build
```

CLI: `calliope-cli.bat <group> <verb> ...` runs `python -m calliope.cli.main`.

## Architecture

**Backend entry** (`src/calliope/main.py`): `create_app()` mounts every router under `/api/*`. Unknown `/api/*` paths get an explicit 404, and the built SPA, if present in `src/calliope/static/` (gitignored), is served with an index.html fallback. On startup the lifespan hook runs `migrate_db`, rebases stale absolute asset paths (handles a moved install folder), recovers orphaned agent sessions, and starts the queue worker.

**Config** (`config.py`): a pydantic-settings singleton `settings` with the `CALLIOPE_` env prefix, persisted to `calliope_config.json` (holds API keys and is never committed). `BACKEND_ROOT` is the exe directory when frozen, so data and config sit next to the exe. `CONFIG_FILE` is resolved at import time from `CALLIOPE_CONFIG_FILE`. `tests/conftest.py` sets this before importing `calliope` so tests never overwrite the real config. Keep that ordering. The `client` fixture mutates the `settings` singleton in place (temp `data_dir`, `dry_run=True`).

**Database** (`db.py`): sync `sqlite3`. `SCHEMA` uses `CREATE TABLE IF NOT EXISTS`, and `migrate_db` adds columns to existing DBs. New columns go there, never into a destructive migration. Core hierarchy: project → story_beats / characters / locations / items → scenes → clips (1 scene : N shot clips) → jobs. Other tables cover agent sessions/events/memory, canvas nodes/edges, shot compositions, and `cli_audit`.

**Render pipeline**: routers enqueue `jobs` → `queue/worker.py` patches the workflow JSON and talks to ComfyUI (`comfyui/client.py`: `/upload/image`, `/prompt`, polls `/history`) → outputs are downloaded into `assets_dir`. In dry-run mode it writes placeholder PNG/MP4 instead. Calliope never reads or writes ComfyUI's local folders. Film export (`export/runner.py`) uses ffmpeg.

**ComfyUI role contract** (`comfyui/roles.py`, `parser.py`, `patcher.py`, `profiles.py`): node IDs are never hardcoded. Inputs and outputs come from node titles like `Name (Input:prompt)` / `(Output:video)`, with an alias table for each role. Generic `(Input:image)` refs are filled in node-id order, and that order defines the `<Subject N>` numbering in the MiniMax H3 prompt profile. The H3 compile is deterministic, and the LLM rewrite runs only on an explicit Regenerate. Text the user types into a prompt is always sent verbatim. Video inputs are workflow-owned (no auto-chaining).

**Events**: `events/bus.py` is an in-process pub/sub that `/api/events` streams to the web over SSE (names like `job.*`, `agent.*`, `canvas.*`).

**Agent harness** (`agent/harness/`): `runner.py` runs agent turns as asyncio tasks, separate from the render queue. The session event log (`log.py`) is the source of truth, and LLM history is derived from it. `orchestrator.py` has a planner that splits complex goals into role sub-agents (story/script/assets/video, tool subsets in `ROLE_TOOLS`). Simple requests use the single loop in `loop.py`. Tools are registered from `harness/plugins/*` through `registry.py`. Agents go through the same DB and job queue as the UI, never around it. Per-session scratch files live in `workspace/sessions/<id>` with path-escape and sensitive-name guards. LLM selection works per role, falling back to the Active LLM.

**Authoring layer + CLI** (`authoring/`, `cli/`): `authoring/` is the only place the CLI's SQL lives. Its functions take a caller's connection and never commit. The transaction, audit row and data write all happen in `cli.context.cli_txn`. The CLI itself has no SQL, no shell and no model calls. Its invariants are enforced by tests: it has no delete-like verbs (`cli/policy.py`, walked by `tests/test_cli_boundary.py`), it never calls an LLM, image/video columns are read-only, and the only filesystem write is the audit JSONL mirror (`tests/test_cli_physical.py`). Writes take `--expect-hash`. Exit 3 means the content drifted and exit 2 means a bad payload. When you change the CLI, also keep `skills_opencode/calliope/SKILL.md` in sync.

**Cross-layer contract gate**: backend and web share string names that nothing type-checks: SSE event names, agent tool names, ComfyUI roles, harness event types, guard codes. `tests/test_contract_vocabulary.py` scans `calliope-web/src` and fails if a referenced name isn't in `contracts.json`. After renaming or adding any of these, run `scripts/export_contracts.py` and commit the updated `contracts.json`.

**Frontend** (`calliope-web/src`): SPA only (static adapter, `fallback: index.html`, absolute `/_app` paths). Routes include `projects`, `project/[id]` (the Story/Assets/Script/Video stages + Film view), `agents`, `canvas` (AI Canvas, @xyflow/svelte), `playground`, `build-scene` (Three.js shot composer, MediaRecorder export normalized to MP4 server-side), `library`, and `settings`. API calls go through `lib/api.ts` with @tanstack/svelte-query, and SSE through `lib/events.ts`. UI strings live in `lib/i18n/{en,zh,es,fr,de,ja,ko}.ts`, so add a new key to every locale. Browser storage must go through `lib/storage.ts`, which is SSR-safe on Node 22+ (`npm test` guards this).

## Conventions

- Version is single-sourced in `calliope-backend/src/calliope/__init__.py` (`__version__`). `calliope-web/package.json` carries the matching version.
- User-facing changes get a dated note in `docs/changelogs/` and a line in `docs/changelogs/README.md` (newest first).
- Never fake results silently. An unreachable ComfyUI or an invalid input fails the job with an error naming the node. Dry-run is the only placeholder path.
