
from contextlib import contextmanager
import hmac
import json
import os
import re
import secrets
import sqlite3
from datetime import datetime, timedelta
from io import BytesIO
from pathlib import Path
from xml.sax.saxutils import escape

from flask import (
    Flask,
    abort,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from pypdf import PdfReader
from pypdf.errors import PdfReadError
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
import reportlab
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.security import generate_password_hash
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SMART_AI_SECRET_KEY") or secrets.token_hex(32)
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(hours=1)
MAX_UPLOAD_BYTES = 5 * 1024 * 1024
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES + 64 * 1024
app.config["DATABASE"] = str(
    Path(app.instance_path) / "smart_ai_validation.sqlite3"
)
REVIEWER_DECISIONS = ("Under Review", "Needs Correction", "Approved for Demo")
PUBLIC_DEMO_INSTITUTION_USERNAME = "institution_demo"
MAX_HISTORY_ENTRIES = 20
DATABASE_SCHEMA_VERSION = 8
APPLICATION_CATEGORIES = (
    "New Institution",
    "Extension of Approval",
    "New Programme",
    "Change in Intake",
    "Other",
)
REPORT_FONT_NAME = "SmartAI-Vera"
REPORT_FONT_BOLD_NAME = "SmartAI-Vera-Bold"
REPORT_FONT_DIRECTORY = Path(reportlab.__file__).parent / "fonts"
if REPORT_FONT_NAME not in pdfmetrics.getRegisteredFontNames():
    pdfmetrics.registerFont(TTFont(
        REPORT_FONT_NAME, str(REPORT_FONT_DIRECTORY / "Vera.ttf")
    ))
if REPORT_FONT_BOLD_NAME not in pdfmetrics.getRegisteredFontNames():
    pdfmetrics.registerFont(TTFont(
        REPORT_FONT_BOLD_NAME, str(REPORT_FONT_DIRECTORY / "VeraBd.ttf")
    ))
pdfmetrics.registerFontFamily(
    REPORT_FONT_NAME,
    normal=REPORT_FONT_NAME,
    bold=REPORT_FONT_BOLD_NAME,
    italic=REPORT_FONT_NAME,
    boldItalic=REPORT_FONT_BOLD_NAME,
)

SAMPLE_REQUIRED_FIELDS = {
    "Application ID": re.compile(
        r"^\s*Application ID\s*:\s*\S.*$", re.IGNORECASE | re.MULTILINE
    ),
    "Institution Name": re.compile(
        r"^\s*Institution Name\s*:\s*\S.*$", re.IGNORECASE | re.MULTILINE
    ),
    "Application Category": re.compile(
        r"^\s*Application Category\s*:\s*\S.*$", re.IGNORECASE | re.MULTILINE
    ),
    "Academic Year": re.compile(
        r"^\s*Academic Year\s*:\s*\S.*$", re.IGNORECASE | re.MULTILINE
    ),
}

# Sample data for our SIH prototype
applications = [
    {
        "id": "APP-001",
        "college": "Sunrise College of Engineering",
        "category": "Extension of Approval",
        "status": "Under Review",
        "issues": 2
    },
    {
        "id": "APP-002",
        "college": "Greenfield Technical Institute",
        "category": "New Programme",
        "status": "Pending",
        "issues": 1
    },
    {
        "id": "APP-003",
        "college": "City Engineering College",
        "category": "Change in Intake",
        "status": "Flagged",
        "issues": 3
    },
    {
        "id": "APP-004",
        "college": "Riverdale Institute of Technology",
        "category": "Extension of Approval",
        "status": "Validated",
        "issues": 0
    }
]

@contextmanager
def database_connection():
    database_path = Path(app.config["DATABASE"])
    if os.environ.get("VERCEL"):
        database_path = Path("/tmp") / database_path.name
    database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database_path, timeout=5)
    connection.row_factory = sqlite3.Row
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def initialize_database():
    try:
        with database_connection() as connection:
            schema_version = connection.execute("PRAGMA user_version").fetchone()[0]
            if schema_version > DATABASE_SCHEMA_VERSION:
                raise RuntimeError(
                    "The local demo database schema is newer than this application."
                )

            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL CHECK (role IN ('Reviewer', 'Institution'))
                )
                """
            )
            existing_demo_user = connection.execute(
                "SELECT 1 FROM users WHERE username = ?",
                (PUBLIC_DEMO_INSTITUTION_USERNAME,),
            ).fetchone()
            if existing_demo_user is None:
                connection.execute(
                    """
                    INSERT INTO users (username, password_hash, role)
                    VALUES (?, ?, 'Institution')
                    """,
                    (
                        PUBLIC_DEMO_INSTITUTION_USERNAME,
                        generate_password_hash(secrets.token_urlsafe(32)),
                    ),
                )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS reviewer_decisions (
                    application_id TEXT PRIMARY KEY,
                    decision TEXT NOT NULL,
                    comment TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL,
                    public_demo INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            reviewer_decision_columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(reviewer_decisions)"
                ).fetchall()
            }
            if "public_demo" not in reviewer_decision_columns:
                connection.execute(
                    "ALTER TABLE reviewer_decisions ADD COLUMN public_demo INTEGER NOT NULL DEFAULT 0"
                )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS validation_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    filename TEXT NOT NULL,
                    file_type TEXT NOT NULL,
                    fields_found INTEGER NOT NULL,
                    fields_missing INTEGER NOT NULL,
                    validation_status TEXT NOT NULL,
                    risk_level TEXT,
                    field_results TEXT,
                    public_demo INTEGER NOT NULL DEFAULT 0,
                    validated_at TEXT NOT NULL
                )
                """
            )
            validation_history_columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(validation_history)"
                ).fetchall()
            }
            if "field_results" not in validation_history_columns:
                connection.execute(
                    "ALTER TABLE validation_history ADD COLUMN field_results TEXT"
                )
            if "application_id" not in validation_history_columns:
                connection.execute(
                    "ALTER TABLE validation_history ADD COLUMN application_id TEXT"
                )
            if "public_demo" not in validation_history_columns:
                connection.execute(
                    "ALTER TABLE validation_history ADD COLUMN public_demo INTEGER NOT NULL DEFAULT 0"
                )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_validation_history_validated_at
                ON validation_history (validated_at DESC)
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_validation_history_application
                ON validation_history (application_id, id DESC)
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS institution_applications (
                    application_id TEXT PRIMARY KEY,
                    institution_user_id INTEGER NOT NULL,
                    institution_name TEXT NOT NULL,
                    category TEXT NOT NULL,
                    academic_year TEXT NOT NULL,
                    institution_address TEXT NOT NULL,
                    contact_person TEXT NOT NULL,
                    contact_email TEXT NOT NULL,
                    description TEXT NOT NULL,
                    supporting_document BLOB,
                    supporting_document_filename TEXT,
                    status TEXT NOT NULL DEFAULT 'Submitted'
                        CHECK (status IN (
                            'Submitted', 'Under Review',
                            'Correction Required', 'Approved'
                        )),
                    submitted_at TEXT NOT NULL,
                    resubmitted_at TEXT,
                    resubmission_count INTEGER NOT NULL DEFAULT 0,
                    public_demo INTEGER NOT NULL DEFAULT 0,
                    FOREIGN KEY (institution_user_id) REFERENCES users(id)
                )
                """
            )
            institution_application_columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(institution_applications)"
                ).fetchall()
            }
            if "resubmitted_at" not in institution_application_columns:
                connection.execute(
                    "ALTER TABLE institution_applications ADD COLUMN resubmitted_at TEXT"
                )
            if "resubmission_count" not in institution_application_columns:
                connection.execute(
                    """
                    ALTER TABLE institution_applications
                    ADD COLUMN resubmission_count INTEGER NOT NULL DEFAULT 0
                    """
                )
            if "public_demo" not in institution_application_columns:
                connection.execute(
                    """
                    ALTER TABLE institution_applications
                    ADD COLUMN public_demo INTEGER NOT NULL DEFAULT 0
                    """
                )
            if "latest_validation_report_id" not in institution_application_columns:
                connection.execute(
                    """
                    ALTER TABLE institution_applications
                    ADD COLUMN latest_validation_report_id INTEGER
                    """
                )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_institution_applications_owner
                ON institution_applications (institution_user_id, submitted_at DESC)
                """
            )
            if schema_version < DATABASE_SCHEMA_VERSION:
                connection.execute(
                    f"PRAGMA user_version = {DATABASE_SCHEMA_VERSION}"
                )
    except sqlite3.Error:
        app.logger.exception("Could not initialize the local demo database.")
        raise


