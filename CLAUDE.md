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
  `fal_client.subscribe`, GLB download, and an automatic `glb_webp_to_png`
  conversion right after download (so Pixal3D/Trellis webp GLBs load in
  Houdini). Subclasses set `MODEL_ID`, `OUTPUT_KEY`, `TAG`, and
  `default_args()`. Uses pip `fal-client` (see §3).
- `trellis_client.py` — fal-ai/trellis-2. Params: `resolution`
  (512/1024/1536), `texture_size` (1024/2048/4096). Entry:
  `generate_3d(image_path, output_glb_path, **params) -> {"request_id",
  "glb_path", "result"}`.
- `pixal3d_client.py` — fal-ai/pixal3d. Params: `resolution` (1024/1536),
  `texture_size` (1024/2048/4096). Same `generate_3d(...)` shape as Trellis.
- `glb_extract.py` — pulls embedded textures out of a GLB into a `textures/`
  dir (maps images → albedo/normal/roughness/metallic/… slots via material
  assignments, splits packed metallic-roughness). stdlib + PIL.
- `glb_webp_to_png.py` — converts `EXT_texture_webp` GLBs (Pixal3D / some
  Trellis) to plain PNG glTF **in place**: ffmpeg-converts each webp image,
  rebuilds the binary buffer with recomputed 4-byte-aligned bufferView offsets,
  repoints `texture.source`, drops the extension declarations. Entry
  `convert(glb_path, verbose=True) -> bool` (no-op → False for non-webp GLBs).
  Called automatically by `fal_base` after every download. stdlib + ffmpeg
  subprocess (PIL has no webp in Houdini's Python).

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
.../<collection>/v001/              ← <collection> = HDA instance name ($OS), see Multi-instance
├── layout/assets.json              ← one entry per asset
├── assets/<asset_name>/            ← folder named after the asset, NOT asset_001
│   ├── data.json                   ← prompt + metadata
│   ├── preview.jpg                 ← isolated placeholder render (style ref)
│   ├── generated.png               ← styled image (image gen output)
│   ├── mesh.glb                    ← image-to-3D output (webp→png converted)
│   ├── textures/                   ← extracted PBR maps (albedo/normal/rough/metal/…)
│   ├── contactsheet.png            ← preview | generated | render contact sheet
│   ├── aligned.bgeo.sc             ← fitted + transformed to placeholder
│   ├── proxy.bgeo.sc               ← decimated
│   └── <asset_name>.usd            ← per-asset deliverable
└── Scene/assets.usd                ← master, references all assets
```

Folder + file paths are authored by the **Resolver** node as a standardized
`path_*` attribute set — see the node graph below. The per-asset folder is the
sanitized asset *name* (de-duplicated), not `asset_001` (that stays as `@id`).

## Node graph (current → planned)

```
Create_JSON (Python SOP)          → assets.json                 [DONE]
  ↓
load_assets (JSON Input TOP)      → one work item per asset     [DONE]
  ↓
Resolver (pythonprocessor)        → expands $HIP + authors the   [DONE]
  standardized path_* attrib set from ONE ARTIFACTS dict:
  path_data / path_preview / path_generated / path_render /
  path_mesh / path_tex_dir / path_asset_usd / path_proxy /
  path_aligned / path_contactsheet (+ name_safe). SINGLE SOURCE OF
  TRUTH for paths — downstream reads @path_* instead of re-joining
  strings. Replaced the old expand_asset_dir attribcreate. To add a
  pipeline path, add one line to ARTIFACTS.
  ↓
OpenGL_Fetch (ropfetch)           → preview.jpg  (picture=@path_preview) [DONE]
  ↓
Keywords_From_HDA → LLM_Prompt_Build → @prompt per asset        [DONE]
  (LLM off: resolves @attrs in prompt_template directly)
  (LLM on:  sends name+keywords to Gemini, falls back to template)
  ↓
Image_Gen (pythonprocessor)       → generated.png               [DONE]
  ↓
{ PIXAL_Mesh_Gen | Meshy_Mesh_Gen } → mesh.glb                  [DONE]
  Two backend branches selected by switch2. Meshy uses submit-all
  then poll_all() in one shared loop (parallel). fal_base converts
  webp→png on download.
  ↓
Extract_Textures_{Pixal | Meshy}  → textures/                   [DONE]
  (one per branch; reads @path_mesh, writes @path_tex_dir)
  ↓
Save_USD (ropfetch)               → <name>.usd (lopoutput=@path_asset_usd) [DONE]
  ↓
Render_Generated (ropfetch)       → render.jpg (@path_render)   [DONE]
  ↓
Render_ContactSheet (ropfetch)    → contactsheet.png (@path_contactsheet) [DONE]
  ↓
Wait_All_Per_Asset → Build_Scene_Refs → Save_Scene_USD → Scene/assets.usd [DONE]

Still planned: mesh import + transform reapply (aligned.bgeo.sc),
proxy gen (proxy.bgeo.sc), and a backend-picker parm to replace the
two-node + switch2 setup.
```

All `path_*` attribs propagate downstream because Image_Gen, both mesh nodes,
and the texture nodes run a generic `_forward(src, dst)` loop (copies all
String/Int/Float attribs) — without it the manually-set attribs would drop the
`path_*` set after Image_Gen.

## HDA parms (asset_forge node)

| Parm | Type | Purpose |
|------|------|---------|
| `assets` | string | Path to Assets_Pre geo node |
| `camera` | string | Scene camera path |
| `collection` | string | Output collection folder under `$HIP` → `$HIP/<collection>/vNNN/`. Default expr `$OS` (the HDA instance name). Currently a **spare parm on the instance** (adding it to the definition over MCP crashes Houdini); promote in Type Properties to make it standard. |
| `version` | int | Pipeline version → `vNNN` folder segment. Read instance-relative everywhere (never via `/obj/asset_forge`). |
| `keywords` | dict | Scene context fed to all work items — add keys freely, they become `@attrs` |
| `prompt_template` | multiline string | Direct image gen prompt with `@attr` placeholders. Used when LLM is off, also the fallback when LLM fails |
| `use_llm` | toggle | On: Gemini writes the prompt from name+keywords. Off: template used directly |
| `llm_model` | menu | Gemini model: `gemini-2.5-flash` / `gemini-2.5-pro` / `gemini-2.0-flash` |
| `llm_prompt` | multiline string | The message sent to the LLM. Supports `@attr` substitution — any work item attrib can be inlined. Prompt_Build reads this with `unexpandedString()` + `_expand_attrs()`. |

## Multi-instance (the HDA runs as several instances)

The `asset_forge::1.0` HDA is instantiated multiple times (e.g. `asset_forge`,
`asset_buildings`, `asset_bottles`), each with its own `collection`/`version`.
So **nothing inside the HDA may hardcode `/obj/asset_forge`** or the literal
`asset_forge` folder — that's the #1 bug when cloning an instance (it silently
reads the *original's* version, paths, sub-nodes, and camera). Reference the
containing instance relatively:

- **String parms:** `` `chs("../../collection")` `` and
  `` v`padzero(3, ch("../../version"))` `` (internal nodes sit 2 levels below
  the HDA root).
- **Python SOPs / scripts:** anchor by *type*, not by fixed depth —
  `n = hou.pwd()` then `while not n.type().name().startswith("asset_forge"): n = n.parent()`.
  (`hou.pwd().parent().parent()` works but breaks if the node is re-nested.)
- **TOP cooktasks:** `self.topNode().parent().parent()`.
- **LOP render camera:** `sceneimport.objects = ../../Scene_Cam` (import this
  HDA's cam only) and
  `` camera = `pythonexprs("'/' + hou.node('../..').name() + '/null1/Scene_Cam'")` ``
  (the imported prim is `/<instance>/null1/Scene_Cam`).

The `collection` parm (default `$OS`) drives the output root. **Adding a parm to
the HDA *definition* over the live MCP bridge crashes Houdini** — add spare
parms on the instance, or edit in Type Properties.

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
  substitution boundary. Expand explicitly with `hou.text.expandString(...)`
  before any node that uses the path. The **Resolver** pythonprocessor does this
  once (expands `$HIP`, authors the absolute `path_*` set). It replaced the old
  `expand_asset_dir` attribcreate.
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
- **Pixal3D (and some Trellis) GLBs use `EXT_texture_webp`** — Houdini's glTF
  loader rejects *any* GLB that lists an unsupported extension in
  `extensionsRequired` ("unsupported extension EXT_texture_webp required by the
  file"), so the whole **mesh** fails to import — not just textures. Fix:
  `glb_webp_to_png.convert()` rewrites the GLB to PNG. It is wired into
  **`fal_base.generate_3d()`** (after download), so **all** fal backends convert
  automatically. ⚠️ This module went missing in the repo reorg and was
  recreated 2026-05-26 — the call now lives in `fal_base`, **not**
  `pixal3d_client` (older docs said otherwise). Fix existing GLBs manually with
  `import glb_webp_to_png; glb_webp_to_png.convert(path)`. (Bonus: `glb_extract`
  + the PIL metallic/roughness split also can't read webp, so converting first
  fixes texture extraction too.)
- **H21 ropfetch has no parm-override multiparm** — can't inject work item attrs
  into ROP parms (like `lopoutput`) natively. Workaround TBD for per-asset USD save.
- When reloading a backend that imports another backend, reload dependencies
  first. `pixal3d_client` imports `fal_base`, which now imports
  `glb_webp_to_png`, so the order is:
  `for m in ("glb_webp_to_png", "fal_base", "pixal3d_client"): sys.modules.pop(m, None)`

## Conventions for new nodes

- One work item in, one out. Prefer in-process Python while building/testing;
  move to scheduler jobs only when scaling.
- try/except that NAMES the work item on failure, so one bad asset is debuggable.
- Cheap caching: skip work if the output file already exists, unless an
  `overwrite` toggle on the node is on. AI calls cost money — never regenerate
  blindly.
- Make backend modules importable by adding their dir to `sys.path` explicitly
  inside the node. Don't assume PYTHONPATH.

## UI (interactive control layer)

`asset_forge_ui.py` (PySide6) + `asset_forge.pypanel` — a floating
**[ Scene Viewer | Controls ]** panel opened by the HDA "Launch UI" button
(`asset_forge_ui.launch(kwargs['node'])`). Phase 1 = the **Layout section**:
live framing sliders bound to the `Layout_Null` orbit rig (Rotation→ry,
Pitch→rx, Height→ty) + `Scene_Cam` (ortho) `orthowidth` (Distance); a "Render
Layout Preview" button (renders `Layout_OpenGL` with a temporary literal
`picture`, then restores `` `@path_previewlayout` ``); and a layout
image-generation block (prompt ← HDA `layout_prompt`; **N sequential** Nano
Banana re-rolls on a worker thread — Gemini has no seed/batch; candidates persist
as `layout_gen_*.png` and reload on open; click-to-keep → `layout_generated.png`).

Conventions: Houdini can't embed a live SceneViewer in a PySide panel → floating
split panel (`createFloatingPanel` + `pane.splitHorizontally()` + a `.pypanel`
interface whose script defines `createInterface()`). Network/render work runs off
the GUI thread (QThread). Bind widgets to live node parms; compare nodes by
`.path()`, not `is`. **Per-asset section** (mirror onto `Asset_Null`/`Asset_Cam`
+ `Centered`) is the next phase. Cam layout: `Scene_Cam` = ortho layout cam under
`Layout_Null`; `Asset_Cam` under `Asset_Null`.

## Tech stack

Houdini 21.0 (TOPs/PDG, ROPs, SOPs) · ComfyUI (local/networked) ·
houdini-comfy-bridge · image-to-3D (Meshy/Tripo/Rodin/Hunyuan3D/TripoSR) ·
style transfer (Nano Banana Pro / IPAdapter / Flux Redux) · USD assembly.
