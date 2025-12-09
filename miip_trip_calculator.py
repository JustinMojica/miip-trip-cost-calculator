import datetime as dt
from statistics import mean

import streamlit as st
from amadeus import Client, ResponseError

# ---------------------------------------------------------
# Config & styling
# ---------------------------------------------------------

st.set_page_config(
    page_title="MIIP Trip Cost Calculator",
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
        padding: 1.1rem 1.4rem;
        border-radius: 0.6rem;
        border: 1px solid #333333;
        background-color: rgba(12, 12, 12, 0.9);
        margin-bottom: 0.9rem;
    }
    .miip-section-title {
        font-size: 1.08rem;
        font-weight: 600;
        margin-bottom: 0.4rem;
    }
    .miip-section-caption {
        font-size: 0.85rem;
        color: #a0a0a0;
        margin-bottom: 0.2rem;
    }
    .miip-microcopy {
        font-size: 0.8rem;
        color: #8a8a8a;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

st.markdown('<div class="miip-title">MIIP Trip Cost Calculator</div>', unsafe_allow_html=True)
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
    # This doesn’t need to be exhaustive, just cover common MIIP routes.
    "BOS", "MHT", "JFK", "LGA", "EWR", "PHL", "DCA", "IAD", "BWI",
    "CLT", "ATL", "MCO", "TPA", "MIA", "FLL",
    "ORD", "MDW", "DFW", "DAL", "IAH", "HOU",
    "DEN", "PHX", "LAS", "LAX", "SFO", "SJC", "SEA", "PDX",
    "HNL", "OGG", "LIH", "KOA",
}

# Hotel nightly rates (business travel style)
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

# Hertz rental car – base daily rates (before SUV uplift & discount)
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

# ---------------------------------------------------------
# Functions
# ---------------------------------------------------------


def get_amadeus_client() -> Client | None:
    """Create the Amadeus client from Streamlit secrets."""
    try:
        amadeus_cfg = st.secrets["amadeus"]
        client = Client(
            client_id=amadeus_cfg["client_id"],
            client_secret=amadeus_cfg["client_secret"],
            hostname=amadeus_cfg.get("hostname", "production"),
        )
        return client
    except Exception as exc:  # secrets not configured, etc.
        st.error("Amadeus configuration is missing or invalid in Streamlit secrets.")
        st.caption(str(exc))
        return None


def is_domestic_route(origin: str, dest: str) -> bool:
    return origin.upper() in US_AIRPORTS and dest.upper() in US_AIRPORTS


