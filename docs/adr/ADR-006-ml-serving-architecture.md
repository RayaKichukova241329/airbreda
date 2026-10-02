# ADR-006: ML Serving Architecture

**Status:** Accepted
**Date:** 2026-10-01 (updated 2026-10-02 after the final training)

## Context

The dashboard must turn current traffic into a predicted NO₂ value and an exceedance risk. Training data is whatever the pipeline has collected: `build_training_data.py` joins clean NO₂ rows from the database with the hourly traffic files in the bucket, giving 5 rows at first training, 6 at the retraining rehearsal and 18 at the final training on 2 October, because NDW traffic exists only for hours when ingestion ran. In the first five rows, the highest NO₂ value occurred at almost no traffic. The model has no information about weather. A Luchtmeetnet value stamped T averages the hour before T, so traffic measured during hour H is joined to the value stamped one hour later; rounding both to the nearest hour would pair traffic with the hour before it. Flagged and null NO₂ rows are excluded from training.

## Decision

The model is a linear regression on total traffic at the four sites and the local Dutch hour of day. With so few rows, the data barely supports even three parameters; a more flexible model would fit noise. Because a train and test split on so few rows would leave almost no test points, leave-one-out error is reported alongside in-sample error. At 5 rows the in-sample MAE was 1.93 µg/m³ and the leave-one-out MAE 47.11; at 6 rows, 2.04 and 6.07. One added row cutting the out-of-sample error from 47 to 6 showed how unstable the model was. The final model, trained on 18 rows, has an in-sample MAE of 7.11, a leave-one-out MAE of 8.58 and an R² of only 0.03. The two errors are now close, but the model explains almost none of the variation. Its traffic coefficient turned negative (about 1.2 µg/m³ less NO₂ per 1,000 vehicles per hour, after about 0.85 more at 6 rows), and its hour-of-day coefficient is positive. The highest value in the training data, 40.17 µg/m³, was measured at 20:00 with 3,180 vehicles per hour. These numbers describe the 18 hours observed; with this little data, no conclusion about the effect of traffic on NO₂ can be drawn from them, and the sign of the traffic coefficient changed once already as data was added. A more flexible model, such as a random forest, would only be considered with weeks of data covering weekdays, weekends and varied weather (dozens of rows per parameter as a rule of thumb), weather features, time-based cross-validation, and a leave-one-out error close to the in-sample error.

`no2_exceedance_risk` is derived with a sigmoid centred on **40 µg/m³**, the EU annual limit value, with steepness 0.2: 0.5 at the threshold, about 0.12 ten below and 0.88 ten above. An annual limit applied to hourly predictions is a warning signal, not a legal breach, and the page says so. The EU hourly limit of 200 would give near-zero risk at this station, where the highest hourly value collected in the first days was about 51. The stretch-goal logistic classifier was not trained: only one of the 18 training hours reaches 40 µg/m³, far too few examples to learn from.

`model.pkl` and `model_metrics.json` are baked into the dashboard image. Training-serving skew here would mean features computed differently at serving time: one site's traffic instead of the total, UTC instead of local hours, another scikit-learn version, or a model file changing under the running service. Training and serving share `features.py`, the dashboard always passes the total, the image pins scikit-learn 1.9.1 from `uv.lock`, and a baked model cannot change at runtime.

One station suffices because all four NDW sites are at the interchange where NL10240 stands: one place, one air measurement, one traffic total, which is also why the prediction is identical for every site. NO₂ falls off quickly with distance from a road, so a second interchange would need its own nearby station.

If `predict()` fails, `/site/{id}` returns 200 with the real NO₂ and traffic values, null prediction fields, a `prediction_error` message and a logged error. The measurements are the most trustworthy part of the response, and failing the request would hide real data because of a model problem. If the database or bucket is unreachable, it returns 503, since there is no real data to show. A test covers this.

## Consequences

The model is simple, explainable and identical to the version tested, but not yet reliable, which the dashboard states using the `/model` endpoint. Every retrain requires rebuilding and redeploying the image; the procedure was rehearsed once and repeated for the final model. Scheduled retraining that deploys only when leave-one-out error does not worsen is the natural next step.
