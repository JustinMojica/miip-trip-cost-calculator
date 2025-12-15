import datetime as dt
from statistics import mean
from typing import Optional

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
DOMESTIC_BAG_FEE_BY_AIRLINE = {"Southwest": 0, "JetBlue": 70, "Delta": 70, "American": 70}

US_AIRPORTS = {
    "BOS","MHT","JFK","LGA","EWR","PHL","DCA","IAD","BWI","CLT","ATL","MCO","TPA",
    "MIA","FLL","ORD","MDW","DFW","DAL","IAH","HOU","DEN","PHX","LAS","LAX","SFO","SEA"
}

HOTEL_BASE_RATE_BY_AIRPORT = {
    "BOS":260,"JFK":280,"LGA":270,"EWR":260,"LAX":260,"SFO":280,"SEA":250,"DEN":210,
    "MCO":210,"TPA":215,"MIA":260,"CLT":190,"PHL":210,"ORD":230,"ATL":210
}
DEFAULT_HOTEL_NIGHTLY_RATE = 190

HERTZ_BASE_DAILY_BY_AIRPORT = {
    "BOS":70,"MHT":60,"JFK":75,"LGA":75,"EWR":72,"TPA":65,"MCO":65,
    "MIA":70,"DEN":68,"SFO":78,"LAX":78,"SEA":72
}
HERTZ_SUV_UPLIFT = 0.15
HERTZ_MEMBERSHIP_DISCOUNT = 0.12

MEALS_PER_DAY = 100
CONTINGENCY_RATE = 0.05

# Fixed incidentals
GAS_COST = 60
TOLLS_COST = 35
PARKING_COST = 50
AIRPORT_TIP = 10
HOUSEKEEPING_PER_NIGHT = 10

# ---------------------------------------------------------
# Helpers
# ---------------------------------------------------------

def get_amadeus_client():
    try:
        cfg = st.secrets["amadeus"]
        return Client(cfg["client_id"], cfg["client_secret"], hostname=cfg.get("hostname","production"))
    except Exception:
        st.error("Amadeus configuration missing.")
        return None

def is_domestic(o,d): 
    return o in US_AIRPORTS and d in US_AIRPORTS

def hotel_rate(d): 
    return HOTEL_BASE_RATE_BY_AIRPORT.get(d, DEFAULT_HOTEL_NIGHTLY_RATE)

def hertz_rate(d): 
    base = HERTZ_BASE_DAILY_BY_AIRPORT.get(d,80)
    return round(base*(1+HERTZ_SUV_UPLIFT)*(1-HERTZ_MEMBERSHIP_DISCOUNT),2)

def avg_flight_cost(client,o,d,dep,ret):
    try:
        r = client.shopping.flight_offers_search.get(
            originLocationCode=o,
            destinationLocationCode=d,
            departureDate=dep.isoformat(),
            returnDate=ret.isoformat(),
            adults=1,
            currencyCode="USD",
            max=20
        )
    except ResponseError:
        st.error("Amadeus flight search failed.")
        return None

    prices = [float(x["price"]["grandTotal"]) for x in r.data if "price" in x]
    return round(mean(prices),2) if prices else None

# ---------------------------------------------------------
# Inputs
# ---------------------------------------------------------

l, r = st.columns(2)

with l:
    st.markdown('<div class="miip-section-title">Traveler & flights</div>', unsafe_allow_html=True)
    travelers = st.number_input("Number of travelers", 1, step=1, help="One room per traveler")
    dep_airport = st.selectbox("Departure airport", ["BOS","MHT"])
    airline = st.selectbox("Preferred airline", list(AIRLINE_CODES))
    dest_airport = st.text_input("Destination airport", help="3-letter IATA code").strip().upper()

with r:
    st.markdown('<div class="miip-section-title">Client & hotel options</div>', unsafe_allow_html=True)
    assignment = st.text_input("Assignment city and state", help="City, State")
    hotel_brand = st.selectbox("Preferred hotel brand", ["Marriott","Hilton","Wyndham"])

d, g = st.columns(2)
today = dt.date.today()

with d:
    st.markdown('<div class="miip-section-title">Dates</div>', unsafe_allow_html=True)
    dep_date = st.date_input("Departure date", today, format="MM/DD/YYYY")
    ret_date = st.date_input(
        "Return date",
        dep_date + dt.timedelta(days=1),
        min_value=dep_date + dt.timedelta(days=1),
        format="MM/DD/YYYY"
    )

