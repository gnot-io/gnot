# Guide 22 — Text-to-Image and Text-to-Speech Actions

**Difficulty:** Intermediate  
**Prerequisite:** [Guide 08 — Advanced Custom Actions](../08-advanced-actions/README.md)  
**Goal:** Add AI media generation to your mesh — generate images from text prompts using multiple providers (DALL-E 3, Stability AI, fal.ai) and convert text to natural-sounding speech (ElevenLabs, OpenAI TTS, gTTS).

---

## Text-to-Image Actions

### Action 1 — DALL-E 3 (OpenAI)

> **Requires:** OpenAI API key with image generation access.

```python
# ~/gnot-nodes/deb-0/actions/text_to_image_dalle.py

import asyncio
import base64
import httpx
import os
import time

ASYNC = True


async def run(params: dict, context: dict) -> dict:
    prompt     = params["prompt"]
    size       = params.get("size", "1024x1024")
    quality    = params.get("quality", "standard")    # standard | hd
    style      = params.get("style", "natural")       # natural | vivid
    output_dir = params.get("output_dir", "/tmp/gnot-images")
    save_file  = params.get("save_file", True)

    creds   = context.get("caller_credentials", {})
    api_key = creds.get("openai_api_key") or os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise ValueError("Missing credential: openai_api_key")

    os.makedirs(output_dir, exist_ok=True)

    async with httpx.AsyncClient(timeout=120) as client:
        r = await client.post(
            "https://api.openai.com/v1/images/generations",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model":   "dall-e-3",
                "prompt":  prompt,
                "n":       1,
                "size":    size,
                "quality": quality,
                "style":   style,
                "response_format": "b64_json" if save_file else "url",
            },
        )
        r.raise_for_status()

    data = r.json()["data"][0]

    result = {
        "prompt":   prompt,
        "model":    "dall-e-3",
        "size":     size,
        "quality":  quality,
        "revised_prompt": data.get("revised_prompt"),
    }

    if save_file and data.get("b64_json"):
        filename = f"dalle_{int(time.time())}.png"
        filepath = os.path.join(output_dir, filename)
        with open(filepath, "wb") as f:
            f.write(base64.b64decode(data["b64_json"]))
        result["file_path"] = filepath
        result["size_kb"]   = round(os.path.getsize(filepath) / 1024, 1)
    else:
        result["url"] = data.get("url")

    return result
```

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "text_to_image_dalle",
  "description": "Generate an image with DALL-E 3. Requires openai_api_key in caller_credentials.",
  "type": "object",
  "properties": {
    "prompt":     {"type": "string", "description": "Image description", "minLength": 1},
    "size":       {"type": "string", "enum": ["1024x1024", "1792x1024", "1024x1792"], "default": "1024x1024"},
    "quality":    {"type": "string", "enum": ["standard", "hd"], "default": "standard"},
    "style":      {"type": "string", "enum": ["natural", "vivid"], "default": "natural"},
    "output_dir": {"type": "string", "default": "/tmp/gnot-images"},
    "save_file":  {"type": "boolean", "default": true}
  },
  "required": ["prompt"],
  "additionalProperties": false
}
```

---

### Action 2 — Stability AI (Stable Diffusion)

> **Requires:** Free API key from [platform.stability.ai](https://platform.stability.ai).

```python
# ~/gnot-nodes/deb-0/actions/text_to_image_stability.py

import asyncio
import base64
import httpx
import os
import time

ASYNC = True

ENGINES = {
    "sd3":      "stable-diffusion-3-medium",
    "sdxl":     "stable-diffusion-xl-1024-v1-0",
    "sd16":     "stable-diffusion-v1-6",
}


async def run(params: dict, context: dict) -> dict:
    prompt         = params["prompt"]
    negative_prompt = params.get("negative_prompt", "")
    engine         = params.get("engine", "sdxl")
    width          = params.get("width", 1024)
    height         = params.get("height", 1024)
    steps          = params.get("steps", 30)
    cfg_scale      = params.get("cfg_scale", 7.0)
    output_dir     = params.get("output_dir", "/tmp/gnot-images")

    creds   = context.get("caller_credentials", {})
    api_key = creds.get("stability_api_key") or os.environ.get("STABILITY_API_KEY")
    if not api_key:
        raise ValueError("Missing credential: stability_api_key")

    engine_id = ENGINES.get(engine, ENGINES["sdxl"])
    os.makedirs(output_dir, exist_ok=True)

    text_prompts = [{"text": prompt, "weight": 1.0}]
    if negative_prompt:
        text_prompts.append({"text": negative_prompt, "weight": -1.0})

    async with httpx.AsyncClient(timeout=120) as client:
        r = await client.post(
            f"https://api.stability.ai/v1/generation/{engine_id}/text-to-image",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
            },
            json={
                "text_prompts": text_prompts,
                "cfg_scale":    cfg_scale,
                "height":       height,
                "width":        width,
                "samples":      1,
                "steps":        steps,
            },
        )
        r.raise_for_status()

    image_b64 = r.json()["artifacts"][0]["base64"]
    filename  = f"stability_{int(time.time())}.png"
    filepath  = os.path.join(output_dir, filename)

    with open(filepath, "wb") as f:
        f.write(base64.b64decode(image_b64))

    return {
        "file_path": filepath,
        "engine":    engine_id,
        "size":      f"{width}x{height}",
        "size_kb":   round(os.path.getsize(filepath) / 1024, 1),
        "prompt":    prompt,
    }
