"""Establish the versioned schema and additive pilot control-plane tables."""

from alembic import op

from social_crawler.storage.store import metadata

revision = "0001_control_plane"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    # The project predates Alembic. create_all is intentional for this one baseline:
    # it preserves existing tables and creates only missing objects before Alembic
    # owns all subsequent schema changes.
    metadata.create_all(op.get_bind())


def downgrade():
    # Never infer that adopting version tracking authorizes deleting crawler data.
    pass
