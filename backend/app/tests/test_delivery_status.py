from unittest.mock import AsyncMock

import pytest

from app.api.publications import delivery_status


@pytest.mark.parametrize(
    "available,state",
    [(True, "CHECK_UNAVAILABLE"), (None, "CHECK_UNAVAILABLE"), (False, "WORKER_UNAVAILABLE")],
)
async def test_delivery_status_does_not_infer_full_readiness(monkeypatch, available, state):
    check = AsyncMock(return_value=available)
    monkeypatch.setattr("app.workers.availability.queue_has_consumer", check)
    result = await delivery_status(None)
    assert result["state"] == state
    assert "План сохранён" in result["message"]
    assert "Готов к автоматической отправке" not in result["message"]
    check.assert_awaited_once_with("publication")
