"""
fal_base.py
-----------
Shared base for all fal.ai image-to-3D backends.

Holds everything the Trellis / Pixal3D / future clients have in common:
  - FAL_KEY check
  - local image upload to fal's CDN
  - task submit + poll (via fal_client.subscribe)
  - GLB download to disk

A concrete backend subclasses FalMeshBackend and sets three things:
  - MODEL_ID   : the fal endpoint, e.g. "fal-ai/trellis-2"
  - OUTPUT_KEY : where the GLB url lives in the result, e.g. "model_glb"
  - default_args() : per-model default arguments (optional)

Then it inherits generate_3d() unchanged.
"""

import os
import ssl
import urllib.request


# Houdini's bundled Python on Linux doesn't find a system trust store on its
# own → urllib hits CERTIFICATE_VERIFY_FAILED. Same workaround as
# meshy_client.py / nano_banana_client.py.
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


class FalMeshBackend:
    # --- subclasses override these ---
    MODEL_ID = None          # e.g. "fal-ai/trellis-2"
    OUTPUT_KEY = "model_glb"  # where the GLB url sits in the result
    TAG = "fal"               # log prefix

    def default_args(self):
        """Per-model default arguments. Subclasses override."""
        return {}

    # --- shared machinery below ---

    def _log(self, msg):
        if self.verbose:
            print(f"[{self.TAG}] {msg}")

    @staticmethod
    def _check_key():
        if not os.environ.get("FAL_KEY"):
            raise RuntimeError(
                "FAL_KEY environment variable not set. "
                "Get a key from https://fal.ai/dashboard/keys "
                "and set it before calling this module."
            )

    @staticmethod
    def _download(url, dest_path):
        """Stream a file to disk. Creates parent dirs if needed."""
        os.makedirs(os.path.dirname(os.path.abspath(dest_path)), exist_ok=True)
        with urllib.request.urlopen(url, timeout=300, context=_ssl_context()) as resp, \
                open(dest_path, "wb") as out:
            while True:
                chunk = resp.read(1024 * 64)
                if not chunk:
                    break
                out.write(chunk)

    def generate_3d(self, image_path, output_glb_path, verbose=True, **params):
        """
        End-to-end: image path in, GLB on disk out.

        params override this backend's default_args() and are passed
        straight through to the fal endpoint.

        Returns:
            {
              "request_id": str,
              "glb_path": str,
              "result": dict,
            }
        """
        self.verbose = verbose

        if not self.MODEL_ID:
            raise NotImplementedError("Subclass must set MODEL_ID")

        self._check_key()

        if not os.path.isfile(image_path):
            raise FileNotFoundError(image_path)

        # Lazy import so `import fal_base` doesn't explode when the pip dep
        # isn't installed — error surfaces at call time with project context.
        import fal_client

        self._log(f"uploading {image_path}")
        image_url = fal_client.upload_file(image_path)

        def _on_update(update):
            if self.verbose and isinstance(update, fal_client.InProgress):
                for log in update.logs:
                    print(f"[{self.TAG}] {log['message']}")

        arguments = {"image_url": image_url}
        arguments.update(self.default_args())
        arguments.update(params)

        self._log("submitting task")
        result = fal_client.subscribe(
            self.MODEL_ID,
            arguments=arguments,
            with_logs=verbose,
            on_queue_update=_on_update,
        )

        glb_url = (result.get(self.OUTPUT_KEY) or {}).get("url")
        if not glb_url:
            raise RuntimeError(
                f"{self.MODEL_ID} succeeded but no GLB url at "
                f"'{self.OUTPUT_KEY}': {result}"
            )

        self._log(f"downloading GLB -> {output_glb_path}")
        self._download(glb_url, output_glb_path)

        return {
            "request_id": result.get("request_id", ""),
            "glb_path": output_glb_path,
            "result": result,
        }
