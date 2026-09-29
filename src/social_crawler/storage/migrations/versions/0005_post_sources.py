"""Track supplied post targets independently from keyword hits."""

import sqlalchemy as sa
from alembic import op

revision = "0005_post_sources"
down_revision = "0004_cron_schedule_management"
branch_labels = None
depends_on = None


def upgrade():
    if "post_sources" not in sa.inspect(op.get_bind()).get_table_names():
        op.create_table(
            "post_sources",
            sa.Column("run_id", sa.String(), sa.ForeignKey("runs.id"), primary_key=True),
            sa.Column("source_id", sa.String(), primary_key=True),
            sa.Column("position", sa.Integer(), nullable=False),
            sa.Column("display", sa.String(), nullable=False),
            sa.Column("task_id", sa.String()),
            sa.Column("content_id", sa.String()),
            sa.Column("status", sa.String(), nullable=False),
            sa.Column("error", sa.String()),
        )


def downgrade():
    op.drop_table("post_sources")