def load_applications():
    with database_connection() as connection:
        decisions = connection.execute(
            """
            SELECT application_id, decision, comment
            FROM reviewer_decisions
            WHERE public_demo = 1
            """
        ).fetchall()

    decisions_by_id = {
        row["application_id"]: row for row in decisions
    }
    records = []
    for sample in applications:
        record = dict(sample)
        saved_decision = decisions_by_id.get(record["id"])
        if saved_decision is not None:
            record["reviewer_decision"] = saved_decision["decision"]
            record["reviewer_comment"] = saved_decision["comment"]
        records.append(record)
    return records


def get_public_demo_institution_user_id():
    with database_connection() as connection:
        user = connection.execute(
            "SELECT id FROM users WHERE username = ?",
            (PUBLIC_DEMO_INSTITUTION_USERNAME,),
        ).fetchone()
    if user is None:
        raise RuntimeError("The public demo institution record is unavailable.")
    return user["id"]


def load_institution_applications(institution_user_id):
    with database_connection() as connection:
        rows = connection.execute(
            """
            SELECT application_id, institution_name, category, academic_year,
                   status, submitted_at, supporting_document_filename,
                   resubmitted_at, resubmission_count
            FROM institution_applications
                WHERE institution_user_id = ? AND public_demo = 1
            ORDER BY COALESCE(resubmitted_at, submitted_at) DESC
            """,
            (institution_user_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def load_reviewer_inbox():
    institution_user_id = get_public_demo_institution_user_id()
    with database_connection() as connection:
        rows = connection.execute(
            """
            SELECT application_id, institution_name, category, academic_year,
                   status, submitted_at, resubmitted_at, resubmission_count
            FROM institution_applications
                WHERE institution_user_id = ? AND public_demo = 1
            ORDER BY COALESCE(resubmitted_at, submitted_at) DESC
                """,
                (institution_user_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def load_validation_history():
    with database_connection() as connection:
        rows = connection.execute(
            """
            SELECT filename, file_type AS document_type, fields_found,
                   fields_missing, validation_status AS status, risk_level,
                   validated_at
            FROM validation_history
            WHERE public_demo = 1
            ORDER BY id DESC
            LIMIT ?
            """,
            (MAX_HISTORY_ENTRIES,),
        ).fetchall()
    return [dict(row) for row in rows]


def load_validation_risk_counts():
    risk_counts = {"LOW": 0, "MEDIUM": 0, "HIGH": 0, "Not assessed": 0}
    with database_connection() as connection:
        rows = connection.execute(
            """
            SELECT risk_level, COUNT(*) AS report_count
            FROM validation_history
            WHERE public_demo = 1
            GROUP BY risk_level
            """
        ).fetchall()

    for row in rows:
        risk_level = row["risk_level"]
        if risk_level not in ("LOW", "MEDIUM", "HIGH"):
            risk_level = "Not assessed"
        risk_counts[risk_level] += row["report_count"]
    return risk_counts


def create_validation_report_pdf(report):
    output = BytesIO()
    document = SimpleDocTemplate(
        output,
        pagesize=letter,
        rightMargin=0.65 * inch,
        leftMargin=0.65 * inch,
        topMargin=0.65 * inch,
        bottomMargin=0.65 * inch,
        title="SMART-AI — Demo Document Validation Report",
    )
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "ReportTitle",
        parent=styles["Title"],
        fontName=REPORT_FONT_BOLD_NAME,
        alignment=TA_LEFT,
        textColor=colors.HexColor("#142746"),
        fontSize=18,
        leading=23,
        spaceAfter=16,
    )
    body_style = ParagraphStyle(
        "ReportBody",
        parent=styles["BodyText"],
        fontName=REPORT_FONT_NAME,
        textColor=colors.HexColor("#23334d"),
        fontSize=10,
        leading=15,
    )
    label_style = ParagraphStyle(
        "ReportLabel",
        parent=body_style,
        textColor=colors.HexColor("#62718a"),
    )
    table_header_style = ParagraphStyle(
        "ReportTableHeader",
        parent=body_style,
        fontName=REPORT_FONT_BOLD_NAME,
        textColor=colors.white,
    )
    story = [
        Paragraph("SMART-AI — Demo Document Validation Report", title_style),
        Paragraph(
            "Synthetic demo report. This checks required fields only; it does not "
            "prove document authenticity, fraud, or eligibility, and it is not an "
            "official AICTE approval decision.",
            body_style,
        ),
        Spacer(1, 14),
    ]

    metadata = [
        [Paragraph("<b>Uploaded filename</b>", label_style), Paragraph(
            escape(report["filename"]), body_style
        )],
        [Paragraph("<b>File type</b>", label_style), Paragraph(
            escape(report["file_type"]), body_style
        )],
        [Paragraph("<b>Validation date/time</b>", label_style), Paragraph(
            escape(report["validated_at"]), body_style
        )],
        *(
            [[Paragraph("<b>Institution Application ID</b>", label_style), Paragraph(
                escape(report["application_id"]), body_style
            )]]
            if report["application_id"] else []
        ),
        [Paragraph("<b>Total required fields</b>", label_style), Paragraph("4", body_style)],
        [Paragraph("<b>Fields found</b>", label_style), Paragraph(
            str(report["fields_found"]), body_style
        )],
        [Paragraph("<b>Fields missing</b>", label_style), Paragraph(
            str(report["fields_missing"]), body_style
        )],
        [Paragraph("<b>Overall status</b>", label_style), Paragraph(
            escape(report["validation_status"]), body_style
        )],
    ]
    metadata_table = Table(metadata, colWidths=[2.0 * inch, 4.9 * inch])
    metadata_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#f1f4f9")),
        ("TEXTCOLOR", (0, 0), (-1, -1), colors.HexColor("#23334d")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#e5eaf2")),
        ("INNERGRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#e5eaf2")),
        ("LEFTPADDING", (0, 0), (-1, -1), 9),
        ("RIGHTPADDING", (0, 0), (-1, -1), 9),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
    ]))
    story.extend([
        metadata_table,
        Spacer(1, 18),
        Paragraph("Required-field results", styles["Heading2"]),
    ])

    field_results = json.loads(report["field_results"] or "[]")
    field_results_by_label = {
        item["label"]: item["present"]
        for item in field_results
        if isinstance(item, dict)
        and isinstance(item.get("label"), str)
        and isinstance(item.get("present"), bool)
    }
    field_labels = (
        "Application ID",
        "Institution Name",
        "Application Category",
        "Academic Year",
    )
    table_data = [
        [
            Paragraph("Required field", table_header_style),
            Paragraph("Status", table_header_style),
        ]
    ]
    status_colors = []
    for label in field_labels:
        present = field_results_by_label.get(label)
        status = "Found" if present is True else "Missing" if present is False else "Not available"
        table_data.append([
            Paragraph(escape(label), body_style),
            Paragraph(status, body_style),
        ])
        status_colors.append(
            colors.HexColor("#e0f7eb") if present is True
            else colors.HexColor("#ffe8e8") if present is False
            else colors.HexColor("#edf1f7")
        )

    fields_table = Table(table_data, colWidths=[4.9 * inch, 2.0 * inch], repeatRows=1)
    fields_table_style = [
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#142746")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#e5eaf2")),
        ("INNERGRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#e5eaf2")),
        ("LEFTPADDING", (0, 0), (-1, -1), 9),
        ("RIGHTPADDING", (0, 0), (-1, -1), 9),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
    ]
    for row_index, background in enumerate(status_colors, start=1):
        fields_table_style.append(
            ("BACKGROUND", (1, row_index), (1, row_index), background)
        )
    fields_table.setStyle(TableStyle(fields_table_style))

    risk_level = report["risk_level"] or "Not available"
    if risk_level in ("LOW", "MEDIUM", "HIGH"):
        risk_explanation = (
            f"{risk_level} is a rule-based demo indicator determined only by the "
            f"number of missing required fields ({report['fields_missing']})."
        )
    else:
        risk_explanation = "A risk level is not available for this report."
    story.extend([
        fields_table,
        Spacer(1, 18),
        Paragraph("Rule-based demo risk indicator", styles["Heading2"]),
        Paragraph(
            f"<b>{escape(risk_level)}</b> — {escape(risk_explanation)}",
            body_style,
        ),
        Spacer(1, 8),
        Paragraph(
            "This indicator is not proof of fraud, authenticity, or eligibility, "
            "and is not an official approval decision. No trained AI model is used.",
            body_style,
        ),
    ])
    document.build(story)
    output.seek(0)
    return output


