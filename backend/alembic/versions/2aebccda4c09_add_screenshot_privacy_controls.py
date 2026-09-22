"""add_screenshot_privacy_controls

Revision ID: 2aebccda4c09
Revises: a3d7c05e1b92
Create Date: 2026-09-22 13:14:00.000000

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '2aebccda4c09'
down_revision = 'a3d7c05e1b92'
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.create_table(
        'screenshot_applications',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('process_name', sa.String(), nullable=False),
        sa.Column('category', sa.String(), nullable=False),
        sa.Column('description', sa.String(), nullable=True),
        sa.Column('is_active', sa.Boolean(), server_default='true', nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_screenshot_applications_id'), 'screenshot_applications', ['id'], unique=False)
    op.create_index(op.f('ix_screenshot_applications_name'), 'screenshot_applications', ['name'], unique=False)
    op.create_index(op.f('ix_screenshot_applications_process_name'), 'screenshot_applications', ['process_name'], unique=False)

    op.create_table(
        'screenshot_urls',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('domain', sa.String(), nullable=False),
        sa.Column('url_pattern', sa.String(), nullable=False),
        sa.Column('category', sa.String(), nullable=False),
        sa.Column('is_active', sa.Boolean(), server_default='true', nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_screenshot_urls_id'), 'screenshot_urls', ['id'], unique=False)
    op.create_index(op.f('ix_screenshot_urls_name'), 'screenshot_urls', ['name'], unique=False)
    op.create_index(op.f('ix_screenshot_urls_domain'), 'screenshot_urls', ['domain'], unique=False)

    op.create_table(
        'screenshot_exclusions',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('application_id', sa.Integer(), nullable=True),
        sa.Column('url_id', sa.Integer(), nullable=True),
        sa.Column('exclusion_type', sa.String(), nullable=False),
        sa.Column('is_excluded', sa.Boolean(), server_default='true', nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
        sa.ForeignKeyConstraint(['application_id'], ['screenshot_applications.id'], ),
        sa.ForeignKeyConstraint(['url_id'], ['screenshot_urls.id'], ),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.CheckConstraint("exclusion_type IN ('application', 'url')", name='check_valid_exclusion_type'),
        sa.CheckConstraint("(exclusion_type = 'application' AND application_id IS NOT NULL AND url_id IS NULL) OR (exclusion_type = 'url' AND url_id IS NOT NULL AND application_id IS NULL)", name='check_exclusion_refs')
    )
    op.create_index(op.f('ix_screenshot_exclusions_application_id'), 'screenshot_exclusions', ['application_id'], unique=False)
    op.create_index(op.f('ix_screenshot_exclusions_id'), 'screenshot_exclusions', ['id'], unique=False)
    op.create_index(op.f('ix_screenshot_exclusions_url_id'), 'screenshot_exclusions', ['url_id'], unique=False)
    op.create_index(op.f('ix_screenshot_exclusions_user_id'), 'screenshot_exclusions', ['user_id'], unique=False)

def downgrade() -> None:
    op.drop_index(op.f('ix_screenshot_exclusions_user_id'), table_name='screenshot_exclusions')
    op.drop_index(op.f('ix_screenshot_exclusions_url_id'), table_name='screenshot_exclusions')
    op.drop_index(op.f('ix_screenshot_exclusions_id'), table_name='screenshot_exclusions')
    op.drop_index(op.f('ix_screenshot_exclusions_application_id'), table_name='screenshot_exclusions')
    op.drop_table('screenshot_exclusions')
    op.drop_index(op.f('ix_screenshot_urls_domain'), table_name='screenshot_urls')
    op.drop_index(op.f('ix_screenshot_urls_name'), table_name='screenshot_urls')
    op.drop_index(op.f('ix_screenshot_urls_id'), table_name='screenshot_urls')
    op.drop_table('screenshot_urls')
    op.drop_index(op.f('ix_screenshot_applications_process_name'), table_name='screenshot_applications')
    op.drop_index(op.f('ix_screenshot_applications_name'), table_name='screenshot_applications')
    op.drop_index(op.f('ix_screenshot_applications_id'), table_name='screenshot_applications')
    op.drop_table('screenshot_applications')
