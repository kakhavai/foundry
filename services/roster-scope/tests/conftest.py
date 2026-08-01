from datetime import UTC, datetime

import pytest
from collector_core.conditional import ETAGS
from collector_core.envelope import ENVELOPE_VERSION, Coverage, Envelope, Upstream
from collector_core.lake import lake_key
from fastapi.testclient import TestClient
from opentelemetry import metrics as otel_metrics
from opentelemetry.exporter.prometheus import PrometheusMetricReader
from opentelemetry.sdk.metrics import MeterProvider
from prometheus_client import REGISTRY, generate_latest

from roster_scope.main import app
from roster_scope.matchups import MATCHUP_SIGNAL
from roster_scope.rules import ALL_RULES, TEAMS
from roster_scope.scope import CHANGE_SIGNAL, MEMBERSHIP_SIGNAL

NOW = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)


@pytest.fixture(scope="session", autouse=True)
def _meter_provider():
    """One real MeterProvider for the whole session.

    Instruments are created at import time and record nothing until a provider
    exists; anything recorded before it is silently lost. `set_meter_provider`
    is one-shot per process, so this must happen exactly once.
    """
    otel_metrics.set_meter_provider(
        MeterProvider(metric_readers=[PrometheusMetricReader()])
    )


@pytest.fixture(autouse=True)
def _reset_etags():
    """`ETAGS` (`collector_core.conditional`) is a **process-global** singleton
    and this collector's depth-chart adapter writes to it on every successful
    fetch.

    Here rather than in the one file that happens to seed it today: pytest runs
    the whole suite in one process, so a stored ETag leaks into every later
    test's transport as an `If-None-Match` header. In `conftest.py` the
    isolation is structural; in a test file it lasts exactly as long as the
    next author remembering to re-add it.
    """
    ETAGS.clear()
    yield
    ETAGS.clear()


def _parse_scrape(text: str) -> list[tuple[str, dict[str, str], float]]:
    series: list[tuple[str, dict[str, str], float]] = []
    for line in text.splitlines():
        if line.startswith("#"):
            continue
        head, _, raw_value = line.rpartition(" ")
        if "{" in head:
            name, _, raw_labels = head.partition("{")
            labels = {
                k: v.strip('"')
                for k, v in (
                    pair.split("=", 1)
                    for pair in raw_labels.rstrip("}").split(",")
                    if pair
                )
            }
        else:
            name, labels = head, {}
        try:
            series.append((name, labels, float(raw_value)))
        except ValueError:
            continue
    return series


def _lookup(series):
    def read(name: str, **labels: str) -> float | None:
        for found_name, found_labels, value in series:
            if found_name != name:
                continue
            if all(found_labels.get(k) == v for k, v in labels.items()):
                return value
        return None

    return read


@pytest.fixture
def scrape():
    """Take one Prometheus scrape, then look series up from it.

    This used to exist because OTel's *synchronous* Gauge is last-value
    aggregated with the point consumed by a collection: it was exported on the
    first collection after a measurement and absent from every one after that,
    so two `metric_value` calls silently reported the second as missing. That
    was a real defect rather than a test-harness quirk — Prometheus scrapes
    every 15-30s against a capture cadence of minutes, so almost every real
    scrape saw nothing — and it is fixed:
    `collector_core.metrics.LastValueGauge` makes every fleet and per-collector
    gauge observable, reporting current state on every collection.

    The fixture stays, because taking one scrape and asserting several series
    against it is the honest shape for a test that means "this is what
    Prometheus sees".
    """

    def take():
        return _lookup(_parse_scrape(generate_latest(REGISTRY).decode()))

    return take


@pytest.fixture
def metric_value():
    """Read one series out of real /metrics output, so these tests check
    exactly what Prometheus scrapes — including OTel's name mangling.

    Prefer `scrape` when one test asserts several series, so they all come
    from one collection rather than one each.

    Returns None when the series is absent, which is distinct from a series
    present with value 0.0. That distinction is the entire point of
    `scope_missed_producers_total`.
    """

    def read(name: str, **labels: str) -> float | None:
        return _lookup(_parse_scrape(generate_latest(REGISTRY).decode()))(
            name, **labels
        )

    return read


TEST_TOKEN = "test-collector-token"


@pytest.fixture(autouse=True)
def _collector_token(monkeypatch):
    monkeypatch.setenv("COLLECTOR_TOKEN", TEST_TOKEN)


@pytest.fixture
def collector_token(_collector_token) -> str:
    return TEST_TOKEN


@pytest.fixture
def client(_collector_token):
    with TestClient(app, headers={"Authorization": f"Bearer {TEST_TOKEN}"}) as c:
        yield c


