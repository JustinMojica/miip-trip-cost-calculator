import math
import re
from datetime import date, datetime, timedelta
from typing import Optional, Tuple, List

import requests
import streamlit as st
from amadeus import Client, ResponseError


# =========================
# 1. Helper functions
# =========================

def load_amadeus_client() -> Optional[Client]:
    """
    Create an Amadeus client from Streamlit secrets.
    Returns None if secrets are missing or misconfigured.
    """
    try:
        cfg = st.secrets["amadeus"]
        client_id = cfg.get("client_id")
        client_secret = cfg.get("client_secret")
        hostname = cfg.get("hostname", "production")

        if not client_id or not client_secret:
            st.error("Amadeus secrets are missing. Please configure them in Streamlit.")
            return None

        amadeus = Client(
            client_id=client_id,
            client_secret=client_secret,
            hostname=hostname,
        )
        return amadeus
    except Exception as e:
        st.error(f"Unable to load Amadeus client from secrets: {e}")
        return None


def extract_zip_from_address(addr: str) -> Optional[str]:
    """
    Try to find a 5-digit ZIP code in the client office address.
    Returns None if no obvious ZIP is found.
    """
    if not addr:
        return None
    m = re.search(r"\b(\d{5})\b", addr)
    return m.group(1) if m else None


def trip_day_counts(dep: Optional[date], ret: Optional[date]) -> Tuple[int, int]:
    """
    Given departure & return dates, compute:
    - trip_days for flights / meals
    - trip_nights for hotels / lodging
    Both are at least 1 if both dates are present.
    """
    if not dep or not ret:
        return 0, 0

    delta_days = (ret - dep).days
    if delta_days <= 0:
        # Same-day trip: 1 day, 0–1 night depending on how you want to treat it.
        return 1, 0
    return delta_days, max(delta_days - 1, 1)


def estimate_hertz_daily_rate(airport_code: str) -> float:
    """
    Smart, *free* estimator for Hertz SUV daily rate.

    - Uses airport tiers (big hubs are more expensive).
    - Applies a hidden membership discount so auditors only see the
      final rate, not the discount %.

    You can adjust these numbers later if you want.
    """
    code = (airport_code or "").strip().upper()

    high_cost = {
        "BOS", "JFK", "LGA", "EWR", "SFO", "LAX", "ORD",
        "DCA", "IAD", "MIA", "SEA",
    }
    mid_cost = {
        "TPA", "MCO", "SAN", "DEN", "ATL", "CLT", "IAH",
        "PHX", "MSP", "DFW", "PHL",
    }

    if code in high_cost:
        base_daily = 95.0  # before membership
    elif code in mid_cost:
        base_daily = 80.0
    else:
        base_daily = 70.0  # smaller / cheaper markets

    membership_discount_factor = 0.87  # ~13% off, hidden from UI
    return round(base_daily * membership_discount_factor, 2)


def lookup_gsa_meal_rate(zip_code: str, travel_year: int) -> Tuple[float, Optional[str]]:
    """
    Call the GSA per diem API to get **meals & incidental (M&IE)** rate for a ZIP+year.

    Returns:
      (m_ie_daily_amount, error_message_or_None)
    """
    try:
        api_key = st.secrets["gsa"]["api_key"]
    except Exception:
        return 0.0, "GSA API key missing from Streamlit secrets."

    if not zip_code:
        return 0.0, "Destination ZIP could not be determined from the client address."

    url = f"https://api.gsa.gov/travel/perdiem/v2/rates/zip/{zip_code}"
    params = {
        "year": str(travel_year),
        "api_key": api_key,
    }

    try:
        resp = requests.get(url, params=params, timeout=10)
        if resp.status_code != 200:
            return 0.0, (
                f"GSA per diem API error: {resp.status_code} "
                f"{resp.reason} for URL: {resp.url}"
            )

        data = resp.json()
        rates = data.get("rates") or []

        if not rates:
            return 0.0, "GSA per diem API returned no rates for that ZIP/year."

        # The exact field name can vary; try a few likely ones.
        m_ie = None
        for rate in rates:
            for key in [
                "meals_and_incidentals",
                "meals_and_incidental_expenses",
                "m_and_ie",
                "meals",
                "mie",
            ]:
                if key in rate:
                    val = rate[key]
                    if isinstance(val, (int, float)):
                        m_ie = float(val)
                        break
                    if isinstance(val, dict):
                        for subkey in ["max", "standard_rate", "total", "full"]:
                            if subkey in val:
                                try:
                                    m_ie = float(val[subkey])
                                    break
                                except Exception:
                                    continue
                if m_ie is not None:
                    break
            if m_ie is not None:
                break

        if m_ie is None:
            return 0.0, (
                "GSA per diem API response did not contain a recognizable "
                "meals & incidentals field."
            )

        return float(m_ie), None
    except Exception as e:
        return 0.0, f"GSA per diem lookup error: {e}"


