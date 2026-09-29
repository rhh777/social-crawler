"""Add Cron expressions and soft deletion for editable schedules."""

import sqlalchemy as sa
from alembic import op

revision = "0004_cron_schedule_management"
down_revision = "0003_unattended_canary_safety"
branch_labels = None
depends_on = None


def upgrade():
    existing = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("schedules")}
    with op.batch_alter_table("schedules") as batch:
        if "cron_expression" not in existing:
            batch.add_column(sa.Column("cron_expression", sa.String(), nullable=True))
        if "deleted_at" not in existing:
            batch.add_column(sa.Column("deleted_at", sa.Float(), nullable=True))


def downgrade():
    with op.batch_alter_table("schedules") as batch:
        batch.drop_column("deleted_at")
        batch.drop_column("cron_expression")
