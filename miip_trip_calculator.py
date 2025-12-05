from datetime import date, timedelta
from typing import Optional, Tuple

import streamlit as st
from amadeus import Client, ResponseError

# -----------------------------------------------------------------------------
# Amadeus client (flights only)
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
            # Skip any malformed offers
                continue

        if not prices:
            return None, "No priced offers returned for this route / airline."

        avg_price = sum(prices) / len(prices)
        return avg_price, None

    except ResponseError as e:
        return None, f"Amadeus flight search failed: [{e.response.status_code}] {e}"


# -----------------------------------------------------------------------------
# Hotels: smart estimates (no API)
# Business-realistic per-airport nightly rates
# -----------------------------------------------------------------------------
# These are *estimate* corporate nightly rates (before tax/fees) for
# typical Marriott/Hilton/Wyndham-type properties near each airport.
# You can tweak any single airport later if you see consistent differences.
HOTEL_BASE_RATE_BY_AIRPORT = {
    # Northeast / NYC / Boston
    "BOS": 260.0,
    "JFK": 310.0,
    "LGA": 300.0,
    "EWR": 260.0,
    "BDL": 190.0,
    "PVD": 185.0,
    "ALB": 175.0,
    "PWM": 190.0,

    # Mid-Atlantic / DC / Philly / Baltimore
    "DCA": 260.0,
    "IAD": 260.0,
    "BWI": 210.0,
    "PHL": 220.0,
    "RIC": 185.0,
    "ORF": 185.0,

    # New York State / NJ (non-NYC)
    "BUF": 170.0,
    "ROC": 165.0,
    "SYR": 165.0,
    "HPN": 240.0,
    "SWF": 180.0,

    # Southeast big hubs
    "ATL": 210.0,
    "CLT": 195.0,
    "RDU": 190.0,
    "BNA": 200.0,
    "CHS": 200.0,
    "GSP": 175.0,

    # Florida
    "MIA": 260.0,
    "FLL": 220.0,
    "PBI": 220.0,
    "TPA": 195.0,
    "MCO": 190.0,
    "RSW": 195.0,
    "JAX": 180.0,
    "SRQ": 195.0,
    "ECP": 185.0,
    "PNS": 185.0,

    # Midwest / Great Lakes
    "ORD": 260.0,
    "MDW": 230.0,
    "MSP": 220.0,
    "DTW": 200.0,
    "CLE": 175.0,
    "CMH": 175.0,
    "IND": 175.0,
    "MKE": 185.0,
    "STL": 185.0,
    "CVG": 180.0,
    "PIT": 180.0,
    "DSM": 170.0,
    "MCI": 185.0,
    "OMA": 180.0,

    # Texas
    "DFW": 195.0,
    "DAL": 185.0,
    "IAH": 195.0,
    "HOU": 185.0,
    "AUS": 220.0,
    "SAT": 190.0,
    "ELP": 175.0,

    # West Coast / California
    "LAX": 280.0,
    "BUR": 250.0,
    "SNA": 260.0,
    "LGB": 240.0,
    "SAN": 260.0,
    "SFO": 320.0,
    "OAK": 270.0,
    "SJC": 275.0,
    "SMF": 210.0,
    "PSP": 240.0,
    "ONT": 210.0,

    # Pacific Northwest
    "SEA": 260.0,
    "PDX": 220.0,
    "GEG": 185.0,
    "BOI": 185.0,

    # Mountain / Rockies
    "DEN": 230.0,
    "SLC": 200.0,
    "COS": 185.0,
    "ABQ": 180.0,
    "BZN": 230.0,
    "JAC": 250.0,

    # Desert / Southwest
    "PHX": 210.0,
    "TUS": 185.0,
    "LAS": 220.0,
    "RNO": 195.0,

    # West / Other
    "FAT": 190.0,
    "RAP": 180.0,

    # Hawaii / Alaska
    "HNL": 320.0,
    "OGG": 340.0,
    "KOA": 320.0,
    "LIH": 330.0,
    "ANC": 260.0,
    "FAI": 240.0,
}

# Default if airport is not specifically in the table.
DEFAULT_HOTEL_NIGHTLY_RATE = 190.0


