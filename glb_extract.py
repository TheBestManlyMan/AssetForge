"""
glb_extract.py
--------------
Extract embedded textures from a GLB file. Stdlib only.

GLB binary layout:
  [12-byte header] [JSON chunk] [BIN chunk]
  header  : magic(4) + version(4) + length(4)
  chunk   : chunk_length(4) + chunk_type(4) + chunk_data(chunk_length)
  JSON type = 0x4E4F534A
  BIN  type = 0x004E4942

Images in the GLTF JSON either reference:
  - a bufferView into the BIN chunk  (most common for GLB)
  - a data URI (base64-encoded inline)

Usage:
    python glb_extract.py mesh.glb ./textures/
    # writes albedo.png, normal.png, etc. (or image_0.png, image_1.png …)

From another module:
    from glb_extract import extract
    paths = extract("mesh.glb", out_dir)
    # returns {"albedo": "/path/albedo.png", "normal": "/path/normal.png", ...}
"""

import os
import sys
import json
import base64
import struct
import mimetypes
from PIL import Image


# ---------------------------------------------------------------------------
# GLB parsing
# ---------------------------------------------------------------------------

_MAGIC   = 0x46546C67   # "glTF"
_CHUNK_JSON = 0x4E4F534A
_CHUNK_BIN  = 0x004E4942

_EXT = {
    "image/png":  ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
}

# Meshy's texture name patterns → canonical slot names
_SLOT_KEYWORDS = {
    "albedo":    ["albedo", "basecolor", "base_color", "diffuse", "color"],
    "normal":    ["normal", "nrm", "nmap"],
    "roughness": ["roughness", "rough"],
    "metallic":  ["metallic", "metal"],
    "occlusion": ["occlusion", "ao"],
    "emissive":  ["emissive", "emit"],
}


def _slot_name(raw_name, index):
    """Map a GLTF image name to a canonical slot name, or fall back to image_{i}."""
    low = (raw_name or "").lower()
    for slot, keywords in _SLOT_KEYWORDS.items():
        if any(k in low for k in keywords):
            return slot
    return "image_{}".format(index)


def _build_material_slot_map(gltf):
    """
    Return {image_index: slot_name} by reading GLTF material assignments.
    This is authoritative — image names from Meshy are not reliable.
    """
    textures = gltf.get("textures") or []
    materials = gltf.get("materials") or []
    slot_map = {}

    def _assign(tex_ref, slot):
        if tex_ref is None:
            return
        idx = tex_ref.get("index")
        if idx is None:
            return
        src = (textures[idx] if idx < len(textures) else {}).get("source")
        if src is not None:
            slot_map.setdefault(src, slot)

    for mat in materials:
        pbr = mat.get("pbrMetallicRoughness") or {}
        _assign(pbr.get("baseColorTexture"),          "albedo")
        _assign(pbr.get("metallicRoughnessTexture"),  "metallic_roughness")
        _assign(mat.get("normalTexture"),              "normal")
        _assign(mat.get("occlusionTexture"),           "occlusion")
        _assign(mat.get("emissiveTexture"),            "emissive")

    return slot_map


def _parse_glb(glb_path):
    """Return (gltf_dict, bin_bytes). bin_bytes may be None if there's no BIN chunk."""
    with open(glb_path, "rb") as f:
        data = f.read()

    magic, version, total_len = struct.unpack_from("<III", data, 0)
    if magic != _MAGIC:
        raise ValueError("Not a GLB file (bad magic): {}".format(glb_path))
    if version != 2:
        raise ValueError("Unsupported GLB version: {}".format(version))

    offset = 12
    gltf = None
    bin_data = None

    while offset < total_len:
        if offset + 8 > len(data):
            break
        chunk_len, chunk_type = struct.unpack_from("<II", data, offset)
        offset += 8
        chunk_data = data[offset: offset + chunk_len]
        offset += chunk_len

        if chunk_type == _CHUNK_JSON:
            gltf = json.loads(chunk_data.decode("utf-8").rstrip("\x00"))
        elif chunk_type == _CHUNK_BIN:
            bin_data = chunk_data

    if gltf is None:
        raise ValueError("No JSON chunk found in GLB: {}".format(glb_path))

    return gltf, bin_data


# ---------------------------------------------------------------------------
# Texture unpacking
# ---------------------------------------------------------------------------

def _split_metallic_roughness(mr_path, out_dir):
    """Unpack a glTF metallic-roughness PNG into separate grayscale images.
    G channel → roughness.png  (0-255 = 0.0-1.0)
    B channel → metallic.png   (0-255 = 0.0-1.0)
    Returns (roughness_path, metallic_path).
    """
    img = Image.open(mr_path).convert("RGB")
    _, g, b = img.split()
    roughness_path = os.path.join(out_dir, "roughness.png")
    metallic_path  = os.path.join(out_dir, "metallic.png")
    g.save(roughness_path)
    b.save(metallic_path)
    return roughness_path, metallic_path


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def extract(glb_path, out_dir, verbose=True):
    """
    Extract all embedded images from glb_path and write them to out_dir.

    Returns a dict mapping slot name → absolute file path.
    e.g. {"albedo": "/path/albedo.png", "normal": "/path/normal.png"}
    """
    def _log(msg):
        if verbose:
            print("[glb_extract] " + msg)

    gltf, bin_data = _parse_glb(glb_path)
    images       = gltf.get("images") or []
    buffer_views = gltf.get("bufferViews") or []
    slot_map     = _build_material_slot_map(gltf)  # image_index -> slot name

    os.makedirs(out_dir, exist_ok=True)
    result = {}

    for i, img in enumerate(images):
        raw_name  = img.get("name", "")
        mime_type = img.get("mimeType", "image/png")
        ext       = _EXT.get(mime_type, ".png")
        # Material assignment is authoritative; name keywords are fallback
        slot      = slot_map.get(i) or _slot_name(raw_name, i)
        out_path  = os.path.join(out_dir, slot + ext)

        if "bufferView" in img:
            # image bytes live in the BIN chunk
            if bin_data is None:
                raise RuntimeError("Image {} references a bufferView but GLB has no BIN chunk".format(i))
            bv     = buffer_views[img["bufferView"]]
            start  = bv.get("byteOffset", 0)
            length = bv["byteLength"]
            img_bytes = bin_data[start: start + length]

        elif "uri" in img:
            uri = img["uri"]
            if uri.startswith("data:"):
                # data URI: data:<mime>;base64,<b64data>
                header, b64 = uri.split(",", 1)
                img_bytes = base64.b64decode(b64)
            else:
                # external file reference — resolve relative to GLB
                ext_path = os.path.join(os.path.dirname(glb_path), uri)
                with open(ext_path, "rb") as f:
                    img_bytes = f.read()
        else:
            _log("image {} has no uri or bufferView, skipping".format(i))
            continue

        with open(out_path, "wb") as f:
            f.write(img_bytes)

        _log("wrote {} ({} bytes) -> {}".format(slot, len(img_bytes), out_path))
        result[slot] = out_path

    if "metallic_roughness" in result:
        mr_path = result.pop("metallic_roughness")
        rough_path, metal_path = _split_metallic_roughness(mr_path, out_dir)
        result["roughness"] = rough_path
        result["metallic"]  = metal_path
        _log("split metallic_roughness -> roughness.png + metallic.png")

    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python glb_extract.py <mesh.glb> [out_dir]")
        sys.exit(1)

    glb   = sys.argv[1]
    out   = sys.argv[2] if len(sys.argv) > 2 else os.path.join(os.path.dirname(glb), "textures")
    paths = extract(glb, out)
    print(json.dumps(paths, indent=2))
