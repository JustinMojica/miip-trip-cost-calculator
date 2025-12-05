import math
import os
import re
from datetime import date, timedelta
from typing import Optional, Tuple

import requests
import streamlit as st
from amadeus import Client, ResponseError


# -----------------------------------------------------------------------------
# Helpers: external clients
# -----------------------------------------------------------------------------
@st.cache_resource(show_spinner=False)
def get_amadeus_client() -> Client:
    """Create a cached Amadeus client using Streamlit secrets."""
    secrets = st.secrets["amadeus"]
    return Client(
        client_id=secrets["client_id"],
        client_secret=secrets["client_secret"],
        hostname=secrets.get("hostname", "production"),
    )


def get_gsa_api_key() -> Optional[str]:
    try:
        return st.secrets["gsa"]["api_key"]
    except Exception:
        return None


# -----------------------------------------------------------------------------
# Flights via Amadeus
# -----------------------------------------------------------------------------
AIRLINE_CODES = {
    "Delta": "DL",
    "Southwest": "WN",
    "JetBlue": "B6",
    "American": "AA",
}

def fetch_roundtrip_flight_avg(
    origin: str,
    destination: str,
    depart: date,
    ret: date,
    preferred_airline: str,
) -> Tuple[Optional[float], Optional[str]]:
    """
    Call Amadeus Flight Offers Search and return an average round-trip fare
    (per traveler) in USD for the preferred airline, if available.
    """
    client = get_amadeus_client()

    airline_code = AIRLINE_CODES.get(preferred_airline)

    params = {
        "originLocationCode": origin,
        "destinationLocationCode": destination,
        "departureDate": depart.isoformat(),
        "returnDate": ret.isoformat(),
        "adults": 1,
        "currencyCode": "USD",
    }
    if airline_code:
        params["includedAirlineCodes"] = airline_code

    try:
        resp = client.shopping.flight_offers_search.get(**params)
        offers = resp.data or []
        prices = []

        for offer in offers:
            try:
                total = float(offer["price"]["grandTotal"])
                prices.append(total)
            except Exception:
                continue

        if not prices:
            return None, "No priced offers returned for this route / airline."

        avg_price = sum(prices) / len(prices)
        return avg_price, None

    except ResponseError as e:
        return None, f"Amadeus flight search failed: [{e.response.status_code}] {e}"


# -----------------------------------------------------------------------------
# Hotels: smart estimates (no Amadeus)
# -----------------------------------------------------------------------------
# Simple mapping of destination airport -> typical corporate nightly rate for
# your preferred brands (Marriott / Hilton / Wyndham).  These are just
# starting points and can be tuned as you see actual data.
HOTEL_BASE_RATE_BY_AIRPORT = {
    # Higher-cost markets
    "BOS": 260.0,
    "JFK": 280.0,
    "LGA": 270.0,
    "EWR": 240.0,
    "LAX": 270.0,
    "SFO": 320.0,
    "MIA": 250.0,
    "FLL": 210.0,
    # Florida markets you mentioned
    "TPA": 190.0,
    "MCO": 185.0,
    "RSW": 195.0,
    # Add more as needed…
}

DEFAULT_HOTEL_NIGHTLY_RATE = 195.0  # catch-all estimate


def estimate_hotel_nightly_rate(dest_airport: str) -> float:
    """
    Smart nightly estimate based on destination airport.
    Can be refined as you get real data.
    """
    dest_airport = (dest_airport or "").upper().strip()
    return HOTEL_BASE_RATE_BY_AIRPORT.get(dest_airport, DEFAULT_HOTEL_NIGHTLY_RATE)


# -----------------------------------------------------------------------------
# Hertz rental car: smart estimates (membership-adjusted, hidden)
# -----------------------------------------------------------------------------
HERTZ_BASE_DAILY_BY_AIRPORT = {
    # Busy, more expensive markets
    "BOS": 95.0,
    "JFK": 110.0,
    "LGA": 105.0,
    "EWR": 100.0,
    "LAX": 115.0,
    "SFO": 120.0,
    "MIA": 100.0,
    "FLL": 90.0,
    # Florida markets you actually use
    "TPA": 80.0,
    "MCO": 82.0,
    "RSW": 78.0,
    # Catch-all
}
HERTZ_DEFAULT_BASE_DAILY = 75.0

# Hidden knobs – you can tweak these, auditors will only see the final price.
HERTZ_SUV_UPLIFT = 0.15           # SUVs cost ~15% more than compact.
HERTZ_MEMBERSHIP_DISCOUNT = 0.12  # 12% off for your corporate/membership rate.