def get_average_flight_price(
    amadeus: Client,
    origin: str,
    destination: str,
    departure_date: date,
    return_date: Optional[date],
    adults: int,
    preferred_airline_code: str,
) -> Tuple[Optional[float], Optional[str]]:
    """
    Use Amadeus Flight Offers Search to compute an average round-trip price
    for a specific airline.

    Returns (average_price, error_message_or_None).
    """
    if adults <= 0:
        adults = 1

    params = {
        "originLocationCode": origin,
        "destinationLocationCode": destination,
        "departureDate": departure_date.isoformat(),
        "adults": adults,
        "currencyCode": "USD",
        "max": 50,
    }
    if return_date:
        params["returnDate"] = return_date.isoformat()

    try:
        resp = amadeus.shopping.flight_offers_search.get(**params)
        offers = resp.data or []

        if not offers:
            return None, "Amadeus returned no flight offers for the given route/dates."

        prices: List[float] = []
        for offer in offers:
            # Filter to offers where ALL segments are the preferred airline
            itineraries = offer.get("itineraries", [])
            all_segments = []
            for itin in itineraries:
                segs = itin.get("segments", [])
                all_segments.extend(segs)

            if not all_segments:
                continue

            airline_codes = {
                seg.get("carrierCode") or seg.get("marketingCarrierCode")
                for seg in all_segments
            }
            if preferred_airline_code and preferred_airline_code not in airline_codes:
                continue

            total_str = offer.get("price", {}).get("total")
            if not total_str:
                continue

            try:
                prices.append(float(total_str))
            except ValueError:
                continue

        if not prices:
            return None, (
                "No flight offers were found for the preferred airline; "
                "try a different airline or use manual pricing."
            )

        avg_price = sum(prices) / len(prices)
        return round(avg_price, 2), None

    except ResponseError as e:
        return None, f"Amadeus flight search failed: [{e.response.status_code}] {e}"
    except Exception as e:
        return None, f"Unexpected error during Amadeus flight search: {e}"


