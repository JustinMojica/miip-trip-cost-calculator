from __future__ import annotations

from datetime import date
from math import radians, sin, cos, asin, sqrt
from typing import Dict, List, Any, Optional

import requests
import streamlit as st
from amadeus import Client, ResponseError


# -----------------------------
# Configuration / constants
# -----------------------------

# Map friendly airline name -> IATA code used by Amadeus
AIRLINE_CODES: Dict[str, str] = {
    "JetBlue": "B6",
    "Delta": "DL",
    "Southwest": "WN",
    "American": "AA",
}

# Brand name -> keywords we use to recognize hotels by name
BRAND_KEYWORDS: Dict[str, List[str]] = {
    "Marriott": [
        "marriott",
        "courtyard",
        "residence inn",
        "fairfield",
        "springhill suites",
        "jw marriott",
        "ritz-carlton",
        "sheraton",
        "westin",
        "aloft",
        "moxy",
        "ac hotel",
        "towneplace suites",
    ],
    "Hilton": [
        "hilton",
        "doubletree",
        "hampton inn",
        "embassy suites",
        "homewood suites",
        "home2 suites",
        "waldorf astoria",
        "curio",
        "tru by hilton",
        "canopy by hilton",
    ],
    "Wyndham": [
        "wyndham",
        "ramada",
        "days inn",
        "super 8",
        "la quinta",
        "baymont",
        "travelodge",
        "microtel",
        "howard johnson",
        "wingate",
    ],
}


# -----------------------------
# Helper functions
# -----------------------------

def init_amadeus_client() -> Client:
    """
    Initialize the Amadeus client from Streamlit secrets.

    Expected secrets.toml:

    [amadeus]
    client_id = "..."
    client_secret = "..."
    hostname = "production"
    """
    try:
        amadeus_section = st.secrets["amadeus"]
        client_id = amadeus_section["client_id"]
        client_secret = amadeus_section["client_secret"]
        hostname = amadeus_section.get("hostname", "production")
    except Exception as exc:  # noqa: BLE001
        st.error(
            "Amadeus credentials are not configured correctly in secrets.\n\n"
            "Please add an [amadeus] section with client_id, client_secret and hostname."
        )
        raise RuntimeError("Missing Amadeus secrets") from exc

    return Client(
        client_id=client_id,
        client_secret=client_secret,
        hostname=hostname,
    )


def compute_fiscal_year(travel_date: date) -> int:
    """
    GSA per diem is organized by US Federal fiscal year:
    - FY N runs from Oct 1 (N-1) to Sep 30 (N)

    So:
    - Jan–Sep 2025 -> FY 2025
    - Oct–Dec 2025 -> FY 2026
    """
    if travel_date.month >= 10:
        return travel_date.year + 1
    return travel_date.year


