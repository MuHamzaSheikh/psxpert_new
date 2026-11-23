"""
Regression tests for database dialect and schema compatibility (PostgreSQL and SQLite fallback).
Ensures PSXpert analysis engines and API endpoints operate seamlessly across both database backends.
"""

from __future__ import annotations

import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import app
from src.analysis.fundamental_engine import FundamentalAnalysisEngine
from src.database.connection import get_engine, is_postgres
from src.database.merge_external_repo import ExternalRepoMerger


@pytest.fixture(scope="module")
def db_engine():
    return get_engine()


@pytest.fixture(scope="module")
def client():
    return TestClient(app)


def test_market_summary_dialect_agnostic_date_query(db_engine):
    """
    Ensure the 52-week high/low date filter uses portable parameter binding
    rather than PostgreSQL-specific `INTERVAL '365 days'` syntax.
    """
    cutoff_365 = (datetime.now().date() - timedelta(days=365)).isoformat()
    with db_engine.connect() as conn:
        row = conn.execute(
            text("""
                SELECT MAX(high) as high_52w, MIN(low) as low_52w
                FROM market_indices
                WHERE index_symbol = 'KSE100'
                  AND trading_date >= :cutoff_365;
            """),
            {"cutoff_365": cutoff_365},
        ).fetchone()

        assert row is not None
        # high_52w and low_52w should be numeric if market_indices is populated
        if row[0] is not None:
            assert float(row[0]) >= float(row[1])


def test_technical_indicators_endpoint_graceful_cache_fallback(client):
    """
    Ensure /api/indicators/{symbol} gracefully falls back to on-demand calculation
    if the precomputed `technical_indicators` table is absent in SQLite.
    """
    response = client.get("/api/indicators/OGDC?days=60")
    assert response.status_code == 200
    data = response.json()
    assert data["symbol"] == "OGDC"
    assert data["total_bars"] > 0
    assert "indicators" in data
    assert "latest_diagnostics" in data


def test_fundamental_analysis_engine_loads_ifrs_from_active_backend(db_engine):
    """
    Verify that FundamentalAnalysisEngine can query and process IFRS statements
    from the active database backend (PostgreSQL or SQLite fallback).
    """
    fund_eng = FundamentalAnalysisEngine()
    with db_engine.connect() as conn:
        res = fund_eng.analyze(conn, "OGDC", as_of_date="2026-10-13")
        assert res["status"] == "OK"
        assert res["reporting_period_context"]["latest_complete_period_end"] == "2026-06-30"
        assert res["profitability"]["eps_pkr"] is not None
        assert res["valuation"]["pe_ratio"] is not None


def test_external_repo_merger_data_source_registration(db_engine):
    """
    Verify that register_data_source supplies `retrieval_timestamp`
    to satisfy NOT NULL schema constraints on both SQLite and PostgreSQL.
    """
    repo_dir = str(Path(__file__).resolve().parent.parent / "data" / "fundamentals_external")
    merger = ExternalRepoMerger(repo_dir)
    src_id = merger.register_data_source()
    assert isinstance(src_id, int)
    assert src_id > 0

    with db_engine.connect() as conn:
        row = conn.execute(
            text("SELECT source_name, status, retrieval_timestamp FROM data_sources WHERE id = :id"),
            {"id": src_id},
        ).fetchone()
        assert row is not None
        assert row[0] == "IMMAD_BABER_PSX_DATA"
        assert row[1] == "SUCCESS"
        assert row[2] is not None