def estimate_hertz_suv_daily_rate(dest_airport: str) -> float:
    """
    Smart Hertz SUV daily rate estimate with membership discount baked in.
    The auditors will ONLY see the final daily rate, not the discount %.
    """
    dest_airport = (dest_airport or "").upper().strip()
    base = HERTZ_BASE_DAILY_BY_AIRPORT.get(dest_airport, HERTZ_DEFAULT_BASE_DAILY)

    # Apply SUV uplift, then apply hidden membership discount.
    suv_price = base * (1.0 + HERTZ_SUV_UPLIFT)
    membership_adjusted = suv_price * (1.0 - HERTZ_MEMBERSHIP_DISCOUNT)
    return round(membership_adjusted, 2)


# -----------------------------------------------------------------------------
# GSA Meals & Incidental (M&IE) via ZIP
# -----------------------------------------------------------------------------
ZIP_RE = re.compile(r"\b(\d{5})(?:-\d{4})?\b")


def extract_zip_from_address(address: str) -> Optional[str]:
    if not address:
        return None
    m = ZIP_RE.search(address)
    if m:
        return m.group(1)
    return None


def guess_gsa_fiscal_year(travel_date: date) -> int:
    """
    GSA uses fiscal years starting Oct 1.
    For simplicity:
      - Jan–Sep: use calendar year
      - Oct–Dec: use calendar year + 1
    """
    if travel_date.month >= 10:
        return travel_date.year + 1
    return travel_date.year


def fetch_gsa_meals_rate(zip_code: str, travel_date: date) -> Tuple[Optional[float], Optional[str]]:
    """
    Fetch GSA M&IE daily rate (meals only) by ZIP.
    Returns (rate, error_message).
    """
    api_key = get_gsa_api_key()
    if not api_key:
        return None, "No GSA API key configured in secrets."

    fiscal_year = guess_gsa_fiscal_year(travel_date)

    # GSA API format using path segments, e.g.
    # https://api.gsa.gov/travel/perdiem/v2/rates/zip/33602/year/2025?api_key=...
    url = (
        f"https://api.gsa.gov/travel/perdiem/v2/rates/"
        f"zip/{zip_code}/year/{fiscal_year}?api_key={api_key}"
    )

    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        # Try a few likely key names for meals-only rate.
        rates = data.get("rates") or data.get("rate") or []
        mie = None
        for r in rates:
            mie = (
                r.get("meals_and_incidental_expenses")
                or r.get("meals_and_incidentals")
                or r.get("m_ie")
                or r.get("meals")
            )
            if mie:
                break

        if mie is None:
            return None, "GSA response did not include a meals (M&IE) field."

        return float(mie), None

    except requests.HTTPError as e:
        return None, f"GSA per diem API error: {e}"
    except Exception as e:
        return None, f"GSA per diem API error: {e}"


# -----------------------------------------------------------------------------
# Trip calculations
# -----------------------------------------------------------------------------
def calc_trip_days(depart: date, ret: date) -> int:
    delta = (ret - depart).days
    return max(delta + 1, 1)


def calc_trip_nights(depart: date, ret: date) -> int:
    # Nights are usually one less than days, but never < 0.
    return max((ret - depart).days, 0)


# -----------------------------------------------------------------------------
# UI
# -----------------------------------------------------------------------------
st.set_page_config(page_title="MIIP Trip Cost Calculator", layout="wide")

st.title("MIIP Trip Cost Calculator")
st.caption("Automatic estimate flight, hotel, meals (GSA per diem), and Hertz car costs for audit trips.")

# ─────────────────────────────────────────────────────────────────────────────
# Traveler & flights
# ─────────────────────────────────────────────────────────────────────────────
st.header("Traveler & flights")

col_a, col_b = st.columns(2)

with col_a:
    auditor_name = st.text_input("Auditor name", value="")

    travelers = st.number_input(
        "Number of travelers (each gets their own room)",
        min_value=1,
        step=1,
        value=1,
    )

    departure_airport = st.selectbox(
        "Departure airport (IATA)",
        options=["BOS", "MHT"],
        index=0,
        help="Home airport – defaults to BOS, but you can switch to MHT.",
    )

    preferred_airline = st.selectbox(
        "Preferred airline",
        options=list(AIRLINE_CODES.keys()),
        index=2,  # JetBlue
    )

