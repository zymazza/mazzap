"""Country adapter registry for the NATO pack.

Adapters hide country-specific source discovery and fetching behind one small
contract used by fetch_nato.py:

  coverage(aoi) -> dict
  fetch_elevation(aoi, out_dir, resolution) -> {"dtm": path, "dsm": path}
  prepare_chm_inputs(data_dir, elevation) -> {"dtm": path, "dsm": path, "chm": path}
  fetch_imagery(aoi, out_dir, footprint, px_per_m) -> {"rgbn": path, ...}
  fetch_forest(aoi, out_dir, data_dir) -> optional layer/source dict
  fetch_landcover(aoi, out_dir, data_dir) -> optional layer/source dict
  provenance() -> dict
  attribution() -> list[str]

The Netherlands, Norway, Spain, Belgium, Czechia, Denmark, Estonia, Finland,
France, Latvia, Luxembourg, Poland, Slovakia, and Sweden are implemented in this pack slice. The other NATO members are intentionally
present as stubs so the CLI can fail clearly instead of silently guessing a
national source.
"""

from dataclasses import dataclass


class AdapterUnavailable(NotImplementedError):
    """Raised when a registered country has no implemented adapter yet."""


@dataclass(frozen=True)
class StubAdapter:
    alpha2: str
    alpha3: str
    name: str
    tier: str

    def _missing(self):
        raise AdapterUnavailable(
            f"{self.name} ({self.alpha3}) is registered in the NATO pack "
            "but its national source adapter is not implemented yet."
        )

    def coverage(self, aoi):
        self._missing()

    def fetch_elevation(self, aoi, out_dir, resolution=0.5):
        self._missing()

    def prepare_chm_inputs(self, data_dir, elevation, resolution=0.5):
        self._missing()

    def fetch_imagery(self, aoi, out_dir, footprint, px_per_m=2):
        self._missing()

    def fetch_forest(self, aoi, out_dir, data_dir):
        return None

    def fetch_landcover(self, aoi, out_dir, data_dir):
        return None

    def provenance(self):
        return {"country": self.alpha3, "status": "stub", "tier": self.tier}

    def attribution(self):
        return []


_MEMBERS = {
    "AL": ("ALB", "Albania", "C"),
    "BE": ("BEL", "Belgium", "A"),
    "BG": ("BGR", "Bulgaria", "C"),
    "CA": ("CAN", "Canada", "B"),
    "HR": ("HRV", "Croatia", "B"),
    "CZ": ("CZE", "Czechia", "A"),
    "DK": ("DNK", "Denmark", "A"),
    "EE": ("EST", "Estonia", "A"),
    "FI": ("FIN", "Finland", "A"),
    "FR": ("FRA", "France", "A"),
    "DE": ("DEU", "Germany", "B"),
    "GR": ("GRC", "Greece", "C"),
    "HU": ("HUN", "Hungary", "C"),
    "IS": ("ISL", "Iceland", "B"),
    "IT": ("ITA", "Italy", "B"),
    "LV": ("LVA", "Latvia", "A"),
    "LT": ("LTU", "Lithuania", "B"),
    "LU": ("LUX", "Luxembourg", "A"),
    "ME": ("MNE", "Montenegro", "C"),
    "MK": ("MKD", "North Macedonia", "C"),
    "NO": ("NOR", "Norway", "A"),
    "PL": ("POL", "Poland", "A"),
    "PT": ("PRT", "Portugal", "B"),
    "RO": ("ROU", "Romania", "B"),
    "SK": ("SVK", "Slovakia", "A"),
    "SI": ("SVN", "Slovenia", "B"),
    "ES": ("ESP", "Spain", "A"),
    "SE": ("SWE", "Sweden", "A"),
    "TR": ("TUR", "Turkey", "C"),
    "GB": ("GBR", "United Kingdom", "B"),
    "US": ("USA", "United States", "S"),
}

_ALPHA3_TO_ALPHA2 = {alpha3: alpha2 for alpha2, (alpha3, _name, _tier) in _MEMBERS.items()}
_ALIASES = {"UK": "GB", "NLD": "NL", "NL": "NL", "USA": "US"}
_REGISTRY = {}


def _stub(alpha2):
    alpha3, name, tier = _MEMBERS[alpha2]
    return StubAdapter(alpha2=alpha2, alpha3=alpha3, name=name, tier=tier)


def _normalize(code):
    key = (code or "").strip().upper()
    if key in _ALIASES:
        key = _ALIASES[key]
    if key in _ALPHA3_TO_ALPHA2:
        key = _ALPHA3_TO_ALPHA2[key]
    return key


def _load_nl():
    from .nl import NetherlandsAdapter

    return NetherlandsAdapter()


def _load_no():
    from .no import NorwayAdapter

    return NorwayAdapter()


def _load_es():
    from .es import SpainAdapter

    return SpainAdapter()


def _load_be():
    from .be import BelgiumAdapter

    return BelgiumAdapter()


def _load_cz():
    from .cz import CzechiaAdapter

    return CzechiaAdapter()


def _load_dk():
    from .dk import DenmarkAdapter

    return DenmarkAdapter()


def _load_ee():
    from .ee import EstoniaAdapter

    return EstoniaAdapter()


def _load_fi():
    from .fi import FinlandAdapter

    return FinlandAdapter()


def _load_fr():
    from .fr import FranceAdapter

    return FranceAdapter()


def _load_lv():
    from .lv import LatviaAdapter

    return LatviaAdapter()


def _load_lu():
    from .lu import LuxembourgAdapter

    return LuxembourgAdapter()


def _load_pl():
    from .pl import PolandAdapter

    return PolandAdapter()


def _load_sk():
    from .sk import SlovakiaAdapter

    return SlovakiaAdapter()


def _load_se():
    from .se import SwedenAdapter

    return SwedenAdapter()


def get_adapter(code):
    """Return the country adapter for a two- or three-letter ISO code."""
    key = _normalize(code)
    if key in _REGISTRY:
        return _REGISTRY[key]
    if key == "NL":
        adapter = _load_nl()
    elif key == "NO":
        adapter = _load_no()
    elif key == "ES":
        adapter = _load_es()
    elif key == "BE":
        adapter = _load_be()
    elif key == "CZ":
        adapter = _load_cz()
    elif key == "DK":
        adapter = _load_dk()
    elif key == "EE":
        adapter = _load_ee()
    elif key == "FI":
        adapter = _load_fi()
    elif key == "FR":
        adapter = _load_fr()
    elif key == "LV":
        adapter = _load_lv()
    elif key == "LU":
        adapter = _load_lu()
    elif key == "PL":
        adapter = _load_pl()
    elif key == "SK":
        adapter = _load_sk()
    elif key == "SE":
        adapter = _load_se()
    elif key in _MEMBERS:
        adapter = _stub(key)
    else:
        raise KeyError(f"{code!r} is not a NATO country code in this pack")
    _REGISTRY[key] = adapter
    _REGISTRY[getattr(adapter, "alpha3", key)] = adapter
    return adapter


def adapters():
    """Materialize the registry for all NATO members."""
    return {alpha2: get_adapter(alpha2) for alpha2 in sorted(_MEMBERS)}


__all__ = ["AdapterUnavailable", "StubAdapter", "adapters", "get_adapter"]
