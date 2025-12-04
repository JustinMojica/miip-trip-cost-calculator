import math
from datetime import date
from typing import Optional, Tuple

import requests
import streamlit as st
from amadeus import Client, ResponseError


# =========================
# 1. HELPERS & INITIAL SETUP
# =========================

st.set_page_config(
    page_title="MIIP Trip Cost Calculator",
    page_icon="✈️",
    layout="wide",
)

# --- Amadeus client ---


@st.cache_resource(show_spinner=False)
def get_amadeus_client() -> Client:
    """
    Build a single Amadeus client using secrets.
    Cached so we don't re-authenticate on every rerun.
    """
    try:
        cfg = st.secrets["amadeus"]
        return Client(
            client_id=cfg["client_id"],
            client_secret=cfg["client_secret"],
            hostname=cfg.get("hostname", "production"),
        )
    except Exception as exc:  # missing or misnamed secrets
        st.error(f"Amadeus configuration error: {exc}")
        raise


def parse_city_state_from_address(address: str) -> Tuple[str, str]:
    """
    Very simple city/state parser for US addresses like:
        '123 Main St, Tampa, FL 33602'
    Returns (city, state). If it can't parse cleanly, returns ('','').
    """
    if not address:
        return "", ""

    parts = [p.strip() for p in address.split(",") if p.strip()]
    if len(parts) < 2:
        return "", ""

    city = parts[-2]
    last = parts[-1].strip()
    state = last.split()[0] if last else ""

    if len(state) != 2:
        state = ""

    return city, state


# --- Airline & hotel mappings ---

AIRLINE_NAME_TO_CODE = {
    "Delta": "DL",
    "Southwest": "WN",
    "JetBlue": "B6",
    "American": "AA",
}

# NOTE: these chain codes are examples and might need to be adjusted
# based on the Amadeus hotel chain list your account uses.
HOTEL_BRAND_TO_CHAIN_CODES = {
    "Marriott": ["EM"],   # Marriott International (example chain code)
    "Hilton": ["HL"],     # Hilton (example)
    "Wyndham": ["WY"],    # Wyndham (example)
}


# =========================
# 2. API CALLS
# =========================

def fetch_average_flight_cost(
    amadeus: Client,
    origin_iata: str,
    dest_iata: str,
    departure: date,
    return_date: Optional[date],
    adults: int,
    preferred_airline: str,
) -> Optional[float]:
    """
    Query Amadeus Flight Offers Search and return
    average per-person roundtrip fare in USD.
    """
    params = {
        "originLocationCode": origin_iata.upper(),
        "destinationLocationCode": dest_iata.upper(),
        "departureDate": departure.isoformat(),
        "adults": adults,
        "currencyCode": "USD",
        "max": 20,  # limit offers so we don't over-query
    }
    if return_date:
        params["returnDate"] = return_date.isoformat()

    airline_code = AIRLINE_NAME_TO_CODE.get(preferred_airline)
    if airline_code:
        params["includedAirlineCodes"] = airline_code

    try:
        resp = amadeus.shopping.flight_offers_search.get(**params)
        offers = resp.data
    except ResponseError as err:
        st.error(f"Amadeus flight search failed: [{err.response.status_code}] {err}")
        return None

    if not offers:
        st.warning("No flight offers returned from Amadeus for these dates/airports.")
        return None

    per_person_prices = []
    for offer in offers:
        try:
            total_price = float(offer["price"]["grandTotal"])
            per_person_prices.append(total_price / adults)
        except Exception:
            continue

    if not per_person_prices:
        st.warning("Could not interpret any prices from Amadeus flight response.")
        return None

    return sum(per_person_prices) / len(per_person_prices)


