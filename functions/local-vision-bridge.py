"""
title: Local Vision Bridge
author: feliscat
version: 1.1.0
description: Intercepts image attachments, describes them with a local vision model,
  and replaces the attachment with structured text before any other filter runs.

Original source: https://github.com/feliscat/local-vision-bridge
Modifications for this deployment (macos-local-ai-stack):
  - inlet made async; parameter renamed to __user__ (required by Open WebUI 0.9.5+)
  - Default vision_server_url set to Ollama's local OpenAI-compatible endpoint
  - Default vision_model set to llama3.2-vision:11b
  - Default vision_prompt updated: extracts text verbatim AND describes the image
  - Image detection hardened to handle all content formats Open WebUI produces:
      • Inline base64 data-URL  (clipboard paste)
      • type="image_url" with URL pointing to the server  (file-picker upload)
      • type="file" items with an image MIME type  (file-picker in newer builds)
      • type="image" with a "source" dict  (Anthropic-style content)
      • Legacy Ollama "images" field on the message
  - Server-hosted file references are fetched over localhost HTTP
  - MIME type is detected from the base64 magic bytes so it survives PNG pastes
  - Request timeout extended to 90 s to accommodate first cold-load of the vision model
"""

import base64
import hashlib
import urllib.request
from typing import Any, Awaitable, Callable, Optional

import requests
from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# MIME-type detection from raw base64 data
# ---------------------------------------------------------------------------