def get_average_hotel_nightly_rate(
    amadeus: Client,
    destination_airport: str,
    check_in: date,
    check_out: date,
    preferred_brand: str,
) -> Tuple[Optional[float], Optional[str]]:
    """
    Use Amadeus Hotel Offers Search to estimate an average nightly rate
    for the preferred hotel brand near the destination airport.

    This is intentionally conservative and heavily error-guarded, so that
    a bad response just yields an explanatory error instead of a crash.
    """
    try:
        city_code = destination_airport.strip().upper()
        if not city_code:
            return None, "Destination airport code is missing."

        params = {
            # Many Amadeus environments accept cityCode ≈ airport code for major cities.
            "cityCode": city_code,
            "checkInDate": check_in.isoformat(),
            "checkOutDate": check_out.isoformat(),
            "adults": 1,
            "roomQuantity": 1,
            "radius": 25,
            "radiusUnit": "MILE",
            "bestRateOnly": True,
        }

        resp = amadeus.shopping.hotel_offers_search.get(**params)
        hotels = resp.data or []
        if not hotels:
            return None, "Amadeus returned no hotel offers for the given city/dates."

        preferred_lower = preferred_brand.lower()
        nightly_rates: List[float] = []

        for item in hotels:
            hotel_info = item.get("hotel", {})
            name = (hotel_info.get("name") or "").lower()
            if preferred_lower not in name:
                # Only consider hotels whose name contains "Marriott", "Hilton", etc.
                continue

            offers = item.get("offers") or []
            for offer in offers:
                price = offer.get("price", {})
                total_str = price.get("total")
                if not total_str:
                    continue

                try:
                    total_price = float(total_str)
                except ValueError:
                    continue

                # derive number of nights from check-in/out if present, else use trip nights
                offer_in = offer.get("checkInDate")
                offer_out = offer.get("checkOutDate")
                try:
                    if offer_in and offer_out:
                        d_in = datetime.fromisoformat(offer_in).date()
                        d_out = datetime.fromisoformat(offer_out).date()
                        nights = max((d_out - d_in).days, 1)
                    else:
                        nights = max((check_out - check_in).days, 1)
                except Exception:
                    nights = max((check_out - check_in).days, 1)

                nightly_rates.append(total_price / nights)

        if not nightly_rates:
            return None, (
                f"No {preferred_brand}-branded hotels were found in Amadeus "
                "for that city and date range."
            )

        avg_nightly = sum(nightly_rates) / len(nightly_rates)
        return round(avg_nightly, 2), None

    except ResponseError as e:
        return None, f"Amadeus hotel list failed: [{e.response.status_code}] {e}"
    except Exception as e:
        return None, f"Unexpected error during Amadeus hotel search: {e}"


# =========================
# 2. Streamlit UI
# =========================

st.set_page_config(
    page_title="MIIP Trip Cost Calculator",
    layout="wide",
)

st.title("MIIP Trip Cost Calculator")
st.caption("Automatically estimate flights, hotel, meals (GSA per diem), and car costs for audit trips.")


# --------- 2.1 Traveler & client info ---------
st.subheader("Traveler & flights")

col_a, col_b = st.columns(2)

with col_a:
    auditor_name = st.text_input("Auditor name", value="")

    num_travelers = st.number_input(
        "Number of travelers (each gets their own room)",
        min_value=1,
        step=1,
        value=1,
    )

    departure_airport = st.selectbox(
        "Departure airport (IATA)",
        ["BOS", "MHT", "TPA", "MCO", "JFK", "LGA", "EWR", "Other"],
        index=0,
    )
    if departure_airport == "Other":
        departure_airport = st.text_input(
            "Enter departure airport code (IATA, e.g. BOS)",
            value="BOS",
            max_chars=3,
        ).upper()

    destination_airport = st.text_input(
        "Destination airport (IATA, e.g. TPA)",
        value="TPA",
        max_chars=3,
    ).upper()

    preferred_airline_label = st.selectbox(
        "Preferred airline",
        ["JetBlue", "Delta", "Southwest", "American"],
        index=0,
    )

with col_b:
    client_office_address = st.text_input(
        "Client office address (include city, state, and ZIP)",
        value="Tampa, FL 33602",
        help="Used for mapping, meals (GSA ZIP), and general documentation.",
    )

    preferred_hotel_brand = st.selectbox(
        "Preferred hotel brand",
        ["Marriott", "Hilton", "Wyndham"],
        index=0,
    )

    st.markdown("**Derived destination info (for display only)**")
    dest_zip = extract_zip_from_address(client_office_address) or "N/A"
    st.write(f"• Destination ZIP (for GSA): `{dest_zip}`")
    st.write(f"• Destination airport: `{destination_airport or '??'}`")


# --------- 2.2 Dates & ground costs ---------
st.subheader("Dates & ground costs")

col_dates, col_ground = st.columns(2)

