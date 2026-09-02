# app/services/file_service.py

import os
import re
import json
from datetime import datetime
from pathlib import Path
from app.services.planner_helpers import clean_sql as robust_clean_sql

# PDF generation
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from reportlab.lib import colors
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer,
    Table, TableStyle, PageBreak, HRFlowable
)
from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_JUSTIFY

import logging
logger = logging.getLogger(__name__)

# Output directory
OUTPUT_DIR = Path("outputs")
OUTPUT_DIR.mkdir(exist_ok=True)


# ── SQL File Generator ───────────────────────────────────────────

def generate_sql_file(
    schema_sql: str,
    project_name: str,
    session_id: str,
) -> str:
    """
    Cleans and saves the schema SQL as a .sql file.
    Returns the file path.
    """
    # Strip markdown code blocks if present
    clean_sql = robust_clean_sql(schema_sql)

    # Build file header
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    safe_name = _safe_filename(project_name)

    from app.validators.schema_validator import rule_count

    header = f"""-- ============================================================
-- Project  : {project_name}
-- Generated: {timestamp}
-- Engine   : AI DB Schema Generator
-- Rules    : {rule_count()} production rules applied
-- ============================================================

SET FOREIGN_KEY_CHECKS = 0;
SET SQL_MODE = "NO_AUTO_VALUE_ON_ZERO";
SET time_zone = "+00:00";
START TRANSACTION;

"""
    footer = """

COMMIT;
SET FOREIGN_KEY_CHECKS = 1;
-- ============================================================
-- End of schema
-- ============================================================
"""
    full_sql = header + clean_sql + footer

    # Save file
    filename = f"{safe_name}_{session_id[:8]}.sql"
    filepath = OUTPUT_DIR / filename

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(full_sql)

    logger.info(f"✅ SQL file saved: {filepath}")
    return str(filepath)


# ── PDF Documentation Generator ─────────────────────────────────

def generate_pdf_documentation(
    schema_sql: str,
    project_name: str,
    session_id: str,
    blueprint: dict,
    validation: dict,
    metadata: dict,
    rules_applied: list[dict],
) -> str:
    """
    Generates a complete PDF documentation file.
    Returns the file path.
    """
    safe_name = _safe_filename(project_name)
    filename = f"{safe_name}_{session_id[:8]}_docs.pdf"
    filepath = OUTPUT_DIR / filename

    # Parse tables from SQL
    tables = _parse_tables_from_sql(schema_sql)

    doc = SimpleDocTemplate(
        str(filepath),
        pagesize=A4,
        leftMargin=1.8*cm,
        rightMargin=1.8*cm,
        topMargin=1.8*cm,
        bottomMargin=1.8*cm,
    )

    story = []
    story += _build_cover(project_name, blueprint, validation, metadata)
    story += _build_overview(blueprint, tables, validation)
    story += _build_table_docs(tables, blueprint)
    story += _build_rules_section(rules_applied)
    story += _build_developer_notes(tables, blueprint)

    doc.build(story)
    logger.info(f"✅ PDF documentation saved: {filepath}")
    return str(filepath)


# ── PDF Sections ─────────────────────────────────────────────────

C_DARK  = colors.HexColor('#1a1a2e')
C_ACC   = colors.HexColor('#0f3460')
C_HIGH  = colors.HexColor('#e94560')
C_LIGHT = colors.HexColor('#f5f5f5')
C_GREEN = colors.HexColor('#2ecc71')
C_WHITE = colors.white
C_GREY  = colors.HexColor('#888888')


