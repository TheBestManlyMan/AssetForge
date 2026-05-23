# CLAUDE.md — Asset Forge

Context for Claude Code working in this project. Read before generating any code.

## What this is

Asset Forge is a Houdini 21.0 TOPs/PDG pipeline that turns a blockout layout into
a fully generated, styled 3D scene. Each asset in the layout becomes a TOP work
item; each work item flows through render → AI image gen → image-to-3D → mesh
import → proxy → per-asset USD; a final gate reassembles every asset into one
scene USD.

Full spec lives in `AssetForge_Spec.md`. This file is the build context.

## Core principles (do not violate)

1. **File-based data flow.** Nothing lives in memory between TOP nodes. Every
   node reads its input from a path and writes its output to a path. Work items
   carry path attributes only.
2. **Backend-swappable AI calls.** Every node that hits an AI service uses a
   pluggable backend (one module per service, common interface). Same graph,
   different service. Don't bake a single vendor into node logic.
3. **Backend client deps — stdlib-only by default.** Backend modules should
   use ONLY the Python standard library (urllib, json, base64) so they run in
   Houdini's bundled Python with zero setup. `meshy_client.py` and
   `nano_banana_client.py` are the reference templates — match their structure
   for new backends.
   **Documented exception:** fal.ai backends (`fal_base.py`, `trellis_client.py`,
   `pixal3d_client.py`) use the `fal-client` pip package because fal's auth +
   queue subscribe + file-upload flow isn't worth rolling in stdlib. Install
   once into Houdini's bundled Python:
   `/opt/hfs21.0.650/python/bin/python -m pip install fal-client`.
   Don't introduce further pip deps without a similarly explicit reason.
4. **Per-asset folders.** Each asset gets its own directory; all its artifacts
   (preview, prompt, image, mesh, proxy, usd) live there.

## Backend interfaces

```python
# llm/base.py        — Prepare Prompt
def generate(messages, image=None, **opts) -> str
# image_gen/base.py  — Style Pass
def generate(input_image, prompt, ref_image, out_path, **opts) -> str
# mesh_gen/base.py   — Image-to-3D
def generate(input_image, out_glb_path, **opts) -> str
```

Existing backend clients:
- `gemini_text_client.py` — Gemini text API for LLM prompt generation. stdlib-only.
  `call(user_message, model=None) -> str` — sends a pre-built message, returns
  the text response. `build_prompt(name, keywords) -> str` — higher-level helper
  (builds message from name + keyword dict). Uses `GEMINI_API_KEY`. Default model
  `gemini-2.5-flash`; override via HDA `llm_model` parm.
- `meshy_client.py` — Meshy image-to-3D, stdlib-only. Template for stdlib
  backends. Entries:
  - `generate(input_image_path, out_glb_path, **opts) -> str` — submit + poll + download in one call (sequential)
  - `submit(input_image_path, **opts) -> task_id` — submit only, returns immediately
  - `poll_all([(task_id, out_glb_path), ...]) -> list` — poll multiple tasks in one shared loop (parallel Meshy processing)
- `nano_banana_client.py` — Nano Banana Pro (Gemini 3 Pro Image) style pass.
  `generate_image(input_image_path, output_png_path, prompt, ref_image_path=None,
  aspect_ratio=None, verbose=True) -> {"model", "image_path"}`. stdlib-only.
- `fal_base.py` — `FalMeshBackend` shared base for fal.ai image-to-3D
  backends. Handles FAL_KEY check, `fal_client.upload_file`,
  `fal_client.subscribe`, and GLB download. Subclasses set `MODEL_ID`,
  `OUTPUT_KEY`, `TAG`, and `default_args()`. Uses pip `fal-client` (see §3).
- `trellis_client.py` — fal-ai/trellis-2. Params: `resolution`
  (512/1024/1536), `texture_size` (1024/2048/4096). Entry:
  `generate_3d(image_path, output_glb_path, **params) -> {"request_id",
  "glb_path", "result"}`.
- `pixal3d_client.py` — fal-ai/pixal3d. Params: `resolution` (1024/1536),
  `texture_size` (1024/2048/4096). Same `generate_3d(...)` shape as Trellis.

Note: fal mesh-gen clients expose `generate_3d(...)` rather than
`generate(...)` — they return a dict (request_id + glb_path + raw result),
not a plain output path. `mesh_gen_top.py` currently calls
`meshy_client.generate(...)` directly; making the TOP backend-pluggable is a
follow-up.