initialize_database()


def get_csrf_token():
    token = session.get("csrf_token")
    if token is None:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


@app.context_processor
def inject_csrf_token():
    return {"csrf_token": get_csrf_token}


@app.before_request
def validate_csrf_token():
    if request.method == "POST":
        submitted_token = request.form.get("csrf_token", "")
        expected_token = session.get("csrf_token", "")
        if not submitted_token or not expected_token or not hmac.compare_digest(
            submitted_token, expected_token
        ):
            abort(400, description="Invalid or missing CSRF token.")


@app.route("/")
def role_selection():
    return render_template("login.html")


@app.route("/institution")
def institution_portal():
    return render_template(
        "institution.html",
        submissions=load_institution_applications(
            get_public_demo_institution_user_id()
        ),
    )


def validate_institution_application(values, uploaded_file, require_document=False):
    field_limits = {
        "institution_name": ("Institution Name", 200),
        "category": ("Application Category", 100),
        "academic_year": ("Academic Year", 20),
        "institution_address": ("Institution Address", 500),
        "contact_person": ("Contact Person", 200),
        "description": ("Application Description", 5000),
    }
    details = {}
    errors = []
    for field, (label, limit) in field_limits.items():
        value = values.get(field, "").strip()
        details[field] = value
        if not value:
            errors.append(f"{label} is required.")
        elif len(value) > limit:
            errors.append(f"{label} must be {limit} characters or fewer.")

    document_bytes = None
    document_filename = None
    upload_too_large = False
    if uploaded_file is not None and uploaded_file.filename:
        file_name = uploaded_file.filename
        extension = file_name.rsplit(".", 1)[-1].lower() if "." in file_name else ""
        if extension != "pdf":
            errors.append("The supporting document must be a PDF file.")
        else:
            document_bytes = uploaded_file.read(MAX_UPLOAD_BYTES + 1)
            if len(document_bytes) > MAX_UPLOAD_BYTES:
                upload_too_large = True
                errors.append(
                    f"The supporting document exceeds the "
                    f"{MAX_UPLOAD_BYTES // (1024 * 1024)} MB limit."
                )
            elif not document_bytes:
                errors.append("The supporting PDF must not be empty.")
            elif b"%PDF-" not in document_bytes[:1024]:
                errors.append(
                    "The supporting document does not have a valid PDF signature."
                )
            else:
                basename = file_name.replace("\\", "/").rsplit("/", 1)[-1]
                document_filename = secure_filename(basename)[:255]
                if not document_filename:
                    document_filename = "supporting-document.pdf"
    elif require_document:
        errors.append("Choose a revised supporting PDF to resubmit this application.")

    return details, errors, document_bytes, document_filename, upload_too_large