@pytest.fixture(autouse=True)
def _reset_collector_singletons():
    """`state` and `refresh_gate` are process-level singletons — that is what
    lets `/signals` serve a cache and `/refresh` enforce a floor across
    requests — so something has to reset them between tests."""
    spec = app.state.collector_spec
    spec.state.envelopes = {}
    spec.state.last_capture_at = None
    spec.refresh_gate._last_allowed_at = None
    yield
    spec.state.envelopes = {}
    spec.state.last_capture_at = None
    spec.refresh_gate._last_allowed_at = None


@pytest.fixture(autouse=True)
def _no_player_identity(monkeypatch):
    """The stub resolver is the default; a stray env var must not turn a unit
    test into a live HTTP call."""
    monkeypatch.delenv("PLAYER_IDENTITY_URL", raising=False)


class SpyLake:
    """An in-memory `LakeWriter`.

    A spy rather than `moto`: CI prunes `collector-core`'s dev dependencies
    inside `services/`, so a `moto` import passes locally off a shared
    virtualenv and fails only in CI.
    """

    def __init__(self, *, fail_list: bool = False, fail_write: bool = False) -> None:
        self.objects: dict[str, dict] = {}
        self.writes: list[Envelope] = []
        self.fail_list = fail_list
        self.fail_write = fail_write

    def write(self, envelope: Envelope) -> str:
        if self.fail_write:
            raise RuntimeError("lake unreachable")
        key = lake_key(envelope)
        self.objects[key] = envelope.to_dict()
        self.writes.append(envelope)
        return key

    def list_keys(
        self,
        collector: str,
        signal_type: str,
        season: int,
        week: int,
        version: str = ENVELOPE_VERSION,
    ) -> list[str]:
        if self.fail_list:
            raise RuntimeError("lake unreachable")
        prefix = f"signals/{collector}/v{version}/season={season}/week={week:02d}/"
        suffix = f"-{signal_type}.json"
        return sorted(
            k for k in self.objects if k.startswith(prefix) and k.endswith(suffix)
        )

    def read(self, key: str) -> dict:
        if self.fail_list:
            raise RuntimeError("lake unreachable")
        return self.objects[key]


@pytest.fixture
def lake() -> SpyLake:
    return SpyLake()


# The real nflverse depth-chart schema, verified against the live asset.
# Deliberately carries the columns the adapter ignores (espn_id, pos_grp,
# pos_name, pos_slot) as well as the five it reads, so a test document
# exercises the same column-indexing the live feed does rather than a
# convenient subset. Note there is no season or week column: the feed is a
# current snapshot stamped with `dt`.
DEPTH_HEADER = (
    "dt,team,player_name,espn_id,gsis_id,pos_grp_id,pos_grp,"
    "pos_id,pos_name,pos_abb,pos_slot,pos_rank"
)

DEFAULT_DT = "2026-09-15T09:00:00Z"


def depth_row(
    team: str,
    position: str,
    order: int,
    name: str,
    *,
    dt: str = DEFAULT_DT,
) -> str:
    return (
        f"{dt},{team},{name},1,00-0000001,1,Group,1,Position Name,"
        f"{position},{order},{order}"
    )


def depth_csv(rows: list[str], header: str = DEPTH_HEADER) -> str:
    return "\n".join([header, *rows]) + "\n"


# The positional quotas as the fixture chart satisfies them.
_CHART_SHAPE = tuple(
    (rule.position, rule.max_depth)
    for rule in ALL_RULES
    if rule.entity_type == "player"
)


def full_league_csv(*, dt: str = DEFAULT_DT, teams=TEAMS) -> str:
    """A chart that satisfies every human slot for every team.

    416 expected slots = 384 human + 32 config-derived team defenses, so a
    capture against this yields 100% coverage and any shortfall in a test is
    attributable to whatever that test changed.
    """
    rows = []
    for team in teams:
        for position, depth in _CHART_SHAPE:
            for rank in range(1, depth + 1):
                rows.append(
                    depth_row(
                        team, position, rank, f"{team} {position} Player{rank}", dt=dt
                    )
                )
    return depth_csv(rows)


def make_membership_envelope(
    rows: list[dict], *, version: int, season: int = 2026, week: int = 1, now=NOW
) -> Envelope:
    return Envelope(
        envelope_version=ENVELOPE_VERSION,
        collector="roster-scope",
        signal_type=MEMBERSHIP_SIGNAL,
        captured_at=now,
        upstream=Upstream("nflverse-depth-charts", now),
        scope={"season": season, "week": week, "scope_version": version},
        coverage=Coverage(expected=len(rows), present=len(rows), missing=[]),
        errors=[],
        signals=rows,
    )


def membership_row(
    player_id: str,
    *,
    team: str = "KC",
    position: str = "WR",
    rule_id: str = "wr_depth_le_4",
    rank: int = 1,
    version: int = 1,
    status: str = "active",
    grace_expires_week: int | None = None,
    added_at_version: int = 1,
) -> dict:
    return {
        "player_id": player_id,
        "entity_type": "player",
        "scope_version": version,
        "membership_status": status,
        "rule_id": rule_id,
        "team": team,
        "position": position,
        "depth_rank": rank,
        "previous_depth_rank": None,
        "depth_source_captured_at": "2026-09-15T12:00:00Z",
        "added_at_version": added_at_version,
        "grace_expires_week": grace_expires_week,
        "is_manual_override": False,
        "override_reason": None,
    }


