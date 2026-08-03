"""Scale agreement with `defensive-front`, demonstrated rather than asserted.

The spec's warning is the reason this file exists:

> All shared-stem metrics must be emitted on the same scale ... because any
> divergence **silently corrupts the differential rather than failing**.

The generator's matchup feature is a plain subtraction —
`front.<metric>_generated_adj − line.<metric>_allowed_adj`, joined on
`(season, week, team_id)` with no unit conversion — so a divergence in a
denominator, a weighting constant or a rounding rule produces a number that is
wrong by a factor nobody can see. Nothing raises. Nothing validates
differently. The two collectors simply stop meaning the same thing.

**Read by AST, never imported.** `defensive-front` is a separate uv workspace
member; importing it from here would need it installed in this service's
environment, which it is not and must not be. The same technique
`tests/test_collector_registry.py` uses on every collector's descriptor: parse
the sibling module, evaluate its module-level constants, and compare.

`defensive_front/ratings.py`'s own docstring says outright that
"`offensive-line` must import the identical weighting". It cannot literally
import it, so this file is the enforcement that stands in for the import — and
unlike an import, it fails on the *sibling* changing too.
"""

import ast
from pathlib import Path

import pytest

from offensive_line import ratings

SIBLING = (
    Path(__file__).resolve().parents[3]
    / "services"
    / "defensive-front"
    / "defensive_front"
    / "ratings.py"
)


def _module_constants(path: Path) -> dict[str, object]:
    """Module-level `NAME = <literal or simple arithmetic>` from a source file.

    Literals through `ast.literal_eval`, and anything built out of earlier
    constants through a restricted evaluation over the table built so far —
    which is what `LINE_YARDS_CAP` needs, since it is deliberately derived
    from the bands rather than written down.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    table: dict[str, object] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        try:
            table[target.id] = ast.literal_eval(node.value)
            continue
        except (ValueError, SyntaxError, TypeError):
            pass
        try:
            table[target.id] = eval(  # noqa: S307 — restricted, see below
                compile(ast.Expression(node.value), str(path), "eval"),
                {"__builtins__": {}},
                dict(table),
            )
        except Exception:  # noqa: BLE001 — a constant we cannot resolve is skipped
            continue
    return table


@pytest.fixture(scope="module")
def sibling() -> dict[str, object]:
    assert SIBLING.exists(), f"{SIBLING} is gone; the pairing has no other side"
    return _module_constants(SIBLING)


# --------------------------------------------------------------------------
# The constants
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "LINE_YARDS_LOSS_CREDIT",
        "LINE_YARDS_FULL_MAX",
        "LINE_YARDS_HALF_MAX",
        "LINE_YARDS_HALF_CREDIT",
        "LINE_YARDS_CAP",
    ],
)
def test_the_line_yards_weighting_matches_defensive_front(sibling, name):
    """`adjusted_line_yards` and `adjusted_line_yards_allowed` are differenced
    with no conversion, so every band of the weighting has to agree."""
    assert name in sibling, f"{name} vanished from defensive-front"
    assert getattr(ratings, name) == sibling[name], name


@pytest.mark.parametrize("name", ["MIN_OPPONENT_STRENGTH", "PRECISION"])
def test_the_adjustment_parameters_match_defensive_front(sibling, name):
    """`PRECISION` is not cosmetic here. Two sides rounding a rate differently
    put phantom hundredths into a subtraction that is supposed to be exact,
    and `MIN_OPPONENT_STRENGTH` decides when a raw value is published in place
    of an adjusted one — a threshold that differed would make one side of the
    differential adjusted and the other side raw, for the same game."""
    assert getattr(ratings, name) == sibling[name], name


def test_the_two_adjustment_methods_are_the_same_arithmetic_named_per_side(
    sibling,
):
    """Mirrored identifiers, not identical ones: a lake reader has to be able
    to tell which side of the ball an object came from, and the method string
    is the only thing on the row that says which yardstick was used."""
    assert ratings.ADJUSTMENT_METHOD == "opposing_defense_production_ratio_loo_v1"
    assert sibling["ADJUSTMENT_METHOD"] == "opposing_offense_production_ratio_loo_v1"
    # Same method, same version, differing only in the side of the ball the
    # yardstick is fit on. The suffix is what a lake reader compares to know
    # the two objects were built by the same arithmetic.
    assert (
        ratings.ADJUSTMENT_METHOD.split("_")[2:]
        == (sibling["ADJUSTMENT_METHOD"].split("_")[2:])
    )


# --------------------------------------------------------------------------
# The curve, not just its parameters
# --------------------------------------------------------------------------


def _sibling_callable(path: Path, name: str, constants: dict[str, object]):
    """The sibling's OWN function, compiled and made callable.

    **Not a reconstruction.** An earlier revision of this file rebuilt the
    shape of `defensive_front.line_yards` from this collector's source while
    reading only the sibling's *constants*, which made a "numerically, every
    tenth of a yard" test into a constants test wearing a behaviour test's
    clothes. Three mutations of the sibling's body — the band boundary moved,
    the cap replaced by `0.0` (every carry past ten yards worth nothing), and
    the loss credit's sign flipped — all survived this collector's entire
    suite. That is the precise failure the pairing constraint exists to
    prevent, since the generator subtracts the two collectors' `_adj` columns.

    So the sibling's `FunctionDef` is lifted out of its module's AST and
    executed in a namespace built from that same module's constants. Nothing
    about the curve's shape comes from this side any more.

    `exec` on a repo-local source file this test already parses, in a
    namespace with no builtins beyond what the function needs. It is not
    importable: `defensive-front` is a separate uv workspace member and is not
    installed in this service's environment, which is why the whole file works
    by AST in the first place.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            # `float` is bound because the signature's annotations are
            # evaluated at definition time; nothing else from builtins is.
            namespace: dict[str, object] = {
                "__builtins__": {},
                "float": float,
                **constants,
            }
            exec(  # noqa: S102 — repo-local source, restricted namespace
                compile(ast.Module(body=[node], type_ignores=[]), str(path), "exec"),
                namespace,
            )
            return namespace[name]
    raise AssertionError(f"{name} is not defined in {path}")


