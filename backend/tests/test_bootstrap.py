from __future__ import annotations

from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from unittest.mock import ANY, AsyncMock, patch

import pytest
import time_machine
from sqlalchemy import text

from nordpool_predictor.ingestion.prices import backfill_prices as _backfill_prices
from nordpool_predictor.scheduler import bootstrap as module
from nordpool_predictor.scheduler.bootstrap import verify_price_history as _verify_price_history


@pytest.mark.parametrize("existing_forecast", [False, True])
async def test_partial_bootstrap_preserves_ready_area_and_completes_missing_area(
    test_settings,
    existing_forecast,
):
    completed = set()

    async def ready(area, version, **kwargs):
        return (area == "DK1" and existing_forecast) or area in completed

    async def forecast_area(area, **kwargs):
        completed.add(area)

    with (
        patch.object(module, "get_settings", return_value=test_settings),
        patch.object(module, "ready_models", AsyncMock(return_value={"DK1": "v1"})),
        patch.object(module, "forecast_ready", AsyncMock(side_effect=ready)) as check,
        patch.object(module, "train_models", AsyncMock(return_value="v2")) as train,
        patch.object(module, "run_forecast", AsyncMock(side_effect=forecast_area)) as forecast,
        patch.object(module, "verify_price_history", AsyncMock()),
        patch.object(module, "backfill_prices", AsyncMock()),
        patch.object(module, "backfill_weather", AsyncMock()),
        patch.object(module, "backfill_production", AsyncMock()),
        patch.object(module, "backfill_crossborder", AsyncMock()),
        patch.object(module, "ingest_weather_forecasts", AsyncMock()),
        patch.object(module, "ingest_production_forecasts", AsyncMock()),
    ):
        await module.bootstrap()
    train.assert_awaited_once_with("DK2")
    check.assert_any_await("DK2", "v2", as_of=ANY)
    assert [call.args[0] for call in forecast.await_args_list] == (
        ["DK2"] if existing_forecast else ["DK1", "DK2"]
    )


@pytest.fixture
def recovery_mocks(test_settings):
    with ExitStack() as stack:
        stack.enter_context(patch.object(module, "get_settings", return_value=test_settings))
        stack.enter_context(
            patch.object(module, "ready_models", AsyncMock(return_value={"DK1": "v1"}))
        )
        operations = {
            name: stack.enter_context(patch.object(module, name, AsyncMock()))
            for name in (
                "backfill_prices",
                "backfill_weather",
                "backfill_production",
                "backfill_crossborder",
                "ingest_weather_forecasts",
                "ingest_production_forecasts",
                "train_models",
                "run_forecast",
                "verify_price_history",
            )
        }
        operations["train_models"].return_value = "trained"

        async def ready(area, version, **kwargs):
            return any(call.args[0] == area for call in operations["run_forecast"].await_args_list)

        stack.enter_context(patch.object(module, "forecast_ready", AsyncMock(side_effect=ready)))
        yield operations


@pytest.mark.parametrize("failed", ["backfill_production", "backfill_weather"])
async def test_backfill_failure_still_recovers_area_with_usable_model(recovery_mocks, failed):
    recovery_mocks[failed].side_effect = RuntimeError("upstream failure")
    with pytest.raises(RuntimeError, match="Bootstrap incomplete"):
        await module.bootstrap()
    recovery_mocks["train_models"].assert_not_awaited()
    recovery_mocks["run_forecast"].assert_awaited_once_with(
        "DK1", issued_at=ANY, model_version=ANY
    )


async def test_bootstrap_pins_validated_and_newly_trained_model_versions(recovery_mocks):
    recovery_mocks["train_models"].return_value = "new-DK2"
    await module.bootstrap()
    assert {
        call.args[0]: call.kwargs.get("model_version")
        for call in recovery_mocks["run_forecast"].await_args_list
    } == {"DK1": "v1", "DK2": "new-DK2"}


