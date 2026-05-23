# Asset Forge — Tools

Backend clients and PDG glue for the **Asset Forge** Houdini pipeline (turns a
blockout layout into a fully generated, styled 3D scene).

See [`CLAUDE.md`](CLAUDE.md) for the build context and core principles
(file-based data flow, backend-swappable AI calls, stdlib-only by default).

## Modules

| File | Role |
|------|------|
| `gemini_text_client.py` | LLM prompt generation (stdlib Gemini text API) |
| `nano_banana_client.py` | Image-gen backend (reference stdlib template) |
| `meshy_client.py` | Image-to-3D via Meshy (`generate`, `submit`/`poll_all`) |
| `fal_base.py` | Shared `FalMeshBackend` (uses `fal-client` pip dep) |
| `trellis_client.py` | fal.ai TRELLIS-2 mesh backend |
| `pixal3d_client.py` | fal.ai Pixal3D mesh backend |
| `glb_extract.py` | GLB texture extraction + metallic-roughness split |
| `mesh_gen_top.py` | TOP/PDG entry point for the mesh-gen stage |

## Setup

Most backends are stdlib-only and run in Houdini's bundled Python with no setup.
The fal.ai backends need one pip dep:

```bash
/opt/hfs21.0.650/python/bin/python -m pip install fal-client
export FAL_KEY=...
```

## Related

- **Pipeline / production data:** Prism project at `~/Projects/AssetForge/`
- **Notes / status / dev log:** Pipeline+ vault, `1_Projects/Asset_Forge/`
