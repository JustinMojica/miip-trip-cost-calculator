import math
import re
from datetime import date
from typing import Optional, List

import requests
import streamlit as st
from amadeus import Client, ResponseError


# -----------------------------------------------------------------------------
# 1. READ SECRETS (these come from Streamlit "Secrets")
# -----------------------------------------------------------------------------

AMADEUS_CLIENT_ID = st.secrets["amadeus"]["client_id"]
AMADEUS_CLIENT_SECRET = st.secrets["amadeus"]["client_secret"]
AMADEUS_HOSTNAME = st.secrets["amadeus"].get("hostname", "production")

GSA_API_KEY = st.secrets["gsa"]["api_key"]

# -----------------------------------------------------------------------------
# 2. INITIALIZE AMADEUS CLIENT
# -----------------------------------------------------------------------------

amadeus = Client(
    client_id=AMADEUS_CLIENT_ID,
    client_secret=AMADEUS_CLIENT_SECRET,
    hostname=AMADEUS_HOSTNAME,
)


# -----------------------------------------------------------------------------
# 3. HELPER FUNCTIONS
# -----------------------------------------------------------------------------


def get_amadeus_average_fare(
    origin: str,
    destination: str,
    departure: date,
    return_date: date,
    adults: int,
    preferred_airline_code: Optional[str],
) -> Optional[float]:
    """
    Call Amadeus Flight Offers Search and compute the average grandTotal
    for the chosen airline (if provided). Returns None on error.
    """
    try:
        params = {
            "originLocationCode": origin,
            "destinationLocationCode": destination,
            "departureDate": departure.isoformat(),
            "returnDate": return_date.isoformat(),
            "adults": adults,
            "currencyCode": "USD",
            "max": 20,
        }

        response = amadeus.shopping.flight_offers_search.get(**params)
        offers = response.data

        if not offers:
            return None

        prices: List[float] = []

        for offer in offers:
            # Airline code is usually in validatingAirlineCodes or first segment carrierCode
            airline_code = None
            if "validatingAirlineCodes" in offer and offer["validatingAirlineCodes"]:
                airline_code = offer["validatingAirlineCodes"][0]
            else:
                try:
                    airline_code = offer["itineraries"][0]["segments"][0]["carrierCode"]
                except Exception:
                    pass

            # Filter by preferred airline if specified
            if preferred_airline_code and airline_code != preferred_airline_code:
                continue

            try:
                total = float(offer["price"]["grandTotal"])
                prices.append(total)
            except Exception:
                continue

        if not prices:
            return None

        return sum(prices) / len(prices)

    except ResponseError as e:
        st.error(f"Amadeus flight search failed: [{e.response.status_code}] {e}")
        return None
    except Exception as e:
        st.error(f"Unexpected error during Amadeus flight search: {e}")
        return None


def get_amadeus_average_hotel_rate(
    city_airport_code: str,
    preferred_brand: str,
    nights: int,
) -> Optional[float]:
    """
    Simple Amadeus hotel search:
    - Uses destination airport code as the cityCode.
    - Filters hotels whose name contains the preferred brand string.
    - Computes average nightly rate across returned offers.

    Returns None on error.
    """
    try:
        # Amadeus expects IATA "cityCode" (often same as airport code, e.g. BOS/TPA)
        city_code = city_airport_code.upper()[:3]

        response = amadeus.shopping.hotel_offers.get(cityCode=city_code)
        hotels = response.data

        if not hotels:
            return None

        nightly_rates: List[float] = []

        for hotel in hotels:
            hotel_name = hotel.get("hotel", {}).get("name", "") or ""
            # Filter by preferred brand name if provided (Marriott, Hilton, Wyndham)
            if preferred_brand and preferred_brand.lower() not in hotel_name.lower():
                continue

            for offer in hotel.get("offers", []):
                try:
                    total_price = float(offer["price"]["total"])
                    nightly_rate = total_price / max(nights, 1)
                    nightly_rates.append(nightly_rate)
                except Exception:
                    continue

        if not nightly_rates:
            return None

        return sum(nightly_rates) / len(nightly_rates)

    except ResponseError as e:
        st.error(f"Amadeus hotel list failed: [{e.response.status_code}] {e}")
        return None
    except Exception as e:
        st.error(f"Unexpected error during Amadeus hotel search: {e}")
        return None


