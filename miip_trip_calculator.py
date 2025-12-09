import datetime as dt
from statistics import mean
from typing import Optional

import streamlit as st
from amadeus import Client, ResponseError

# ---------------------------------------------------------
# Config & styling
# ---------------------------------------------------------

st.set_page_config(
    page_title="MIIP Trip Expense Calculator",
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
    /* Keep the structural divs but visually remove the "boxes" */
    .miip-section-card {
        padding: 0 0 0.75rem 0;
        border: none;
        background-color: transparent;
        margin-bottom: 0.4rem;
    }
    .miip-section-title {
        font-size: 1.08rem;
        font-weight: 600;
        margin-bottom: 0.2rem;
    }
    .miip-section-caption {
        font-size: 0.85rem;
        color: #a0a0a0;
        margin-bottom: 0.15rem;
    }
    .miip-microcopy {
        font-size: 0.8rem;
        color: #8a8a8a;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

st.markdown('<div class="miip-title">MIIP Trip Expense Calculator</div>', unsafe_allow_html=True)
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
