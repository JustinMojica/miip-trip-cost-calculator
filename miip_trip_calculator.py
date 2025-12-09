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
    'Estimate audit trip costs for flights, hotel, meals, and Hertz rental car.'
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

CONTINGENCY_RATE = 0.15  # 15% buffer still applied in the math

# ---------------------------------------------------------
# Functions
# ---------------------------------------------------------

def get_amadeus_client() -> Optional[Client]:
    try:
        amadeus_cfg = st.secrets["amadeus"]
        client = Client(
            client_id=amadeus_cfg["client_id"],
            client_secret=amadeus_cfg["client_secret"],
            hostname=amadeus_cfg.get("hostname", "production"),
        )
        return client
    except Exception as exc:
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

    preferred_code = AIRLINE_CODES.get(airline_name)

    try:
        response = amadeus_client.shopping.flight_offers_search.get(
            originLocationCode=origin,
            destinationLocationCode=dest,
            departureDate=departure_date.isoformat(),
            returnDate=return_date.isoformat(),
            adults=1,
            currencyCode="USD",
            max=20,
        )
        offers = response.data
    except ResponseError as error:
        st.error("Amadeus flight search failed. You can switch to manual flight entry.")
        st.caption(str(error))
        return None
    except Exception as error:
        st.error("Unexpected error while calling Amadeus. You can switch to manual flight entry.")
        st.caption(str(error))
        return None

    if not offers:
        st.error("Amadeus returned no offers for this route/dates. You can enter flights manually.")
        return None

    prices_all: List[float] = []
    prices_preferred: List[float] = []

    for offer in offers:
        try:
            price = float(offer["price"]["grandTotal"])
        except Exception:
            continue

        prices_all.append(price)

        if preferred_code:
            validating_codes = offer.get("validatingAirlineCodes") or offer.get("validatingAirlineCode")
            if isinstance(validating_codes, list):
                if preferred_code in validating_codes:
                    prices_preferred.append(price)
            elif isinstance(validating_codes, str):
                if validating_codes == preferred_code:
                    prices_preferred.append(price)

    if not prices_all:
        st.error("Amadeus returned no usable prices for this route/dates. You can enter flights manually.")
        return None

    if prices_preferred:
        avg_price = round(mean(prices_preferred), 2)
        st.caption(
            f"Estimated average round-trip fare per traveler for **{airline_name}**: "
            f"**${avg_price:,.0f}**."
        )
    else:
        avg_price = round(mean(prices_all), 2)
        st.warning(
            "No usable prices found for the preferred airline only; using average of available airlines instead. "
            f"Average used: **${avg_price:,.0f}**."
        )

    return avg_price

# ---------------------------------------------------------
# Inputs – layout
# ---------------------------------------------------------

col_left, col_right = st.columns(2)

with col_left:
    st.markdown('<div class="miip-section-card">', unsafe_allow_html=True)
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

    destination_airport_raw = st.text_input(
        "Destination airport",
        placeholder="TPA",
        help="3-letter IATA code, e.g., TPA",
    )
    destination_airport = destination_airport_raw.strip().upper()

    st.markdown("</div>", unsafe_allow_html=True)

with col_right:
    st.markdown('<div class="miip-section-card">', unsafe_allow_html=True)
    st.markdown('<div class="miip-section-title">Client & hotel options</div>', unsafe_allow_html=True)

    client_address = st.text_input("Client office address", "", help="City, State")
    preferred_hotel_brand = st.selectbox("Preferred hotel brand", ["Marriott", "Hilton", "Wyndham"])
    st.markdown("</div>", unsafe_allow_html=True)

col_dates, col_ground = st.columns(2)

today = dt.date.today()

with col_dates:
    st.markdown('<div class="miip-section-card">', unsafe_allow_html=True)
    st.markdown('<div class="miip-section-title">Dates</div>', unsafe_allow_html=True)

    departure_date = st.date_input(
        "Departure date",
        value=today,
        format="MM/DD/YYYY",
        key="departure_date",
    )

    min_ret = departure_date + dt.timedelta(days=1)
    return_date = st.date_input(
        "Return date",
        value=min_ret,
        min_value=min_ret,
        format="MM/DD/YYYY",
        key="return_date",
    )

    st.markdown("</div>", unsafe_allow_html=True)

with col_ground:
    st.markdown('<div class="miip-section-card">', unsafe_allow_html=True)
    st.markdown('<div class="miip-section-title">Ground costs</div>', unsafe_allow_html=True)

    include_rental_car = st.checkbox("Include Hertz rental SUV", value=True)
    other_fixed_costs = st.number_input(
        "Other fixed costs",
        min_value=0.0,
        value=0.0,
        step=50.0,
        help="Additional fixed expenses in USD (parking, tolls, etc.)",
    )
    st.markdown("</div>", unsafe_allow_html=True)

# ---------------------------------------------------------
# Computation
# ---------------------------------------------------------

if return_date <= departure_date:
    st.error("Return date must be after the departure date.")
    can_calculate = False
    trip_days = 1
    trip_nights = 0
else:
    can_calculate = True
    delta_days = (return_date - departure_date).days
    trip_days = delta_days + 1
    trip_nights = delta_days

valid_destination_for_flights = bool(destination_airport) and len(destination_airport) == 3

if destination_airport_raw.strip() and not valid_destination_for_flights:
    st.warning(
        "Destination airport must be a 3-letter IATA code (e.g., TPA, BWI, DEN). "
        "Flights will not be estimated until corrected."
    )

st.markdown('<div class="miip-section-card">', unsafe_allow_html=True)
st.markdown('<div class="miip-section-title">Flights</div>', unsafe_allow_html=True)