@app.route("/institution/applications/new", methods=["GET", "POST"])
def submit_institution_application():
    values = request.form if request.method == "POST" else {}
    errors = []
    upload_too_large = False
    if request.method == "POST":
        details, errors, document_bytes, document_filename, upload_too_large = (
            validate_institution_application(
                request.form,
                request.files.get("supporting_document"),
            )
        )

        if not errors:
            application_id = f"INST-{secrets.token_hex(8).upper()}"
            submitted_at = datetime.now().astimezone().isoformat(timespec="seconds")
            with database_connection() as connection:
                institution_user = connection.execute(
                    "SELECT id FROM users WHERE username = ?",
                    (PUBLIC_DEMO_INSTITUTION_USERNAME,),
                ).fetchone()
                connection.execute(
                    """
                    INSERT INTO institution_applications (
                        application_id, institution_user_id, institution_name,
                        category, academic_year, institution_address,
                        contact_person, contact_email, description,
                        supporting_document, supporting_document_filename,
                        status, submitted_at, public_demo
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'Submitted', ?, 1)
                    """,
                    (
                        application_id,
                        institution_user["id"],
                        details["institution_name"],
                        details["category"],
                        details["academic_year"],
                        details["institution_address"],
                        details["contact_person"],
                        "",
                        details["description"],
                        document_bytes,
                        document_filename,
                        submitted_at,
                    ),
                )
            return redirect(
                url_for(
                    "institution_application_details",
                    application_id=application_id,
                )
            )

    return render_template(
        "institution_application_form.html",
        values=values,
        errors=errors,
        application_categories=APPLICATION_CATEGORIES,
        max_upload_mb=MAX_UPLOAD_BYTES // (1024 * 1024),
        is_edit=False,
    ), 413 if upload_too_large else 400 if errors else 200


