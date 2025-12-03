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
            client_id=st.secrets["AMADEUS_CLIENT_ID"],
            client_secret=st.secrets["AMADEUS_CLIENT_SECRET"]
        )
    except Exception:
        return None


amadeus = get_amadeus_client()

# -------------------------------------------------------------------
# 3. UTILITIES
# -------------------------------------------------------------------
def estimate_jetblue_price(origin: str, destination: str, depart: date, return_date: date) -> Optional[float]:
    """
    Query Amadeus for average JetBlue roundtrip fare.
    If unavailable, return None and the user can enter manually.
    """
    if not amadeus:
        return None

    try:
        response = amadeus.shopping.flight_offers_search.get(
            originLocationCode=origin,
            destinationLocationCode=destination,
            departureDate=depart.isoformat(),
            returnDate=return_date.isoformat(),
            adults=1,
            nonStop=False
        )

        offers = response.data
        if not offers:
            return None

        total_prices = []
        for offer in offers:
            try:
                price = float(offer["price"]["grandTotal"])
                total_prices.append(price)
            except Exception:
                continue

        if not total_prices:
            return None

        return round(sum(total_prices) / len(total_prices), 2)

    except ResponseError:
        return None


# -------------------------------------------------------------------
# 4. INPUT SECTIONS
# -------------------------------------------------------------------
st.header("1. Traveler & Trip Basics")

traveler = st.text_input("Traveler Name (optional):", "")
num_travelers = st.number_input("Number of Travelers", min_value=1, value=1)

origin = st.sele