def _make_styles():
    H1   = ParagraphStyle('H1',   fontSize=22, textColor=C_DARK,
                          fontName='Helvetica-Bold', spaceAfter=6,
                          spaceBefore=16, leading=28)
    H2   = ParagraphStyle('H2',   fontSize=14, textColor=C_ACC,
                          fontName='Helvetica-Bold', spaceAfter=4,
                          spaceBefore=14, leading=20)
    H3   = ParagraphStyle('H3',   fontSize=11, textColor=C_DARK,
                          fontName='Helvetica-Bold', spaceAfter=3,
                          spaceBefore=10, leading=15)
    BODY = ParagraphStyle('BODY', fontSize=9,  textColor=C_DARK,
                          fontName='Helvetica', spaceAfter=4,
                          leading=14, alignment=TA_JUSTIFY)
    MONO = ParagraphStyle('MONO', fontSize=8,  textColor=C_ACC,
                          fontName='Courier', spaceAfter=2,
                          leading=12, leftIndent=8)
    SMALL= ParagraphStyle('SMALL',fontSize=8,  textColor=C_GREY,
                          fontName='Helvetica', spaceAfter=2, leading=11)
    return H1, H2, H3, BODY, MONO, SMALL


def _hr():
    return HRFlowable(
        width='100%', thickness=0.5,
        color=colors.HexColor('#dddddd'), spaceAfter=6
    )


def _badge(text, color=C_ACC):
    data = [[Paragraph(
        f'<font color="white"><b>{text}</b></font>',
        ParagraphStyle('b', fontSize=10, fontName='Helvetica-Bold',
                       textColor=C_WHITE, leading=14)
    )]]
    t = Table(data, colWidths=[A4[0] - 3.6*cm])
    t.setStyle(TableStyle([
        ('BACKGROUND',   (0,0),(-1,-1), color),
        ('TOPPADDING',   (0,0),(-1,-1), 7),
        ('BOTTOMPADDING',(0,0),(-1,-1), 7),
        ('LEFTPADDING',  (0,0),(-1,-1), 12),
    ]))
    return t


