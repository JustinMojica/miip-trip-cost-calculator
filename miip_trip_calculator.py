import math
from datetime import date
from typing import Optional, Tuple, List

import requests
import streamlit as st
from amadeus import Client, ResponseError


# =========================
# 1. PAGE CONFIG
# =========================

st.set_page_config(
    page_title="MIIP Trip Cost Calculator",
    page_icon="✈️",
    layout="wide",
)


# =========================
# 2. DEBUG: AMADEUS CONNECTION TEST
# =========================

def amadeus_debug_test():
    """
    Quick debug helper to verify that Streamlit secrets match what Amadeus expects.
    Shows short hashes of the keys (not the real values) and does a tiny test call.
    """
    import hashlib

    try:
        cfg = st.secrets["amadeus"]
    except Exception as exc:
        st.error(f"[DEBUG] Cannot read [amadeus] from secrets: {exc}")
        return

    cid = cfg.get("client_id", "")
    csec = cfg.get("client_secret", "")
    host = cfg.get("hostname", "")

    st.write("**[DEBUG] Hostname:**", repr(host))
    st.write(
        "**[DEBUG] client_id hash:**",
        hashlib.sha256(cid.encode()).hexdigest()[:10],
    )
    st.write(
        "**[DEBUG] client_secret hash:**",
        hashlib.sha256(csec.encode()).hexdigest()[:10],
    )

    try:
        client = Client(
            client_id=cid,
            client_secret=csec,
            hostname=host or "production",
        )
        # Very cheap test call: look up TPA airport
        resp = client.reference_data.locations.get(
            keyword="TPA",
            subType="AIRPORT",
            page={"limit": 1},
        )
        st.success("[DEBUG] Amadeus test OK – credentials accepted.")
    except ResponseError as e:
        st.error(f"[DEBUG] Amadeus test failed: [{e.response.status_code}] {e}")
    except Exception as e:
        st.error(f"[DEBUG] Unexpected error: {e}")


# =========================
# 3. AMADEUS CLIENT + HELPERS
# =========================

@st.cache_resource(show_spinner=False)
def get_amadeus_client() -> Client:
    """
    Build a single Amadeus client using secrets.
    Cached so we don't re-authenticate on every rerun.
    """
    cfg = st.secrets["amadeus"]
    client = Client(
        client_id=cfg["client_id"],
        client_secret=cfg["client_secret"],
        hostname=cfg.get("hostname", "production"),
    )
    return client


