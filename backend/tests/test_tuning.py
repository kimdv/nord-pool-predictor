from __future__ import annotations

from datetime import UTC, timedelta
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest
from sklearn.model_selection import TimeSeriesSplit

from nordpool_predictor.ml.train import _objective


def test_tuning_purges_unavailable_labels_in_every_chronological_fold():
    index = pd.date_range("2026-01-01", periods=5000, freq="15min", tz=UTC)
    features = pd.DataFrame({"horizon_step": 1, "feature": range(len(index))}, index=index)
    labels = pd.Series(range(len(index)), index=index)
    splitter = TimeSeriesSplit(n_splits=3)
    folds = list(splitter.split(features))
    for _, validation in folds:
        # A later validation target can simulate an earlier issuance than the first row.
        features.iloc[validation[10], 0] = 8 * 96

    estimator = MagicMock()
    estimator.predict.side_effect = lambda frame: [0] * len(frame)
    with patch("nordpool_predictor.ml.train.lgb.LGBMRegressor", return_value=estimator):
        score = _objective(MagicMock(), features, labels, splitter)

    assert estimator.fit.call_count == len(folds)
    for (_, validation), fit, predict in zip(
        folds, estimator.fit.call_args_list, estimator.predict.call_args_list, strict=True
    ):
        earliest = index[validation[10]] - timedelta(days=8)
        training, training_labels = fit.args
        assert training.index.max() == earliest - timedelta(minutes=15)
        assert earliest not in training.index
        pd.testing.assert_frame_equal(training, features.loc[features.index < earliest])
        pd.testing.assert_series_equal(training_labels, labels.loc[training.index])
        validation_features, validation_labels = fit.kwargs["eval_set"][0]
        pd.testing.assert_frame_equal(validation_features, features.iloc[validation])
        pd.testing.assert_series_equal(validation_labels, labels.iloc[validation])
        pd.testing.assert_frame_equal(predict.args[0], features.iloc[validation])
    assert score == pytest.approx(sum(labels.iloc[val].mean() for _, val in folds) / len(folds))


@pytest.mark.parametrize("remaining", [0, 99, 100])
def test_tuning_requires_100_training_samples_after_purging(remaining):
    index = pd.date_range("2026-01-01", periods=600, freq="15min", tz=UTC)
    features = pd.DataFrame({"horizon_step": 1}, index=index)
    labels = pd.Series(1, index=index)
    splitter = TimeSeriesSplit(n_splits=2, test_size=100)
    for _, validation in splitter.split(features):
        features.iloc[validation[0], 0] = validation[0] - remaining

    estimator = MagicMock()
    estimator.predict.side_effect = lambda frame: [0] * len(frame)
    with patch("nordpool_predictor.ml.train.lgb.LGBMRegressor", return_value=estimator):
        if remaining < 100:
            with pytest.raises(
                ValueError,
                match=rf"Insufficient training samples in tuning fold 1 .*: {remaining}$",
            ):
                _objective(MagicMock(), features, labels, splitter)
            estimator.fit.assert_not_called()
        else:
            assert _objective(MagicMock(), features, labels, splitter) == 1
            assert estimator.fit.call_count == 2
            assert all(len(call.args[0]) == 100 for call in estimator.fit.call_args_list)