def _build_cover(project_name, blueprint, validation, metadata):
    H1, H2, H3, BODY, MONO, SMALL = _make_styles()
    story = []

    story.append(Spacer(1, 2*cm))

    # Title block
    title_data = [[Paragraph(
        project_name.upper(),
        ParagraphStyle('ct', fontSize=26, textColor=C_WHITE,
                       fontName='Helvetica-Bold', leading=32,
                       alignment=TA_CENTER)
    )]]
    tt = Table(title_data, colWidths=[A4[0] - 3.6*cm])
    tt.setStyle(TableStyle([
        ('BACKGROUND',   (0,0),(-1,-1), C_DARK),
        ('TOPPADDING',   (0,0),(-1,-1), 24),
        ('BOTTOMPADDING',(0,0),(-1,-1), 8),
    ]))
    story.append(tt)

    sub_data = [[Paragraph(
        'Database Schema Documentation',
        ParagraphStyle('cs', fontSize=12, textColor=colors.HexColor('#aaaacc'),
                       fontName='Helvetica', leading=18, alignment=TA_CENTER)
    )]]
    st = Table(sub_data, colWidths=[A4[0] - 3.6*cm])
    st.setStyle(TableStyle([
        ('BACKGROUND',   (0,0),(-1,-1), C_DARK),
        ('TOPPADDING',   (0,0),(-1,-1), 0),
        ('BOTTOMPADDING',(0,0),(-1,-1), 20),
    ]))
    story.append(st)

    # Stats row
    score = validation.get("score", 0) if validation else 0
    grade = validation.get("grade", "N/A") if validation else "N/A"
    tables_count = len(validation.get("tables_found", [])) if validation else 0
    rules_count = metadata.get("total_rules_applied", 0) if metadata else 0

    stats = [
        [
            Paragraph(f'<b>{score}/100</b>\nQuality Score',
                      ParagraphStyle('s', fontSize=11, textColor=C_WHITE,
                                     fontName='Helvetica-Bold', leading=16,
                                     alignment=TA_CENTER)),
            Paragraph(f'<b>Grade {grade}</b>\nSchema Grade',
                      ParagraphStyle('s', fontSize=11, textColor=C_WHITE,
                                     fontName='Helvetica-Bold', leading=16,
                                     alignment=TA_CENTER)),
            Paragraph(f'<b>{tables_count}</b>\nTables Generated',
                      ParagraphStyle('s', fontSize=11, textColor=C_WHITE,
                                     fontName='Helvetica-Bold', leading=16,
                                     alignment=TA_CENTER)),
            Paragraph(f'<b>{rules_count}</b>\nRules Applied',
                      ParagraphStyle('s', fontSize=11, textColor=C_WHITE,
                                     fontName='Helvetica-Bold', leading=16,
                                     alignment=TA_CENTER)),
        ]
    ]
    col_w = (A4[0] - 3.6*cm) / 4
    stat_table = Table(stats, colWidths=[col_w]*4)
    stat_table.setStyle(TableStyle([
        ('BACKGROUND', (0,0),(0,0), C_HIGH),
        ('BACKGROUND', (1,0),(1,0), C_ACC),
        ('BACKGROUND', (2,0),(2,0), C_GREEN),
        ('BACKGROUND', (3,0),(3,0), colors.HexColor('#8e44ad')),
        ('TOPPADDING',   (0,0),(-1,-1), 12),
        ('BOTTOMPADDING',(0,0),(-1,-1), 12),
        ('ALIGN', (0,0),(-1,-1), 'CENTER'),
        ('VALIGN', (0,0),(-1,-1), 'MIDDLE'),
    ]))
    story.append(stat_table)

    # Meta info
    story.append(Spacer(1, 1*cm))
    timestamp = datetime.now().strftime("%B %d, %Y at %H:%M")
    domain = blueprint.get("domain", "").replace("_", " ").title() if blueprint else ""
    scale = blueprint.get("scale", "").title() if blueprint else ""
    gst = "Yes" if (blueprint or {}).get("gst_required") else "No"
    provider = metadata.get("ai_provider", "").title() if metadata else ""
    model = metadata.get("ai_model", "") if metadata else ""

    meta_rows = [
        ["Generated On",   timestamp],
        ["Domain",         domain],
        ["Scale",          scale],
        ["GST Compliance", gst],
        ["AI Provider",    f"{provider} — {model}"],
    ]
    meta_data = [
        [Paragraph(f'<b>{k}</b>', ParagraphStyle('mk', fontSize=9,
                   fontName='Helvetica-Bold', textColor=C_ACC, leading=12)),
         Paragraph(v, ParagraphStyle('mv', fontSize=9,
                   fontName='Helvetica', textColor=C_DARK, leading=12))]
        for k, v in meta_rows
    ]
    meta_table = Table(meta_data, colWidths=[5*cm, 10*cm])
    meta_table.setStyle(TableStyle([
        ('ROWBACKGROUNDS', (0,0),(-1,-1), [C_LIGHT, C_WHITE]),
        ('TOPPADDING',     (0,0),(-1,-1), 5),
        ('BOTTOMPADDING',  (0,0),(-1,-1), 5),
        ('LEFTPADDING',    (0,0),(-1,-1), 8),
        ('GRID', (0,0),(-1,-1), 0.3, colors.HexColor('#dddddd')),
    ]))
    story.append(meta_table)
    story.append(PageBreak())
    return story


