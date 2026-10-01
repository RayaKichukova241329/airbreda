"""
train_model.py - train the AirBreda NO2 regression on training_data.csv.

Model: a plain linear regression on two features (total traffic intensity and
local hour of day). With a few dozen rows at most, this is the right amount of
model: anything more flexible would fit noise, not traffic (see ADR-006).

Evaluation: with so few rows, a train/test split would leave one or two test
points, which says nothing. Instead the script reports two numbers:
- in-sample R2 and MAE: how well the line fits the data it was trained on.
  This is always optimistic, and with few rows it is close to meaningless.
- leave-one-out MAE: each row is predicted by a model trained on all OTHER rows.
  This is the more honest estimate of how far off a new prediction will be.

Outputs: model.pkl (the trained model) and model_metrics.json (the numbers
above plus the coefficients), both used in ADR-006.
"""

import json
import logging
from datetime import datetime, timezone

import joblib
import pandas as pd
import sklearn
from sklearn.linear_model import LinearRegression
from sklearn.metrics import mean_absolute_error, r2_score
from sklearn.model_selection import LeaveOneOut, cross_val_predict

from common import log_event, setup_logging
from features import FEATURE_COLUMNS

TRAINING_DATA = "training_data.csv"
TARGET = "no2_ug_m3"
MODEL_PATH = "model.pkl"
METRICS_PATH = "model_metrics.json"
MIN_ROWS = 4  # an intercept plus two coefficients needs at least this many rows


def main() -> None:
    df = pd.read_csv(TRAINING_DATA)
    if len(df) < MIN_ROWS:
        raise SystemExit(f"Only {len(df)} rows in {TRAINING_DATA}; need at least {MIN_ROWS}.")

    X, y = df[FEATURE_COLUMNS], df[TARGET]
    model = LinearRegression().fit(X, y)
    fitted = model.predict(X)
    held_out = cross_val_predict(LinearRegression(), X, y, cv=LeaveOneOut())

    metrics = {
        "trained_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "rows": len(df),
        "first_hour": df["hour_start"].min(),
        "last_hour": df["hour_start"].max(),
        "features": FEATURE_COLUMNS,
        "r2_in_sample": round(float(r2_score(y, fitted)), 3),
        "mae_in_sample": round(float(mean_absolute_error(y, fitted)), 2),
        "mae_leave_one_out": round(float(mean_absolute_error(y, held_out)), 2),
        "intercept": round(float(model.intercept_), 4),
        "coefficients": {f: round(float(c), 6) for f, c in zip(FEATURE_COLUMNS, model.coef_)},
        "sklearn_version": sklearn.__version__,
    }

    joblib.dump(model, MODEL_PATH)
    with open(METRICS_PATH, "w") as f:
        json.dump(metrics, f, indent=2)
    log_event(logging.INFO, "model_trained", **metrics)


if __name__ == "__main__":
    setup_logging()
    main()