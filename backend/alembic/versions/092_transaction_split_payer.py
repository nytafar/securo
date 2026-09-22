"""The payer override: an explicit payer on a transaction's shares

The payer of a shared transaction is normally derived from the owner of
the account it sits on. That is wrong for cash, for an account outside
the app, and for a member who has no Securo user at all, so a
transaction's group sharing may now name its payer explicitly.

The column lives on the share side, `transaction_splits`, because a
payer only means something relative to a group and the group is only
known through the shares. It is written identically on every share row
of one transaction by `split_service.replace_splits`, and it disappears
with the shares.

Additive and nullable: existing rows keep NULL, which means "derive the
payer from the account's owner", exactly what every row did before. No
row is read or rewritten. The foreign key is RESTRICT, like the share's
own `group_member_id`, so the database refuses to drop a member who is
still named as a payer; the service refuses first, with a message.

Revision ID: 092
Revises: 091
Create Date: 2026-09-22
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "092"
down_revision: Union[str, None] = "091"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_COLUMN = "payer_group_member_id"
_TABLE = "transaction_splits"
_INDEX = "ix_transaction_splits_payer_group_member_id"
_FK = "fk_transaction_splits_payer_group_member_id"


def _uuid_type(bind) -> sa.types.TypeEngine:
    """`UUID` on PostgreSQL, the same type the existing member column
    uses there; plain text elsewhere, which is how the app's UUIDs are
    stored on SQLite."""
    if bind.dialect.name == "postgresql":
        return UUID(as_uuid=True)
    return sa.String(length=36)


def upgrade() -> None:
    bind = op.get_bind()

    # batch_alter_table so the foreign key is created on SQLite too,
    # where ALTER TABLE ... ADD CONSTRAINT does not exist. On PostgreSQL
    # — which is what production runs — it emits the plain ALTER
    # statements and rewrites no row. On SQLite it copies the table, and
    # the copy carries only what reflection can see, so the unnamed
    # RESTRICT on the existing `group_member_id` is not reproduced there.
    with op.batch_alter_table(_TABLE) as batch_op:
        batch_op.add_column(sa.Column(_COLUMN, _uuid_type(bind), nullable=True))
        batch_op.create_foreign_key(
            _FK, "group_members", [_COLUMN], ["id"], ondelete="RESTRICT"
        )

    op.create_index(_INDEX, _TABLE, [_COLUMN])


def downgrade() -> None:
    op.drop_index(_INDEX, table_name=_TABLE)

    with op.batch_alter_table(_TABLE) as batch_op:
        batch_op.drop_constraint(_FK, type_="foreignkey")
        batch_op.drop_column(_COLUMN)
