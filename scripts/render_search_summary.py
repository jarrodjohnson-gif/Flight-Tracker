"""Render flight search results into the GitHub Actions job summary.

Reads the JSON produced by ``fli dates`` and ``fli flights`` in the Flight
Search workflow and writes a Markdown report to ``$GITHUB_STEP_SUMMARY``.
Missing or malformed files are reported rather than raising, so a partial
run still produces a readable summary.
"""

import json
import os
import statistics
import sys
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
ROUNDTRIP_DATES_FILE = Path("dates-roundtrip.json")
RETURN_DATES_FILE = Path("dates-return.json")
RETURN_NONSTOP_FILE = Path("dates-return-nonstop.json")
RETURN_NONSTOP_FLIGHTS_FILE = Path("flights-return-nonstop.json")


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


def render_by_weekday(dates: list[dict[str, Any]]) -> list[str]:
    """Break the cheapest fare down by day of the week."""
    order = [
        "Monday",
        "Tuesday",
        "Wednesday",
        "Thursday",
        "Friday",
        "Saturday",
        "Sunday",
    ]
    by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for entry in dates:
        try:
            weekday = datetime.strptime(entry["departure_date"], "%Y-%m-%d").strftime("%A")
        except (KeyError, ValueError):
            continue
        by_day[weekday].append(entry)

    if not by_day:
        return []

    overall = min(entry["price"] for entry in dates)
    lines = ["## Cheapest by day of week", ""]
    lines.append("| Day | Best price | On | Dates priced |")
    lines.append("| --- | --- | --- | --- |")
    for day in order:
        entries = by_day.get(day)
        if not entries:
            continue
        best = min(entries, key=lambda d: d["price"])
        marker = " ✅" if best["price"] == overall else ""
        lines.append(
            f"| {day}{marker} | {format_money(best['price'], best['currency'])} "
            f"| {format_date(best['departure_date'])} | {len(entries)} |"
        )
    lines.append("")
    return lines


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


