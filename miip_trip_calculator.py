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

# Expanded airport list (IATA codes)
# User asked to add: BWI, SLC, Dallas, Houston, Austin, FFL, AID, Chicago, DCA, MSY, SDF
# Interpreting city names / typos as common IATA:
# Dallas -> DFW, Houston -> IAH, Austin -> AUS, Chicago -> ORD, FFL -> FLL, AID -> IAD
US_AIRPORTS = {
    "BOS","MHT","JFK","LGA","EWR","PHL","DCA","IAD","BWI","CLT","ATL","MCO","TPA",
    "MIA","FLL","ORD","MDW","DFW","DAL","IAH","HOU","DEN","PHX","LAS","LAX","SFO","SEA",
    "HNL","OGG","LIH","KOA",
    "SLC","MSY","SDF","AUS"
}

# Hotel estimates (nightly)
HOTEL_BASE_RATE_BY_AIRPORT = {
    "BOS": 260.0, "JFK": 280.0, "LGA": 270.0, "EWR": 260.0,
    "LAX": 260.0, "SFO": 280.0, "SEA": 250.0, "DEN": 210.0,
    "MCO": 210.0, "TPA": 215.0, "MIA": 260.0, "CLT": 190.0,
    "PHL": 210.0, "ORD": 230.0, "ATL": 210.0,
    # Added/adjusted per request
    "BWI": 195.0,
    "SLC": 200.0,
    "DFW": 195.0,   # Dallas
    "IAH": 190.0,   # Houston
    "AUS": 210.0,   # Austin
    "FLL": 230.0,   # Fort Lauderdale
    "IAD": 210.0,   # (user typed AID)
    "DCA": 240.0,
    "MSY": 200.0,   # New Orleans
    "SDF": 175.0,   # Louisville
}
DEFAULT_HOTEL_NIGHTLY_RATE = 190.0

# Hertz base daily (before SUV uplift + membership discount)
HERTZ_BASE_DAILY_BY_AIRPORT = {
    "BOS": 70.0, "MHT": 60.0, "JFK": 75.0, "LGA": 75.0, "EWR": 72.0,
    "TPA": 65.0, "MCO": 65.0, "MIA": 70.0, "DEN": 68.0,
    "SFO": 78.0, "LAX": 78.0, "SEA": 72.0,
    # Added/adjusted per request
    "BWI": 62.0,
    "SLC": 64.0,
    "DFW": 60.0,
    "IAH": 60.0,
    "AUS": 62.0,
    "FLL": 66.0,
    "IAD": 64.0,
    "DCA": 66.0,
    "MSY": 60.0,
    "SDF": 55.0,
}
HERTZ_SUV_UPLIFT = 0.15
HERTZ_MEMBERSHIP_DISCOUNT = 0.12

MEALS_PER_DAY = 100.0
CONTINGENCY_RATE = 0.075  # 7.5%

# Fixed add-ons (always included)
GAS_COST = 60.0
TOLLS_COST = 35.0
PARKING_COST = 50.0
AIRPORT_SHUTTLE_TIPS = 10.0
HOUSEKEEPING_PER_NIGHT = 10.0  # per night per traveler

# Car service contract rates (BOS = Logan, MHT = Manchester)
# Contract is one-way.
CAR_SERVICE_RATES_ONE_WAY = {
    "BOS": {  # Logan -> Nashua/Methuen/Lawrence
        "1-3": 161.76,   # Lincoln MKT/Aviator
        "4-5": 229.12,   # SUV
        "6-14": 295.00,  # Transit
    },
    "MHT": {  # Manchester -> Nashua/Methuen/Lawrence
        "1-3": 97.19,
        "4-5": 184.76,
        "6-14": 228.54,
    },
}
CAR_SERVICE_HOLIDAY_SURCHARGE = 25.00
CAR_SERVICE_CITIES = ["Nashua, NH", "Methuen, MA", "Lawrence, MA"]

# ---------------------------------------------------------
# Holiday helpers (true-date holidays, not observed)
# ---------------------------------------------------------

def nth_weekday_of_month(year: int, month: int, weekday: int, n: int) -> dt.date:
    first = dt.date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    day = 1 + offset + 7 * (n - 1)
    return dt.date(year, month, day)

def last_weekday_of_month(year: int, month: int, weekday: int) -> dt.date:
    if month == 12:
        next_month = dt.date(year + 1, 1, 1)
    else:
        next_month = dt.date(year, month + 1, 1)
    last_day = next_month - dt.timedelta(days=1)
    offset = (last_day.weekday() - weekday) % 7
    return last_day - dt.timedelta(days=offset)