def _build_overview(blueprint, tables, validation):
    H1, H2, H3, BODY, MONO, SMALL = _make_styles()
    story = []

    story.append(_badge('1. PROJECT OVERVIEW'))
    story.append(Spacer(1, 8))

    if blueprint:
        story.append(Paragraph(blueprint.get("description", ""), BODY))
        story.append(Spacer(1, 8))

        # Module summary
        story.append(Paragraph('<b>Modules</b>', H3))
        for i, module in enumerate(blueprint.get("modules", []), 1):
            module_tables = ", ".join(
                f"`{t['name']}`" for t in module.get("tables", [])
            )
            row_data = [[
                Paragraph(f'<b>{i}. {module["name"]}</b>',
                          ParagraphStyle('mn', fontSize=9, fontName='Helvetica-Bold',
                                         textColor=C_WHITE, leading=13)),
                Paragraph(module.get("description", ""),
                          ParagraphStyle('md', fontSize=8, fontName='Helvetica',
                                         textColor=C_LIGHT, leading=12)),
            ]]
            rt = Table(row_data, colWidths=[5*cm, 10*cm])
            rt.setStyle(TableStyle([
                ('BACKGROUND', (0,0),(-1,-1), C_ACC),
                ('TOPPADDING',   (0,0),(-1,-1), 6),
                ('BOTTOMPADDING',(0,0),(-1,-1), 6),
                ('LEFTPADDING',  (0,0),(-1,-1), 8),
            ]))
            story.append(rt)

            table_rows = []
            for t in module.get("tables", []):
                table_rows.append([
                    Paragraph(f'`{t["name"]}`',
                              ParagraphStyle('tn', fontSize=8, fontName='Courier',
                                             textColor=C_ACC, leading=12)),
                    Paragraph(t.get("purpose", ""),
                              ParagraphStyle('tp', fontSize=8, fontName='Helvetica',
                                             textColor=C_DARK, leading=12)),
                ])
            if table_rows:
                tt = Table(table_rows, colWidths=[6*cm, 9*cm])
                tt.setStyle(TableStyle([
                    ('ROWBACKGROUNDS', (0,0),(-1,-1), [C_LIGHT, C_WHITE]),
                    ('TOPPADDING',     (0,0),(-1,-1), 4),
                    ('BOTTOMPADDING',  (0,0),(-1,-1), 4),
                    ('LEFTPADDING',    (0,0),(-1,-1), 12),
                    ('LINEBELOW', (0,-1),(-1,-1), 0.5,
                     colors.HexColor('#cccccc')),
                ]))
                story.append(tt)
            story.append(Spacer(1, 4))

    # Validation summary
    if validation:
        story.append(Spacer(1, 8))
        story.append(Paragraph('<b>Quality Validation</b>', H3))
        score = validation.get("score", 0)
        issues = validation.get("issues", [])
        passed = score >= 60

        val_rows = [
            ["Score",          f"{score}/100"],
            ["Grade",          validation.get("grade", "N/A")],
            ["Status",         "✅ PASSED" if passed else "❌ FAILED"],
            ["Total Issues",   str(validation.get("total_issues", 0))],
            ["Tables Found",   str(len(validation.get("tables_found", [])))],
        ]
        val_data = [
            [Paragraph(f'<b>{k}</b>',
                       ParagraphStyle('vk', fontSize=9, fontName='Helvetica-Bold',
                                      textColor=C_ACC, leading=12)),
             Paragraph(v,
                       ParagraphStyle('vv', fontSize=9, fontName='Helvetica',
                                      textColor=C_DARK, leading=12))]
            for k, v in val_rows
        ]
        vt = Table(val_data, colWidths=[5*cm, 10*cm])
        vt.setStyle(TableStyle([
            ('ROWBACKGROUNDS', (0,0),(-1,-1), [C_LIGHT, C_WHITE]),
            ('TOPPADDING',     (0,0),(-1,-1), 5),
            ('BOTTOMPADDING',  (0,0),(-1,-1), 5),
            ('LEFTPADDING',    (0,0),(-1,-1), 8),
            ('GRID', (0,0),(-1,-1), 0.3, colors.HexColor('#dddddd')),
        ]))
        story.append(vt)

    story.append(PageBreak())
    return story


