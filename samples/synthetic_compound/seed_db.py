"""Seed a local SQLite database with synthetic data matching the named queries.

Tables match the source schemas referenced by the YAML queries under
samples/synthetic_compound/queries/. Numbers are aligned with the
prose in the synthetic DOCX corpus (so the LLM-generated narrative
and the deterministic tables agree).

Run:
    python samples/synthetic_compound/seed_db.py

Output: samples/synthetic_compound/edc.sqlite
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

HERE = Path(__file__).parent
DB_PATH = HERE / "edc.sqlite"


SCHEMA = """
DROP TABLE IF EXISTS ae_events_by_soc;
CREATE TABLE ae_events_by_soc (
    compound_id TEXT NOT NULL,
    soc TEXT NOT NULL,
    any_grade_n INTEGER NOT NULL,
    grade_3_4_n INTEGER NOT NULL,
    sae_n INTEGER NOT NULL
);

DROP TABLE IF EXISTS exposure_summary;
CREATE TABLE exposure_summary (
    compound_id TEXT NOT NULL,
    study_id TEXT NOT NULL,
    dose_group TEXT NOT NULL,
    n_subjects INTEGER NOT NULL,
    median_duration_weeks REAL NOT NULL,
    n_at_least_12wk INTEGER NOT NULL
);

DROP TABLE IF EXISTS clinical_pk_ss;
CREATE TABLE clinical_pk_ss (
    compound_id TEXT NOT NULL,
    study_id TEXT NOT NULL,
    dose_text TEXT NOT NULL,
    n_subjects INTEGER NOT NULL,
    cmax_ss_ng_per_ml REAL NOT NULL,
    auc_24_ss_ng_h_per_ml REAL NOT NULL,
    accumulation_ratio REAL NOT NULL
);

DROP TABLE IF EXISTS nonclinical_pk;
CREATE TABLE nonclinical_pk (
    compound_id TEXT NOT NULL,
    species TEXT NOT NULL,
    dose_mg_per_kg REAL NOT NULL,
    route TEXT NOT NULL,
    cmax_ng_per_ml REAL NOT NULL,
    auc_inf_ng_h_per_ml REAL NOT NULL,
    half_life_h REAL NOT NULL,
    bioavailability_pct REAL NOT NULL
);

DROP TABLE IF EXISTS pivotal_tox;
CREATE TABLE pivotal_tox (
    compound_id TEXT NOT NULL,
    study_id TEXT NOT NULL,
    species TEXT NOT NULL,
    duration_text TEXT NOT NULL,
    noael_mg_per_kg_per_day REAL NOT NULL,
    noael_auc_24 REAL NOT NULL,
    target_organs TEXT NOT NULL
);
DROP TABLE IF EXISTS safety_pharmacology;
CREATE TABLE safety_pharmacology (
    compound_id TEXT NOT NULL,
    study_id TEXT NOT NULL,
    domain TEXT NOT NULL,
    model TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    finding TEXT NOT NULL,
    key_metric TEXT NOT NULL
);

DROP TABLE IF EXISTS genotoxicity;
CREATE TABLE genotoxicity (
    compound_id TEXT NOT NULL,
    study_id TEXT NOT NULL,
    assay TEXT NOT NULL,
    test_system TEXT NOT NULL,
    doses_or_concentrations TEXT NOT NULL,
    metabolic_activation TEXT NOT NULL,
    result TEXT NOT NULL
);

