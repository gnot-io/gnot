# Guide 16 — Content Automation Pipeline

**Difficulty:** Advanced  
**Prerequisite:** [Guide 15 — Security Hardening](../15-security-hardening/README.md)  
**Goal:** Build a fully automated content production pipeline — topic → research → article → images → video assembly → publish — orchestrated entirely by the GNOT mesh from a single prompt.

---

## Pipeline Overview

```
User prompt: "Write a 5-minute video about solar energy"
        │
        ▼
POST /intent on deb-0
        │
  ┌─────▼──────────────────────────────────────────────────┐
  │  LLM Orchestrator (ReAct loop on deb-0)                │
  │                                                        │
  │  Step 1: research_topic(topic)         → deb-0         │
  │  Step 2: write_script(research)        → deb-0 (LLM)   │
  │  Step 3: generate_images(script)       → deb-1 (GPU)   │
  │  Step 4: text_to_speech(script)        → cen-0         │
  │  Step 5: assemble_video(imgs, audio)   → alm-0 (ffmpeg)│
  │  Step 6: publish_to_youtube(video)     → deb-0         │
  └────────────────────────────────────────────────────────┘
```

Each step is a custom action. You build the actions progressively — start with a simple text pipeline, then add image/video stages.

---

## Part 1 — Research and Writing Actions

### `research_topic.py` (on deb-0)

```python
# ~/gnot-nodes/deb-0/actions/research_topic.py
"""
Search the web and summarize key facts about a topic.
Uses DuckDuckGo's text search (no API key needed).
"""

import asyncio
import httpx

ASYNC = True


async def run(params: dict, context: dict) -> dict:
    topic = params["topic"]
    max_results = params.get("max_results", 5)

    # Use DuckDuckGo instant answer API (free, no key)
    url = f"https://api.duckduckgo.com/?q={httpx.utils.quote(topic)}&format=json&no_html=1"

    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(url, headers={"User-Agent": "GNOT/1.0"})
        r.raise_for_status()

    data = r.json()

    results = []

    # Main abstract
    if data.get("AbstractText"):
        results.append({
            "source": data.get("AbstractSource", "Wikipedia"),
            "text": data["AbstractText"][:500],
        })

    # Related topics
    for item in data.get("RelatedTopics", [])[:max_results]:
        if isinstance(item, dict) and item.get("Text"):
            results.append({
                "source": "DuckDuckGo",
                "text": item["Text"][:200],
            })

    return {
        "topic": topic,
        "summary": data.get("AbstractText", "")[:1000],
        "results": results,
        "result_count": len(results),
    }
```

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "research_topic",
  "description": "Search for information about a topic and return a structured summary.",
  "type": "object",
  "properties": {
    "topic": {
      "type": "string",
      "description": "Topic to research"
    },
    "max_results": {
      "type": "integer",
      "description": "Maximum number of search results to return",
      "default": 5,
      "minimum": 1,
      "maximum": 20
    }
  },
  "required": ["topic"],
  "additionalProperties": false
}
```

### `write_article.py` (on deb-0)

```python
# ~/gnot-nodes/deb-0/actions/write_article.py
"""
Use the node's LLM to write an article or script from research notes.
Requires llm_provider configured on deb-0.
"""

import httpx
import os

ASYNC = True


async def run(params: dict, context: dict) -> dict:
    topic = params["topic"]
    research = params.get("research", "")
    format_type = params.get("format", "article")   # article | video_script | social_post
    word_count = params.get("word_count", 500)
    tone = params.get("tone", "informative")

    api_key = os.environ.get("LLM_API_KEY") or params.get("_llm_api_key")
    if not api_key:
        raise ValueError("LLM_API_KEY not set. Configure llm_api_key in node.yaml.")

    format_instructions = {
        "article": f"Write a {word_count}-word {tone} article",
        "video_script": f"Write a {word_count}-word video script with scene markers [SCENE: description]",
        "social_post": "Write a concise social media post (under 280 characters)",
    }.get(format_type, f"Write a {word_count}-word text")

    prompt = f"""{format_instructions} about: {topic}

Research notes:
{research}

Requirements:
- Tone: {tone}
- Format: {format_type}
- Be engaging and accurate
- Use the research notes as source material"""

    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
            json={
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 2000,
                "messages": [{"role": "user", "content": prompt}],
            },
        )
        r.raise_for_status()

    result = r.json()
    text = result["content"][0]["text"]

    return {
        "topic": topic,
        "format": format_type,
        "content": text,
        "word_count": len(text.split()),
    }
```

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "write_article",
  "description": "Write an article, video script, or social post using AI. Requires LLM_API_KEY environment variable.",
  "type": "object",
  "properties": {
    "topic": {"type": "string", "description": "Main topic"},
    "research": {"type": "string", "description": "Research notes to base the content on", "default": ""},
    "format": {
      "type": "string",
      "enum": ["article", "video_script", "social_post"],
      "default": "article"
    },
    "word_count": {"type": "integer", "default": 500, "minimum": 50, "maximum": 3000},
    "tone": {"type": "string", "default": "informative"}
  },
  "required": ["topic"],
  "additionalProperties": false
}
```

---

## Part 2 — Image Generation Action

### `generate_image.py` (on deb-1 or deb-0)

