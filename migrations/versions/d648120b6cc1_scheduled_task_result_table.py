"""Scheduled task result table

Revision ID: d648120b6cc1
Revises: 2668147c5428
Create Date: 2019-11-10 19:18:15.234118

"""

# revision identifiers, used by Alembic.
revision = 'd648120b6cc1'
down_revision = '2668147c5428'

from alembic import op
import sqlalchemy as sa


def upgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.create_table('scheduled_task_result',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(), nullable=False),
    sa.Column('start_time', sa.DateTime(timezone=True), nullable=False),
    sa.Column('duration', sa.Interval(), nullable=False),
    sa.Column('result', sa.JSON(), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_scheduled_task_result'))
    )
    # ### end Alembic commands ###


def downgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_table('scheduled_task_result')
    # ### end Alembic commands ###
