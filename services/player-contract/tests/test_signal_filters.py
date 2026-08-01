"""`signal_matches`, and the boolean filter that is the whole reason it exists.

`is_contract_year` is the query this collector was built to answer. It is also
the one filter in the fleet whose row value is a **bool**, and the obvious
implementation —

    if key in params and str(row.get(key)) != params[key]:

— matches nothing at all for `?is_contract_year=true`, because `str(True)` is
`"True"` and no client sends a capital T. That is the failure this file exists
for: it looks exactly like a working filter returning an honest empty result.

It is additionally **tri-state**: a row whose term the upstream did not supply
carries `None`, and such a row is an answer to neither `true` nor `false`.
"""

import pytest

from player_contract.signals import (
    ROW_FILTERS,
    SUPPORTED_FILTERS,
    signal_matches,
)

CONTRACT_YEAR = {
    "player_id": "fdy-aaaa11112222",
    "team": "GB",
    "is_contract_year": True,
}
NOT_CONTRACT_YEAR = {
    "player_id": "fdy-bbbb11112222",
    "team": "BUF",
    "is_contract_year": False,
}
UNKNOWN_TERM = {
    "player_id": "fdy-cccc11112222",
    "team": None,
    "is_contract_year": None,
}


def test_every_row_filter_is_declared_as_supported():
    """A filter the router rejects can never reach `signal_matches`, and one
    that is declared but not implemented returns everything while looking like
    it worked."""
    assert set(ROW_FILTERS) <= set(SUPPORTED_FILTERS)
    assert set(SUPPORTED_FILTERS) - set(ROW_FILTERS) == {
        "season",
        "week",
        "signal_type",
    }


def test_no_params_matches_everything():
    for row in (CONTRACT_YEAR, NOT_CONTRACT_YEAR, UNKNOWN_TERM):
        assert signal_matches(row, {})


@pytest.mark.parametrize("spelling", ["true", "True", "TRUE", "1", "yes"])
def test_a_true_filter_matches_a_contract_year_however_it_is_spelled(spelling):
    assert signal_matches(CONTRACT_YEAR, {"is_contract_year": spelling})


@pytest.mark.parametrize("spelling", ["true", "True", "1"])
def test_a_true_filter_excludes_a_non_contract_year(spelling):
    """The other arm. Without it, a `signal_matches` that returned `True`
    unconditionally would pass every test above."""
    assert not signal_matches(NOT_CONTRACT_YEAR, {"is_contract_year": spelling})


@pytest.mark.parametrize("spelling", ["false", "False", "0", "no"])
def test_a_false_filter_selects_the_players_with_time_left(spelling):
    assert signal_matches(NOT_CONTRACT_YEAR, {"is_contract_year": spelling})
    assert not signal_matches(CONTRACT_YEAR, {"is_contract_year": spelling})


@pytest.mark.parametrize("asked", ["true", "false"])
def test_an_unknown_term_answers_NEITHER_question(asked):
    """ "We do not know when this deal ends" is not "this is not a contract
    year". A row that matched `false` would put every unsourced term into the
    has-time-left bucket, which is a claim this collector cannot make."""
    assert not signal_matches(UNKNOWN_TERM, {"is_contract_year": asked})


def test_a_null_team_is_not_selected_by_the_string_None():
    """`str(None)` is `"None"`. Without the null arm, `?team=None` would select
    every row whose club the upstream left ambiguous — 61 of them — which is a
    working-looking query for a team that does not exist."""
    assert not signal_matches(UNKNOWN_TERM, {"team": "None"})
    assert not signal_matches(UNKNOWN_TERM, {"team": "GB"})


def test_a_team_filter_narrows_by_exact_abbreviation():
    assert signal_matches(CONTRACT_YEAR, {"team": "GB"})
    assert not signal_matches(CONTRACT_YEAR, {"team": "BUF"})


def test_a_player_id_filter_narrows():
    assert signal_matches(CONTRACT_YEAR, {"player_id": "fdy-aaaa11112222"})
    assert not signal_matches(CONTRACT_YEAR, {"player_id": "fdy-bbbb11112222"})


def test_filters_combine_with_AND():
    assert signal_matches(CONTRACT_YEAR, {"team": "GB", "is_contract_year": "true"})
    assert not signal_matches(
        CONTRACT_YEAR, {"team": "BUF", "is_contract_year": "true"}
    )
    assert not signal_matches(
        CONTRACT_YEAR, {"team": "GB", "is_contract_year": "false"}
    )