def us_holidays_for_year(year: int) -> set:
    new_years = dt.date(year, 1, 1)
    juneteenth = dt.date(year, 6, 19)
    independence = dt.date(year, 7, 4)
    veterans = dt.date(year, 11, 11)
    christmas = dt.date(year, 12, 25)

    mlk = nth_weekday_of_month(year, 1, weekday=0, n=3)
    presidents = nth_weekday_of_month(year, 2, weekday=0, n=3)
    memorial = last_weekday_of_month(year, 5, weekday=0)
    labor = nth_weekday_of_month(year, 9, weekday=0, n=1)
    columbus = nth_weekday_of_month(year, 10, weekday=0, n=2)
    thanksgiving = nth_weekday_of_month(year, 11, weekday=3, n=4)

    return {
        new_years, mlk, presidents, memorial, juneteenth, independence,
        labor, columbus, veterans, thanksgiving, christmas
    }

def is_holiday(date_obj: dt.date) -> bool:
    return date_obj in us_holidays_for_year(date_obj.year)

def holiday_name(date_obj: dt.date) -> Optional[str]:
    y = date_obj.year
    if date_obj == dt.date(y, 1, 1):
        return "New Year's Day"
    if date_obj == dt.date(y, 6, 19):
        return "Juneteenth"
    if date_obj == dt.date(y, 7, 4):
        return "Independence Day"
    if date_obj == dt.date(y, 11, 11):
        return "Veterans Day"
    if date_obj == dt.date(y, 12, 25):
        return "Christmas Day"
    if date_obj == nth_weekday_of_month(y, 1, weekday=0, n=3):
        return "MLK Day"
    if date_obj == nth_weekday_of_month(y, 2, weekday=0, n=3):
        return "Presidents Day"
    if date_obj == last_weekday_of_month(y, 5, weekday=0):
        return "Memorial Day"
    if date_obj == nth_weekday_of_month(y, 9, weekday=0, n=1):
        return "Labor Day"
    if date_obj == nth_weekday_of_month(y, 10, weekday=0, n=2):
        return "Columbus Day"
    if date_obj == nth_weekday_of_month(y, 11, weekday=3, n=4):
        return "Thanksgiving"
    return None

# ---------------------------------------------------------
# Amadeus + pricing helpers
# ---------------------------------------------------------

def try_get_amadeus_client() -> Tuple[Optional[Client], Optional[str]]:
    try:
        if "amadeus" not in st.secrets:
            return None, "Missing [amadeus] section in Streamlit secrets."
        cfg = st.secrets["amadeus"]

        client_id = cfg.get("client_id")
        client_secret = cfg.get("client_secret")
        hostname = cfg.get("hostname", "production")

        if not client_id or not client_secret:
            return None, "Missing amadeus.client_id or amadeus.client_secret in Streamlit secrets."

        client = Client(client_id=client_id, client_secret=client_secret, hostname=hostname)
        return client, None
    except Exception as exc:
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
# Car service helpers
# ---------------------------------------------------------

def car_service_vehicle_tier(travelers: int) -> Optional[str]:
    if 1 <= travelers <= 3:
        return "1-3"
    if 4 <= travelers <= 5:
        return "4-5"
    if 6 <= travelers <= 14:
        return "6-14"
    return None