async def test_area_weather_failure_does_not_block_other_area_or_training(recovery_mocks):
    async def weather(*, area):
        if area == "DK2":
            raise RuntimeError("DK2 unavailable")

    recovery_mocks["ingest_weather_forecasts"].side_effect = weather
    with pytest.raises(RuntimeError, match="DK2 weather forecasts"):
        await module.bootstrap()
    recovery_mocks["train_models"].assert_awaited_once_with("DK2")
    recovery_mocks["run_forecast"].assert_awaited_once_with(
        "DK1", issued_at=ANY, model_version=ANY
    )


async def test_live_production_failure_happens_after_training(recovery_mocks):
    recovery_mocks["ingest_production_forecasts"].side_effect = RuntimeError("unavailable")
    with pytest.raises(RuntimeError, match="production forecasts"):
        await module.bootstrap()
    recovery_mocks["train_models"].assert_awaited_once_with("DK2")
    recovery_mocks["run_forecast"].assert_not_awaited()


async def test_weather_backfill_is_isolated_per_training_area(recovery_mocks):
    async def weather(*, days, area):
        if area == "DK1":
            raise RuntimeError("DK1 unavailable")

    with patch.object(module, "ready_models", AsyncMock(return_value={})):
        recovery_mocks["backfill_weather"].side_effect = weather
        with pytest.raises(RuntimeError, match="DK1 weather backfill"):
            await module.bootstrap()
    recovery_mocks["train_models"].assert_awaited_once_with("DK2")
    recovery_mocks["run_forecast"].assert_awaited_once_with(
        "DK2", issued_at=ANY, model_version=ANY
    )


@pytest.mark.parametrize("price_failure", [False, True])
async def test_reused_models_restore_recent_prices_without_retraining(
    recovery_mocks, price_failure
):
    with patch.object(module, "ready_models", AsyncMock(return_value={"DK1": "v1", "DK2": "v2"})):
        if price_failure:
            recovery_mocks["backfill_prices"].side_effect = RuntimeError("prices unavailable")
            with pytest.raises(RuntimeError, match="recent price backfill"):
                await module.bootstrap()
        else:
            await module.bootstrap()
        # Persisted usable history permits recovery even when refreshing it fails.
        assert [call.args[0] for call in recovery_mocks["run_forecast"].await_args_list] == [
            "DK1",
            "DK2",
        ]
    recovery_mocks["backfill_prices"].assert_awaited_once_with(days=8)
    recovery_mocks["train_models"].assert_not_awaited()


async def test_failed_full_price_backfill_can_still_recover_existing_model(recovery_mocks):
    async def prices(*, days):
        if days != 8:
            raise RuntimeError("full history unavailable")

    recovery_mocks["backfill_prices"].side_effect = prices
    with pytest.raises(RuntimeError, match="price backfill"):
        await module.bootstrap()
    recovery_mocks["train_models"].assert_not_awaited()
    recovery_mocks["run_forecast"].assert_awaited_once_with(
        "DK1", issued_at=ANY, model_version=ANY
    )


@pytest.mark.parametrize("training_days,backfill_days", [(2, [2, 8]), (8, [8]), (365, [365])])
async def test_training_backfill_must_cover_prediction_window(
    recovery_mocks, test_settings, training_days, backfill_days
):
    test_settings.training_window_days = training_days
    await module.bootstrap()
    assert [call.kwargs["days"] for call in recovery_mocks["backfill_prices"].await_args_list] == (
        backfill_days
    )
    recovery_mocks["run_forecast"].assert_any_await("DK1", issued_at=ANY, model_version=ANY)