def describe_layovers(legs: list[dict[str, Any]]) -> str:
    """Describe each connection as its airport and ground time."""
    if len(legs) < 2:
        return "nonstop"
    parts = []
    for prev, nxt in zip(legs, legs[1:], strict=False):
        airport = prev.get("arrival_airport", {}).get("code", "?")
        try:
            arrive = datetime.fromisoformat(prev["arrival_time"])
            depart = datetime.fromisoformat(nxt["departure_time"])
        except (KeyError, ValueError):
            parts.append(f"{airport} ?")
            continue
        minutes = int((depart - arrive).total_seconds() // 60)
        parts.append(f"{airport} {minutes // 60}h {minutes % 60:02d}m")
    return ", ".join(parts)


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

    lines.append("| Price | Duration | Stops | Airline | Depart | Arrive | Route | Connection |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
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
            f"| {departs} | {arrives} | {route} | {describe_layovers(legs)} |"
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


def render_roundtrip(one_way_floor: float, currency: str) -> list[str]:
    """Compare round-trip pricing against twice the one-way fare."""
    duration = os.environ.get("TRIP_DURATION", "").strip()
    if not duration or duration == "0":
        return []

    lines = [f"## Round trip ({duration} nights)", ""]

    payload = load_payload(ROUNDTRIP_DATES_FILE)
    dates = payload.get("dates") or [] if payload and payload.get("success") else []
    if not dates:
        lines.append(
            "No round-trip pricing came back for this window. The fares above are one-way only."
        )
        lines.append("")
        return lines

    cheapest = min(dates, key=lambda d: d["price"])
    two_singles = one_way_floor * 2
    delta = cheapest["price"] - two_singles

    lines.append("| Measure | Price |")
    lines.append("| --- | --- |")
    lines.append(f"| Cheapest round trip | {format_money(cheapest['price'], currency)} |")
    lines.append(f"| Two one-ways at the floor | {format_money(two_singles, currency)} |")
    if delta < 0:
        verdict = f"round trip saves {format_money(abs(delta), currency)}"
    elif delta > 0:
        verdict = f"round trip costs {format_money(delta, currency)} more"
    else:
        verdict = "identical either way"
    lines.append(f"| Difference | {verdict} |")
    lines.append("")

    out = format_date(cheapest["departure_date"])
    back = cheapest.get("return_date")
    when = f"{out} returning {format_date(back)}" if back else out
    lines.append(
        f"Cheapest round trip is {format_money(cheapest['price'], currency)} "
        f"departing {when}, across {len(dates)} priced pairs."
    )
    lines.append("")

    try:
        top_n = int(os.environ.get("TOP_N", "15"))
    except ValueError:
        top_n = 15
    ranked = sorted(dates, key=lambda d: d["price"])[:top_n]
    floor = ranked[0]["price"]

    lines.append(f"### {len(ranked)} cheapest trips, best first")
    lines.append("")
    lines.append("| # | Out | Back | Total | Over cheapest |")
    lines.append("| --- | --- | --- | --- | --- |")
    for rank, entry in enumerate(ranked, start=1):
        ret = entry.get("return_date")
        extra = entry["price"] - floor
        over = "—" if extra == 0 else f"+{format_money(extra, currency)}"
        lines.append(
            f"| {rank} | {format_date(entry['departure_date'])} "
            f"| {format_date(ret) if ret else '—'} "
            f"| {format_money(entry['price'], currency)} | {over} |"
        )
    lines.append("")
    return lines


def cheapest_entry(path: Path) -> dict[str, Any] | None:
    """Return the cheapest priced date in a payload, or None."""
    payload = load_payload(path)
    if not payload or not payload.get("success"):
        return None
    dates = payload.get("dates") or []
    return min(dates, key=lambda d: d["price"]) if dates else None


def render_return_leg(
    outbound_floor: float, outbound_nonstop: float | None, currency: str
) -> list[str]:
    """Price the return direction separately and cost each mix of the two legs."""
    if os.environ.get("RETURN_LEG", "").strip().lower() != "true":
        return []

    origin = os.environ.get("ORIGIN", "?")
    destination = os.environ.get("DESTINATION", "?")
    lines = [f"## Return leg ({destination} to {origin})", ""]

    back = cheapest_entry(RETURN_DATES_FILE)
    if not back:
        lines.append("No return-direction pricing came back for this window.")
        lines.append("")
        return lines

    back_nonstop = cheapest_entry(RETURN_NONSTOP_FILE)

    lines.append("| Leg | Cheapest | Nonstop | Nonstop premium |")
    lines.append("| --- | --- | --- | --- |")
    out_ns = format_money(outbound_nonstop, currency) if outbound_nonstop else "none"
    out_prem = (
        format_money(outbound_nonstop - outbound_floor, currency) if outbound_nonstop else "—"
    )
    lines.append(
        f"| {origin} to {destination} | {format_money(outbound_floor, currency)} "
        f"| {out_ns} | {out_prem} |"
    )
    back_ns = format_money(back_nonstop["price"], currency) if back_nonstop else "none"
    back_prem = (
        format_money(back_nonstop["price"] - back["price"], currency) if back_nonstop else "—"
    )
    lines.append(
        f"| {destination} to {origin} | {format_money(back['price'], currency)} "
        f"| {back_ns} | {back_prem} |"
    )
    lines.append("")

    both = outbound_floor + back["price"]
    combos = [("Both legs connecting", both)]
    if outbound_nonstop:
        combos.append(("Nonstop out, connecting back", outbound_nonstop + back["price"]))
    if back_nonstop:
        combos.append(("Connecting out, nonstop back", outbound_floor + back_nonstop["price"]))
    if outbound_nonstop and back_nonstop:
        combos.append(("Nonstop both ways", outbound_nonstop + back_nonstop["price"]))

    lines.append("### Buying the two legs separately")
    lines.append("")
    lines.append("| Combination | Total | Over cheapest |")
    lines.append("| --- | --- | --- |")
    for label, total in sorted(combos, key=lambda c: c[1]):
        extra = total - both
        over = "—" if extra == 0 else f"+{format_money(extra, currency)}"
        lines.append(f"| {label} | {format_money(total, currency)} | {over} |")
    lines.append("")

    flights = load_payload(RETURN_NONSTOP_FLIGHTS_FILE)
    if back_nonstop and flights and flights.get("success"):
        lines.extend(
            render_flights(
                flights,
                back_nonstop["departure_date"],
                heading=f"Nonstop {destination} to {origin} on",
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


def print_digest() -> None:
    """Print a compact, log-friendly digest of the ranked trips."""
    name = os.environ.get("SEARCH_NAME", "").strip()
    origin = os.environ.get("ORIGIN", "?")
    destination = os.environ.get("DESTINATION", "?")
    duration = os.environ.get("TRIP_DURATION", "").strip()
    print(f"DIGEST {name or origin + '-' + destination} [{origin}->{destination}]")

    one_way = cheapest_entry(DATES_FILE)
    if one_way:
        currency = one_way["currency"]
        print(f"DIGEST one-way floor {format_money(one_way['price'], currency)}")
    nonstop = cheapest_entry(NONSTOP_DATES_FILE)
    if nonstop:
        print(f"DIGEST nonstop floor {format_money(nonstop['price'], nonstop['currency'])}")

    payload = load_payload(ROUNDTRIP_DATES_FILE)
    dates = payload.get("dates") or [] if payload and payload.get("success") else []
    if not dates:
        print("DIGEST no round-trip pricing")
        return
    try:
        top_n = int(os.environ.get("TOP_N", "15"))
    except ValueError:
        top_n = 15
    ranked = sorted(dates, key=lambda d: d["price"])[:top_n]
    print(f"DIGEST round trips ({duration} nights), cheapest first:")
    for rank, entry in enumerate(ranked, start=1):
        ret = entry.get("return_date") or "?"
        price = format_money(entry["price"], entry["currency"])
        print(f"DIGEST {rank:>2}. {entry['departure_date']} -> {ret}  {price}")


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

    name = os.environ.get("SEARCH_NAME", "").strip()
    heading = f"{name} — {origin} to {destination}" if name else f"{origin} to {destination}"
    lines = [
        f"# {heading}",
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
            lines.extend(render_by_weekday(dates))
            lines.extend(render_price_spread(dates))

            flights_payload = load_payload(FLIGHTS_FILE)
            if flights_payload and flights_payload.get("success", False):
                lines.extend(render_flights(flights_payload, cheapest["departure_date"]))

            lines.extend(render_roundtrip(cheapest["price"], cheapest["currency"]))
            lines.extend(render_nonstop(cheapest["price"], cheapest["currency"]))

            out_ns = cheapest_entry(NONSTOP_DATES_FILE)
            lines.extend(
                render_return_leg(
                    cheapest["price"],
                    out_ns["price"] if out_ns else None,
                    cheapest["currency"],
                )
            )
            lines.extend(render_compare(cheapest["price"], cheapest["currency"]))

    report = "\n".join(lines)
    print(report)

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a") as handle:
            handle.write(report + "\n")


if __name__ == "__main__":
    if "--digest" in sys.argv:
        print_digest()
    else:
        main()
