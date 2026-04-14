#!/usr/bin/env python3
"""Price alert checker — runs standalone or via GitHub Actions cron.

Reads watches.json, checks current prices, sends ntfy.sh push notification
when any watched route drops to or below the target price.

Usage:
    python checker.py                          # uses NTFY_TOPIC env var
    NTFY_TOPIC=fli-alerts-yourname python checker.py
"""

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx

# ── Config ───────────────────────────────────────────────────────────────────

WATCHES_FILE = Path(__file__).parent / "watches.json"
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
NTFY_BASE = "https://ntfy.sh"


# ── fli imports ──────────────────────────────────────────────────────────────

sys.path.insert(0, str(Path(__file__).parent.parent))

from fli.models import (
    Airport,
    DateSearchFilters,
    FlightSegment,
    MaxStops,
    PassengerInfo,
    SeatType,
    TripType,
)
from fli.search import SearchDates


# ── Helpers ──────────────────────────────────────────────────────────────────

def parse_seat(s: str) -> SeatType:
    return {"economy": SeatType.ECONOMY, "premium_economy": SeatType.PREMIUM_ECONOMY,
            "business": SeatType.BUSINESS, "first": SeatType.FIRST}.get(s, SeatType.ECONOMY)


def parse_stops(s: str) -> MaxStops:
    return {"nonstop": MaxStops.NON_STOP, "one": MaxStops.ONE_STOP_OR_FEWER,
            "two": MaxStops.TWO_OR_FEWER_STOPS, "any": MaxStops.ANY}.get(s, MaxStops.ANY)


def get_cheapest_price(watch: dict) -> float | None:
    """Return cheapest price across the watched date range."""
    try:
        origin = Airport[watch["origin"]]
        destination = Airport[watch["destination"]]
        is_rt = watch["trip_type"] in ("round_trip", "two_oneway")
        trip_type = TripType.ROUND_TRIP if is_rt else TripType.ONE_WAY

        from_date = watch["from_date"]
        to_date   = watch["to_date"]
        duration  = watch.get("duration")

        segments = [FlightSegment(
            departure_airport=[[origin, 0]],
            arrival_airport=[[destination, 0]],
            travel_date=from_date,
        )]
        if is_rt:
            ret_from = watch.get("return_from_date") or from_date
            segments.append(FlightSegment(
                departure_airport=[[destination, 0]],
                arrival_airport=[[origin, 0]],
                travel_date=ret_from,
            ))
            if not duration:
                # estimate from date ranges if not set
                from datetime import datetime
                d1 = datetime.strptime(from_date, "%Y-%m-%d")
                d2 = datetime.strptime(ret_from, "%Y-%m-%d")
                duration = max(1, (d2 - d1).days)

        filters = DateSearchFilters(
            trip_type=trip_type,
            passenger_info=PassengerInfo(adults=watch.get("adults", 1)),
            flight_segments=segments,
            stops=parse_stops(watch.get("max_stops", "any")),
            seat_type=parse_seat(watch.get("seat_type", "economy")),
            from_date=from_date,
            to_date=to_date,
            duration=duration,
        )

        results = SearchDates().search(filters)
        if not results:
            return None

        return min(r.price for r in results)

    except Exception as e:
        print(f"  Error fetching price: {e}")
        return None


def send_notification(watch: dict, current_price: float):
    """Push alert via ntfy.sh."""
    if not NTFY_TOPIC:
        print("  [no NTFY_TOPIC set — skipping push notification]")
        print(f"  ALERT: {watch['label']} hit ${current_price:.0f} (target ${watch['target_price']:.0f})")
        return

    label = watch["label"]
    date_str = watch["date"]
    target = watch["target_price"]
    savings = target - current_price

    title = f"✈ Price alert: {label}"
    body = (
        f"${current_price:.0f} found in {watch['from_date']} – {watch['to_date']} — "
        f"${savings:.0f} below your ${target:.0f} target!\n"
        f"Open Google Flights to book."
    )

    try:
        httpx.post(
            f"{NTFY_BASE}/{NTFY_TOPIC}",
            content=body,
            headers={
                "Title": title,
                "Priority": "high",
                "Tags": "airplane,money_with_wings",
                "Click": f"https://www.google.com/travel/flights",
            },
            timeout=10,
        )
        print(f"  Notification sent: {title}")
    except Exception as e:
        print(f"  Failed to send notification: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    if not WATCHES_FILE.exists():
        print("No watches.json found — nothing to check.")
        return

    watches = json.loads(WATCHES_FILE.read_text())
    if not watches:
        print("Watch list is empty.")
        return

    print(f"Checking {len(watches)} watch(es) at {datetime.now().strftime('%Y-%m-%d %H:%M')}...")
    updated = False

    for i, watch in enumerate(watches):
        label = watch["label"]
        target = watch["target_price"]
        print(f"\n[{i+1}/{len(watches)}] {label} — target ${target:.0f}")

        if i > 0:
            time.sleep(3)  # be polite to Google

        price = get_cheapest_price(watch)
        if price is None:
            print("  No results.")
            continue

        print(f"  Current cheapest: ${price:.0f}")
        watch["last_price"] = price
        watch["last_checked"] = datetime.now().isoformat()
        updated = True

        if price <= target and not watch.get("triggered"):
            print(f"  TARGET HIT — sending alert!")
            send_notification(watch, price)
            watch["triggered"] = True
        elif price > target and watch.get("triggered"):
            # Price went back up — reset so we alert again if it drops
            watch["triggered"] = False

    if updated:
        WATCHES_FILE.write_text(json.dumps(watches, indent=2))
        print("\nwatches.json updated.")


if __name__ == "__main__":
    main()
