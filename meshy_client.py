"""
meshy_client.py
---------------
Stdlib-only Meshy image-to-3D client. Template for all mesh-gen backends.

Runs inside Houdini's bundled Python with zero pip dependencies.
API ref: https://docs.meshy.ai/api-image-to-3d

Single entry point:
    generate(input_image_path, out_glb_path, **opts) -> str (out_glb_path)

Usage:
    export MESHY_API_KEY=...
    python meshy_client.py preview.jpg mesh.glb

Or from Houdini Python TOP:
    from meshy_client import generate
    generate("/path/to/generated.png", "/path/to/mesh.glb")
"""

import os
import sys
import ssl
import json
import time
import base64
import mimetypes
import urllib.request
import urllib.error


# Same CA-bundle strategy as nano_banana_client.py — Houdini's Python on Linux
# doesn't find a system trust store on its own.
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
    return ssl.create_default_context()


API_BASE = "https://api.meshy.ai/openapi/v1"


def _api_key():
    key = os.environ.get("MESHY_API_KEY")
    if not key:
        raise RuntimeError(
            "MESHY_API_KEY environment variable not set. "
            "Get a key from https://app.meshy.ai/settings/api-keys "
            "and set it before calling this module."
        )
    return key


def _auth_headers():
    return {
        "Content-Type": "application/json",
        "Authorization": "Bearer " + _api_key(),
    }


def _post(endpoint, body):
    url = API_BASE + endpoint
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=_auth_headers(), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60, context=_ssl_context()) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            err_body = e.read().decode("utf-8")
        except Exception:
            err_body = ""
        raise RuntimeError("Meshy POST {} -> {}: {}".format(endpoint, e.code, err_body)) from e


def _get(endpoint):
    url = API_BASE + endpoint
    req = urllib.request.Request(url, headers=_auth_headers(), method="GET")
    try:
        with urllib.request.urlopen(req, timeout=60, context=_ssl_context()) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            err_body = e.read().decode("utf-8")
        except Exception:
            err_body = ""
        raise RuntimeError("Meshy GET {} -> {}: {}".format(endpoint, e.code, err_body)) from e


