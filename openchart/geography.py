"""Small, dependency-free geographic projection helpers for chart rendering."""

from __future__ import annotations

from dataclasses import dataclass
import math


WGS84_SEMI_MAJOR_AXIS_M = 6_378_137.0
WGS84_INVERSE_FLATTENING = 298.257_223_563
FEET_PER_METER = 3.280_839_895_013_123


class GeographicProjectionError(ValueError):
    """Raised when an authored theater projection is unsupported or malformed."""


@dataclass(frozen=True)
class TransverseMercatorProjection:
    """The WGS84 transverse-Mercator form authored by BMS theaters."""

    central_meridian_degrees: float
    latitude_of_origin_degrees: float
    scale_factor: float
    false_easting_m: float
    false_northing_m: float

    @classmethod
    def from_proj_string(cls, value: str) -> TransverseMercatorProjection:
        """Parse the constrained PROJ string form used by BMS terrain metadata."""

        if not isinstance(value, str) or not value.strip():
            raise GeographicProjectionError("projection string must not be empty")
        parameters: dict[str, str] = {}
        for token in value.split():
            if not token.startswith("+"):
                raise GeographicProjectionError(
                    f"invalid projection token {token!r}"
                )
            key, separator, raw_value = token[1:].partition("=")
            if not separator or not key or not raw_value:
                raise GeographicProjectionError(
                    f"invalid projection token {token!r}"
                )
            if key in parameters:
                raise GeographicProjectionError(
                    f"duplicate projection parameter +{key}"
                )
            parameters[key] = raw_value

        if parameters.get("proj", "").casefold() != "tmerc":
            raise GeographicProjectionError("only +proj=tmerc is supported")
        if parameters.get("ellps", "").casefold() != "wgs84":
            raise GeographicProjectionError("only +ellps=WGS84 is supported")
        if parameters.get("units", "").casefold() != "m":
            raise GeographicProjectionError("only +units=m is supported")

        def number(name: str, *, default: float | None = None) -> float:
            raw = parameters.get(name)
            if raw is None:
                if default is not None:
                    return default
                raise GeographicProjectionError(
                    f"projection is missing +{name}"
                )
            try:
                parsed = float(raw)
            except ValueError as exc:
                raise GeographicProjectionError(
                    f"projection +{name} is not numeric: {raw!r}"
                ) from exc
            if not math.isfinite(parsed):
                raise GeographicProjectionError(
                    f"projection +{name} must be finite"
                )
            return parsed

        scale_raw = parameters.get("k", parameters.get("k_0"))
        if scale_raw is None:
            raise GeographicProjectionError("projection is missing +k")
        try:
            scale_factor = float(scale_raw)
        except ValueError as exc:
            raise GeographicProjectionError(
                f"projection +k is not numeric: {scale_raw!r}"
            ) from exc
        if not math.isfinite(scale_factor) or scale_factor <= 0.0:
            raise GeographicProjectionError(
                "projection +k must be a positive finite number"
            )

        return cls(
            central_meridian_degrees=number("lon_0"),
            latitude_of_origin_degrees=number("lat_0", default=0.0),
            scale_factor=scale_factor,
            false_easting_m=number("x_0"),
            false_northing_m=number("y_0"),
        )

    def project(self, latitude: float, longitude: float) -> tuple[float, float]:
        """Return projected easting and northing in meters."""

        latitude_radians = math.radians(latitude)
        longitude_delta = math.radians(
            longitude - self.central_meridian_degrees
        )
        origin_radians = math.radians(self.latitude_of_origin_degrees)
        eccentricity_squared = _wgs84_eccentricity_squared()
        second_eccentricity_squared = (
            eccentricity_squared / (1.0 - eccentricity_squared)
        )
        sine = math.sin(latitude_radians)
        cosine = math.cos(latitude_radians)
        tangent = math.tan(latitude_radians)
        radius = WGS84_SEMI_MAJOR_AXIS_M / math.sqrt(
            1.0 - eccentricity_squared * sine * sine
        )
        tangent_squared = tangent * tangent
        eccentricity_term = second_eccentricity_squared * cosine * cosine
        longitude_term = cosine * longitude_delta
        meridional_arc = _meridional_arc(latitude_radians)
        origin_arc = _meridional_arc(origin_radians)
        scale = self.scale_factor

        easting = self.false_easting_m + scale * radius * (
            longitude_term
            + (1.0 - tangent_squared + eccentricity_term)
            * longitude_term**3
            / 6.0
            + (
                5.0
                - 18.0 * tangent_squared
                + tangent_squared**2
                + 72.0 * eccentricity_term
                - 58.0 * second_eccentricity_squared
            )
            * longitude_term**5
            / 120.0
        )
        northing = self.false_northing_m + scale * (
            meridional_arc
            - origin_arc
            + radius
            * tangent
            * (
                longitude_term**2 / 2.0
                + (
                    5.0
                    - tangent_squared
                    + 9.0 * eccentricity_term
                    + 4.0 * eccentricity_term**2
                )
                * longitude_term**4
                / 24.0
                + (
                    61.0
                    - 58.0 * tangent_squared
                    + tangent_squared**2
                    + 600.0 * eccentricity_term
                    - 330.0 * second_eccentricity_squared
                )
                * longitude_term**6
                / 720.0
            )
        )
        return easting, northing

    def unproject(self, easting_m: float, northing_m: float) -> tuple[float, float]:
        """Return latitude and longitude in decimal degrees."""

        eccentricity_squared = _wgs84_eccentricity_squared()
        second_eccentricity_squared = (
            eccentricity_squared / (1.0 - eccentricity_squared)
        )
        origin_arc = _meridional_arc(
            math.radians(self.latitude_of_origin_degrees)
        )
        meridional_arc = origin_arc + (
            northing_m - self.false_northing_m
        ) / self.scale_factor
        eccentricity_fourth = eccentricity_squared**2
        eccentricity_sixth = eccentricity_squared**3
        footprint_denominator = WGS84_SEMI_MAJOR_AXIS_M * (
            1.0
            - eccentricity_squared / 4.0
            - 3.0 * eccentricity_fourth / 64.0
            - 5.0 * eccentricity_sixth / 256.0
        )
        footprint_base = meridional_arc / footprint_denominator
        root = math.sqrt(1.0 - eccentricity_squared)
        first_eccentricity = (1.0 - root) / (1.0 + root)
        footprint = (
            footprint_base
            + (3.0 * first_eccentricity / 2.0 - 27.0 * first_eccentricity**3 / 32.0)
            * math.sin(2.0 * footprint_base)
            + (21.0 * first_eccentricity**2 / 16.0 - 55.0 * first_eccentricity**4 / 32.0)
            * math.sin(4.0 * footprint_base)
            + 151.0 * first_eccentricity**3 / 96.0
            * math.sin(6.0 * footprint_base)
            + 1097.0 * first_eccentricity**4 / 512.0
            * math.sin(8.0 * footprint_base)
        )

        sine = math.sin(footprint)
        cosine = math.cos(footprint)
        tangent = math.tan(footprint)
        denominator = 1.0 - eccentricity_squared * sine * sine
        radius = WGS84_SEMI_MAJOR_AXIS_M / math.sqrt(denominator)
        meridional_radius = (
            WGS84_SEMI_MAJOR_AXIS_M
            * (1.0 - eccentricity_squared)
            / denominator**1.5
        )
        tangent_squared = tangent * tangent
        eccentricity_term = second_eccentricity_squared * cosine * cosine
        normalized_easting = (
            (easting_m - self.false_easting_m)
            / (radius * self.scale_factor)
        )

        latitude_radians = footprint - (
            radius * tangent / meridional_radius
        ) * (
            normalized_easting**2 / 2.0
            - (
                5.0
                + 3.0 * tangent_squared
                + 10.0 * eccentricity_term
                - 4.0 * eccentricity_term**2
                - 9.0 * second_eccentricity_squared
            )
            * normalized_easting**4
            / 24.0
            + (
                61.0
                + 90.0 * tangent_squared
                + 298.0 * eccentricity_term
                + 45.0 * tangent_squared**2
                - 252.0 * second_eccentricity_squared
                - 3.0 * eccentricity_term**2
            )
            * normalized_easting**6
            / 720.0
        )
        longitude_radians = math.radians(
            self.central_meridian_degrees
        ) + (
            normalized_easting
            - (1.0 + 2.0 * tangent_squared + eccentricity_term)
            * normalized_easting**3
            / 6.0
            + (
                5.0
                - 2.0 * eccentricity_term
                + 28.0 * tangent_squared
                - 3.0 * eccentricity_term**2
                + 8.0 * second_eccentricity_squared
                + 24.0 * tangent_squared**2
            )
            * normalized_easting**5
            / 120.0
        ) / cosine
        return math.degrees(latitude_radians), math.degrees(longitude_radians)


def _wgs84_eccentricity_squared() -> float:
    flattening = 1.0 / WGS84_INVERSE_FLATTENING
    return flattening * (2.0 - flattening)


def _meridional_arc(latitude_radians: float) -> float:
    eccentricity_squared = _wgs84_eccentricity_squared()
    eccentricity_fourth = eccentricity_squared**2
    eccentricity_sixth = eccentricity_squared**3
    return WGS84_SEMI_MAJOR_AXIS_M * (
        (
            1.0
            - eccentricity_squared / 4.0
            - 3.0 * eccentricity_fourth / 64.0
            - 5.0 * eccentricity_sixth / 256.0
        )
        * latitude_radians
        - (
            3.0 * eccentricity_squared / 8.0
            + 3.0 * eccentricity_fourth / 32.0
            + 45.0 * eccentricity_sixth / 1024.0
        )
        * math.sin(2.0 * latitude_radians)
        + (
            15.0 * eccentricity_fourth / 256.0
            + 45.0 * eccentricity_sixth / 1024.0
        )
        * math.sin(4.0 * latitude_radians)
        - 35.0 * eccentricity_sixth / 3072.0
        * math.sin(6.0 * latitude_radians)
    )