with col_b:
    client_address = st.text_input(
        "Client office address",
        value="",
        help="Include city, state and ZIP if possible – we’ll use the ZIP for GSA meals.",
    )

    destination_airport = st.text_input(
        "Destination airport (IATA, e.g. TPA)",
        value="TPA",
    )

    preferred_hotel_brand = st.selectbox(
        "Preferred hotel brand",
        options=["Marriott", "Hilton", "Wyndham"],
        index=0,
    )

# ─────────────────────────────────────────────────────────────────────────────
# Dates & ground costs
# ─────────────────────────────────────────────────────────────────────────────
st.header("Dates & ground costs")

col_dates, col_ground = st.columns(2)

with col_dates:
    st.write("#### Dates")

    departure_date = st.date_input(
        "Departure date",
        value=date.today(),
        help="You can change this – it defaults to today because Streamlit requires an initial date.",
    )

    return_date = st.date_input(
        "Return date",
        value=departure_date + timedelta(days=1),
        min_value=departure_date + timedelta(days=1),
        help="Must be after the departure date.",
    )

with col_ground:
    st.write("#### Ground costs")

    include_rental_car = st.checkbox(
        "Include Hertz rental car",
        value=True,
        help="If checked, the tool will estimate a Hertz SUV rental cost automatically.",
    )

    other_fixed_costs = st.number_input(
        "Other fixed costs (USD)",
        min_value=0.0,
        step=10.0,
        value=0.0,
        help="Parking, tolls, etc. – if you want to lump them in.",
    )

# Basic date validation
date_error = None
if return_date <= departure_date:
    date_error = "Return date must be after the departure date."
    st.error(date_error)

trip_days = calc_trip_days(departure_date, return_date)
trip_nights = calc_trip_nights(departure_date, return_date)

# ─────────────────────────────────────────────────────────────────────────────
# 3. Flights
# ─────────────────────────────────────────────────────────────────────────────
st.header("3. Flights (preferred airline)")

flight_mode = st.radio(
    "How should we calculate flights?",
    ["Use Amadeus average (preferred airline)", "Enter manually"],
    index=0,
)

manual_flight_cost = st.number_input(
    "Manual flight cost per person (round trip, USD)",
    min_value=0.0,
    step=50.0,
    value=0.0,
    help="Only used if 'Enter manually' is selected.",
)

flight_cost_per_person = 0.0
flight_debug_msg = ""

if date_error:
    st.warning("Fix the date error above to enable flight pricing.")
else:
    if flight_mode == "Use Amadeus average (preferred airline)":
        st.info("Will query Amadeus for an average round trip fare for the preferred airline.")
        avg_price, err = fetch_roundtrip_flight_avg(
            origin=departure_airport,
            destination=destination_airport,
            depart=departure_date,
            ret=return_date,
            preferred_airline=preferred_airline,
        )
        if err:
            st.error(err)
            flight_debug_msg = err
            flight_cost_per_person = 0.0
        else:
            flight_cost_per_person = avg_price
            st.success(f"Amadeus average round trip fare (per person): ${avg_price:,.2f}")
    else:
        flight_cost_per_person = manual_flight_cost
        if manual_flight_cost <= 0:
            st.warning("Manual flight cost is 0 – flights will be treated as $0 in the total.")

# ─────────────────────────────────────────────────────────────────────────────
# 4. Hotel – smart estimate only
# ─────────────────────────────────────────────────────────────────────────────
st.header("4. Hotel (preferred brand – smart estimate)")

st.info(
    "Hotel pricing now uses a smart nightly estimate based on the destination airport "
    "and typical rates for your preferred brand. No manual nightly entry and no hotel API "
    "calls, so it always works."
)

hotel_nightly_rate = estimate_hotel_nightly_rate(destination_airport)
hotel_total = hotel_nightly_rate * trip_nights * travelers

st.write(
    f"- Estimated nightly rate for {preferred_hotel_brand} near {destination_airport}: "
    f"**${hotel_nightly_rate:,.2f}**"
)
st.write(f"- Trip nights: **{trip_nights}**")
st.write(f"- Travelers / rooms: **{travelers}** (one room per traveler)")

# ─────────────────────────────────────────────────────────────────────────────
# 5. Meals – GSA M&IE by ZIP
# ─────────────────────────────────────────────────────────────────────────────
st.header("5. Meals (GSA Per Diem – M&IE only)")

auto_zip = extract_zip_from_address(client_address)
zip_override = st.text_input(
    "Destination ZIP for GSA (optional override)",
    value=auto_zip or "",
    help="If blank, we’ll try to detect a 5-digit ZIP from the client office address above.",
)

