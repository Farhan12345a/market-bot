"""
Keep an append-only CSV's header honest when its schema grows.

The bug this exists to prevent
------------------------------
Both CSV writers in this project decide `write_header = not path.exists()`, so
the header is written once - when the file is first created - and never looked
at again. Every column added afterwards is written into the DATA rows but never
named in the header.

The file does not become corrupt, it becomes UNREADABLE BY NAME. On 2026-08-26
logs/signal_journal.csv had a 19-column header over 32-column rows, so the
thirteen columns added since the file was created - including every continuation
factor and cf_score - were invisible to csv.DictReader. The continuation feature
looked like it had never recorded anything when in fact it had recorded
everything. logs/trade_history.csv had the same fault: a 14-column header over
21-column rows, which put the burst note under entry_rsi.

Why the repair maps by NAME and not by position
-----------------------------------------------
The obvious repair - pad short rows on the right - is WRONG here, and quietly
so. Neither schema grew by appending. The signal journal's new columns were
inserted at index 11, before `taken`; trade history's went in after
`entry_method`. An old 19-column row therefore does NOT hold the first 19
values of the 32-column schema, and padding it would leave `taken` sitting
under `opening_hit_rate` and every later value one to twelve columns out of
place. Corrupting the file while reporting success is worse than the stale
header it set out to fix.

So old rows are read under the header they were written with - which is exactly
what the stale header on disk is - and rewritten by column NAME into the new
order. Any column the old schema lacked is written blank, which is honest: that
data was never recorded.

Rows already at the new width are passed through untouched. A row at no known
width is left alone and reported, because a row this code cannot identify is a
row it must not rewrite.

Why a schema HISTORY is needed
------------------------------
Knowing only the on-disk header and the current schema is not enough once a file
has lived through more than one change. Adding the sector columns on 2026-08-26
put three generations in one file at once: a 19-column header, 32-column rows
from earlier that same day, and a 34-column schema. The 32-column rows matched
neither end and would have been copied verbatim - silently reintroducing exactly
the fault this module exists to fix, one day after fixing it.

So callers pass every past version of their schema. Each row is matched by width
against the on-disk header, then the known history, then the current schema.
"""

import csv
import logging
import os
import shutil

logger = logging.getLogger(__name__)


def read_header(path):
    """The header currently on disk, or None if the file is absent/empty."""
    try:
        if not os.path.exists(path) or os.path.getsize(path) == 0:
            return None
        with open(path, newline="") as f:
            for row in csv.reader(f):
                return row
    except Exception as e:
        logger.debug(f"could not read header of {path}: {e}")
    return None


def remap_row(row, old_fields, new_fields):
    """One row written under old_fields, re-expressed under new_fields."""
    values = dict(zip(old_fields, row))
    return [values.get(name, "") for name in new_fields]


def repair_header(path, fieldnames, legacy_schemas=()):
    """
    Make `path` match `fieldnames`, rewriting it in place if it does not.

    legacy_schemas: past versions of this schema, oldest first. Needed whenever
    a file may contain rows from more than one generation - see the module
    docstring.

    Returns True if a repair happened. No-ops when the file is missing, empty,
    or already correct.
    """
    fieldnames = list(fieldnames)
    header = read_header(path)
    if header is None or header == fieldnames:
        return False

    # Every name in the old header must exist in the new schema. If one does
    # not, a column was RENAMED or REMOVED rather than added, and no automatic
    # mapping can be trusted - a stale header is a nuisance, guessing where a
    # vanished column's data belongs is data loss.
    unknown = [c for c in header if c not in fieldnames]
    if unknown:
        logger.error(
            f"{path}: on-disk header has column(s) absent from the current "
            f"schema ({', '.join(unknown)}) - refusing to rewrite. Columns "
            f"added since the file was created stay unreadable by name until "
            f"this is resolved by hand."
        )
        return False

    try:
        with open(path, newline="") as f:
            rows = list(csv.reader(f))
        if len(rows) < 1:
            return False

        old_width, new_width = len(header), len(fieldnames)

        # width -> the schema a row of that width was written under. Built
        # oldest-first so a later generation wins any width collision: two
        # schemas of equal width mean one replaced the other, and the newer
        # one is what recent rows were written with.
        by_width = {old_width: header}
        for schema in legacy_schemas or ():
            schema = list(schema)
            if all(c in fieldnames for c in schema):
                by_width[len(schema)] = schema
            else:
                logger.warning(
                    f"{path}: ignoring a declared legacy schema with column(s) "
                    f"absent from the current one"
                )
        by_width[new_width] = fieldnames

        remapped = passed = odd = 0

        backup = f"{path}.bak"
        shutil.copy2(path, backup)

        with open(path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(fieldnames)
            for row in rows[1:]:               # rows[0] is the stale header
                if not row:
                    continue
                schema = by_width.get(len(row))
                if schema is None:
                    writer.writerow(row)       # unidentifiable - do not touch
                    odd += 1
                elif len(row) == new_width:
                    writer.writerow(row)       # already current
                    passed += 1
                else:
                    writer.writerow(remap_row(row, schema, fieldnames))
                    remapped += 1

        logger.info(
            f"{path}: header repaired {old_width} -> {new_width} columns "
            f"({remapped} older rows remapped by name across "
            f"{len(set(by_width)) - 1} generation(s), {passed} already current"
            + (f", {odd} of unrecognised width left as-is" if odd else "")
            + f"; previous file saved as {backup})"
        )
        if odd:
            logger.warning(
                f"{path}: {odd} row(s) matched no known schema width "
                f"(known: {sorted(by_width)}) and were copied verbatim - "
                f"they will still misread. Inspect {backup} if that matters."
            )
        return True
    except Exception as e:
        logger.error(f"could not repair header of {path}: {e}")
        return False
