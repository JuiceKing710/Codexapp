from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class VehicleImageResolution:
    vehicle_id: str
    year: int | None
    make: str
    model: str
    trim: str | None
    image_asset_path: str
    fallback_image_asset_path: str
    model_3d_ref: str | None
    resolved_from: str
    lookup_key_used: str | None


_DATA_PATH = Path(__file__).resolve().parent / "data" / "vehicle_images.json"


class VehicleImageLibrary:
    def __init__(self, data_path: Path = _DATA_PATH) -> None:
        payload = json.loads(data_path.read_text())
        self.vehicles = payload["vehicles"]
        self.vin_wmi_map = payload.get("vin_wmi_map", {})
        self.generic = payload["generic_placeholder"]

    def resolve(
        self,
        *,
        vin: str | None,
        vin_parse_status: str | None,
        manual_vehicle_id: str | None,
        active_vehicle_id: str | None,
        assignment_source: str | None,
    ) -> VehicleImageResolution:
        normalized_vin = (vin or "").strip().upper()
        if assignment_source == "auto_vin" and len(normalized_vin) == 17:
            match = self._resolve_by_vin(normalized_vin)
            if match:
                return self._to_resolution(match, "auto_vin", normalized_vin[:3])

        vin_failed = vin_parse_status in {"failed-manual-selection-required", "manual-selection", "not-run"}
        if vin_failed or assignment_source != "auto_vin":
            for candidate, source in (
                (manual_vehicle_id, "manual_selection"),
                (active_vehicle_id, "active_session_vehicle"),
            ):
                if candidate:
                    match = self._resolve_by_vehicle_id(candidate)
                    if match:
                        return self._to_resolution(match, source, candidate)
                    closest = self._resolve_closest(candidate)
                    if closest:
                        return self._to_resolution(closest, "closest_supported", candidate)

        return VehicleImageResolution(
            vehicle_id="generic_placeholder",
            year=None,
            make="Generic",
            model="Vehicle",
            trim=None,
            image_asset_path=self.generic["image_asset_path"],
            fallback_image_asset_path=self.generic["fallback_image_asset_path"],
            model_3d_ref=self.generic.get("model_3d_ref"),
            resolved_from="generic_placeholder",
            lookup_key_used=None,
        )

    def _resolve_by_vin(self, vin: str) -> dict | None:
        mapped_vehicle_id = self.vin_wmi_map.get(vin[:3])
        if not mapped_vehicle_id:
            return None
        return self._resolve_by_vehicle_id(mapped_vehicle_id)

    def _resolve_by_vehicle_id(self, vehicle_id: str) -> dict | None:
        return next((v for v in self.vehicles if v["vehicle_id"] == vehicle_id), None)

    def _resolve_closest(self, vehicle_id: str) -> dict | None:
        normalized = vehicle_id.lower()
        tokens = [part for part in normalized.replace("-", "_").split("_") if part]

        def score(entry: dict) -> int:
            entry_id = entry["vehicle_id"].lower()
            entry_tokens = entry_id.split("_")
            return sum(1 for token in tokens if token in entry_tokens)

        ranked = sorted(self.vehicles, key=score, reverse=True)
        return ranked[0] if ranked and score(ranked[0]) > 0 else None

    @staticmethod
    def _to_resolution(entry: dict, resolved_from: str, lookup_key: str | None) -> VehicleImageResolution:
        return VehicleImageResolution(
            vehicle_id=entry["vehicle_id"],
            year=entry.get("year"),
            make=entry["make"],
            model=entry["model"],
            trim=entry.get("trim"),
            image_asset_path=entry["image_asset_path"],
            fallback_image_asset_path=entry["fallback_image_asset_path"],
            model_3d_ref=entry.get("model_3d_ref"),
            resolved_from=resolved_from,
            lookup_key_used=lookup_key,
        )


vehicle_image_library = VehicleImageLibrary()