def _detect_mime(b64: str) -> str:
    try:
        header = base64.b64decode(b64[:16] + "==")[:8]
    except Exception:
        return "image/jpeg"
    if header[:4] == b"\x89PNG":
        return "image/png"
    if header[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if header[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if header[:4] == b"RIFF":
        return "image/webp"
    return "image/jpeg"


# ---------------------------------------------------------------------------
# Fetch a server-hosted image and return it as base64
# ---------------------------------------------------------------------------

OPEN_WEBUI_BASE = "http://127.0.0.1:8080"


def _fetch_b64(url: str, debug: bool = False) -> str:
    if url.startswith("/"):
        url = f"{OPEN_WEBUI_BASE}{url}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "local-vision-bridge/1.1"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return base64.b64encode(r.read()).decode()
    except Exception as exc:
        if debug:
            print(f"[Vision Bridge] Could not fetch image from {url}: {exc}")
        return ""


# ---------------------------------------------------------------------------
# Filter
# ---------------------------------------------------------------------------

class Filter:
    class Valves(BaseModel):
        priority: int = Field(
            default=-10,
            description="Execution priority. Lower number runs first. "
                        "Must be negative to run before Adaptive Memory (priority 0).",
        )
        vision_server_url: str = Field(
            default="http://127.0.0.1:11434/v1/chat/completions",
            description="OpenAI-compatible endpoint of the local vision LLM",
        )
        vision_model: str = Field(
            default="llama3.2-vision:11b",
            description="Ollama model tag for the vision model",
        )
        vision_prompt: str = Field(
            default=(
                "Analyze this image and respond in this exact format:\n\n"
                "TEXT: [Copy any visible text VERBATIM — signs, labels, UI elements, "
                "documents, watermarks. Write 'none' if no text is present.]\n\n"
                "DESCRIPTION: [Describe the main subject, key objects, people (if any), "
                "setting, colors, and spatial layout clearly enough for someone who cannot "
                "see the image to fully understand it. Be specific and factual.]"
            ),
            description="Prompt sent to the vision model for every image",
        )
        debug_mode: bool = Field(
            default=False,
            description="Print debug logs to the server console",
        )

    def __init__(self):
        self.valves = self.Valves()
        self._cache: dict[str, str] = {}   # md5(b64) → description

    # ------------------------------------------------------------------
    # Image extraction — handles every format Open WebUI 0.9.5 produces
    # ------------------------------------------------------------------

    def _extract_images(self, message: dict) -> tuple[list[str], object]:
        """
        Return (list_of_b64, cleaned_content).

        Formats handled:
          1. content is a list with type="image_url" items (inline base64 or server URL)
          2. content is a list with type="file" items that have an image MIME type
          3. content is a list with type="image" items (Anthropic-style source dict)
          4. message has a top-level "images" list (legacy Ollama format)
        """
        content = message.get("content", "")
        images: list[str] = []
        remaining = content        # returned as-is if we can't process it

        if isinstance(content, list):
            kept = []
            for item in content:
                if not isinstance(item, dict):
                    kept.append(item)
                    continue

                itype = item.get("type", "")
                extracted = ""

                # ── Format 1: image_url (standard OpenAI / Open WebUI clipboard paste) ──
                if itype == "image_url":
                    img_field = item.get("image_url") or {}
                    url = img_field.get("url", "") if isinstance(img_field, dict) else str(img_field)
                    if "base64," in url:
                        extracted = url.split("base64,", 1)[1]
                    elif url:
                        extracted = _fetch_b64(url, self.valves.debug_mode)

                # ── Format 2: file (Open WebUI file-picker upload) ──
                elif itype == "file":
                    file_info = item.get("file") or {}
                    if isinstance(file_info, dict):
                        mime = file_info.get("type", "")
                        if mime.startswith("image/"):
                            url = file_info.get("url", "")
                            if url:
                                extracted = _fetch_b64(url, self.valves.debug_mode)

                # ── Format 3: image (Anthropic-style) ──
                elif itype == "image":
                    source = item.get("source") or {}
                    if isinstance(source, dict):
                        if source.get("type") == "base64":
                            extracted = source.get("data", "")
                        elif source.get("url"):
                            extracted = _fetch_b64(source["url"], self.valves.debug_mode)

                if extracted:
                    images.append(extracted)
                else:
                    kept.append(item)

            remaining = kept

        # ── Format 4: legacy Ollama "images" field ──
        if message.get("images"):
            images.extend(message["images"])

        if self.valves.debug_mode and isinstance(content, list):
            types_seen = [i.get("type", "?") for i in content if isinstance(i, dict)]
            if not images:
                print(f"[Vision Bridge] Content types seen (no images found): {types_seen}")
            else:
                print(f"[Vision Bridge] Extracted {len(images)} image(s) from types: {types_seen}")

        return images, remaining

    # ------------------------------------------------------------------
    # Inlet
    # ------------------------------------------------------------------

    async def inlet(
        self,
        body: dict,
        __user__: Optional[dict] = None,
        __event_emitter__: Optional[Callable[[Any], Awaitable[None]]] = None,
    ) -> dict:
        messages = body.get("messages", [])
        if not messages:
            return body

        if self.valves.debug_mode:
            print(f"[Vision Bridge] Scanning {len(messages)} messages ...")

        new_count = cached_count = 0

        for message in messages:
            images, cleaned_content = self._extract_images(message)

            if not images:
                continue

            # Replace image items with cleaned content immediately
            message["content"] = cleaned_content
            if "images" in message:
                del message["images"]

            descriptions = []
            for img_b64 in images:
                key = hashlib.md5(img_b64.encode()).hexdigest()
                if key in self._cache:
                    desc = self._cache[key]
                    cached_count += 1
                else:
                    if self.valves.debug_mode:
                        print("[Vision Bridge] Cache miss — sending to vision model ...")
                    if __event_emitter__:
                        await __event_emitter__({
                            "type": "status",
                            "data": {
                                "description": "🖼️ Analysing image with vision model…",
                                "done": False,
                            },
                        })
                    # Run the blocking HTTP call in a thread so the async event
                    # loop stays free — prevents WebSocket "Connection lost" drops.
                    import asyncio as _asyncio
                    loop = _asyncio.get_event_loop()
                    desc = await loop.run_in_executor(None, self._describe, img_b64)
                    # Only cache successful descriptions — never cache error strings.
                    if not desc.startswith("(Vision model error:"):
                        self._cache[key] = desc
                    new_count += 1
                    if __event_emitter__:
                        await __event_emitter__({
                            "type": "status",
                            "data": {
                                "description": "🖼️ Image analysed." if not desc.startswith("(Vision model error:") else "⚠️ Vision model error — image could not be analysed.",
                                "done": True,
                            },
                        })
                descriptions.append(desc)

            # Reassemble: vision output + original text
            if isinstance(cleaned_content, list):
                text_parts = [
                    p.get("text", "") for p in cleaned_content
                    if isinstance(p, dict) and p.get("type") == "text"
                ]
                user_text = "\n".join(text_parts).strip()
            else:
                user_text = str(cleaned_content).strip() if cleaned_content else ""

            if self.valves.debug_mode:
                for i, d in enumerate(descriptions):
                    print(f"[Vision Bridge] Image {i+1} description: {d[:200]}")

            # Format the image analysis as natural language the LLM will use.
            # The model never sees raw image bytes — this text IS the image for it.
            if len(descriptions) == 1:
                image_section = (
                    f"The user has attached an image. "
                    f"It has been automatically analysed:\n{descriptions[0]}"
                )
            else:
                parts = [f"Image {i+1}:\n{d}" for i, d in enumerate(descriptions)]
                image_section = (
                    f"The user has attached {len(descriptions)} images. "
                    f"They have been automatically analysed:\n\n"
                    + "\n\n".join(parts)
                )

            message["content"] = (
                image_section
                + (f"\n\nUser message: {user_text}" if user_text else "")
            )

        if self.valves.debug_mode:
            print(f"[Vision Bridge] Done — new: {new_count}, cached: {cached_count}")

        return body

    # ------------------------------------------------------------------
    # Vision model call
    # ------------------------------------------------------------------

    def _describe(self, b64: str) -> str:
        mime = _detect_mime(b64)
        payload = {
            "model": self.valves.vision_model,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": self.valves.vision_prompt},
                    {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                ],
            }],
            "max_tokens": 768,
            "temperature": 0.1,
        }
        # Retry once on timeout: the first call may trigger a cold model load
        # (llama3.2-vision:11b needs ~30-60s to swap in from disk). The second
        # call finds it already loaded and succeeds immediately.
        last_exc: Exception = RuntimeError("no attempts made")
        for attempt in range(2):
            try:
                resp = requests.post(
                    self.valves.vision_server_url,
                    json=payload,
                    timeout=120,
                )
                resp.raise_for_status()
                return resp.json()["choices"][0]["message"]["content"]
            except requests.exceptions.Timeout as exc:
                last_exc = exc
                if self.valves.debug_mode:
                    print(f"[Vision Bridge] Attempt {attempt + 1} timed out — "
                          f"{'retrying (model loading)' if attempt == 0 else 'giving up'}")
            except Exception as exc:
                return f"(Vision model error: {exc})"
        return f"(Vision model error: timed out after 2 attempts — {last_exc})"