API keys come from env vars (`MESHY_API_KEY`, `GEMINI_API_KEY`, `FAL_KEY`).
Never hardcode.

Nano Banana model note: `gemini-3-pro-image-preview` (Pro) needs billing
enabled — free tier has `limit: 0` for all Gemini image models. Until billing
is on, use `gemini-2.5-flash-image`. Swap the `MODEL` constant to flip.

## Per-asset folder layout

```
.../asset_forge/v001/
├── layout/assets.json              ← one entry per asset
├── assets/asset_001/
│   ├── data.json                   ← prompt + metadata
│   ├── preview.jpg                 ← isolated placeholder render (style ref)
│   ├── generated.png               ← styled image (image gen output)
│   ├── mesh.glb                    ← raw image-to-3D output
│   ├── aligned.bgeo.sc             ← fitted + transformed to placeholder
│   ├── proxy.bgeo.sc               ← decimated
│   └── asset.usd                   ← per-asset deliverable
└── final/scene.usd                 ← master, references all assets
```

## Node graph (current → planned)

```
Layout Export (Python SOP)        → assets.json                 [DONE]
  ↓
load_assets (JSON Input TOP)      → one work item per asset     [DONE]
  ↓
expand_asset_dir (attrib create)  → asset_dir made absolute     [DONE]
  ↓
OpenGL_Fetch (ropfetch)           → preview.jpg                 [DONE]
  ↓
Preview_OpenGL_mplay              → opens preview.jpg in mplay  [DONE]
  ↓
Keywords_From_HDA (pythonprocessor) → @place, @year (+ any     [DONE]
                                      keys in HDA keywords dict)
  ↓
Prompt_Build (pythonprocessor)    → @prompt per asset           [DONE]
  (LLM off: resolves @attrs in prompt_template directly)
  (LLM on:  sends name+keywords to Gemini, falls back to template)
  ↓
Image_Gen (pythonprocessor)       → generated.png               [DONE]
  ↓
Preview_Gen_mplay                 → opens generated.png in mplay [DONE]
  ↓
Mesh_Gen (pythonprocessor)        → mesh.glb                    [BUILDING]
  (submit all assets first, then poll_all in shared loop)
  ↓
Mesh Import + Reapply (SOP)       → aligned.bgeo.sc             [TODO]
  ↓
Proxy Gen                         → proxy.bgeo.sc               [TODO]
  ↓
Per-Asset USD                     → asset.usd                   [TODO]
  ↓
Waitfor All → Scene Assembly      → scene.usd                   [TODO]
```

## HDA parms (asset_forge node)

| Parm | Type | Purpose |
|------|------|---------|
| `assets` | string | Path to Assets_Pre geo node |
| `camera` | string | Scene camera path |
| `keywords` | dict | Scene context fed to all work items — add keys freely, they become `@attrs` |
| `prompt_template` | multiline string | Direct image gen prompt with `@attr` placeholders. Used when LLM is off, also the fallback when LLM fails |
| `use_llm` | toggle | On: Gemini writes the prompt from name+keywords. Off: template used directly |
| `llm_model` | menu | Gemini model: `gemini-2.5-flash` / `gemini-2.5-pro` / `gemini-2.0-flash` |
| `llm_prompt` | multiline string | The message sent to the LLM. Supports `@attr` substitution — any work item attrib can be inlined. Prompt_Build reads this with `unexpandedString()` + `_expand_attrs()`. |

## Known gotchas (learned the hard way — don't repeat)

- `pdg.WorkItem` has no `attribs()` or `copyAttributesFrom()` method in H21.0.
  Iterate with `attribNames()` + `attribType()`, copy manually using
  `stringAttribArray()` / `intAttribArray()` / `floatAttribArray()`. Always
  guard against `None`/empty return with `if not val: continue`.
- `pythonprocessor` nodes warn "A custom Cook Task callback is defined, but no
  work items are configured to cook in process" if the cooktask parm has content
  but no `in_process=True` items are created. Fix: clear the cooktask parm on
  nodes that do all their work in `generate`. Only `Mesh_Gen` needs a cooktask.
- `addWorkItem(parent=upstream)` without `in_process=True` triggers the above
  warning when a cooktask is defined. Set `in_process=True` explicitly or clear
  the cooktask.
- Gemini 429 `RESOURCE_EXHAUSTED` means prepaid credits are depleted, not a rate
  limit — retrying won't help. Toggle `use_llm` off on the HDA to fall back to
  the direct prompt template.