@pytest.mark.parametrize("elapsed_minutes,expected_days", [(5, [365]), (20, [365, 8])])
async def test_price_fetch_is_refreshed_when_training_crosses_utc_midnight(
    recovery_mocks, elapsed_minutes, expected_days
):
    started = datetime(2026, 1, 10, 23, 50, tzinfo=UTC)
    with time_machine.travel(started, tick=False) as clock:

        async def train(area):
            clock.move_to(started + timedelta(minutes=elapsed_minutes))
            return "trained"

        recovery_mocks["train_models"].side_effect = train
        await module.bootstrap()
    assert [call.kwargs["days"] for call in recovery_mocks["backfill_prices"].await_args_list] == (
        expected_days
    )
    assert [call.args[0] for call in recovery_mocks["run_forecast"].await_args_list] == [
        "DK1",
        "DK2",
    ]


async def test_forecast_that_expires_during_training_is_recovered(recovery_mocks):
    trained = False

    async def train(area):
        nonlocal trained
        trained = True
        return "trained"

    async def ready(area, version, **kwargs):
        return not trained or any(
            call.args[0] == area for call in recovery_mocks["run_forecast"].await_args_list
        )

    recovery_mocks["train_models"].side_effect = train
    with patch.object(module, "forecast_ready", AsyncMock(side_effect=ready)) as check:
        await module.bootstrap()
    assert trained
    check.assert_any_await("DK1", "v1", as_of=ANY)
    assert [call.args[0] for call in recovery_mocks["run_forecast"].await_args_list] == [
        "DK1",
        "DK2",
    ]


async def test_forecast_that_expires_during_another_areas_recovery_is_regenerated(recovery_mocks):
    expiry = datetime(2026, 1, 11, tzinfo=UTC)

    async def ready(area, version, *, as_of):
        if any(call.args[0] == area for call in recovery_mocks["run_forecast"].await_args_list):
            return True
        return area == "DK1" and as_of < expiry

    with (
        time_machine.travel(expiry - timedelta(minutes=1), tick=False) as clock,
        patch.object(module, "ready_models", AsyncMock(return_value={"DK1": "v1", "DK2": "v2"})),
        patch.object(module, "forecast_ready", AsyncMock(side_effect=ready)),
    ):

        async def weather(*, area):
            if area == "DK2":
                clock.move_to(expiry + timedelta(minutes=1))

        recovery_mocks["ingest_weather_forecasts"].side_effect = weather
        await module.bootstrap()
    assert [call.args[0] for call in recovery_mocks["run_forecast"].await_args_list] == [
        "DK2",
        "DK1",
    ]


@pytest.mark.parametrize("rollover_during", ["weather", "coverage"])
async def test_coverage_and_prediction_share_cutoff_across_midnight(
    recovery_mocks, rollover_during
):
    before = datetime(2026, 1, 10, 23, 59, tzinfo=UTC)
    after = datetime(2026, 1, 11, 0, 1, tzinfo=UTC)
    validated = {}
    with time_machine.travel(before, tick=False) as clock:

        async def weather(*, area):
            if area == "DK1" and rollover_during == "weather":
                clock.move_to(after)

        async def verify(area, *, issued_at):
            validated[area] = issued_at
            if area == "DK1" and rollover_during == "coverage":
                clock.move_to(after)

        recovery_mocks["ingest_weather_forecasts"].side_effect = weather
        recovery_mocks["verify_price_history"].side_effect = verify
        await module.bootstrap()
    assert validated["DK1"] == (after if rollover_during == "weather" else before)
    for call in recovery_mocks["run_forecast"].await_args_list:
        assert call.kwargs["issued_at"] == validated[call.args[0]]
    assert [call.kwargs["days"] for call in recovery_mocks["backfill_prices"].await_args_list] == [
        365,
        8,
    ]


async def test_generated_forecast_that_is_still_unready_is_reported_without_retry_loop(
    recovery_mocks,
):
    with patch.object(module, "forecast_ready", AsyncMock(return_value=False)):
        with pytest.raises(RuntimeError, match="forecast readiness after recovery"):
            await module.bootstrap()
    assert [call.args[0] for call in recovery_mocks["run_forecast"].await_args_list] == [
        "DK1",
        "DK2",
    ]


