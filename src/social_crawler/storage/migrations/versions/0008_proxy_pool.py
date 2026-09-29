"""Independent proxy inventory; credentials remain in private local files."""

from alembic import op

from social_crawler.storage.store import proxies

revision = "0008_proxy_pool"
down_revision = "0007_agent_analysis"
branch_labels = None
depends_on = None


def upgrade():
    proxies.create(op.get_bind(), checkfirst=True)


def downgrade():
    proxies.drop(op.get_bind(), checkfirst=True)