def _build_table_docs(tables: list[dict], blueprint: dict):
    H1, H2, H3, BODY, MONO, SMALL = _make_styles()
    story = []

    story.append(_badge('2. TABLE-BY-TABLE DOCUMENTATION'))
    story.append(Spacer(1, 8))
    story.append(Paragraph(
        'For each table: purpose, column definitions, when to INSERT, '
        'when to UPDATE, and relationships.',
        ParagraphStyle('i', fontSize=9, fontName='Helvetica',
                       textColor=C_GREY, leading=13)
    ))
    story.append(Spacer(1, 12))

    for i, table in enumerate(tables, 1):
        table_name = table["name"]
        columns = table["columns"]

        # Determine table type and purpose
        purpose, insert_when, update_when = _get_table_logic(table_name, blueprint)

        # Table header
        story.append(Paragraph(
            f'{i}. <font color="#0f3460">`{table_name}`</font>',
            H2
        ))
        story.append(Paragraph(purpose, BODY))
        story.append(Spacer(1, 4))

        # Column table
        if columns:
            col_headers = [
                Paragraph('<b>Column</b>',
                          ParagraphStyle('ch', fontSize=8, fontName='Helvetica-Bold',
                                         textColor=C_WHITE, leading=12)),
                Paragraph('<b>Type</b>',
                          ParagraphStyle('ch', fontSize=8, fontName='Helvetica-Bold',
                                         textColor=C_WHITE, leading=12)),
                Paragraph('<b>Notes</b>',
                          ParagraphStyle('ch', fontSize=8, fontName='Helvetica-Bold',
                                         textColor=C_WHITE, leading=12)),
            ]
            col_rows = [col_headers]
            for col in columns:
                col_rows.append([
                    Paragraph(f'`{col["name"]}`',
                              ParagraphStyle('cn', fontSize=8, fontName='Courier',
                                             textColor=C_ACC, leading=12)),
                    Paragraph(col["type"],
                              ParagraphStyle('ct', fontSize=8, fontName='Courier',
                                             textColor=C_DARK, leading=12)),
                    Paragraph(col.get("comment", ""),
                              ParagraphStyle('cc', fontSize=8, fontName='Helvetica',
                                             textColor=C_GREY, leading=12)),
                ])
            ct = Table(col_rows, colWidths=[5*cm, 4*cm, 6*cm])
            ct.setStyle(TableStyle([
                ('BACKGROUND',    (0,0),(-1,0),  C_ACC),
                ('ROWBACKGROUNDS',(0,1),(-1,-1),  [C_LIGHT, C_WHITE]),
                ('TOPPADDING',    (0,0),(-1,-1),  4),
                ('BOTTOMPADDING', (0,0),(-1,-1),  4),
                ('LEFTPADDING',   (0,0),(-1,-1),  6),
                ('GRID', (0,0),(-1,-1), 0.3, colors.HexColor('#dddddd')),
                ('VALIGN', (0,0),(-1,-1), 'TOP'),
            ]))
            story.append(ct)

        # Logic guide
        story.append(Spacer(1, 6))
        logic_rows = [
            ("🟢 INSERT when",  insert_when),
            ("🔄 UPDATE when",  update_when),
        ]
        for label, text in logic_rows:
            ld = Table([[
                Paragraph(f'<b>{label}</b>',
                          ParagraphStyle('ll', fontSize=8, fontName='Helvetica-Bold',
                                         textColor=C_ACC, leading=12)),
                Paragraph(text,
                          ParagraphStyle('lt', fontSize=8, fontName='Helvetica',
                                         textColor=C_DARK, leading=12)),
            ]], colWidths=[3.5*cm, 11.5*cm])
            ld.setStyle(TableStyle([
                ('BACKGROUND',   (0,0),(0,0), C_LIGHT),
                ('TOPPADDING',   (0,0),(-1,-1), 5),
                ('BOTTOMPADDING',(0,0),(-1,-1), 5),
                ('LEFTPADDING',  (0,0),(-1,-1), 8),
                ('LINEABOVE', (0,0),(-1,0), 0.3, colors.HexColor('#dddddd')),
                ('VALIGN', (0,0),(-1,-1), 'TOP'),
            ]))
            story.append(ld)

        story.append(Spacer(1, 16))
        story.append(_hr())

    story.append(PageBreak())
    return story