```python
# ~/gnot-nodes/deb-1/actions/generate_image.py
"""
Generate an image using the Stable Diffusion API (or any compatible API).
Saves the image to disk and returns the path.
"""

import asyncio
import base64
import httpx
import os
import time

ASYNC = True


async def run(params: dict, context: dict) -> dict:
    prompt = params["prompt"]
    output_dir = params.get("output_dir", "/tmp/gnot-images")
    width = params.get("width", 1024)
    height = params.get("height", 576)   # 16:9 for video

    os.makedirs(output_dir, exist_ok=True)

    # Use Stability AI API (get free key at platform.stability.ai)
    api_key = context.get("caller_credentials", {}).get("stability_api_key")
    if not api_key:
        api_key = os.environ.get("STABILITY_API_KEY")
    if not api_key:
        raise ValueError("Missing credential: stability_api_key")

    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(
            "https://api.stability.ai/v1/generation/stable-diffusion-v1-6/text-to-image",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
            },
            json={
                "text_prompts": [{"text": prompt, "weight": 1}],
                "cfg_scale": 7,
                "height": height,
                "width": width,
                "samples": 1,
                "steps": 30,
            },
        )
        r.raise_for_status()

    data = r.json()
    image_b64 = data["artifacts"][0]["base64"]
    image_bytes = base64.b64decode(image_b64)

    filename = f"img_{int(time.time())}.png"
    filepath = os.path.join(output_dir, filename)
    with open(filepath, "wb") as f:
        f.write(image_bytes)

    return {
        "path": filepath,
        "filename": filename,
        "prompt": prompt,
        "size": f"{width}x{height}",
        "size_bytes": len(image_bytes),
    }
```

---

## Part 3 — Video Assembly Action

### `assemble_video.py` (on alm-0)

```python
# ~/gnot-nodes/alm-0/actions/assemble_video.py
"""
Assemble a video from a list of image files using ffmpeg.
Images are displayed sequentially, each for a configurable duration.
"""

import asyncio
import os
import tempfile

ASYNC = True


async def run(params: dict, context: dict) -> dict:
    image_paths = params["image_paths"]      # list of local file paths
    output_path = params.get("output_path", f"/tmp/gnot-video-{os.getpid()}.mp4")
    seconds_per_image = params.get("seconds_per_image", 5)
    audio_path = params.get("audio_path")    # optional narration audio
    fps = params.get("fps", 24)

    if not image_paths:
        raise ValueError("image_paths cannot be empty")

    # Verify all images exist
    for p in image_paths:
        if not os.path.exists(p):
            raise FileNotFoundError(f"Image not found: {p}")

    # Write ffmpeg input list
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
        list_path = f.name
        for img in image_paths:
            f.write(f"file '{img}'\n")
            f.write(f"duration {seconds_per_image}\n")
        # ffmpeg requires a final entry without duration
        f.write(f"file '{image_paths[-1]}'\n")

    # Build ffmpeg command
    if audio_path and os.path.exists(audio_path):
        cmd = (
            f"ffmpeg -y -f concat -safe 0 -i {list_path} "
            f"-i {audio_path} "
            f"-c:v libx264 -pix_fmt yuv420p -r {fps} "
            f"-c:a aac -shortest {output_path}"
        )
    else:
        cmd = (
            f"ffmpeg -y -f concat -safe 0 -i {list_path} "
            f"-c:v libx264 -pix_fmt yuv420p -r {fps} "
            f"{output_path}"
        )

    proc = await asyncio.create_subprocess_shell(
        cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    _, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)
    os.unlink(list_path)

    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {stderr.decode()[-500:]}")

    size_mb = os.path.getsize(output_path) / (1024 * 1024)

    return {
        "output_path": output_path,
        "image_count": len(image_paths),
        "duration_seconds": len(image_paths) * seconds_per_image,
        "size_mb": round(size_mb, 2),
    }
```

> **Dependency:** Install ffmpeg on alm-0:
> ```bash
> sudo dnf install -y ffmpeg   # AlmaLinux
> ```

---

## Part 4 — Full Pipeline via Single Prompt

With all actions deployed, trigger the complete pipeline:

```bash
curl -s -X POST http://localhost:8080/intent \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "Create a short video about solar energy. Steps: 1) Research the topic, 2) Write a 3-scene video script, 3) Generate one image per scene, 4) Assemble the images into a 15-second MP4 video at /tmp/solar-video.mp4, 5) Tell me where the final file is.",
    "session_id": "content-pipeline",
    "caller_credentials": {
      "stability_api_key": "sk-your-stability-key"
    }
  }' | python3 -c "import sys,json; print(json.load(sys.stdin)['response'])"
```

The LLM orchestrates all five steps across the mesh autonomously.

---

## Extending the Pipeline

Add more nodes and actions to extend what the pipeline can do:

| Action | Node | What to add |
|--------|------|------------|
| `text_to_speech` | cen-0 | ElevenLabs or gTTS API → `.mp3` |
| `publish_to_youtube` | deb-0 | YouTube Data API v3 upload |
| `post_to_social` | deb-0 | Twitter/X, LinkedIn, Facebook APIs |
| `send_newsletter` | deb-0 | Mailchimp or SendGrid |
| `translate_article` | any | DeepL or LibreTranslate API |

Each addition is just two files in `actions/` — no changes to the orchestrator or any other node.

---

## Summary

You have built:
- ✅ A research action that queries the web
- ✅ An AI writing action (article, script, social post)
- ✅ An image generation action (Stability AI)
- ✅ A video assembly action (ffmpeg)
- ✅ A full pipeline orchestrated from one prompt

**Next:** [Guide 17 — Telegram Bot + CRM Integration](../17-telegram-crm/README.md)

---

*Part of the [GNOT Examples](../README.md) series.*