def estimate_bag_fee_total(preferred_airline: str, origin: str, dest: str, travelers: int) -> float:
    if is_domestic_route(origin, dest):
        per_traveler = DOMESTIC_BAG_FEE_BY_AIRLINE.get(preferred_airline, 70.0)
    else:
        per_traveler = 0.0
    return per_traveler * travelers


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
) -> float | None:
    """Call Amadeus Flight Offers Search and return an average RT fare per traveler in USD."""
    airline_code = AIRLINE_CODES.get(airline_name)
    if not airline_code:
        st.error(f"Unknown airline: {airline_name}")
        return None

    try:
        response = amadeus_client.shopping.flight_offers_search.get(
            originLocationCode=origin,
            destinationLocationCode=dest,
            departureDate=departure_date.isoformat(),
            returnDate=return_date.isoformat(),
            adults=1,
            currencyCode="USD",
            includedAirlineCodes=airline_code,
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

    prices = []
    for offer in offers:
        try:
            price = float(offer["price"]["grandTotal"])
            prices.append(price)
        except Exception:
            continue

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

# --- Traveler & flights + Client & hotel options ---
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

    client_address = st.text_input("Client office address", "")
    preferred_hotel_brand = st.selectbox("Preferred hotel brand", ["Marriott", "Hilton", "Wyndham"])
    st.markdown("</div>", unsafe_allow_html=True)

# --- Dates & ground costs ---
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
        '<div class="miip-microcopy">'
        'Trip days and nights are computed automatically from your dates.'
        '</div>',
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
st.markdown('<div class="miip-section-title">5. Flights (preferred airline + smart checked bags)</div>', unsafe_allow_html=True)
st.markdown(
    '<div class="miip-section-caption">'
    'Choose Amadeus-based pricing or enter a manual round-trip fare per traveler.'
    '</div>',
    unsafe_allow_html=True,
)

flight_pricing_mode = st.radio(
    "Flight pricing mode",
    ("Use Amadeus average (preferred airline)", "Enter manually"),
    index=0,
)

flight_cost_per_person = 0.0
flight_source_note = ""
amadeus_client = None

if flight_pricing_mode == "Enter manually":
    manual_flight_cost = st.number_input(
        "Manual flight cost per person (round trip, USD)",
        min_value=0.0,
        value=0.0,
        step=50.0,
    )
    flight_cost_per_person = manual_flight_cost
    flight_source_note = "Using manual flight cost entered above."

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
                st.caption(f"Estimated average round-trip fare per traveler from Amadeus: **${avg_fare:,.0f}**.")
                flight_source_note = "Using Amadeus Flight Offers Search average for preferred airline."
    else:
        st.caption("Enter a valid destination airport and dates to estimate flights via Amadeus.")

bags_total = estimate_bag_fee_total(
    preferred_airline,
    departure_airport,
    destination_airport,
    travelers,
)

if is_domestic_route(departure_airport, destination_airport):
    bag_note = (
        f"Domestic route detected. Checked bags estimated at airline-specific domestic rates "
        f"({preferred_airline} & route dependent)."
    )
else:
    bag_note = "Non-domestic route or unknown airports – checked bag fees treated as $0."
st.caption(bag_note)
st.markdown("</div>", unsafe_allow_html=True)

# ---------------------------------------------------------
# 6. Hotel (preferred brand – smart estimate)
# ---------------------------------------------------------

st.markdown('<div class="miip-section-card">', unsafe_allow_html=True)
st.markdown('<div class="miip-section-title">6. Hotel (preferred brand – smart estimate)</div>', unsafe_allow_html=True)

hotel_nightly_rate = estimate_hotel_nightly_rate(destination_airport) if destination_airport else DEFAULT_HOTEL_NIGHTLY_RATE
st.write(
    f"Estimated nightly rate for **{preferred_hotel_brand}** near **{destination_airport or 'destination'}** "
    f"is approximately **${hotel_nightly_rate:,.0f}**."
)
st.caption(
    "One room per traveler. Nightly rates come from a centrally managed airport mapping; "
    "if an airport is unknown, a default business nightly rate is used."
)
st.markdown("</div>", unsafe_allow_html=True)

# ---------------------------------------------------------
# 7. Meals (smart estimate)
# ---------------------------------------------------------

st.markdown('<div class="miip-section-card">', unsafe_allow_html=True)
st.markdown('<div class="miip-section-title">7. Meals (smart estimate)</div>', unsafe_allow_html=True)

meal_rate_per_day = estimate_meal_rate_per_day(destination_airport) if destination_airport else BASE_MEAL_RATE
st.write(
    f"Estimated meal rate per day is **${meal_rate_per_day:,.0f}** per traveler "
    f"based on the cost tier for **{destination_airport or 'destination'}**."
)
st.caption(
    "Base is $100/day. High-cost cities apply a +25% uplift, mid-tier cities apply +10%."
)
st.markdown("</div>", unsafe_allow_html=True)

# ---------------------------------------------------------
# 8. Hertz rental car (smart estimate)
# ---------------------------------------------------------

st.markdown('<div class="miip-section-card">', unsafe_allow_html=True)
st.markdown('<div class="miip-section-title">8. Hertz rental car (smart estimate)</div>', unsafe_allow_html=True)

hertz_daily_rate = 0.0
if include_rental_car:
    hertz_daily_rate = estimate_hertz_suv_daily_rate(destination_airport) if destination_airport else estimate_hertz_suv_daily_rate("BOS")
    st.write(
        f"Estimated **Hertz SUV** daily rate near **{destination_airport or 'destination'}**: "
        f"**${hertz_daily_rate:,.0f}** per day."
    )
    st.caption(
        "Based on a standard Hertz base daily rate per airport, adjusted +15% for SUV and "
        "then reduced by an internal membership discount. Auditors only see the final rate."
    )
else:
    st.caption("Rental car is excluded from this estimate.")

st.markdown("</div>", unsafe_allow_html=True)

# ---------------------------------------------------------
# 9. Trip cost summary & grand total
# ---------------------------------------------------------

if can_calculate:
    flights_total = flight_cost_per_person * travelers
    bags_total = bags_total  # already computed
    hotel_total = hotel_nightly_rate * trip_nights * travelers
    meals_total = meal_rate_per_day * trip_days * travelers
    rental_total = hertz_daily_rate * trip_days if include_rental_car else 0.0

    grand_total = (
        flights_total
        + bags_total
        + hotel_total
        + meals_total
        + rental_total
        + other_fixed_costs
    )

    st.markdown('<div class="miip-section-card">', unsafe_allow_html=True)
    st.markdown('<div class="miip-section-title">9. Trip cost summary</div>', unsafe_allow_html=True)

    st.write("**Breakdown**")
    st.write(
        f"- Route: **{departure_airport} → {destination_airport or '???'} → {departure_airport}**"
    )
    st.write(f"- Dates: **{departure_date.isoformat()} – {return_date.isoformat()}**")
    st.write(f"- Trip days / nights: **{trip_days} days / {trip_nights} nights**")
    st.write(f"- Travelers: **{travelers}**")

    st.write("")
    st.write("**Cost components**")
    st.write(f"- Flights total (tickets): **${flights_total:,.0f}**")
    st.write(f"  - Source: {flight_source_note or 'No flight pricing source available (defaults to $0).'}")
    st.write(f"- Checked bags total: **${bags_total:,.0f}**")
    st.write(f"- Hotel total: **${hotel_total:,.0f}**")
    st.write(f"- Meals total: **${meals_total:,.0f}**")
    if include_rental_car:
        st.write(f"- Hertz rental car total: **${rental_total:,.0f}**")
    else:
        st.write("- Hertz rental car total: **$0** (excluded)")
    st.write(f"- Other fixed costs: **${other_fixed_costs:,.0f}**")

    st.markdown("</div>", unsafe_allow_html=True)

    # Grand total (single green box)
    st.success(f"Grand total: ${grand_total:,.0f}")

    # -----------------------------------------------------
    # Geek math & logic (collapsible)
    # -----------------------------------------------------
    with st.expander("Show detailed cost logic (geek math)", expanded=False):
        st.markdown("### Trip length")
        st.markdown(
            """
            - **Trip days**  
              `trip_days = max((return_date - departure_date).days + 1, 1)`
            - **Trip nights**  
              `trip_nights = max((return_date - departure_date).days, 0)`
            """
        )

        st.markdown("### Flights (Amadeus)")
        st.markdown(
            """
            - Uses the Amadeus **Flight Offers Search** API.
            - Parameters: origin, destination, departure date, return date, `adults = 1`,
              `currency = "USD"`, and your preferred airline via `includedAirlineCodes`.
            - For each returned offer, the app reads `offer["price"]["grandTotal"]`.
            - It averages all those values to get an **estimated round-trip fare per traveler**.

            **Formulas**

            ```python
            flight_cost_per_person = average(offer["price"]["grandTotal"] for offer in results)
            flights_total = flight_cost_per_person * travelers
            ```
            """
        )

        st.markdown("### Checked bags")
        st.markdown(
            """
            - Assumes **1 checked bag per traveler** for the whole round trip.
            - Detects **domestic vs non-domestic** using a predefined set of U.S. airports.
            - Domestic routes:
              - Southwest → `$0` (bags fly free)
              - Delta / JetBlue / American → `~$70` per traveler for the round trip
              - Other airlines → default `$70` per traveler
            - Non-domestic routes → bag fee treated as `$0`.

            **Formula**

            ```python
            if is_domestic_route(origin, dest):
                per_traveler = DOMESTIC_BAG_FEE_BY_AIRLINE.get(preferred_airline, 70.0)
            else:
                per_traveler = 0.0

            bags_total = per_traveler * travelers
            ```
            """
        )

        st.markdown("### Hotel (nightly estimate, no API)")
        st.markdown(
            """
            - Each destination airport has a typical **business nightly rate** in
              `HOTEL_BASE_RATE_BY_AIRPORT`.
            - If the airport is missing, the app uses `DEFAULT_HOTEL_NIGHTLY_RATE`.
            - One room per traveler.

            **Formulas**

            ```python
            hotel_nightly_rate = HOTEL_BASE_RATE_BY_AIRPORT.get(
                dest_airport, DEFAULT_HOTEL_NIGHTLY_RATE
            )
            hotel_total = hotel_nightly_rate * trip_nights * travelers
            ```
            """
        )

        st.markdown("### Meals (daily estimate, no GSA API)")
        st.markdown(
            """
            - Base per-diem: **$100/day** per traveler.
            - If destination airport is in `EXPENSIVE_AIRPORTS` → **+25%**.
            - If destination airport is in `MID_TIER_AIRPORTS` → **+10%**.
            - Otherwise stays at `$100/day`.

            **Formulas**

            ```python
            meal_rate_per_day = 100.0
            if dest_airport in EXPENSIVE_AIRPORTS:
                meal_rate_per_day *= 1.25
            elif dest_airport in MID_TIER_AIRPORTS:
                meal_rate_per_day *= 1.10

            meals_total = meal_rate_per_day * trip_days * travelers
            ```
            """
        )

        st.markdown("### Hertz SUV rental (estimate, no API)")
        st.markdown(
            """
            - Each airport has a **base daily rate** for a standard car in
              `HERTZ_BASE_DAILY_BY_AIRPORT`.
            - If missing, the app falls back to `80.0` USD per day.
            - The estimate applies:
              - **SUV uplift**: +15%  
              - **Membership discount**: -12% (hidden from auditors)
            - Assumes **one shared vehicle** for the team.

            **Formulas**

            ```python
            base = HERTZ_BASE_DAILY_BY_AIRPORT.get(dest_airport, 80.0)
            suv_price = base * 1.15
            hertz_daily_rate = suv_price * (1 - 0.12)

            rental_total = hertz_daily_rate * trip_days  # if include_rental_car else 0
            ```
            """
        )

        st.markdown("### Final cost roll-up")
        st.markdown(
            """
            **Component totals**

            ```python
            flights_total = flight_cost_per_person * travelers
            bags_total    = estimate_bag_fee_total(...)
            hotel_total   = hotel_nightly_rate * trip_nights * travelers
            meals_total   = meal_rate_per_day * trip_days * travelers
            rental_total  = hertz_daily_rate * trip_days  # if include_rental_car else 0
            ```

            **Grand total**

            ```python
            grand_total = (
                flights_total
                + bags_total
                + hotel_total
                + meals_total
                + rental_total
                + other_fixed_costs
            )
            ```
            """
        )

        st.markdown(
            '<div class="miip-microcopy">'
            "These heuristics are tuned for typical MIIP audit travel and can be adjusted centrally as pricing shifts."
            "</div>",
            unsafe_allow_html=True,
        )
else:
    st.info("Fix the date error above to see the full trip cost breakdown and grand total.")