@app.route("/institution/applications/<application_id>")
def institution_application_details(application_id):
    with database_connection() as connection:
        application = connection.execute(
            """
            SELECT institution_applications.application_id,
                   institution_applications.institution_name,
                   institution_applications.category,
                   institution_applications.academic_year,
                   institution_applications.institution_address,
                   institution_applications.contact_person,
                   institution_applications.description,
                   institution_applications.supporting_document_filename,
                   institution_applications.status,
                   institution_applications.submitted_at,
                   institution_applications.resubmitted_at,
                   institution_applications.resubmission_count,
                   reviewer_decisions.comment AS reviewer_comment
            FROM institution_applications
            LEFT JOIN reviewer_decisions
                ON reviewer_decisions.application_id =
                   institution_applications.application_id
               AND reviewer_decisions.public_demo = 1
            WHERE institution_applications.application_id = ?
              AND institution_applications.institution_user_id = ?
                            AND institution_applications.public_demo = 1
            """,
                        (application_id, get_public_demo_institution_user_id()),
        ).fetchone()
    if application is None:
        abort(404)
    application = dict(application)
    return render_template(
        "submitted_application_details.html",
        application=application,
        is_reviewer=False,
        reviewer_decisions=(),
        error=None,
        comment="",
    )


@app.route("/institution/applications/<application_id>/edit", methods=["GET", "POST"])
def edit_institution_application(application_id):
    with database_connection() as connection:
        existing_application = connection.execute(
            """
            SELECT application_id, institution_name, category, academic_year,
                     institution_address, contact_person,
                   description, supporting_document_filename, status
            FROM institution_applications
            WHERE application_id = ? AND institution_user_id = ?
                AND public_demo = 1
            """,
                 (application_id, get_public_demo_institution_user_id()),
        ).fetchone()
    if existing_application is None:
        abort(404)
    if existing_application["status"] != "Correction Required":
        abort(409, description="This application is not awaiting corrections.")

    existing_application = dict(existing_application)
    values = request.form if request.method == "POST" else existing_application
    errors = []
    upload_too_large = False
    if request.method == "POST":
        details, errors, document_bytes, document_filename, upload_too_large = (
            validate_institution_application(
                request.form,
                request.files.get("supporting_document"),
                require_document=True,
            )
        )
        if not errors:
            resubmitted_at = datetime.now().astimezone().isoformat(timespec="seconds")
            with database_connection() as connection:
                result = connection.execute(
                    """
                    UPDATE institution_applications
                    SET institution_name = ?, category = ?, academic_year = ?,
                        institution_address = ?, contact_person = ?,
                        description = ?,
                        supporting_document = ?,
                        supporting_document_filename = ?,
                        status = 'Submitted', resubmitted_at = ?,
                        resubmission_count = resubmission_count + 1,
                        latest_validation_report_id = NULL
                    WHERE application_id = ? AND institution_user_id = ?
                      AND status = 'Correction Required'
                                            AND public_demo = 1
                    """,
                    (
                        details["institution_name"],
                        details["category"],
                        details["academic_year"],
                        details["institution_address"],
                        details["contact_person"],
                        details["description"],
                        document_bytes,
                        document_filename,
                        resubmitted_at,
                        application_id,
                        get_public_demo_institution_user_id(),
                    ),
                )
            if result.rowcount != 1:
                abort(409, description="This application is no longer awaiting corrections.")
            return redirect(
                url_for(
                    "institution_application_details",
                    application_id=application_id,
                )
            )

    return render_template(
        "institution_application_form.html",
        values=values,
        errors=errors,
        application_categories=APPLICATION_CATEGORIES,
        max_upload_mb=MAX_UPLOAD_BYTES // (1024 * 1024),
        is_edit=True,
        application_id=application_id,
        current_document=existing_application["supporting_document_filename"],
    ), 413 if upload_too_large else 400 if errors else 200


@app.route("/institution/applications/<application_id>/document")
def download_institution_application_document(application_id):
    with database_connection() as connection:
        document = connection.execute(
            """
            SELECT supporting_document, supporting_document_filename
            FROM institution_applications
            WHERE application_id = ? AND institution_user_id = ?
                            AND public_demo = 1
            """,
                        (application_id, get_public_demo_institution_user_id()),
        ).fetchone()
    if document is None:
        abort(404)
    if document["supporting_document"] is None:
        abort(404)
    return send_file(
        BytesIO(document["supporting_document"]),
        mimetype="application/pdf",
        as_attachment=True,
        download_name=document["supporting_document_filename"],
    )


