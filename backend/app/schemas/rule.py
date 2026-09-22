# backend/app/schemas/rule.py
import datetime
import uuid
from decimal import Decimal
from typing import Any, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator


class RuleCondition(BaseModel):
    field: str   # description, payee, notes, amount, type, account_id, payee_id, date
    op: str      # contains, not_contains, equals, not_equals, starts_with, ends_with, regex, gt, gte, lt, lte
    value: Any   # str or number depending on field

    @field_validator("value")
    @classmethod
    def value_must_not_be_blank(cls, v: Any) -> Any:
        """Reject blank values — they silently match every transaction.

        A blank value turns `contains`/`starts_with`/`ends_with`/`regex` into a
        tautology, and numeric comparisons fall back to 0, so the rule applies
        its actions to the whole ledger. Explicit `0` and `False` stay valid.
        """
        if v is None or (isinstance(v, str) and not v.strip()):
            raise ValueError("Condition value cannot be blank")
        return v


class RuleConditionGroup(BaseModel):
    """A nested group of conditions joined by its own operator.

    Groups let a rule mix AND and OR — `type is debit AND (contains UBER OR
    contains 99POP)`. They hold leaf conditions only: `conditions` is typed as
    `list[RuleCondition]`, so a nested group fails validation and rule depth
    stays capped at two levels, which is what the engine and editor support.
    """

    op: str = "or"   # and, or
    conditions: list[RuleCondition]

    @field_validator("op")
    @classmethod
    def op_must_be_and_or(cls, v: str) -> str:
        if v not in ("and", "or"):
            raise ValueError("Condition group operator must be 'and' or 'or'")
        return v

    @field_validator("conditions")
    @classmethod
    def group_must_not_be_empty(cls, v: list[RuleCondition]) -> list[RuleCondition]:
        """An empty group never matches, so it can only make a rule confusing."""
        if not v:
            raise ValueError("Condition group cannot be empty")
        return v


# A rule's condition list mixes leaves and one level of groups. The two shapes
# are disjoint — a leaf has no `conditions`, a group has no `field`/`value` — so
# Pydantic's smart union resolves them without a discriminator.
RuleConditionNode = Union[RuleConditionGroup, RuleCondition]


class RuleAction(BaseModel):
    op: str      # set_category, set_payee, set_description, append_notes, ignore,
                 # share_in_group, mark_as_contribution
    value: Any   # entity UUID, text, or an action object (see below)


class RuleShareSplit(BaseModel):
    """One member's place in a sharing rule's distribution."""

    group_member_id: uuid.UUID
    # Only read for share_type="percent"; equal needs nothing but the
    # member. Bounded, so a negative share — which would hand a member
    # money for a cost the group carried — cannot be stored, and neither
    # can one over the whole amount.
    share_pct: Optional[Decimal] = Field(default=None, ge=0, le=100)


class RuleShareAction(BaseModel):
    """The `value` of a `share_in_group` action.

    No exact amounts: a rule fires on transactions of every size, and an
    amount that fits one of them fits no other. Equal and percent are the
    distributions that generalize, which is also what bulk add-to-group
    accepts.
    """

    group_id: uuid.UUID
    share_type: Literal["equal", "percent"] = "equal"
    splits: list[RuleShareSplit]


class RuleContributionAction(BaseModel):
    """The `value` of a `mark_as_contribution` action.

    `member_id` is the member on the OTHER side of the transaction, as in
    marking by hand: a debit makes the account's owner the payer and this
    member the receiver, a credit the other way round.
    """

    group_id: uuid.UUID
    member_id: uuid.UUID


class RuleCreate(BaseModel):
    name: str
    conditions_op: str = "and"
    conditions: list[RuleConditionNode]
    actions: list[RuleAction]
    priority: int = 0
    is_active: bool = True
    apply_to_existing: bool = True
    overwrite_existing_categories: bool = False


class RuleUpdate(BaseModel):
    name: Optional[str] = None
    conditions_op: Optional[str] = None
    conditions: Optional[list[RuleConditionNode]] = None
    actions: Optional[list[RuleAction]] = None
    priority: Optional[int] = None
    is_active: Optional[bool] = None
    apply_to_existing: Optional[bool] = None
    overwrite_existing_categories: bool = False


