"""Merge MCP connection and code-interpreter purge heads.

Revision ID: 4f6a2c8d9e10
Revises: 2b7c9d4e8f01, 7b5f4d2a9c31
Create Date: 2026-05-31
"""

from typing import Sequence, Union


revision: str = "4f6a2c8d9e10"
down_revision: Union[str, Sequence[str], None] = ("2b7c9d4e8f01", "7b5f4d2a9c31")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