@app.route("/reviewer")
def dashboard():
    dashboard_applications = load_applications()
    submitted_applications = load_reviewer_inbox()
    current_statuses = [
        application.get("reviewer_decision") or application["status"]
        for application in dashboard_applications
    ]
    current_statuses.extend(
        application["status"] for application in submitted_applications
    )
    status_labels = (
        "Under Review",
        "Pending",
        "Needs Correction",
        "Approved for Demo",
        "Flagged",
        "Validated",
        "Submitted",
        "Correction Required",
        "Approved",
    )
    status_counts = {
        label: current_statuses.count(label) for label in status_labels
    }
    stats = {
        "total": len(applications) + len(submitted_applications),
        "under_review": status_counts["Under Review"],
        "pending": status_counts["Pending"],
        "needs_correction": (
            status_counts["Needs Correction"] + status_counts["Correction Required"]
        ),
        "approved_for_demo": (
            status_counts["Approved for Demo"] + status_counts["Approved"]
        ),
        "flagged": status_counts["Flagged"],
    }
    max_status_count = max(status_counts.values(), default=0)
    status_chart = [
        {
            "label": label,
            "count": count,
            "width": count / max_status_count * 100 if max_status_count else 0,
        }
        for label, count in status_counts.items()
    ]

    risk_counts = load_validation_risk_counts()
    max_risk_count = max(risk_counts.values(), default=0)
    risk_chart = [
        {
            "label": label,
            "count": count,
            "width": count / max_risk_count * 100 if max_risk_count else 0,
        }
        for label, count in risk_counts.items()
    ]

    return render_template(
        "index.html",
        applications=dashboard_applications,
        submitted_applications=submitted_applications,
        stats=stats,
        status_chart=status_chart,
        risk_chart=risk_chart,
        validation_history=load_validation_history(),
    )


@app.route("/application/<application_id>", methods=["GET", "POST"])
def application_details(application_id):
    application = next(
        (item for item in applications if item["id"] == application_id),
        None,
    )
    if application is None:
        abort(404)

    application = dict(application)
    with database_connection() as connection:
        saved_decision = connection.execute(
            """
            SELECT decision, comment
            FROM reviewer_decisions
            WHERE application_id = ? AND public_demo = 1
            """,
            (application_id,),
        ).fetchone()
    if saved_decision is not None:
        application["reviewer_decision"] = saved_decision["decision"]
        application["reviewer_comment"] = saved_decision["comment"]

    error = None
    comment = request.form.get("reviewer_comment", "")
    if request.method == "POST":
        decision = request.form.get("reviewer_decision", "")
        if decision not in REVIEWER_DECISIONS:
            error = "Select a valid simulated reviewer decision."
        else:
            with database_connection() as connection:
                connection.execute(
                    """
                    INSERT INTO reviewer_decisions
                        (application_id, decision, comment, updated_at, public_demo)
                    VALUES (?, ?, ?, ?, 1)
                    ON CONFLICT(application_id) DO UPDATE SET
                        decision = excluded.decision,
                        comment = excluded.comment,
                        updated_at = excluded.updated_at,
                        public_demo = 1
                    """,
                    (
                        application_id,
                        decision,
                        comment.strip(),
                        datetime.now().astimezone().isoformat(timespec="seconds"),
                    ),
                )
            return redirect(url_for("application_details", application_id=application_id))

    return render_template(
        "application_details.html",
        application=application,
        reviewer_decisions=REVIEWER_DECISIONS,
        error=error,
        comment=comment,
    ), 400 if error else 200


@app.route("/reviewer/submissions/<application_id>", methods=["GET", "POST"])
def reviewer_submission_details(application_id):
    error = None
    comment = request.form.get("reviewer_comment", "")
    with database_connection() as connection:
        application = connection.execute(
            """
            SELECT institution_applications.application_id,
                   institution_applications.institution_name,
                   institution_applications.category,
                   institution_applications.academic_year,
                   institution_applications.institution_address,
                   institution_applications.contact_person,
                   institution_applications.description,
                   institution_applications.supporting_document_filename,
                   institution_applications.status,
                   institution_applications.submitted_at,
                   institution_applications.resubmitted_at,
                   institution_applications.resubmission_count,
                   institution_applications.latest_validation_report_id,
                   reviewer_decisions.decision AS reviewer_decision,
                   reviewer_decisions.comment AS reviewer_comment
            FROM institution_applications
            LEFT JOIN reviewer_decisions
                ON reviewer_decisions.application_id =
                   institution_applications.application_id
                 AND reviewer_decisions.public_demo = 1
            WHERE institution_applications.application_id = ?
                AND institution_applications.institution_user_id = ?
                AND institution_applications.public_demo = 1
            """,
                 (application_id, get_public_demo_institution_user_id()),
        ).fetchone()
    if application is None:
        abort(404)
    application = dict(application)
    validation_result = None
    if application["latest_validation_report_id"] is not None:
        with database_connection() as connection:
            validation = connection.execute(
                """
                SELECT id AS report_id, filename, file_type AS document_type,
                       fields_found, fields_missing,
                       validation_status AS status, risk_level,
                       field_results, validated_at
                FROM validation_history
                WHERE id = ? AND application_id = ? AND public_demo = 1
                """,
                (
                    application["latest_validation_report_id"],
                    application_id,
                ),
            ).fetchone()
        if validation is not None:
            validation_result = dict(validation)
            validation_result["field_checks"] = json.loads(
                validation_result.pop("field_results") or "[]"
            )
            validation_result["missing_fields"] = [
                check["label"]
                for check in validation_result["field_checks"]
                if not check["present"]
            ]
            validation_result["findings"] = (
                [
                    f"{len(validation_result['missing_fields'])} sample "
                    "required field(s) were not found."
                ]
                if validation_result["missing_fields"]
                else ["All sample required fields were found."]
            )
            validation_result["risk_reason"] = (
                f"{len(validation_result['missing_fields'])} required field(s) missing."
                if validation_result["missing_fields"]
                else "All required fields are present (0 missing)."
            )
    if request.method == "POST":
        decision = request.form.get("reviewer_decision", "")
        if len(comment) > 1000:
            error = "Reviewer comments must be 1000 characters or fewer."
        elif decision not in REVIEWER_DECISIONS:
            error = "Select a valid simulated reviewer decision."
        elif (
            decision == "Approved for Demo"
            and (
                validation_result is None
                or validation_result["status"] != "Complete"
            )
        ):
            error = (
                "Validate the latest supporting PDF and resolve all missing "
                "required fields before approving this demo application."
            )
        else:
            status_by_decision = {
                "Under Review": "Under Review",
                "Needs Correction": "Correction Required",
                "Approved for Demo": "Approved",
            }
            updated_at = datetime.now().astimezone().isoformat(timespec="seconds")
            with database_connection() as connection:
                connection.execute(
                    """
                    INSERT INTO reviewer_decisions
                        (application_id, decision, comment, updated_at, public_demo)
                    VALUES (?, ?, ?, ?, 1)
                    ON CONFLICT(application_id) DO UPDATE SET
                        decision = excluded.decision,
                        comment = excluded.comment,
                        updated_at = excluded.updated_at,
                        public_demo = 1
                    """,
                    (
                        application_id,
                        decision,
                        comment.strip(),
                        updated_at,
                    ),
                )
                connection.execute(
                    """
                    UPDATE institution_applications
                    SET status = ?
                    WHERE application_id = ? AND public_demo = 1
                    """,
                    (status_by_decision[decision], application_id),
                )
            return redirect(
                url_for(
                    "reviewer_submission_details",
                    application_id=application_id,
                )
            )

    if request.method == "GET":
        comment = application.get("reviewer_comment") or ""
        if application["resubmission_count"] and application["status"] == "Submitted":
            application["previous_reviewer_comment"] = application["reviewer_comment"]
            application["reviewer_decision"] = None
            comment = ""
    if request.args.get("validation_error"):
        error = request.args["validation_error"]
    return render_template(
        "submitted_application_details.html",
        application=application,
        is_reviewer=True,
        validation_result=validation_result,
        reviewer_decisions=REVIEWER_DECISIONS,
        error=error,
        comment=comment,
    ), 400 if error else 200