```

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "text_to_image_stability",
  "description": "Generate an image with Stability AI (Stable Diffusion). Requires stability_api_key in caller_credentials.",
  "type": "object",
  "properties": {
    "prompt":          {"type": "string", "minLength": 1},
    "negative_prompt": {"type": "string", "default": ""},
    "engine":          {"type": "string", "enum": ["sd3", "sdxl", "sd16"], "default": "sdxl"},
    "width":           {"type": "integer", "default": 1024},
    "height":          {"type": "integer", "default": 1024},
    "steps":           {"type": "integer", "minimum": 10, "maximum": 50, "default": 30},
    "cfg_scale":       {"type": "number", "minimum": 1, "maximum": 35, "default": 7},
    "output_dir":      {"type": "string", "default": "/tmp/gnot-images"}
  },
  "required": ["prompt"],
  "additionalProperties": false
}
```

---

### Action 3 — fal.ai (Fast Inference, Multiple Models)

> **Requires:** API key from [fal.ai](https://fal.ai). Fast and cheap — great for high-volume generation.

```python
# ~/gnot-nodes/deb-0/actions/text_to_image_fal.py

import asyncio
import httpx
import os
import time
import urllib.request

ASYNC = True

# Available models on fal.ai
MODELS = {
    "flux-schnell":  "fal-ai/flux/schnell",       # fast, free tier
    "flux-dev":      "fal-ai/flux/dev",            # higher quality
    "flux-pro":      "fal-ai/flux-pro",            # best quality
    "sd3-medium":    "fal-ai/stable-diffusion-v3-medium",
}


async def run(params: dict, context: dict) -> dict:
    prompt      = params["prompt"]
    model       = params.get("model", "flux-schnell")
    image_size  = params.get("image_size", "landscape_4_3")
    num_steps   = params.get("num_steps", 4)
    output_dir  = params.get("output_dir", "/tmp/gnot-images")

    creds   = context.get("caller_credentials", {})
    api_key = creds.get("fal_api_key") or os.environ.get("FAL_KEY")
    if not api_key:
        raise ValueError("Missing credential: fal_api_key")

    model_id = MODELS.get(model, MODELS["flux-schnell"])
    os.makedirs(output_dir, exist_ok=True)

    async with httpx.AsyncClient(timeout=120) as client:
        # Submit request
        r = await client.post(
            f"https://fal.run/{model_id}",
            headers={
                "Authorization": f"Key {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "prompt":     prompt,
                "image_size": image_size,
                "num_inference_steps": num_steps,
                "num_images": 1,
            },
        )
        r.raise_for_status()
        result = r.json()

    # Download the image
    image_url = result["images"][0]["url"]
    filename  = f"fal_{int(time.time())}.png"
    filepath  = os.path.join(output_dir, filename)

    async with httpx.AsyncClient(timeout=30) as client:
        img_r = await client.get(image_url)
        img_r.raise_for_status()
        with open(filepath, "wb") as f:
            f.write(img_r.content)

    return {
        "file_path": filepath,
        "model":     model_id,
        "size_kb":   round(os.path.getsize(filepath) / 1024, 1),
        "prompt":    prompt,
        "seed":      result.get("seed"),
    }
```

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "text_to_image_fal",
  "description": "Generate an image using fal.ai (FLUX, SD3). Requires fal_api_key in caller_credentials.",
  "type": "object",
  "properties": {
    "prompt":     {"type": "string", "minLength": 1},
    "model":      {"type": "string", "enum": ["flux-schnell", "flux-dev", "flux-pro", "sd3-medium"], "default": "flux-schnell"},
    "image_size": {"type": "string", "enum": ["square_hd", "square", "portrait_4_3", "portrait_16_9", "landscape_4_3", "landscape_16_9"], "default": "landscape_4_3"},
    "num_steps":  {"type": "integer", "minimum": 1, "maximum": 50, "default": 4},
    "output_dir": {"type": "string", "default": "/tmp/gnot-images"}
  },
  "required": ["prompt"],
  "additionalProperties": false
}
```

---

## Text-to-Speech Actions

### Action 4 — ElevenLabs (Highest Quality)

> **Requires:** API key from [elevenlabs.io](https://elevenlabs.io). Free tier: 10,000 characters/month.

```python
# ~/gnot-nodes/deb-0/actions/text_to_speech_elevenlabs.py