@pytest.fixture
def lake_backed():
    """Swap the process's `NullLakeWriter` for a spy holding one older version.

    `/scope/players?scope_version=<older>` and `/scope/diff` read history from
    the lake rather than from this process's memory, and with the default
    NullLakeWriter those branches are unreachable.
    """
    spec = app.state.collector_spec
    original = spec.lake
    spy = SpyLake()
    spy.write(
        make_membership_envelope([membership_row("fdy-historic", version=1)], version=1)
    )
    spy.write(
        Envelope(
            envelope_version=ENVELOPE_VERSION,
            collector="roster-scope",
            signal_type=CHANGE_SIGNAL,
            captured_at=NOW,
            upstream=Upstream("nflverse-depth-charts", NOW),
            scope={"season": 2026, "week": 1, "scope_version": 1},
            coverage=Coverage(expected=0, present=0, missing=[]),
            errors=[],
            signals=[
                {
                    "scope_version": 1,
                    "player_id": "fdy-historic",
                    "transition": "entered",
                    "from_depth_rank": None,
                    "to_depth_rank": 1,
                    "trigger": "depth_chart",
                    "occurred_at": "2026-09-13T12:00:00Z",
                }
            ],
        )
    )
    spec.lake = spy
    yield spy
    spec.lake = original


@pytest.fixture
def seeded_state():
    """Route tests pre-populate the cache directly — the routes never call an
    upstream. Not autouse: most of the suite never touches it."""
    state = app.state.collector_spec.state
    rows = [
        membership_row("fdy-aaa", team="KC", rank=1, version=3),
        membership_row("fdy-bbb", team="KC", rank=2, version=3),
        membership_row(
            "fdy-ccc",
            team="BUF",
            rank=1,
            version=3,
            status="grace",
            grace_expires_week=3,
        ),
    ]
    state.envelopes = {
        MEMBERSHIP_SIGNAL: make_membership_envelope(rows, version=3),
        CHANGE_SIGNAL: Envelope(
            envelope_version=ENVELOPE_VERSION,
            collector="roster-scope",
            signal_type=CHANGE_SIGNAL,
            captured_at=NOW,
            upstream=Upstream("nflverse-depth-charts", NOW),
            scope={"season": 2026, "week": 1, "scope_version": 3},
            coverage=Coverage(expected=0, present=0, missing=[]),
            errors=[],
            signals=[
                {
                    "scope_version": 2,
                    "player_id": "fdy-aaa",
                    "transition": "entered",
                    "from_depth_rank": None,
                    "to_depth_rank": 1,
                    "trigger": "depth_chart",
                    "occurred_at": "2026-09-14T12:00:00Z",
                },
                {
                    "scope_version": 3,
                    "player_id": "fdy-ccc",
                    "transition": "entered_grace",
                    "from_depth_rank": 1,
                    "to_depth_rank": None,
                    "trigger": "depth_chart",
                    "occurred_at": "2026-09-15T12:00:00Z",
                },
            ],
        ),
    }
    state.last_capture_at = NOW
    yield
    state.envelopes = {}
    state.last_capture_at = None


@pytest.fixture
def seeded_matchup_state():
    """Populates only the matchup envelope — kept separate from `seeded_state`
    so `/signals`' count-of-two assertions (membership + change) are not
    disturbed by a third envelope appearing in `spec.state.envelopes`; nothing
    in `/signals` tests wants a matchup row and `MATCHUP_SIGNAL` is not one of
    the signal types `/signals` iterates for those tests to fail loudly on.
    """
    state = app.state.collector_spec.state
    state.envelopes[MATCHUP_SIGNAL] = Envelope(
        envelope_version=ENVELOPE_VERSION,
        collector="roster-scope",
        signal_type=MATCHUP_SIGNAL,
        captured_at=NOW,
        upstream=Upstream("nflverse-depth-charts", NOW),
        scope={"season": 2026, "week": 1, "scope_version": 3},
        coverage=Coverage(expected=2, present=2, missing=[]),
        errors=[],
        signals=[
            {
                "player_id": "fdy-corner-1",
                "slot_key": "KC:cb_matchup_le_4:1",
                "rule_id": "cb_matchup_le_4",
                "team": "KC",
                "position": "CB",
                "depth_rank": 1,
            },
            {
                "player_id": "fdy-corner-2",
                "slot_key": "KC:cb_matchup_le_4:2",
                "rule_id": "cb_matchup_le_4",
                "team": "KC",
                "position": "CB",
                "depth_rank": 2,
            },
        ],
    )
    yield
    state.envelopes.pop(MATCHUP_SIGNAL, None)
