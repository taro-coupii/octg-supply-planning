"""Length/mass conversion to metric tonnes -- for the Executive Dashboard ONLY.

MVP-COMPROMISE[C-05]: introducing a length<->mass conversion at all.
    WHY:    `app.models.product.UnitOfMeasure`'s docstring used to say, correctly
            at the time, "NOT a conversion table... any factor stored here would
            be a plausible-looking invention". That objection assumed there was no
            per-product fact to convert from. `Product.weight` (lb/ft, the API
            nominal weight) IS such a fact -- it is measured per product, not
            invented as a platform-wide constant -- so a length<->mass conversion
            for a SPECIFIC product is no longer a fabrication in the sense that
            docstring meant.
    REMOVE: N/A to remove outright; the boundary below is what must never widen.
            See MVP_COMPROMISES.md C-05 for the boundary review to redo whenever
            this module gains a new caller.

THE BOUNDARY THIS MODULE MUST NEVER CROSS
------------------------------------------
This conversion is confined to the Executive Dashboard's PRESENTATION layer
(`app.engines.executive`, the inventory-utilisation block). Coverage, MRP and
every demand-line comparison stay in native units, on purpose: coverage compares
on-hand against needed DIRECTLY, in the unit both were recorded in, and a rounded
metric-tonnes re-expression of either side could flip a COVERED verdict to
UNCOVERED (or the reverse) for a shortfall that only exists in the rounding. A
dashboard headline can absorb that; a verdict that drives an order cannot. If a
future caller outside `app.engines.executive` wants this function, that is the
signal to re-open MVP_COMPROMISES.md C-05, not to assume the boundary was always
meant to be crossed.

WHAT IS AND IS NOT CONVERTIBLE
-------------------------------
`FT` and `MTR` convert through `Product.weight` (lb/ft): a weight-per-length fact
per product turns a length into a mass. `MT` passes through unchanged -- it is
already mass. `JT` (joints) converts to feet first via `JOINT_LENGTH_FEET`, an
ASSUMED average joint length -- see MVP-COMPROMISE[C-04] below; that step, unlike
the FT/MTR ones, remains an invention. `PC` (pieces) is PERMANENTLY unconvertible:
a piece has neither a stated length nor a stated weight, so there is no factor to
apply and there never will be one for this unit -- this is not an MVP gap to
close, it is a fact about what "one piece" means.

A `Product.weight` of `None` (nullable -- see MVP-COMPROMISE[C-06] at the call
site below) is also unconvertible, but for a different reason than PC: the
product COULD in principle convert, the fact required to do so simply has not
been recorded yet.

Never a fabricated 0.0. Whenever a quantity cannot be converted,
`TonnesResult.tonnes` is `None` and `available` is `False` -- exactly the
"measured or explicitly unavailable" rule the rest of this platform enforces
everywhere else a number might otherwise be silently invented.
"""

from dataclasses import dataclass

from app.models.product import UnitOfMeasure

#: 1 lb = this many kg. Used to convert `Product.weight` (lb/ft) into a metric mass.
POUNDS_TO_KILOGRAMS = 0.453592
#: 1 ft = this many metres. Used to convert a per-foot weight into a per-metre one.
METRES_PER_FOOT = 0.3048
#: 1 tonne = this many kg.
KILOGRAMS_PER_TONNE = 1000.0
#: ASSUMED average joint length, in feet, used ONLY to turn a JT quantity into a
#: length before applying the FT formula.
#:
#: MVP-COMPROMISE[C-04]: `1 JT = 40 ft` is a provisional average, not a measured
#: fact of any specific joint.
#:     WHY:    the real average joint length is set by API Range (R1/R2/R3) and by
#:             actual mill tolerance on the joints delivered, neither of which this
#:             platform records per product or per incoming lot. The user-supplied
#:             figures were "1 JT = 40 feet" and "1 JT = 12 mtr", and those two do
#:             NOT agree: 40 ft = 12.192 m, a 1.6% difference. Feet was chosen
#:             because `Product.weight` is already expressed per FOOT, so
#:             converting JT -> ft -> tonnes is one fewer conversion (and one fewer
#:             rounding step) than JT -> m -> ft -> tonnes would be.
#:     REMOVE: carry a real average (or per-Range, or per-lot) joint length instead
#:             of one constant. It is deliberately isolated to this single name so
#:             that replacement is a one-line change.
JOINT_LENGTH_FEET = 40.0  # MVP-COMPROMISE[C-04]

_PC_REASON = (
    "PC (pieces) has no length and no weight -- there is no conversion factor for "
    "it and there never will be one; this is a permanent property of the unit, not "
    "a gap to close."
)


@dataclass(frozen=True)
class TonnesResult:
    """A metric-tonnes figure that may not exist. Mirrors
    `app.engines.executive.Measure`'s "available or explicitly not" shape -- never
    a fabricated 0.0 for "could not convert".
    """

    available: bool
    tonnes: float | None
    reason: str | None = None


def to_metric_tonnes(
    unit: UnitOfMeasure, quantity: float, weight_lb_per_ft: float | None
) -> TonnesResult:
    """Convert `quantity` (in `unit`) to metric tonnes, or say why it cannot be.

    See the module docstring for the boundary this function must stay behind
    (Executive Dashboard presentation only) and for which units convert and why.
    """
    if unit == UnitOfMeasure.PC:
        # Permanent, not an MVP gap -- see the module docstring. No marker here on
        # purpose: this refusal will still be correct after every other compromise
        # in this file is retired.
        return TonnesResult(available=False, tonnes=None, reason=_PC_REASON)

    if unit == UnitOfMeasure.MT:
        # Already a mass -- no weight fact is needed to state that. Checked
        # before the weight-is-None guard below so an MT product with no stated
        # weight (irrelevant to this unit) still converts.
        return TonnesResult(available=True, tonnes=quantity)

    if weight_lb_per_ft is None:
        # MVP-COMPROMISE[C-06]: `Product.weight` is nullable. Every seeded product
        # currently has one, so this branch is untested by the demo data alone --
        # but a real catalogue WILL eventually contain a product nobody has
        # weighed, and that quantity must report unconvertible, never a silent 0 t.
        return TonnesResult(
            available=False,
            tonnes=None,
            reason=(
                "Product.weight has never been stated for this product (unknown, "
                "not zero), so its quantity cannot be converted to metric tonnes."
            ),
        )

    if unit == UnitOfMeasure.FT:
        tonnes = quantity * weight_lb_per_ft * POUNDS_TO_KILOGRAMS / KILOGRAMS_PER_TONNE
        return TonnesResult(available=True, tonnes=tonnes)

    if unit == UnitOfMeasure.MTR:
        tonnes = (
            quantity
            * weight_lb_per_ft
            * POUNDS_TO_KILOGRAMS
            / METRES_PER_FOOT
            / KILOGRAMS_PER_TONNE
        )
        return TonnesResult(available=True, tonnes=tonnes)

    if unit == UnitOfMeasure.JT:
        # MVP-COMPROMISE[C-04]: route through the assumed average joint length,
        # then apply the exact FT formula on the resulting feet.
        feet = quantity * JOINT_LENGTH_FEET
        tonnes = feet * weight_lb_per_ft * POUNDS_TO_KILOGRAMS / KILOGRAMS_PER_TONNE
        return TonnesResult(available=True, tonnes=tonnes)

    raise ValueError(f"Unhandled UnitOfMeasure {unit!r}")
