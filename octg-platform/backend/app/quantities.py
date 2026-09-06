"""One rule for every quantity that enters the platform.

WHY THIS MODULE EXISTS
======================
Every quantity here is arithmetic input: it is summed into MRP, compared against
on-hand stock to decide coverage, and decremented month by month in the runout.
Until 2026-09-06 the API schemas typed them as bare `float`, which meant three
things reached the database that no engine can do arithmetic on:

  * a NEGATIVE demand (-100 Mtr), which coverage then judged "Covered";
  * positive INFINITY on an on-hand row, because JSON `1e309` parses to inf and
    the only check was `quantity < 0`;
  * fractional pieces (12.5 PC) for units that count whole objects.

The three Excel parsers each had their own finite check and their own sign rule,
the API writers each had their own `< 0`, and the scenario engine had a third
opinion. Same idea, five spellings, three holes. This module is the one spelling.

THE RULE (product-owner ruling, 2026-09-06)
============================================
  finite            NaN and ±inf are refused everywhere, at the schema.
  bounded           <= MAX_QUANTITY. Nothing on this platform is a billion of
                    anything; a figure above it is a typo or an attack.
  demand > 0        A demand line for nothing is not demand -- removing a line
                    is a different operation, and it must not be spelled "0".
  stock >= 0        Inventory, assignments, on-order and safety stock may be 0:
                    "we counted and hold none" is a fact the model must be able
                    to state, and it is distinct from "no row = unknown"
                    (design principle #1).
  PC / JT integer   Pieces and joints are whole objects. Mtr, FT and MT are
                    continuous. A half-piece is refused, not rounded.

Sign and integrality depend on WHAT the number is and WHICH product it counts,
so the schema-level type only fixes finiteness, range and non-negativity; each
writer calls `validate_quantity` once the product is known.
"""

from __future__ import annotations

import math
from typing import Annotated, Literal

from pydantic import Field

from app.models.product import UnitOfMeasure

MAX_QUANTITY = 1_000_000_000.0
INTEGER_UNITS = frozenset({UnitOfMeasure.PC, UnitOfMeasure.JT})

QuantityKind = Literal["demand", "stock"]

#: Schema-level type for every quantity a request body carries. Finite, within
#: range, non-negative. Pydantic answers 422 before any handler runs, so junk
#: like 1e309 never reaches a session. The unit-aware half of the rule lives in
#: `validate_quantity`, applied by the writer once the product is known.
Quantity = Annotated[float, Field(ge=0, le=MAX_QUANTITY, allow_inf_nan=False)]


class InvalidQuantity(ValueError):
    """The value is not a quantity this platform can do arithmetic on."""


def _unit_value(unit: UnitOfMeasure | str | None) -> UnitOfMeasure | None:
    if unit is None:
        return None
    if isinstance(unit, UnitOfMeasure):
        return unit
    try:
        return UnitOfMeasure(unit)
    except ValueError:
        return None


def validate_quantity(
    value: float,
    *,
    kind: QuantityKind,
    unit: UnitOfMeasure | str | None,
    label: str = "quantity",
) -> float:
    """Return `value` as a float if it is a legal quantity of `kind` in `unit`.

    Raises `InvalidQuantity` with a sentence a planner can act on. `unit` may be
    None when the product is genuinely unknown (a parser that has not resolved
    the row yet); the integer rule is then skipped and must be applied later.
    """
    if isinstance(value, bool):
        raise InvalidQuantity(f"{label} {value!r} is not a number")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise InvalidQuantity(f"{label} {value!r} is not a number") from None
    if math.isnan(number) or math.isinf(number):
        raise InvalidQuantity(f"{label} is not a finite number")
    if number > MAX_QUANTITY:
        raise InvalidQuantity(
            f"{label} {number:g} exceeds the platform maximum of {MAX_QUANTITY:,.0f}. "
            "Nothing on this platform is a billion of anything; check the figure."
        )
    if kind == "demand":
        if number <= 0:
            raise InvalidQuantity(
                f"{label} must be greater than zero, got {number:g}. A demand line "
                "for nothing is not demand -- to remove a line, remove it rather "
                "than revising it to zero."
            )
    else:
        if number < 0:
            raise InvalidQuantity(
                f"{label} cannot be negative, got {number:g}. Nothing can be held, "
                "assigned or ordered in a negative amount; use 0 to state that "
                "there is none."
            )
    resolved = _unit_value(unit)
    if resolved in INTEGER_UNITS and number != math.floor(number):
        raise InvalidQuantity(
            f"{label} {number:g} is not a whole number, and this product is counted "
            f"in {resolved.value} -- whole {'pieces' if resolved == UnitOfMeasure.PC else 'joints'} "
            "only. Half a piece is not a quantity; it is not rounded for you."
        )
    return number


def parse_quantity_cell(
    cell: object,
    *,
    kind: QuantityKind,
    unit: UnitOfMeasure | str | None,
    label: str = "quantity",
) -> tuple[float | None, str | None]:
    """Coerce one spreadsheet cell to a quantity, returning (value, error).

    The coercion the three Excel importers used to spell separately: empty is an
    error, booleans are refused (openpyxl hands TRUE/FALSE through as bool),
    thousands separators and stray spaces are stripped from text. The rule itself
    is `validate_quantity`, so a workbook and a request body are held to the same
    standard.
    """
    if cell is None or (isinstance(cell, str) and not cell.strip()):
        return None, f"{label} is empty"
    if isinstance(cell, bool):
        return None, f"{label} {cell!r} is not a number"
    if isinstance(cell, (int, float)):
        raw: float | str = float(cell)
    else:
        raw = str(cell).strip().replace(",", "").replace(" ", "")
        try:
            raw = float(raw)
        except ValueError:
            return None, f"{label} {str(cell).strip()!r} is not a number"
    try:
        return validate_quantity(raw, kind=kind, unit=unit, label=label), None
    except InvalidQuantity as exc:
        return None, str(exc)
