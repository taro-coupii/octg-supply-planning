import enum

from sqlalchemy import Boolean, Column, Enum as SAEnum, Float, String

from app.db import Base
from app.models.customer import _uuid


class UnitOfMeasure(str, enum.Enum):
    """How a product's quantity is counted. A CLOSED set, deliberately.

    Why an enum rather than free text
    ---------------------------------
    The same argument `LeadTimeDimension` makes, applied to quantity. Every
    quantity in this platform is ARITHMETIC INPUT, not decoration: it is summed
    into MRP recommendation rows, compared against `InventoryOnHand.quantity` to
    decide coverage, decremented month by month in `app.engines.mrp._runout_series`
    and totalled on the Executive Dashboard. Each of those operations is only
    meaningful inside ONE dimensional system, so "which system is this number in"
    is a modelling fact the engines have to be able to branch on -- and a closed
    set is what makes the branch writable at all.

    Free text would make the honesty rule unenforceable rather than merely untidy.
    The rule this project now holds itself to is "never silently sum across units"
    (see `app.engines.executive.quantity_by_unit`), and that rule is implemented by
    GROUPING quantities on this value. With free text, "Mtr", "mtr", "m", "M" and
    "metre" are five groups describing one physical unit, so a genuinely
    single-unit total would silently fragment into five partial totals -- and,
    worse, a typo'd "MT" on a casing row would silently move real tonnage into the
    tonnes bucket where nobody would question it. That is the same failure mode
    `LeadTimeDimension`'s docstring describes for its retired free-text
    `component` column: a typo creating a new term nobody could find.

    The three members are the three quantity columns the source workbook actually
    carries, and no more:

      MTR  Metres. Tubulars are bought and issued by length; every seeded product
           is in metres, and the pilot customer is Norwegian.
      PC   Pieces. Accessories -- centralisers, float equipment, pup joints -- are
           counted, not measured.
      MT   Metric tonnes. How mill capacity and some framework contracts are
           denominated.
      FT   Feet. Added to support the Executive Dashboard's metric-tonnes
           conversion layer (`app.engines.units`): `Product.weight` is expressed
           per FOOT (API nominal weight, lb/ft), so a quantity already in feet
           converts to tonnes in one step with no intermediate unit.
      JT   Joints. A joint is one length of pipe as delivered; added for the same
           reason as FT -- some source data counts by the joint rather than by
           length or piece, and the dashboard needs a unit to label it with before
           it can even attempt (or refuse) a conversion.

    Adding a member is a deliberate edit here, which is the point: a new unit
    means new arithmetic (a conversion, or a new refusal to convert), and that
    decision belongs in this file rather than in whatever row happened to
    introduce the string.

    NOT a conversion table, but no longer for the reason this paragraph used to
    give
    -------------------------------------------------------------------------
    This enum still stores no conversion factor -- that arithmetic lives in
    `app.engines.units`, not here -- but the OLD reasoning above ("any factor
    would be a plausible-looking invention") no longer holds for length<->mass in
    general, and it is worth being honest about exactly what changed and what did
    not:

      * Length<->mass (FT/MTR <-> MT) IS now possible, and is not an invention,
        because `Product.weight` is a REAL PER-PRODUCT fact -- a measured lb/ft
        figure from the API catalogue -- not a platform-wide constant somebody
        guessed. The original objection was to inventing a single metres-per-tonne
        factor for every product; a per-product weight is not that.
      * PC -> MT remains impossible, PERMANENTLY. A piece has no stated length and
        no stated weight, so there is no factor to derive one from and there never
        will be -- seeing FT/MTR gain a conversion does not mean PC quietly gets
        one too.
      * JT -> length IS still an invention, and is explicitly provisional: it goes
        through `JOINT_LENGTH_FEET`, an ASSUMED average joint length, because this
        platform does not record the real one per product or per delivered lot.
        See `app.engines.units` and MVP_COMPROMISES.md C-04.
      * The conversion is CONFINED to the Executive Dashboard's presentation
        layer. Coverage, MRP and every demand-line comparison stay in native
        units, on purpose: coverage compares on-hand against needed DIRECTLY, and
        a rounded metric-tonnes re-expression of either side could flip a verdict
        for a shortfall that only exists in the rounding. See `app.engines.units`
        and MVP_COMPROMISES.md C-05.

    Quantities in different units are still reported separately by default
    (`app.engines.executive.quantity_by_unit`); MT conversion is the one
    deliberate, boundaried exception, not a reversal of the rule.
    """

    MTR = "Mtr"
    PC = "PC"
    MT = "MT"
    FT = "FT"
    JT = "JT"


class Product(Base):
    """Product master, field-shaped after the workbook's Safety Stock tab.

    CATALOGUE ONLY -- no quantity lives here
    ----------------------------------------
    A Product row describes "CSG 9-5/8 53.5 P110 VAM 21 SMLS" for everybody. The
    quantity standing on the shelf is a property of (Business Unit, product), not
    of the catalogue entry, and it lives in
    `app.models.inventory_on_hand.InventoryOnHand`.

    There used to be an `on_hand_qty` column here. It was a single global scalar
    that two different Business Units both read as their own, and it has been
    dropped -- column and all -- rather than merely left unread, because an unread
    second source of truth is still a second source of truth. Resolution is done
    in exactly one place: `app.engines.inventory`.

    THE UNIT, however, does belong here
    -----------------------------------
    `unit_of_measure` is the exception that proves the rule above, not a violation
    of it. It is not a quantity; it is the DIMENSION every quantity of this SKU is
    expressed in. A given SKU is quantified exactly one way -- casing is bought by
    the metre, a float collar by the piece -- whoever is buying it, in whichever
    Business Unit, on whichever demand line. So it is a property of the catalogue
    entry in precisely the way `on_hand_qty` was not.

    It is deliberately NOT on `DemandLine` or `InventoryOnHand`. Putting it there
    would permit a demand line in metres to be compared against an on-hand row in
    tonnes, which is the dimensional error the field exists to prevent -- and it
    would need reconciling on every write instead of being true by construction.
    One SKU, one unit, one row.

    NOT NULL, with no default. There is no such thing as an unlabelled quantity: a
    bare number on a screen is the defect this column was added to fix, so a
    product whose unit nobody has stated must fail to insert rather than serve an
    unlabelled figure. Existing rows were back-filled to metres by the Alembic
    revision -- see it for why that back-fill is honest for THIS database and would
    not be for an arbitrary one.
    """

    __tablename__ = "products"

    id = Column(String(36), primary_key=True, default=_uuid)
    type = Column(String, nullable=False)  # e.g. TBG, CSG
    size = Column(String, nullable=False)
    weight = Column(Float, nullable=True)
    grade = Column(String, nullable=False)
    grade_type = Column(String, nullable=False)  # groups grades for lead-time lookup
    connection = Column(String, nullable=False)
    commodity = Column(String, nullable=True)
    description = Column(String, nullable=True)
    #: The unit every quantity of this product is counted in. See the class
    #: docstring for why it lives here and `UnitOfMeasure` for why it is a closed
    #: enum. NOT NULL and no default -- an unlabelled quantity is the defect.
    unit_of_measure = Column(
        SAEnum(UnitOfMeasure, name="unitofmeasure"), nullable=False
    )
    active = Column(Boolean, nullable=False, default=True)
