"""Add the help query route."""

from collections.abc import Sequence

from alembic import op

revision: str = "0002_add_help_run_route"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TYPE agent.run_route ADD VALUE 'help'")


def downgrade() -> None:
    op.execute("UPDATE agent.query_runs SET route = 'clarify' WHERE route = 'help'")
    op.execute("ALTER TYPE agent.run_route RENAME TO run_route_with_help")
    op.execute("CREATE TYPE agent.run_route AS ENUM ('sql','documents','hybrid','clarify')")
    op.execute(
        """ALTER TABLE agent.query_runs
        ALTER COLUMN route TYPE agent.run_route
        USING route::text::agent.run_route"""
    )
    op.execute("DROP TYPE agent.run_route_with_help")
