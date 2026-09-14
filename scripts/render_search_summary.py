"""Render flight search results into the GitHub Actions job summary.

Reads the JSON produced by ``fli dates`` and ``fli flights`` in the Flight
Search workflow and writes a Markdown report to ``$GITHUB_STEP_SUMMARY``.
Missing or malformed files are reported rather than raising, so a partial
run still produces a readable summary.
"""

import json
import os
import statistics
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

DATES_FILE = Path("dates.json")
FLIGHTS_FILE = Path("flights.json")
NONSTOP_DATES_FILE = Path("dates-nonstop.json")
NONSTOP_FLIGHTS_FILE = Path("flights-nonstop.json")
COMPARE_DATES_FILE = Path("dates-compare.json")
COMPARE_FLIGHTS_FILE = Path("flights-compare.json")


def load_payload(path: Path) -> dict[str, Any] | None:
    """Load a CLI JSON payload, returning None when it is absent or invalid."""
    if not path.exists() or path.stat().st_size == 0:
        return None
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return None


def format_money(amount: float, currency: str) -> str:
    """Format a price with its currency code."""
    symbol = "$" if currency == "USD" else ""
    return f"{symbol}{amount:,.0f} {currency}"


def format_date(value: str) -> str:
    """Render an ISO date as a readable weekday-and-day string."""
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return value
    return parsed.strftime("%a %b %d, %Y")


def render_price_spread(dates: list[dict[str, Any]]) -> list[str]:
    """Summarise the price range across the window and list the priciest dates."""
    prices = [entry["price"] for entry in dates]
    currency = dates[0]["currency"]
    ranked = sorted(dates, key=lambda d: d["price"], reverse=True)

    lines = ["## Price spread", ""]
    lines.append("| Measure | Price |")
    lines.append("| --- | --- |")
    lines.append(f"| Cheapest | {format_money(min(prices), currency)} |")
    lines.append(f"| Median | {format_money(statistics.median(prices), currency)} |")
    lines.append(f"| Most expensive | {format_money(max(prices), currency)} |")
    lines.append("")

    cheapest_count = sum(1 for price in prices if price == min(prices))
    lines.append(
        f"The floor fare of {format_money(min(prices), currency)} is available on "
        f"{cheapest_count} of {len(dates)} dates in this window."
    )
    lines.append("")

    lines.append("### Most expensive dates to avoid")
    lines.append("")
    lines.append("| Departure | Price |")
    lines.append("| --- | --- |")
    for entry in ranked[:8]:
        lines.append(
            f"| {format_date(entry['departure_date'])} "
            f"| {format_money(entry['price'], entry['currency'])} |"
        )
    lines.append("")
    return lines


def render_cheapest_dates(dates: list[dict[str, Any]], top_n: int) -> list[str]:
    """Build the cheapest-dates table and a per-month breakdown."""
    lines: list[str] = []
    ranked = sorted(dates, key=lambda d: d["price"])

    lines.append(f"## {min(top_n, len(ranked))} cheapest departure dates")
    lines.append("")
    lines.append("| # | Departure | Price |")
    lines.append("| --- | --- | --- |")
    for rank, entry in enumerate(ranked[:top_n], start=1):
        price = format_money(entry["price"], entry["currency"])
        lines.append(f"| {rank} | {format_date(entry['departure_date'])} | {price} |")
    lines.append("")

    by_month: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for entry in dates:
        by_month[entry["departure_date"][:7]].append(entry)

    lines.append("## Cheapest by month")
    lines.append("")
    lines.append("| Month | Best price | On | Dates priced |")
    lines.append("| --- | --- | --- | --- |")
    for month in sorted(by_month):
        entries = by_month[month]
        best = min(entries, key=lambda d: d["price"])
        label = datetime.strptime(month, "%Y-%m").strftime("%B %Y")
        price = format_money(best["price"], best["currency"])
        lines.append(
            f"| {label} | {price} | {format_date(best['departure_date'])} | {len(entries)} |"
        )
    lines.append("")
    return lines


def format_time(value: str) -> str:
    """Render an ISO datetime as a short 24-hour clock time."""
    try:
        return datetime.fromisoformat(value).strftime("%H:%M")
    except ValueError:
        return value


def render_flights(
    payload: dict[str, Any], cheapest_date: str, heading: str = "Flights on"
) -> list[str]:
    """Build the table of individual flights on the given date."""
    flights = payload.get("flights") or []
    lines = [f"## {heading} {format_date(cheapest_date)}", ""]
    if not flights:
        lines.append("No individual flights returned for this date.")
        lines.append("")
        return lines

    lines.append("| Price | Duration | Stops | Airline | Depart | Arrive | Route |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- |")
    for flight in flights[:10]:
        legs = flight.get("legs") or []
        codes = [leg.get("airline", {}).get("code", "?") for leg in legs]
        airlines = ", ".join(dict.fromkeys(codes)) or "?"
        if legs:
            departs = format_time(legs[0].get("departure_time", ""))
            arrives = format_time(legs[-1].get("arrival_time", ""))
            stations = [legs[0].get("departure_airport", {}).get("code", "?")]
            stations += [leg.get("arrival_airport", {}).get("code", "?") for leg in legs]
            route = "-".join(stations)
        else:
            departs = arrives = route = "?"
        duration = flight.get("duration") or 0
        duration_text = f"{duration // 60}h {duration % 60:02d}m" if duration else "?"
        price = format_money(flight.get("price", 0), flight.get("currency", "USD"))
        stops = flight.get("stops", max(len(legs) - 1, 0))
        lines.append(
            f"| {price} | {duration_text} | {stops} | {airlines} "
            f"| {departs} | {arrives} | {route} |"
        )
    lines.append("")
    return lines