flight_pricing_mode = st.radio(
    "",
    ("Auto calculate", "Enter manually"),
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
        st.warning("Manual flight cost is set to $0.")
else:
    if can_calculate and valid_destination_for_flights:
        amadeus_client = get_amadeus_client()
        if amadeus_client:
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

# Bag fee
if valid_destination_for_flights and is_domestic_route(departure_airport, destination_airport):
    bag_fee_per_traveler = DOMESTIC_BAG_FEE_BY_AIRLINE.get(preferred_airline, 70.0)
else:
    bag_fee_per_traveler = 0.0

bags_total = bag_fee_per_traveler * travelers

st.markdown("</div>", unsafe_allow_html=True)

# Hidden / smart estimates
hotel_nightly_rate = estimate_hotel_nightly_rate(destination_airport) if destination_airport else DEFAULT_HOTEL_NIGHTLY_RATE
meal_rate_per_day = estimate_meal_rate_per_day(destination_airport) if destination_airport else BASE_MEAL_RATE
hertz_daily_rate = estimate_hertz_suv_daily_rate(destination_airport or "BOS") if include_rental_car else 0.0

# ---------------------------------------------------------
# Totals & summary
# ---------------------------------------------------------

if can_calculate:
    flights_total = flight_cost_per_person * travelers
    hotel_total = hotel_nightly_rate * trip_nights * travelers
    meals_total = meal_rate_per_day * trip_days * travelers
    rental_total = hertz_daily_rate * trip_days if include_rental_car else 0.0

    base_total = flights_total + bags_total + hotel_total + meals_total + rental_total + other_fixed_costs
    contingency = base_total * CONTINGENCY_RATE
    final_total = base_total + contingency

    st.markdown('<div class="miip-section-card">', unsafe_allow_html=True)
    st.markdown('<div class="miip-section-title">Trip cost summary</div>', unsafe_allow_html=True)

    st.write(f"- Route: **{departure_airport} → {destination_airport or '???'} → {departure_airport}**")
    st.write(f"- Dates: **{departure_date.strftime('%m/%d/%Y')} – {return_date.strftime('%m/%d/%Y')}**")
    st.write(f"- Trip days/nights: **{trip_days} / {trip_nights}**")
    st.write(f"- Travelers: **{travelers}**")

    st.write("")
    st.write("**Cost components**")
    st.write(f"- Flights total: **${flights_total:,.0f}**")
    st.write(f"- Checked bags total: **${bags_total:,.0f}**")
    st.write(f"- Hotel total: **${hotel_total:,.0f}**")
    st.write(f"- Meals total: **${meals_total:,.0f}**")
    st.write(f"- Hertz rental car total: **${rental_total:,.0f}**")
    st.write(f"- Other fixed costs: **${other_fixed_costs:,.0f}**")

    st.markdown("</div>", unsafe_allow_html=True)

    # Final green box (no contingency wording)
    st.success(f"Grand total: ${final_total:,.0f}")

    # Geek math expander
    with st.expander("Show detailed cost math", expanded=False):
        st.markdown('<div class="miip-geek-math">', unsafe_allow_html=True)

        st.markdown("#### Trip length")
        st.markdown(f"- `trip_days` = {trip_days}")
        st.markdown(f"- `trip_nights` = {trip_nights}")

        st.markdown("#### Flights")
        st.markdown(f"- Average round-trip fare per traveler: **${flight_cost_per_person:,.2f}**")
        st.markdown(
            f"- Flights total: `${flight_cost_per_person:,.2f} × {travelers}`"
            f" = **${flights_total:,.2f}**"
        )

        st.markdown("#### Checked bags")
        st.markdown(f"- Bag fee per traveler: **${bag_fee_per_traveler:,.2f}**")
        st.markdown(
            f"- Bags total: `${bag_fee_per_traveler:,.2f} × {travelers}`"
            f" = **${bags_total:,.2f}**"
        )

        st.markdown("#### Hotel")
        st.markdown(f"- Nightly rate: **${hotel_nightly_rate:,.2f}**")
        st.markdown(
            f"- Hotel total: `${hotel_nightly_rate:,.2f} × {trip_nights} × {travelers}`"
            f" = **${hotel_total:,.2f}**"
        )

        st.markdown("#### Meals")
        st.markdown(f"- Daily meal rate: **${meal_rate_per_day:,.2f}**")
        st.markdown(
            f"- Meals total: `${meal_rate_per_day:,.2f} × {trip_days} × {travelers}`"
            f" = **${meals_total:,.2f}**"
        )

        st.markdown("#### Hertz rental car")
        st.markdown(f"- Daily SUV rate: **${hertz_daily_rate:,.2f}**")
        st.markdown(
            f"- Rental total: `${hertz_daily_rate:,.2f} × {trip_days}`"
            f" = **${rental_total:,.2f}**"
        )

        st.markdown("#### Other fixed costs")
        st.markdown(f"- Other fixed costs entered: **${other_fixed_costs:,.2f}**")

        st.markdown("#### Contingency")
        st.markdown(
            f"- Subtotal before contingency: **${base_total:,.2f}**"
        )
        st.markdown(
            f"- 15% contingency buffer: `${base_total:,.2f} × 0.15` = **${contingency:,.2f}**"
        )
        st.markdown(
            f"- Final total: `${base_total:,.2f} + ${contingency:,.2f}`"
            f" = **${final_total:,.2f}**"
        )

        st.markdown("</div>", unsafe_allow_html=True)

else:
    st.info("Select valid departure and return dates to see the full trip cost breakdown.")