def parse_city_state_from_address(address: str) -> Tuple[str, str]:
    """
    Simple parser for addresses like:
        '123 Main St, Tampa, FL 33602'
    Returns (city, state) or ('','') if it can't parse.
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


# Airline and hotel mappings

AIRLINE_NAME_TO_CODE = {
    "Delta": "DL",
    "Southwest": "WN",
    "JetBlue": "B6",
    "American": "AA",
}

# NOTE: These chain codes are examples; they may need adjustment depending on your Amadeus account.
HOTEL_BRAND_TO_CHAIN_CODES = {
    "Marriott": ["EM"],   # Example Marriott chain code
    "Hilton": ["HL"],     # Example Hilton chain code
    "Wyndham": ["WY"],    # Example Wyndham chain code
}


# =========================
# 4. API CALLS
# =========================

def fetch_average_flight_cost(
    amadeus: Client,
    origin_iata: str,
    dest_iata: str,
    departure: date,
    return_date: Optional[date],
    preferred_airline: str,
) -> float:
    """
    Query Amadeus Flight Offers Search and return
    average per-person roundtrip fare in USD.

    Raises RuntimeError if anything fails.
    """
    params = {
        "originLocationCode": origin_iata.upper(),
        "destinationLocationCode": dest_iata.upper(),
        "departureDate": departure.isoformat(),
        "adults": 1,
        "currencyCode": "USD",
        "max": 20,
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
        raise RuntimeError(f"Amadeus flight search failed: [{err.response.status_code}] {err}") from err

    if not offers:
        raise RuntimeError("No flight offers returned from Amadeus for these dates/airports.")

    prices: List[float] = []
    for offer in offers:
        try:
            total_price = float(offer["price"]["grandTotal"])
            prices.append(total_price)  # already per 1 adult
        except Exception:
            continue

    if not prices:
        raise RuntimeError("Could not interpret any prices from Amadeus flight response.")

    return sum(prices) / len(prices)


def fetch_average_hotel_rate(
    amadeus: Client,
    dest_city_iata: str,
    preferred_brand: str,
    checkin: date,
    checkout: date,
    travelers: int,
) -> float:
    """
    Use Amadeus hotel APIs to estimate an average nightly rate
    (USD) for the preferred hotel brand in the destination city.

    Strategy:
      1. Get a list of hotels in the city.
      2. Filter to those whose chainCode matches the brand mapping.
      3. Query offers and compute nightly rate from cheapest offers.
      4. Return average nightly rate.

    Raises RuntimeError if anything fails.
    """
    city_code = dest_city_iata[:3].upper()
    chain_codes = HOTEL_BRAND_TO_CHAIN_CODES.get(preferred_brand, [])
    chain_param = ",".join(chain_codes) if chain_codes else None

    hotel_list_params = {"cityCode": city_code, "radius": 20}
    if chain_param:
        hotel_list_params["chainCodes"] = chain_param

    try:
        hotels_resp = amadeus.reference_data.locations.hotels.by_city.get(
            **hotel_list_params
        )
        hotels = hotels_resp.data
    except ResponseError as err:
        raise RuntimeError(f"Amadeus hotel list failed: [{err.response.status_code}] {err}") from err

    if not hotels:
        raise RuntimeError("No hotels found for that city/brand combination.")

    sampled_hotels = hotels[:5]
    hotel_ids = ",".join(h["hotelId"] for h in sampled_hotels if "hotelId" in h)

    if not hotel_ids:
        raise RuntimeError("Hotels returned without hotelId; cannot query offers.")

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
        raise RuntimeError(f"Amadeus hotel offers failed: [{err.response.status_code}] {err}") from err

    if not offers:
        raise RuntimeError("No hotel offers returned from Amadeus.")

    nights = (checkout - checkin).days
    if nights <= 0:
        raise RuntimeError("Return date must be after departure date for hotels.")

    nightly_rates: List[float] = []
    for wrapper in offers:
        hotel_offers = wrapper.get("offers", [])
        if not hotel_offers:
            continue
        try:
            cheapest = min(
                hotel_offers,
                key=lambda o: float(o["price"]["total"]),
            )
            total_price = float(cheapest["price"]["total"])
            nightly = total_price / nights
            nightly_rates.append(nightly)
        except Exception:
            continue

    if not nightly_rates:
        raise RuntimeError("Could not interpret any hotel prices from Amadeus response.")

    return sum(nightly_rates) / len(nightly_rates)


def fetch_gsa_meals_per_diem(
    city: str,
    state: str,
    travel_date: date,
    days: int,
    travelers: int,
) -> float:
    """
    Call GSA Per Diem API and return total meals & incidentals cost
    for the whole trip (all travelers).

    We ONLY use the M&IE (meals) portion.
    Endpoint style used:
      GET https://api.gsa.gov/travel/perdiem/v2/rates
        ?city=City&state=ST&year=YYYY&api_key=KEY

    Raises RuntimeError if anything fails.
    """
    try:
        api_key = st.secrets["gsa"]["api_key"]
    except Exception as exc:
        raise RuntimeError("GSA API key missing in [gsa].api_key secrets.") from exc

    year = travel_date.year
    url = "https://api.gsa.gov/travel/perdiem/v2/rates"
    params = {
        "city": city,
        "state": state,
        "year": str(year),
        "api_key": api_key,
    }

    try:
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise RuntimeError(f"GSA per diem API error: {exc}") from exc

    try:
        payload = resp.json()
    except ValueError as exc:
        raise RuntimeError("GSA per diem API returned non-JSON response.") from exc

    rates = payload.get("rates", [])
    if not rates:
        raise RuntimeError("GSA per diem API returned no rate data for that city/state/year.")

    month = travel_date.month
    chosen_meals = None

    for r in rates:
        try:
            if int(r.get("month", 0)) == month:
                chosen_meals = float(r["rate"][0]["meals"])
                break
        except Exception:
            continue

    if chosen_meals is None:
        try:
            chosen_meals = float(rates[0]["rate"][0]["meals"])
        except Exception as exc:
            raise RuntimeError("Could not interpret meals value from GSA response.") from exc

    daily_meals_per_person = chosen_meals
    return daily_meals_per_person * days * travelers


# =========================
# 5. UI
# =========================

st.title("MIIP Trip Cost Calculator")
st.caption(
    "Automatically estimate **flight**, **hotel**, **car**, and **GSA meals per diem** "
    "for audit trips."
)

# Debug expander – TEMPORARY, JUST FOR CREDENTIALS
with st.expander("Amadeus debug (temporary – to fix 401)", expanded=True):
    st.markdown(
        "Use this to confirm Streamlit is using the same production keys as your Amadeus dashboard. "
        "Once it's green, you can collapse/remove this section."
    )
    if st.button("Run Amadeus debug test"):
        amadeus_debug_test()

with st.sidebar:
    st.header("How this works")
    st.write(
        """
        • Flights & hotels from **Amadeus (production)**  
        • Meals (M&IE only) from **GSA Per Diem API**  
        • Each traveler gets **their own room**  
        • No hard-coded defaults for flights, hotels, or meals.
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
        index=2,  # JetBlue by default if you like
    )