def render_nonstop(connecting_floor: float, currency: str) -> list[str]:
    """Compare nonstop pricing against the cheapest connecting fare."""
    lines = ["## Nonstop", ""]

    payload = load_payload(NONSTOP_DATES_FILE)
    dates = payload.get("dates") or [] if payload and payload.get("success") else []
    if not dates:
        lines.append(
            "Google Flights returned no nonstop service on this route in this window, "
            "so every option is a connecting itinerary."
        )
        lines.append("")
        return lines

    cheapest = min(dates, key=lambda d: d["price"])
    premium = cheapest["price"] - connecting_floor
    floor_count = sum(1 for d in dates if d["price"] == cheapest["price"])

    lines.append("| Measure | Price |")
    lines.append("| --- | --- |")
    lines.append(f"| Cheapest connecting | {format_money(connecting_floor, currency)} |")
    lines.append(f"| Cheapest nonstop | {format_money(cheapest['price'], currency)} |")
    lines.append(f"| Nonstop premium | {format_money(premium, currency)} |")
    lines.append("")
    lines.append(
        f"Cheapest nonstop is {format_money(cheapest['price'], currency)} on "
        f"{format_date(cheapest['departure_date'])}, available at that price on "
        f"{floor_count} of {len(dates)} nonstop dates."
    )
    lines.append("")

    flights_payload = load_payload(NONSTOP_FLIGHTS_FILE)
    if flights_payload and flights_payload.get("success"):
        lines.extend(
            render_flights(
                flights_payload, cheapest["departure_date"], heading="Nonstop flights on"
            )
        )
    return lines


def render_compare(baseline_floor: float, currency: str) -> list[str]:
    """Compare a second destination against the primary one."""
    compare_code = os.environ.get("COMPARE_DESTINATION", "").strip()
    destination = os.environ.get("DESTINATION", "").strip()
    if not compare_code or compare_code == destination:
        return []

    lines = [f"## Alternate destination: {compare_code}", ""]

    payload = load_payload(COMPARE_DATES_FILE)
    dates = payload.get("dates") or [] if payload and payload.get("success") else []
    if not dates:
        lines.append(f"No priced dates came back for {compare_code} in this window.")
        lines.append("")
        return lines

    cheapest = min(dates, key=lambda d: d["price"])
    delta = cheapest["price"] - baseline_floor
    if delta < 0:
        verdict = f"{compare_code} is {format_money(abs(delta), currency)} cheaper"
    elif delta > 0:
        verdict = f"{compare_code} is {format_money(delta, currency)} more expensive"
    else:
        verdict = f"{compare_code} costs the same"

    lines.append("| Destination | Cheapest fare | On |")
    lines.append("| --- | --- | --- |")
    lines.append(f"| {destination} | {format_money(baseline_floor, currency)} | (baseline) |")
    lines.append(
        f"| {compare_code} | {format_money(cheapest['price'], currency)} "
        f"| {format_date(cheapest['departure_date'])} |"
    )
    lines.append("")
    lines.append(f"**{verdict}** at its cheapest across {len(dates)} priced dates.")
    lines.append("")

    flights_payload = load_payload(COMPARE_FLIGHTS_FILE)
    if flights_payload and flights_payload.get("success"):
        lines.extend(
            render_flights(
                flights_payload,
                cheapest["departure_date"],
                heading=f"Cheapest {compare_code} flights on",
            )
        )
    return lines


def main() -> None:
    """Write the Markdown report to the job summary and stdout."""
    origin = os.environ.get("ORIGIN", "?")
    destination = os.environ.get("DESTINATION", "?")
    from_date = os.environ.get("FROM_DATE", "?")
    to_date = os.environ.get("TO_DATE", "?")
    cabin = os.environ.get("CABIN_CLASS", "ECONOMY")
    stops = os.environ.get("MAX_STOPS", "ANY")
    try:
        top_n = int(os.environ.get("TOP_N", "15"))
    except ValueError:
        top_n = 15

    lines = [
        f"# {origin} to {destination}",
        "",
        f"Departures between **{format_date(from_date)}** and **{format_date(to_date)}** "
        f"— {cabin.replace('_', ' ').title()}, stops: {stops}.",
        "",
    ]

    dates_payload = load_payload(DATES_FILE)
    if dates_payload is None:
        lines.append("The date search produced no usable output. Check the step logs above.")
    elif not dates_payload.get("success", False):
        error = dates_payload.get("error", "unknown error")
        lines.append(f"The date search failed: {error}")
    else:
        dates = dates_payload.get("dates") or []
        if not dates:
            lines.append("Google Flights returned no priced dates for this route and window.")
        else:
            cheapest = min(dates, key=lambda d: d["price"])
            lines.append(
                f"**Best price: {format_money(cheapest['price'], cheapest['currency'])} "
                f"on {format_date(cheapest['departure_date'])}** "
                f"across {len(dates)} priced dates."
            )
            lines.append("")
            lines.extend(render_cheapest_dates(dates, top_n))
            lines.extend(render_price_spread(dates))

            flights_payload = load_payload(FLIGHTS_FILE)
            if flights_payload and flights_payload.get("success", False):
                lines.extend(render_flights(flights_payload, cheapest["departure_date"]))

            lines.extend(render_nonstop(cheapest["price"], cheapest["currency"]))
            lines.extend(render_compare(cheapest["price"], cheapest["currency"]))

    report = "\n".join(lines)
    print(report)

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a") as handle:
            handle.write(report + "\n")


if __name__ == "__main__":
    main()
