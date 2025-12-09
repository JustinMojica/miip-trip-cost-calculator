import datetime as dt
from statistics import mean
from typing import Optional, List

import streamlit as st
from amadeus import Client, ResponseError

# ---------------------------------------------------------
# Config & styling
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
    /* Structural section wrapper without visible box */
    .miip-section-card {
        padding: 0 0 0.75rem 0;
        border: none;
        background-color: transparent;
        margin-bottom: 0.4rem;
    }
    .miip-section-title {
        font-size: 1.08rem;
        font-weight: 600;
        margin-bottom: 0.2rem;
    }
    .miip-section-caption {
        font-size: 0.85rem;
        color: #a0a0a0;
        margin-bottom: 0.15rem;
    }
    .miip-microcopy {
        font-size: 0.8rem;
        color: #8a8a8a;
    }
    /* Tighter spacing in geek math expander */
    .miip-geek-math p {
        margin-bottom: 0.2rem !important;
    }
    .miip-geek-math li {
        margin-bottom: 0.2rem !important;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

st.markdown('<div class="miip-title">Expense Calculator</div>', unsafe_allow_html=True)
st.markdown(
    '<div class="miip-subtitle">'
    'Estimate audit trip costs quickly with smart defaults for flights, hotel, meals, and Hertz rental car.'
    '</div>',
    unsafe_allow_html=True,
)

# ---------------------------------------------------------
# Static data & helpers
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
    "BOS", "MHT", "JFK", "LGA", "EWR", "PHL", "DCA", "IAD", "BWI",
    "CLT", "ATL", "MCO", "TPA", "MIA", "FLL",
    "ORD", "MDW", "DFW", "DAL", "IAH", "HOU",
    "DEN", "PHX", "LAS", "LAX", "SFO", "SJC", "SEA", "PDX",
    "HNL", "OGG", "LIH", "KOA",
}

# Hotel nightly rates
HOTEL_BASE_RATE_BY_AIRPORT = {
    "BOS": 260.0,
    "JFK": 280.0,
    "LGA": 270.0,
    "EWR": 260.0,
    "LAX": 260.0,
    "SFO": 280.0,
    "SEA": 250.0,
    "DEN": 210.0,
    "MCO": 210.0,
    "TPA": 215.0,
    "MIA": 260.0,
    "CLT": 190.0,
    "PHL": 210.0,
    "ORD": 230.0,
    "ATL": 210.0,
    "HNL": 320.0,
    "OGG": 330.0,
}
DEFAULT_HOTEL_NIGHTLY_RATE = 190.0

# Meals
BASE_MEAL_RATE = 100.0
EXPENSIVE_AIRPORTS = {
    "JFK", "LGA", "EWR", "NYC", "BOS", "SFO", "LAX", "SEA", "DCA", "IAD",
    "MIA", "HNL", "OGG", "LIH", "KOA",
}
MID_TIER_AIRPORTS = {
    "AUS", "DEN", "CLT", "PHL", "TPA", "MCO", "ATL", "PHX", "LAS", "SAN",
}

# Hertz rental car
HERTZ_BASE_DAILY_BY_AIRPORT = {
    "BOS": 70.0,
    "MHT": 60.0,
    "JFK": 75.0,
    "LGA": 75.0,
    "EWR": 72.0,
    "TPA": 65.0,
    "MCO": 65.0,
    "MIA": 70.0,
    "DEN": 68.0,
    "SFO": 78.0,
    "LAX": 78.0,
    "SEA": 72.0,
}
HERTZ_SUV_UPLIFT = 0.15
HERTZ_MEMBERSHIP_DISCOUNT = 0.12

CONTINGENCY_RATE = 0.15  # 15% buffer


# ---------------------------------------------------------
# Functions
# ---------------------------------------------------------


def get_amadeus_client() -> Optional[Client]:
    """Create the Amadeus client from Streamlit secrets."""
    try:
        amadeus_cfg = st.secrets["amadeus"]
        client = Client(
            client_id=amadeus_cfg["client_id"],
            client_secret=amadeus_cfg["client_secret"],
            hostname=amadeus_cfg.get("hostname", "production"),
        )
        return client
    except Exception as exc:  # secrets missing / invalid
        st.error("Amadeus configuration is missing or invalid in Streamlit secrets.")
        st.caption(str(exc))
        return None