with g:
    st.markdown('<div class="miip-section-title">Ground costs</div>', unsafe_allow_html=True)
    include_rental = st.checkbox("Include Hertz rental SUV", True)
    other_fixed = st.number_input("Other fixed costs", 0.0, step=50.0)

# ---------------------------------------------------------
# Calculations
# ---------------------------------------------------------

trip_days = (ret_date - dep_date).days + 1
trip_nights = trip_days - 1

st.markdown('<div class="miip-section-title">Flights</div>', unsafe_allow_html=True)
mode = st.radio("", ["Auto calculate","Enter manually"])
flight_pp = 0

if mode == "Enter manually":
    flight_pp = st.number_input("Manual flight cost per traveler", 0.0, step=50.0)
else:
    if len(dest_airport) == 3:
        client = get_amadeus_client()
        if client:
            v = avg_flight_cost(client, dep_airport, dest_airport, dep_date, ret_date)
            if v:
                flight_pp = v

flights_total = flight_pp * travelers
bags_total = (DOMESTIC_BAG_FEE_BY_AIRLINE.get(airline,70) if is_domestic(dep_airport,dest_airport) else 0) * travelers
hotel_total = hotel_rate(dest_airport) * trip_nights * travelers
meals_total = MEALS_PER_DAY * trip_days * travelers
rental_total = hertz_rate(dest_airport) * trip_days if include_rental else 0

housekeeping_total = HOUSEKEEPING_PER_NIGHT * trip_nights * travelers
fixed_incidentals = GAS_COST + TOLLS_COST + PARKING_COST + AIRPORT_TIP + housekeeping_total

subtotal = (
    flights_total + bags_total + hotel_total +
    meals_total + rental_total + fixed_incidentals + other_fixed
)

contingency = subtotal * CONTINGENCY_RATE
grand_total = subtotal + contingency

# ---------------------------------------------------------
# Summary
# ---------------------------------------------------------

st.markdown('<div class="miip-section-title">Trip cost summary</div>', unsafe_allow_html=True)

st.write(f"- Flights total: **${flights_total:,.0f}**")
st.write(f"- Checked bags total: **${bags_total:,.0f}**")
st.write(f"- Hotel total: **${hotel_total:,.0f}**")
st.write(f"- Meals total: **${meals_total:,.0f}**")
st.write(f"- Rental car total: **${rental_total:,.0f}**")
st.write(f"- Fixed incidentals total: **${fixed_incidentals:,.0f}**")
st.write(f"- Other fixed costs: **${other_fixed:,.0f}**")

st.success(f"Grand total: ${grand_total:,.0f}")

# ---------------------------------------------------------
# Geek math (FULL breakdown)
# ---------------------------------------------------------

with st.expander("Show detailed cost math", expanded=False):
    st.markdown('<div class="miip-geek-math">', unsafe_allow_html=True)

    st.markdown("**Trip length**")
    st.markdown(f"- Trip days = `{trip_days}`")
    st.markdown(f"- Trip nights = `{trip_nights}`")

    st.markdown("**Flights**")
    st.markdown(f"- Flight per traveler = ${flight_pp:,.2f}")
    st.markdown(f"- Flights total = ${flight_pp:,.2f} × {travelers} = ${flights_total:,.2f}")

    st.markdown("**Checked bags**")
    st.markdown(f"- Bags total = ${bags_total:,.2f}")

    st.markdown("**Hotel**")
    st.markdown(f"- Nightly rate = ${hotel_rate(dest_airport):,.2f}")
    st.markdown(f"- Hotel total = ${hotel_total:,.2f}")

    st.markdown("**Meals**")
    st.markdown(f"- Meals = $100 × {trip_days} × {travelers} = ${meals_total:,.2f}")

    st.markdown("**Rental car**")
    st.markdown(f"- Rental total = ${rental_total:,.2f}")

    st.markdown("**Fixed incidentals**")
    st.markdown(f"- Gas = $60")
    st.markdown(f"- Tolls = $35")
    st.markdown(f"- Parking = $50")
    st.markdown(f"- Airport shuttle tips = $10")
    st.markdown(f"- Housekeeping = $10 × {trip_nights} × {travelers} = ${housekeeping_total:,.2f}")
    st.markdown(f"- Fixed incidentals total = ${fixed_incidentals:,.2f}")

    st.markdown("**Totals**")
    st.markdown(f"- Subtotal = ${subtotal:,.2f}")
    st.markdown(f"- Contingency (5%) = ${contingency:,.2f}")
    st.markdown(f"- Final total = ${grand_total:,.2f}")

    st.markdown('</div>', unsafe_allow_html=True)
