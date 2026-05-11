"""
audio.analyze — download audio from URL, send to Gemini 3.1 Flash via OpenRouter,
return full analysis: mood, tempo, structure, instruments, vocals, genre, lyrics.
"""
import base64
from typing import Optional

import httpx

from config import OPENROUTER_KEY, MODEL

AUDIO_MODEL = "google/gemini-3.1-flash-lite"

AUDIO_ANALYSIS_PROMPT = """You are an expert music analyst. Listen to this audio carefully and provide a detailed analysis:

1. **Genre & Subgenre** — be specific
2. **Mood & Atmosphere** — what emotions does it evoke
3. **Tempo & Energy** — BPM estimate, energy level
4. **Structure** — intro, verse, chorus, bridge, outro — what you hear
5. **Instruments** — identify every instrument/sound you hear
6. **Vocals** — presence, style, language, any lyrics you can transcribe
7. **Production style** — mixing, effects, lo-fi/hi-fi, notable techniques
8. **Strengths** — what works well
9. **Weaknesses** — what could be improved
10. **Overall impression** — honest take in 2-3 sentences

Be specific and honest. Don't pad."""


async def audio_analyze(
    url: str,
    prompt: Optional[str] = None,
) -> dict:
    """
    Download audio from URL and analyze with Gemini 3.1 Flash.
    Returns structured analysis dict.
    """
    if not OPENROUTER_KEY:
        return {"error": "OPENROUTER_API_KEY not set", "mock": True}

    # Download audio
    async with httpx.AsyncClient(timeout=60.0) as client:
        try:
            resp = await client.get(url, follow_redirects=True)
            resp.raise_for_status()
            audio_bytes = resp.content
            content_type = resp.headers.get("content-type", "audio/mpeg")
            # Normalize content type
            if "mp3" in content_type or "mpeg" in content_type:
                audio_format = "mp3"
                mime = "audio/mpeg"
            elif "wav" in content_type:
                audio_format = "wav"
                mime = "audio/wav"
            elif "ogg" in content_type:
                audio_format = "ogg"
                mime = "audio/ogg"
            else:
                audio_format = "mp3"
                mime = "audio/mpeg"
        except Exception as e:
            return {"error": f"Failed to download audio: {e}"}

    audio_b64 = base64.b64encode(audio_bytes).decode("utf-8")
    analysis_prompt = prompt or AUDIO_ANALYSIS_PROMPT

    # Send to Gemini via OpenRouter with audio modality
    payload = {
        "model": AUDIO_MODEL,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": analysis_prompt,
                    },
                    {
                        "type": "input_audio",
                        "input_audio": {
                            "data": audio_b64,
                            "format": audio_format,
                        },
                    },
                ],
            }
        ],
        "max_tokens": 2000,
        "temperature": 0.3,
    }

    async with httpx.AsyncClient(timeout=120.0) as client:
        try:
            resp = await client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {OPENROUTER_KEY}",
                    "HTTP-Referer": "https://eiros.local",
                    "X-Title": "EirosKernel-AudioAnalyzer",
                },
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()
            analysis_text = data["choices"][0]["message"]["content"]
            return {
                "status": "ok",
                "model": AUDIO_MODEL,
                "url": url,
                "audio_size_kb": round(len(audio_bytes) / 1024, 1),
                "format": audio_format,
                "analysis": analysis_text,
            }
        except Exception as e:
            return {"error": f"Gemini API error: {e}", "url": url}
