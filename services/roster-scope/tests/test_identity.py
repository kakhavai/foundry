"""The resolver seam — the boundary with `player-identity`."""

import httpx
import pytest
import respx

from roster_scope.adapters.identity import (
    HttpPlayerIdentityResolver,
    PlayerRef,
    StubPlayerIdentityResolver,
    UnresolvablePlayer,
    build_resolver,
    normalize_name,
)


def test_normalize_name_folds_case_diacritics_punctuation_and_suffix():
    # An apostrophe is elided rather than split on, so `Ja'Marr` stays one
    # token and still matches a feed that writes `JaMarr`.
    assert normalize_name("Ja'Marr Chase") == "jamarr chase"
    assert normalize_name("JaMarr Chase") == "jamarr chase"
    assert normalize_name("Odell Beckham Jr.") == "odell beckham"
    assert normalize_name("  A.J.  Brown ") == "a j brown"
    assert normalize_name("Amon-Ra St. Brown") == "amon ra st brown"
    assert normalize_name("Nuñez") == "nunez"
    assert normalize_name("Patrick Mahomes II") == "patrick mahomes"


async def test_stub_is_deterministic_and_attribute_keyed():
    resolver = StubPlayerIdentityResolver()
    ref = PlayerRef("Patrick Mahomes", "KC", "QB")
    first = await resolver.resolve(ref)
    assert first == await resolver.resolve(ref)
    assert first.startswith("fdy-")
    assert len(first) == len("fdy-") + 12

    # Same human, different team: a different slot, so a different id. The
    # stub keys on the attributes it was handed and nothing else.
    assert await resolver.resolve(PlayerRef("Patrick Mahomes", "BUF", "QB")) != first


async def test_stub_ignores_suffix_and_punctuation_differences():
    resolver = StubPlayerIdentityResolver()
    a = await resolver.resolve(PlayerRef("Odell Beckham Jr.", "MIA", "WR"))
    b = await resolver.resolve(PlayerRef("Odell Beckham", "MIA", "WR"))
    assert a == b


@pytest.mark.parametrize(
    "ref,reason",
    [
        (PlayerRef("Patrick Mahomes", "", "QB"), "identity_missing_attributes"),
        (PlayerRef("Patrick Mahomes", "KC", "  "), "identity_missing_attributes"),
        (PlayerRef("A", "KC", "QB"), "identity_unresolvable_name"),
        (PlayerRef("", "KC", "QB"), "identity_unresolvable_name"),
        (PlayerRef("!!!", "KC", "QB"), "identity_unresolvable_name"),
    ],
)
async def test_stub_refuses_rather_than_guesses(ref, reason):
    """A stable id derived from nothing is worse than an absent one: it looks
    resolved to every downstream collector."""
    resolver = StubPlayerIdentityResolver()
    with pytest.raises(UnresolvablePlayer) as exc:
        await resolver.resolve(ref)
    assert exc.value.reason == reason


def test_build_resolver_defaults_to_the_stub(monkeypatch):
    monkeypatch.delenv("PLAYER_IDENTITY_URL", raising=False)
    assert isinstance(build_resolver(httpx.AsyncClient()), StubPlayerIdentityResolver)


def test_build_resolver_switches_on_the_env_var(monkeypatch):
    monkeypatch.setenv("PLAYER_IDENTITY_URL", "http://player-identity:8002")
    assert isinstance(build_resolver(httpx.AsyncClient()), HttpPlayerIdentityResolver)


def test_build_resolver_treats_whitespace_as_unset(monkeypatch):
    monkeypatch.setenv("PLAYER_IDENTITY_URL", "   ")
    assert isinstance(build_resolver(httpx.AsyncClient()), StubPlayerIdentityResolver)


@respx.mock
async def test_http_resolver_takes_the_most_confident_candidate():
    respx.get("http://identity.test/resolve").mock(
        return_value=httpx.Response(
            200,
            json={
                "candidates": [
                    {"player_id": "fdy-low", "confidence": 0.6},
                    {"player_id": "fdy-high", "confidence": 0.95},
                ]
            },
        )
    )
    async with httpx.AsyncClient() as client:
        resolver = HttpPlayerIdentityResolver(client, "http://identity.test/")
        assert await resolver.resolve(PlayerRef("X Y", "KC", "QB")) == "fdy-high"


@respx.mock
async def test_http_resolver_sends_the_bearer_token(monkeypatch):
    monkeypatch.setenv("COLLECTOR_TOKEN", "shared-secret")
    route = respx.get("http://identity.test/resolve").mock(
        return_value=httpx.Response(
            200, json={"candidates": [{"player_id": "fdy-x", "confidence": 0.9}]}
        )
    )
    async with httpx.AsyncClient() as client:
        resolver = HttpPlayerIdentityResolver(client, "http://identity.test")
        await resolver.resolve(PlayerRef("X Y", "KC", "QB", jersey_number=15))
    request = route.calls[0].request
    assert request.headers["Authorization"] == "Bearer shared-secret"
    assert "jersey_number=15" in str(request.url)


@respx.mock
async def test_http_resolver_treats_a_tie_as_unresolvable():
    """A candidate inside the margin goes to the miss queue rather than to the
    higher score — a wrong link is far more expensive than a missing one."""
    respx.get("http://identity.test/resolve").mock(
        return_value=httpx.Response(
            200, json={"candidates": [{"player_id": "fdy-x", "confidence": 0.2}]}
        )
    )
    async with httpx.AsyncClient() as client:
        resolver = HttpPlayerIdentityResolver(client, "http://identity.test")
        with pytest.raises(UnresolvablePlayer) as exc:
            await resolver.resolve(PlayerRef("X Y", "KC", "QB"))
    assert exc.value.reason == "identity_no_confident_match"


@respx.mock
async def test_http_resolver_maps_transport_failure_to_unresolvable():
    """It must not raise a bare httpx error — the caller records a *reason*
    against a slot, and an unhandled exception would abort the whole pass."""
    respx.get("http://identity.test/resolve").mock(
        side_effect=httpx.ConnectError("refused")
    )
    async with httpx.AsyncClient() as client:
        resolver = HttpPlayerIdentityResolver(client, "http://identity.test")
        with pytest.raises(UnresolvablePlayer) as exc:
            await resolver.resolve(PlayerRef("X Y", "KC", "QB"))
    assert exc.value.reason == "identity_upstream_error"


@respx.mock
async def test_http_resolver_rejects_a_response_without_an_id():
    respx.get("http://identity.test/resolve").mock(
        return_value=httpx.Response(200, json={"candidates": [{"confidence": 0.99}]})
    )
    async with httpx.AsyncClient() as client:
        resolver = HttpPlayerIdentityResolver(client, "http://identity.test")
        with pytest.raises(UnresolvablePlayer) as exc:
            await resolver.resolve(PlayerRef("X Y", "KC", "QB"))
    assert exc.value.reason == "identity_malformed_response"
