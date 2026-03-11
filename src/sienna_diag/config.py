from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


load_dotenv()


@dataclass(frozen=True)
class Settings:
    app_id: str = os.getenv("APP_ID", "Zebs_obdAi")
    app_display_name: str = os.getenv("APP_DISPLAY_NAME", "Zeb’s OBD AI")

    host: str = os.getenv("HOST", "127.0.0.1")
    port: int = int(os.getenv("PORT", "8000"))

    default_protocol: str = os.getenv("DEFAULT_PROTOCOL", "ISO_9141_2")
    enable_hardware: bool = os.getenv("ENABLE_HARDWARE", "false").lower() == "true"
    enable_phone_live_bridge: bool = os.getenv("ENABLE_PHONE_LIVE_BRIDGE", "false").lower() == "true"

    obdlink_port: str = os.getenv("OBDLINK_PORT", "/dev/tty.usbserial-0001")
    obdlink_baud: int = int(os.getenv("OBDLINK_BAUD", "115200"))
    obdlink_timeout_seconds: float = float(os.getenv("OBDLINK_TIMEOUT_SECONDS", "1.0"))

    lm_studio_base_url: str = os.getenv("LM_STUDIO_BASE_URL", "http://127.0.0.1:1234")
    lm_studio_model: str = os.getenv("LM_STUDIO_MODEL", "local-model")

    openai_api_key: str = os.getenv("OPENAI_API_KEY", "")
    openai_model: str = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
    openai_base_url: str = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")


settings = Settings()
