"""
glb_webp_to_png.py
------------------
Convert EXT_texture_webp GLBs to plain glTF PNG so Houdini can load them.

Pixal3D / some fal backends return GLBs whose textures are WebP, declared via
the EXT_texture_webp extension and listed in extensionsRequired. Houdini's glTF
loader refuses any GLB that *requires* an extension it doesn't support, so the
whole mesh fails to import ("unsupported extension EXT_texture_webp required by
the file"). PIL in Houdini's bundled Python has no WebP support either, so we
convert each WebP image to PNG with an ffmpeg subprocess, rebuild the binary
buffer with recomputed (4-byte-aligned) bufferView offsets, drop the extension,
and rewrite the GLB in place.

Stdlib + ffmpeg subprocess only — runs in Houdini's bundled Python.

    from glb_webp_to_png import convert
    convert("mesh.glb")   # rewrites in place if it has webp; no-op otherwise
"""

import os
import sys
import json
import base64
import struct
import tempfile
import subprocess

_MAGIC      = 0x46546C67   # "glTF"
_CHUNK_JSON = 0x4E4F534A
_CHUNK_BIN  = 0x004E4942
_EXT_NAME   = "EXT_texture_webp"


def _parse_glb(data):
    """Return (gltf_dict, bin_bytes)."""
    magic, version, total_len = struct.unpack_from("<III", data, 0)
    if magic != _MAGIC:
        raise ValueError("Not a GLB file (bad magic)")
    if version != 2:
        raise ValueError("Unsupported GLB version: {}".format(version))

    offset = 12
    gltf = None
    bin_data = b""
    while offset < total_len and offset + 8 <= len(data):
        chunk_len, chunk_type = struct.unpack_from("<II", data, offset)
        offset += 8
        chunk = data[offset:offset + chunk_len]
        offset += chunk_len
        if chunk_type == _CHUNK_JSON:
            gltf = json.loads(chunk.decode("utf-8").rstrip("\x00"))
        elif chunk_type == _CHUNK_BIN:
            bin_data = chunk
    if gltf is None:
        raise ValueError("No JSON chunk in GLB")
    return gltf, bin_data


def _webp_to_png(webp_bytes):
    """Convert WebP bytes -> PNG bytes via ffmpeg (PIL lacks webp in Houdini)."""
    with tempfile.TemporaryDirectory() as td:
        ip = os.path.join(td, "in.webp")
        op = os.path.join(td, "out.png")
        with open(ip, "wb") as f:
            f.write(webp_bytes)
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", ip, op],
                       check=True)
        with open(op, "rb") as f:
            return f.read()


def _pad4(b, fill):
    return b + fill * ((-len(b)) % 4)


def convert(glb_path, verbose=True):
    """
    If glb_path uses EXT_texture_webp (or has image/webp images), convert every
    WebP image to PNG and rewrite the GLB in place. No-op (returns False) for
    GLBs that don't use WebP. Returns True when a conversion happened.
    """
    def _log(m):
        if verbose:
            print("[glb_webp] " + m)

    with open(glb_path, "rb") as f:
        data = f.read()
    gltf, bin_data = _parse_glb(data)

    images       = gltf.get("images") or []
    buffer_views = gltf.get("bufferViews") or []
    used         = set(gltf.get("extensionsUsed", []))

    if _EXT_NAME not in used and not any(
            img.get("mimeType") == "image/webp" for img in images):
        return False

    # 1) Convert each WebP image to PNG. For bufferView-backed images remember
    #    the new bytes keyed by bufferView index; data-URI images are inlined.
    new_bytes_for_bv = {}
    n_converted = 0
    for img in images:
        if img.get("mimeType") != "image/webp":
            continue
        bv_idx = img.get("bufferView")
        if bv_idx is not None:
            bv     = buffer_views[bv_idx]
            start  = bv.get("byteOffset", 0)
            length = bv["byteLength"]
            new_bytes_for_bv[bv_idx] = _webp_to_png(bin_data[start:start + length])
        elif str(img.get("uri", "")).startswith("data:"):
            webp = base64.b64decode(img["uri"].split(",", 1)[1])
            png  = _webp_to_png(webp)
            img["uri"] = "data:image/png;base64," + base64.b64encode(png).decode("ascii")
        img["mimeType"] = "image/png"
        n_converted += 1

    # 2) Rebuild the binary buffer: copy each bufferView's bytes (substituting
    #    converted PNGs), recomputing 4-byte-aligned offsets as we go.
    new_buf = bytearray()
    for i, bv in enumerate(buffer_views):
        if i in new_bytes_for_bv:
            blob = new_bytes_for_bv[i]
        else:
            start = bv.get("byteOffset", 0)
            blob  = bin_data[start:start + bv["byteLength"]]
        new_buf.extend(b"\x00" * ((-len(new_buf)) % 4))   # align to 4
        bv["byteOffset"] = len(new_buf)
        bv["byteLength"] = len(blob)
        new_buf.extend(blob)
    new_buf = bytes(new_buf)

    if gltf.get("buffers"):
        gltf["buffers"][0]["byteLength"] = len(new_buf)

    # 3) Drop EXT_texture_webp: point each texture at its (now-PNG) image and
    #    remove the extension declarations.
    for tex in gltf.get("textures", []):
        exts = tex.get("extensions") or {}
        if _EXT_NAME in exts:
            src = exts[_EXT_NAME].get("source")
            if src is not None:
                tex["source"] = src
            del exts[_EXT_NAME]
            if exts:
                tex["extensions"] = exts
            else:
                tex.pop("extensions", None)
    for key in ("extensionsUsed", "extensionsRequired"):
        if key in gltf:
            kept = [e for e in gltf[key] if e != _EXT_NAME]
            if kept:
                gltf[key] = kept
            else:
                del gltf[key]

    # 4) Re-serialize the GLB (JSON chunk space-padded, BIN chunk zero-padded).
    json_bytes = _pad4(json.dumps(gltf, separators=(",", ":")).encode("utf-8"), b" ")
    bin_bytes  = _pad4(new_buf, b"\x00")
    total = 12 + 8 + len(json_bytes) + 8 + len(bin_bytes)

    out = bytearray()
    out.extend(struct.pack("<III", _MAGIC, 2, total))
    out.extend(struct.pack("<II", len(json_bytes), _CHUNK_JSON))
    out.extend(json_bytes)
    out.extend(struct.pack("<II", len(bin_bytes), _CHUNK_BIN))
    out.extend(bin_bytes)
    with open(glb_path, "wb") as f:
        f.write(out)

    _log("converted {} webp image(s) -> png: {}".format(n_converted, glb_path))
    return True


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python glb_webp_to_png.py <mesh.glb>")
        sys.exit(1)
    converted = convert(sys.argv[1])
    print("converted" if converted else "no webp — unchanged")
