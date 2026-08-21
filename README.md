# 🚀 LRJK Blender AI Studio

> **Imagine. Create. Render. Evolve.**
> *Developed by RK Offisium*

[![Cross-Platform Build & Release](https://github.com/bcatsky-maker/LRJK-Blender-AI-Studio/actions/workflows/release.yml/badge.svg)](https://github.com/bcatsky-maker/LRJK-Blender-AI-Studio/actions)
[![License: MIT](https://img.shields.io/badge/License-MIT-gold.svg)](LICENSE.md)
[![Python Version](https://img.shields.io/badge/Python-3.11%2B-blue.svg)](https://python.org)
[![Blender Version](https://img.shields.io/badge/Blender-3.6%2B-orange.svg)](https://blender.org)

---

## Table of Contents

- [What this is](#what-this-is)
- [How it works](#how-it-works)
- [Quick Start](#quick-start)
- [Configuring AI generation](#configuring-ai-generation)
- [Free AI & 3D resources you can plug in](#free-ai--3d-resources-you-can-plug-in)
- [Building an installer from source](#building-an-installer-from-source)
- [Shipping updates (silent auto-update)](#shipping-updates-silent-auto-update)
- [Project layout](#project-layout)
- [Security & data notes](#security--data-notes)
- [Troubleshooting](#troubleshooting)

---

## What this is

**LRJK Blender AI Studio** is a small desktop app that sits next to Blender and lets you generate scene content from a plain-English prompt. It's two pieces working together:

- A **desktop app** (Windows/macOS/Linux, built with PySide6) that manages settings, ingests reference assets, and decides what to generate.
- A **Blender add-on** ("LRJK AI Studio" panel in the 3D viewport sidebar) that sends your prompt to the desktop app and builds the result in your scene.

When you type a prompt, the AI turns it into a **scene program** — an ordered list of small, composable actions (add a torus, give it a gold material, drop in a ground plane, add a light, place the camera) that together build the scene. The model doesn't just pick one of a few canned results; it composes primitives, so "a vibrant blue and golden donut on a table" actually produces a shaped, materialed, lit, framed scene. It can also pull real 3D models out of your ingested asset library (Poly Haven, Sketchfab, etc.) when a prompt calls for something primitives can't approximate.

Every action the AI can emit comes from a **fixed, whitelisted vocabulary** (see `src/core/ai_provider.py`'s `ACTION_SCHEMA`); the Blender add-on only ever runs its own pre-written, parameter-clamped handler for each named action, and never executes code sent over the wire. Widening the vocabulary from a toy set of three actions to a real toolkit didn't change that guarantee.

If no AI provider is configured, or the AI call fails, the app falls back to a **deterministic rule-based program builder** that still assembles a real lit, framed scene from keywords in your prompt (shape, color, "gold" → metallic, "glow" → emissive) — so a first run with zero API keys produces an actual result, not a grey box in the dark.

There's also a separate **"🧊 Generate 3D Mesh from Text"** button that generates an actual new mesh via [Tripo3D](https://www.tripo3d.ai/)'s text-to-3D API, downloads it, and imports it into your scene. It's kept deliberately separate — it calls an external credit-metered service, so it only ever runs when you explicitly click it.

## How it works

```
 You (in Blender)                Desktop App                          Blender Scene
 ─────────────────                ───────────                         ─────────────
 Type a prompt   ──POST──▶  Check attached BlendKit refs
 in the sidebar             Ask your AI provider for a SCENE PROGRAM
                            (or build one with rule-based fallback)
                            Resolve any library-asset lookups to files
                       ◀──response── {"actions": [ {action, params}, ... ]}
                                                          ──▶  Add-on runs each built-in
                                                                handler in order, building
                                                                the scene — nothing else
```

The desktop app and the Blender add-on talk over a small local HTTP server (`127.0.0.1:8081` by default). That connection is authenticated with a **pairing token** (see [Configuring AI generation](#configuring-ai-generation)) and every action in the returned program is one of a fixed, whitelisted set — never free-form code — so an AI provider (or anything else) can't make Blender run something outside that list.

## Quick Start

1. **Install the desktop app.** Grab the installer for your OS from [Releases](https://github.com/bcatsky-maker/LRJK-Blender-AI-Studio/releases), or build one yourself (see [Building an installer from source](#building-an-installer-from-source)). On Windows, the installer detects your local Blender install and copies the add-on into `%APPDATA%\Blender Foundation\Blender\<version>\scripts\addons\` automatically.
2. **Enable the add-on in Blender:** Edit → Preferences → Add-ons → search "LRJK AI Studio" → enable it. A new "LRJK AI Studio" tab appears in the 3D viewport sidebar (press `N` if the sidebar is hidden).
3. **Launch the desktop app** and open **AI Settings**. A pairing token is generated automatically the first time — copy it.
4. **Paste the token** into the Blender panel's "Bridge Token" field, then click **Connect** to confirm the two are talking.
5. Type a prompt in the panel and click **🚀 Generate AI Asset**.
6. *(Optional)* To generate an actual mesh from text instead of a placeholder, get a free [Tripo3D](https://platform.tripo3d.ai/) API key, paste it into AI Settings' "Tripo3D API Key" field (or set `TRIPO3D_API_KEY`), then type a description and click **🧊 Generate 3D Mesh from Text**. This calls out to Tripo3D and can take up to ~3 minutes.

That's it for a first run — everything above works with zero API keys, using the built-in rule-based program builder. Add a real AI provider once you want prompts composed more flexibly and creatively (next section).

## Configuring AI generation

Open **AI Settings** in the desktop app to set this up:

| Field | What it's for |
| :--- | :--- |
| **AI Service Preset** | Pick OpenAI, Anthropic Claude, a local Ollama model, or any custom REST endpoint that speaks the OpenAI chat-completions format. |
| **API Endpoint / Secret Key / Model** | Standard provider connection details. Local Ollama models don't need a key. |
| **Blender Bridge Port** | The local port the desktop app and the Blender add-on talk over. Only change this if `8081` is already in use on your machine. |
| **Blender Bridge Pairing Token** | Generated automatically, shown here so you can copy or regenerate it. Paste it into the Blender panel's "Bridge Token" field — requests without a matching token are rejected. |
| **Tripo3D API Key** | Powers the separate "🧊 Generate 3D Mesh from Text" button in Blender. Optional — leave blank and that button just reports it needs a key. Free keys at [platform.tripo3d.ai](https://platform.tripo3d.ai/). |

Click **Test Connection** to make a real request to your configured provider and confirm it works before relying on it.

You can also set provider keys as environment variables instead of typing them into the UI, if you prefer:

```powershell
# PowerShell (Windows)
$env:GROQ_API_KEY = "your_groq_api_key_here"
$env:GEMINI_API_KEY = "your_gemini_api_key_here"
$env:SKETCHFAB_API_TOKEN = "your_sketchfab_api_token_here"
$env:TRIPO3D_API_KEY = "your_tripo3d_api_key_here"
```

## Free AI & 3D resources you can plug in

The Studio is built around free and free-tier APIs so you're not required to pay for anything to get started.

### Language models (used to choose a scene action from your prompt)

| Provider | Free tier | Best for | Link |
| :--- | :--- | :--- | :--- |
| **Groq Cloud** | Very high rate limits on Llama 3 / Mixtral | Fast, low-latency generation | [groq.com](https://console.groq.com/) |
| **Google Gemini** | 15 requests/min free | Multimodal & scene-layout reasoning | [ai.google.dev](https://ai.google.dev/) |
| **OpenRouter** | Free open-weight models (Mistral, Gemma, Phi-3) | Fallback LLM option | [openrouter.ai](https://openrouter.ai/) |
| **Hugging Face Inference API** | Free serverless access | Custom fine-tuned models | [huggingface.co](https://huggingface.co/docs/api-inference/) |
| **Ollama** | 100% free, runs locally | No API key, fully offline | [ollama.com](https://ollama.com/) |

### Text-to-3D & mesh generation

| Provider | Status | Free tier | Formats | Link |
| :--- | :--- | :--- | :--- | :--- |
| **Tripo3D** | ✅ Wired in — the "🧊 Generate 3D Mesh from Text" button (`src/core/text_to_3d.py`) | Free monthly credits | GLB, GLTF, OBJ, FBX | [platform.tripo3d.ai](https://platform.tripo3d.ai/) |
| **Meshy AI** | Not wired in — reference for future work | Free monthly credits | GLTF, USDZ | [meshy.ai](https://www.meshy.ai/) |

`CSM (Common Sense Machines)` was listed here previously, but its public docs (`docs.csm.ai`) currently only document image-to-3D and text-to-image endpoints, not text-to-3D — it's been dropped from this table for accuracy.

### AI textures & images (not yet wired into the app - reference for future work)

| Provider | Free tier | Features | Link |
| :--- | :--- | :--- | :--- |
| **Pollinations.ai** | Unrestricted, no key needed | Fast Stable Diffusion synthesis | [pollinations.ai](https://pollinations.ai/) |
| **Polycam** | Free tier | AI texture maps from prompts | [poly.cam](https://poly.cam/) |
| **Clipdrop (Stability AI)** | Free trial credits | Background removal, upscaling, relighting | [clipdrop.co](https://clipdrop.co/apis) |

### Free 3D assets & HDRIs (ingested locally, then usable during generation)

Ingest any of these into your local library, and the AI can pull real models from it during generation via the `import_asset_from_library` action — when a prompt asks for something primitives can't approximate (a chair, a car, a tree), the desktop app searches your ingested models, extracts the best match, and imports it into the scene.

| Provider | License | What you get | Link |
| :--- | :--- | :--- | :--- |
| **Poly Pizza** | Free / CC0 | Thousands of low-poly models | [poly.pizza](https://poly.pizza/api) |
| **Poly Haven** | Public domain (CC0) | HDRIs, PBR textures, models | [polyhaven.com](https://polyhaven.com/api) |
| **Sketchfab** | Free downloadable assets | Millions of community models | [sketchfab.com](https://sketchfab.com/developers/download-api) |
| **BlendKit** | Free tier | Models, materials, HDRIs, brushes | in-app "Ingest BlendKit Free Library" |
| **MakeHuman** | CC0 | Character asset packs | in-app ingestion scripts |

## Building an installer from source

```powershell
git clone https://github.com/bcatsky-maker/LRJK-Blender-AI-Studio.git
cd LRJK-Blender-AI-Studio
pip install -r requirements.txt
python build_all.py
```

This runs the tests, compiles `src/core/generator.py` with Cython, bundles everything with PyInstaller, and (on Windows, with Inno Setup installed) produces a signed-or-unsigned installer under `Release_Installers/`. It does **not** push anything to GitHub by default — pass `--push` if you want it to commit and push tracked-file changes (like the version bump in `installer_setup.iss`) once the build succeeds:

```powershell
python build_all.py --push
```

Run just the test suite on its own with:

```powershell
python -m pytest tests/ -v
```

### Code quality (black · ruff · mypy)

Before the unit tests, both `build_all.py` and `build_update.py` run a code-quality gate: **black** (formatting), **ruff `--fix`** (linting with autofix), and **mypy** (type-checking). All three are configured in `pyproject.toml`. Install them once:

```powershell
pip install -r dev-requirements.txt
```

The gate is **advisory by default** — black and ruff apply their fixes in place and everything only *reports* what's left, so a style nit never blocks producing an installer. Any tool that isn't installed is skipped with a warning, so a machine without the dev extras can still build. Pass `--strict-checks` to make ruff/mypy findings **fail the build** (for CI):

```powershell
python build_all.py --strict-checks
python build_update.py --push --strict-checks
```

Run the tools by hand any time:

```powershell
black .            # format
ruff check --fix . # lint + autofix
mypy .             # type-check
```

mypy is deliberately tuned for a GUI + Blender codebase (`ignore_missing_imports`, no forced annotations) so it catches real bugs — undefined names, wrong types — without drowning in noise from PySide6/`bpy`. The Blender add-on (`src/blender_addon/`) is excluded from mypy since it only type-checks meaningfully inside Blender.

### Bundling your asset library into the installer (self-contained distribution)

If you've ingested an asset library (Poly Haven, Sketchfab, MakeHuman, etc.), `build_all.py` will **bundle the whole thing into the installer automatically** — so whoever you hand the installer to gets a fully-stocked app with no ingestion step. This happens whenever both `studio_memory.db` and `asset_store/` are present at build time:

1. `build_all.py` generates a small, portable `seed_library.db` (metadata only, with paths *relative* to `asset_store` so they survive being installed to a different machine — see `src/core/seed_library.py`).
2. Inno Setup bundles `asset_store/` and `seed_library.db` into the installer (the `#ifdef BundleLibrary` block in `installer_setup.iss`, enabled via `/DBundleLibrary=1`). Because the payload is large, the installer is split into `~2 GB` slices (`DiskSpanning`) — keep all `.bin` slices next to the `.exe` when distributing.
3. On the recipient's **first launch**, the app registers every bundled asset into their own writable database, resolving each path back to the `asset_store` the installer laid down next to the app. The files are read in place — never copied into the user profile — so there's no multi-gigabyte duplication.

> **Shrink `studio_memory.db` before building.** After the earlier BLOB→file migration, `studio_memory.db` can still be many GB of *freed-but-unreclaimed* space (SQLite doesn't shrink the file on its own). Reclaim it — this doesn't touch your assets and makes the seed step instant:
> ```powershell
> python -c "import sqlite3; c=sqlite3.connect('studio_memory.db'); c.execute('VACUUM'); c.close()"
> ```

Running the **installed** app on a machine that already has a library elsewhere (e.g. a dev checkout)? Point it at that data directly instead of the per-user folder:

```powershell
setx LRJK_STUDIO_DATA_DIR "G:\path\to\lrjk-blender-ai-studio"
```

## Shipping updates (silent auto-update)

`build_all.py` builds the big **first-time** installer (app + the multi-GB asset library). Once someone has that installed, you don't want to re-ship gigabytes every time you change a line of Python. `build_update.py` builds a small **update** installer — the app and add-on only, no asset library — that installs over an existing install and can be applied silently by the app itself.

**Build an update.** From the project root, after making your code changes:

```powershell
python build_update.py
```

It first fingerprints the application source (`src/` + `assets/`, deliberately excluding `installer_setup.iss`'s ever-changing version line and the large re-fetchable asset staging). If nothing about the app actually changed since the last update build, it prints "nothing to do" and stops — so it's safe to run any time. When it does detect a change it bumps the version, runs the tests, compiles a lean single-file update installer (`installer_setup.iss` compiled with `/DUpdateMode=1` — no disk spanning, closes/relaunches the running app), and writes an **update feed** into `updates/`:

```text
updates/
├── LRJK_Blender_AI_Studio_Update_v2.1.30.exe   # the small update installer
└── latest_update.json                          # {version, file, url, sha256}
```

Force a build even when nothing changed with `python build_update.py --force`.

**Publish to GitHub (private source repo + public releases repo).** The source repo is **private**, but a silent auto-updater needs a *public* (tokenless) URL to fetch from — a private repo's raw files and release assets both require an auth token. The clean split: keep all source in the private repo, and publish only the update feed (installer + `latest_update.json`) as **GitHub Releases on a separate public repo**.

One-time setup (uses the GitHub CLI, `gh`):

```powershell
# 1. Create the public releases repo that holds ONLY the update feed
gh repo create bcatsky-maker/LRJK-Studio-Releases --public ^
   --description "Auto-update feed for LRJK Blender AI Studio"

# 2. Make the source repo private
gh repo edit bcatsky-maker/LRJK-Blender-AI-Studio --visibility private ^
   --accept-visibility-change-consequences
```

Then publish an update with one command:

```powershell
python build_update.py --push
```

`--push` builds the update (if the app changed) and creates a GitHub Release `vX.Y.Z` on the public releases repo, uploading the installer and `latest_update.json` as assets (re-uploading over the tag if it already exists). Override the target with `--push --releases-repo=owner/name`. It needs `gh` installed and authenticated (`gh auth status`).

The app's **Update feed URL** is then this stable "latest release" URL (it always resolves to the newest release's asset):

```text
https://github.com/bcatsky-maker/LRJK-Studio-Releases/releases/latest/download/latest_update.json
```

Because the manifest's `url` field is left blank, the app resolves the installer *relative* to that manifest URL — and GitHub serves `releases/latest/download/<installer-name>` the same way, so both assets resolve with no app-side configuration.

Prefer to host the feed yourself instead? The *contents* of `updates/` work behind any HTTPS host, direct OneDrive/Dropbox link, or a UNC / `file://` path on a LAN — just point the app's Update feed URL at wherever `latest_update.json` lands.

**Point the app at it.** In the desktop app, open **AI Settings → ⬆️ Automatic Updates**, paste the manifest URL into **Update feed URL**, and leave **"Install newer updates silently on launch"** checked. On each launch the app fetches the manifest in the background; if the feed's version is newer than the running build it downloads the installer, verifies its SHA-256, and runs it silently (`/VERYSILENT`), then relaunches itself. Uncheck the box to get a non-intrusive "update available" banner instead of an automatic install.

The `updates/` folder and the local `.update_state.json` fingerprint are git-ignored — they're build/host artifacts, not source.

## Project layout

```text
lrjk-blender-ai-studio/
├── .github/workflows/release.yml   # Cross-platform CI/CD release workflow
├── assets/                         # App icon, banner, splash video, source images
├── browser_extension/              # Optional Chrome extension: send a web link/model to the Studio
├── src/
│   ├── core/
│   │   ├── generator.py            # Procedural generator (Cythonized at build time)
│   │   ├── ai_provider.py          # Calls your configured AI provider, whitelists its response
│   │   ├── text_to_3d.py           # Tripo3D text-to-3D client (submit / poll / download)
│   │   ├── memory_db.py            # Local SQLite history + reference index
│   │   ├── asset_manager.py        # On-disk asset store + metadata + library search
│   │   ├── seed_library.py         # Build/import the installer-bundled asset library
│   │   ├── paths.py                # Data-dir resolution (dev vs installed, env override)
│   │   └── download_*.py           # Ingestion scripts for Poly Haven, Sketchfab, MakeHuman, etc.
│   ├── ui/main_window.py           # PySide6 desktop app
│   └── blender_addon/blender_rag_addon.py   # Blender viewport add-on
├── tests/                          # Pytest suite
├── build_all.py                    # Cython → PyInstaller → Inno Setup build pipeline (full installer)
├── build_update.py                 # Change-detected lean UPDATE installer + update feed
├── pyproject.toml                  # black / ruff / mypy config
├── dev-requirements.txt            # Build + code-quality tooling (pytest, black, ruff, mypy)
└── requirements.txt                # Python dependencies
```

## Security & data notes

A few things worth knowing if you're extending this project:

- **The local bridge is authenticated.** The desktop app and the Blender add-on only talk to each other if the pairing token matches, and the connection only accepts `application/json` requests. Regenerate the token from AI Settings any time you want to invalidate the old one.
- **Responses are whitelisted, never executed as raw code.** The desktop app replies with a program of named actions drawn from a fixed vocabulary; the add-on runs its own matching built-in handler for each. This holds whether the program came from an AI provider or the rule-based fallback, and adding more actions to the vocabulary never changes it — no action is ever free-form code.
- **Assets are stored as files, not database blobs.** New assets you ingest go to disk under a per-user asset store, with only metadata in `studio_memory.db`. If you're carrying forward an older, very large database, see `src/core/migrate_blobs_to_files.py` for a safe (interruptible, re-runnable) way to shrink it.
- **`studio_memory.db` isn't bundled into installers or committed to git.** It's your local data, not application code — see `.gitignore`.
- **Don't commit API keys.** Provider keys can be set via the environment variables shown above, or stored in AI Settings (kept in your OS's local app settings, not in the repo). If a key or token ever ends up committed to git by mistake, rotate it from the provider's dashboard, not just from your local copy.

## Troubleshooting

| Symptom | Likely cause |
| :--- | :--- |
| Blender panel says "Bridge Token missing/incorrect" | Copy the current token from the desktop app's AI Settings dialog into the Blender panel's "Bridge Token" field. |
| "Connect" button reports the port as offline | Make sure the desktop app is running, and that the port in Blender matches the "Blender Bridge Port" in AI Settings (default `8081`). |
| Generation always uses the rule-based fallback | Open AI Settings and click "Test Connection" — it'll tell you exactly why the AI provider call is failing (bad key, wrong endpoint, model not found, etc.). |
| "🧊 Generate 3D Mesh from Text" reports a missing/rejected key | Set a Tripo3D API key in AI Settings or via `TRIPO3D_API_KEY` — get one free at [platform.tripo3d.ai](https://platform.tripo3d.ai/). |
| Mesh generation times out or takes a long time | Normal — Tripo3D generation can take up to ~3 minutes, and both the Blender add-on and the desktop app wait that long before giving up. Avoid closing either app mid-generation. |
| `studio_memory.db` is huge | See [Security & data notes](#security--data-notes) above — run the migration script to move old asset data out of the database and onto disk. |
