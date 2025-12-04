import math
from datetime import date
from typing import Optional

import streamlit as st
from amadeus import Client, ResponseError

# -------------------------------------------------------------------
# 1. PAGE SETUP
# -------------------------------------------------------------------
st.set_page_config(
    page_title="MIIP Trip Cost Calculator",
    page_icon="✈️",
    layout="centered"
)

st.title("✈️ MIIP Trip Cost Calculator")
st.write("Estimate travel costs including flights, hotel, Hertz rental, and car service.")

# -------------------------------------------------------------------
# 2. LOAD AMADEUS CLIENT (IF SECRETS ARE AVAILABLE)
# -------------------------------------------------------------------
def get_amadeus_client():
    """Initialize Amadeus only if secrets are set."""
    try:
        return Client(
            client_id=st.secrets["4iGEIhJyBuLwpkk3YV6WUvoQNL0fMHWG"],
            client_secret=st.secrets["xaE0EZasXwY6KNBD"],
            hostname="production",  # 👈 use live data
        )
    except Exception:
        return None



amadeus = get_amadeus_client()

# -------------------------------------------------------------------
# 3. UTILITIES
# -------------------------------------------------------------------
def estimate_jetblue_price(origin: str, destination: str, depart: date, return_date: date) -> Optional[float]:
    """
    Query Amadeus for average roundtrip fare.
    Shows helpful messages if something goes wrong.
    """
    # 1. Did we even create the Amadeus client?
    if not amadeus:
        st.error("Amadeus client not initialized – check API keys in Streamlit secrets.")
        return None

    try:
        response = amadeus.shopping.flight_offers_search.get(
            originLocationCode=origin,
            destinationLocationCode=destination,
            departureDate=depart.isoformat(),
            returnDate=return_date.isoformat(),
            adults=1,
            nonStop=False,
        )

        offers = response.data

        # 2. Did Amadeus return any offers at all?
        if not offers:
            st.warning("Amadeus returned no flight offers for this route and dates.")
            return None

        total_prices = []
        for offer in offers:
            try:
                total_prices.append(float(offer["price"]["grandTotal"]))
            except Exception:
                continue

        # 3. Offers but no usable price field
        if not total_prices:
            st.warning("Amadeus returned offers but no usable prices.")
            return None

        return round(sum(total_prices) / len(total_prices), 2)

    except ResponseError as e:
        # 4. API returned an error (bad key, quota, etc)
        st.error(f"Amadeus API error: {e}")
        return None



# -------------------------------------------------------------------
# 4. INPUT SECTIONS
# -------------------------------------------------------------------
st.header("1. Traveler & Trip Basics")

traveler = st.text_input("Traveler Name (optional):", "")
num_travelers = st.number_input("Number of Travelers", min_value=1, value=1)

origin = st.selectbox(
    "Departure Airport",
    ["BOS", "MHT"],
    index=0
)
destination = "TPA"   # Fixed

depart_date = st.date_input("Departure Date")
return_date = st.date_input("Return Date")

trip_nights = (return_date - depart_date).days
if trip_nights < 1:
    st.warning("Return date must be after departure date.")
    st.stop()


# -------------------------------------------------------------------
# 5. CAR SERVICE (AXIS COACH)
# -------------------------------------------------------------------
st.header("2. Car Service (Axis Coach)")

car_service_rate = st.number_input("Car Service One-Way Cost", min_value=0.0, value=160.0)
car_roundtrip_cost = car_service_rate * 2


# -------------------------------------------------------------------
# 6. FLIGHTS
# -------------------------------------------------------------------
st.header("3. Flights (JetBlue)")

flight_pricing_mode = st.radio(
    "How should we calculate flights?",
    ["Use Amadeus average (JetBlue)", "Enter manually"],
)

if flight_pricing_mode == "Use Amadeus average (JetBlue)":
    st.info("Will query Amadeus for average fare…")

    avg_fare = estimate_jetblue_price(origin, destination, depart_date, return_date)

    if avg_fare:
        st.success(f"Estimated JetBlue average roundtrip fare: **${avg_fare}**")
        flight_cost_per_person = avg_fare
    else:
        st.error("Unable to retrieve JetBlue pricing. Please enter flight cost manually.")
        flight_cost_per_person = st.number_input("Manual Flight Cost Per Person", min_value=0.0, value=0.0)
else:
    flight_cost_per_person = st.number_input("Manual Flight Cost Per Person", min_value=0.0, value=0.0)

total_flight_cost = flight_cost_per_person * num_travelers


# -------------------------------------------------------------------
# 7. HOTEL (MARRIOTT)
# -------------------------------------------------------------------
st.header("4. Hotel (Marriott Preferred)")

hotel_rate = st.number_input("Marriott Nightly Rate ($)", min_value=0.0, value=180.0)
hotel_cost = hotel_rate * trip_nights * num_travelers  # each traveler gets their own room


# -------------------------------------------------------------------
# 8. RENTAL CAR (HERTZ)
# -------------------------------------------------------------------
st.header("5. Hertz Rental Car")

rental_rate = st.number_input("Hertz Daily Rate", min_value=0.0, value=55.0)
rental_days = trip_nights
rental_cost = rental_rate * rental_days


# -------------------------------------------------------------------
# 9. ADD-INS
# -------------------------------------------------------------------
st.header("6. Additional Expenses")

misc_cost = st.number_input("Miscellaneous Add-ins ($)", min_value=0.0, value=0.0)
per_diem = st.number_input("Per Diem Per Day ($)", min_value=0.0, value=0.0)
per_diem_total = per_diem * num_travelers * trip_nights


# -------------------------------------------------------------------
# 10. CALCULATE TOTAL
# -------------------------------------------------------------------
st.header("7. Final Calculation")

if st.button("💰 Calculate Trip Cost"):
    total_cost = (
        car_roundtrip_cost
        + total_flight_cost
        + hotel_cost
        + rental_cost
        + misc_cost
        + per_diem_total
    )

    st.subheader("Trip Summary")
    st.write(f"**Traveler:** {traveler if traveler else 'N/A'}")
    st.write(f"**Travelers:** {num_travelers}")
    st.write(f"**Travel Dates:** {depart_date} → {return_date}")
    st.write(f"**Trip Nights:** {trip_nights}")

    st.subheader("Cost Breakdown")
    st.write(f"Car Service (RT): **${car_roundtrip_cost}**")
    st.write(f"Flights Total: **${total_flight_cost}**")
    st.write(f"Hotel Total: **${hotel_cost}**")
    st.write(f"Hertz Total: **${rental_cost}**")
    st.write(f"Misc Add-ins: **${misc_cost}**")
    st.write(f"Per Diem Total: **${per_diem_total}**")

    st.success(f"### **Grand Total: ${total_cost:,.2f}**")




