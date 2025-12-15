import datetime as dt
from statistics import mean
from typing import Optional, List, Tuple

import streamlit as st
from amadeus import Client, ResponseError

# ---------------------------------------------------------
# Page config & styling
# ---------------------------------------------------------

st.set_page_config(page_title="Expense Calculator", layout="wide")

st.markdown(
    """
    <style>
    .miip-title { font-size: 2rem; font-weight: 700; margin-bottom: 0.15rem; }
    .miip-subtitle { font-size: 0.95rem; color: #c4c4c4; margin-bottom: 1.5rem; }
    .miip-section-title { font-size: 1.05rem; font-weight: 600; margin-bottom: 0.3rem; }
    .miip-geek-math p, .miip-geek-math li { margin-bottom: 0.2rem !important; }
    </style>
    """,
    unsafe_allow_html=True,
)

st.markdown('<div class="miip-title">Expense Calculator</div>', unsafe_allow_html=True)
st.markdown(
    '<div class="miip-subtitle">Estimate audit trip costs for flights, hotel, meals, and Hertz rental car.</div>',
    unsafe_allow_html=True,
)

# ---------------------------------------------------------
# Constants
# ---------------------------------------------------------

AIRLINE_CODES = {"Delta": "DL", "Southwest": "WN", "JetBlue": "B6", "American": "AA"}
DOMESTIC_BAG_FEE_BY_AIRLINE = {"Southwest": 0.0, "JetBlue": 70.0, "Delta": 70.0, "American": 70.0}

US_AIRPORTS = {
    "BOS","MHT","JFK","LGA","EWR","PHL","DCA","IAD","BWI","CLT","ATL","MCO","TPA",
    "MIA","FLL","ORD","MDW","DFW","DAL","IAH","HOU","DEN","PHX","LAS","LAX","SFO","SEA",
    "HNL","OGG","LIH","KOA"
}

HOTEL_BASE_RATE_BY_AIRPORT = {
    "BOS": 260.0, "JFK": 280.0, "LGA": 270.0, "EWR": 260.0,
    "LAX": 260.0, "SFO": 280.0, "SEA": 250.0, "DEN": 210.0,
    "MCO": 210.0, "TPA": 215.0, "MIA": 260.0, "CLT": 190.0,
    "PHL": 210.0, "ORD": 230.0, "ATL": 210.0,
}
DEFAULT_HOTEL_NIGHTLY_RATE = 190.0

HERTZ_BASE_DAILY_BY_AIRPORT = {
    "BOS": 70.0, "MHT": 60.0, "JFK": 75.0, "LGA": 75.0, "EWR": 72.0,
    "TPA": 65.0, "MCO": 65.0, "MIA": 70.0, "DEN": 68.0,
    "SFO": 78.0, "LAX": 78.0, "SEA": 72.0,
}
HERTZ_SUV_UPLIFT = 0.15
HERTZ_MEMBERSHIP_DISCOUNT = 0.12

MEALS_PER_DAY = 100.0
CONTINGENCY_RATE = 0.05  # 5%

# Fixed add-ons (always included)
GAS_COST = 60.0
TOLLS_COST = 35.0
PARKING_COST = 50.0
AIRPORT_SHUTTLE_TIPS = 10.0
HOUSEKEEPING_PER_NIGHT = 10.0  # per night per traveler

# ---------------------------------------------------------
# Helpers
# ---------------------------------------------------------

def try_get_amadeus_client() -> Tuple[Optional[Client], Optional[str]]:
    """
    Returns (client, error_message). Never throws.
    Uses keyword args to match Amadeus SDK.
    """
    try:
        if "amadeus" not in st.secrets:
            return None, "Missing [amadeus] section in Streamlit secrets."
        cfg = st.secrets["amadeus"]

        client_id = cfg.get("client_id")
        client_secret = cfg.get("client_secret")
        hostname = cfg.get("hostname", "production")

        if not client_id or not client_secret:
            return None, "Missing amadeus.client_id or amadeus.client_secret in Streamlit secrets."

        # ✅ Correct Amadeus SDK initialization
        client = Client(
            client_id=client_id,
            client_secret=client_secret,
            hostname=hostname,
        )
        return client, None
    except Exception as exc:
        # This catches SDK init errors too (wrong types, etc.)
        return None, f"Amadeus client init error: {exc}"