def estimate_hotel_nightly_rate(dest_airport: str) -> float:
    """
    Smart nightly estimate based on destination airport.
    Falls back to DEFAULT_HOTEL_NIGHTLY_RATE only if not in the table.
    """
    dest_airport = (dest_airport or "").upper().strip()
    return HOTEL_BASE_RATE_BY_AIRPORT.get(dest_airport, DEFAULT_HOTEL_NIGHTLY_RATE)


# -----------------------------------------------------------------------------
# Hertz rental car: smart estimates (membership-adjusted, hidden)
# Business-realistic daily rates per airport
# -----------------------------------------------------------------------------
HERTZ_BASE_DAILY_BY_AIRPORT = {
    # Northeast / NYC / Boston
    "BOS": 95.0,
    "JFK": 115.0,
    "LGA": 112.0,
    "EWR": 110.0,
    "BDL": 80.0,
    "PVD": 78.0,
    "ALB": 78.0,
    "PWM": 82.0,

    # Mid-Atlantic / DC / Philly / Baltimore
    "DCA": 100.0,
    "IAD": 98.0,
    "BWI": 88.0,
    "PHL": 92.0,
    "RIC": 80.0,
    "ORF": 80.0,

    # New York State / NJ (non-NYC)
    "BUF": 78.0,
    "ROC": 76.0,
    "SYR": 76.0,
    "HPN": 90.0,
    "SWF": 78.0,

    # Southeast big hubs
    "ATL": 90.0,
    "CLT": 85.0,
    "RDU": 84.0,
    "BNA": 86.0,
    "CHS": 88.0,
    "GSP": 80.0,

    # Florida
    "MIA": 95.0,
    "FLL": 88.0,
    "PBI": 90.0,
    "TPA": 82.0,
    "MCO": 84.0,
    "RSW": 82.0,
    "JAX": 78.0,
    "SRQ": 82.0,
    "ECP": 76.0,
    "PNS": 76.0,

    # Midwest / Great Lakes
    "ORD": 95.0,
    "MDW": 92.0,
    "MSP": 92.0,
    "DTW": 88.0,
    "CLE": 80.0,
    "CMH": 80.0,
    "IND": 80.0,
    "MKE": 82.0,
    "STL": 82.0,
    "CVG": 80.0,
    "PIT": 80.0,
    "DSM": 78.0,
    "MCI": 80.0,
    "OMA": 80.0,

    # Texas
    "DFW": 86.0,
    "DAL": 84.0,
    "IAH": 86.0,
    "HOU": 84.0,
    "AUS": 90.0,
    "SAT": 84.0,
    "ELP": 78.0,

    # West Coast / California
    "LAX": 105.0,
    "BUR": 98.0,
    "SNA": 100.0,
    "LGB": 95.0,
    "SAN": 95.0,
    "SFO": 110.0,
    "OAK": 98.0,
    "SJC": 100.0,
    "SMF": 88.0,
    "PSP": 96.0,
    "ONT": 88.0,

    # Pacific Northwest
    "SEA": 95.0,
    "PDX": 88.0,
    "GEG": 80.0,
    "BOI": 80.0,

    # Mountain / Rockies
    "DEN": 95.0,
    "SLC": 88.0,
    "COS": 82.0,
    "ABQ": 80.0,
    "BZN": 90.0,
    "JAC": 98.0,

    # Desert / Southwest
    "PHX": 90.0,
    "TUS": 82.0,
    "LAS": 90.0,
    "RNO": 84.0,

    # West / Other
    "FAT": 82.0,
    "RAP": 80.0,

    # Hawaii / Alaska
    "HNL": 110.0,
    "OGG": 112.0,
    "KOA": 108.0,
    "LIH": 110.0,
    "ANC": 95.0,
    "FAI": 90.0,
}

HERTZ_DEFAULT_BASE_DAILY = 80.0

# Hidden knobs – realistic + conservative.
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
# Meals: smart formula (base + uplift by city type)
# -----------------------------------------------------------------------------
BASE_MEAL_RATE = 100.0  # USD per traveler per day

