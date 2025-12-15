import datetime as dt
from statistics import mean
from typing import Optional, List

import streamlit as st
from amadeus import Client, ResponseError

# ---------------------------------------------------------
# Page config & styling
# ---------------------------------------------------------

st.set_page_config(
    page_title="Expense Calculator",
    layout="wide",
)

st.markdown(
    """
    <style>
    .miip-title {
        font-size: 2.0rem;
        font-weight: 700;
        margin-bottom: 0.15rem;
    }
    .miip-subtitle {
        font-size: 0.95rem;
        color: #c4c4c4;
        margin-bottom: 1.5rem;
    }
    .miip-section-title {
        font-size: 1.05rem;
        font-weight: 600;
        margin-bottom: 0.3rem;
    }
    .miip-geek-math p, .miip-geek-math li {
        margin-bottom: 0.2rem !important;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

st.markdown('<div class="miip-title">Expense Calculator</div>', unsafe_allow_html=True)
st.markdown(
    '<div class="miip-subtitle">'
    'Estimate audit trip costs for flights, hotel, meals, and Hertz rental car.'
    '</div>',
    unsafe_allow_html=True,
)

# ---------------------------------------------------------
# Static data
# ---------------------------------------------------------

AIRLINE_CODES = {
    "Delta": "DL",
    "Southwest": "WN",
    "JetBlue": "B6",
    "American": "AA",
}

DOMESTIC_BAG_FEE_BY_AIRLINE = {
    "Southwest": 0.0,
    "JetBlue": 70.0,
    "Delta": 70.0,
    "American": 70.0,
}

US_AIRPORTS = {
    "BOS","MHT","JFK","LGA","EWR","PHL","DCA","IAD","BWI","CLT","ATL",
    "MCO","TPA","MIA","FLL","ORD","MDW","DFW","DAL","IAH","HOU",
    "DEN","PHX","LAS","LAX","SFO","SJC","SEA","PDX","HNL","OGG","LIH","KOA",
}

HOTEL_BASE_RATE_BY_AIRPORT = {
    "BOS": 260, "JFK": 280, "LGA": 270, "EWR": 260,
    "LAX": 260, "SFO": 280, "SEA": 250, "DEN": 210,
    "MCO": 210, "TPA": 215, "MIA": 260, "CLT": 190,
    "PHL": 210, "ORD": 230, "ATL": 210,
}

DEFAULT_HOTEL_NIGHTLY_RATE = 190.0

HERTZ_BASE_DAILY_BY_AIRPORT = {
    "BOS": 70, "MHT": 60, "JFK": 75, "LGA": 75, "EWR": 72,
    "TPA": 65, "MCO": 65, "MIA": 70, "DEN": 68,
    "SFO": 78, "LAX": 78, "SEA": 72,
}

HERTZ_SUV_UPLIFT = 0.15
HERTZ_MEMBERSHIP_DISCOUNT = 0.12

MEAL_RATE_PER_DAY = 100.0
CONTINGENCY_RATE = 0.05  # 5%

# ---------------------------------------------------------
# Helper functions
# ---------------------------------------------------------

def get_amadeus_client():
    try:
        cfg = st.secrets["amadeus"]
        return Client(
            client_id=cfg["client_id"],
            client_secret=cfg["client_secret"],
            hostname=cfg.get("hostname", "production"),
        )
    except Exception:
        st.error("Amadeus configuration missing or invalid.")
        return None


def is_domestic(origin, dest):
    return origin in US_AIRPORTS and dest in US_AIRPORTS


def estimate_hotel_rate(dest):
    return HOTEL_BASE_RATE_BY_AIRPORT.get(dest, DEFAULT_HOTEL_NIGHTLY_RATE)


def estimate_hertz_rate(dest):
    base = HERTZ_BASE_DAILY_BY_AIRPORT.get(dest, 80.0)
    return round(base * (1 + HERTZ_SUV_UPLIFT) * (1 - HERTZ_MEMBERSHIP_DISCOUNT), 2)


def get_avg_flight_cost(client, origin, dest, dep, ret, airline):
    code = AIRLINE_CODES.get(airline)
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
    except ResponseError:
        st.error("Amadeus flight search failed. You can switch to manual entry.")
        return None

    offers = resp.data
    if not offers:
        st.error("Amadeus returned no offers for this route/dates.")
        return None

    all_prices = []
    preferred_prices = []

    for o in offers:
        try:
            price = float(o["price"]["grandTotal"])
            all_prices.append(price)
            if code and code in (o.get("validatingAirlineCodes") or []):
                preferred_prices.append(price)
        except Exception:
            pass

    if preferred_prices:
        avg = round(mean(preferred_prices), 2)
    else:
        avg = round(mean(all_prices), 2)
        st.warning(
            f"No usable prices found for the preferred airline only; "
            f"using average of available airlines instead. Average used: ${avg:,.0f}."
        )

    return avg

# ---------------------------------------------------------
# Layout
# ---------------------------------------------------------

left, right = st.columns(2)

with left:
    st.markdown('<div class="miip-section-title">Traveler & flights</div>', unsafe_allow_html=True)

    travelers = st.number_input(
        "Number of travelers",
        min_value=1,
        value=1,
        step=1,
        help="One room per traveler",
    )

    departure_airport = st.selectbox("Departure airport", ["BOS", "MHT"])
    preferred_airline = st.selectbox("Preferred airline", list(AIRLINE_CODES.keys()))

    destination_airport = st.text_input(
        "Destination airport",
        help="3-letter IATA code (e.g., TPA)",
    ).strip().upper()

with right:
    st.markdown('<div class="miip-section-title">Client & hotel options</div>', unsafe_allow_html=True)

    assignment_location = st.text_input(
        "Assignment city and state",
        help="City, State",
    )

    preferred_hotel_brand = st.selectbox(
        "Preferred hotel brand",
        ["Marriott", "Hilton", "Wyndham"],
    )

dates_col, ground_col = st.columns(2)
today = dt.date.today()

with dates_col:
    st.markdown('<div class="miip-section-title">Dates</div>', unsafe_allow_html=True)

    departure_date = st.date_input(
        "Departure date",
        value=today,
        format="MM/DD/YYYY",
    )

    return_date = st.date_input(
        "Return date",
        value=departure_date + dt.timedelta(days=1),
        min_value=departure_date + dt.timedelta(days=1),
        format="MM/DD/YYYY",
    )

with ground_col:
    st.markdown('<div class="miip-section-title">Ground costs</div>', unsafe_allow_html=True)

    include_rental = st.checkbox("Include Hertz rental SUV", value=True)

    other_fixed_costs = st.number_input(
        "Other fixed costs",
        min_value=0.0,
        value=0.0,
        step=50.0,
        help="Additional fixed expenses in USD",
    )

# ---------------------------------------------------------
# Calculations
# ---------------------------------------------------------

trip_days = (return_date - departure_date).days + 1
trip_nights = max(trip_days - 1, 0)

st.markdown('<div class="miip-section-title">Flights</div>', unsafe_allow_html=True)

flight_mode = st.radio("", ["Auto calculate", "Enter manually"])

flight_cost = 0.0

if flight_mode == "Enter manually":
    flight_cost = st.number_input(
        "Manual flight cost per traveler (round trip)",
        min_value=0.0,
        value=0.0,
        step=50.0,
    )
else:
    if len(destination_airport) == 3:
        client = get_amadeus_client()
        if client:
            cost = get_avg_flight_cost(
                client,
                departure_airport,
                destination_airport,
                departure_date,
                return_date,
                preferred_airline,
            )
            if cost:
                flight_cost = cost

bag_fee = (
    DOMESTIC_BAG_FEE_BY_AIRLINE.get(preferred_airline, 70)
    if is_domestic(departure_airport, destination_airport)
    else 0
)

hotel_rate = estimate_hotel_rate(destination_airport)
meals_total = MEAL_RATE_PER_DAY * trip_days * travelers
rental_rate = estimate_hertz_rate(destination_airport) if include_rental else 0

# Totals
flights_total = flight_cost * travelers
bags_total = bag_fee * travelers
hotel_total = hotel_rate * trip_nights * travelers
rental_total = rental_rate * trip_days

subtotal = (
    flights_total
    + bags_total
    + hotel_total
    + meals_total
    + rental_total
    + other_fixed_costs
)

contingency = subtotal * CONTINGENCY_RATE
grand_total = subtotal + contingency

# ---------------------------------------------------------
# Summary
# ---------------------------------------------------------

st.markdown('<div class="miip-section-title">Trip cost summary</div>', unsafe_allow_html=True)

st.write(f"- Route: **{departure_airport} → {destination_airport} → {departure_airport}**")
st.write(f"- Dates: **{departure_date:%m/%d/%Y} – {return_date:%m/%d/%Y}**")
st.write(f"- Travelers: **{travelers}**")

st.write("")
st.write(f"- Flights total: **${flights_total:,.0f}**")
st.write(f"- Checked bags total: **${bags_total:,.0f}**")
st.write(f"- Hotel total: **${hotel_total:,.0f}**")
st.write(f"- Meals total: **${meals_total:,.0f}**")
st.write(f"- Rental car total: **${rental_total:,.0f}**")
st.write(f"- Other fixed costs: **${other_fixed_costs:,.0f}**")

st.success(f"Grand total: ${grand_total:,.0f}")

with st.expander("Show detailed cost math", expanded=False):
    st.markdown('<div class="miip-geek-math">', unsafe_allow_html=True)
    st.markdown(f"- Meals: `$100 × {trip_days} days × {travelers} travelers = ${meals_total:,.2f}`")
    st.markdown(f"- Subtotal: **${subtotal:,.2f}**")
    st.markdown(f"- Contingency (5%): **${contingency:,.2f}**")
    st.markdown(f"- Final total: **${grand_total:,.2f}**")
    st.markdown('</div>', unsafe_allow_html=True)