def is_domestic(origin: str, dest: str) -> bool:
    return origin.upper() in US_AIRPORTS and dest.upper() in US_AIRPORTS


def hotel_rate(dest: str) -> float:
    return HOTEL_BASE_RATE_BY_AIRPORT.get(dest.upper(), DEFAULT_HOTEL_NIGHTLY_RATE)


def hertz_rate(dest: str) -> float:
    base = HERTZ_BASE_DAILY_BY_AIRPORT.get(dest.upper(), 80.0)
    return round(base * (1 + HERTZ_SUV_UPLIFT) * (1 - HERTZ_MEMBERSHIP_DISCOUNT), 2)


def avg_flight_cost(
    client: Client,
    origin: str,
    dest: str,
    dep: dt.date,
    ret: dt.date,
    preferred_airline: str,
) -> Tuple[Optional[float], str]:
    """
    Returns (avg_price, status)
    status in {"preferred", "fallback_all", "none", "error"}
    """
    preferred_code = AIRLINE_CODES.get(preferred_airline)

    try:
        resp = client.shopping.flight_offers_search.get(
            originLocationCode=origin,
            destinationLocationCode=dest,
            departureDate=dep.isoformat(),
            returnDate=ret.isoformat(),
            adults=1,
            currencyCode="USD",
            max=20,
        )
        offers = resp.data or []
    except ResponseError:
        return None, "error"
    except Exception:
        return None, "error"

    if not offers:
        return None, "none"

    all_prices: List[float] = []
    preferred_prices: List[float] = []

    for o in offers:
        try:
            price = float(o["price"]["grandTotal"])
        except Exception:
            continue

        all_prices.append(price)

        if preferred_code:
            validating = o.get("validatingAirlineCodes") or o.get("validatingAirlineCode")
            if isinstance(validating, list) and preferred_code in validating:
                preferred_prices.append(price)
            elif isinstance(validating, str) and validating == preferred_code:
                preferred_prices.append(price)

    if preferred_prices:
        return round(mean(preferred_prices), 2), "preferred"

    if all_prices:
        return round(mean(all_prices), 2), "fallback_all"

    return None, "none"


# ---------------------------------------------------------
# Inputs
# ---------------------------------------------------------

l, r = st.columns(2)

with l:
    st.markdown('<div class="miip-section-title">Traveler & flights</div>', unsafe_allow_html=True)
    travelers = st.number_input("Number of travelers", min_value=1, value=1, step=1, help="One room per traveler")
    dep_airport = st.selectbox("Departure airport", ["BOS", "MHT"])
    preferred_airline = st.selectbox("Preferred airline", list(AIRLINE_CODES.keys()))
    dest_airport = st.text_input("Destination airport", help="3-letter IATA code").strip().upper()

with r:
    st.markdown('<div class="miip-section-title">Client & hotel options</div>', unsafe_allow_html=True)
    assignment = st.text_input("Assignment city and state", help="City, State")
    hotel_brand = st.selectbox("Preferred hotel brand", ["Marriott", "Hilton", "Wyndham"])

d, g = st.columns(2)
today = dt.date.today()

with d:
    st.markdown('<div class="miip-section-title">Dates</div>', unsafe_allow_html=True)
    dep_date = st.date_input("Departure date", today, format="MM/DD/YYYY")
    ret_date = st.date_input(
        "Return date",
        dep_date + dt.timedelta(days=1),
        min_value=dep_date + dt.timedelta(days=1),
        format="MM/DD/YYYY",
    )

