"""Tests for the trained model and predict() (Day 4, Lab 1)."""

import joblib

import predict


def test_model_pkl_loads():
    model = joblib.load(predict.MODEL_PATH)
    assert hasattr(model, "predict")


def test_predict_returns_plausible_values():
    result = predict.predict(total_intensity_veh_per_hr=3000, hour_of_day=8)

    assert isinstance(result["no2_ug_m3_predicted"], float)
    assert 0 <= result["no2_ug_m3_predicted"] <= 200
    assert 0 <= result["no2_exceedance_risk"] <= 1


def test_risk_is_one_half_at_the_threshold():
    assert predict.exceedance_risk(predict.EXCEEDANCE_THRESHOLD_UG_M3) == 0.5