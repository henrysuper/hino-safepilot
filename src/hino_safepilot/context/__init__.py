"""External-context contracts, leakage-safe joins, parsers, and injected providers."""

from .models import (
    ContextAvailability,
    ContextBundle,
    ContextQuery,
    DisasterContextSnapshot,
    TrafficContextSnapshot,
    WeatherContextSnapshot,
)
from .providers import ContextAggregator, ContextProvider, NullContextProvider, SyntheticContextProvider
from .cache import ContextCache
from .cwa import CWAContextProvider, parse_cwa_rainfall_observations
from .joining import ContextObservation, haversine_distance_m, nearest_valid_observation, normalize_timestamp
from .tdx import TDXContextProvider, parse_tdx_vd_live

__all__ = [
    "ContextAggregator",
    "ContextCache",
    "ContextAvailability",
    "ContextBundle",
    "ContextQuery",
    "ContextObservation",
    "ContextProvider",
    "CWAContextProvider",
    "DisasterContextSnapshot",
    "NullContextProvider",
    "SyntheticContextProvider",
    "TDXContextProvider",
    "TrafficContextSnapshot",
    "WeatherContextSnapshot",
    "haversine_distance_m",
    "nearest_valid_observation",
    "normalize_timestamp",
    "parse_cwa_rainfall_observations",
    "parse_tdx_vd_live",
]
