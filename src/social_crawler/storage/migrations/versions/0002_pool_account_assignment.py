"""Select execution accounts from the pool instead of pinning schedules."""

import sqlalchemy as sa
from alembic import op

revision = "0002_pool_account_assignment"
down_revision = "0001_control_plane"
branch_labels = None
depends_on = None


def upgrade():
    # Existing pilot schedules adopt the same pool-selection behavior as new
    # schedules. Historical runs keep their account_id in run_contexts.
    op.execute(sa.text("UPDATE schedules SET account_id = NULL"))
    with op.batch_alter_table("schedules") as batch:
        batch.alter_column(
            "account_id",
            existing_type=sa.String(),
            nullable=True,
        )


def downgrade():
    # There is no safe account to infer for pool-based schedules.
    pass
