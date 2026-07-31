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


# --- the HTTP resolver: `player-identity` decides, this module obeys ---------
#
# `GET /resolve` answers with `resolved`, `player_id`, `reason` and a ranked
# `candidates` array — and it populates `candidates` *precisely when it has
# decided not to resolve*, having already filed the row in its own standing
# miss queue. Re-ranking that array against a local floor adopts an identity
# the collector that owns identity deliberately refused, and a wrong
# `player_id` propagates into an append-only lake that is never rewritten.


def resolved_body(player_id="fdy-canonical", *, confidence=1.0, **extra):
    body = {
        "resolved": True,
        "player_id": player_id,
        "link_method": "attribute_score",
        "confidence": confidence,
        "reason": None,
        "candidates": [{"player_id": player_id, "confidence": confidence}],
    }
    body.update(extra)
    return body


def refused_body(reason, candidates):
    """What `/resolve` returns when it declines: `resolved` false, no
    `player_id`, a named reason, and the candidates it would not choose
    between."""
    return {
        "resolved": False,
        "player_id": None,
        "link_method": None,
        "confidence": candidates[0]["confidence"] if candidates else 0.0,
        "reason": reason,
        "candidates": candidates,
    }


@respx.mock
async def test_http_resolver_takes_the_id_player_identity_resolved():
    respx.get("http://identity.test/resolve").mock(
        return_value=httpx.Response(200, json=resolved_body("fdy-high"))
    )
    async with httpx.AsyncClient() as client:
        resolver = HttpPlayerIdentityResolver(client, "http://identity.test/")
        assert await resolver.resolve(PlayerRef("X Y", "KC", "QB")) == "fdy-high"


@respx.mock
async def test_the_resolved_player_id_wins_over_the_candidate_ranking():
    """`player_id` is the answer; `candidates` is the working. Reading the id
    back off the ranked list is how the refusal path got inverted, so the two
    are made to disagree here and the contract field must win."""
    respx.get("http://identity.test/resolve").mock(
        return_value=httpx.Response(
            200,
            json=resolved_body(
                "fdy-canonical",
                candidates=[
                    {"player_id": "fdy-runner-up", "confidence": 0.99},
                    {"player_id": "fdy-canonical", "confidence": 0.72},
                ],
            ),
        )
    )
    async with httpx.AsyncClient() as client:
        resolver = HttpPlayerIdentityResolver(client, "http://identity.test")
        assert await resolver.resolve(PlayerRef("X Y", "KC", "QB")) == "fdy-canonical"


@pytest.mark.parametrize(
    "reason,candidates",
    [
        # A tie. `player-identity` sends this to the miss queue *never* to the
        # higher score, and both scores are far above any local floor.
        (
            "ambiguous",
            [
                {"player_id": "fdy-a", "confidence": 0.97},
                {"player_id": "fdy-b", "confidence": 0.93},
            ],
        ),
        # The worked case from `resolution.py`: a sparse canonical record gives
        # a small denominator, so a name-only query scores 0.30/0.45 = 0.667 on
        # one agreeing attribute. Confident-looking, and nothing of the kind.
        (
            "insufficient_agreeing_attributes",
            [{"player_id": "fdy-sparse", "confidence": 0.667}],
        ),
        ("below_threshold", [{"player_id": "fdy-weak", "confidence": 0.55}]),
        ("no_candidate", []),
    ],
)
@respx.mock
async def test_a_refusal_is_never_re_ranked_into_a_resolution(reason, candidates):
    """THE test for this defect. Every one of these carries a candidate above
    the 0.5 floor the resolver used to apply — the first two above 0.9 — and
    `player-identity` refused all of them anyway. A local re-rank inverts the
    fleet's refuse-rather-than-guess rule at the one seam where identity is
    decided."""
    respx.get("http://identity.test/resolve").mock(
        return_value=httpx.Response(200, json=refused_body(reason, candidates))
    )
    async with httpx.AsyncClient() as client:
        resolver = HttpPlayerIdentityResolver(client, "http://identity.test")
        with pytest.raises(UnresolvablePlayer) as exc:
            await resolver.resolve(PlayerRef("X Y", "KC", "QB"))
    # The upstream's own reason survives into the slot's `coverage.missing`
    # entry: "we could not tell two players apart" and "nobody by that name"
    # are different operational problems and must not collapse.
    assert exc.value.reason == f"identity_unresolved_{reason}"


