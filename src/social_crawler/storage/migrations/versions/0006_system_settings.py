"""Persist shared runtime controls for web, CLI and workers."""

from alembic import op

from social_crawler.storage.store import system_settings

revision = "0006_system_settings"
down_revision = "0005_post_sources"
branch_labels = None
depends_on = None


def upgrade():
    system_settings.create(op.get_bind(), checkfirst=True)


def downgrade():
    system_settings.drop(op.get_bind(), checkfirst=True)