with col_b:
    client_address = st.text_input(
        "Client office address",
        help="Used to deduce city & state for meals and hotel city context.",
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
        "Preferred hotel brand",
        ["Marriott", "Hilton", "Wyndham"],
        index=0,
    )


# --- Destination & dates ---

st.subheader("Flights & hotel location")

col_c, col_d = st.columns(2)

with col_c:
    destination_airport = st.text_input(
        "Destination airport (IATA)",
        value="TPA",
        help="Used for flights and as the hotel city code (e.g. TPA for Tampa).",
    )

with col_d:
    st.write(" ")
    st.info(
        "Hotel search uses the **city code** associated with the destination airport "
        "(e.g. TPA → Tampa hotels)."
    )

st.subheader("Dates & ground costs")

col_e, col_f = st.columns(2)

with col_e:
    departure_date = st.date_input("Departure date", value=date.today())
    return_date = st.date_input(
        "Return date",
        value=date.today(),
        min_value=departure_date,
    )

with col_f:
    include_rental = st.checkbox("Include rental car", value=True)
    rental_daily_rate = st.number_input(
        "Rental car rate per day (USD)",
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

st.subheader("Car service (home ↔ airport)")

col_g, col_h = st.columns(2)
with col_g:
    car_service_one_way = st.number_input(
        "Car service one-way cost (USD)",
        min_value=0.0,
        step=10.0,
        value=160.0,
        help="Total cost for one-way between home and airport.",
    )

with col_h:
    st.write(" ")
    st.caption("Round-trip car service cost = 2 × this amount.")

st.markdown("---")

days = max((return_date - departure_date).days, 1)
nights = days

if st.button("Calculate trip cost"):
    # 1. Build Amadeus client once
    try:
        amadeus_client = get_amadeus_client()
    except Exception as exc:
        st.error(f"Failed to initialize Amadeus client: {exc}")
        st.stop()

    components = []

    # 2. Flights
    st.subheader("Flights (Amadeus)")
    try:
        with st.spinner("Querying Amadeus for flights..."):
            avg_flight_per_person = fetch_average_flight_cost(
                amadeus=amadeus_client,
                origin_iata=departure_airport,
                dest_iata=destination_airport,
                departure=departure_date,
                return_date=return_date,
                preferred_airline=preferred_airline,
            )
        total_flights = avg_flight_per_person * num_travelers
        st.write(
            f"- Average roundtrip fare per person ({preferred_airline}): "
            f"`${avg_flight_per_person:,.2f}`"
        )
        st.write(
            f"- Total flights for {num_travelers} traveler(s): "
            f"`${total_flights:,.2f}`"
        )
        components.append(("Flights", total_flights))
    except RuntimeError as exc:
        st.error(str(exc))
        st.stop()

    # 3. Hotels
    st.subheader("Hotels (Amadeus)")
    try:
        with st.spinner("Querying Amadeus for hotel rates..."):
            avg_hotel_nightly = fetch_average_hotel_rate(
                amadeus=amadeus_client,
                dest_city_iata=destination_airport,
                preferred_brand=preferred_hotel_brand,
                checkin=departure_date,
                checkout=return_date,
                travelers=num_travelers,
            )
        total_hotel = avg_hotel_nightly * nights * num_travelers
        st.write(
            f"- Average nightly rate per room ({preferred_hotel_brand}): "
            f"`${avg_hotel_nightly:,.2f}`"
        )
        st.write(
            f"- Total hotel: {nights} night(s) × {num_travelers} room(s) "
            f"= `${total_hotel:,.2f}`"
        )
        components.append(("Hotel", total_hotel))
    except RuntimeError as exc:
        st.error(str(exc))
        st.stop()

    # 4. Meals (GSA per diem)
    st.subheader("Meals (GSA Per Diem – M&IE only)")
    try:
        with st.spinner("Querying GSA per diem API..."):
            total_meals = fetch_gsa_meals_per_diem(
                city=dest_city_for_gsa,
                state=dest_state_for_gsa,
                travel_date=departure_date,
                days=days,
                travelers=num_travelers,
            )
        per_person_per_day = total_meals / (days * num_travelers)
        st.write(
            f"- Daily meals per person: `${per_person_per_day:,.2f}` "
            f"for **{dest_city_for_gsa}, {dest_state_for_gsa}**"
        )
        st.write(
            f"- Total meals: {days} day(s) × {num_travelers} traveler(s) "
            f"= `${total_meals:,.2f}`"
        )
        components.append(("Meals (GSA M&IE)", total_meals))
    except RuntimeError as exc:
        st.error(str(exc))
        st.stop()

    # 5. Car service & rental
    st.subheader("Ground transport")

    total_car_service = car_service_one_way * 2.0
    st.write(
        f"- Car service (round trip home ↔ airport): "
        f"`${total_car_service:,.2f}`"
    )
    components.append(("Car service (round trip)", total_car_service))

    if include_rental:
        total_rental = rental_daily_rate * days
        st.write(
            f"- Rental car: {days} day(s) × ${rental_daily_rate:,.2f}/day "
            f"= `${total_rental:,.2f}`"
        )
        components.append(("Rental car", total_rental))
    else:
        total_rental = 0.0
        st.write("- Rental car: not included")

    if other_fixed_costs > 0:
        st.write(f"- Other fixed costs: `${other_fixed_costs:,.2f}`")
        components.append(("Other fixed costs", other_fixed_costs))

    # 6. Summary
    st.markdown("---")
    st.subheader("Summary")

    total_trip_cost = sum(v for _, v in components)
    st.metric("Total estimated cost (USD)", f"${total_trip_cost:,.2f}")

    st.write("### Breakdown")
    for label, value in components:
        st.write(f"- **{label}**: `${value:,.2f}`")

    st.write("### Per-traveler view")
    st.write(f"- Travelers: **{num_travelers}**")
    st.write(f"- Length: **{days} day(s) / {nights} night(s)**")
    st.write(
        f"- Approx. cost per traveler: "
        f"`{total_trip_cost / num_travelers:,.2f}` USD"
    )

    if auditor_name:
        st.caption(
            f"Scenario for **{auditor_name}** from **{departure_airport}** "
            f"to **{destination_airport}**."
        )