@app.route("/reviewer/submissions/<application_id>/document")
def download_reviewer_application_document(application_id):
    with database_connection() as connection:
        document = connection.execute(
            """
            SELECT supporting_document, supporting_document_filename
            FROM institution_applications
                        WHERE application_id = ? AND institution_user_id = ?
                            AND public_demo = 1
            """,
                        (application_id, get_public_demo_institution_user_id()),
        ).fetchone()
    if document is None:
        abort(404)
    if document["supporting_document"] is None:
        abort(404)
    return send_file(
        BytesIO(document["supporting_document"]),
        mimetype="application/pdf",
        as_attachment=True,
        download_name=document["supporting_document_filename"],
    )


def analyze_document(file_bytes, filename, extension):
    if extension not in {"pdf", "txt"}:
        return None, "Only PDF and TXT files are supported.", 400
    if len(file_bytes) > MAX_UPLOAD_BYTES:
        return (
            None,
            f"The selected file exceeds the {MAX_UPLOAD_BYTES // (1024 * 1024)} MB limit.",
            413,
        )
    if not file_bytes:
        return None, "The selected file is empty.", 400

    if extension == "pdf":
        if b"%PDF-" not in file_bytes[:1024]:
            return None, "The file does not have a valid PDF signature.", 400
        try:
            reader = PdfReader(BytesIO(file_bytes))
            if reader.is_encrypted:
                return (
                    None,
                    "Password-protected PDFs are not supported in this demo.",
                    400,
                )
            extracted_text = "\n".join(
                page.extract_text() or "" for page in reader.pages
            )
            page_count = len(reader.pages)
        except (PdfReadError, ValueError):
            app.logger.warning("Could not read an uploaded PDF.")
            return (
                None,
                "The PDF could not be read. Check that it is a valid, unencrypted PDF.",
                400,
            )
        document_type = "PDF"
    else:
        if b"\x00" in file_bytes:
            return (
                None,
                "The TXT file contains binary data; upload a UTF-8 text file.",
                400,
            )
        try:
            extracted_text = file_bytes.decode("utf-8-sig")
        except UnicodeDecodeError:
            return None, "The TXT file must use UTF-8 text encoding.", 400
        page_count = None
        document_type = "TXT"

    field_checks = [
        {"label": label, "present": bool(pattern.search(extracted_text))}
        for label, pattern in SAMPLE_REQUIRED_FIELDS.items()
    ]
    missing_fields = [check["label"] for check in field_checks if not check["present"]]
    findings = []
    if missing_fields:
        findings.append(
            f"{len(missing_fields)} sample required field(s) were not found."
        )
    else:
        findings.append("All sample required fields were found.")
    if not extracted_text.strip():
        findings.append(
            "No text was extracted. Image-only PDFs are not processed with OCR in this demo."
        )

    fields_found = len(field_checks) - len(missing_fields)
    fields_missing = len(missing_fields)
    risk_level = (
        "LOW" if fields_missing == 0
        else "MEDIUM" if fields_missing == 1
        else "HIGH"
    )
    validated_at = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    return {
        "filename": filename,
        "document_type": document_type,
        "size_bytes": len(file_bytes),
        "page_count": page_count,
        "extracted_characters": len(extracted_text),
        "field_checks": field_checks,
        "fields_found": fields_found,
        "fields_missing": fields_missing,
        "status": "Incomplete" if missing_fields else "Complete",
        "risk_level": risk_level,
        "risk_reason": (
            f"{fields_missing} required field(s) missing."
            if fields_missing
            else "All required fields are present (0 missing)."
        ),
        "missing_fields": missing_fields,
        "findings": findings,
        "validated_at": validated_at,
    }, None, 200