- `importlib.reload(gemini_text_client)` must be called in the node script, not
  just `import` — Houdini caches the module across cooks.

- `$HIP` inside a Python SOP parm is expanded by Houdini BEFORE the code runs.
  Build the literal at runtime via concatenation (`"$" + "HIP"`).
- PDG does NOT expand `$HIP` inside attribute *values* across the `@attr`
  substitution boundary. Expand explicitly with an attributecreate node using
  `hou.text.expandString(pdg.workItem().stringAttribValue(...))` before any node
  that uses the path. (This is what `expand_asset_dir` does.)
- `PackedFragment` has no `unpackGeometry()` / `embeddedGeometry()` in H21.0 —
  use `prim.boundingBox()` (inherited from `hou.Prim`).
- JSON Input TOP's `prop` is JSON Pointer, NOT JSONPath. `assets` works,
  `$.assets[*]` fails.
- `extractmult` (Data Extractions multiparm) is silently ignored in Array
  Retrieve mode. Use `Unpacked Attributes` + `field = *` instead.
- `attribValue("transform")` on a float-array returns only the first element —
  use `floatAttribArray("transform")` for the full list.
- OpenGL ROP `scenepath` must be an OBJ network, not a leaf geo OBJ; `vobjects`
  filters its OBJ children. (Mental model: scenepath = folder, vobjects = glob.)
- Houdini's bundled Python finds no CA bundle on Linux → `SSL:
  CERTIFICATE_VERIFY_FAILED`. Build the `urlopen` SSL context explicitly with
  `ssl.create_default_context(cafile=...)` pointing at
  `/etc/ssl/certs/ca-certificates.crt` (Debian/Ubuntu), with RHEL/Alpine
  fallbacks and `SSL_CERT_FILE` override. Stdlib-only.
- `pdg.WorkItem` in H21.0 has NO `expandString` method — that's a `pdg.Node`
  thing. Do `@attr` substitution manually (regex + `stringAttribValue`) or use
  node-level expand.
- Reading a parm with `evaluateString()` when its text contains `@attr` fails
  with "No work item to use" if there's no current item context. Use
  `unexpandedString()` and expand yourself per item.
- Python is import-cached: editing a backend module (e.g.
  `nano_banana_client.py`) does NOT take effect on the next cook.
  `sys.modules.pop("nano_banana_client", None)` or restart Houdini.
- `houdini.env` is read only at startup. `hou.putenv(name, value)` injects into
  `os.environ` for the running process if you need a key without restarting.
- **Menu parms:** `evalAsInt()` returns the menu *index* (0, 1, 2…), not the
  token value. For menu parms that store numeric values (resolution, texture_size),
  use `int(node.parm("x").evalAsString())` to get the actual number.
- **PIL has no WebP support** in Houdini's bundled Python (compiled without
  libwebp). Use ffmpeg subprocess for WebP conversion instead.
  `glb_webp_to_png.py` handles this for GLB post-processing.
- **Pixal3D GLBs use `EXT_texture_webp`** — Houdini can't load them without
  conversion. `pixal3d_client.py` calls `glb_webp_to_png.convert()` automatically
  after every download. To fix existing GLBs manually: import and call `convert(path)`.
- **H21 ropfetch has no parm-override multiparm** — can't inject work item attrs
  into ROP parms (like `lopoutput`) natively. Workaround TBD for per-asset USD save.
- When reloading a backend that imports another backend (e.g. `pixal3d_client`
  imports `fal_base`), reload the dependency first:
  `for m in ("fal_base", "pixal3d_client"): sys.modules.pop(m, None)`

## Conventions for new nodes

- One work item in, one out. Prefer in-process Python while building/testing;
  move to scheduler jobs only when scaling.
- try/except that NAMES the work item on failure, so one bad asset is debuggable.
- Cheap caching: skip work if the output file already exists, unless an
  `overwrite` toggle on the node is on. AI calls cost money — never regenerate
  blindly.
- Make backend modules importable by adding their dir to `sys.path` explicitly
  inside the node. Don't assume PYTHONPATH.

## Tech stack

Houdini 21.0 (TOPs/PDG, ROPs, SOPs) · ComfyUI (local/networked) ·
houdini-comfy-bridge · image-to-3D (Meshy/Tripo/Rodin/Hunyuan3D/TripoSR) ·
style transfer (Nano Banana Pro / IPAdapter / Flux Redux) · USD assembly.
