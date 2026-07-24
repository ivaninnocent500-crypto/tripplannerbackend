"""
Weather Engine — migrated to `monthly_weather_patterns`. This is a real
upgrade over the previous fixture-based engine (which used one generic
seasonal fixture set for all destinations, explicitly flagged as
inaccurate for anything outside Kenya's highland/savanna climate). The
new schema has real per-destination, per-month climate data
(avg_high_temp_c, avg_low_temp_c, avg_rainfall_mm, rainy_days_count),
so this engine can now be destination-accurate rather than one-size-fits-all.

FALLBACK BEHAVIOR: if a destination has no monthly_weather_patterns row
yet (not populated), falls back to the same generic seasonal fixture as
before — an honest degradation, not a crash, while the knowledge base
grows from 5 to ~30 destinations.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any
from sqlalchemy import Column, Text, Numeric, Integer, ForeignKey
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Session

from app.db.destinations import resolve_slugs_to_ids
from app.db.models_v2 import Base, gen_uuid


class MonthlyWeatherPattern(Base):
    __tablename__ = "monthly_weather_patterns"
    __table_args__ = {"extend_existing": True}

    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    destination_id = Column(UUID(as_uuid=False), ForeignKey("travel_places.id", ondelete="CASCADE"))
    month = Column(Text, nullable=False)
    avg_high_temp_c = Column(Numeric(4, 1))
    avg_low_temp_c = Column(Numeric(4, 1))
    avg_rainfall_mm = Column(Numeric(6, 1))
    rainy_days_count = Column(Integer)


@dataclass
class DayForecast:
    day: int
    temp_min_c: int
    temp_max_c: int
    rain_probability_pct: int
    cloud_cover_pct: int
    conditions: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# Fallback fixture, used only when no real data exists yet for a
# destination — same values as the pre-migration WeatherEngine, kept
# identical so behavior doesn't silently change for un-populated
# destinations during the knowledge base's growth period.
_SEASONAL_FIXTURES: dict[str, dict[str, Any]] = {
    "dry": {"temp_min": 13, "temp_max": 29, "rain_pct": 10, "cloud_pct": 20, "conditions": "Mostly sunny"},
    "shoulder": {"temp_min": 14, "temp_max": 27, "rain_pct": 35, "cloud_pct": 45, "conditions": "Partly cloudy"},
    "rainy": {"temp_min": 15, "temp_max": 24, "rain_pct": 65, "cloud_pct": 70, "conditions": "Scattered showers"},
}
_DRY_MONTHS = {"june", "july", "august", "september", "january", "february"}
_RAINY_MONTHS = {"april", "may", "november"}


def _season_for_month(month: str) -> str:
    m = month.lower()
    if m in _DRY_MONTHS:
        return "dry"
    if m in _RAINY_MONTHS:
        return "rainy"
    return "shoulder"


class WeatherEngine:
    def __init__(self, db: Session | None = None):
        # db is optional to preserve the old no-arg constructor signature
        # some callers may still use; real data lookup only happens if a
        # session is provided.
        self.db = db

    def fetch(self, destination_slugs: list[str], month: str, days: int) -> list[DayForecast]:
        real_pattern = self._get_real_pattern(destination_slugs, month) if self.db else None

        if real_pattern is not None:
            return self._forecast_from_real_pattern(real_pattern, days)

        return self._forecast_from_fixture(month, days)

    def _get_real_pattern(self, destination_slugs: list[str], month: str) -> MonthlyWeatherPattern | None:
        if not destination_slugs or not month:
            return None
        slug_to_id = resolve_slugs_to_ids(self.db, destination_slugs)
        if not slug_to_id:
            return None
        # Use the first resolved destination's pattern as representative
        # for the trip — matches the old engine's single-fixture-per-trip
        # simplification; a true per-leg forecast would need this called
        # per-destination-per-day, a reasonable future enhancement.
        first_id = next(iter(slug_to_id.values()))
        return (
            self.db.query(MonthlyWeatherPattern)
            .filter(
                MonthlyWeatherPattern.destination_id == first_id,
                MonthlyWeatherPattern.month == month.lower(),
            )
            .first()
        )

    def _forecast_from_real_pattern(self, pattern: MonthlyWeatherPattern, days: int) -> list[DayForecast]:
        temp_min = int(pattern.avg_low_temp_c) if pattern.avg_low_temp_c is not None else 15
        temp_max = int(pattern.avg_high_temp_c) if pattern.avg_high_temp_c is not None else 28
        rainy_days = pattern.rainy_days_count or 0
        rain_pct = min(100, int((rainy_days / 30.0) * 100)) if rainy_days else 15

        forecast = []
        for day_num in range(1, days + 1):
            variation = (day_num % 3) - 1
            forecast.append(DayForecast(
                day=day_num,
                temp_min_c=temp_min + variation,
                temp_max_c=temp_max + variation,
                rain_probability_pct=max(0, rain_pct + variation * 5),
                cloud_cover_pct=max(0, rain_pct + variation * 8),
                conditions="Mostly sunny" if rain_pct < 30 else "Partly cloudy",
            ))
        return forecast

    def _forecast_from_fixture(self, month: str, days: int) -> list[DayForecast]:
        season = _season_for_month(month) if month else "dry"
        fixture = _SEASONAL_FIXTURES[season]
        forecast = []
        for day_num in range(1, days + 1):
            variation = (day_num % 3) - 1
            forecast.append(DayForecast(
                day=day_num,
                temp_min_c=fixture["temp_min"] + variation,
                temp_max_c=fixture["temp_max"] + variation,
                rain_probability_pct=max(0, fixture["rain_pct"] + variation * 5),
                cloud_cover_pct=max(0, fixture["cloud_pct"] + variation * 8),
                conditions=fixture["conditions"],
            ))
        return forecast

    @staticmethod
    def summarise(forecast: list[DayForecast]) -> dict[str, Any]:
        if not forecast:
            return {"average_temp_c": 0, "temp_range_c": [0, 0], "average_rain_pct": 0,
                    "likely_rain_days_over_window": 0, "season_label": "Unknown"}

        avg_temp = round(sum((f.temp_min_c + f.temp_max_c) / 2 for f in forecast) / len(forecast))
        temp_min = min(f.temp_min_c for f in forecast)
        temp_max = max(f.temp_max_c for f in forecast)
        avg_rain = round(sum(f.rain_probability_pct for f in forecast) / len(forecast))
        likely_rain_days = sum(1 for f in forecast if f.rain_probability_pct > 60)
        avg_cloud = sum(f.cloud_cover_pct for f in forecast) / len(forecast)
        season_label = "Dry" if avg_cloud < 30 else "Rainy" if avg_cloud > 55 else "Shoulder"

        return {
            "average_temp_c": avg_temp, "temp_range_c": [temp_min, temp_max],
            "average_rain_pct": avg_rain, "likely_rain_days_over_window": likely_rain_days,
            "season_label": season_label,
        }
