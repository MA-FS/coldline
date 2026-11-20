"""Fictional shift fixtures, relative to the fixed local demo date."""

from datetime import timedelta

from coldline.contracts import Lot, WorkOrder
from coldline.sim.plant import DEMO_START


def lots() -> dict[str, Lot]:
    """Seed eight chilled beef lots, with the hero load alone in CH4."""
    result = {}
    for i, zone in enumerate([1, 2, 3, 4, 5, 6, 1, 6]):
        count = 120 if zone == 4 else 70 + 4 * i
        lot = Lot(
            id=f"L-{2228 + i}",
            zone_asset_id=f"CH{zone}",
            product="Beef carcases, chilled",
            carcases=count,
            weight_kg=count * 308.5,
            export=zone == 4,
            customer="Coastal Export Co" if zone == 4 else "Wattle Inlet Provisions",
            dispatch_due=DEMO_START + timedelta(minutes=75 if zone == 4 else 100 + 12 * i),
        )
        result[lot.id] = lot
    return result


def work_orders() -> dict[str, WorkOrder]:
    """Seed the accessible maintenance history and yesterday's open calibration check."""
    records = [
        (89, "CH3", "Inspect evaporator drain"),
        (73, "C1", "Inspect oil separator"),
        (59, "CH4", "Verify door seal"),
        (42, "C2", "C2 motor overload trip, standby offline"),
        (27, "CH5", "Service evaporator fan"),
        (12, "C3", "Standby compressor function test"),
        (1, "CH4", "TT-401 reading high intermittently, check calibration"),
    ]
    result = {}
    for i, (days, asset, title) in enumerate(records):
        wo = WorkOrder(
            id=f"WO-{1036 + i}",
            asset_id=asset,
            title=title,
            created_at=DEMO_START - timedelta(days=days),
            created_by="Maintenance planner",
            status="open" if days == 1 else "closed",
        )
        result[wo.id] = wo
    return result
