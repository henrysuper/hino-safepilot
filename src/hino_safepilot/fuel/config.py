"""Validated configuration (config/fuel_v1.json)."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SegmentConfig(_Strict):
    length_km: float = Field(5.0, gt=0)
    candidate_lengths_km: list[float] = [3.0, 5.0, 10.0]
    tail_min_km: float = Field(3.0, gt=0)
    min_l_per_100km: float = 2.0
    max_l_per_100km: float = 120.0
    exposure_max_gap_seconds: float = 300.0
    moving_min_speed_kph: float = 3.0


class TimeBand(_Strict):
    name: str
    start_hour: int = Field(ge=0, le=23)
    end_hour: int = Field(ge=0, le=24)

    def contains(self, hour: int) -> bool:
        if self.start_hour < self.end_hour:
            return self.start_hour <= hour < self.end_hour
        return hour >= self.start_hour or hour < self.end_hour  # wraps midnight


class OsmConfig(_Strict):
    pbf_path: str
    index_path: str
    densify_spacing_m: float = Field(20.0, gt=0)
    max_match_distance_m: float = Field(50.0, gt=0)
    class_to_band: dict[str, str]


class DemConfig(_Strict):
    tif_path: str
    tfw_path: str
    crs: str = "EPSG:3826"
    nodata: float = -32767


class ProfileConfig(_Strict):
    spacing_m: float = Field(100.0, gt=0)
    smooth_points: int = Field(5, ge=1)


class GridConfig(_Strict):
    crs: str = "EPSG:3826"
    cell_m: float = Field(500.0, gt=0)


class SpeedLimitBands(_Strict):
    city_max: float = 50.0
    normal_max: float = 80.0


class MapsConfig(_Strict):
    shrinkage_k: float = Field(5.0, ge=0)
    free_flow_quantile: float = Field(0.85, gt=0, lt=1)
    free_flow_min_records: int = 20
    congestion_clip: tuple[float, float] = (0.05, 1.5)


class DriverTypeConfig(_Strict):
    names: list[str]
    min_trip_km: float = 10.0
    ratio_cap: float = 5.0
    min_cell_trips: int = 20

    @model_validator(mode="after")
    def _five(self) -> "DriverTypeConfig":
        if len(self.names) != 5 or "normal" not in self.names:
            raise ValueError("driver_types.names must list five types including 'normal'")
        return self


class DriversConfig(_Strict):
    """Virtual driver roster and personal profiles (此為模擬資料; see drivers.py)."""

    per_type: dict[str, int] = {"300": 20, "500": 10, "700": 10}
    new_driver_style_quantiles: list[float] = [0.15, 0.85, 0.5]  # one test-period starter per type (sorted type order)
    assignment_sigma: float = Field(0.15, gt=0)
    other_vehicle_weight: float = Field(0.3, ge=0)  # drivers of the series whose home vehicle is another truck
    profile_k_trips: float = Field(10.0, gt=0)
    profile_k_segments: float = Field(20.0, gt=0)  # per road type: w = segments / (segments + k)
    neighbours: int = Field(3, ge=1)
    min_neighbour_trips: int = Field(10, ge=1)
    multiplier_clip: tuple[float, float] = (0.2, 5.0)
    seed: int = 20261003


class ModelConfig(_Strict):
    device_preference: str = "cpu"
    random_seed: int = 20261002
    num_boost_round: int = 3000
    early_stopping_rounds: int = 150
    candidates: list[dict[str, float]]
    monotone_increasing: list[str] = []
    interval_quantiles: tuple[float, float] = (0.1, 0.9)
    route_split_folds: int = 5
    route_split_block_km: float = 5.0


class ScoreConfig(_Strict):
    base: float = 75.0
    scale: float = 100.0
    min_trip_km: float = 10.0


class MockConfig(_Strict):
    label: str = "此為模擬資料"
    fuel_price_twd_per_l: float = 29.0


class OrsConfig(_Strict):
    base_url: str
    max_alternative_km: float = 100.0
    alternatives: int = 3
    share_factor: float = 0.6
    weight_factor: float = 1.6
    avoid_toll_candidate: bool = True
    timeout_s: float = 40


class TdxConfig(_Strict):
    token_url: str
    base_url: str
    max_match_distance_m: float = 40.0
    max_congestion: float = 1.0
    timeout_s: float = 60


class FuelConfig(_Strict):
    version: str
    train_months: int = Field(8, ge=1)
    validation_months: int = Field(1, ge=1)
    segment: SegmentConfig
    time_bands: list[TimeBand]
    osm: OsmConfig
    dem: DemConfig
    profile: ProfileConfig
    grid: GridConfig
    speed_limit_bands: SpeedLimitBands
    maps: MapsConfig
    driver_types: DriverTypeConfig
    drivers: DriversConfig = DriversConfig()
    model: ModelConfig
    score: ScoreConfig
    mock: MockConfig
    ors: OrsConfig
    tdx: TdxConfig

    def time_band(self, hour: int) -> str:
        for band in self.time_bands:
            if band.contains(hour):
                return band.name
        raise ValueError(f"No time band covers hour {hour}")

    @property
    def time_band_names(self) -> list[str]:
        return [band.name for band in self.time_bands]


def load_config(path: Path) -> FuelConfig:
    return FuelConfig.model_validate_json(Path(path).read_text(encoding="utf-8"))
