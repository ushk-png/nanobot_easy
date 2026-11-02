---
name: testy-voice
description: Generate requested speech, narration, or MP3 audio for Testy. Use only for an explicit audio request; ordinary text and Japanese study do not require audio.
metadata:
  nanobot:
    id: testy-voice
    version: 1.0.0
    status: draft
    category: media.speech
    risk_level: low
    requires_exec: true
---

# Testy Voice

For requested speech, use the existing local MLX/Qwen3 helper
`/Users/imkimhk/Project/nanobot_skill/.local/workspace/scripts/agent_tts.py`.
Inspect its supported arguments before running it. Default model:
`mlx-community/Qwen3-TTS-12Hz-1.7B-CustomVoice-8bit`; voice: `serena`;
format: MP3 with an ASCII filename. Follow any explicit backend/voice choice.
Deliver the requested artifact using the available media tool and check its result.
Do not install packages or send to another recipient without authorization.
Japanese study is text-only unless Frank explicitly requests an exception.