@pytest.mark.parametrize(
    "models,failed_area,healthy_area",
    [
        ({"DK1": "v1", "DK2": "v2"}, "DK1", "DK2"),
        ({"DK1": "v1", "DK2": "v2"}, "DK2", "DK1"),
        ({"DK1": "v1"}, "DK1", "DK2"),
    ],
)
async def test_readiness_query_failure_does_not_block_other_area_recovery(
    recovery_mocks, models, failed_area, healthy_area
):
    async def ready(area, version, **kwargs):
        if area == failed_area:
            raise RuntimeError("readiness query unavailable")
        return any(call.args[0] == area for call in recovery_mocks["run_forecast"].await_args_list)

    with (
        patch.object(module, "ready_models", AsyncMock(return_value=models)),
        patch.object(module, "forecast_ready", AsyncMock(side_effect=ready)) as check,
    ):
        with pytest.raises(
            RuntimeError, match=f"Bootstrap incomplete: {failed_area} forecast readiness"
        ):
            await module.bootstrap()
    assert sum(call.args[0] == failed_area for call in check.await_args_list) == 1
    assert any(call.args[0] == healthy_area for call in check.await_args_list)
    recovery_mocks["run_forecast"].assert_awaited_once_with(
        healthy_area, issued_at=ANY, model_version=ANY
    )
    if healthy_area not in models:
        recovery_mocks["train_models"].assert_awaited_once_with(healthy_area)
    else:
        recovery_mocks["train_models"].assert_not_awaited()


async def test_ready_models_reject_missing_artifacts(database):
    async with database.begin() as conn:
        await conn.execute(
            text("""
            INSERT INTO model_versions (model_version, model_type, area, horizon_strategy,
                feature_set_version, trained_at, artifact_path, is_active)
            VALUES ('test', 'test', 'DK1', 'direct', 'v2-asof', NOW(), '/missing', TRUE)
        """)
        )
    with patch("nordpool_predictor.ml.predict._load_models", side_effect=FileNotFoundError):
        assert await module.ready_models() == {}


@pytest.mark.parametrize(
    "status,model_version,future,expected",
    [
        ("completed", "v1", True, True),
        ("scored", "v1", True, True),
        ("completed", "v1", False, False),
        ("completed", "v2", True, False),
        ("failed", "v1", True, False),
        ("running", "v1", True, False),
    ],
)
async def test_forecast_readiness_uses_matching_successful_unexpired_values(
    database, status, model_version, future, expected
):
    now = datetime.now(UTC)
    async with database.begin() as conn:
        await conn.execute(
            text("""
            INSERT INTO model_versions (model_version, model_type, area, horizon_strategy,
                feature_set_version, trained_at, artifact_path)
            VALUES (:version, 'test', 'DK1', 'direct', 'v2-asof', NOW(), '/tmp/test')
        """),
            {"version": model_version},
        )
        run_id = (
            await conn.execute(
                text("""
            INSERT INTO forecast_runs (area, model_version, issued_at, horizon_steps, status)
            VALUES ('DK1', :version, :issued, 1, :status) RETURNING run_id
        """),
                {"version": model_version, "issued": now, "status": status},
            )
        ).scalar_one()
        await conn.execute(
            text("""
            INSERT INTO forecast_values (run_id, ts, horizon_step, predicted_price_dkk_kwh)
            VALUES (:id, :ts, 1, 0.5)
        """),
            {"id": run_id, "ts": now + timedelta(days=1 if future else -1)},
        )
    assert await module.forecast_ready("DK1", "v1") is expected
    assert await module.forecast_ready("DK2", "v1") is False