import httpx
import os
import time

ASYNC = True

# Popular voice IDs — get full list from GET /v1/voices
VOICES = {
    "rachel":  "21m00Tcm4TlvDq8ikWAM",    # calm, female
    "adam":    "pNInz6obpgDQGcFmaJgB",    # deep, male
    "bella":   "EXAVITQu4vr4xnSDxMaL",    # soft, female
    "daniel":  "onwK4e9ZLuTAKqWW03F9",    # authoritative, male
}


async def run(params: dict, context: dict) -> dict:
    text       = params["text"]
    voice      = params.get("voice", "rachel")
    model_id   = params.get("model_id", "eleven_multilingual_v2")
    stability  = params.get("stability", 0.5)
    similarity = params.get("similarity_boost", 0.75)
    output_dir = params.get("output_dir", "/tmp/gnot-audio")

    creds   = context.get("caller_credentials", {})
    api_key = creds.get("elevenlabs_api_key") or os.environ.get("ELEVENLABS_API_KEY")
    if not api_key:
        raise ValueError("Missing credential: elevenlabs_api_key")

    voice_id = VOICES.get(voice, voice)   # allow raw voice_id too
    os.makedirs(output_dir, exist_ok=True)

    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
            headers={
                "xi-api-key":    api_key,
                "Content-Type":  "application/json",
            },
            json={
                "text":     text,
                "model_id": model_id,
                "voice_settings": {
                    "stability":        stability,
                    "similarity_boost": similarity,
                },
            },
        )
        r.raise_for_status()

    filename = f"tts_elevenlabs_{int(time.time())}.mp3"
    filepath = os.path.join(output_dir, filename)
    with open(filepath, "wb") as f:
        f.write(r.content)

    duration_estimate = len(text.split()) / 2.5   # ~2.5 words/sec

    return {
        "file_path": filepath,
        "voice":     voice,
        "model":     model_id,
        "size_kb":   round(os.path.getsize(filepath) / 1024, 1),
        "estimated_duration_s": round(duration_estimate, 1),
        "char_count": len(text),
    }
```

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "text_to_speech_elevenlabs",
  "description": "Convert text to speech using ElevenLabs. Requires elevenlabs_api_key in caller_credentials.",
  "type": "object",
  "properties": {
    "text":       {"type": "string", "description": "Text to convert", "minLength": 1, "maxLength": 5000},
    "voice":      {"type": "string", "description": "Voice preset (rachel, adam, bella, daniel) or raw voice_id", "default": "rachel"},
    "model_id":   {"type": "string", "enum": ["eleven_multilingual_v2", "eleven_turbo_v2", "eleven_monolingual_v1"], "default": "eleven_multilingual_v2"},
    "stability":  {"type": "number", "minimum": 0, "maximum": 1, "default": 0.5},
    "similarity_boost": {"type": "number", "minimum": 0, "maximum": 1, "default": 0.75},
    "output_dir": {"type": "string", "default": "/tmp/gnot-audio"}
  },
  "required": ["text"],
  "additionalProperties": false
}
```

---

### Action 5 — OpenAI TTS

> **Requires:** OpenAI API key. 6 voices, natural sound, fast.

```python
# ~/gnot-nodes/deb-0/actions/text_to_speech_openai.py

import httpx
import os
import time

ASYNC = True


async def run(params: dict, context: dict) -> dict:
    text       = params["text"]
    voice      = params.get("voice", "nova")     # alloy echo fable onyx nova shimmer
    model      = params.get("model", "tts-1")    # tts-1 | tts-1-hd
    speed      = params.get("speed", 1.0)
    output_dir = params.get("output_dir", "/tmp/gnot-audio")

    creds   = context.get("caller_credentials", {})
    api_key = creds.get("openai_api_key") or os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise ValueError("Missing credential: openai_api_key")

    os.makedirs(output_dir, exist_ok=True)

    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(
            "https://api.openai.com/v1/audio/speech",
            headers={"Authorization": f"Bearer {api_key}"},
            json={"model": model, "input": text, "voice": voice, "speed": speed},
        )
        r.raise_for_status()

    filename = f"tts_openai_{int(time.time())}.mp3"
    filepath = os.path.join(output_dir, filename)
    with open(filepath, "wb") as f:
        f.write(r.content)

    return {
        "file_path": filepath,
        "voice":     voice,
        "model":     model,
        "size_kb":   round(os.path.getsize(filepath) / 1024, 1),
        "char_count": len(text),
    }
```

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "text_to_speech_openai",
  "description": "Convert text to speech using OpenAI TTS. Requires openai_api_key in caller_credentials.",
  "type": "object",
  "properties": {
    "text":  {"type": "string", "minLength": 1, "maxLength": 4096},
    "voice": {"type": "string", "enum": ["alloy", "echo", "fable", "onyx", "nova", "shimmer"], "default": "nova"},
    "model": {"type": "string", "enum": ["tts-1", "tts-1-hd"], "default": "tts-1"},
    "speed": {"type": "number", "minimum": 0.25, "maximum": 4.0, "default": 1.0},
    "output_dir": {"type": "string", "default": "/tmp/gnot-audio"}
  },
  "required": ["text"],
  "additionalProperties": false
}
```

---

### Action 6 — gTTS (Free, No API Key)

> **Requires:** No account — uses Google Translate's TTS endpoint. Install: `pip install gtts`

```python
# ~/gnot-nodes/deb-0/actions/text_to_speech_gtts.py