def _build_rules_section(rules_applied: list[dict]):
    H1, H2, H3, BODY, MONO, SMALL = _make_styles()
    story = []

    story.append(_badge('3. PRODUCTION RULES APPLIED', color=C_GREEN))
    story.append(Spacer(1, 8))
    story.append(Paragraph(
        f'The following {len(rules_applied)} rules from the AI Database Rule Framework '
        f'were applied during schema generation.',
        BODY
    ))
    story.append(Spacer(1, 8))

    PRI_COLOR = {
        'critical': colors.HexColor('#e74c3c'),
        'high':     colors.HexColor('#e67e22'),
        'medium':   colors.HexColor('#3498db'),
        'low':      colors.HexColor('#2ecc71'),
    }

    rule_rows = [[
        Paragraph('<b>Rule ID</b>',
                  ParagraphStyle('rh', fontSize=8, fontName='Helvetica-Bold',
                                 textColor=C_WHITE, leading=12)),
        Paragraph('<b>Rule Name</b>',
                  ParagraphStyle('rh', fontSize=8, fontName='Helvetica-Bold',
                                 textColor=C_WHITE, leading=12)),
        Paragraph('<b>Category</b>',
                  ParagraphStyle('rh', fontSize=8, fontName='Helvetica-Bold',
                                 textColor=C_WHITE, leading=12)),
        Paragraph('<b>Priority</b>',
                  ParagraphStyle('rh', fontSize=8, fontName='Helvetica-Bold',
                                 textColor=C_WHITE, leading=12)),
    ]]

    for rule in rules_applied:
        pc = PRI_COLOR.get(rule.get("priority", "medium"), C_ACC)
        rule_rows.append([
            Paragraph(str(rule.get("rule_id", "")),
                      ParagraphStyle('ri', fontSize=8, fontName='Courier',
                                     textColor=C_ACC, leading=12)),
            Paragraph(rule.get("rule_name", ""),
                      ParagraphStyle('rn', fontSize=8, fontName='Helvetica',
                                     textColor=C_DARK, leading=12)),
            Paragraph(rule.get("category", ""),
                      ParagraphStyle('rc', fontSize=8, fontName='Helvetica',
                                     textColor=C_GREY, leading=12)),
            Paragraph(rule.get("priority", "").upper(),
                      ParagraphStyle('rp', fontSize=7, fontName='Helvetica-Bold',
                                     textColor=pc, leading=12)),
        ])

    rt = Table(rule_rows, colWidths=[1.5*cm, 7*cm, 3.5*cm, 3*cm])
    rt.setStyle(TableStyle([
        ('BACKGROUND',    (0,0),(-1,0),  C_DARK),
        ('ROWBACKGROUNDS',(0,1),(-1,-1),  [C_WHITE, C_LIGHT]),
        ('TOPPADDING',    (0,0),(-1,-1),  4),
        ('BOTTOMPADDING', (0,0),(-1,-1),  4),
        ('LEFTPADDING',   (0,0),(-1,-1),  6),
        ('GRID', (0,0),(-1,-1), 0.3, colors.HexColor('#dddddd')),
        ('VALIGN', (0,0),(-1,-1), 'TOP'),
    ]))
    story.append(rt)
    story.append(PageBreak())
    return story


