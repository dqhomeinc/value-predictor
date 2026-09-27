"""add build_limits_lookup

Revision ID: c3e8f1a2b7d4
Revises: 68752a5180b9
Create Date: 2026-09-11 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c3e8f1a2b7d4'
down_revision = '68752a5180b9'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'build_limits_lookup',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('jurisdiction', sa.String(length=120), nullable=False),
        sa.Column('district', sa.String(length=50), nullable=False),
        sa.Column('status', sa.String(length=12), nullable=False),
        sa.Column('limits', sa.JSON(), nullable=True),
        sa.Column('dropped', sa.JSON(), nullable=True),
        sa.Column('code_title', sa.String(length=255), nullable=True),
        sa.Column('code_url', sa.Text(), nullable=True),
        sa.Column('district_as_written', sa.String(length=100), nullable=True),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('model', sa.String(length=50), nullable=True),
        sa.Column('web_searches', sa.Integer(), server_default='0', nullable=False),
        sa.Column('web_fetches', sa.Integer(), server_default='0', nullable=False),
        sa.Column('requested_by_user_id', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=True),
        sa.ForeignKeyConstraint(['requested_by_user_id'], ['user.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('build_limits_lookup', schema=None) as batch_op:
        batch_op.create_index('ix_build_limits_lookup_key', ['jurisdiction', 'district'], unique=False)


def downgrade():
    with op.batch_alter_table('build_limits_lookup', schema=None) as batch_op:
        batch_op.drop_index('ix_build_limits_lookup_key')

    op.drop_table('build_limits_lookup')
