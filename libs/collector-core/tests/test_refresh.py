from datetime import UTC, datetime, timedelta

from collector_core.refresh import RefreshGate

NOW = datetime(2026, 9, 20, 16, 0, tzinfo=UTC)
FLOOR = timedelta(minutes=5)


def test_first_refresh_is_allowed():
    gate = RefreshGate(FLOOR)
    assert gate.try_acquire(NOW) is not None


def test_refresh_ids_are_unique():
    gate = RefreshGate(FLOOR)
    first = gate.try_acquire(NOW)
    second = gate.try_acquire(NOW + timedelta(minutes=6))
    assert first != second


def test_second_refresh_inside_the_floor_is_refused():
    """Force-refresh must not become a way to get an API key banned."""
    gate = RefreshGate(FLOOR)
    gate.try_acquire(NOW)
    assert gate.try_acquire(NOW + timedelta(minutes=2)) is None


def test_refresh_allowed_once_the_floor_elapses():
    gate = RefreshGate(FLOOR)
    gate.try_acquire(NOW)
    assert gate.try_acquire(NOW + timedelta(minutes=5)) is not None


def test_retry_after_reports_whole_seconds_remaining():
    gate = RefreshGate(FLOOR)
    gate.try_acquire(NOW)
    assert gate.retry_after(NOW + timedelta(minutes=2)) == 180


def test_retry_after_is_zero_when_allowed():
    assert RefreshGate(FLOOR).retry_after(NOW) == 0


def test_a_refused_attempt_does_not_extend_the_floor():
    """Otherwise a client polling every second could never get through."""
    gate = RefreshGate(FLOOR)
    gate.try_acquire(NOW)
    gate.try_acquire(NOW + timedelta(minutes=1))
    gate.try_acquire(NOW + timedelta(minutes=2))
    assert gate.try_acquire(NOW + timedelta(minutes=5)) is not None


def test_retry_after_rounds_up_fractional_seconds():
    """Fractional seconds are rounded up so a caller knows the full wait time."""
    gate = RefreshGate(FLOOR)
    gate.try_acquire(NOW)
    # 2 minutes 0.25 seconds elapsed, leaving 2:59.75 remaining
    result = gate.retry_after(NOW + timedelta(minutes=2, microseconds=250000))
    assert result == 180  # ceil(179.75) = 180, int(179.75) = 179