def test_the_two_line_yards_curves_agree_across_the_whole_range(sibling):
    """The sibling's own curve against ours, at every tenth of a yard from a
    20-yard loss to a 99-yard gain.

    Kept **alongside** the AST comparison below rather than replaced by it,
    and it earns its place: the AST test fails on a semantically identical
    refactor (the sibling rewriting the bands as a loop, say), where this one
    passes. Between them, a behavioural divergence is caught twice and a
    cosmetic one is caught once and can be argued about.
    """
    theirs = _sibling_callable(SIBLING, "line_yards", sibling)
    for tenths in range(-200, 991):
        yards = tenths / 10.0
        assert ratings.line_yards(yards) == pytest.approx(theirs(yards)), yards


# --------------------------------------------------------------------------
# The adjustment's arithmetic, and the denominator it runs over
# --------------------------------------------------------------------------


def _body_without_docstring(path: Path, name: str) -> str:
    """One function's body as a normalised AST dump, docstring removed.

    Comments and formatting are invisible to `ast.dump`, so this compares what
    the two functions *do* rather than how they are written — which is the
    right granularity: the two modules' docstrings and comments necessarily
    differ, and their arithmetic necessarily must not.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            body = list(node.body)
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                body = body[1:]
            return "\n".join(ast.dump(statement) for statement in body)
    raise AssertionError(f"{name} is not defined in {path}")


def _side_neutral(dump: str) -> str:
    """Rename the side-of-ball local so two mirrored functions can be compared.

    `_faced_strength` iterates `(defense, game)` in `defensive-front` and
    `(offense, game)` here, because in each collector that local names the
    unit being rated. That is the one difference the mirror *requires*, and
    normalising it is what keeps this comparison about arithmetic. Nothing
    else is normalised — a changed operator, operand or ordering still fails.
    """
    return dump.replace("'defense'", "'unit'").replace("'offense'", "'unit'")


OURS = Path(ratings.__file__)


@pytest.mark.parametrize(
    "name", ["line_yards", "opponent_strengths", "_faced_strength", "_adjust"]
)
def test_the_adjustment_arithmetic_is_identical_to_defensive_fronts(name):
    """**The leave-one-out adjustment, statement for statement.**

    Matching constants is not enough: the two adjusted columns are subtracted
    from each other, so `raw / strength` on one side and `raw * (2 - strength)`
    on the other would agree on every constant in this file and still produce
    a differential that is wrong by an amount that varies with the schedule.
    Compared as ASTs, so the two modules' comments and docstrings — which
    describe opposite sides of the ball and must differ — do not enter into
    it.

    If this ever has to diverge, the divergence belongs in a shared library
    rather than in a weakened assertion here.
    """
    assert _side_neutral(_body_without_docstring(OURS, name)) == _side_neutral(
        _body_without_docstring(SIBLING, name)
    ), name


async def test_the_two_collectors_count_the_same_denominator():
    """`pass_block_snaps` here and `pass_rush_snaps` there are **the same
    number**, counted from opposite sides of one play set.

    That is the precondition for differencing two rates without conversion,
    and it is structural rather than agreed: `_fold` increments both from the
    same intersection of play-by-play dropbacks and charted pass rushes. A
    collector that counted all dropbacks on one side and charted snaps on the
    other would produce two plausible rates whose difference means nothing.
    """
    import httpx
    import respx

    from offensive_line.adapters import participation as participation_adapter
    from offensive_line.adapters import pbp as pbp_adapter
    from offensive_line.capture import _fold

    from .conftest import SEASON, WEEK, Feeds

    feeds = Feeds()
    with respx.mock(assert_all_called=False) as router:
        feeds.install(router)
        async with httpx.AsyncClient() as client:
            fold = await pbp_adapter.fetch_pbp(SEASON, WEEK, client=client)
            charted = await participation_adapter.fetch_block_snaps(
                SEASON, client=client
            )
    totals = _fold(fold, charted)

    assert totals.offense_game, "nothing was folded"
    for (team, game), blocking in totals.offense_game.items():
        opponent = totals.opponents[(team, game)]
        rushing = totals.defense_game[(opponent, game)]
        assert blocking.pass_block_snaps == rushing.pass_rush_snaps, (team, game)
        assert blocking.pressures_allowed == rushing.pressures, (team, game)
        assert blocking.sacks_allowed == rushing.sacks, (team, game)
        assert blocking.carries == rushing.carries_faced, (team, game)
        assert blocking.line_yards == pytest.approx(rushing.line_yards_allowed)


async def test_a_nullified_snap_is_charted_but_is_not_a_pass_block_snap():
    """**The intersection, and the 5.24% it is worth.**

    Penalty-nullified `no_play` rows are charted pass rushes that
    play-by-play does not call dropbacks. They can carry a pressure and can
    never carry a sack, so folding participation without intersecting it
    against the dropback index inflates every pressure denominator and
    deflates every sack rate — by 5.24% on the real 2025 season, with every
    field populated and plausible throughout.

    `defensive-front` takes the same intersection, so this is a pairing claim
    as much as a correctness one: a collector that counted them would put
    `pass_block_snaps` and `pass_rush_snaps` on two different play sets.
    """
    import httpx
    import respx

    from offensive_line.adapters import participation as participation_adapter
    from offensive_line.adapters import pbp as pbp_adapter
    from offensive_line.capture import _fold

    from . import season as season_module
    from .conftest import SEASON, WEEK, Feeds

    feeds = Feeds()
    with respx.mock(assert_all_called=False) as router:
        feeds.install(router)
        async with httpx.AsyncClient() as client:
            fold = await pbp_adapter.fetch_pbp(SEASON, WEEK, client=client)
            charted = await participation_adapter.fetch_block_snaps(
                SEASON, client=client
            )
    totals = _fold(fold, charted)

    per_team_game = season_module.SNAPS_PER_GAME
    nullified = season_module.NO_PLAYS_PER_GAME
    assert nullified > 0, "the fixture no longer carries a nullified snap"

    for (team, game), blocking in totals.offense_game.items():
        assert blocking.pass_block_snaps == per_team_game, (team, game)
        # The nullified snaps were charted as pressures, so counting them
        # would show up here as well as in the denominator.
        assert blocking.pressures_allowed <= per_team_game

    charted_for_one = per_team_game + nullified
    assert charted_for_one > per_team_game, (
        "the fixture must chart more rush snaps than there are dropbacks, or "
        "the intersection is not being exercised at all"
    )


def test_the_sibling_source_actually_defines_the_curve_we_reconstructed(sibling):
    """A guard on this file's own method. If `defensive-front` stopped having
    a `line_yards` function, every comparison above would still pass while
    comparing against a shape nothing computes."""
    tree = ast.parse(SIBLING.read_text(encoding="utf-8"))
    functions = {node.name for node in tree.body if isinstance(node, ast.FunctionDef)}
    assert "line_yards" in functions
