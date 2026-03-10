from __future__ import annotations

import json
from collections import Counter
from pathlib import Path


def parse_obd_log_lines(lines: list[str]) -> list[dict]:
    parsed: list[dict] = []
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue

        # Expected format: timestamp|command|raw_response
        parts = line.split("|", maxsplit=2)
        if len(parts) != 3:
            parsed.append({"type": "invalid", "raw": line})
            continue

        ts, command, raw_response = parts
        parsed.append(
            {
                "type": "obd_read",
                "timestamp": ts,
                "command": command.strip().upper(),
                "raw_response": raw_response.strip(),
            }
        )
    return parsed


def summarize_records(records: list[dict]) -> dict:
    mode_counter: Counter[str] = Counter()
    invalid_count = 0

    for item in records:
        if item.get("type") == "obd_read":
            mode_counter[item["command"][:2]] += 1
        else:
            invalid_count += 1

    return {
        "total_records": len(records),
        "invalid_records": invalid_count,
        "mode_counts": dict(mode_counter),
    }


def run_preprocessing(input_path: Path, output_dir: Path) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    lines = input_path.read_text(encoding="utf-8").splitlines()
    records = parse_obd_log_lines(lines)
    summary = summarize_records(records)

    parsed_path = output_dir / "parsed_records.json"
    summary_path = output_dir / "summary.json"
    parsed_path.write_text(json.dumps(records, indent=2), encoding="utf-8")
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    return {
        "parsed_path": str(parsed_path),
        "summary_path": str(summary_path),
        "summary": summary,
    }