def extract_zip_from_address(address: str) -> Optional[str]:
    """
    Try to pull a 5-digit US ZIP code from the client office address.
    Example: '123 Main St, Tampa, FL 33602' -> '33602'
    """
    if not address:
        return None

    match = re.search(r"\b(\d{5})\b", address)
    if match:
        return match.group(1)
    return None


def get_gsa_meals_rate_from_zip(zip_code: str, year: int) -> Optional[float]:
    """
    Call GSA Per Diem API to get M&IE (Meals & Incidental Expenses) rate
    for a given ZIP and year.

    Endpoint pattern:
        /v2/rates/zip/{zip}/year/{year}?api_key=...

    We pull rates[0].rate[0].meals which is a daily M&IE amount in USD.
    """
    try:
        base_url = "https://api.gsa.gov/travel/perdiem"
        url = f"{base_url}/v2/rates/zip/{zip_code}/year/{year}?api_key={GSA_API_KEY}"

        resp = requests.get(url, timeout=10)

        if resp.status_code == 404:
            st.error(
                f"GSA per diem API error: 404 Not Found for ZIP {zip_code} and year {year}."
            )
            return None

        resp.raise_for_status()
        data = resp.json()

        rates = data.get("rates", [])
        if not rates:
            st.error("GSA per diem API returned no rates for this location.")
            return None

        first_rate_block = rates[0]
        rate_array = first_rate_block.get("rate", [])
        if not rate_array:
            st.error("GSA per diem API response did not contain a 'rate' array.")
            return None

        meals = rate_array[0].get("meals")
        if meals is None:
            st.error("GSA per diem API response did not contain a 'meals' value.")
            return None

        return float(meals)

    except requests.HTTPError as e:
        st.error(f"GSA per diem API HTTP error: {e}")
        return None
    except Exception as e:
        st.error(f"GSA per diem API unexpected error: {e}")
        return None


def airline_name_to_code(name: str) -> Optional[str]:
    """
    Map airline display name to IATA carrier code for Amadeus.
    """
    mapping = {
        "JetBlue": "B6",
        "Delta": "DL",
        "American": "AA",
        "Southwest": "WN",
    }
    return mapping.get(name)


# -----------------------------------------------------------------------------
# HERTZ RENTAL ESTIMATE (HIDDEN MEMBERSHIP DISCOUNT)
# -----------------------------------------------------------------------------


def get_estimated_hertz_membership_daily_rate(destination_airport: str) -> float:
    """
    Estimate Hertz SUV daily rate for MIIP, with membership discount applied.
    Auditors never see the discount %, only the final daily rate.

    1) Choose a base 'public' SUV rate by airport.
    2) Apply a hidden membership discount (e.g. 20% off).
    3) Return the discounted daily rate.
    """
    # Base "public" SUV daily rates by airport (before membership)
    base_rates = {
        "BOS": 80.0,
        "MHT": 70.0,
        "TPA": 65.0,
        # Add more airports as you learn real averages:
        # "MCO": 65.0,
        # "FLL": 65.0,
        # "ATL": 70.0,
        # "DFW": 70.0,
    }

    # Fallback base if an airport is not listed
    fallback_base_rate = 70.0

    airport = (destination_airport or "").upper()
    base = base_rates.get(airport, fallback_base_rate)

    # Hidden membership discount (e.g. 20% off Hertz for MIIP)
    MEMBERSHIP_DISCOUNT_PERCENT = 20.0  # change this in code if your deal changes

    discounted = base * (1.0 - MEMBERSHIP_DISCOUNT_PERCENT / 100.0)

    # Round for clean display
    return round(discounted, 2)


# -----------------------------------------------------------------------------
# 4. STREAMLIT UI
# -----------------------------------------------------------------------------

st.set_page_config(page_title="MIIP Trip Cost Calculator", layout="wide")

st.title("MIIP Trip Cost Calculator")
st.caption(
    "Automatically estimate flight, hotel, meals (GSA per diem), "
    "and Hertz rental costs for audit trips."
)

# --- Top-level layout --------------------------------------------------------
col_left, col_right = st.columns(2)