def estimate_car_service_total(
    departure_airport: str,
    travelers: int,
    include: bool,
    city_choice: Optional[str],
    dep_date: dt.date,
    ret_date: dt.date,
    individual_return_home: bool,
) -> Tuple[float, str, float, float, float, bool, bool, str]:
    """
    - Base contract is one-way.
    - Outbound leg (home -> airport):
        Uses tier based on total travelers (group transport).
    - Return leg (airport -> home):
        If individual_return_home=True, each traveler gets their own service home:
            return_cost = one_way_rate_for_1_3 * travelers
        Else group transport back:
            return_cost = one_way_rate_for_group
    - Holiday surcharge applies if EITHER dep_date or ret_date is a holiday, applied once.
    Returns:
      (total, outbound_tier, outbound_one_way, return_one_way, return_total, dep_is_holiday, ret_is_holiday, return_tier)
    """
    if not include:
        return 0.0, "n/a", 0.0, 0.0, 0.0, False, False, "n/a"

    airport = departure_airport.upper()
    if airport not in CAR_SERVICE_RATES_ONE_WAY:
        return 0.0, "unsupported-airport", 0.0, 0.0, 0.0, False, False, "n/a"

    _ = city_choice  # for audit clarity only

    outbound_tier = car_service_vehicle_tier(travelers)
    if outbound_tier is None:
        return 0.0, "unsupported", 0.0, 0.0, 0.0, False, False, "n/a"

    outbound_one_way = float(CAR_SERVICE_RATES_ONE_WAY[airport][outbound_tier])

    if individual_return_home:
        # each traveler returns solo (assume "1-3" tier per person)
        return_tier = "1-3"
        return_one_way = float(CAR_SERVICE_RATES_ONE_WAY[airport][return_tier])
        return_total = round(return_one_way * travelers, 2)
    else:
        # group returns together using same tier as outbound
        return_tier = outbound_tier
        return_one_way = float(CAR_SERVICE_RATES_ONE_WAY[airport][return_tier])
        return_total = round(return_one_way, 2)

    base_total = round(outbound_one_way + return_total, 2)

    dep_h = is_holiday(dep_date)
    ret_h = is_holiday(ret_date)
    holiday_fee = CAR_SERVICE_HOLIDAY_SURCHARGE if (dep_h or ret_h) else 0.0

    total = round(base_total + holiday_fee, 2)
    return total, outbound_tier, outbound_one_way, return_one_way, return_total, dep_h, ret_h, return_tier

# ---------------------------------------------------------
# Inputs
# ---------------------------------------------------------

l, r = st.columns(2)

with l:
    st.markdown('<div class="miip-section-title">Traveler & flights</div>', unsafe_allow_html=True)
    travelers = st.number_input("Number of travelers", min_value=1, value=1, step=1, help="One room per traveler")

    # Expand departure options, but note: car service contract only supports BOS/MHT
    dep_airport = st.selectbox(
        "Departure airport",
        ["BOS", "MHT", "BWI", "SLC", "DFW", "IAH", "AUS", "FLL", "IAD", "ORD", "DCA", "MSY", "SDF"],
    )

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

    st.write("")

    include_car_service = st.checkbox("Include car service", value=False)
    car_service_city = None
    individual_return_home = False

    if include_car_service:
        car_service_city = st.selectbox("Car service area", CAR_SERVICE_CITIES)
        if travelers >= 2:
            individual_return_home = st.checkbox("Individual return home", value=False)

# ---------------------------------------------------------
# Calculations
# ---------------------------------------------------------

trip_days = (ret_date - dep_date).days + 1
trip_nights = max(trip_days - 1, 0)

st.markdown('<div class="miip-section-title">Flights</div>', unsafe_allow_html=True)
mode = st.radio("", ["Auto calculate", "Enter manually"])

flight_pp = 0.0

if mode == "Enter manually":
    flight_pp = st.number_input("Manual flight cost per traveler", min_value=0.0, value=0.0, step=50.0)
else:
    if len(dest_airport) != 3:
        st.warning("Enter a valid 3-letter destination airport code to auto-calculate flights.")
    else:
        amadeus_client, amadeus_err = try_get_amadeus_client()
        if amadeus_client is None:
            st.warning(f"Auto-calculate unavailable: {amadeus_err} Switch to manual flight entry.")
        else:
            avg_price, status = avg_flight_cost(amadeus_client, dep_airport, dest_airport, dep_date, ret_date, preferred_airline)
            if status == "error":
                st.error("Amadeus flight search failed. You can switch to manual entry.")
            elif status == "none" or avg_price is None:
                st.error("Amadeus returned no offers for this route/dates. You can enter flights manually.")
            elif status == "preferred":
                flight_pp = avg_price
                st.caption(f"Estimated average round-trip fare per traveler for **{preferred_airline}**: **${flight_pp:,.0f}**.")
            else:
                flight_pp = avg_price
                st.warning(
                    f"No usable prices found for the preferred airline only; using average of available airlines instead. "
                    f"Average used: **${flight_pp:,.0f}**."
                )

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

# Fixed incidentals
housekeeping_total = HOUSEKEEPING_PER_NIGHT * trip_nights * travelers
fixed_incidentals_total = GAS_COST + TOLLS_COST + PARKING_COST + AIRPORT_SHUTTLE_TIPS + housekeeping_total