def fetch_average_hotel_rate(
    amadeus: Client,
    dest_city_iata: str,
    preferred_brand: str,
    checkin: date,
    checkout: date,
    travelers: int,
) -> Optional[float]:
    """
    Use Amadeus hotel APIs to estimate an average nightly rate
    (USD) for the preferred hotel brand in the destination city.

    Strategy:
      1. Get a list of hotels in the city.
      2. Filter to those whose chainCode matches the brand mapping.
      3. For a few of those properties, call hotel_offers_search and
         compute nightly rate from the cheapest offer.
      4. Return the average nightly rate across properties.
    """
    city_code = dest_city_iata[:3].upper()  # city IATA like TPA, BOS, etc

    chain_codes = HOTEL_BRAND_TO_CHAIN_CODES.get(preferred_brand, [])
    chain_param = ",".join(chain_codes) if chain_codes else None

    # 1. List hotels by city
    hotel_list_params = {"cityCode": city_code, "radius": 20}
    if chain_param:
        hotel_list_params["chainCodes"] = chain_param

    try:
        hotels_resp = amadeus.reference_data.locations.hotels.by_city.get(
            **hotel_list_params
        )
        hotels = hotels_resp.data
    except ResponseError as err:
        st.error(f"Amadeus hotel list failed: [{err.response.status_code}] {err}")
        return None

    if not hotels:
        st.warning("No hotels found for that city/brand combination.")
        return None

    # Only sample first few hotels to keep calls reasonable
    sampled_hotels = hotels[:5]
    hotel_ids = ",".join(h["hotelId"] for h in sampled_hotels if "hotelId" in h)

    if not hotel_ids:
        st.warning("Hotels returned without hotelId; cannot query offers.")
        return None

    try:
        offers_resp = amadeus.shopping.hotel_offers_search.get(
            hotelIds=hotel_ids,
            checkInDate=checkin.isoformat(),
            checkOutDate=checkout.isoformat(),
            adults=travelers,
            currency="USD",
        )
        offers = offers_resp.data
    except ResponseError as err:
        st.error(f"Amadeus hotel offers failed: [{err.response.status_code}] {err}")
        return None

    if not offers:
        st.warning("No hotel offers returned from Amadeus.")
        return None

    nights = (checkout - checkin).days
    if nights <= 0:
        return None

    nightly_rates = []
    for offer in offers:
        try:
            hotel_offers = offer.get("offers", [])
            if not hotel_offers:
                continue
            cheapest = min(
                hotel_offers, key=lambda o: float(o["price"]["total"])
            )
            total_price = float(cheapest["price"]["total"])
            nightly_rates.append(total_price / nights)
        except Exception:
            continue

    if not nightly_rates:
        st.warning("Could not interpret any hotel prices from Amadeus response.")
        return None

    return sum(nightly_rates) / len(nightly_rates)


def fetch_gsa_meals_per_diem(
    city: str, state: str, travel_date: date, days: int, travelers: int
) -> Optional[float]:
    """
    Call GSA Per Diem API and return total meals & incidentals cost
    for the whole trip (all travelers).

    We only use the M&IE (meals) portion. The endpoint we use:
      /v2/rates/city/{city}/state/{state}/year/{year}

    Then we pick the record for the travel month.
    """
    api_key = st.secrets["gsa"].get("api_key")
    if not api_key:
        st.error(
            "GSA API key is missing in secrets. Please set [gsa].api_key "
            "in your Streamlit secrets."
        )
        return None

    year = travel_date.year
    url = (
        f"https://api.gsa.gov/travel/perdiem/v2/rates/"
        f"city/{city}/state/{state}/year/{year}"
    )
    headers = {"X-API-KEY": api_key}

    try:
        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
    except requests.RequestException as exc:
        st.error(f"GSA per diem API error: {exc}")
        return None

    try:
        payload = resp.json()
    except ValueError:
        st.error("GSA per diem API returned non-JSON response.")
        return None

    rates = payload.get("rates", [])
    if not rates:
        st.warning("GSA per diem API returned no rate data.")
        return None

    month = travel_date.month

    # rates entries usually have month and a nested rate list
    chosen_meals = None
    for r in rates:
        try:
            if int(r.get("month", 0)) == month:
                chosen_meals = float(r["rate"][0]["meals"])
                break
        except Exception:
            continue

    if chosen_meals is None:
        # Fallback: just take the first month's meals value
        try:
            chosen_meals = float(rates[0]["rate"][0]["meals"])
        except Exception:
            st.error("Could not interpret meals value from GSA response.")
            return None

    daily_meals_per_person = chosen_meals
    return daily_meals_per_person * days * travelers


# =========================
# 3. UI
# =========================

st.title("MIIP Trip Cost Calculator")
st.caption(
    "Automatically estimate flight, hotel, rental car, and GSA meals (per diem) "
    "for audit trips."
)

with st.sidebar:
    st.header("How this works")
    st.write(
        """
        • Flights & hotels: **Amadeus APIs** using your production credentials  
        • Meals: **GSA Per Diem API** (M&IE only, per city/state & year)  
        • Each traveler gets **their own room**  
        • Costs are estimated; they may differ from final booking prices.
        """
    )


# --- Traveler & client info ---

st.subheader("Traveler & client info")

col_a, col_b = st.columns(2)

with col_a:
    auditor_name = st.text_input("Auditor name", value="Justin Mojica")
    num_travelers = st.number_input(
        "Number of travelers (each gets their own room)",
        min_value=1,
        step=1,
        value=1,
    )
    departure_airport = st.selectbox("Departure airport (IATA)", ["BOS", "MHT"])
    preferred_airline = st.selectbox(
        "Preferred airline",
        list(AIRLINE_NAME_TO_CODE.keys()),
        index=0,
    )

with col_b:
    client_address = st.text_input(
        "Client office address",
        help="Used to auto-detect city & state for meals and 'nearest' hotel city.",
        value="Tampa, FL",
    )

    auto_city, auto_state = parse_city_state_from_address(client_address)

    dest_city_for_gsa = st.text_input(
        "Destination city (for GSA per diem)",
        value=auto_city or "Tampa",
        help="Typically the city where the client office & hotel are located.",
    )
    dest_state_for_gsa = st.text_input(
        "Destination state (2-letter)",
        value=auto_state or "FL",
        max_chars=2,
    )
    preferred_hotel_brand = st.selectbox(
        "Preferred hotel brand", ["Marriott", "Hilton", "Wyndham"], index=0
    )