@pytest.mark.parametrize("minute", [0, 8])
@pytest.mark.parametrize("missing_slot", [None, 0, 400, 767])
async def test_price_history_requires_each_slot_in_prediction_window(
    database, minute, missing_slot
):
    issued = datetime(2026, 1, 10, 12, minute, tzinfo=UTC)
    end = datetime(2026, 1, 10, 12, 15 if minute else 0, tzinfo=UTC)
    start = end - timedelta(days=8)
    async with database.begin() as conn:
        await conn.execute(
            text("""
                INSERT INTO price_observations (area, ts, price_dkk_kwh, source)
                VALUES (:area, :ts, 0, 'test')
            """),
            [
                {"area": area, "ts": start + timedelta(minutes=15 * step)}
                for area in ["DK1", "DK2"]
                for step in range(768)
                if area == "DK2" or step != missing_slot
            ],
        )
        # Rows outside the consumed window must not compensate for a missing slot.
        await conn.execute(
            text("""
                INSERT INTO price_observations (area, ts, price_dkk_kwh, source)
                VALUES ('DK1', :before, 0, 'test'), ('DK1', :after, 0, 'test')
            """),
            {"before": start - timedelta(minutes=15), "after": end},
        )
    with time_machine.travel(issued, tick=False):
        if missing_slot is None:
            await module.verify_price_history("DK1")
        else:
            with pytest.raises(RuntimeError, match="DK1: 1 missing slots"):
                await module.verify_price_history("DK1")
        await module.verify_price_history("DK2")


@pytest.mark.parametrize("missing_area", ["DK1", "DK2"])
async def test_empty_backfill_only_blocks_area_without_persisted_history(
    database, recovery_mocks, missing_area
):
    issued = datetime(2026, 1, 10, 12, 0, tzinfo=UTC)
    healthy_area = "DK2" if missing_area == "DK1" else "DK1"
    async with database.begin() as conn:
        await conn.execute(
            text("""
                INSERT INTO price_observations (area, ts, price_dkk_kwh, source)
                VALUES (:area, :ts, 0.5, 'test')
            """),
            [
                {"area": healthy_area, "ts": issued - timedelta(minutes=15 * step)}
                for step in range(1, 769)
            ],
        )
    # Exercise the real coverage query while ingestion returns successfully without writing rows.
    with (
        time_machine.travel(issued, tick=False),
        patch.object(module, "ready_models", AsyncMock(return_value={"DK1": "v1", "DK2": "v2"})),
        patch.object(module, "verify_price_history", _verify_price_history),
    ):
        with pytest.raises(RuntimeError, match=f"{missing_area} price history"):
            await module.bootstrap()
    recovery_mocks["backfill_prices"].assert_awaited_once_with(days=8)
    recovery_mocks["run_forecast"].assert_awaited_once_with(
        healthy_area, issued_at=ANY, model_version=ANY
    )
    recovery_mocks["train_models"].assert_not_awaited()


async def test_midnight_refresh_restores_persisted_coverage_for_both_areas(
    database, recovery_mocks, test_settings
):
    from nordpool_predictor.ingestion import prices

    test_settings.training_window_days = 8
    started = datetime(2026, 1, 10, 23, 50, tzinfo=UTC)

    async def fetch(start, end, areas):
        return [
            {
                "area": area,
                "ts": start + timedelta(minutes=15 * step),
                "price_dkk_kwh": 0.5,
                "source": "test",
            }
            for area in areas
            for step in range(int((end - start).total_seconds() // 900))
        ]

    with (
        time_machine.travel(started, tick=False) as clock,
        patch.object(prices, "get_settings", return_value=test_settings),
        patch.object(prices, "_fetch_chunk", AsyncMock(side_effect=fetch)) as request,
        patch.object(module, "backfill_prices", _backfill_prices),
        patch.object(module, "verify_price_history", _verify_price_history),
    ):

        async def train(area):
            clock.move_to(datetime(2026, 1, 11, 0, 10, tzinfo=UTC))
            return "trained"

        recovery_mocks["train_models"].side_effect = train
        await module.bootstrap()

    assert [call.args[1] for call in request.await_args_list] == [
        datetime(2026, 1, 11, tzinfo=UTC),
        datetime(2026, 1, 12, tzinfo=UTC),
    ]
    assert [call.args[0] for call in recovery_mocks["run_forecast"].await_args_list] == [
        "DK1",
        "DK2",
    ]