# +25% expensive markets
EXPENSIVE_AIRPORTS = {
    # Boston / NYC
    "BOS", "JFK", "LGA", "EWR",
    # DC
    "DCA", "IAD",
    # Chicago
    "ORD", "MDW",
    # California big coastal
    "SFO", "OAK", "SJC", "LAX", "SAN",
    # Seattle
    "SEA",
    # South Florida
    "MIA", "FLL",
    # Hawaii
    "HNL", "OGG", "KOA", "LIH",
    # Denver (high cost of dining)
    "DEN",
}

# +10% mid-tier markets
MID_TIER_AIRPORTS = {
    # East / Southeast
    "PHL", "BWI", "CLT", "RDU", "BNA", "ATL", "MCO", "TPA", "RSW", "PBI",
    # Midwest
    "MSP", "DTW", "CLE", "CMH", "IND", "STL", "MKE", "PIT",
    # Texas
    "DFW", "DAL", "IAH", "HOU", "AUS", "SAT",
    # West
    "PDX", "SMF", "SNA", "BUR", "LGB", "ONT", "PHX", "LAS", "RNO",
    # Mountain
    "SLC", "ABQ",
}


def estimate_meal_rate_per_day(dest_airport: str) -> float:
    """
    Meal formula:
      - Base $100/day per traveler
      - +25% for expensive airports
      - +10% for mid-tier airports
    """
    dest_airport = (dest_airport or "").upper().strip()
    rate = BASE_MEAL_RATE

    if dest_airport in EXPENSIVE_AIRPORTS:
        rate *= 1.25
    elif dest_airport in MID_TIER_AIRPORTS:
        rate *= 1.10

    return round(rate, 2)


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
st.caption("Automatic estimate of flights, hotel, meals, and Hertz car costs for audit trips.")

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
        help="Currently used only for documentation in this version.",
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
        help="Defaults to today because Streamlit requires an initial date.",
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
    "Hotel pricing uses a smart nightly estimate based on the destination airport "
    "and typical rates for your preferred brand. No manual nightly entry and no hotel API "
    "calls, so it always works."
)

hotel_nightly_rate = estimate_hotel_nightly_rate(destination_airport)
hotel_total = hotel_nightly_rate * trip_nights * travelers

st.write(
    f"- Estimated nightly rate for {preferred_hotel_brand} near {destination_airport.upper()}: "
    f"**${hotel_nightly_rate:,.2f}**"
)
st.write(f"- Trip nights: **{trip_nights}**")
st.write(f"- Travelers / rooms: **{travelers}** (one room per traveler)")

# ─────────────────────────────────────────────────────────────────────────────
# 5. Meals – smart formula (base + uplift by city tier)
# ─────────────────────────────────────────────────────────────────────────────
st.header("5. Meals (smart estimate)")

meal_rate_per_day = estimate_meal_rate_per_day(destination_airport)
dest_upper = destination_airport.upper()

if dest_upper in EXPENSIVE_AIRPORTS:
    st.info(
        f"{dest_upper} is treated as a **high-cost city** (Boston/NYC/LA/SF/DC/etc). "
        f"Base $100/day increased by 25%."
    )
elif dest_upper in MID_TIER_AIRPORTS:
    st.info(
        f"{dest_upper} is treated as a **mid-tier city** (Austin, Denver, Charlotte, etc.). "
        f"Base $100/day increased by 10%."
    )
else:
    st.info("Destination treated as a standard-cost city with a base $100/day meal rate.")

st.write(
    f"- Meal rate per traveler per day: **${meal_rate_per_day:,.2f}**"
)
meals_total = meal_rate_per_day * trip_days * travelers

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
        f"- Estimated Hertz SUV daily rate near {destination_airport.upper()}: "
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

    st.write(f"**Route:** {departure_airport} → {destination_airport.upper()}")
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
    st.caption(
        f"{travelers} traveler(s) × {trip_days} day(s) × "
        f"${meal_rate_per_day:,.2f}/day (smart formula with city-tier uplift)."
    )

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
        "Hotels use business-realistic nightly estimates by destination airport. "
        "Meals use a $100/day base with +25% for expensive cities and +10% for mid-tier cities. "
        "Hertz rental car prices are smart membership-adjusted estimates for SUVs."
    )