class RuleRead(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID
    name: str
    conditions_op: str
    conditions: list[dict]
    actions: list[dict]
    priority: int
    is_active: bool

    model_config = ConfigDict(from_attributes=True)


class RuleMutationResponse(RuleRead):
    """A changed rule plus how many existing transactions it just affected."""

    applied_count: int = 0


class RuleCreateResponse(RuleMutationResponse):
    """A created rule response kept for API compatibility."""


class RuleExportItem(BaseModel):
    name: str
    conditions_op: str = "and"
    conditions: list[RuleConditionNode]
    actions: list[RuleAction]
    priority: int = 0
    is_active: bool = True


class RuleExportPayload(BaseModel):
    format: str = "securo-categorization-rules"
    version: int = 1
    rules: list[RuleExportItem]


class RuleImportRequest(BaseModel):
    payload: RuleExportPayload
    overwrite: bool = False


class RuleImportResponse(BaseModel):
    imported: int
    skipped: int
    overwritten: int


class RulePreviewRequest(BaseModel):
    """A draft rule sent from the editor, before it is saved.

    Carries the same save-time flags as `RuleCreate` — the preview answers
    "what happens when I save this?", and saving an inactive rule, or one not
    being applied to existing transactions, changes nothing right now.
    Name and priority play no part: neither decides what a rule matches.
    """

    conditions_op: str = "and"
    conditions: list[RuleConditionNode]
    actions: list[RuleAction] = []
    is_active: bool = True
    apply_to_existing: bool = True
    overwrite_existing_categories: bool = False
    limit: int = Field(default=20, ge=1, le=100)
    # The sample is a window over the matches, newest first. Counts are exact
    # whatever the window is, so the editor pages through a broad rule's
    # matches instead of judging it by the first screenful.
    offset: int = Field(default=0, ge=0)


class RulePreviewShareLine(BaseModel):
    group_member_id: uuid.UUID
    member_name: Optional[str] = None
    # float for the same reason `RulePreviewItem.amount` is one: display
    # data for the editor's table.
    amount: float


class RulePreviewShare(BaseModel):
    """The shares a draft rule would write on one matched transaction."""

    group_id: uuid.UUID
    group_name: Optional[str] = None
    share_type: str
    shares: list[RulePreviewShareLine]


class RulePreviewContribution(BaseModel):
    """The contribution a draft rule would make of one matched transaction."""

    group_id: uuid.UUID
    group_name: Optional[str] = None
    from_member_id: uuid.UUID
    from_member_name: Optional[str] = None
    to_member_id: uuid.UUID
    to_member_name: Optional[str] = None
    amount: float
    currency: str
    date: datetime.date
    # "payer" or "receiver" — which side the transaction itself is.
    side: str
    # "create" writes a new contribution, "attach" hangs this transaction
    # off the one the other leg of the same transfer already made.
    outcome: str


class RulePreviewItem(BaseModel):
    """One matched transaction plus the category the draft rule would leave it in."""

    id: uuid.UUID
    date: datetime.date
    description: str
    # float, not Decimal: this is display data for the editor's preview table,
    # and Decimal would serialize as a JSON string the UI has to coerce back.
    amount: float
    currency: str
    type: str
    current_category_id: Optional[uuid.UUID] = None
    current_category_name: Optional[str] = None
    new_category_id: Optional[uuid.UUID] = None
    new_category_name: Optional[str] = None
    # False when the rule matches but leaves the transaction as it is — most
    # often because it already has a category and the draft does not overwrite.
    will_change: bool
    # What the draft's group actions would do to this row, decided by the
    # same services that would write them. Null when the rule plans none,
    # or when the row is one they would pass over — in which case
    # `skipped_effects` says why, in English, e.g. because the
    # transaction already carries shares or is already a contribution.
    planned_share: Optional[RulePreviewShare] = None
    planned_contribution: Optional[RulePreviewContribution] = None
    skipped_effects: list[str] = []


class RulePreviewResponse(BaseModel):
    matched: int
    will_change: int
    # How many of the matches the draft's group actions would really act
    # on: rows that carry no shares yet for `will_share`, rows that are
    # not a contribution and carry no shares for
    # `will_mark_contribution`. Both are exact over every match, like
    # `matched`, and both are 0 when the draft has no such action or
    # would not apply at all.
    will_share: int = 0
    will_mark_contribution: int = 0
    # False when the draft's own flags mean saving it touches nothing now: an
    # inactive rule, or one not being applied to existing transactions. The
    # matches are still reported, so the conditions can be checked either way.
    will_apply: bool
    # The requested window of the matches — `offset` through `offset + limit`,
    # newest first. Compare `offset + len(sample)` with `matched` to know
    # whether more can be fetched.
    sample: list[RulePreviewItem]
    offset: int = 0