with g:
    st.markdown('<div class="miip-section-title">Ground costs</div>', unsafe_allow_html=True)
    include_rental = st.checkbox("Include Hertz rental SUV", value=True)
    other_fixed = st.number_input("Other fixed costs", min_value=0.0, value=0.0, step=50.0)

# ---------------------------------------------------------
# Calculations
# ---------------------------------------------------------

trip_days = (ret_date - dep_date).days + 1
trip_nights = max(trip_days - 1, 0)

st.markdown('<div class="miip-section-title">Flights</div>', unsafe_allow_html=True)
mode = st.radio("", ["Auto calculate", "Enter manually"])

flight_pp = 0.0
flight_status = "manual"

if mode == "Enter manually":
    flight_pp = st.number_input("Manual flight cost per traveler", min_value=0.0, value=0.0, step=50.0)
    flight_status = "manual"
else:
    if len(dest_airport) != 3:
        st.warning("Enter a valid 3-letter destination airport code to auto-calculate flights.")
        flight_status = "blocked"
    else:
        amadeus_client, amadeus_err = try_get_amadeus_client()
        if amadeus_client is None:
            # ✅ Don’t “break” the app — just warn and allow manual
            st.warning(f"Auto-calculate unavailable: {amadeus_err} Switch to manual flight entry.")
            flight_status = "no_secrets"
        else:
            avg_price, status = avg_flight_cost(amadeus_client, dep_airport, dest_airport, dep_date, ret_date, preferred_airline)
            if status == "error":
                st.error("Amadeus flight search failed. You can switch to manual entry.")
                flight_status = "error"
            elif status == "none" or avg_price is None:
                st.error("Amadeus returned no offers for this route/dates. You can enter flights manually.")
                flight_status = "none"
            elif status == "preferred":
                flight_pp = avg_price
                st.caption(f"Estimated average round-trip fare per traveler for **{preferred_airline}**: **${flight_pp:,.0f}**.")
                flight_status = "preferred"
            else:
                # fallback_all
                flight_pp = avg_price
                st.warning(
                    f"No usable prices found for the preferred airline only; using average of available airlines instead. "
                    f"Average used: **${flight_pp:,.0f}**."
                )
                flight_status = "fallback_all"

flights_total = flight_pp * travelers

bag_fee_per_traveler = (
    DOMESTIC_BAG_FEE_BY_AIRLINE.get(preferred_airline, 70.0)
    if (len(dest_airport) == 3 and is_domestic(dep_airport, dest_airport))
    else 0.0
)
bags_total = bag_fee_per_traveler * travelers

nightly_hotel_rate = hotel_rate(dest_airport) if len(dest_airport) == 3 else DEFAULT_HOTEL_NIGHTLY_RATE
hotel_total = nightly_hotel_rate * trip_nights * travelers

meals_total = MEALS_PER_DAY * trip_days * travelers

daily_rental_rate = hertz_rate(dest_airport) if (include_rental and len(dest_airport) == 3) else (hertz_rate(dep_airport) if include_rental else 0.0)
rental_total = daily_rental_rate * trip_days if include_rental else 0.0

housekeeping_total = HOUSEKEEPING_PER_NIGHT * trip_nights * travelers
fixed_incidentals_total = GAS_COST + TOLLS_COST + PARKING_COST + AIRPORT_SHUTTLE_TIPS + housekeeping_total

subtotal = (
    flights_total
    + bags_total
    + hotel_total
    + meals_total
    + rental_total
    + fixed_incidentals_total
    + other_fixed
)

contingency = subtotal * CONTINGENCY_RATE
grand_total = subtotal + contingency

# ---------------------------------------------------------
# Summary
# ---------------------------------------------------------

st.markdown('<div class="miip-section-title">Trip cost summary</div>', unsafe_allow_html=True)