# Car service
(
    car_service_total,
    car_outbound_tier,
    car_outbound_one_way,
    car_return_one_way,
    car_return_total,
    dep_holiday,
    ret_holiday,
    car_return_tier,
) = estimate_car_service_total(
    departure_airport=dep_airport,
    travelers=travelers,
    include=include_car_service,
    city_choice=car_service_city,
    dep_date=dep_date,
    ret_date=ret_date,
    individual_return_home=individual_return_home,
)

if include_car_service and car_outbound_tier == "unsupported-airport":
    st.error("Car service pricing is only available for BOS or MHT (per contract).")
if include_car_service and car_outbound_tier == "unsupported":
    st.error("Car service supports up to 14 passengers. Please contact dispatch for a custom quote.")

# Holiday surcharge amount (applied once if either date is a holiday)
car_holiday_fee = CAR_SERVICE_HOLIDAY_SURCHARGE if (dep_holiday or ret_holiday) else 0.0

subtotal = (
    flights_total
    + bags_total
    + hotel_total
    + meals_total
    + rental_total
    + fixed_incidentals_total
    + car_service_total
    + other_fixed
)

contingency = subtotal * CONTINGENCY_RATE
grand_total = subtotal + contingency

# ---------------------------------------------------------
# Summary (summarized rollups)
# ---------------------------------------------------------

st.markdown('<div class="miip-section-title">Trip cost summary</div>', unsafe_allow_html=True)

st.write(f"- Flights total: **${flights_total:,.0f}**")
st.write(f"- Checked bags total: **${bags_total:,.0f}**")
st.write(f"- Hotel total: **${hotel_total:,.0f}**")
st.write(f"- Meals total: **${meals_total:,.0f}**")
st.write(f"- Rental car total: **${rental_total:,.0f}**")
st.write(f"- Fixed incidentals total: **${fixed_incidentals_total:,.0f}**")

if include_car_service:
    st.write(f"- Car service total: **${car_service_total:,.0f}**")

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

    st.markdown("**Car service (home ↔ airport)**")
    if include_car_service:
        st.markdown(f"- Service area = `{car_service_city}`")
        st.markdown(f"- Outbound tier (group) = `{car_outbound_tier}`")
        st.markdown(f"- Outbound one-way (home → airport) = `${car_outbound_one_way:,.2f}`")

        if individual_return_home and travelers >= 2:
            st.markdown(f"- Return mode = `Individual return home`")
            st.markdown(f"- Return tier (per traveler) = `{car_return_tier}`")
            st.markdown(f"- Return one-way (airport → home, per traveler) = `${car_return_one_way:,.2f}`")
            st.markdown(f"- Return total = `${car_return_one_way:,.2f} × {travelers}` = `${car_return_total:,.2f}`")
        else:
            st.markdown(f"- Return mode = `Group return`")
            st.markdown(f"- Return tier (group) = `{car_return_tier}`")
            st.markdown(f"- Return one-way (airport → home) = `${car_return_one_way:,.2f}`")
            st.markdown(f"- Return total = `${car_return_total:,.2f}`")

        st.markdown(f"- Base (outbound + return) = `${car_outbound_one_way:,.2f} + ${car_return_total:,.2f}` = `${(car_outbound_one_way + car_return_total):,.2f}`")

        # Only show holiday surcharge if applied
        if car_holiday_fee > 0:
            dep_name = holiday_name(dep_date) if dep_holiday else None
            ret_name = holiday_name(ret_date) if ret_holiday else None

            if dep_name and ret_name:
                holiday_label = f"{dep_name} (departure) / {ret_name} (return)"
            elif dep_name:
                holiday_label = f"{dep_name} (departure)"
            elif ret_name:
                holiday_label = f"{ret_name} (return)"
            else:
                holiday_label = "Holiday"

            st.markdown(f"- Holiday surcharge ({holiday_label}) = `${car_holiday_fee:,.2f}`")

        st.markdown(f"- Car service total = `${car_service_total:,.2f}`")
    else:
        st.markdown("- Car service excluded")

    st.markdown("**Other fixed costs**")
    st.markdown(f"- Other fixed costs entered = `${other_fixed:,.2f}`")

    st.markdown("**Totals**")
    st.markdown(f"- Subtotal = `${subtotal:,.2f}`")
    st.markdown(f"- Contingency (7.5%) = `${subtotal:,.2f} × 0.075` = `${contingency:,.2f}`")
    st.markdown(f"- Final total = `${subtotal:,.2f} + ${contingency:,.2f}` = `${grand_total:,.2f}`")

    st.markdown("</div>", unsafe_allow_html=True)