def is_domestic_route(origin: str, dest: str) -> bool:
    return origin.upper() in US_AIRPORTS and dest.upper() in US_AIRPORTS


def estimate_hotel_nightly_rate(dest_airport: str) -> float:
    return HOTEL_BASE_RATE_BY_AIRPORT.get(dest_airport.upper(), DEFAULT_HOTEL_NIGHTLY_RATE)


def estimate_meal_rate_per_day(dest_airport: str) -> float:
    airport = dest_airport.upper()
    rate = BASE_MEAL_RATE
    if airport in EXPENSIVE_AIRPORTS:
        rate *= 1.25
    elif airport in MID_TIER_AIRPORTS:
        rate *= 1.10
    return round(rate, 2)


def estimate_hertz_suv_daily_rate(dest_airport: str) -> float:
    base = HERTZ_BASE_DAILY_BY_AIRPORT.get(dest_airport.upper(), 80.0)
    suv_price = base * (1 + HERTZ_SUV_UPLIFT)
    membership_adjusted = suv_price * (1 - HERTZ_MEMBERSHIP_DISCOUNT)
    return round(membership_adjusted, 2)


def get_average_round_trip_fare(
    amadeus_client: Client,
    origin: str,
    dest: str,
    departure_date: dt.date,
    return_date: dt.date,
    airline_name: str,
) -> Optional[float]:
    """Return an average RT fare per traveler in USD.

    Strategy:
    1. Try with the preferred airline (includedAirlineCodes).
    2. If no usable prices, retry without airline filter (any airline).
    """

    airline_code = AIRLINE_CODES.get(airline_name)

    def _fetch_offers(params: dict) -> List[float]:
        try:
            response = amadeus_client.shopping.flight_offers_search.get(**params)
            offers = response.data
        except ResponseError as error:
            st.error("Amadeus flight search failed. You can switch to manual flight entry.")
            st.caption(str(error))
            return []
        except Exception as error:
            st.error("Unexpected error while calling Amadeus. You can switch to manual flight entry.")
            st.caption(str(error))
            return []

        prices: List[float] = []
        for offer in offers:
            try:
                price = float(offer["price"]["grandTotal"])
                prices.append(price)
            except Exception:
                continue
        return prices

    base_params = dict(
        originLocationCode=origin,
        destinationLocationCode=dest,
        departureDate=departure_date.isoformat(),
        returnDate=return_date.isoformat(),
        adults=1,
        currencyCode="USD",
        max=20,
    )

    prices: List[float] = []

    # 1) Try with preferred airline, if we know its code
    if airline_code:
        params_with_airline = {**base_params, "includedAirlineCodes": airline_code}
        prices = _fetch_offers(params_with_airline)

    # 2) If no usable prices, try again with ANY airline
    if not prices:
        prices = _fetch_offers(base_params)
        if prices:
            st.caption(
                "No usable prices found for the preferred airline only; "
                "using average of available airlines instead."
            )

    if not prices:
        st.error("Amadeus returned no usable prices for this route/dates. You can enter flights manually.")
        return None

    return round(mean(prices), 2)


# ---------------------------------------------------------
# Inputs – layout
# ---------------------------------------------------------

today = dt.date.today()
default_departure_date = today
default_return_date = today + dt.timedelta(days=1)

col_left, col_right = st.columns(2)

with col_left:
    st.markdown('<div class="miip-section-card">', unsafe_allow_html=True)
    st.markdown('<div class="miip-section-title">1. Traveler & flights</div>', unsafe_allow_html=True)
    st.markdown(
        '<div class="miip-section-caption">'
        'Who is traveling and which route/airline you prefer.'
        '</div>',
        unsafe_allow_html=True,
    )

    travelers = st.number_input("Number of travelers (one room per traveler)", min_value=1, value=1, step=1)
    departure_airport = st.selectbox("Departure airport (home base)", ["BOS", "MHT"])
    preferred_airline = st.selectbox("Preferred airline", list(AIRLINE_CODES.keys()))
    destination_airport = st.text_input("Destination airport (IATA, e.g., TPA)").upper()
    st.markdown("</div>", unsafe_allow_html=True)

