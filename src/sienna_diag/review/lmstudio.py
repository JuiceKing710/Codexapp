from __future__ import annotations

import httpx

from sienna_diag.config import settings


def local_llama_review(summary_text: str) -> dict:
    url = f"{settings.lm_studio_base_url.rstrip('/')}/v1/chat/completions"
    payload = {
        "model": settings.lm_studio_model,
        "messages": [
            {
                "role": "system",
                "content": "You are a read-only automotive diagnostics reviewer. Reject any write/control suggestion.",
            },
            {"role": "user", "content": summary_text},
        ],
        "temperature": 0.1,
    }

    with httpx.Client(timeout=20.0) as client:
        response = client.post(url, json=payload)
        response.raise_for_status()
        return response.json()