effective_zip = zip_override.strip() or (auto_zip or "").strip()

meals_rate_per_day = 0.0
meals_debug_msg = ""

if not effective_zip:
    st.warning(
        "Could not detect a valid ZIP from the client office address. "
        "Enter a ZIP above to enable GSA meals per diem."
    )
else:
    mie_rate, err = fetch_gsa_meals_rate(effective_zip, departure_date)
    if err:
        st.error(err)
        meals_debug_msg = err
        st.warning(
            "Unable to retrieve meals per diem from GSA. "
            "Meals will be treated as $0 unless you add them into 'Other fixed costs'."
        )
    else:
        meals_rate_per_day = mie_rate
        st.success(
            f"GSA M&IE daily rate for ZIP {effective_zip} (fiscal year estimate): "
            f"**${mie_rate:,.2f} per day**"
        )

meals_total = meals_rate_per_day * trip_days * travelers

# ─────────────────────────────────────────────────────────────────────────────
# 6. Hertz rental car – smart estimate
# ─────────────────────────────────────────────────────────────────────────────
st.header("6. Hertz rental car (smart estimate)")

rental_car_total = 0.0
rental_daily_rate = 0.0

if include_rental_car:
    rental_daily_rate = estimate_hertz_suv_daily_rate(destination_airport)
    rental_car_total = rental_daily_rate * trip_days
    st.info(
        "Using a smart Hertz SUV daily rate estimate with a hidden membership discount "
        "already applied. Auditors only see the final price."
    )
    st.write(
        f"- Estimated Hertz SUV daily rate near {destination_airport}: "
        f"**${rental_daily_rate:,.2f} / day**"
    )
    st.write(f"- Rental days: **{trip_days}**")
else:
    st.write("Rental car not included in this estimate.")

# ─────────────────────────────────────────────────────────────────────────────
# Final calculation & summary
# ─────────────────────────────────────────────────────────────────────────────
st.header("7. Trip cost summary")

if date_error:
    st.error("Cannot calculate totals until the date error above is fixed.")
else:
    flights_total = flight_cost_per_person * travelers
    hotel_total = hotel_nightly_rate * trip_nights * travelers
    grand_total = (
        flights_total
        + hotel_total
        + meals_total
        + rental_car_total
        + other_fixed_costs
    )

    st.subheader("Breakdown")

    st.write(f"**Route:** {departure_airport} → {destination_airport}")
    st.write(
        f"**Dates:** {departure_date.isoformat()} to {return_date.isoformat()} "
        f"({trip_days} day(s), {trip_nights} night(s))"
    )
    st.write(f"**Travelers:** {travelers}")

    st.write("---")
    st.write(f"**Flights total:** ${flights_total:,.2f}")
    if flight_cost_per_person > 0:
        st.caption(
            f"{travelers} traveler(s) × ${flight_cost_per_person:,.2f} "
            f"(Amadeus avg for {preferred_airline} or manual entry)."
        )
    else:
        st.caption("Flights treated as $0 (no price available).")

    st.write(f"**Hotel total:** ${hotel_total:,.2f}")
    st.caption(
        f"{travelers} room(s) × {trip_nights} night(s) × "
        f"${hotel_nightly_rate:,.2f}/night (smart estimate for {preferred_hotel_brand})."
    )

    st.write(f"**Meals total:** ${meals_total:,.2f}")
    if meals_rate_per_day > 0:
        st.caption(
            f"{travelers} traveler(s) × {trip_days} day(s) × "
            f"${meals_rate_per_day:,.2f}/day (GSA M&IE per diem)."
        )
    else:
        st.caption("Meals treated as $0 (GSA rate not available or ZIP missing).")

    st.write(f"**Rental car total:** ${rental_car_total:,.2f}")
    if include_rental_car and rental_daily_rate > 0:
        st.caption(
            f"{trip_days} day(s) × ${rental_daily_rate:,.2f}/day "
            "(Hertz SUV, membership-adjusted estimate)."
        )
    elif not include_rental_car:
        st.caption("Rental car not included.")
    else:
        st.caption("Rental car treated as $0.")

    st.write(f"**Other fixed costs:** ${other_fixed_costs:,.2f}")

    st.write("## Grand total")
    st.success(f"${grand_total:,.2f}")

    st.caption(
        "Notes: Flights use Amadeus Production APIs where available. "
        "Hotels use a smart nightly estimate by destination airport. "
        "Meals use GSA M&IE per diem by ZIP when available. "
        "Hertz rental car prices are smart membership-adjusted estimates for SUVs."
    )
