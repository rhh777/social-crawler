"""Add unattended-canary network, quota, and recovery safety records."""

from alembic import op

from social_crawler.storage.store import (
    account_network_bindings,
    network_observations,
    quota_buckets,
    quota_policies,
    quota_policy_audits,
    quota_reservations,
    recovery_probes,
)

revision = "0003_unattended_canary_safety"
down_revision = "0002_pool_account_assignment"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    for table in (
        account_network_bindings,
        recovery_probes,
        network_observations,
        quota_policies,
        quota_buckets,
        quota_reservations,
        quota_policy_audits,
    ):
        table.create(bind, checkfirst=True)


def downgrade():
    bind = op.get_bind()
    for table in (
        quota_policy_audits,
        quota_reservations,
        quota_buckets,
        quota_policies,
        network_observations,
        recovery_probes,
        account_network_bindings,
    ):
        table.drop(bind, checkfirst=True)
