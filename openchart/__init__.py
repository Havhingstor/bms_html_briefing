"""Falcon BMS airport-chart generation."""

from .source import AirportData, load_airports

__version__ = "0.3.1"

__all__ = ["__version__", "AirportData", "load_airports"]