def _build_developer_notes(tables: list[dict], blueprint: dict):
    H1, H2, H3, BODY, MONO, SMALL = _make_styles()
    story = []

    story.append(_badge('4. DEVELOPER NOTES', color=colors.HexColor('#8e44ad')))
    story.append(Spacer(1, 8))

    notes = [
        (
            "Business ID Generation",
            "All business IDs (student_id, fee_id, etc.) must be generated from "
            "unique_id_header_all. Insert a row to register each entity type with "
            "its prefix. Increment last_id on every new record. Never use the "
            "auto-increment 'id' column as a business identifier.",
        ),
        (
            "Foreign Key References",
            "All foreign keys must reference the integer 'id' PRIMARY KEY column, "
            "not the business ID (varchar) column. The business ID is for display "
            "only. Join tables using integer id columns for performance.",
        ),
        (
            "Archive Table Usage",
            "Copy the full row to the corresponding _archive_all table BEFORE "
            "making any UPDATE to a _header_all table. Archive tables store the "
            "history of every change. Never DELETE from _header_all — use status instead.",
        ),
        (
            "Life Cycle Table Usage",
            "Insert a row into _life_cycle_all every time the status column changes. "
            "Store previous_status, new_status, and the datetime of the change. "
            "This enables full audit trail of every state transition.",
        ),
        (
            "GST Handling",
            "Always store CGST and SGST amounts separately. Never combine them into "
            "a single tax column. Calculate at write time and store — never compute "
            "at read time. Current rate: CGST 9% + SGST 9% = 18% total (verify "
            "with your accountant for current applicable rates).",
        ),
        (
            "Closing Balance",
            "The closing_balance column on transaction tables must be updated "
            "atomically with the transaction insert. Use a database transaction "
            "(BEGIN/COMMIT) to ensure the balance and the transaction record are "
            "always consistent. Never update balance separately.",
        ),
        (
            "Status Convention",
            "Status 1 = active/success. Status 2 = inactive/failure. "
            "Status 3 = deleted/cancelled. Never hard DELETE rows. "
            "Set status = 2 or 3 and use soft delete everywhere.",
        ),
        (
            "Index Usage",
            "All foreign key columns are indexed. Use the named indexes in your "
            "WHERE clauses. Avoid SELECT * in production — always select specific "
            "columns to use covering indexes effectively.",
        ),
    ]

    for title, content in notes:
        nd = Table([[
            Paragraph(f'<b>{title}</b>',
                      ParagraphStyle('nt', fontSize=9, fontName='Helvetica-Bold',
                                     textColor=C_WHITE, leading=13)),
            Paragraph(content,
                      ParagraphStyle('nc', fontSize=8, fontName='Helvetica',
                                     textColor=C_DARK, leading=13)),
        ]], colWidths=[4*cm, 11*cm])
        nd.setStyle(TableStyle([
            ('BACKGROUND',   (0,0),(0,0), C_ACC),
            ('BACKGROUND',   (1,0),(1,0), C_LIGHT),
            ('TOPPADDING',   (0,0),(-1,-1), 8),
            ('BOTTOMPADDING',(0,0),(-1,-1), 8),
            ('LEFTPADDING',  (0,0),(-1,-1), 8),
            ('VALIGN', (0,0),(-1,-1), 'TOP'),
            ('LINEBELOW', (0,0),(-1,0), 0.5, colors.HexColor('#cccccc')),
        ]))
        story.append(nd)
        story.append(Spacer(1, 4))

    return story


# ── Table Logic Inference ────────────────────────────────────────

