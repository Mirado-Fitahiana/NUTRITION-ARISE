"""pilotage : collecte et exécutions longues

Revision ID: 4c7a1e2f9b30
Revises: b95ec122313a
Create Date: 2026-09-15 18:00:00

Écrite à la main : la base de développement n'était pas joignable lors de sa
rédaction, l'autogénération n'a pas pu servir. Elle suit les conventions de
nommage de `app/models/base.py`, et se relit hors connexion :

    alembic upgrade b95ec122313a:4c7a1e2f9b30 --sql

Les deux sources pré-qualifiées au sprint 02 sont insérées. `bonmarche.mg` en
est volontairement absente : son ajout relève d'un arbitrage de conformité
(plan, §5.1), pas d'une migration.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = '4c7a1e2f9b30'
down_revision: Union[str, Sequence[str], None] = 'b95ec122313a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('scraping_sources',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('slug', sa.String(length=64), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('platform', sa.String(length=32), nullable=False),
    sa.Column('base_url', sa.String(length=300), nullable=False),
    sa.Column('pages', postgresql.ARRAY(sa.Text()), nullable=False),
    sa.Column('delay_seconds', sa.Numeric(precision=5, scale=2), nullable=False),
    sa.Column('max_pages', sa.Integer(), nullable=False),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('vendor_id', sa.UUID(), nullable=True),
    sa.Column('tos_attested_by', sa.String(length=128), nullable=True),
    sa.Column('tos_attested_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('tos_url', sa.String(length=500), nullable=True),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('delay_seconds >= 3', name=op.f('ck_scraping_sources_delay_floor')),
    sa.CheckConstraint('max_pages BETWEEN 1 AND 50', name=op.f('ck_scraping_sources_max_pages_range')),
    sa.CheckConstraint("platform IN ('prestashop', 'woocommerce')", name=op.f('ck_scraping_sources_platform_known')),
    sa.CheckConstraint('(tos_attested_by IS NULL AND tos_attested_at IS NULL) OR (tos_attested_by IS NOT NULL AND tos_attested_at IS NOT NULL)', name=op.f('ck_scraping_sources_tos_attestation_complete')),
    sa.ForeignKeyConstraint(['vendor_id'], ['vendors.id'], name=op.f('fk_scraping_sources_vendor_id_vendors'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_scraping_sources'))
    )
    op.create_index(op.f('ix_scraping_sources_slug'), 'scraping_sources', ['slug'], unique=True)

    op.create_table('operation_runs',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('kind', sa.String(length=64), nullable=False),
    sa.Column('subject', sa.String(length=200), nullable=True),
    sa.Column('status', sa.Enum('pending', 'running', 'succeeded', 'failed', 'cancelled', 'interrupted', name='operation_status'), nullable=False),
    sa.Column('params', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('step', sa.String(length=64), nullable=True),
    sa.Column('done', sa.Integer(), nullable=False),
    sa.Column('total', sa.Integer(), nullable=True),
    sa.Column('counters', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('alert', sa.Boolean(), nullable=False),
    sa.Column('requested_by', sa.String(length=128), nullable=True),
    sa.Column('correlation_id', sa.String(length=64), nullable=True),
    sa.Column('cancel_requested', sa.Boolean(), nullable=False),
    sa.Column('error_code', sa.String(length=64), nullable=True),
    sa.Column('error_detail', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint('done >= 0', name=op.f('ck_operation_runs_done_not_negative')),
    sa.CheckConstraint('total IS NULL OR total >= 0', name=op.f('ck_operation_runs_total_not_negative')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_operation_runs'))
    )
    op.create_index(op.f('ix_operation_runs_correlation_id'), 'operation_runs', ['correlation_id'], unique=False)
    op.create_index('ix_operation_runs_kind_created_at', 'operation_runs', ['kind', 'created_at'], unique=False)
    op.create_index('ix_operation_runs_subject_status', 'operation_runs', ['subject', 'status'], unique=False)

    op.create_table('operation_run_events',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('run_id', sa.UUID(), nullable=False),
    sa.Column('seq', sa.Integer(), nullable=False),
    sa.Column('occurred_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('level', sa.String(length=16), nullable=False),
    sa.Column('step', sa.String(length=64), nullable=True),
    sa.Column('message', sa.Text(), nullable=False),
    sa.Column('data', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.CheckConstraint("level IN ('info', 'warning', 'error')", name=op.f('ck_operation_run_events_level_known')),
    sa.ForeignKeyConstraint(['run_id'], ['operation_runs.id'], name=op.f('fk_operation_run_events_run_id_operation_runs'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_operation_run_events')),
    sa.UniqueConstraint('run_id', 'seq', name=op.f('uq_operation_run_events_run_id_seq'))
    )
    op.create_index(op.f('ix_operation_run_events_run_id'), 'operation_run_events', ['run_id'], unique=False)

    op.create_table('scraping_errors',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('run_id', sa.UUID(), nullable=False),
    sa.Column('source_id', sa.UUID(), nullable=True),
    sa.Column('url', sa.String(length=1000), nullable=True),
    sa.Column('step', sa.String(length=64), nullable=False),
    sa.Column('http_status', sa.Integer(), nullable=True),
    sa.Column('message', sa.Text(), nullable=False),
    sa.Column('context', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('occurred_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['run_id'], ['operation_runs.id'], name=op.f('fk_scraping_errors_run_id_operation_runs'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['source_id'], ['scraping_sources.id'], name=op.f('fk_scraping_errors_source_id_scraping_sources'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_scraping_errors'))
    )
    op.create_index(op.f('ix_scraping_errors_occurred_at'), 'scraping_errors', ['occurred_at'], unique=False)
    op.create_index(op.f('ix_scraping_errors_run_id'), 'scraping_errors', ['run_id'], unique=False)

    op.create_table('scraped_offers',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('run_id', sa.UUID(), nullable=False),
    sa.Column('source_id', sa.UUID(), nullable=True),
    sa.Column('url', sa.String(length=1000), nullable=True),
    sa.Column('label_raw', sa.String(length=500), nullable=False),
    sa.Column('label_normalized', sa.String(length=500), nullable=False),
    sa.Column('price_raw', sa.String(length=120), nullable=True),
    sa.Column('price', sa.Numeric(precision=12, scale=2), nullable=True),
    sa.Column('currency', sa.String(length=3), nullable=True),
    sa.Column('price_status', sa.String(length=16), nullable=False),
    sa.Column('price_reason', sa.String(length=255), nullable=True),
    sa.Column('packaging_raw', sa.String(length=200), nullable=True),
    sa.Column('quantity', sa.Numeric(precision=10, scale=3), nullable=True),
    sa.Column('unit', sa.String(length=40), nullable=True),
    sa.Column('availability', sa.String(length=40), nullable=True),
    sa.Column('ingredient_id', sa.UUID(), nullable=True),
    sa.Column('match_status', sa.String(length=16), nullable=False),
    sa.Column('match_reason', sa.String(length=255), nullable=True),
    sa.Column('collected_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint('price IS NULL OR price > 0', name=op.f('ck_scraped_offers_price_positive')),
    sa.CheckConstraint("price_status IN ('read', 'absent', 'unreadable')", name=op.f('ck_scraped_offers_price_status_known')),
    sa.CheckConstraint("match_status IN ('matched', 'unmatched')", name=op.f('ck_scraped_offers_match_status_known')),
    sa.ForeignKeyConstraint(['ingredient_id'], ['ingredients.id'], name=op.f('fk_scraped_offers_ingredient_id_ingredients'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['run_id'], ['operation_runs.id'], name=op.f('fk_scraped_offers_run_id_operation_runs'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['source_id'], ['scraping_sources.id'], name=op.f('fk_scraped_offers_source_id_scraping_sources'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_scraped_offers'))
    )
    op.create_index('ix_scraped_offers_label_match', 'scraped_offers', ['label_normalized', 'match_status'], unique=False)
    op.create_index(op.f('ix_scraped_offers_run_id'), 'scraped_offers', ['run_id'], unique=False)

    # Sources pré-qualifiées au sprint 02 (critère 1 de SPIKE-01 atteint). Les
    # pages sont celles du relevé de structure : une page de catalogue, pas la
    # page d'accueil, qui avait faussé la première passe.
    op.execute(
        "INSERT INTO scraping_sources"
        " (slug, name, platform, base_url, pages, delay_seconds, max_pages, is_active, notes)"
        " VALUES"
        " ('kibo', 'kibo.mg', 'prestashop', 'https://www.kibo.mg',"
        "  ARRAY['https://www.kibo.mg/tananarive/182-sucres'], 3, 5, true,"
        "  'PrestaShop. Qualifiee au sprint 02 : prix publies dans le HTML, pages produits autorisees par robots.txt.'),"
        " ('abcie', 'abcie.org', 'woocommerce', 'https://www.abcie.org',"
        "  ARRAY['https://www.abcie.org/boutique/'], 3, 5, true,"
        "  'WooCommerce. Qualifiee au sprint 02, perimetre etroit : peu de prix alimentaires.')"
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_scraped_offers_run_id'), table_name='scraped_offers')
    op.drop_index('ix_scraped_offers_label_match', table_name='scraped_offers')
    op.drop_table('scraped_offers')
    op.drop_index(op.f('ix_scraping_errors_run_id'), table_name='scraping_errors')
    op.drop_index(op.f('ix_scraping_errors_occurred_at'), table_name='scraping_errors')
    op.drop_table('scraping_errors')
    op.drop_index(op.f('ix_operation_run_events_run_id'), table_name='operation_run_events')
    op.drop_table('operation_run_events')
    op.drop_index('ix_operation_runs_subject_status', table_name='operation_runs')
    op.drop_index('ix_operation_runs_kind_created_at', table_name='operation_runs')
    op.drop_index(op.f('ix_operation_runs_correlation_id'), table_name='operation_runs')
    op.drop_table('operation_runs')
    op.drop_index(op.f('ix_scraping_sources_slug'), table_name='scraping_sources')
    op.drop_table('scraping_sources')

    # `op.drop_table` ne supprime pas les types énumérés PostgreSQL : sans cette
    # ligne, le prochain upgrade échouerait sur « type already exists ».
    sa.Enum(name='operation_status').drop(op.get_bind(), checkfirst=True)
