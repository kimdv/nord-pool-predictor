from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings


class WeatherPoint:
    __slots__ = ("id", "name", "lat", "lon")

    def __init__(self, id: str, name: str, lat: float, lon: float) -> None:
        self.id = id
        self.name = name
        self.lat = lat
        self.lon = lon

    def __repr__(self) -> str:
        return f"WeatherPoint({self.id}, {self.lat}, {self.lon})"


class AreaConfig:
    __slots__ = ("code", "label", "weather_points")

    def __init__(self, code: str, label: str, weather_points: list[WeatherPoint]) -> None:
        self.code = code
        self.label = label
        self.weather_points = weather_points

    def __repr__(self) -> str:
        return f"AreaConfig({self.code}, points={len(self.weather_points)})"


def _load_areas_yaml(path: Path) -> dict[str, AreaConfig]:
    with open(path) as f:
        raw: dict[str, Any] = yaml.safe_load(f)

    areas: dict[str, AreaConfig] = {}
    for code, cfg in raw.get("areas", {}).items():
        points = [
            WeatherPoint(id=p["id"], name=p["name"], lat=p["lat"], lon=p["lon"])
            for p in cfg.get("weather_points", [])
        ]
        areas[code] = AreaConfig(code=code, label=cfg.get("label", code), weather_points=points)
    return areas


class Settings(BaseSettings):
    database_url: str = "postgresql+asyncpg://nordpool:nordpool@localhost:5432/nordpool"
    cors_allowed_origins: str = "http://localhost:3000"
    model_dir: str = "/data/models"

    retention_weather_obs_days: int = 180
    retention_weather_fc_days: int = 365
    retention_production_days: int = 365
    retention_crossborder_days: int = 365
    retention_forecast_days: int = 365

    training_window_days: int = 365

    areas_yaml_path: str = Field(default="")

    _areas: dict[str, AreaConfig] | None = None

    model_config = {"env_file": ".env", "extra": "ignore"}

    @model_validator(mode="after")
    def _resolve_areas_path(self) -> Settings:
        if not self.areas_yaml_path:
            candidates = [
                Path("areas.yaml"),
                Path(__file__).resolve().parent.parent.parent / "areas.yaml",
            ]
            for c in candidates:
                if c.exists():
                    self.areas_yaml_path = str(c)
                    break
        return self

    @property
    def areas(self) -> dict[str, AreaConfig]:
        if self._areas is None:
            p = Path(self.areas_yaml_path)
            if p.exists():
                self._areas = _load_areas_yaml(p)
            else:
                self._areas = {}
        return self._areas

    @property
    def area_codes(self) -> list[str]:
        return list(self.areas.keys())

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.cors_allowed_origins.split(",") if o.strip()]

    @property
    def sync_database_url(self) -> str:
        return self.database_url.replace("+asyncpg", "")


@lru_cache
def get_settings() -> Settings:
    return Settings()
