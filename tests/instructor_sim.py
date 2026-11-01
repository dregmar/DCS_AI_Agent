"""A tiny flight simulator for Instructor Mode tests: moves a helicopter in metres, feeds the real
Instructor one Sample per 0.25 s (Export.lua's 4 Hz) with lat/lon and fuel, like main.py's observer."""
import math

from instructor import geometry
from instructor.models import Sample

LAT0, LON0 = 42.0, 41.0


def ll_of(n: float, e: float):
    return (LAT0 + n / geometry.M_PER_DEG, LON0 + e / (geometry.M_PER_DEG * math.cos(math.radians(LAT0))))


class Sim:
    def __init__(self, ins, aircraft="UH-1H", fuel=1200.0, skid_ft=5.0):
        self.ins, self.ac = ins, aircraft
        self.t, self.n, self.e, self.agl, self.fuel, self.skid = 0.0, 0.0, 0.0, skid_ft, fuel, skid_ft

    def snapshot(self) -> dict:
        """What the instructor's handle() reads (state_store.snapshot() in the program)."""
        return {"aircraft": self.ac, "t_model": self.t, "telemetry_age": 0.0, "my_ll": list(ll_of(self.n, self.e)),
                "vel": [0.0, 0.0, 0.0], "agl_ft": self.agl, "vs_fpm": 0.0, "pos": [self.n, self.e, 100.0]}

    def step(self, vn=0.0, ve=0.0, vs_fpm=0.0, dt=0.25, burn_lbs_hr=600.0, ground=False, count=1):
        for _ in range(count):
            self.n += vn * dt
            self.e += ve * dt
            self.agl = self.skid if ground else max(self.skid, self.agl + vs_fpm / 60.0 * dt)
            if not (ground and not vn and not ve):
                self.fuel -= burn_lbs_hr / 3600.0 * dt
            hdg = math.degrees(math.atan2(ve, vn)) % 360 if (vn or ve) else 0.0
            self.ins.tick(Sample(self.t, self.ac, (self.n, self.e, 100.0), (vn, ve, vs_fpm / 196.85),
                                 float(round(self.agl)), 0.0 if ground else vs_fpm, hdg, math.hypot(vn, ve) * 1.94384,
                                 ll_of(self.n, self.e), round(self.fuel, 1)))
            self.t += dt

    def sit(self, seconds=4.0):
        self.step(ground=True, count=int(seconds / 0.25))

    def good_approach(self, from_n=1200.0, agl=300.0):
        """South onto (0, 0): slowing to a hover over the point, sinking gently, then on the ground."""
        self.n, self.e, self.agl = from_n, 0.0, agl
        while self.n > 0.5:
            v = max(1.0, min(25.0, self.n / 25.0 + 1.0))
            vs = -max(0.0, (self.agl - self.skid - self.n * 0.08)) * 6
            self.step(vn=-v, vs_fpm=max(-700.0, min(0.0, vs)))
        self.sit(3.0)
