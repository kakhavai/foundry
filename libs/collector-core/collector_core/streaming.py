"""Reading a large upstream document without holding it in memory.

Two rules fell out of `roster-scope` being `OOMKilled` at a 256Mi limit on its
first deploy — exit 137, `CrashLoopBackOff`, probes reporting `connection
refused` because the process was simply gone. Neither its 171 passing tests nor
a local `docker run` could see it, because neither had a memory limit.

**1. Never hold an upstream response in memory more than once.** `resp.text`
plus a decode plus a `io.StringIO` copy handed to `csv` is three copies of the
document. At 36.8 MB that is over 110 MB before a single row is mapped.

**2. Filter to what you actually keep as you parse, not after.** `roster-scope`
took 300,000 rows in and retained 983; materializing the other 299,017 first
peaked at 38.5 MiB of pure waste.

`stream_csv_dicts` is rule 1 made reusable, and it hands rows out one at a time
so a caller can obey rule 2 with an ordinary `continue`. Raising the memory
limit instead is the wrong fix and was deliberately reverted during 8A: it
hides the bug and re-sizes the pod against an upstream nobody controls.
"""

import csv
import logging
from collections.abc import AsyncIterator

import httpx

logger = logging.getLogger(__name__)

# A ceiling on how much a collector will pull before giving up. Not a memory
# guard -- streaming already bounds that -- but a guard against an upstream
# that starts serving something unbounded, so a capture cannot download
# forever inside its deadline.
MAX_UPSTREAM_CHARS = 256 * 1024 * 1024


class UpstreamTooLarge(ValueError):
    """The upstream exceeded its character ceiling before it finished."""


class UpstreamSchemaError(ValueError):
    """The document did not carry the columns the caller depends on.

    Subclasses `ValueError` so `CollectorMetrics.reason_for` classifies it as
    `malformed`, per the Phase 8 failure-handling contract: an upstream that
    renames a field must fail the capture loudly rather than map nulls into an
    append-only lake that is never rewritten.
    """


def _split_line(line: str) -> list[str] | None:
    """One CSV line to fields, or `None` for a blank one.

    Parsed a line at a time rather than by handing `csv` the whole document,
    which is what keeps memory flat. The trade-off is that a quoted field
    containing a literal newline would be split across two rows; the feeds
    this reads carry timestamps, team codes, ids, position codes and player
    names, none of which can contain one. A feed that can must not use this.
    """
    line = line.rstrip("\r")
    if not line:
        return None
    return next(csv.reader([line]))


async def stream_csv_dicts(
    client: httpx.AsyncClient,
    url: str,
    *,
    required_columns: frozenset[str] | set[str] | None = None,
    max_chars: int = MAX_UPSTREAM_CHARS,
    follow_redirects: bool = True,
) -> AsyncIterator[dict[str, str]]:
    """Stream a CSV document, yielding one header-keyed dict per row.

    Peak memory is one chunk plus one row, independent of the document's size.
    The caller filters as it iterates, so nothing it does not keep is ever
    retained.

    `required_columns`, when given, is asserted against the header before any
    row is yielded — schema drift fails immediately rather than after a
    million rows have been mapped to nulls.
    """
    header: list[str] | None = None
    consumed = 0
    remainder = ""

    async with client.stream("GET", url, follow_redirects=follow_redirects) as response:
        response.raise_for_status()
        async for chunk in response.aiter_text():
            consumed += len(chunk)
            if consumed > max_chars:
                raise UpstreamTooLarge(
                    f"{url} exceeded {max_chars} characters before it finished"
                )
            remainder += chunk
            lines = remainder.split("\n")
            # The last element is a partial line unless the chunk happened to
            # end on a boundary; either way it belongs to the next chunk.
            remainder = lines.pop()
            for line in lines:
                fields = _split_line(line)
                if fields is None:
                    continue
                if header is None:
                    header = _validated_header(fields, required_columns, url)
                    continue
                yield _row(header, fields)

    trailing = _split_line(remainder)
    if trailing is not None:
        if header is None:
            header = _validated_header(trailing, required_columns, url)
        else:
            yield _row(header, trailing)

    if header is None:
        raise UpstreamSchemaError(f"{url} returned an empty document")


def _validated_header(
    fields: list[str],
    required_columns: frozenset[str] | set[str] | None,
    url: str,
) -> list[str]:
    if required_columns:
        missing = set(required_columns) - set(fields)
        if missing:
            raise UpstreamSchemaError(
                f"{url} is missing column(s): {', '.join(sorted(missing))}"
            )
    return fields


def _row(header: list[str], fields: list[str]) -> dict[str, str]:
    """Header-keyed, tolerating a short row.

    A row with fewer fields than the header gets empty strings rather than
    raising: `csv.DictReader` fills with `None`, and every caller here treats
    an absent value as empty anyway. A row with *more* fields than the header
    keeps only the named ones.
    """
    return {
        name: fields[index] if index < len(fields) else ""
        for index, name in enumerate(header)
    }
