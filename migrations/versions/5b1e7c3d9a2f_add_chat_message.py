"""add chat_message

Revision ID: 5b1e7c3d9a2f
Revises: 68752a5180b9
Create Date: 2026-09-10 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '5b1e7c3d9a2f'
down_revision = '68752a5180b9'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'chat_message',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('analysis_id', sa.Integer(), nullable=False),
        sa.Column('role', sa.String(length=10), nullable=False),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('sources', sa.JSON(), nullable=True),
        sa.Column('web_searches', sa.Integer(), server_default='0', nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=True),
        sa.ForeignKeyConstraint(['analysis_id'], ['analysis.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('chat_message', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_chat_message_analysis_id'), ['analysis_id'], unique=False)


def downgrade():
    with op.batch_alter_table('chat_message', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_chat_message_analysis_id'))

    op.drop_table('chat_message')