def save_validation_result(result, application_id=None):
    with database_connection() as connection:
        cursor = connection.execute(
            """
            INSERT INTO validation_history (
                filename, file_type, fields_found, fields_missing,
                validation_status, risk_level, field_results, validated_at,
                application_id, public_demo
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            """,
            (
                result["filename"],
                result["document_type"],
                result["fields_found"],
                result["fields_missing"],
                result["status"],
                result["risk_level"],
                json.dumps(result["field_checks"]),
                result["validated_at"],
                application_id,
            ),
        )
        result["report_id"] = cursor.lastrowid
        if application_id is not None:
            connection.execute(
                """
                UPDATE institution_applications
                SET latest_validation_report_id = ?
                WHERE application_id = ? AND public_demo = 1
                """,
                (result["report_id"], application_id),
            )
    return result


def render_document_validation(error=None, result=None, status=200):
    return render_template(
        "document_validation.html",
        error=error,
        result=result,
        max_upload_mb=MAX_UPLOAD_BYTES // (1024 * 1024),
        sample_required_fields=SAMPLE_REQUIRED_FIELDS,
    ), status


@app.route("/document-validation", methods=["GET", "POST"])
def document_validation():
    if request.method == "GET":
        return render_document_validation()

    uploaded_file = request.files.get("document")
    if uploaded_file is None or not uploaded_file.filename:
        return render_document_validation("Choose a PDF or TXT file to continue.", status=400)

    filename = uploaded_file.filename
    extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    file_bytes = uploaded_file.read(MAX_UPLOAD_BYTES + 1)
    result, error, status = analyze_document(file_bytes, filename, extension)
    if error:
        return render_document_validation(error, status=status)
    result = save_validation_result(result)
    return render_document_validation(result=result)


@app.route("/reviewer/submissions/<application_id>/validate", methods=["POST"])
def validate_institution_application_document(application_id):
    with database_connection() as connection:
        application = connection.execute(
            """
            SELECT supporting_document, supporting_document_filename
            FROM institution_applications
                        WHERE application_id = ? AND institution_user_id = ?
                            AND public_demo = 1
            """,
                        (application_id, get_public_demo_institution_user_id()),
        ).fetchone()
    if application is None:
        abort(404)
    if application["supporting_document"] is None:
        abort(404, description="This application does not have a stored PDF document.")

    file_bytes = application["supporting_document"]
    filename = application["supporting_document_filename"] or "supporting-document.pdf"
    result, error, _status = analyze_document(file_bytes, filename, "pdf")
    if error:
        return redirect(
            url_for(
                "reviewer_submission_details",
                application_id=application_id,
                validation_error=error,
            )
        )
    save_validation_result(result, application_id=application_id)
    return redirect(
        url_for("reviewer_submission_details", application_id=application_id)
    )


@app.route("/validation-report/<int:report_id>/download")
def download_validation_report(report_id):
    with database_connection() as connection:
        report = connection.execute(
            """
            SELECT filename, file_type, fields_found, fields_missing,
                   validation_status, risk_level, field_results, validated_at,
                   application_id
            FROM validation_history
            WHERE id = ? AND public_demo = 1
            """,
            (report_id,),
        ).fetchone()
    if report is None:
        abort(404)

    pdf_file = create_validation_report_pdf(report)
    return send_file(
        pdf_file,
        mimetype="application/pdf",
        as_attachment=True,
        download_name=f"SMART-AI-validation-report-{report_id}.pdf",
    )


@app.errorhandler(RequestEntityTooLarge)
def handle_request_too_large(error):
    if request.endpoint in (
        "submit_institution_application",
        "edit_institution_application",
    ):
        template_values = {
            "values": {},
            "errors": [
                f"The request exceeds the {MAX_UPLOAD_BYTES // (1024 * 1024)} MB "
                "PDF upload limit. Please reduce the PDF size and re-enter the form details."
            ],
            "application_categories": APPLICATION_CATEGORIES,
            "max_upload_mb": MAX_UPLOAD_BYTES // (1024 * 1024),
            "is_edit": False,
        }
        if request.endpoint == "edit_institution_application":
            application_id = (request.view_args or {}).get("application_id")
            with database_connection() as connection:
                application = connection.execute(
                    """
                    SELECT status, supporting_document_filename
                    FROM institution_applications
                                        WHERE application_id = ? AND institution_user_id = ?
                                            AND public_demo = 1
                    """,
                                        (application_id, get_public_demo_institution_user_id()),
                ).fetchone()
            if application is None:
                abort(404)
            if application["status"] != "Correction Required":
                abort(409, description="This application is not awaiting corrections.")
            template_values.update({
                "is_edit": True,
                "application_id": application_id,
                "current_document": application["supporting_document_filename"],
            })
        return render_template(
            "institution_application_form.html",
            **template_values,
        ), 413
    return render_document_validation(
        f"The upload exceeds the {MAX_UPLOAD_BYTES // (1024 * 1024)} MB file limit.",
        status=413,
    )


@app.errorhandler(sqlite3.Error)
def handle_database_error(error):
    app.logger.exception("A local demo database operation failed.")
    return "Local demo storage is temporarily unavailable. Please try again.", 503


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
