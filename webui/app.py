"""Flight search web UI backend."""

import json
import time
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from fli.models import (
    Airport,
    BagsFilter,
    DateSearchFilters,
    FlightSearchFilters,
    FlightSegment,
    MaxStops,
    PassengerInfo,
    SeatType,
    TripType,
)
from fli.search import SearchDates, SearchFlights

app = FastAPI()

STATIC_DIR = Path(__file__).parent / "static"
WATCHES_FILE = Path(__file__).parent / "watches.json"

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# ── Models ──────────────────────────────────────────────────────────────────

class FlightSearchRequest(BaseModel):
    origin: str
    destination: str
    date: str
    return_date: str | None = None
    trip_type: str = "one_way"
    seat_type: str = "economy"
    adults: int = 1
    max_stops: str = "any"
    checked_bags: int = 0
    carry_on: bool = False
    top_n: int = 10


class DateSearchRequest(BaseModel):
    origin: str
    destination: str
    from_date: str
    to_date: str
    trip_type: str = "one_way"
    seat_type: str = "economy"
    adults: int = 1
    duration: int | None = None
    checked_bags: int = 0
    carry_on: bool = False


class WatchRequest(BaseModel):
    origin: str
    destination: str
    date: str
    return_date: str | None = None
    trip_type: str = "one_way"
    seat_type: str = "economy"
    adults: int = 1
    max_stops: str = "any"
    target_price: float
    label: str | None = None


# ── Helpers ──────────────────────────────────────────────────────────────────

def parse_seat_type(seat_type: str) -> SeatType:
    return {
        "economy": SeatType.ECONOMY,
        "premium_economy": SeatType.PREMIUM_ECONOMY,
        "business": SeatType.BUSINESS,
        "first": SeatType.FIRST,
    }.get(seat_type, SeatType.ECONOMY)


def parse_max_stops(max_stops: str) -> MaxStops:
    return {
        "nonstop": MaxStops.NON_STOP,
        "one": MaxStops.ONE_STOP_OR_FEWER,
        "two": MaxStops.TWO_OR_FEWER_STOPS,
        "any": MaxStops.ANY,
    }.get(max_stops, MaxStops.ANY)


def bags_filter(checked_bags: int, carry_on: bool) -> BagsFilter | None:
    if checked_bags > 0 or carry_on:
        return BagsFilter(checked_bags=checked_bags, carry_on=carry_on)
    return None


def serialize_single_flight(flight) -> dict:
    return {
        "price": flight.price,
        "currency": flight.currency or "USD",
        "duration": flight.duration,
        "stops": flight.stops,
        "legs": [
            {
                "airline": leg.airline.value if hasattr(leg.airline, "value") else str(leg.airline),
                "flight_number": leg.flight_number,
                "departure_airport": leg.departure_airport.value if hasattr(leg.departure_airport, "value") else str(leg.departure_airport),
                "arrival_airport": leg.arrival_airport.value if hasattr(leg.arrival_airport, "value") else str(leg.arrival_airport),
                "departure_time": leg.departure_datetime.strftime("%H:%M") if leg.departure_datetime else None,
                "arrival_time": leg.arrival_datetime.strftime("%H:%M") if leg.arrival_datetime else None,
                "departure_date": leg.departure_datetime.strftime("%b %d") if leg.departure_datetime else None,
                "duration": leg.duration,
            }
            for leg in flight.legs
        ],
    }


def serialize_flight(flight, trip_type: str) -> dict:
    if isinstance(flight, tuple):
        return {
            "type": "round_trip",
            "outbound": serialize_single_flight(flight[0]),
            "return": serialize_single_flight(flight[1]),
            "total_price": flight[0].price + flight[1].price,
            "currency": flight[0].currency or "USD",
        }
    return {"type": "one_way", **serialize_single_flight(flight)}


def _search_one_way(origin, destination, date, req):
    filters = FlightSearchFilters(
        trip_type=TripType.ONE_WAY,
        passenger_info=PassengerInfo(adults=req.adults),
        flight_segments=[FlightSegment(
            departure_airport=[[origin, 0]],
            arrival_airport=[[destination, 0]],
            travel_date=date,
        )],
        stops=parse_max_stops(req.max_stops),
        seat_type=parse_seat_type(req.seat_type),
        bags=bags_filter(req.checked_bags, req.carry_on),
    )
    return SearchFlights().search(filters, top_n=req.top_n) or []


def load_watches() -> list:
    if WATCHES_FILE.exists():
        return json.loads(WATCHES_FILE.read_text())
    return []


def save_watches(watches: list):
    WATCHES_FILE.write_text(json.dumps(watches, indent=2))


# ── Routes ───────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def root():
    return (STATIC_DIR / "index.html").read_text()