def test_the_resolver_has_no_local_confidence_floor():
    """Deleted rather than tuned. `player-identity` owns THRESHOLD, MARGIN and
    MIN_AGREEING_ATTRIBUTES; a second, weaker floor downstream could never
    *tighten* the decision — a resolved attribute_score is already >= 0.60 and
    an adopted crosswalk link is 1.0 — so it could only ever loosen it."""
    assert not hasattr(HttpPlayerIdentityResolver, "MIN_CONFIDENCE")


@respx.mock
async def test_a_response_that_omits_resolved_fails_closed():
    """An absent field must not read as permission. Anything that is not
    explicitly `resolved: true` is a refusal."""
    respx.get("http://identity.test/resolve").mock(
        return_value=httpx.Response(
            200, json={"candidates": [{"player_id": "fdy-x", "confidence": 0.99}]}
        )
    )
    async with httpx.AsyncClient() as client:
        resolver = HttpPlayerIdentityResolver(client, "http://identity.test")
        with pytest.raises(UnresolvablePlayer) as exc:
            await resolver.resolve(PlayerRef("X Y", "KC", "QB"))
    assert exc.value.reason == "identity_unresolved_unknown"


@respx.mock
async def test_an_unrecognisable_upstream_reason_is_bounded():
    """`reason` is an upstream string landing in an append-only errors array.
    Sanitised so a garbage value cannot widen that vocabulary arbitrarily."""
    respx.get("http://identity.test/resolve").mock(
        return_value=httpx.Response(
            200, json=refused_body("Ambiguous!! " + "x" * 200, [])
        )
    )
    async with httpx.AsyncClient() as client:
        resolver = HttpPlayerIdentityResolver(client, "http://identity.test")
        with pytest.raises(UnresolvablePlayer) as exc:
            await resolver.resolve(PlayerRef("X Y", "KC", "QB"))
    assert exc.value.reason.startswith("identity_unresolved_")
    assert len(exc.value.reason) <= len("identity_unresolved_") + 40
    assert exc.value.reason.replace("identity_unresolved_", "", 1).isascii()
    assert "!" not in exc.value.reason and " " not in exc.value.reason


@respx.mock
async def test_http_resolver_sends_the_bearer_token(monkeypatch):
    monkeypatch.setenv("COLLECTOR_TOKEN", "shared-secret")
    route = respx.get("http://identity.test/resolve").mock(
        return_value=httpx.Response(200, json=resolved_body("fdy-x"))
    )
    async with httpx.AsyncClient() as client:
        resolver = HttpPlayerIdentityResolver(client, "http://identity.test")
        await resolver.resolve(PlayerRef("X Y", "KC", "QB", jersey_number=15))
    request = route.calls[0].request
    assert request.headers["Authorization"] == "Bearer shared-secret"
    assert "jersey_number=15" in str(request.url)


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
async def test_http_resolver_rejects_a_resolution_carrying_no_id():
    """`resolved: true` with no `player_id` is a contract violation, not a
    licence to go looking in `candidates` for one."""
    respx.get("http://identity.test/resolve").mock(
        return_value=httpx.Response(
            200,
            json=resolved_body(
                None, candidates=[{"player_id": "fdy-x", "confidence": 0.99}]
            ),
        )
    )
    async with httpx.AsyncClient() as client:
        resolver = HttpPlayerIdentityResolver(client, "http://identity.test")
        with pytest.raises(UnresolvablePlayer) as exc:
            await resolver.resolve(PlayerRef("X Y", "KC", "QB"))
    assert exc.value.reason == "identity_malformed_response"
