"""Experimental liftoff at full AB, sea-level ISA and authored takeoff pitch.

Lift plus vertical thrust supports takeoff gross weight. Pitch approximates AoA;
the full-TEF table is an additive lift increment. No ground effect, trim/rotation
dynamics, advanced thrust corrections or separate CFT/store lift increments.
"""
from __future__ import annotations

from dataclasses import dataclass
import math

from lib.cam.opencam.aircraft_data import AircraftSimulationData
from lib.fuel import _Table, _bracket, _number
from lib.route_fuel import air_state

_REFERENCE_MACH, _, _REFERENCE_Q = air_state(0, 100)
_KCAS_PER_MACH = 100 / _REFERENCE_MACH


@dataclass(frozen=True)
class LiftoffPerformance:
    q_area_per_mach2: float
    mach: tuple[float, ...]
    lift: tuple[float, ...]
    vertical_thrust: tuple[float, ...]

    @classmethod
    def from_simulation(cls, sim: AircraftSimulationData) -> LiftoffPerformance:
        if sim.fm_type != "FM_AFM":
            raise ValueError("AFM liftoff tables required")
        area = _number(sim.airframe.wing_area_sqft, "wing area", positive=True)
        alpha = _number(sim.takeoff_pitch_deg, "takeoff pitch", positive=True)
        if alpha >= 30:
            raise ValueError("Takeoff pitch range")
        tef = sim.flaps.get("tef", {})
        flap_fraction = (_number(tef.get("tef_take_off"), "takeoff TEF")
                         / _number(tef.get("tef_max_angle"), "maximum TEF", positive=True))
        if not 0 <= flap_fraction <= 1:
            raise ValueError("Takeoff TEF range")
        aero = sim.aerodynamics.get("aero_new2")
        if aero is None:
            raise ValueError("Liftoff lift tables required")
        cl = _Table.read(aero.tables.get("clift_table"),
                         aero.breakpoints.get("mach_bp"), aero.breakpoints.get("alpha_bp"))
        flap = _Table.read(aero.tables.get("c_lift_tef_table"),
                           aero.breakpoints.get("mach_tef_bp"), aero.breakpoints.get("alpha_tef_bp"))
        correction = _Table.read(aero.tables.get("perc_lift_table"),
                                 aero.breakpoints.get("beta_perc_bp"), aero.breakpoints.get("alpha_bp"))
        lift_factor = correction.at(0, alpha)
        idle = _Table.read(sim.engine.tables.get("alt_mach_thrust_idle_table"))
        thrust = _Table.read(sim.engine.tables.get("alt_mach_thrust_ab_table"), idle.x, idle.y)
        scale = (_number(sim.engine.number_of_engines, "engine count", positive=True)
                 * _number(sim.engine.parameters.get("thrust_factor"), "thrust factor", positive=True)
                 * math.sin(math.radians(alpha)))
        low, high = (v / _KCAS_PER_MACH for v in (70., 330.))
        mach = tuple(sorted({low, high, *(m for m in (*cl.x, *flap.x, *thrust.y) if low < m < high)}))
        lift = tuple(_number(cl.at(m, alpha) * lift_factor + flap.at(m, alpha) * flap_fraction,
                             "takeoff lift coefficient", positive=True) for m in mach)
        vertical = tuple(_number(thrust.at(0, m) * scale, "vertical AB thrust", positive=True) for m in mach)
        result = cls(_REFERENCE_Q / _REFERENCE_MACH ** 2 * area, mach, lift, vertical)

        # At fixed alpha each table is linear in Mach between these nodes.
        # Support is cubic: q_area * M^2 * (a*M+b) + c*M+d. Check its
        # derivative at both endpoints and its possible interior extremum,
        # so bisection cannot choose an ambiguous/non-increasing branch.
        for i, (m0, m1) in enumerate(zip(mach, mach[1:])):
            a = (lift[i + 1] - lift[i]) / (m1 - m0)
            b = lift[i] - a * m0
            c = (vertical[i + 1] - vertical[i]) / (m1 - m0)
            candidates = [m0, m1]
            if a and m0 < -b / (3 * a) < m1:
                candidates.append(-b / (3 * a))
            if any(result.q_area_per_mach2 * (3 * a * m * m + 2 * b * m) + c <= 0
                   for m in candidates):
                raise ValueError("Non-increasing liftoff support")
        return result

    def support(self, mach: float) -> float:
        i, fraction = _bracket(self.mach, mach)
        cl = self.lift[i] + fraction * (self.lift[i + 1] - self.lift[i])
        thrust = self.vertical_thrust[i] + fraction * (self.vertical_thrust[i + 1] - self.vertical_thrust[i])
        return self.q_area_per_mach2 * mach ** 2 * cl + thrust

    def speed_kcas(self, gross_weight_lb: float) -> float:
        weight = _number(gross_weight_lb, "takeoff gross weight", positive=True)
        low, high = self.mach[0], self.mach[-1]
        if not self.support(low) <= weight <= self.support(high):
            raise ValueError("Liftoff speed outside 70-330 KCAS")
        for _ in range(60):
            middle = (low + high) / 2
            if self.support(middle) < weight:
                low = middle
            else:
                high = middle
        return (low + high) / 2 * _KCAS_PER_MACH