def _get_table_logic(table_name: str, blueprint: dict) -> tuple[str, str, str]:
    """Infer purpose, insert trigger, and update trigger from table name."""

    if table_name == "unique_id_header_all":
        return (
            "Central registry for all business IDs. One row per entity type.",
            "Once per entity type during initial setup. "
            "Example: register 'student_header_all' with prefix 'STU-'.",
            "Increment last_id and modified_on every time a new business ID is generated.",
        )

    if "_archive_all" in table_name:
        entity = table_name.replace("_archive_all", "")
        return (
            f"Historical mirror of {entity}_header_all. "
            f"Stores a full copy of every row before it was changed.",
            f"Every time a row in {entity}_header_all is about to be UPDATED. "
            f"Copy the old row here first.",
            "Never update archive rows — they are immutable historical records.",
        )

    if "_life_cycle_all" in table_name:
        entity = table_name.replace("_life_cycle_all", "")
        return (
            f"Status change audit trail for {entity}_header_all. "
            f"Records every status transition with timestamp.",
            f"Every time the status column in {entity}_header_all changes. "
            f"Record previous_status, new_status, and datetime.",
            "Never update life cycle rows — append only.",
        )

    if "_transaction_all" in table_name:
        return (
            f"Event/ledger table for {table_name.replace('_transaction_all', '')} operations. "
            f"Append-only record of all events.",
            "Every time a new transaction, payment, or event occurs. "
            "Each row represents one atomic event.",
            "Update status column only (e.g. pending → paid). "
            "Never modify financial columns after insert.",
        )

    if "_configuration_all" in table_name:
        return (
            f"Temporal configuration for {table_name.replace('_configuration_all', '')}. "
            f"Stores settings with effective date ranges.",
            "When a new configuration is set. Insert new row with new from_date. "
            "Do not update the old row — let it expire.",
            "Update status to 0 (past) when a newer configuration takes over.",
        )

    if "_header_all" in table_name:
        entity = table_name.replace("_header_all", "").replace("_", " ").title()
        return (
            f"Master entity table for {entity} records. "
            f"One row per {entity.lower()} entity.",
            f"When a new {entity.lower()} is registered or onboarded into the system.",
            f"When {entity.lower()} profile information changes. "
            f"Always copy old row to {table_name.replace('_header_all', '_archive_all')} first.",
        )

    return (
        f"Supporting table: {table_name}",
        "When the corresponding business event occurs.",
        "When the record status or key fields change.",
    )


# ── SQL Parser ───────────────────────────────────────────────────

def _parse_tables_from_sql(sql: str) -> list[dict]:
    """Parse CREATE TABLE statements into structured dicts."""
    clean = robust_clean_sql(sql)
    tables = []

    pattern = re.finditer(
        r'CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[`"]?(\w+)[`"]?\s*\((.*?)\)\s*(?:ENGINE|;)',
        clean, re.IGNORECASE | re.DOTALL
    )

    for match in pattern:
        table_name = match.group(1)
        body = match.group(2)
        columns = _parse_columns(body)
        tables.append({
            "name": table_name,
            "columns": columns,
        })

    return tables


def _parse_columns(body: str) -> list[dict]:
    """Parse column definitions from a table body."""
    columns = []
    lines = [line.strip() for line in body.split("\n") if line.strip()]

    for line in lines:
        # Skip constraints and indexes
        if re.match(r'^(PRIMARY|UNIQUE|INDEX|KEY|CONSTRAINT|FOREIGN)', line, re.I):
            continue
        if not line or line.startswith("--"):
            continue

        # Extract column name and type
        col_match = re.match(
            r'[`"]?(\w+)[`"]?\s+(\w+(?:\([^)]+\))?(?:\s+UNSIGNED)?)',
            line, re.IGNORECASE
        )
        if not col_match:
            continue

        col_name = col_match.group(1)
        col_type = col_match.group(2)

        # Skip SQL keywords that aren't columns
        if col_name.upper() in ('PRIMARY', 'UNIQUE', 'INDEX', 'KEY',
                                  'CONSTRAINT', 'FOREIGN', 'ENGINE'):
            continue

        # Extract comment if present
        comment_match = re.search(r"COMMENT\s+'([^']*)'", line, re.IGNORECASE)
        comment = comment_match.group(1) if comment_match else ""

        columns.append({
            "name": col_name,
            "type": col_type,
            "comment": comment,
        })

    return columns


def _safe_filename(name: str) -> str:
    """Convert project name to safe filename."""
    safe = re.sub(r'[^a-zA-Z0-9_]', '_', name.lower())
    return safe[:40]