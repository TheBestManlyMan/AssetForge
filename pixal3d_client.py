"""
pixal3d_client.py
-----------------
Pixal3D Image-to-3D backend (fal.ai).

Thin wrapper over FalMeshBackend. Accepts JPG, PNG, WebP, GIF, AVIF.

Usage:
    export FAL_KEY=...
    python pixal3d_client.py /path/to/input.png /path/to/output.glb

    from pixal3d_client import generate_3d
    generate_3d("styled.png", "mesh.glb", resolution="2048")
"""

import sys
import json

from fal_base import FalMeshBackend


class Pixal3DBackend(FalMeshBackend):
    MODEL_ID = "fal-ai/pixal3d"
    OUTPUT_KEY = "model_glb"
    TAG = "pixal3d"

    def default_args(self):
        return {
            "resolution": "1024",     # 1024 | 1536  (geometry detail)
            "texture_size": "2048",   # 1024 | 2048 | 4096  (texture map)
        }


# module-level entry point (backwards compatible)
def generate_3d(image_path, output_glb_path, verbose=True, **params):
    return Pixal3DBackend().generate_3d(
        image_path, output_glb_path, verbose=verbose, **params
    )


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python pixal3d_client.py <input_image> <output.glb>")
        sys.exit(1)
    res = generate_3d(sys.argv[1], sys.argv[2])
    print(json.dumps({
        "request_id": res["request_id"],
        "glb_path": res["glb_path"],
    }, indent=2))
