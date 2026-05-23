"""
gemini_text_client.py
---------------------
Thin stdlib-only Gemini text client for Asset Forge prompt generation.

Given an asset name + keyword dict, asks Gemini to write a rich image-gen
prompt that preserves the asset silhouette and applies the scene context.

Usage:
    export GEMINI_API_KEY=...
    python gemini_text_client.py "barrel-food" '{"place": "Malta valletta harbour", "time": "1800"}'

Or from Python / Houdini:
    from gemini_text_client import build_prompt
    prompt = build_prompt("barrel-food", {"place": "Malta valletta harbour", "time": "1800"})
"""

import os
import ssl
import json
import urllib.request
import urllib.error

_CA_CANDIDATES = [
    os.environ.get("SSL_CERT_FILE"),
    "/etc/ssl/certs/ca-certificates.crt",
    "/etc/pki/tls/certs/ca-bundle.crt",
    "/etc/ssl/cert.pem",
]

API_BASE = "https://generativelanguage.googleapis.com/v1beta"
MODEL = "gemini-2.5-flash"

SYSTEM_PROMPT = (
    "You are a prompt engineer for AI image generation. "
    "Given an asset name and scene context keywords, write a single concise "
    "image generation prompt. Rules:\n"
    "- Keep the silhouette and shape of the asset exactly as-is\n"
    "- Focus on visual descriptors: materials, textures, surface wear, lighting\n"
    "- Apply the time period and place to the style and materials\n"
    "- Return ONLY the prompt string, no explanation, no quotes"
)


def _ssl_context():
    for path in _CA_CANDIDATES:
        if path and os.path.isfile(path):
            return ssl.create_default_context(cafile=path)
    return ssl.create_default_context()


def _api_key():
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise RuntimeError(
            "GEMINI_API_KEY not set. Export it before calling this module."
        )
    return key


def _post(url, body):
    data = json.dumps(body).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "x-goog-api-key": _api_key(),
    }
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30, context=_ssl_context()) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            err_body = e.read().decode("utf-8")
        except Exception:
            err_body = ""
        raise RuntimeError(f"Gemini API -> {e.code}: {err_body}") from e


def call(user_message, model=None):
    """Send a pre-built message string to Gemini. Returns the text response."""
    body = {
        "system_instruction": {"parts": [{"text": "You are a prompt engineer for AI image generation. Return only the prompt string, no explanation, no quotes."}]},
        "contents": [{"role": "user", "parts": [{"text": user_message}]}],
        "generationConfig": {
            "temperature": 0.7,
            "maxOutputTokens": 8192,
            "thinkingConfig": {"thinkingBudget": 0},
        },
    }
    url = f"{API_BASE}/models/{model or MODEL}:generateContent"
    resp = _post(url, body)
    try:
        candidate = resp["candidates"][0]
        finish = candidate.get("finishReason", "?")
        text = candidate["content"]["parts"][0]["text"].strip()
        if finish != "STOP":
            print(f"[gemini] finishReason={finish} — response may be truncated")
        return text
    except (KeyError, IndexError) as e:
        raise RuntimeError(f"Unexpected response: {json.dumps(resp)[:300]}") from e


def build_prompt(name, keywords):
    """
    name     : asset name string, e.g. "barrel-food"
    keywords : dict of scene context, e.g. {"place": "Malta valletta harbour", "time": "1800"}

    Returns a single enriched prompt string for image gen.
    """
    kw_str = ", ".join(f"{k}: {v}" for k, v in keywords.items())
    user_message = f"Asset: {name}\nScene context: {kw_str}"

    body = {
        "system_instruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": [{"role": "user", "parts": [{"text": user_message}]}],
        "generationConfig": {
            "temperature": 0.7,
            "maxOutputTokens": 8192,
            "thinkingConfig": {"thinkingBudget": 0},
        },
    }

    url = f"{API_BASE}/models/{MODEL}:generateContent"
    resp = _post(url, body)

    try:
        return resp["candidates"][0]["content"]["parts"][0]["text"].strip()
    except (KeyError, IndexError) as e:
        raise RuntimeError(f"Unexpected response shape: {json.dumps(resp)[:300]}") from e


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 3:
        print("Usage: python gemini_text_client.py <asset_name> '<keywords_json>'")
        sys.exit(1)
    name = sys.argv[1]
    keywords = json.loads(sys.argv[2])
    print(build_prompt(name, keywords))