import asyncio
import os
import time

ASYNC = True


async def run(params: dict, context: dict) -> dict:
    text       = params["text"]
    lang       = params.get("lang", "en")        # en, vi, ja, fr, es, ...
    slow       = params.get("slow", False)
    output_dir = params.get("output_dir", "/tmp/gnot-audio")

    os.makedirs(output_dir, exist_ok=True)
    filename = f"tts_gtts_{int(time.time())}.mp3"
    filepath = os.path.join(output_dir, filename)

    # Run in thread pool to avoid blocking
    loop = asyncio.get_event_loop()

    def _generate():
        from gtts import gTTS
        tts = gTTS(text=text, lang=lang, slow=slow)
        tts.save(filepath)

    await loop.run_in_executor(None, _generate)

    return {
        "file_path": filepath,
        "lang":      lang,
        "size_kb":   round(os.path.getsize(filepath) / 1024, 1),
        "slow":      slow,
        "char_count": len(text),
    }
```

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "text_to_speech_gtts",
  "description": "Convert text to speech using gTTS (free, no API key needed). Requires gtts package installed.",
  "type": "object",
  "properties": {
    "text": {"type": "string", "minLength": 1, "maxLength": 5000},
    "lang": {"type": "string", "description": "Language code: en, vi, ja, fr, es, ko, zh, de...", "default": "en"},
    "slow": {"type": "boolean", "description": "Speak slowly", "default": false},
    "output_dir": {"type": "string", "default": "/tmp/gnot-audio"}
  },
  "required": ["text"],
  "additionalProperties": false
}
```

Install dependency on deb-0:

```bash
python3 gnot/src/mesh_ctl.py run deb-0 execute_command \
  '{"command": "pip install gtts --break-system-packages"}'
```

---

## Combined Media Workflow

Generate image + audio narration from a single prompt:

```bash
curl -s -X POST http://localhost:8080/intent \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "Create a product showcase for a coffee brand: 1) Generate an image of a steaming cup of artisan coffee in a cozy cafe using FLUX (fal.ai), 2) Write a 30-word narration script for the image, 3) Convert the narration to speech using ElevenLabs (voice: bella), 4) Tell me the file paths of both outputs.",
    "session_id": "media-gen",
    "caller_credentials": {
      "fal_api_key": "your-fal-key",
      "elevenlabs_api_key": "your-elevenlabs-key"
    }
  }' | python3 -c "import sys,json; print(json.load(sys.stdin)['response'])"
```

---

## Quick Provider Comparison

| Action | Provider | Cost | Quality | Speed | No-key option |
|--------|----------|------|---------|-------|---------------|
| `text_to_image_dalle` | OpenAI | ~$0.04/img | ⭐⭐⭐⭐ | Medium | ❌ |
| `text_to_image_stability` | Stability AI | ~$0.002/img | ⭐⭐⭐ | Medium | ❌ |
| `text_to_image_fal` | fal.ai | ~$0.001/img | ⭐⭐⭐⭐ | Fast | ❌ |
| `text_to_speech_elevenlabs` | ElevenLabs | ~$0.30/1k chars | ⭐⭐⭐⭐⭐ | Medium | ❌ |
| `text_to_speech_openai` | OpenAI | $0.015/1k chars | ⭐⭐⭐⭐ | Fast | ❌ |
| `text_to_speech_gtts` | Google (free) | Free | ⭐⭐ | Fast | ✅ |

---

## Summary

You now have a complete AI media generation library:
- ✅ **Text-to-Image:** DALL-E 3, Stability AI (SDXL/SD3), fal.ai (FLUX)
- ✅ **Text-to-Speech:** ElevenLabs, OpenAI TTS, gTTS (free)
- ✅ All actions use `caller_credentials` — plug in your own keys
- ✅ Combinable in single `/intent` prompts for full media pipelines

**Back to:** [Examples Index](../README.md)

---

*Part of the [GNOT Examples](../README.md) series.*