# --- Destination & dates ---

st.subheader("Flights & hotel location")

col_c, col_d = st.columns(2)

with col_c:
    destination_airport = st.text_input(
        "Destination airport (IATA)",
        value="TPA",
        help="Used for flights and hotel city (e.g. TPA for Tampa).",
    )

with col_d:
    st.write(" ")  # spacing
    st.info(
        "Hotel search will use the **city code** associated with the "
        "destination airport (e.g. TPA → Tampa)."
    )

st.subheader("Dates & ground costs")

col_e, col_f = st.columns(2)

with col_e:
    departure_date = st.date_input("Departure date", value=date.today())
    return_date = st.date_input(
        "Return date", value=date.today(), min_value=departure_date
    )

with col_f:
    include_rental = st.checkbox("Include rental car (Hertz or other)", value=True)
    rental_daily_rate = st.number_input(
        "Estimated rental car rate per day (USD)",
        min_value=0.0,
        step=5.0,
        value=60.0,
    )
    other_fixed_costs = st.number_input(
        "Other fixed costs (USD)",
        min_value=0.0,
        step=10.0,
        value=0.0,
        help="Parking, tolls, misc. costs not covered elsewhere.",
    )

st.subheader("Car service (to/from home airport)")

col_g, col_h = st.columns(2)
with col_g:
    car_service_one_way = st.number_input(
        "Car service one-way cost (USD)",
        min_value=0.0,
        step=10.0,
        value=160.0,
        help="Total cost for a one-way trip between home and airport.",
    )

with col_h:
    st.write(" ")
    st.caption(
        "Round-trip car service cost is assumed to be **two times** this amount."
    )

st.markdown("---")

# =========================
# 4. CALCULATION
# =========================

days = (return_date - departure_date).days or 1
nights = days  # for business trips we usually assume nights == days


if st.button("Calculate trip cost"):
    # Wrap everything so that a failure in one API doesn't crash the rest
    amadeus_client = get_amadeus_client()

    # --- Flights ---
    with st.spinner("Querying Amadeus for flights..."):
        avg_flight_per_person = fetch_average_flight_cost(
            amadeus=amadeus_client,
            origin_iata=departure_airport,
            dest_iata=destination_airport,
            departure=departure_date,
            return_date=return_date,
            adults=num_travelers,
            preferred_airline=preferred_airline,
        )

    if avg_flight_per_person is not None:
        total_flights = avg_flight_per_person * num_travelers
    else:
        total_flights = None

    # --- Hotels ---
    with st.spinner("Querying Amadeus for hotel rates..."):
        avg_hotel_nightly = fetch_average_hotel_rate(
            amadeus=amadeus_client,
            dest_city_iata=destination_airport,
            preferred_brand=preferred_hotel_brand,
            checkin=departure_date,
            checkout=return_date,
            travelers=num_travelers,
        )

    if avg_hotel_nightly is not None:
        total_hotel = avg_hotel_nightly * nights * num_travelers
    else:
        total_hotel = None

    # --- Meals (GSA per diem) ---
    with st.spinner("Querying GSA per diem (meals only)..."):
        total_meals = fetch_gsa_meals_per_diem(
            city=dest_city_for_gsa,
            state=dest_state_for_gsa,
            travel_date=departure_date,
            days=days,
            travelers=num_travelers,
        )

    # --- Ground costs ---
    total_car_service = car_service_one_way * 2.0  # round trip
    total_rental = rental_daily_rate * days if include_rental else 0.0
    total_other = other_fixed_costs

    # --- Aggregate ---
    components = []

    if total_flights is not None:
        components.append(("Flights", total_flights))
    if total_hotel is not None:
        components.append(("Hotel", total_hotel))
    if total_meals is not None:
        components.append(("Meals (GSA M&IE)", total_meals))

    components.append(("Car service (round trip)", total_car_service))
    if include_rental:
        components.append(("Rental car", total_rental))
    if total_other:
        components.append(("Other fixed costs", total_other))

    if not components:
        st.error(
            "Unable to calculate any component. Please check that your Amadeus "
            "and GSA credentials are valid and try again."
        )
    else:
        total_trip_cost = sum(c[1] for c in components)

        st.subheader("Estimated trip cost (all travelers)")

        st.metric("Total estimated cost (USD)", f"${total_trip_cost:,.2f}")

        st.write("### Breakdown")
        for label, value in components:
            st.write(f"- **{label}**: ${value:,.2f}")

        st.write("### Per-traveler view")
        st.write(f"- Number of travelers: **{num_travelers}**")
        st.write(f"- Trip length: **{days} days / {nights} nights**")
        st.write(f"- Approximate cost **per traveler**: ${total_trip_cost / num_travelers:,.2f}")