with col_right:
    st.markdown('<div class="miip-section-card">', unsafe_allow_html=True)
    st.markdown('<div class="miip-section-title">2. Client & hotel options</div>', unsafe_allow_html=True)
    st.markdown(
        '<div class="miip-section-caption">'
        'Client address is informational; hotel brand steers the nightly estimate.'
        '</div>',
        unsafe_allow_html=True,
    )

    client_address = st.text_input("Client office address", "", help="City, State")
    preferred_hotel_brand = st.selectbox("Preferred hotel brand", ["Marriott", "Hilton", "Wyndham"])
    st.markdown("</div>", unsafe_allow_html=True)

col_dates, col_ground = st.columns(2)

with col_dates:
    st.markdown('<div class="miip-section-card">', unsafe_allow_html=True)
    st.markdown('<div class="miip-section-title">3. Dates</div>', unsafe_allow_html=True)

    departure_date = st.date_input("Departure date", value=default_departure_date)
    return_date = st.date_input(
        "Return date",
        value=max(default_return_date, departure_date + dt.timedelta(days=1)),
        min_value=departure_date + dt.timedelta(days=1),
    )
    st.markdown(
        '<div class="miip-microcopy">Trip days and nights are computed automatically from your dates.</div>',
        unsafe_allow_html=True,
    )
    st.markdown("</div>", unsafe_allow_html=True)

with col_ground:
    st.markdown('<div class="miip-section-card">', unsafe_allow_html=True)
    st.markdown('<div class="miip-section-title">4. Ground costs</div>', unsafe_allow_html=True)

    include_rental_car = st.checkbox("Include Hertz rental SUV", value=True)
    other_fixed_costs = st.number_input("Other fixed costs (USD)", min_value=0.0, value=0.0, step=50.0)
    st.markdown("</div>", unsafe_allow_html=True)

# ---------------------------------------------------------
# Derived trip length & validation
# ---------------------------------------------------------

if return_date <= departure_date:
    st.error("Return date must be after the departure date.")
    can_calculate = False
    trip_days = 1
    trip_nights = 0
else:
    can_calculate = True
    delta_days = (return_date - departure_date).days
    trip_days = max(delta_days + 1, 1)
    trip_nights = max(delta_days, 0)

# ---------------------------------------------------------
# 5. Flights (preferred airline + smart checked bags)
# ---------------------------------------------------------

st.markdown('<div class="miip-section-card">', unsafe_allow_html=True)
st.markdown('<div class="miip-section-title">5. Flights</div>', unsafe_allow_html=True)

flight_pricing_mode = st.radio(
    "Flight pricing mode",
    ("Use Amadeus average (preferred airline)", "Enter manually"),
    index=0,
)

flight_cost_per_person = 0.0
amadeus_client: Optional[Client] = None

if flight_pricing_mode == "Enter manually":
    manual_flight_cost = st.number_input(
        "Manual flight cost per person (round trip, USD)",
        min_value=0.0,
        value=0.0,
        step=50.0,
    )
    flight_cost_per_person = manual_flight_cost

    if manual_flight_cost == 0:
        st.warning("Manual flight cost is set to $0. Update this if you want flights included.")
else:
    if can_calculate and destination_airport:
        amadeus_client = get_amadeus_client()
        if amadeus_client is not None:
            avg_fare = get_average_round_trip_fare(
                amadeus_client,
                origin=departure_airport,
                dest=destination_airport,
                departure_date=departure_date,
                return_date=return_date,
                airline_name=preferred_airline,
            )
            if avg_fare is not None:
                flight_cost_per_person = avg_fare
                st.caption(f"Estimated average round-trip fare per traveler: **${avg_fare:,.0f}**.")
    else:
        st.caption("Enter a valid destination airport and dates to estimate flights via Amadeus.")