@app.post("/api/search/flights")
async def search_flights(req: FlightSearchRequest):
    try:
        origin = Airport[req.origin.upper()]
        destination = Airport[req.destination.upper()]
    except KeyError as e:
        return JSONResponse({"error": f"Unknown airport code: {e}"}, status_code=400)

    try:
        if req.trip_type == "two_oneway" and req.return_date:
            outbound = _search_one_way(origin, destination, req.date, req)
            time.sleep(2)
            inbound = _search_one_way(destination, origin, req.return_date, req)
            if not outbound and not inbound:
                return {"flights": [], "message": "No flights found"}
            combos = [
                {
                    "type": "two_oneway",
                    "outbound": serialize_single_flight(ob),
                    "return": serialize_single_flight(ib),
                    "total_price": ob.price + ib.price,
                    "currency": ob.currency or "USD",
                }
                for ob in outbound for ib in inbound
            ]
            combos.sort(key=lambda x: x["total_price"])
            return {"flights": combos[:req.top_n]}

        trip_type = TripType.ROUND_TRIP if req.trip_type == "round_trip" else TripType.ONE_WAY
        segments = [FlightSegment(
            departure_airport=[[origin, 0]],
            arrival_airport=[[destination, 0]],
            travel_date=req.date,
        )]
        if trip_type == TripType.ROUND_TRIP and req.return_date:
            segments.append(FlightSegment(
                departure_airport=[[destination, 0]],
                arrival_airport=[[origin, 0]],
                travel_date=req.return_date,
            ))

        filters = FlightSearchFilters(
            trip_type=trip_type,
            passenger_info=PassengerInfo(adults=req.adults),
            flight_segments=segments,
            stops=parse_max_stops(req.max_stops),
            seat_type=parse_seat_type(req.seat_type),
            bags=bags_filter(req.checked_bags, req.carry_on),
        )

        results = SearchFlights().search(filters, top_n=req.top_n)
        if not results:
            return {"flights": [], "message": "No flights found"}
        return {"flights": [serialize_flight(f, req.trip_type) for f in results]}

    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/search/dates")
async def search_dates(req: DateSearchRequest):
    try:
        origin = Airport[req.origin.upper()]
        destination = Airport[req.destination.upper()]
    except KeyError as e:
        return JSONResponse({"error": f"Unknown airport code: {e}"}, status_code=400)

    try:
        trip_type = TripType.ROUND_TRIP if req.trip_type == "round_trip" else TripType.ONE_WAY
        segments = [FlightSegment(
            departure_airport=[[origin, 0]],
            arrival_airport=[[destination, 0]],
            travel_date=req.from_date,
        )]
        if trip_type == TripType.ROUND_TRIP:
            segments.append(FlightSegment(
                departure_airport=[[destination, 0]],
                arrival_airport=[[origin, 0]],
                travel_date=req.from_date,
            ))

        filters = DateSearchFilters(
            trip_type=trip_type,
            passenger_info=PassengerInfo(adults=req.adults),
            flight_segments=segments,
            from_date=req.from_date,
            to_date=req.to_date,
            duration=req.duration,
            bags=bags_filter(req.checked_bags, req.carry_on),
        )

        results = SearchDates().search(filters)
        if not results:
            return {"dates": [], "message": "No dates found"}

        dates = [
            {
                "date": dp.date[0].strftime("%Y-%m-%d"),
                "return_date": dp.date[1].strftime("%Y-%m-%d") if len(dp.date) > 1 else None,
                "price": dp.price,
                "currency": dp.currency or "USD",
            }
            for dp in sorted(results, key=lambda x: x.price)
        ]
        return {"dates": dates}

    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ── Watch endpoints ──────────────────────────────────────────────────────────

@app.post("/api/watch")
async def add_watch(req: WatchRequest):
    try:
        Airport[req.origin.upper()]
        Airport[req.destination.upper()]
    except KeyError as e:
        return JSONResponse({"error": f"Unknown airport code: {e}"}, status_code=400)

    watches = load_watches()
    watch = {
        "id": int(datetime.now().timestamp() * 1000),
        "origin": req.origin.upper(),
        "destination": req.destination.upper(),
        "date": req.date,
        "return_date": req.return_date,
        "trip_type": req.trip_type,
        "seat_type": req.seat_type,
        "adults": req.adults,
        "max_stops": req.max_stops,
        "target_price": req.target_price,
        "label": req.label or f"{req.origin.upper()} → {req.destination.upper()}",
        "created_at": datetime.now().isoformat(),
        "last_checked": None,
        "last_price": None,
        "triggered": False,
    }
    watches.append(watch)
    save_watches(watches)
    return {"ok": True, "watch": watch}


@app.get("/api/watches")
async def list_watches():
    return {"watches": load_watches()}


@app.delete("/api/watch/{watch_id}")
async def delete_watch(watch_id: int):
    watches = [w for w in load_watches() if w["id"] != watch_id]
    save_watches(watches)
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=7654, reload=False)