DROP TABLE IF EXISTS projected_human_exposure;
CREATE TABLE projected_human_exposure (
    compound_id TEXT NOT NULL,
    basis TEXT NOT NULL,
    projected_dose_text TEXT NOT NULL,
    projected_cmax_ng_per_ml REAL NOT NULL,
    projected_auc_24_ng_h_per_ml REAL NOT NULL
);
"""


# Numbers match the synthetic DOCX corpus.
COMPOUND = "XYZ-001"


AE_ROWS = [
    (COMPOUND, "Gastrointestinal disorders", 78, 12, 4),
    (COMPOUND, "General disorders", 63, 5, 2),
    (COMPOUND, "Skin and subcutaneous tissue disorders", 52, 7, 1),
    (COMPOUND, "Investigations (lab abnormalities)", 47, 13, 2),
    (COMPOUND, "Nervous system disorders", 31, 3, 1),
    (COMPOUND, "Respiratory, thoracic and mediastinal disorders", 29, 5, 5),
    (COMPOUND, "Vascular disorders", 18, 4, 3),
    (COMPOUND, "Cardiac disorders", 9, 2, 2),
]

EXPOSURE_ROWS = [
    (COMPOUND, "XYZ-101", "25 mg QD", 3, 8.0, 1),
    (COMPOUND, "XYZ-101", "50 mg QD", 3, 10.0, 2),
    (COMPOUND, "XYZ-101", "100 mg QD", 6, 14.0, 4),
    (COMPOUND, "XYZ-101", "200 mg QD", 6, 20.0, 5),
    (COMPOUND, "XYZ-101", "300 mg QD", 12, 22.0, 8),
    (COMPOUND, "XYZ-101", "400 mg QD", 6, 16.0, 4),
    (COMPOUND, "XYZ-102", "300 mg QD (RP2D)", 84, 18.5, 38),
]

CLINICAL_PK_ROWS = [
    (COMPOUND, "XYZ-101", "25 mg QD", 3, 72.0, 590.0, 1.4),
    (COMPOUND, "XYZ-101", "100 mg QD", 6, 304.0, 2680.0, 1.5),
    (COMPOUND, "XYZ-101", "200 mg QD", 6, 612.0, 5400.0, 1.6),
    (COMPOUND, "XYZ-101", "300 mg QD", 12, 945.0, 8400.0, 1.6),
    (COMPOUND, "XYZ-101", "400 mg QD", 6, 1380.0, 12100.0, 1.7),
]

NONCLINICAL_PK_ROWS = [
    (COMPOUND, "Rat", 5.0, "PO", 95.0, 580.0, 7.2, 38.0),
    (COMPOUND, "Rat", 25.0, "PO", 480.0, 2900.0, 8.1, 38.0),
    (COMPOUND, "Rat", 100.0, "PO", 1840.0, 11400.0, 8.6, 38.0),
    (COMPOUND, "Dog", 1.0, "PO", 38.0, 340.0, 13.5, 62.0),
    (COMPOUND, "Dog", 5.0, "PO", 185.0, 1700.0, 14.0, 62.0),
    (COMPOUND, "Dog", 25.0, "PO", 920.0, 8600.0, 14.2, 62.0),
]

PIVOTAL_TOX_ROWS = [
    (COMPOUND, "XYZ-NC-002", "Rat", "28-day", 60.0, 12000.0, "Liver (hypertrophy)"),
    (COMPOUND, "XYZ-NC-003", "Dog", "28-day", 12.0, 9600.0, "GI tract (soft stools)"),
    (
        COMPOUND, "XYZ-NC-004", "Rat", "13-week", 30.0, 6200.0,
        "Liver (hypertrophy), bone marrow (erythroid)",
    ),
    (
        COMPOUND, "XYZ-NC-005", "Dog", "26-week", 6.0, 4800.0,
        "GI tract (soft stools), liver (mild ALT elevation)",
    ),
]


# Safety pharmacology. Every row here is stated in the DOCX corpus
# (XYZ-NC-001 primary pharmacology, XYZ-NC-005 dog 26-week), because the
# corpus and these tables have to agree — a contradiction between the prose the
# model reads and the table it is handed shows up as a critique failure, which
# is the right outcome but a waste of a run. Respiratory and CNS are absent on
# purpose: the corpus does not cover them, so inventing "no effect" findings
# would be manufacturing results. The section reports the gap instead.
SAFETY_PHARM_ROWS = [
    (
        COMPOUND, "XYZ-NC-001", "Cardiovascular", "hERG patch clamp (in vitro)",
        "hERG potassium channel tail-current inhibition",
        "Inhibition with IC50 of 12 uM", "IC50 12 uM; ~60-fold over projected human Cmax",
    ),
    (
        COMPOUND, "XYZ-NC-001", "Cardiovascular", "Anaesthetised dog (in vivo)",
        "Heart rate, arterial blood pressure, QTc interval",
        "No clinically meaningful change up to 30 mg/kg IV", "NOEL >= 30 mg/kg IV",
    ),
    (
        COMPOUND, "XYZ-NC-005", "Cardiovascular", "Beagle dog, 26-week oral (ECG)",
        "QTcF interval at weeks 4, 13 and 26",
        "No compound-related change at any dose", "QTcF within 1.5% of baseline at 20 mg/kg/day",
    ),
]

# Genotoxicity. Synthetic and table-only: the DOCX corpus has no genotoxicity
# narrative, so this exercises the deterministic-table path with no prose
# counterpart to agree with. Called out in
# docs/data-connections-and-visuals-plan.md rather than left as a surprise.
GENOTOX_ROWS = [
    (
        COMPOUND, "XYZ-NC-010", "Bacterial reverse mutation (Ames)",
        "S. typhimurium TA98/TA100/TA1535/TA1537, E. coli WP2uvrA",
        "up to 5000 ug/plate", "With and without S9", "Negative",
    ),
    (
        COMPOUND, "XYZ-NC-011", "In vitro micronucleus",
        "Human peripheral blood lymphocytes", "up to 250 ug/mL",
        "With and without S9", "Negative",
    ),
    (
        COMPOUND, "XYZ-NC-012", "In vivo micronucleus",
        "Rat bone marrow", "up to 500 mg/kg/day for 3 days", "Not applicable (in vivo)",
        "Negative",
    ),
]

# The denominator for every exposure margin. Held as data rather than hardcoded
# in the query so the margin and the projection it is measured against are
# visible in the same report, and so changing the projection changes the
# margins rather than silently disagreeing with them.
PROJECTED_HUMAN_EXPOSURE_ROWS = [
    (
        COMPOUND, "Anticipated efficacious dose, projected from rat and dog PK",
        "300 mg QD equivalent", 200.0, 800.0,
    ),
]


def seed(db_path: Path = DB_PATH) -> Path:
    if db_path.exists():
        db_path.unlink()
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(SCHEMA)
        conn.executemany(
            "INSERT INTO ae_events_by_soc VALUES (?,?,?,?,?)", AE_ROWS
        )
        conn.executemany(
            "INSERT INTO exposure_summary VALUES (?,?,?,?,?,?)", EXPOSURE_ROWS
        )
        conn.executemany(
            "INSERT INTO clinical_pk_ss VALUES (?,?,?,?,?,?,?)", CLINICAL_PK_ROWS
        )
        conn.executemany(
            "INSERT INTO nonclinical_pk VALUES (?,?,?,?,?,?,?,?)", NONCLINICAL_PK_ROWS
        )
        conn.executemany(
            "INSERT INTO pivotal_tox VALUES (?,?,?,?,?,?,?)", PIVOTAL_TOX_ROWS
        )
        conn.executemany(
            "INSERT INTO safety_pharmacology VALUES (?,?,?,?,?,?,?)", SAFETY_PHARM_ROWS
        )
        conn.executemany(
            "INSERT INTO genotoxicity VALUES (?,?,?,?,?,?,?)", GENOTOX_ROWS
        )
        conn.executemany(
            "INSERT INTO projected_human_exposure VALUES (?,?,?,?,?)",
            PROJECTED_HUMAN_EXPOSURE_ROWS,
        )
        conn.commit()
    finally:
        conn.close()
    return db_path


def main() -> int:
    path = seed()
    print(f"Seeded EDC SQLite at: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
