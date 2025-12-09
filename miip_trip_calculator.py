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
    - Query Amadeus once for all airlines.
    - If there are offers whose validating airline matches the preferred airline,
      average only those.
    - Otherwise, average across all returned offers.
    """

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
            # Try to infer validating airline code(s)
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
        st.caption(
            "Estimated average round-trip fare per traveler based on all available airlines "
            f"(no specific offers found for {airline_name}). "
            f"Average used: **${avg_price:,.0f}**."
        )

    return avg_price


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
    ("Use Amadeus average (preferred airline where available)", "Enter manually"),
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
    else:
        st.caption("Enter a valid destination airport and dates to estimate flights via Amadeus.")

# Bag fees – compute per traveler so we can show in geek math
if is_domestic_route(departure_airport, destination_airport):
    bag_fee_per_traveler = DOMESTIC_BAG_FEE_BY_AIRLINE.get(preferred_airline, 70.0)
    bag_route_note = "Domestic route; using airline-specific domestic bag fee."
else:
    bag_fee_per_traveler = 0.0
    bag_route_note = "Non-domestic or unknown route; bag fees assumed $0."

bags_total = bag_fee_per_traveler * travelers
st.caption(bag_route_note)
st.markdown("</div>", unsafe_allow_html=True)

# ---------------------------------------------------------
# Hidden calculation for hotel, meals, Hertz (no visible sections)
# ---------------------------------------------------------

hotel_nightly_rate = estimate_hotel_nightly_rate(destination_airport) if destination_airport else DEFAULT_HOTEL_NIGHTLY_RATE
meal_rate_per_day = estimate_meal_rate_per_day(destination_airport) if destination_airport else BASE_MEAL_RATE

hertz_daily_rate = 0.0
if include_rental_car:
    if destination_airport:
        hertz_daily_rate = estimate_hertz_suv_daily_rate(destination_airport)
    else:
        hertz_daily_rate = estimate_hertz_suv_daily_rate("BOS")

# ---------------------------------------------------------
# 6. Trip cost summary & totals
# ---------------------------------------------------------

if can_calculate:
    flights_total = flight_cost_per_person * travelers
    hotel_total = hotel_nightly_rate * trip_nights * travelers
    meals_total = meal_rate_per_day * trip_days * travelers
    rental_total = hertz_daily_rate * trip_days if include_rental_car else 0.0

    base_grand_total = (
        flights_total
        + bags_total
        + hotel_total
        + meals_total
        + rental_total
        + other_fixed_costs
    )

    contingency_amount = base_grand_total * CONTINGENCY_RATE
    final_total = base_grand_total + contingency_amount

    st.markdown('<div class="miip-section-card">', unsafe_allow_html=True)
    st.markdown('<div class="miip-section-title">6. Trip cost summary</div>', unsafe_allow_html=True)

    st.write("**Trip overview**")
    st.write(
        f"- Route: **{departure_airport} → {destination_airport or '???'} → {departure_airport}**"
    )
    st.write(f"- Dates: **{departure_date.isoformat()} – {return_date.isoformat()}**")
    st.write(f"- Trip days / nights: **{trip_days} days / {trip_nights} nights**")
    st.write(f"- Travelers: **{travelers}**")

    st.write("")
    st.write("**Cost components**")
    st.write(f"- Flights total (tickets): **${flights_total:,.0f}**")
    st.write(f"- Checked bags total: **${bags_total:,.0f}**")
    st.write(f"- Hotel total: **${hotel_total:,.0f}**")
    st.write(f"- Meals total: **${meals_total:,.0f}**")
    if include_rental_car:
        st.write(f"- Hertz rental car total: **${rental_total:,.0f}**")
    else:
        st.write("- Hertz rental car total: **$0** (excluded)")
    st.write(f"- Other fixed costs: **${other_fixed_costs:,.0f}**")

    st.write("")
    st.write("**Contingency**")
    st.write(f"- Subtotal before contingency: **${base_grand_total:,.0f}**")
    st.write(f"- 15% contingency buffer: **${contingency_amount:,.0f}**")

    st.markdown("</div>", unsafe_allow_html=True)

    # Single green box with final total (incl. 15% buffer)
    st.success(f"Grand total (incl. 15% contingency): ${final_total:,.0f}")

    # -----------------------------------------------------
    # Geek math & logic (collapsible, input-specific)
    # -----------------------------------------------------
    with st.expander("Show detailed cost math", expanded=False):
        st.markdown('<div class="miip-geek-math">', unsafe_allow_html=True)

        st.markdown("#### Trip length")
        st.markdown(
            f"- `trip_days` = max(({(return_date - departure_date).days} + 1), 1) = **{trip_days}**  "
            "_(counts both departure and return days)_"
        )
        st.markdown(
            f"- `trip_nights` = max({(return_date - departure_date).days}, 0) = **{trip_nights}**  "
            "_(hotel billed per night)_"
        )

        st.markdown("#### Flights")
        st.markdown(
            f"- Average round-trip fare per traveler: **${flight_cost_per_person:,.2f}**"
        )
        st.markdown(
            f"- Flights total: `${flight_cost_per_person:,.2f} × {travelers}`"
            f" = **${flights_total:,.2f}**"
        )

        st.markdown("#### Checked bags")
        st.markdown(
            f"- Bag fee per traveler: **${bag_fee_per_traveler:,.2f}** "
            f"({'domestic route' if bag_fee_per_traveler > 0 else 'non-domestic/assumed $0'})"
        )
        st.markdown(
            f"- Bags total: `${bag_fee_per_traveler:,.2f} × {travelers}`"
            f" = **${bags_total:,.2f}**  _(1 checked bag per traveler for the round trip)_"
        )

        st.markdown("#### Hotel")
        st.markdown(
            f"- Nightly rate: **${hotel_nightly_rate:,.2f}** per room near {destination_airport or 'destination'}"
        )
        st.markdown(
            f"- Hotel total: `${hotel_nightly_rate:,.2f} × {trip_nights} nights × {travelers} rooms`"
            f" = **${hotel_total:,.2f}**"
        )

        st.markdown("#### Meals")
        st.markdown(
            f"- Daily meal rate: **${meal_rate_per_day:,.2f}** per traveler"
        )
        st.markdown(
            f"- Meals total: `${meal_rate_per_day:,.2f} × {trip_days} days × {travelers} travelers`"
            f" = **${meals_total:,.2f}**"
        )

        st.markdown("#### Hertz rental car")
        if include_rental_car:
            st.markdown(
                f"- Daily SUV rate: **${hertz_daily_rate:,.2f}** "
                "_(base Hertz rate → +15% SUV → −12% membership discount)_"
            )
            st.markdown(
                f"- Rental total: `${hertz_daily_rate:,.2f} × {trip_days} days`"
                f" = **${rental_total:,.2f}**"
            )
        else:
            st.markdown("- Rental car excluded → **$0**")

        st.markdown("#### Other fixed costs")
        st.markdown(
            f"- Other fixed costs entered: **${other_fixed_costs:,.2f}**"
        )

        st.markdown("#### Roll-up & contingency")
        st.markdown(
            f"- Subtotal before contingency: **${base_grand_total:,.2f}**"
        )
        st.markdown(
            f"- Contingency (15%): `${base_grand_total:,.2f} × 0.15`"
            f" = **${contingency_amount:,.2f}**  _(buffer for missed fees / surprises)_"
        )
        st.markdown(
            f"- Final total: `${base_grand_total:,.2f} + ${contingency_amount:,.2f}`"
            f" = **${final_total:,.2f}**"
        )

        st.markdown('</div>', unsafe_allow_html=True)
else:
    st.info("Fix the date error above to see the full trip cost breakdown and totals.")
