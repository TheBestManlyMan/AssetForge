"""
nano_banana_client.py
---------------------
Standalone Nano Banana Pro (Gemini 3 Pro Image) style-pass client.

Stdlib only -- no `google-genai`, no `requests`. Runs in Houdini's Python as-is,
same approach as meshy_client.py.

Single entry point: generate_image(input_image_path, output_png_path, prompt, ref_image_path=None)
- Reads a local preview image, base64-encodes it, sends it + a prompt to Gemini
- Optionally includes a master style reference image
- Writes the returned styled image to the requested path
- Returns a small dict with the model used and the saved path

Usage:
    export GEMINI_API_KEY=...
    python nano_banana_client.py preview.jpg generated.png "weathered granite boulder, mossy"

Or from another Python tool (Houdini, etc.):
    from nano_banana_client import generate_image
    result = generate_image("preview.jpg", "generated.png",
                            "weathered granite boulder, mossy",
                            ref_image_path="style_ref.png")
"""

import os
import sys
import ssl
import json
import base64
import mimetypes
import urllib.request
import urllib.error


# Houdini's bundled Python doesn't pick up the OS CA bundle on Linux. Point at
# a system trust store explicitly so HTTPS verifies. Override with SSL_CERT_FILE
# if your distro keeps certs somewhere unusual.
_CA_CANDIDATES = [
    os.environ.get("SSL_CERT_FILE"),
    "/etc/ssl/certs/ca-certificates.crt",   # Debian/Ubuntu
    "/etc/pki/tls/certs/ca-bundle.crt",     # RHEL/Fedora
    "/etc/ssl/cert.pem",                    # Alpine/macOS-style
]


def _ssl_context():
    for path in _CA_CANDIDATES:
        if path and os.path.isfile(path):
            return ssl.create_default_context(cafile=path)
    # Last resort: stdlib default (works if Python finds certs on its own)
    return ssl.create_default_context()


API_BASE = "https://generativelanguage.googleapis.com/v1beta"

# Nano Banana Pro = "gemini-3-pro-image-preview" (paid tier only).
# Using the non-Pro model for free-tier dry-runs; flip back once billing is on.
MODEL = "gemini-2.5-flash-image"


# ---------------------------------------------------------------------------
# Low-level helpers (stdlib only)
# ---------------------------------------------------------------------------

def _api_key():
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise RuntimeError(
            "GEMINI_API_KEY environment variable not set. "
            "Get a key from https://aistudio.google.com (Get API key) "
            "and set it before calling this module."
        )
    return key


def _image_part(image_path):
    """Read a local image and return an inline_data part dict."""
    mime, _ = mimetypes.guess_type(image_path)
    if mime not in ("image/jpeg", "image/png"):
        raise ValueError(
            f"Unsupported image type: {mime}. Use .jpg, .jpeg, or .png."
        )
    with open(image_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("ascii")
    return {"inline_data": {"mime_type": mime, "data": b64}}


def _post(url, body):
    """JSON POST. Returns parsed dict. Raises with Gemini's message on error."""
    data = json.dumps(body).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "x-goog-api-key": _api_key(),
    }
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=300, context=_ssl_context()) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            err_body = e.read().decode("utf-8")
        except Exception:
            err_body = ""
        raise RuntimeError(f"Gemini API POST -> {e.code}: {err_body}") from e


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate_image(input_image_path, output_png_path, prompt,
                   ref_image_path=None, aspect_ratio=None, verbose=True):
    """
    End-to-end: preview image + prompt in, styled PNG on disk out.

    input_image_path : the placeholder/preview render to restyle
    output_png_path  : where to write the styled result
    prompt           : the LLM-refined generation prompt
    ref_image_path   : optional master style reference image
    aspect_ratio     : optional, e.g. "1:1", "16:9". Omit to let the model decide.

    Returns:
        {"model": str, "image_path": str}
    """
    def _log(msg):
        if verbose:
            print("[nano] " + msg)

    if not os.path.isfile(input_image_path):
        raise FileNotFoundError(input_image_path)

    # parts = preview image, optional style ref, then the text prompt
    parts = [_image_part(input_image_path)]
    if ref_image_path:
        if not os.path.isfile(ref_image_path):
            raise FileNotFoundError(ref_image_path)
        parts.append(_image_part(ref_image_path))
    parts.append({"text": prompt})

    generation_config = {"responseModalities": ["IMAGE"]}
    if aspect_ratio:
        generation_config["imageConfig"] = {"aspectRatio": aspect_ratio}

    body = {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": generation_config,
    }

    url = API_BASE + "/models/" + MODEL + ":generateContent"

    _log("submitting " + input_image_path)
    resp = _post(url, body)

    # Find the first inline image in the response and write it out
    candidates = resp.get("candidates") or []
    for cand in candidates:
        for part in (cand.get("content", {}).get("parts") or []):
            inline = part.get("inlineData") or part.get("inline_data")
            if inline and inline.get("data"):
                os.makedirs(os.path.dirname(os.path.abspath(output_png_path)),
                            exist_ok=True)
                with open(output_png_path, "wb") as out:
                    out.write(base64.b64decode(inline["data"]))
                _log("wrote " + output_png_path)
                return {"model": MODEL, "image_path": output_png_path}

    # No image came back -- surface any text returned to help debug
    raise RuntimeError(
        "Nano Banana returned no image. Raw response: " + json.dumps(resp)[:500]
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if len(sys.argv) < 4:
        print("Usage: python nano_banana_client.py <input_image> <output.png> <prompt> [ref_image]")
        sys.exit(1)
    ref = sys.argv[4] if len(sys.argv) > 4 else None
    result = generate_image(sys.argv[1], sys.argv[2], sys.argv[3], ref_image_path=ref)
    print(json.dumps(result, indent=2))