def get_gsa_meal_rate(city: str, state_abbrev: str, travel_date: date) -> float:
    """
    Call the GSA Per Diem API to get the daily M&IE (meals & incidental expenses)
    rate for a city/state and travel date.

    Docs: https://api.gsa.gov/travel/perdiem/v2/
    Example endpoint:
      GET https://api.gsa.gov/travel/perdiem/v2/rates?city=Tampa&state=FL&year=2025&api_key=...

    Returns:
        Daily meal rate in USD as float.
    """
    try:
        gsa_section = st.secrets["gsa"]
        api_key = gsa_section["api_key"]
    except Exception as exc:  # noqa: BLE001
        st.error(
            "GSA API key is not configured in secrets.\n\n"
            "Please add a [gsa] section with api_key in .streamlit/secrets.toml."
        )
        raise RuntimeError("Missing GSA API credentials") from exc

    fiscal_year = compute_fiscal_year(travel_date)

    params = {
        "city": city,
        "state": state_abbrev,
        "year": str(fiscal_year),
        "api_key": api_key,
    }

    url = "https://api.gsa.gov/travel/perdiem/v2/rates"
    try:
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        st.error(
            "Failed to contact GSA Per Diem API for meal rates.\n\n"
            f"Endpoint: {url}\n"
            f"City: {city}, State: {state_abbrev}, FY: {fiscal_year}"
        )
        raise RuntimeError("GSA Per Diem API call failed") from exc

    # GSA response usually: {"rates": [ { "meals": 74, ... }, ... ]}
    rates = data.get("rates") or data.get("data") or []
    if not rates:
        raise RuntimeError("No per diem rates returned for this city/state/year.")

    meals_value = rates[0].get("meals")  # first record is usually enough
    if meals_value is None:
        raise RuntimeError("Per diem data did not include 'meals' field.")

    try:
        return float(meals_value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("Could not parse meals per diem value.") from exc


def geocode_address(address: str) -> Optional[Dict[str, Any]]:
    """
    Geocode the client office address using OpenStreetMap Nominatim.

    Returns:
        dict with 'lat', 'lon', and optional 'display_name',
        or None if nothing is found.

    NOTE: This uses a public service, so don't abuse it.
    """
    url = "https://nominatim.openstreetmap.org/search"
    params = {
        "q": address,
        "format": "json",
        "limit": 1,
    }
    headers = {
        # Basic user-agent so the service knows who's calling
        "User-Agent": "MIIPTripTool/1.0 (contact: example@example.com)",
    }

    try:
        resp = requests.get(url, params=params, headers=headers, timeout=10)
        resp.raise_for_status()
        results = resp.json()
    except Exception:
        return None

    if not results:
        return None

    top = results[0]
    try:
        lat = float(top["lat"])
        lon = float(top["lon"])
    except (KeyError, ValueError):
        return None

    return {
        "lat": lat,
        "lon": lon,
        "display_name": top.get("display_name", ""),
    }


def haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """
    Compute distance in miles between two lat/lon points using the haversine formula.
    """
    # Earth radius in miles
    r = 3958.8

    phi1, phi2 = radians(lat1), radians(lat2)
    dphi = radians(lat2 - lat1)
    dlambda = radians(lon2 - lon1)

    a = sin(dphi / 2) ** 2 + cos(phi1) * cos(phi2) * sin(dlambda / 2) ** 2
    c = 2 * asin(sqrt(a))
    return r * c


def get_average_roundtrip_flight_price(
    amadeus: Client,
    origin_airport: str,
    dest_airport: str,
    departure_date: date,
    return_date: date,
    airline_name: str,
) -> float:
    """
    Use Amadeus Flight Offers Search to get a set of round-trip offers and
    return the average price per traveler.

    Strategy:
    - Request offers with adults=1 (per-person price).
    - Filter to preferred airline via includedAirlineCodes.
    - Average price.grandTotal over the offers returned.
    """
    airline_code = AIRLINE_CODES.get(airline_name)

    params: Dict[str, Any] = {
        "originLocationCode": origin_airport.upper(),
        "destinationLocationCode": dest_airport.upper(),
        "departureDate": departure_date.isoformat(),
        "returnDate": return_date.isoformat(),
        "adults": 1,
        "currencyCode": "USD",
        "max": 20,
    }

    if airline_code:
        params["includedAirlineCodes"] = airline_code

    try:
        resp = amadeus.shopping.flight_offers_search.get(**params)
        offers = resp.data
    except ResponseError as error:
        st.error(f"Amadeus flight search failed: {error}")
        raise RuntimeError("Amadeus flight search failed") from error

    prices: List[float] = []
    for offer in offers:
        price_block = offer.get("price", {})
        grand_total = price_block.get("grandTotal")
        if grand_total is None:
            continue
        try:
            prices.append(float(grand_total))
        except (TypeError, ValueError):
            continue

    if not prices:
        raise RuntimeError(
            "No priced flight offers returned from Amadeus "
            "(check dates, airports, or airline preference)."
        )

    return sum(prices) / len(prices)


def find_brand_hotels_near(
    amadeus: Client,
    lat: float,
    lon: float,
    preferred_brand: str,
) -> List[Dict[str, Any]]:
    """
    Use the Amadeus Hotel List API (by geocode) to find hotels near the
    client's office, then filter by brand via hotel name keywords.

    Returns:
        A list of hotel objects (possibly empty).
    """
    try:
        resp = amadeus.reference_data.locations.hotels.by_geocode.get(
            latitude=lat,
            longitude=lon,
            radius=10,        # km radius around client office
            radiusUnit="KM",
            hotelSource="ALL",
        )
        hotels = resp.data
    except ResponseError as error:
        st.error(f"Amadeus hotel list search failed: {error}")
        raise RuntimeError("Amadeus hotel list search failed") from error

    if not hotels:
        raise RuntimeError("No hotels returned near the client office location.")

    keywords = [kw.lower() for kw in BRAND_KEYWORDS.get(preferred_brand, [])]

    if keywords:
        filtered: List[Dict[str, Any]] = []
        for h in hotels:
            name = h.get("name", "").lower()
            if any(kw in name for kw in keywords):
                filtered.append(h)
    else:
        filtered = hotels

    # If no brand match, fall back to all hotels so we at least get *something*
    return filtered or hotels


def get_average_hotel_price(
    amadeus: Client,
    hotels: List[Dict[str, Any]],
    check_in: date,
    check_out: date,
    num_travelers: int,
) -> float:
    """
    Use Amadeus Hotel Search API to get offers for a set of hotels and compute
    the average nightly rate per room.

    Strategy:
    - Take up to 20 hotelIds from our brand-filtered list.
    - Call /v3/shopping/hotel-offers with:
        - hotelIds
        - adults = num_travelers (for occupancy)
        - roomQuantity = num_travelers (each traveler gets their own room)
        - checkInDate / checkOutDate
    - Offers return total price per room for entire stay.
    - Compute the average total per room across offers, then divide by nights.
    """
    nights = (check_out - check_in).days
    if nights <= 0:
        raise RuntimeError("Return date must be after departure date to compute hotel nights.")

    hotel_ids = ",".join(
        h.get("hotelId") for h in hotels[:20] if h.get("hotelId")
    )
    if not hotel_ids:
        raise RuntimeError("Selected hotels did not contain valid hotelIds.")

    try:
        resp = amadeus.shopping.hotel_offers.get(
            hotelIds=hotel_ids,
            adults=num_travelers,
            checkInDate=check_in.isoformat(),
            checkOutDate=check_out.isoformat(),
            roomQuantity=num_travelers,
            currency="USD",
        )
        offers = resp.data
    except ResponseError as error:
        st.error(f"Amadeus hotel offers search failed: {error}")
        raise RuntimeError("Amadeus hotel offers search failed") from error

    prices: List[float] = []
    for offer in offers:
        price_block = offer.get("price", {})
        total = price_block.get("total")
        if total is None:
            continue
        try:
            prices.append(float(total))
        except (TypeError, ValueError):
            continue

    if not prices:
        raise RuntimeError(
            "No priced hotel offers returned from Amadeus for the selected dates."
        )

    avg_total_per_room_for_stay = sum(prices) / len(prices)
    avg_nightly_per_room = avg_total_per_room_for_stay / nights
    return avg_nightly_per_room


# -----------------------------
# Streamlit App UI
# -----------------------------

st.set_page_config(
    page_title="MIIP Trip Cost Calculator",
    layout="wide",
)

st.title("MIIP Trip Cost Calculator")
st.caption(
    "Automatically estimate **flight**, **hotel**, **meals (GSA per diem)**, "
    "and **car** costs for audit trips."
)

st.markdown("---")

with st.form("trip_form"):
    col_left, col_right = st.columns(2)

    with col_left:
        st.subheader("Traveler & flights")

        auditor_name = st.text_input("Auditor name", placeholder="e.g. Justin Mojica")

        num_travelers = st.number_input(
            "Number of travelers (each gets their own room)",
            min_value=1,
            max_value=20,
            value=1,
            step=1,
        )

        origin_airport = st.selectbox(
            "Departure airport",
            options=["BOS", "MHT"],
            index=0,
            help="Default is BOS, but you can switch to MHT.",
        )

        dest_airport = st.text_input(
            "Destination airport (IATA)",
            value="TPA",
            help="3-letter airport code, e.g. TPA, JFK, ORD.",
        )

        preferred_airline = st.selectbox(
            "Preferred airline",
            options=list(AIRLINE_CODES.keys()),
            index=2,  # JetBlue as default
        )

    with col_right:
        st.subheader("Client & hotels")

        client_address = st.text_input(
            "Client office address",
            value="Tampa, FL",
            help="Used to find the nearest preferred-brand hotel.",
        )

        dest_city = st.text_input(
            "Destination city (for GSA per diem)",
            value="Tampa",
            help="City name as used by GSA, e.g. Tampa",
        )

        dest_state = st.text_input(
            "Destination state (2-letter)",
            value="FL",
            max_chars=2,
            help="State abbreviation, e.g. FL, MA, NY.",
        ).upper()

        preferred_hotel_brand = st.selectbox(
            "Preferred hotel brand",
            options=list(BRAND_KEYWORDS.keys()),
            index=0,  # Marriott default
        )

    st.subheader("Dates & ground costs")

    col_dates, col_ground = st.columns(2)

    with col_dates:
        departure_date = st.date_input(
            "Departure date",
            value=date.today(),
        )
        return_date = st.date_input(
            "Return date",
            value=date.today(),
            help="Must be after the departure date.",
        )

    with col_ground:
        include_car = st.checkbox(
            "Include rental car (Hertz or other)",
            value=True,
        )
        car_daily_rate = st.number_input(
            "Estimated rental car rate per day (USD)",
            min_value=0.0,
            value=60.0,
            step=5.0,
            help="Use your Hertz contract rate here.",
        )

        misc_costs = st.number_input(
            "Other fixed costs (USD)",
            min_value=0.0,
            value=0.0,
            step=25.0,
            help="Parking, tolls, baggage, etc. for the entire trip.",
        )

    submitted = st.form_submit_button("Calculate trip cost")

if submitted:
    # -----------------------------
    # Basic date validation
    # -----------------------------
    if return_date <= departure_date:
        st.error("Return date must be after departure date.")
        st.stop()

    trip_nights = (return_date - departure_date).days
    trip_days = trip_nights  # for most business trips, days ~ nights

    st.info(
        f"Trip length: **{trip_nights} nights / {trip_days} days** "
        f"for **{num_travelers} traveler(s)**."
    )

    # -----------------------------
    # Initialize Amadeus once
    # -----------------------------
    amadeus_client = init_amadeus_client()

    # -----------------------------
    # Flights (Amadeus)
    # -----------------------------
    st.subheader("Step 1 – Flights (Amadeus)")

    try:
        flight_price_per_traveler = get_average_roundtrip_flight_price(
            amadeus=amadeus_client,
            origin_airport=origin_airport,
            dest_airport=dest_airport,
            departure_date=departure_date,
            return_date=return_date,
            airline_name=preferred_airline,
        )
    except RuntimeError as exc:
        st.error(str(exc))
        st.stop()

    total_flight_cost = flight_price_per_traveler * num_travelers

    st.write(
        f"- Estimated **round-trip flight (per traveler)**: "
        f"`${flight_price_per_traveler:,.2f}` "
        f"with **{preferred_airline}**"
    )
    st.write(
        f"- Total flights for **{num_travelers} traveler(s)**: "
        f"`${total_flight_cost:,.2f}`"
    )

    # -----------------------------
    # Geocode client office
    # -----------------------------
    st.subheader("Step 2 – Hotels near client office (Amadeus + OSM)")

    geocoded = geocode_address(client_address)
    if not geocoded:
        st.error("Could not geocode the client office address. Please adjust the address.")
        st.stop()

    office_lat = geocoded["lat"]
    office_lon = geocoded["lon"]
    office_display_name = geocoded.get("display_name", client_address)

    st.write(f"- Client office location resolved to: `{office_display_name}`")

    # -----------------------------
    # Hotels (Amadeus)
    # -----------------------------
    try:
        brand_hotels = find_brand_hotels_near(
            amadeus=amadeus_client,
            lat=office_lat,
            lon=office_lon,
            preferred_brand=preferred_hotel_brand,
        )
    except RuntimeError as exc:
        st.error(str(exc))
        st.stop()

    # Find nearest hotel (for distance info)
    nearest_hotel = None
    nearest_distance_miles = None

    for h in brand_hotels:
        geo = h.get("geoCode") or {}
        hlat = geo.get("latitude")
        hlon = geo.get("longitude")
        if hlat is None or hlon is None:
            continue
        dist = haversine_miles(office_lat, office_lon, float(hlat), float(hlon))
        if nearest_distance_miles is None or dist < nearest_distance_miles:
            nearest_distance_miles = dist
            nearest_hotel = h

    if nearest_hotel and nearest_distance_miles is not None:
        st.write(
            f"- Nearest **{preferred_hotel_brand}**-brand hotel candidate: "
            f"**{nearest_hotel.get('name', 'Unknown')}** – "
            f"~`{nearest_distance_miles:.1f}` miles from the client office."
        )
    else:
        st.warning(
            f"Could not compute distance to any {preferred_hotel_brand} hotel; "
            "using hotel list for pricing only."
        )

    try:
        avg_nightly_rate_per_room = get_average_hotel_price(
            amadeus=amadeus_client,
            hotels=brand_hotels,
            check_in=departure_date,
            check_out=return_date,
            num_travelers=int(num_travelers),
        )
    except RuntimeError as exc:
        st.error(str(exc))
        st.stop()

    total_hotel_cost = avg_nightly_rate_per_room * trip_nights * num_travelers

    st.write(
        f"- Estimated **average nightly rate per room** "
        f"(brand: {preferred_hotel_brand}): "
        f"`${avg_nightly_rate_per_room:,.2f}`"
    )
    st.write(
        f"- Total hotel cost: **{trip_nights} nights × {num_travelers} room(s)** "
        f"= `${total_hotel_cost:,.2f}`"
    )

    # -----------------------------
    # Meals (GSA Per Diem)
    # -----------------------------
    st.subheader("Step 3 – Meals (GSA Per Diem)")

    try:
        daily_meal_rate = get_gsa_meal_rate(
            city=dest_city,
            state_abbrev=dest_state,
            travel_date=departure_date,
        )
    except RuntimeError as exc:
        st.error(str(exc))
        st.stop()

    meals_total = daily_meal_rate * trip_days * num_travelers

    st.write(
        f"- GSA M&IE daily rate for **{dest_city}, {dest_state}**: "
        f"`${daily_meal_rate:,.2f}` per person per day"
    )
    st.write(
        f"- Total meals: **{trip_days} days × {num_travelers} traveler(s)** "
        f"= `${meals_total:,.2f}`"
    )

    # -----------------------------
    # Car rental
    # -----------------------------
    st.subheader("Step 4 – Ground transportation")

    if include_car:
        car_total = car_daily_rate * trip_days
        st.write(
            f"- Rental car: **{trip_days} days × "
            f"${car_daily_rate:,.2f}/day** = `${car_total:,.2f}` "
            "(shared across travelers)"
        )
    else:
        car_total = 0.0
        st.write("- Rental car: not included")

    if misc_costs > 0:
        st.write(f"- Other fixed costs: `${misc_costs:,.2f}`")

    # -----------------------------
    # Summary
    # -----------------------------
    st.markdown("---")
    st.subheader("Summary")

    grand_total = total_flight_cost + total_hotel_cost + meals_total + car_total + misc_costs

    col_a, col_b = st.columns(2)

    with col_a:
        st.markdown("**Trip cost components**")
        st.write(f"- Flights: `${total_flight_cost:,.2f}`")
        st.write(f"- Hotels: `${total_hotel_cost:,.2f}`")
        st.write(f"- Meals (GSA): `${meals_total:,.2f}`")
        st.write(f"- Car rental: `${car_total:,.2f}`")
        st.write(f"- Other: `${misc_costs:,.2f}`")

    with col_b:
        st.markdown("**Totals**")
        st.write(f"- **Total trip cost**: `${grand_total:,.2f}`")
        st.write(
            f"- Approximate **cost per traveler**: "
            f"`${(grand_total / num_travelers):,.2f}`"
        )

    if auditor_name:
        st.caption(
            f"Scenario for **{auditor_name}** departing **{origin_airport}** "
            f"to **{dest_airport}**."
        )
