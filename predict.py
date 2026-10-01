"""
predict.py - turn traffic into a predicted NO2 value and an exceedance risk.

The dashboard imports predict() directly; there is no separate model server,
because the model is small enough to load inside the dashboard container.

Exceedance threshold: 40 ug/m3, the current EU annual limit value for NO2.
- It is an ANNUAL average limit, while this model predicts HOURLY values, so a
  predicted hour above 40 does not mean the legal limit is breached. It means
  "this hour is above the level the annual average must stay under", which is a
  useful warning signal for a single interchange.
- The EU's hourly limit (200 ug/m3) is far above anything measured at NL10240
  (the highest hourly value collected so far is about 51), so a risk based on it
  would always be close to zero and tell the user nothing.
- The WHO guideline is stricter (25 ug/m3 as a 24-hour mean). A lower threshold
  would flag more hours; 40 keeps the warning for clearly elevated hours.

Risk score: a sigmoid centred on the threshold. A prediction exactly at the
threshold gives 0.5; with steepness 0.2, ten ug/m3 below it gives about 0.12 and
ten above it about 0.88. It is a smooth way to express "how close to the
threshold", not a calibrated probability.
"""

import math
from functools import lru_cache
from pathlib import Path

import joblib
import pandas as pd

from features import FEATURE_COLUMNS

MODEL_PATH = Path(__file__).with_name("model.pkl")
EXCEEDANCE_THRESHOLD_UG_M3 = 40.0
STEEPNESS = 0.2


@lru_cache(maxsize=1)
def load_model():
    """Load model.pkl once and reuse it for every prediction."""
    return joblib.load(MODEL_PATH)


def exceedance_risk(predicted_no2: float, threshold: float = EXCEEDANCE_THRESHOLD_UG_M3,
                    steepness: float = STEEPNESS) -> float:
    """Map a predicted NO2 value to a 0-1 score, 0.5 exactly at the threshold."""
    return float(1 / (1 + math.exp(-steepness * (predicted_no2 - threshold))))


def predict(total_intensity_veh_per_hr: float, hour_of_day: int) -> dict:
    """Predict NO2 for the given total traffic and local hour (0-23)."""
    if total_intensity_veh_per_hr < 0:
        raise ValueError("total_intensity_veh_per_hr cannot be negative")
    if not 0 <= int(hour_of_day) <= 23:
        raise ValueError("hour_of_day must be between 0 and 23")

    features = pd.DataFrame([[float(total_intensity_veh_per_hr), int(hour_of_day)]],
                            columns=FEATURE_COLUMNS)
    predicted = float(load_model().predict(features)[0])
    # A concentration cannot be negative, but a straight line can extrapolate
    # below zero for inputs far outside the training data.
    predicted = max(predicted, 0.0)

    return {
        "no2_ug_m3_predicted": round(predicted, 2),
        "no2_exceedance_risk": round(exceedance_risk(predicted), 3),
    }