st.write(f"- Flights total: **${flights_total:,.0f}**")
st.write(f"- Checked bags total: **${bags_total:,.0f}**")
st.write(f"- Hotel total: **${hotel_total:,.0f}**")
st.write(f"- Meals total: **${meals_total:,.0f}**")
st.write(f"- Rental car total: **${rental_total:,.0f}**")

st.write("**Fixed incidentals**")
st.write(f"- Gas: **${GAS_COST:,.0f}**")
st.write(f"- Tolls: **${TOLLS_COST:,.0f}**")
st.write(f"- Parking: **${PARKING_COST:,.0f}**")
st.write(f"- Airport shuttle tips: **${AIRPORT_SHUTTLE_TIPS:,.0f}**")
st.write(f"- Housekeeping: **${housekeeping_total:,.0f}**")

st.write(f"- Other fixed costs: **${other_fixed:,.0f}**")

st.success(f"Grand total: ${grand_total:,.0f}")

# ---------------------------------------------------------
# Geek math (full breakdown)
# ---------------------------------------------------------

with st.expander("Show detailed cost math", expanded=False):
    st.markdown('<div class="miip-geek-math">', unsafe_allow_html=True)

    st.markdown("**Trip length**")
    st.markdown(f"- Trip days = `{trip_days}`")
    st.markdown(f"- Trip nights = `{trip_nights}`")

    st.markdown("**Flights**")
    st.markdown(f"- Flight per traveler = `${flight_pp:,.2f}`")
    st.markdown(f"- Flights total = `${flight_pp:,.2f} × {travelers}` = `${flights_total:,.2f}`")

    st.markdown("**Checked bags**")
    st.markdown(f"- Bag fee per traveler = `${bag_fee_per_traveler:,.2f}`")
    st.markdown(f"- Bags total = `${bag_fee_per_traveler:,.2f} × {travelers}` = `${bags_total:,.2f}`")

    st.markdown("**Hotel**")
    st.markdown(f"- Nightly hotel rate = `${nightly_hotel_rate:,.2f}`")
    st.markdown(f"- Hotel total = `${nightly_hotel_rate:,.2f} × {trip_nights} × {travelers}` = `${hotel_total:,.2f}`")

    st.markdown("**Meals**")
    st.markdown(f"- Meals total = `$100 × {trip_days} × {travelers}` = `${meals_total:,.2f}`")

    st.markdown("**Rental car**")
    if include_rental:
        st.markdown(f"- Hertz daily rate = `${daily_rental_rate:,.2f}`")
        st.markdown(f"- Rental total = `${daily_rental_rate:,.2f} × {trip_days}` = `${rental_total:,.2f}`")
    else:
        st.markdown("- Rental excluded")

    st.markdown("**Fixed incidentals**")
    st.markdown(f"- Gas = `${GAS_COST:,.2f}`")
    st.markdown(f"- Tolls = `${TOLLS_COST:,.2f}`")
    st.markdown(f"- Parking = `${PARKING_COST:,.2f}`")
    st.markdown(f"- Airport shuttle tips = `${AIRPORT_SHUTTLE_TIPS:,.2f}`")
    st.markdown(f"- Housekeeping = `$10 × {trip_nights} × {travelers}` = `${housekeeping_total:,.2f}`")
    st.markdown(f"- Fixed incidentals total = `${fixed_incidentals_total:,.2f}`")

    st.markdown("**Other fixed costs**")
    st.markdown(f"- Other fixed costs entered = `${other_fixed:,.2f}`")

    st.markdown("**Totals**")
    st.markdown(f"- Subtotal = `${subtotal:,.2f}`")
    st.markdown(f"- Contingency (5%) = `${subtotal:,.2f} × 0.05` = `${contingency:,.2f}`")
    st.markdown(f"- Final total = `${subtotal:,.2f} + ${contingency:,.2f}` = `${grand_total:,.2f}`")

    st.markdown("</div>", unsafe_allow_html=True)
