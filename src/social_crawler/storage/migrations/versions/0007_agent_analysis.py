"""Persist manual Agent conversations and task evidence snapshots."""

from alembic import op

from social_crawler.storage.store import (
    analysis_messages,
    analysis_sessions,
    analysis_sources,
)

revision = "0007_agent_analysis"
down_revision = "0006_system_settings"
branch_labels = None
depends_on = None


def upgrade():
    for table in (analysis_sessions, analysis_messages, analysis_sources):
        table.create(op.get_bind(), checkfirst=True)


def downgrade():
    for table in (analysis_sources, analysis_messages, analysis_sessions):
        table.drop(op.get_bind(), checkfirst=True)