with col_dates:
    # Streamlit now supports value=None for empty date inputs in recent versions.
    departure_date = st.date_input(
        "Departure date",
        value=None,
        min_value=date.today(),
        help="Select the first day of travel.",
    )

    if departure_date:
        return_date = st.date_input(
            "Return date",
            value=departure_date + timedelta(days=1),
            min_value=departure_date,
            help="Select the last day of travel.",
        )
    else:
        st.info("Select a departure date first to choose a return date.")
        return_date = None

with col_ground:
    include_rental = st.checkbox(
        "Include Hertz rental car",
        value=True,
        help=(
            "If checked, the tool will estimate a Hertz SUV rental cost "
            "automatically using MIIP's internal membership-adjusted rates."
        ),
    )

    other_fixed_costs = st.number_input(
        "Other fixed costs (USD)",
        min_value=0.0,
        step=10.0,
        value=0.0,
        help="Parking, tolls, other ground costs not covered elsewhere.",
    )


# --------- 2.3 Flights (preferred airline) ---------
st.subheader("3. Flights (preferred airline)")

flight_mode = st.radio(
    "How should we calculate flights?",
    ["Use Amadeus average (preferred airline)", "Enter manually"],
    index=0,
    help="Amadeus uses live flight offers. Manual override is available if needed.",
)

manual_flight_cost_per_person = 0.0
if flight_mode == "Enter manually":
    manual_flight_cost_per_person = st.number_input(
        "Manual flight cost per person (round trip, USD)",
        min_value=0.0,
        step=10.0,
        value=0.0,
    )
else:
    st.info("Will query Amadeus for an average round-trip fare for the preferred airline.")


# --------- 2.4 Hotel (preferred brand) ---------
st.subheader("4. Hotel (preferred brand)")

st.info(
    "This tool always uses Amadeus hotel data for nightly rates. "
    "If Amadeus cannot provide hotel pricing, the hotel portion will be $0 "
    "and a warning message will be shown in the summary."
)

# NOTE: Manual nightly rate input has been **removed** per your request.


# --------- 2.5 Meals (GSA per diem – M&IE only) ---------
st.subheader("5. Meals (GSA Per Diem – M&IE only)")

st.caption("Uses GSA M&IE per diem for the client ZIP, multiplied by number of travelers and days.")


# =========================
# 3. Calculation
# =========================