# ----- Traveler & flight info -----------------------------------------------
with col_left:
    st.subheader("Traveler & flights")

    auditor_name = st.text_input("Auditor name", value="")

    num_travelers = st.number_input(
        "Number of travelers (each gets their own room)",
        min_value=1,
        step=1,
        value=1,
    )

    departure_airport = st.selectbox(
        "Departure airport",
        options=["BOS", "MHT"],
        index=0,
    )

    destination_airport = st.text_input(
        "Destination airport (IATA, e.g. TPA)",
        value="TPA",
        help="3-letter airport code for the audit city.",
    )

    preferred_airline = st.selectbox(
        "Preferred airline",
        options=["Delta", "Southwest", "JetBlue", "American"],
        index=2,  # JetBlue default
    )

# ----- Client office & hotel / meals info -----------------------------------
with col_right:
    st.subheader("Client & hotel")

    client_office_address = st.text_input(
        "Client office address",
        value="",
        help=(
            "Used to approximate hotel proximity and to look up GSA meal per diem "
            "via ZIP code (e.g. '123 Main St, Tampa, FL 33602')."
        ),
    )

    destination_city_for_meals = st.text_input(
        "Destination city (for display only)",
        value="Tampa",
        help="For your reference. Meals are actually based on the ZIP code "
             "extracted from the client office address.",
    )

    destination_state_for_meals = st.text_input(
        "Destination state (2-letter)",
        value="FL",
        max_chars=2,
    )

    preferred_hotel_brand = st.selectbox(
        "Preferred hotel brand",
        options=["Marriott", "Hilton", "Wyndham"],
        index=0,
    )

# ----- Dates & ground costs --------------------------------------------------
st.subheader("Dates & ground costs")

col_dates_left, col_dates_right = st.columns(2)

with col_dates_left:
    # Streamlit requires a default; we use today as a starting point.
    departure_date = st.date_input(
        "Departure date",
        value=date.today(),
        help="Select your departure date.",
    )

with col_dates_right:
    # Return date cannot be earlier than departure date.
    return_date = st.date_input(
        "Return date",
        value=departure_date,
        min_value=departure_date,
        help="Must be on or after the departure date.",
    )

include_rental_car = st.checkbox("Include Hertz rental car", value=True)
st.caption(
    "If checked, the tool will estimate Hertz SUV rental cost automatically "
    "using MIIP's internal membership-adjusted rate (hidden)."
)

other_fixed_costs = st.number_input(
    "Other fixed costs (USD)",
    min_value=0.0,
    value=0.0,
    step=10.0,
    help="Any additional one-time costs (parking, tolls, etc.)",
)

trip_nights = max((return_date - departure_date).days, 0)
trip_days = trip_nights or 1  # at least one day for meals / car

# -----------------------------------------------------------------------------
# 5. FLIGHTS SECTION
# -----------------------------------------------------------------------------

st.subheader("3. Flights (preferred airline)")

flight_calc_mode = st.radio(
    "How should we calculate flights?",
    options=["Use Amadeus average (preferred airline)", "Enter manually"],
    index=0,
)

manual_flight_cost = st.number_input(
    "Manual flight cost per person (round trip, USD)",
    min_value=0.0,
    value=0.0,
    step=10.0,
)

avg_flight_cost_per_person: Optional[float] = None

if flight_calc_mode == "Use Amadeus average (preferred airline)":
    st.info("Will query Amadeus for an average round-trip fare for the preferred airline.")

    airline_code = airline_name_to_code(preferred_airline)

    avg_flight_cost_per_person = get_amadeus_average_fare(
        origin=departure_airport,
        destination=destination_airport,
        departure=departure_date,
        return_date=return_date,
        adults=num_travelers,
        preferred_airline_code=airline_code,
    )

    if avg_flight_cost_per_person is None:
        st.error(
            "Unable to retrieve pricing from Amadeus. Please double-check airports, "
            "dates, and that your Production API keys are active. "
            "If the problem persists, use manual flight pricing."
        )
else:
    st.info("Using manually entered flight cost per person.")
    avg_flight_cost_per_person = manual_flight_cost

# -----------------------------------------------------------------------------
# 6. HOTEL SECTION
# -----------------------------------------------------------------------------

st.subheader("4. Hotel (preferred brand)")

hotel_calc_mode = st.radio(
    "How should we calculate hotel?",
    options=["Use Amadeus hotel average (preferred brand)", "Enter manually"],
    index=1,
)

manual_hotel_nightly_rate = st.number_input(
    "Manual hotel nightly rate (USD)",
    min_value=0.0,
    value=180.0,
    step=10.0,
)

avg_hotel_nightly_rate: Optional[float] = None

