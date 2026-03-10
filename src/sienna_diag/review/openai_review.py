from __future__ import annotations

import json

import httpx

from sienna_diag.config import settings
from sienna_diag.models import OpenAISecondOpinion


def openai_second_opinion(summary_text: str) -> OpenAISecondOpinion:
    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY is not set")

    url = f"{settings.openai_base_url.rstrip('/')}/chat/completions"
    schema = {
        "name": "safe_diag_second_opinion",
        "schema": {
            "type": "object",
            "properties": {
                "risk_level": {"type": "string", "enum": ["low", "medium", "high"]},
                "key_findings": {"type": "array", "items": {"type": "string"}},
                "recommended_next_read_only_steps": {"type": "array", "items": {"type": "string"}},
                "prohibited_actions_confirmed": {"type": "array", "items": {"type": "string"}},
            },
            "required": [
                "risk_level",
                "key_findings",
                "recommended_next_read_only_steps",
                "prohibited_actions_confirmed",
            ],
            "additionalProperties": False,
        },
        "strict": True,
    }

    payload = {
        "model": settings.openai_model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "Provide only read-only diagnostic guidance. Do not suggest replay, security access, "
                    "programming, write routines, or control commands."
                ),
            },
            {"role": "user", "content": summary_text},
        ],
        "response_format": {"type": "json_schema", "json_schema": schema},
        "temperature": 0.1,
    }

    headers = {
        "Authorization": f"Bearer {settings.openai_api_key}",
        "Content-Type": "application/json",
    }

    with httpx.Client(timeout=30.0) as client:
        response = client.post(url, headers=headers, json=payload)
        response.raise_for_status()
        data = response.json()

    content = data["choices"][0]["message"]["content"]
    parsed = json.loads(content)
    return OpenAISecondOpinion(**parsed)
