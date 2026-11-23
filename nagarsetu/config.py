"""Static configuration: policy, wards, crews, geo helpers, .env loading."""
from __future__ import annotations

import json
import math
import os
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"


def load_dotenv(path: Path | None = None) -> None:
    """Minimal .env loader. Real environment variables always win."""
    path = path or ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


@lru_cache(maxsize=1)
def policy() -> dict:
    return json.loads((ROOT / "policy.json").read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def city() -> dict:
    return json.loads((DATA / "wards.json").read_text(encoding="utf-8"))


def wards() -> list[dict]:
    return city()["wards"]


def ward_by_id(ward_id: str) -> dict | None:
    return next((w for w in wards() if w["id"] == ward_id), None)


def crews() -> list[dict]:
    """Deterministic crew roster: N crews per department per zone."""
    zones = sorted({w["zone"] for w in wards()})
    out = []
    for dept, n in city()["crews_per_dept_zone"].items():
        for zone in zones:
            for i in range(1, n + 1):
                out.append({"id": f"{dept}-{zone[:3].upper()}-{i}", "dept": dept, "zone": zone})
    return out


def haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    r = 6_371_000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def nearest_ward(lat: float, lng: float) -> dict:
    return min(wards(), key=lambda w: haversine_m(lat, lng, w["lat"], w["lng"]))


def in_city(lat: float, lng: float) -> bool:
    """Rough Indore bounding box, so a mis-tapped map point is caught."""
    return 22.55 <= lat <= 22.85 and 75.70 <= lng <= 76.00