if hotel_calc_mode == "Use Amadeus hotel average (preferred brand)":
    if trip_nights <= 0:
        st.warning("Trip has zero nights; hotel cost will be zero.")
        avg_hotel_nightly_rate = 0.0
    else:
        st.info(
            "Will query Amadeus for hotels in the destination city and "
            f"average nightly rate for {preferred_hotel_brand} brands."
        )
        avg_hotel_nightly_rate = get_amadeus_average_hotel_rate(
            city_airport_code=destination_airport,
            preferred_brand=preferred_hotel_brand,
            nights=trip_nights,
        )

        if avg_hotel_nightly_rate is None:
            st.error(
                "Unable to retrieve hotel pricing from Amadeus. "
                "You can switch to 'Enter manually' to provide a nightly rate."
            )
else:
    st.info("Using manually entered hotel nightly rate.")
    avg_hotel_nightly_rate = manual_hotel_nightly_rate

# -----------------------------------------------------------------------------
# 7. MEALS (GSA PER DIEM) SECTION
# -----------------------------------------------------------------------------

st.subheader("5. Meals (GSA Per Diem – M&IE only)")

meals_rate_per_day: Optional[float] = None

zip_code = extract_zip_from_address(client_office_address)

if not zip_code:
    st.error(
        "Could not detect a ZIP code in the client office address. "
        "Please include a 5-digit ZIP (e.g. 'Tampa, FL 33602') to use GSA per diem."
    )
else:
    fiscal_year = departure_date.year
    st.caption(
        f"Using GSA M&IE per diem for ZIP **{zip_code}** and fiscal year **{fiscal_year}**."
    )

    meals_rate_per_day = get_gsa_meals_rate_from_zip(zip_code, fiscal_year)

if meals_rate_per_day is None:
    meals_rate_per_day = 0.0

# -----------------------------------------------------------------------------
# 8. COST CALCULATION
# -----------------------------------------------------------------------------

if st.button("Calculate trip cost"):
    if avg_flight_cost_per_person is None:
        st.error(
            "Flight cost per person is not available. Please either let Amadeus "
            "calculate it successfully or enter a manual value."
        )
    else:
        # Flights: each traveler buys a seat
        total_flights = avg_flight_cost_per_person * num_travelers

        # Hotels: each traveler has their own room
        hotel_nights = trip_nights
        total_hotel = avg_hotel_nightly_rate * hotel_nights * num_travelers

        # Meals: GSA per-diem * days * travelers
        total_meals = meals_rate_per_day * trip_days * num_travelers

        # Rental car: one shared Hertz SUV for the group
        if include_rental_car:
            hertz_daily_rate = get_estimated_hertz_membership_daily_rate(destination_airport)
            total_rental = hertz_daily_rate * trip_days
        else:
            hertz_daily_rate = 0.0
            total_rental = 0.0

        total_cost = (
            total_flights
            + total_hotel
            + total_meals
            + total_rental
            + other_fixed_costs
        )

        st.markdown("### Trip cost summary")

        st.write(f"**Auditor(s):** {auditor_name or 'N/A'}")
        st.write(f"**Travelers:** {num_travelers}")
        st.write(
            f"**Route:** {departure_airport} → {destination_airport}"
        )
        st.write(
            f"**Dates:** {departure_date.isoformat()} to {return_date.isoformat()} "
            f"({trip_days} day(s), {trip_nights} night(s))"
        )

        st.markdown("---")

        st.write(
            f"**Flights total** ({num_travelers} × ${avg_flight_cost_per_person:,.2f}): "
            f"${total_flights:,.2f}"
        )
        st.write(
            f"**Hotel total** ({num_travelers} rooms × {hotel_nights} night(s) "
            f"× ${avg_hotel_nightly_rate:,.2f}/night): ${total_hotel:,.2f}"
        )
        st.write(
            f"**Meals total** ({num_travelers} traveler(s) × {trip_days} day(s) "
            f"× ${meals_rate_per_day:,.2f}/day): ${total_meals:,.2f}"
        )
        st.write(
            f"**Hertz rental total** "
            f"({trip_days} day(s) × ${hertz_daily_rate:,.2f}/day, 1 shared vehicle): "
            f"${total_rental:,.2f}"
        )
        st.write(f"**Other fixed costs**: ${other_fixed_costs:,.2f}")

        st.markdown("### **Grand total**")
        st.markdown(f"# ${total_cost:,.2f}")