def _download(url, out_path):
    """Stream-download a URL to a file. GLB URLs from Meshy are pre-signed S3."""
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=300, context=_ssl_context()) as resp:
        os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
        with open(out_path, "wb") as f:
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                f.write(chunk)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def submit(input_image_path,
           topology="quad", target_polycount=30000, enable_pbr=True,
           verbose=True):
    """Submit an image-to-3D task. Returns task_id immediately."""
    def _log(msg):
        if verbose: print("[meshy] " + msg)

    if not os.path.isfile(input_image_path):
        raise FileNotFoundError(input_image_path)

    mime, _ = mimetypes.guess_type(input_image_path)
    if mime not in ("image/jpeg", "image/png"):
        raise ValueError("Unsupported image type: {}".format(mime))
    with open(input_image_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("ascii")
    image_url = "data:{};base64,{}".format(mime, b64)

    body = {
        "image_url": image_url,
        "enable_pbr": enable_pbr,
        "should_remesh": True,
        "topology": topology,
        "target_polycount": target_polycount,
    }
    _log("submitting {} ...".format(os.path.basename(input_image_path)))
    resp = _post("/image-to-3d", body)
    task_id = resp.get("result")
    if not task_id:
        raise RuntimeError("No task_id in Meshy response: {}".format(resp))
    _log("task_id: {}".format(task_id))
    return task_id


def poll_all(tasks, poll_interval=10, timeout=600, verbose=True):
    """
    Poll multiple tasks to completion in a single shared loop.

    tasks : list of (task_id, out_glb_path) tuples
    Returns list of out_glb_paths in the same order.
    """
    def _log(msg):
        if verbose: print("[meshy] " + msg)

    pending = {task_id: out_path for task_id, out_path in tasks}
    deadline = time.time() + timeout

    while pending and time.time() < deadline:
        for task_id in list(pending.keys()):
            task     = _get("/image-to-3d/{}".format(task_id))
            status   = task.get("status", "UNKNOWN")
            progress = task.get("progress", 0)
            _log("{} status={} progress={}%".format(task_id[:8], status, progress))

            if status == "SUCCEEDED":
                glb_url = (task.get("model_urls") or {}).get("glb")
                if not glb_url:
                    raise RuntimeError("SUCCEEDED but no glb URL: {}".format(task))
                out_path = pending.pop(task_id)
                _log("downloading GLB -> {}".format(out_path))
                _download(glb_url, out_path)
                _log("done: {}".format(out_path))

            elif status in ("FAILED", "EXPIRED"):
                raise RuntimeError("Meshy task {}: {}".format(
                    status, task.get("task_error") or task))

        if pending:
            time.sleep(poll_interval)

    if pending:
        raise RuntimeError("Meshy timed out after {}s, still pending: {}".format(
            timeout, list(pending.keys())))

    return [out_path for _, out_path in tasks]


def generate(input_image_path, out_glb_path,
             topology="quad", target_polycount=30000, enable_pbr=True,
             poll_interval=10, timeout=600, verbose=True):
    """
    Submit image-to-3D task, poll until SUCCEEDED, download GLB.

    input_image_path : local .jpg or .png — the styled generated image
    out_glb_path     : where to write the GLB
    topology         : "quad" (default) or "triangle"
    target_polycount : target triangle/quad count (default 30 000)
    enable_pbr       : generate PBR textures (default True)
    poll_interval    : seconds between status checks (default 10)
    timeout          : max seconds to wait (default 600 = 10 min)

    Returns out_glb_path on success.
    """
    def _log(msg):
        if verbose:
            print("[meshy] " + msg)

    if not os.path.isfile(input_image_path):
        raise FileNotFoundError(input_image_path)

    # Base64-encode the image as a data URI — Meshy accepts both URLs and data URIs
    mime, _ = mimetypes.guess_type(input_image_path)
    if mime not in ("image/jpeg", "image/png"):
        raise ValueError("Unsupported image type: {}. Use .jpg or .png.".format(mime))
    with open(input_image_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("ascii")
    image_url = "data:{};base64,{}".format(mime, b64)

    # Submit
    body = {
        "image_url": image_url,
        "enable_pbr": enable_pbr,
        "should_remesh": True,
        "topology": topology,
        "target_polycount": target_polycount,
    }
    _log("submitting {} …".format(os.path.basename(input_image_path)))
    resp = _post("/image-to-3d", body)
    task_id = resp.get("result")
    if not task_id:
        raise RuntimeError("No task_id in Meshy response: {}".format(resp))
    _log("task_id: {}".format(task_id))

    # Poll until terminal state
    deadline = time.time() + timeout
    while time.time() < deadline:
        task = _get("/image-to-3d/{}".format(task_id))
        status   = task.get("status", "UNKNOWN")
        progress = task.get("progress", 0)
        _log("status={} progress={}%".format(status, progress))

        if status == "SUCCEEDED":
            glb_url = (task.get("model_urls") or {}).get("glb")
            if not glb_url:
                raise RuntimeError("SUCCEEDED but no glb URL in task: {}".format(task))
            _log("downloading GLB → {}".format(out_glb_path))
            _download(glb_url, out_glb_path)
            _log("done: {}".format(out_glb_path))
            return out_glb_path

        if status in ("FAILED", "EXPIRED"):
            raise RuntimeError(
                "Meshy task {}: {}".format(status, task.get("task_error") or task)
            )

        time.sleep(poll_interval)

    raise RuntimeError(
        "Meshy task timed out after {}s (task_id={})".format(timeout, task_id)
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python meshy_client.py <input_image> <out.glb>")
        sys.exit(1)
    out = generate(sys.argv[1], sys.argv[2])
    print("Written:", out)
