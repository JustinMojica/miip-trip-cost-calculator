import math
from datetime import date, timedelta
from typing import Optional, Tuple

import requests
import streamlit as st
from amadeus import Client, ResponseError


# -----------------------------------------------------------------------------
# 1. Helpers: parse address, dates, etc.
# -----------------------------------------------------------------------------

def parse_client_address(address: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """
    Very simple parser for client office address.

    Expected common formats:
      - "123 Main St, Tampa, FL 33602"
      - "Tampa, FL 33602"
      - "Tampa FL 33602"

    Returns (city, state, zip_code) – any of them may be None if parsing fails.
    """
    if not address:
        return None, None, None

    # Try to grab a 5-digit ZIP from the string
    import re
    zip_match = re.search(r"\b(\d{5})(?:-\d{4})?\b", address)
    zip_code = zip_match.group(1) if zip_match else None

    # Split by commas and look at the last part for "ST ZIP"
    parts = [p.strip() for p in address.split(",") if p.strip()]

    city = None
    state = None

    if len(parts) >= 2:
        # e.g. ["123 Main St", "Tampa", "FL 33602"]
        city_candidate = parts[-2]
        tail = parts[-1]
        tokens = tail.split()
        if len(tokens) >= 1:
            # last token might be ZIP, previous one might be state
            maybe_state = tokens[0]
            if len(maybe_state) == 2 and maybe_state.isalpha():
                state = maybe_state.upper()
                city = city_candidate
    elif len(parts) == 1:
        # e.g. "Tampa FL 33602"
        tokens = parts[0].split()
        if len(tokens) >= 3:
            # assume "... CITY STATE ZIP"
            maybe_state = tokens[-2]
            if len(maybe_state) == 2 and maybe_state.isalpha():
                state = maybe_state.upper()
                city = " ".join(tokens[:-2])

    return city, state, zip_code


def compute_days_and_nights(dep: date, ret: date) -> Tuple[int, int]:
    """
    Returns (trip_days, hotel_nights).
    If dep == ret, it's a 1-day, 1-night trip.
    """
    if dep > ret:
        return 0, 0
    delta = (ret - dep).days
    if delta <= 0:
        return 1, 1
    return delta + 1, delta


# -----------------------------------------------------------------------------
# 2. Amadeus: client + flights + hotels
# -----------------------------------------------------------------------------

@st.cache_resource(show_spinner=False)
def get_amadeus_client() -> Client:
    """
    Create and cache a single Amadeus client using Streamlit secrets.
    """
    cfg = st.secrets["amadeus"]
    return Client(
        client_id=cfg["client_id"],
        client_secret=cfg["client_secret"],
        hostname=cfg.get("hostname", "production"),
    )


AIRLINE_TO_CARRIER = {
    "Delta": "DL",
    "JetBlue": "B6",
    "Southwest": "WN",
    "American": "AA",
}


def get_average_roundtrip_fare(
    amadeus: Client,
    origin: str,
    destination: str,
    dep: date,
    ret: date,
    travelers: int,
    preferred_airline: str,
) -> Optional[float]:
    """
    Use Amadeus Flight Offers Search to get an average roundtrip fare
    for the preferred airline. Returns price per person in USD, or None on error.
    """
    carrier_code = AIRLINE_TO_CARRIER.get(preferred_airline)
    if not carrier_code:
        return None

    try:
        response = amadeus.shopping.flight_offers_search.get(
            originLocationCode=origin,
            destinationLocationCode=destination,
            departureDate=dep.isoformat(),
            returnDate=ret.isoformat(),
            adults=travelers,
            currencyCode="USD",
            max=50,
        )
        offers = response.data
    except ResponseError as e:
        st.error(f"Amadeus flight search failed: [{e.response.status_code}] {e}")
        return None
    except Exception as e:
        st.error(f"Unexpected error during Amadeus flight search: {e}")
        return None

    prices = []
    for offer in offers:
        try:
            # Each offer can have multiple itineraries; check marketing carrier
            airlines = set(
                seg["carrierCode"]
                for itin in offer.get("itineraries", [])
                for seg in itin.get("segments", [])
            )
            if carrier_code not in airlines:
                continue

            total_str = offer["price"]["grandTotal"]
            total = float(total_str)
            # price is for ALL travelers; convert to per-person
            per_person = total / travelers if travelers > 0 else total
            prices.append(per_person)
        except Exception:
            continue

    if not prices:
        return None

    # Average, then round sensibly
    avg = sum(prices) / len(prices)
    return round(avg, 2)


HOTEL_BRANDS = ["Marriott", "Hilton", "Wyndham"]


def get_average_hotel_nightly_rate(
    amadeus: Client,
    city_code: str,
    check_in: date,
    check_out: date,
    preferred_brand: str,
) -> Optional[float]:
    """
    Use Amadeus Hotel Offers to get an average nightly rate in USD
    for hotels in a city that roughly match the preferred brand.

    NOTE: We use the low-level GET on '/v3/shopping/hotel-offers' because
    the high-level 'shopping.hotel_offers' attribute isn't available
    in every version of the Amadeus SDK.
    """
    params = {
        "cityCode": city_code,
        "checkInDate": check_in.isoformat(),
        "checkOutDate": check_out.isoformat(),
        "currencyCode": "USD",
        "adults": 1,
        "radius": 20,
        "radiusUnit": "MILE",
        "paymentPolicy": "NONE",
        "includeClosed": False,
    }

    try:
        response = amadeus.get("/v3/shopping/hotel-offers", **params)
        hotels = response.data
    except ResponseError as e:
        st.error(f"Amadeus hotel list failed: [{e.response.status_code}] {e}")
        return None
    except Exception as e:
        st.error(f"Unexpected error during Amadeus hotel search: {e}")
        return None

    nights = max((check_out - check_in).days, 1)
    brand_lower = preferred_brand.lower()
    nightly_rates = []

    for item in hotels:
        try:
            hotel_info = item.get("hotel", {})
            hotel_name = (hotel_info.get("name") or "").lower()

            # Very rough brand filter: look for brand name in hotel name
            if brand_lower not in hotel_name:
                continue

            offers = item.get("offers", [])
            for offer in offers:
                price = offer.get("price", {})
                total_str = price.get("total") or price.get("grandTotal")
                if not total_str:
                    continue
                total = float(total_str)
                nightly = total / nights
                nightly_rates.append(nightly)
        except Exception:
            continue

    if not nightly_rates:
        return None

    avg = sum(nightly_rates) / len(nightly_rates)
    # Round to nearest whole dollar – hotel forecasts don’t need cents
    return round(avg)


# -----------------------------------------------------------------------------
# 3. GSA per diem (meals only, via ZIP if possible)
# -----------------------------------------------------------------------------

def get_gsa_meals_per_diem(zip_code: Optional[str], dep: date) -> Optional[float]:
    """
    Fetch GSA M&IE (meals & incidental expenses) rate.

    We primarily use ZIP if available. If ZIP is missing, we fall back to None
    to avoid mysterious 404s on city/state combos.
    """
    if not zip_code:
        return None

    gsa_cfg = st.secrets.get("gsa")
    if not gsa_cfg or "api_key" not in gsa_cfg:
        return None

    api_key = gsa_cfg["api_key"]

    # GSA uses fiscal years (Oct 1 – Sep 30). Approximate fiscal year from departure date.
    fiscal_year = dep.year + 1 if dep.month >= 10 else dep.year

    url = "https://api.gsa.gov/travel/perdiem/v2/rates"
    params = {
        "zip": zip_code,
        "year": fiscal_year,
        "api_key": api_key,
    }

    try:
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        st.error(
            f"GSA per diem API error: {getattr(e, 'response', None) or e}"
        )
        return None

    # The exact schema can vary slightly; be defensive.
    try:
        rates = data.get("rates") or data.get("result") or []
        if not rates:
            return None

        # Pick the first rate row
        first = rates[0]
        # Common keys seen in GSA API: "m_and_ie" or "meals_and_incidentals"
        m_ie = first.get("m_and_ie") or first.get("meals_and_incidentals")
        if m_ie is None:
            return None
        return float(m_ie)
    except Exception:
        return None


# -----------------------------------------------------------------------------
# 4. Hertz SUV rental smart default (with hidden membership discount)
# -----------------------------------------------------------------------------

def estimate_hertz_suv_daily_rate(city: Optional[str], state: Optional[str]) -> float:
    """
    Smart-default estimator for Hertz SUV daily rate.

    - Base public rate: $80/day
    - City factor:
        * Major hub (Tampa, Orlando, Miami, Boston, NYC) => +10–15%
        * Otherwise => no change
    - Membership discount: ~15% (applied silently at the end)

    We hide the underlying percentages from auditors; they only see the final
    estimated daily rate.
    """
    base_public = 80.0

    city_key = (city or "").lower()
    state_key = (state or "").upper()

    # Rough list of higher-cost airport metro areas
    premium_cities = {"tampa", "orlando", "miami", "boston", "new york", "newark"}
    premium_states = {"CA", "NY", "MA", "FL"}

    city_factor = 1.0
    if city_key in premium_cities or state_key in premium_states:
        city_factor = 1.15  # 15% more expensive markets

    # Membership discount – hidden from UI
    membership_discount = 0.15  # 15% off

    public_rate = base_public * city_factor
    net_rate = public_rate * (1.0 - membership_discount)

    # Round to nice whole dollar
    return round(net_rate)


# -----------------------------------------------------------------------------
# 5. Streamlit UI
# -----------------------------------------------------------------------------

st.set_page_config(page_title="MIIP Trip Cost Calculator", layout="wide")

st.title("MIIP Trip Cost Calculator")
st.caption(
    "Automatically estimate flight, hotel, meals (GSA per diem), and rental car costs for audit trips."
)

# -------------------- Top: traveler & client info -----------------------------

col_left, col_right = st.columns(2)

with col_left:
    auditor_name = st.text_input("Auditor name", placeholder="e.g. Justin Mojica")

    travelers = st.number_input(
        "Number of travelers (each gets their own room)",
        min_value=1,
        max_value=10,
        value=1,
        step=1,
    )

    departure_airport = st.selectbox(
        "Departure airport (IATA)",
        options=["BOS", "MHT"],
        index=0,
        help="We usually fly out of BOS, but MHT is available as an option.",
    )

    destination_airport = st.text_input(
        "Destination airport (IATA, e.g. TPA)",
        value="TPA",
        help="3-letter airport code near the client office.",
    )

    preferred_airline = st.selectbox(
        "Preferred airline",
        options=list(AIRLINE_TO_CARRIER.keys()),
        index=1,  # JetBlue default
    )

with col_right:
    client_address = st.text_input(
        "Client office address",
        placeholder="123 Main St, Tampa, FL 33602",
        help="Used to infer city/ZIP for hotel search and GSA per diem.",
    )

    city, state, zip_code = parse_client_address(client_address)

    preferred_hotel_brand = st.selectbox(
        "Preferred hotel brand",
        options=HOTEL_BRANDS,
        index=0,
    )

# -------------------- Dates & ground costs ------------------------------------

st.subheader("Dates & ground costs")

col_dates, col_ground = st.columns(2)

with col_dates:
    today = date.today()
    departure_date = st.date_input(
        "Departure date",
        value=today,
        min_value=today,
    )

    return_date = st.date_input(
        "Return date",
        value=departure_date + timedelta(days=1),
        min_value=departure_date,
        help="Must be on or after the departure date.",
    )

with col_ground:
    include_rental = st.checkbox(
        "Include Hertz rental car",
        value=True,
        help="If checked, the tool will estimate Hertz SUV rental cost automatically using MIIP's internal membership-adjusted rate (hidden).",
    )

    other_fixed_costs = st.number_input(
        "Other fixed costs (USD)",
        min_value=0.0,
        value=0.0,
        step=10.0,
        format="%.2f",
        help="Any extra costs not covered elsewhere (parking, tolls, etc.).",
    )

trip_days, hotel_nights = compute_days_and_nights(departure_date, return_date)

if trip_days <= 0 or hotel_nights <= 0:
    st.error("Return date must be on or after departure date.")
    st.stop()

# -------------------- Section 3: Flights --------------------------------------

st.subheader("3. Flights (preferred airline)")

flight_mode = st.radio(
    "How should we calculate flights?",
    options=["Use Amadeus average (preferred airline)", "Enter manually"],
    index=0,
)

amadeus_fare_per_person = None
manual_flight_cost_per_person = None

if flight_mode == "Use Amadeus average (preferred airline)":
    st.info("Will query Amadeus for an average round-trip fare for the preferred airline.")

    amadeus_client = get_amadeus_client()
    with st.spinner("Querying Amadeus for flights..."):
        amadeus_fare_per_person = get_average_roundtrip_fare(
            amadeus_client,
            origin=departure_airport,
            destination=destination_airport,
            dep=departure_date,
            ret=return_date,
            travelers=travelers,
            preferred_airline=preferred_airline,
        )

    if amadeus_fare_per_person is None:
        st.error(
            "Unable to retrieve pricing from Amadeus. "
            "Please double-check airports, dates, and that your Production API keys are active. "
            "If the problem persists, use manual flight pricing."
        )
        flight_mode = "Enter manually"  # fall back to manual
else:
    st.info("You can enter the estimated round-trip flight cost per person manually.")

if flight_mode == "Enter manually":
    manual_flight_cost_per_person = st.number_input(
        "Manual flight cost per person (round trip, USD)",
        min_value=0.0,
        value=0.0,
        step=50.0,
        format="%.2f",
    )

# -------------------- Section 4: Hotel ---------------------------------------

st.subheader("4. Hotel (preferred brand)")

hotel_mode = st.radio(
    "How should we calculate hotel?",
    options=["Use Amadeus hotel average (preferred brand)", "Enter manually"],
    index=0,
)

amadeus_nightly_rate = None
manual_hotel_rate = None

# Use destination airport as cityCode; for many U.S. markets, this works.
city_code_for_hotels = destination_airport.upper().strip()

if hotel_mode == "Use Amadeus hotel average (preferred brand)":
    st.info(
        "Will query Amadeus for hotels in the destination city and average nightly rate "
        f"for {preferred_hotel_brand} brands."
    )
    amadeus_client = get_amadeus_client()
    with st.spinner("Querying Amadeus for hotels..."):
        amadeus_nightly_rate = get_average_hotel_nightly_rate(
            amadeus_client,
            city_code=city_code_for_hotels,
            check_in=departure_date,
            check_out=return_date,
            preferred_brand=preferred_hotel_brand,
        )

    if amadeus_nightly_rate is None:
        st.error(
            "Unable to retrieve hotel pricing from Amadeus. "
            "You can switch to 'Enter manually' to provide a nightly rate."
        )
        hotel_mode = "Enter manually"

if hotel_mode == "Enter manually":
    manual_hotel_rate = st.number_input(
        "Manual nightly hotel rate (USD)",
        min_value=0.0,
        value=110.0,
        step=10.0,
        format="%.2f",
    )

# -------------------- Section 5: Meals (GSA per diem – M&IE only) ------------

st.subheader("5. Meals (GSA Per Diem – M&IE only)")

meals_per_diem = None
if zip_code:
    with st.spinner("Querying GSA per diem (meals only)..."):
        meals_per_diem = get_gsa_meals_per_diem(zip_code, departure_date)

if meals_per_diem is not None:
    st.caption(
        f"Using GSA M&IE per diem for ZIP {zip_code} and fiscal year "
        f"{departure_date.year + 1 if departure_date.month >= 10 else departure_date.year}."
    )
else:
    st.warning(
        "Unable to retrieve meals per diem from GSA (missing or unrecognized ZIP). "
        "Meals will be treated as $0 unless you add them into 'Other fixed costs'."
    )

# -------------------- Calculate ------------------------------------------------

if st.button("Calculate trip cost"):
    # Flights
    if flight_mode == "Use Amadeus average (preferred airline)" and amadeus_fare_per_person is not None:
        flight_per_person = amadeus_fare_per_person
        flight_source = "Amadeus"
    else:
        flight_per_person = manual_flight_cost_per_person or 0.0
        flight_source = "Manual"

    flights_total = flight_per_person * travelers

    # Hotel
    if hotel_mode == "Use Amadeus hotel average (preferred brand)" and amadeus_nightly_rate is not None:
        nightly_rate = amadeus_nightly_rate
        hotel_source = "Amadeus"
    else:
        nightly_rate = manual_hotel_rate or 0.0
        hotel_source = "Manual"

    hotel_total = nightly_rate * hotel_nights * travelers

    # Meals
    per_diem_meals = meals_per_diem or 0.0
    meals_total = per_diem_meals * trip_days * travelers

    # Rental car
    rental_total = 0.0
    daily_rental_rate = 0.0
    if include_rental:
        daily_rental_rate = estimate_hertz_suv_daily_rate(city, state)
        rental_total = daily_rental_rate * hotel_nights

    # Grand total
    grand_total = flights_total + hotel_total + meals_total + rental_total + other_fixed_costs

    # -------------------- Summary ---------------------------------------------

    st.subheader("Trip cost summary")

    route_str = f"{departure_airport} → {destination_airport}"
    dates_str = f"{departure_date.isoformat()} to {return_date.isoformat()}"

    st.write(f"**Auditor(s):** {auditor_name or 'N/A'}")
    st.write(f"**Travelers:** {travelers}")
    st.write(f"**Route:** {route_str}")
    st.write(f"**Dates:** {dates_str} ({trip_days} day(s), {hotel_nights} night(s))")

    st.markdown("---")

    st.write(
        f"**Flights total** ({travelers} × {flight_per_person:,.2f} via {flight_source}): "
        f"{flights_total:,.2f}"
    )

    st.write(
        f"**Hotel total** ({travelers} rooms × {hotel_nights} night(s) × "
        f"{nightly_rate:,.2f}/night via {hotel_source}): "
        f"{hotel_total:,.2f}"
    )

    st.write(
        f"**Meals total** ({travelers} traveler(s) × {trip_days} day(s) × "
        f"{per_diem_meals:,.2f}/day): {meals_total:,.2f}"
    )

    if include_rental:
        st.write(
            f"**Rental car total** ({hotel_nights} day(s) × {daily_rental_rate:,.2f}/day, "
            f"Hertz SUV w/ membership pricing): {rental_total:,.2f}"
        )
    else:
        st.write("**Rental car total:** $0.00 (no rental selected)")

    st.write(f"**Other fixed costs:** {other_fixed_costs:,.2f}")

    st.markdown("### Grand total")
    st.markdown(f"## ${grand_total:,.2f}")
