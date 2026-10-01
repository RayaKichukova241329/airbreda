"""
features.py - feature helpers shared by training and serving.

build_training_data.py uses these to build the training table, and predict.py
and the dashboard use the same functions when they make a prediction. Computing
a feature in exactly one place is how this project avoids training-serving skew:
the model always sees features calculated the same way it was trained on.
"""

import pandas as pd

LOCAL_TZ = "Europe/Amsterdam"


def hour_of_day(ts) -> int:
    """Return the local Dutch hour (0-23) for a timestamp.

    Traffic follows local time (rush hours are at 8:00 and 17:00 in Breda, not
    in UTC), so the feature uses local time. Timestamps without a time zone are
    treated as UTC, which is what both data sources use.
    """
    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        t = t.tz_localize("UTC")
    return int(t.tz_convert(LOCAL_TZ).hour)


# The model's input columns, in order. Training (train_model.py) and serving
# (predict.py) both import this list, so they can never disagree on it.
FEATURE_COLUMNS = ["total_intensity_veh_per_hr", "hour_of_day"]