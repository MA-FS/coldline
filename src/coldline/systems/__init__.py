"""In-memory business systems. There is no API that writes to plant control."""

from coldline.systems.plant import Sandbox, VersionConflict
from coldline.systems.sessions import SessionManager

__all__ = ["Sandbox", "SessionManager", "VersionConflict", "create_plant"]


def create_plant(seed: int = 42) -> Sandbox:
    """Create a new isolated PlantSystems sandbox at the demo start time."""
    return Sandbox(seed)
