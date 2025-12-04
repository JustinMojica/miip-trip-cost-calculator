import math
from datetime import date
from typing import Optional

import streamlit as st
from amadeus import Client, ResponseError


# -----------------------------------------------------------------------------
# Amadeus client
# -----------------------------------------------------------------------------
def get_amadeus_client() -> Optional[Client]:
    """
    Initialize the Amadeus client in PRODUCTION mode using Streamlit secrets.

    You MUST define these in Streamlit Cloud:
        AMADEUS_CLIENT_ID
        AMADEUS_CLIENT_SECRET
    """
    try:
        client_id = st.secrets["AMADEUS_CLIENT_ID"]
        client_secret = st.secrets["AMADEUS_CLIENT_SECRET"]
    except Exception as e:
        st.sidebar.error(
            "Amadeus secrets not found. "
            "Set AMADEUS_CLIENT_ID and AMADEUS_CLIENT_SECRET in Streamlit Secrets."
        )
        return None

    try:
        return Client(
            client_id=client_id,
            client_secret=client_secret,
            hostname="production",  # use live production data
        )
    except Exception as e:
        st.sidebar.error(f"Error initializing Amadeus client: {e}")
        return None


amadeus: Optional[Client] = get_amadeus_client()


# -----------------------------------------------------------------------------
# Amadeus helper: estimate JetBlue fare
# -----------------------------------------------------------------------------
def estimate_jetblue_price(
    origin: str,
    destination: str,
    depart: date,
    return_date: date,
) -> Optional[float]:
    """
    Query Amadeus PRODUCTION for an average JetBlue (B6) roundtrip fare in USD.

    Returns:
        float: average price per person, or None if the API fails.
    """
    if not amadeus:
        st.error("Amadeus client not initialized – check Streamlit secrets.")
        return None

    # Basic sanity checks that can also trigger 400s
    if origin == destination:
        st.error("Origin and destination airports must be different.")
        return None

    if depart > return_date:
        st.error("Departure date must be on or before return date.")
        return None

    try:
        response = amadeus.shopping.flight_offers_search.get(
            originLocationCode=origin,
            destinationLocationCode=destination,
            departureDate=depart.isoformat(),
            returnDate=return_date.isoformat(),
            adults=1,
            currencyCode="USD",
            max=20,
            sources="GDS",  # required in production for live GDS fares
        )

        offers = response.data

        if not
