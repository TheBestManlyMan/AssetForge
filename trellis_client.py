"""
trellis_client.py
-----------------
TRELLIS 2 Image-to-3D backend (fal.ai).

Thin wrapper over FalMeshBackend — only the endpoint, output key,
and defaults differ. Keeps the same generate_3d(image, out) entry
point as before so callers don't change.

Usage:
    export FAL_KEY=...
    python trellis_client.py /path/to/input.png /path/to/output.glb

    from trellis_client import generate_3d
    generate_3d("styled.png", "mesh.glb", resolution="1536", texture_size="2048")
"""

import sys
import json

from fal_base import FalMeshBackend


class TrellisBackend(FalMeshBackend):
    MODEL_ID = "fal-ai/trellis-2"
    OUTPUT_KEY = "model_glb"
    TAG = "trellis"

    def default_args(self):
        return {
            "resolution": "1024",     # 512 | 1024 | 1536  (geometry detail)
            "texture_size": "1024",   # 1024 | 2048 | 4096  (texture map)
        }


# module-level entry point (backwards compatible)
def generate_3d(image_path, output_glb_path, verbose=True, **params):
    return TrellisBackend().generate_3d(
        image_path, output_glb_path, verbose=verbose, **params
    )


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python trellis_client.py <input_image> <output.glb>")
        sys.exit(1)
    res = generate_3d(sys.argv[1], sys.argv[2])
    print(json.dumps({
        "request_id": res["request_id"],
        "glb_path": res["glb_path"],
    }, indent=2))