if st.button("Calculate trip cost"):
    # 3.1 Basic validation
    if not departure_airport or not destination_airport:
        st.error("Please provide both departure and destination airports.")
        st.stop()

    if not departure_date or not return_date:
        st.error("Please choose both departure and return dates.")
        st.stop()

    trip_days, trip_nights = trip_day_counts(departure_date, return_date)

    if trip_days <= 0:
        st.error("Trip length must be at least one day. Check your dates.")
        st.stop()

    # Airline code mapping
    airline_map = {
        "JetBlue": "B6",
        "Delta": "DL",
        "Southwest": "WN",
        "American": "AA",
    }
    preferred_airline_code = airline_map.get(preferred_airline_label, "")

    # 3.2 Amadeus client
    amadeus = load_amadeus_client()

    # 3.3 Flights
    flights_total = 0.0
    flight_detail = ""
    flight_error = None

    if flight_mode == "Enter manually":
        flights_total = manual_flight_cost_per_person * num_travelers
        flight_detail = (
            f"{num_travelers} traveler(s) × ${manual_flight_cost_per_person:,.2f} "
            f"(manual round trip)"
        )
    else:
        if not amadeus:
            flight_error = "Amadeus client is not available; cannot price flights."
        else:
            avg_price, err = get_average_flight_price(
                amadeus,
                departure_airport,
                destination_airport,
                departure_date,
                return_date,
                num_travelers,
                preferred_airline_code,
            )
            if err:
                flight_error = err
            elif avg_price is not None:
                flights_total = avg_price * num_travelers
                flight_detail = (
                    f"{num_travelers} traveler(s) × ${avg_price:,.2f} "
                    f"(Amadeus avg for {preferred_airline_label})"
                )
            else:
                flight_error = "Amadeus did not return a usable flight price."

    # 3.4 Hotel (Amadeus only, per traveler room)
    hotel_total = 0.0
    hotel_detail = ""
    hotel_error = None

    if amadeus:
        nightly_rate, err = get_average_hotel_nightly_rate(
            amadeus,
            destination_airport,
            departure_date,
            return_date,
            preferred_hotel_brand,
        )
        if err:
            hotel_error = err
        elif nightly_rate is not None:
            hotel_total = nightly_rate * trip_nights * num_travelers
            hotel_detail = (
                f"{num_travelers} rooms × {trip_nights} night(s) × "
                f"${nightly_rate:,.2f}/night (Amadeus avg for {preferred_hotel_brand})"
            )
        else:
            hotel_error = "Amadeus did not return a usable hotel rate."
    else:
        hotel_error = "Amadeus client is not available; cannot price hotels."

    # 3.5 Meals (GSA per diem M&IE)
    meals_total = 0.0
    meals_detail = ""
    meals_error = None

    travel_year = departure_date.year
    zip_for_gsa = extract_zip_from_address(client_office_address)

    daily_mie, mie_err = lookup_gsa_meal_rate(zip_for_gsa, travel_year)
    if mie_err:
        meals_error = mie_err
    else:
        meals_total = daily_mie * trip_days * num_travelers
        meals_detail = (
            f"{num_travelers} traveler(s) × {trip_days} day(s) × "
            f"${daily_mie:,.2f}/day (GSA M&IE ZIP {zip_for_gsa})"
        )

    # 3.6 Hertz rental car (automatic)
    rental_total = 0.0
    rental_detail = ""

    if include_rental:
        daily_rate = estimate_hertz_daily_rate(destination_airport)
        rental_total = daily_rate * trip_days
        rental_detail = (
            f"{trip_days} day(s) × ${daily_rate:,.2f}/day (Hertz SUV, "
            f"membership-adjusted estimate)"
        )

    # 3.7 Grand total
    grand_total = flights_total + hotel_total + meals_total + rental_total + other_fixed_costs

    # =========================
    # 4. Output
    # =========================

    st.markdown("---")
    st.subheader("Trip cost summary")

    if auditor_name:
        st.write(f"**Auditor(s):** {auditor_name}")
    else:
        st.write("**Auditor(s):** N/A")

    st.write(f"**Travelers:** {num_travelers}")
    st.write(f"**Route:** {departure_airport} → {destination_airport}")
    st.write(
        f"**Dates:** {departure_date.isoformat()} to {return_date.isoformat()} "
        f"({trip_days} day(s), {trip_nights} night(s))"
    )

    st.markdown("### Line items")

    # Flights
    if flight_error:
        st.error(f"Flights not included: {flight_error}")
    else:
        st.write(f"**Flights total:** ${flights_total:,.2f}")
        if flight_detail:
            st.caption(f"▪ {flight_detail}")

    # Hotel
    if hotel_error:
        st.error(f"Hotel not included: {hotel_error}")
    else:
        st.write(f"**Hotel total:** ${hotel_total:,.2f}")
        if hotel_detail:
            st.caption(f"▪ {hotel_detail}")

    # Meals
    if meals_error:
        st.error(
            f"Meals not included (treated as $0 in total): {meals_error}"
        )
    else:
        st.write(f"**Meals total:** ${meals_total:,.2f}")
        if meals_detail:
            st.caption(f"▪ {meals_detail}")

    # Rental car
    if include_rental:
        st.write(f"**Rental car total:** ${rental_total:,.2f}")
        if rental_detail:
            st.caption(f"▪ {rental_detail}")
    else:
        st.write("**Rental car total:** $0.00 (not included)")

    # Other fixed costs
    st.write(f"**Other fixed costs:** ${other_fixed_costs:,.2f}")

    st.markdown("### Grand total")
    st.success(f"**${grand_total:,.2f}**")

    # Small note about data sources
    st.caption(
        "Notes: Flights & hotels use Amadeus Production APIs where available. "
        "Meals use GSA M&IE per diem by ZIP. Hertz daily rate is a smart internal "
        "estimate adjusted for membership, market, and SUV preference."
    )
