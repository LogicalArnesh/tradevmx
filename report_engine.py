"""TradeXVm :: institutional PDF research reports (ReportLab).

Generates a multi-section A4 research note:

1. hero panel  - instrument, CMP, change, VWAP, bias
2. executive summary
3. KPI grid
4. AI desk verdict + trade plan (entry / invalidation / T1 / T2 / R:R)
5. signal tape
6. key support / resistance ladder around CMP
7. data-quality and compliance footer

``ReportEngine.render`` returns a :class:`ReportArtifact` carrying both the file
path (for email attachments) and an in-memory buffer (for Telegram's
``send_document``), so no temporary reads are needed by callers.

Note: the base-14 Helvetica fonts do not carry the rupee glyph, therefore all
amounts are printed as plain numbers and the page header declares ``INR``.
"""

from __future__ import annotations

import io
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Sequence
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas as pdfcanvas
from reportlab.platypus import (
    BaseDocTemplate,
    Flowable,
    Frame,
    HRFlowable,
    KeepTogether,
    PageBreak,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)

from config import IST, Settings, get_settings

if TYPE_CHECKING:  # pragma: no cover
    from ai_engine import TradePlan
    from market_scanner import MarketSnapshot, OrderBlock

LOGGER = logging.getLogger(__name__)

PAGE_WIDTH, PAGE_HEIGHT = A4
CONTENT_WIDTH = PAGE_WIDTH - 2 * 16 * mm  # 178 mm

NAVY = colors.HexColor("#0B1E33")
STEEL = colors.HexColor("#1F3A5F")
TEAL = colors.HexColor("#0E8C8A")
BULL = colors.HexColor("#0F7B3F")
BEAR = colors.HexColor("#B3261E")
NEUTRAL = colors.HexColor("#5B6773")
INK = colors.HexColor("#16202B")
MUTED = colors.HexColor("#6B7785")
RULE = colors.HexColor("#D8E0E8")
PANEL = colors.HexColor("#F3F6F9")
PANEL_ALT = colors.HexColor("#E9EFF5")
WHITE = colors.white


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------
def inr(value: float | None, digits: int = 2) -> str:
    """Indian digit grouping (12,34,567.89)."""
    if value is None or not isinstance(value, (int, float)) or not math.isfinite(value):
        return "-"
    negative = value < 0
    whole, fraction = f"{abs(value):.{digits}f}".split(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups: list[str] = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join(groups + [tail])
    return f"{'-' if negative else ''}{whole}.{fraction}"


def signed_pct(value: float | None) -> str:
    if value is None or not math.isfinite(value):
        return "-"
    return f"{value:+.2f}%"


def _safe(text: Any) -> str:
    return escape(str(text), {"'": "&apos;", '"': "&quot;"})


def _bias_colour(bias: str) -> colors.Color:
    return {"BULLISH": BULL, "BEARISH": BEAR}.get(bias.upper(), NEUTRAL)


def _verdict_colour(verdict: str) -> colors.Color:
    return {"BUY": BULL, "SELL": BEAR}.get(verdict.upper(), NEUTRAL)


# ---------------------------------------------------------------------------
# Artifact
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class ReportArtifact:
    """A rendered PDF, available as path *and* in-memory bytes."""

    path: Path
    filename: str
    buffer: io.BytesIO
    title: str = "TradeXVm Research"
    generated_at: datetime = field(default_factory=lambda: datetime.now(IST))

    def read(self) -> bytes:
        return self.buffer.getvalue()

    @property
    def size_kb(self) -> float:
        return round(len(self.read()) / 1024.0, 1)


# ---------------------------------------------------------------------------
# Canvas with "Page x of y"
# ---------------------------------------------------------------------------
class _NumberedCanvas(pdfcanvas.Canvas):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._saved_page_states: list[dict[str, Any]] = []

    def showPage(self) -> None:  # noqa: N802 - ReportLab API
        self._saved_page_states.append(dict(self.__dict__))
        self._startPage()

    def save(self) -> None:
        total = len(self._saved_page_states)
        for state in self._saved_page_states:
            self.__dict__.update(state)
            self._draw_footer(total)
            super().showPage()
        super().save()

    def _draw_footer(self, total: int) -> None:
        self.saveState()
        self.setStrokeColor(RULE)
        self.setLineWidth(0.6)
        self.line(16 * mm, 15 * mm, PAGE_WIDTH - 16 * mm, 15 * mm)
        self.setFont("Helvetica", 6.8)
        self.setFillColor(MUTED)
        self.drawString(
            16 * mm,
            10.3 * mm,
            "TradeXVm Research  |  Prices in INR  |  Educational analysis, not investment advice.",
        )
        self.drawRightString(
            PAGE_WIDTH - 16 * mm, 10.3 * mm, f"Page {self._pageNumber} of {total}"
        )
        self.restoreState()


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------
class ReportEngine:
    """Builds institutional research PDFs."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        try:
            self.settings.report_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:  # pragma: no cover
            LOGGER.error("Cannot create report directory %s: %s", self.settings.report_dir, exc)
        self._styles = self._build_styles()

    # ------------------------------------------------------------- styles
    def _build_styles(self) -> dict[str, ParagraphStyle]:
        base = getSampleStyleSheet()
        return {
            "Overline": ParagraphStyle(
                "Overline", parent=base["Normal"], fontName="Helvetica-Bold",
                fontSize=7.2, leading=9, textColor=TEAL, spaceAfter=2,
            ),
            "DocTitle": ParagraphStyle(
                "DocTitle", parent=base["Title"], fontName="Helvetica-Bold",
                fontSize=19, leading=22, textColor=NAVY, alignment=TA_LEFT, spaceAfter=2,
            ),
            "Meta": ParagraphStyle(
                "Meta", parent=base["Normal"], fontName="Helvetica",
                fontSize=8, leading=11, textColor=MUTED,
            ),
            "HeroLabel": ParagraphStyle(
                "HeroLabel", parent=base["Normal"], fontName="Helvetica-Bold",
                fontSize=6.6, leading=8, textColor=colors.HexColor("#8FA6BD"),
            ),
            "HeroPrice": ParagraphStyle(
                "HeroPrice", parent=base["Normal"], fontName="Helvetica-Bold",
                fontSize=21, leading=23, textColor=WHITE,
            ),
            "HeroValue": ParagraphStyle(
                "HeroValue", parent=base["Normal"], fontName="Helvetica-Bold",
                fontSize=13, leading=15, textColor=WHITE,
            ),
            "HeroBias": ParagraphStyle(
                "HeroBias", parent=base["Normal"], fontName="Helvetica-Bold",
                fontSize=15, leading=17, textColor=WHITE,
            ),
            "HeroSub": ParagraphStyle(
                "HeroSub", parent=base["Normal"], fontName="Helvetica",
                fontSize=7.4, leading=9.6, textColor=colors.HexColor("#C7D6E4"),
            ),
            "Section": ParagraphStyle(
                "Section", parent=base["Normal"], fontName="Helvetica-Bold",
                fontSize=9.6, leading=12, textColor=NAVY, spaceBefore=6, spaceAfter=1,
                keepWithNext=1,
            ),
            "Body": ParagraphStyle(
                "Body", parent=base["Normal"], fontName="Helvetica",
                fontSize=8.8, leading=12.6, textColor=INK, spaceAfter=3,
            ),
            "BodySmall": ParagraphStyle(
                "BodySmall", parent=base["Normal"], fontName="Helvetica",
                fontSize=7.4, leading=10, textColor=MUTED,
            ),
            "Bullet": ParagraphStyle(
                "Bullet", parent=base["Normal"], fontName="Helvetica",
                fontSize=8.4, leading=11.4, textColor=INK, leftIndent=8,
                bulletIndent=1, spaceAfter=1.5,
            ),
            "KpiLabel": ParagraphStyle(
                "KpiLabel", parent=base["Normal"], fontName="Helvetica",
                fontSize=6.8, leading=8.6, textColor=MUTED,
            ),
            "KpiValue": ParagraphStyle(
                "KpiValue", parent=base["Normal"], fontName="Helvetica-Bold",
                fontSize=10.4, leading=12.6, textColor=NAVY,
            ),
            "KpiNote": ParagraphStyle(
                "KpiNote", parent=base["Normal"], fontName="Helvetica",
                fontSize=6.2, leading=8, textColor=MUTED,
            ),
            "Cell": ParagraphStyle(
                "Cell", parent=base["Normal"], fontName="Helvetica",
                fontSize=7.4, leading=9.6, textColor=INK,
            ),
            "CellRight": ParagraphStyle(
                "CellRight", parent=base["Normal"], fontName="Helvetica",
                fontSize=7.4, leading=9.6, textColor=INK, alignment=TA_RIGHT,
            ),
            "CellHead": ParagraphStyle(
                "CellHead", parent=base["Normal"], fontName="Helvetica-Bold",
                fontSize=6.8, leading=8.6, textColor=WHITE,
            ),
            "CellHeadRight": ParagraphStyle(
                "CellHeadRight", parent=base["Normal"], fontName="Helvetica-Bold",
                fontSize=6.8, leading=8.6, textColor=WHITE, alignment=TA_RIGHT,
            ),
            "Verdict": ParagraphStyle(
                "Verdict", parent=base["Normal"], fontName="Helvetica-Bold",
                fontSize=15, leading=17, textColor=WHITE,
            ),
            "PlanLabel": ParagraphStyle(
                "PlanLabel", parent=base["Normal"], fontName="Helvetica",
                fontSize=6.6, leading=8.4, textColor=MUTED,
            ),
            "PlanValue": ParagraphStyle(
                "PlanValue", parent=base["Normal"], fontName="Helvetica-Bold",
                fontSize=10, leading=12, textColor=NAVY,
            ),
        }

    # ------------------------------------------------------------- public
    def render(self, snapshot: "MarketSnapshot", plan: "TradePlan | None" = None) -> ReportArtifact:
        """Single-instrument research note."""
        slug = _slug(snapshot.display_name)
        stamp = snapshot.as_of.strftime("%Y%m%d_%H%M")
        filename = f"TradeXVm_{slug}_{stamp}.pdf"
        story: list[Flowable] = self._instrument_story(snapshot, plan, include_page_header=True)
        artifact = self._build(
            story,
            filename=filename,
            title=f"{snapshot.display_name} - Intraday Research",
            subject=f"{snapshot.display_name} {snapshot.timeframe} desk note",
        )
        LOGGER.info("Rendered %s (%.1f KB)", artifact.filename, artifact.size_kb)
        return artifact

    def render_portfolio(
        self,
        snapshots: Sequence["MarketSnapshot"],
        plans: dict[str, "TradePlan"] | None = None,
        *,
        title: str = "End of Day Wrap",
    ) -> ReportArtifact:
        """Multi-instrument wrap-up (one page per instrument + summary)."""
        plans = plans or {}
        stamp = datetime.now(IST).strftime("%Y%m%d_%H%M")
        filename = f"TradeXVm_{_slug(title)}_{stamp}.pdf"

        story: list[Flowable] = [self._document_header(title, f"{len(snapshots)} instruments")]
        story.append(Spacer(1, 2 * mm))
        story.append(self._section_block("Session Snapshot", [self._summary_table(snapshots, plans)]))
        story.append(Spacer(1, 4 * mm))
        story.append(
            Paragraph(
                "Detail pages follow: one page per instrument with the full metric set, "
                "AI desk verdict and the live support/resistance ladder.",
                self._styles["BodySmall"],
            )
        )

        for snapshot in snapshots:
            story.append(PageBreak())
            story.extend(self._instrument_story(snapshot, plans.get(snapshot.symbol), include_page_header=True))

        artifact = self._build(
            story,
            filename=filename,
            title=title,
            subject=f"TradeXVm {title}",
        )
        LOGGER.info("Rendered portfolio %s (%.1f KB)", artifact.filename, artifact.size_kb)
        return artifact

    # -------------------------------------------------------------- build
    def _build(self, story: list[Flowable], *, filename: str, title: str, subject: str) -> ReportArtifact:
        buffer = io.BytesIO()
        doc = BaseDocTemplate(
            buffer,
            pagesize=A4,
            leftMargin=16 * mm,
            rightMargin=16 * mm,
            topMargin=25 * mm,
            bottomMargin=19 * mm,
            title=title,
            author="TradeXVm Market Desk",
            subject=subject,
            creator="TradeXVm",
        )
        frame = Frame(
            doc.leftMargin,
            doc.bottomMargin,
            doc.width,
            doc.height,
            id="body",
            leftPadding=0,
            rightPadding=0,
            topPadding=0,
            bottomPadding=0,
        )
        doc.addPageTemplates(
            [PageTemplate(id="main", frames=[frame], onPage=self._draw_page_header)]
        )
        doc.build(story, canvasmaker=_NumberedCanvas)

        data = buffer.getvalue()
        buffer.seek(0)
        path = self.settings.report_dir / filename
        try:
            path.write_bytes(data)
        except OSError as exc:  # pragma: no cover
            LOGGER.error("Could not persist %s: %s", path, exc)
        return ReportArtifact(path=path, filename=filename, buffer=buffer, title=title)

    @staticmethod
    def _draw_page_header(canvas: pdfcanvas.Canvas, doc: BaseDocTemplate) -> None:
        canvas.saveState()
        canvas.setFillColor(NAVY)
        canvas.rect(0, PAGE_HEIGHT - 16 * mm, PAGE_WIDTH, 16 * mm, stroke=0, fill=1)
        canvas.setFillColor(WHITE)
        canvas.setFont("Helvetica-Bold", 10.5)
        canvas.drawString(16 * mm, PAGE_HEIGHT - 10 * mm, "TRADEXVM")
        canvas.setFillColor(colors.HexColor("#8FA6BD"))
        canvas.setFont("Helvetica", 7)
        canvas.drawRightString(
            PAGE_WIDTH - 16 * mm,
            PAGE_HEIGHT - 9.6 * mm,
            "INSTITUTIONAL MARKET DESK  |  NSE / BSE  |  PRICES IN INR",
        )
        canvas.setStrokeColor(TEAL)
        canvas.setLineWidth(1.4)
        canvas.line(16 * mm, PAGE_HEIGHT - 16 * mm, PAGE_WIDTH - 16 * mm, PAGE_HEIGHT - 16 * mm)
        canvas.restoreState()

    # ------------------------------------------------------------ sections
    def _document_header(self, title: str, subtitle: str) -> Flowable:
        now = datetime.now(IST)
        left = [
            Paragraph("TRADEXVM RESEARCH", self._styles["Overline"]),
            Paragraph(_safe(title), self._styles["DocTitle"]),
            Paragraph(
                f"{_safe(subtitle)} &nbsp;|&nbsp; Generated {now.strftime('%d %b %Y, %H:%M')} IST",
                self._styles["Meta"],
            ),
        ]
        table = Table([[left]], colWidths=[CONTENT_WIDTH])
        table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, -1), PANEL),
                    ("BOX", (0, 0), (-1, -1), 0.8, RULE),
                    ("LEFTPADDING", (0, 0), (-1, -1), 9),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 9),
                    ("TOPPADDING", (0, 0), (-1, -1), 8),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
                    ("LINEBEFORE", (0, 0), (0, 0), 3, TEAL),
                ]
            )
        )
        return table

    def _section(self, title: str) -> list[Flowable]:
        return [
            Paragraph(_safe(title.upper()), self._styles["Section"]),
            HRFlowable(width="100%", thickness=0.6, color=RULE, spaceBefore=0, spaceAfter=3),
        ]

    def _section_block(self, title: str, body: Sequence[Flowable]) -> Flowable:
        """Heading, rule and body kept on one page where they fit."""
        return KeepTogether([*self._section(title), *body])

    def _instrument_story(
        self, snapshot: "MarketSnapshot", plan: "TradePlan | None", *, include_page_header: bool
    ) -> list[Flowable]:
        story: list[Flowable] = []
        if include_page_header:
            story.extend(
                [
                    Paragraph("INTRADAY RESEARCH NOTE", self._styles["Overline"]),
                    Paragraph(_safe(snapshot.display_name), self._styles["DocTitle"]),
                    Paragraph(
                        f"{_safe(snapshot.symbol)} &nbsp;|&nbsp; {_safe(snapshot.timeframe)} timeframe "
                        f"&nbsp;|&nbsp; {snapshot.as_of.strftime('%d %b %Y, %H:%M:%S')} IST "
                        f"&nbsp;|&nbsp; Feed: {_safe(snapshot.ltp_source)}",
                        self._styles["Meta"],
                    ),
                    Spacer(1, 3 * mm),
                ]
            )

        story.append(self._hero_panel(snapshot, plan))
        story.append(Spacer(1, 4 * mm))
        story.append(
            self._section_block(
                "Executive Summary",
                [Paragraph(_safe(self._executive_summary(snapshot, plan)), self._styles["Body"])],
            )
        )
        story.append(Spacer(1, 3 * mm))
        story.append(self._section_block("Market Metrics", [self._kpi_grid(snapshot)]))
        story.append(Spacer(1, 4 * mm))
        story.append(self._section_block("AI Desk Verdict", [self._plan_panel(snapshot, plan)]))
        story.append(Spacer(1, 4 * mm))
        story.append(self._section_block("Signal Tape", [self._signal_table(snapshot)]))
        story.append(Spacer(1, 4 * mm))
        # Levels and data-quality notes travel together so a break never leaves a
        # page holding nothing but the disclaimer.
        story.append(
            KeepTogether(
                [
                    *self._section("Key Levels"),
                    *self._level_ladder(snapshot),
                    Spacer(1, 4 * mm),
                    *self._section("Data Quality & Disclosures"),
                    self._disclosures(snapshot),
                ]
            )
        )
        return story

    # --------------------------------------------------------------- hero
    def _hero_panel(self, snapshot: "MarketSnapshot", plan: "TradePlan | None") -> Flowable:
        change_colour = BULL if snapshot.change_pct >= 0 else BEAR
        arrow = "\u25b2" if snapshot.change_pct >= 0 else "\u25bc"
        trend_word = "above" if snapshot.cmp >= snapshot.vwap else "below"

        left = [
            Paragraph(_safe(f"{snapshot.display_name}  |  {snapshot.timeframe}"), self._styles["HeroLabel"]),
            Spacer(1, 1.5 * mm),
            Paragraph(inr(snapshot.cmp), self._styles["HeroPrice"]),
            Paragraph(
                f'<font color="{change_colour.hexval()}">{arrow} {signed_pct(snapshot.change_pct)}</font>'
                f'<font color="#C7D6E4"> vs prev close {inr(snapshot.prev_close)}</font>',
                self._styles["HeroSub"],
            ),
        ]
        middle = [
            Paragraph("SESSION VWAP", self._styles["HeroLabel"]),
            Spacer(1, 1.5 * mm),
            Paragraph(inr(snapshot.vwap), self._styles["HeroValue"]),
            Paragraph(
                f"{trend_word} by {abs(snapshot.vwap_distance_pct):.2f}%  |  ATR {inr(snapshot.atr)}",
                self._styles["HeroSub"],
            ),
        ]
        bias_colour = _bias_colour(snapshot.bias)
        posture = plan.verdict if plan else snapshot.bias.title()
        right = [
            Paragraph("DESK BIAS", self._styles["HeroLabel"]),
            Spacer(1, 1.5 * mm),
            Paragraph(
                f'<font color="{bias_colour.hexval()}">{_safe(snapshot.bias)}</font>',
                self._styles["HeroBias"],
            ),
            Paragraph(
                f"Structure: {_safe(snapshot.structure)}  |  Stance: {_safe(posture)}",
                self._styles["HeroSub"],
            ),
        ]

        table = Table(
            [[left, middle, right]],
            colWidths=[CONTENT_WIDTH * 0.40, CONTENT_WIDTH * 0.31, CONTENT_WIDTH * 0.29],
        )
        table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, -1), NAVY),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 9),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 9),
                    ("TOPPADDING", (0, 0), (-1, -1), 9),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
                    ("LINEBEFORE", (1, 0), (-1, 0), 0.5, colors.HexColor("#2C4A6B")),
                ]
            )
        )
        return table

    # --------------------------------------------------------------- kpis
    def _kpi_grid(self, snapshot: "MarketSnapshot") -> Flowable:
        ratio = snapshot.volume_ratio
        ratio_text = f"{ratio:.2f}x" if isinstance(ratio, float) and math.isfinite(ratio) else "-"
        if snapshot.is_index:
            volume_value, volume_note = "n/a", "indices publish no volume"
        elif isinstance(ratio, float) and math.isfinite(ratio):
            volume_value = ratio_text
            volume_note = f"{snapshot.volume:,.0f} vs {snapshot.volume_sma:,.0f}"
        else:
            volume_value, volume_note = "-", "insufficient history"

        cells = [
            ("DAY OPEN", inr(snapshot.day_open), "session first print"),
            ("DAY HIGH", inr(snapshot.day_high), "running extreme"),
            ("DAY LOW", inr(snapshot.day_low), "running extreme"),
            ("PREVIOUS CLOSE", inr(snapshot.prev_close), "daily series"),
            ("CMP", inr(snapshot.cmp), f"feed: {snapshot.ltp_source}"),
            ("VWAP", inr(snapshot.vwap), snapshot.vwap_source),
            ("ATR (14)", inr(snapshot.atr), "Wilder volatility"),
            ("VOLUME vs SMA", volume_value, volume_note),
        ]
        rows = []
        for start in range(0, len(cells), 4):
            row = []
            for label, value, note in cells[start : start + 4]:
                row.append(
                    [
                        Paragraph(_safe(label), self._styles["KpiLabel"]),
                        Paragraph(_safe(value), self._styles["KpiValue"]),
                        Paragraph(_safe(note), self._styles["KpiNote"]),
                    ]
                )
            rows.append(row)

        table = Table(rows, colWidths=[CONTENT_WIDTH / 4.0] * 4)
        table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, -1), PANEL),
                    ("GRID", (0, 0), (-1, -1), 0.5, RULE),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 7),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 7),
                    ("TOPPADDING", (0, 0), (-1, -1), 6),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                ]
            )
        )
        return table

    # --------------------------------------------------------------- plan
    def _plan_panel(self, snapshot: "MarketSnapshot", plan: "TradePlan | None") -> Flowable:
        if plan is None:
            body = Paragraph(
                "No AI verdict available for this instrument.", self._styles["Body"]
            )
            return Table([[body]], colWidths=[CONTENT_WIDTH])

        verdict_colour = _verdict_colour(plan.verdict)
        header = Table(
            [
                [
                    Paragraph(_safe(plan.verdict), self._styles["Verdict"]),
                    Paragraph(
                        f"Confidence {plan.confidence * 100:.0f}%  |  Engine {_safe(plan.model)}"
                        f"  |  Horizon {_safe(plan.horizon)}",
                        self._styles["HeroSub"],
                    ),
                ]
            ],
            colWidths=[CONTENT_WIDTH * 0.28, CONTENT_WIDTH * 0.72],
        )
        header.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, -1), verdict_colour),
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 9),
                    ("TOPPADDING", (0, 0), (-1, -1), 7),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
                ]
            )
        )

        if plan.verdict == "NO TRADE":
            levels = Table(
                [[Paragraph("Desk stance: capital preservation. No trade is the trade.", self._styles["Body"])]],
                colWidths=[CONTENT_WIDTH],
            )
            levels.setStyle(
                TableStyle(
                    [
                        ("BACKGROUND", (0, 0), (-1, -1), PANEL),
                        ("BOX", (0, 0), (-1, -1), 0.5, RULE),
                        ("LEFTPADDING", (0, 0), (-1, -1), 9),
                        ("TOPPADDING", (0, 0), (-1, -1), 6),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                    ]
                )
            )
        else:
            rr = f"1:{plan.risk_reward:.2f}"
            columns = [
                ("ENTRY ZONE", f"{inr(plan.entry_low)} - {inr(plan.entry_high)}"),
                ("INVALIDATION", inr(plan.stop_loss)),
                ("TARGET 1", inr(plan.target_1)),
                ("TARGET 2", inr(plan.target_2)),
                ("RISK : REWARD", rr),
            ]
            cells = [
                [
                    Paragraph(_safe(label), self._styles["PlanLabel"]),
                    Paragraph(_safe(value), self._styles["PlanValue"]),
                ]
                for label, value in columns
            ]
            levels = Table(
                [cells],
                colWidths=[CONTENT_WIDTH * w for w in (0.27, 0.18, 0.18, 0.18, 0.19)],
            )
            levels.setStyle(
                TableStyle(
                    [
                        ("BACKGROUND", (0, 0), (-1, -1), PANEL_ALT),
                        ("GRID", (0, 0), (-1, -1), 0.5, RULE),
                        ("VALIGN", (0, 0), (-1, -1), "TOP"),
                        ("LEFTPADDING", (0, 0), (-1, -1), 7),
                        ("RIGHTPADDING", (0, 0), (-1, -1), 7),
                        ("TOPPADDING", (0, 0), (-1, -1), 6),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                    ]
                )
            )

        story: list[Flowable] = [header, Spacer(1, 1.5 * mm), levels, Spacer(1, 2 * mm)]
        for reason in plan.rationale:
            story.append(Paragraph(_safe(reason), self._styles["Bullet"], bulletText="\u2022"))
        if plan.invalidation:
            story.append(
                Paragraph(
                    f"<b>Invalidation:</b> {_safe(plan.invalidation)}", self._styles["Body"]
                )
            )
        for note in plan.notes:
            story.append(Paragraph(f"<i>{_safe(note)}</i>", self._styles["BodySmall"]))
        if plan.is_fallback:
            story.append(
                Paragraph(
                    "Groq was unavailable or declined to answer; this plan is generated by the "
                    "deterministic ATR rule engine and carries reduced confidence.",
                    self._styles["BodySmall"],
                )
            )

        wrapper = Table([[story]], colWidths=[CONTENT_WIDTH])
        wrapper.setStyle(
            TableStyle(
                [
                    ("BOX", (0, 0), (-1, -1), 0.7, RULE),
                    ("LEFTPADDING", (0, 0), (-1, -1), 0),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                    ("TOPPADDING", (0, 0), (-1, -1), 0),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                ]
            )
        )
        return wrapper

    # ------------------------------------------------------------ signals
    def _signal_table(self, snapshot: "MarketSnapshot") -> Flowable:
        head = [
            Paragraph("SETUP", self._styles["CellHead"]),
            Paragraph("TF", self._styles["CellHead"]),
            Paragraph("DIRECTION", self._styles["CellHead"]),
            Paragraph("SEV", self._styles["CellHeadRight"]),
            Paragraph("DETAIL", self._styles["CellHead"]),
        ]
        rows: list[list[Any]] = [head]
        style_commands: list[tuple[Any, ...]] = []

        signals = sorted(snapshot.signals, key=lambda s: s.severity, reverse=True)
        if not signals:
            rows.append(
                [
                    Paragraph("No qualifying setup on the current tape.", self._styles["Cell"]),
                    "", "", "", "",
                ]
            )
        else:
            for signal in signals:
                direction_colour = _bias_colour(
                    {"bullish": "BULLISH", "bearish": "BEARISH"}.get(signal.direction, "NEUTRAL")
                )
                rows.append(
                    [
                        Paragraph(_safe(signal.label), self._styles["Cell"]),
                        Paragraph(_safe(signal.timeframe), self._styles["Cell"]),
                        Paragraph(
                            f'<font color="{direction_colour.hexval()}">'
                            f"{_safe(signal.direction.upper())}</font>",
                            self._styles["Cell"],
                        ),
                        Paragraph(str(signal.severity), self._styles["CellRight"]),
                        Paragraph(_safe(signal.detail), self._styles["Cell"]),
                    ]
                )

        table = Table(
            rows,
            colWidths=[CONTENT_WIDTH * 0.20, CONTENT_WIDTH * 0.07, CONTENT_WIDTH * 0.12,
                       CONTENT_WIDTH * 0.06, CONTENT_WIDTH * 0.55],
            repeatRows=1,
        )
        base_style = [
            ("BACKGROUND", (0, 0), (-1, 0), NAVY),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("GRID", (0, 0), (-1, -1), 0.4, RULE),
            ("LEFTPADDING", (0, 0), (-1, -1), 5),
            ("RIGHTPADDING", (0, 0), (-1, -1), 5),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [WHITE, PANEL]),
        ]
        table.setStyle(TableStyle(base_style + style_commands))
        return table

    # ------------------------------------------------------------- levels
    def _level_ladder(self, snapshot: "MarketSnapshot") -> list[Flowable]:
        rows: list[list[Any]] = [
            [
                Paragraph("ROLE", self._styles["CellHead"]),
                Paragraph("LEVEL", self._styles["CellHeadRight"]),
                Paragraph("SOURCE", self._styles["CellHead"]),
                Paragraph("DISTANCE", self._styles["CellHeadRight"]),
            ]
        ]
        highlight_row: int | None = None

        for level in sorted(snapshot.resistances, key=lambda lv: lv.price, reverse=True):
            rows.append(self._level_row(level, snapshot.cmp, "RESISTANCE"))
        highlight_row = len(rows)
        rows.append(
            [
                Paragraph("<b>CMP</b>", self._styles["Cell"]),
                Paragraph(f"<b>{inr(snapshot.cmp)}</b>", self._styles["CellRight"]),
                Paragraph("live", self._styles["Cell"]),
                Paragraph("0.00%", self._styles["CellRight"]),
            ]
        )
        for level in sorted(snapshot.supports, key=lambda lv: lv.price, reverse=True):
            rows.append(self._level_row(level, snapshot.cmp, "SUPPORT"))

        table = Table(
            rows,
            colWidths=[CONTENT_WIDTH * 0.20, CONTENT_WIDTH * 0.22, CONTENT_WIDTH * 0.38,
                       CONTENT_WIDTH * 0.20],
            repeatRows=1,
        )
        commands: list[tuple[Any, ...]] = [
            ("BACKGROUND", (0, 0), (-1, 0), NAVY),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("GRID", (0, 0), (-1, -1), 0.4, RULE),
            ("LEFTPADDING", (0, 0), (-1, -1), 5),
            ("RIGHTPADDING", (0, 0), (-1, -1), 5),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("BACKGROUND", (0, highlight_row), (-1, highlight_row), PANEL_ALT),
        ]
        table.setStyle(TableStyle(commands))

        trailing: list[Flowable] = [table, Spacer(1, 2 * mm)]
        if snapshot.order_blocks:
            trailing.append(
                Paragraph(
                    "<b>Live order blocks:</b> "
                    + "; ".join(
                        f"{ob.direction} {inr(ob.bottom)}-{inr(ob.top)} ({ob.state})"
                        for ob in snapshot.order_blocks[:3]
                    ),
                    self._styles["BodySmall"],
                )
            )
        return trailing

    def _level_row(self, level: Any, price: float, role: str) -> list[Any]:
        distance = (level.price - price) / price * 100.0 if price else 0.0
        colour = BEAR if role == "RESISTANCE" else BULL
        return [
            Paragraph(f'<font color="{colour.hexval()}">{role}</font>', self._styles["Cell"]),
            Paragraph(inr(level.price), self._styles["CellRight"]),
            Paragraph(_safe(level.source), self._styles["Cell"]),
            Paragraph(f"{distance:+.2f}%", self._styles["CellRight"]),
        ]

    # -------------------------------------------------------------- misc
    def _summary_table(
        self, snapshots: Sequence["MarketSnapshot"], plans: dict[str, "TradePlan"]
    ) -> Flowable:
        rows: list[list[Any]] = [
            [
                Paragraph("INSTRUMENT", self._styles["CellHead"]),
                Paragraph("CMP", self._styles["CellHeadRight"]),
                Paragraph("CHG", self._styles["CellHeadRight"]),
                Paragraph("BIAS", self._styles["CellHead"]),
                Paragraph("STRUCTURE", self._styles["CellHead"]),
                Paragraph("TOP SETUP / STANCE", self._styles["CellHead"]),
            ]
        ]
        for snapshot in snapshots:
            plan = plans.get(snapshot.symbol)
            top = max(snapshot.signals, key=lambda s: s.severity, default=None)
            descriptor = top.label if top else "-"
            if plan is not None:
                descriptor = f"{descriptor} | {plan.verdict}"
            colour = _bias_colour(snapshot.bias)
            rows.append(
                [
                    Paragraph(
                        f"<b>{_safe(snapshot.display_name)}</b>", self._styles["Cell"]
                    ),
                    Paragraph(inr(snapshot.cmp), self._styles["CellRight"]),
                    Paragraph(f"{snapshot.change_pct:+.2f}%", self._styles["CellRight"]),
                    Paragraph(
                        f'<font color="{colour.hexval()}">{_safe(snapshot.bias)}</font>',
                        self._styles["Cell"],
                    ),
                    Paragraph(_safe(snapshot.structure), self._styles["Cell"]),
                    Paragraph(_safe(descriptor), self._styles["Cell"]),
                ]
            )

        table = Table(
            rows,
            colWidths=[CONTENT_WIDTH * 0.19, CONTENT_WIDTH * 0.14, CONTENT_WIDTH * 0.10,
                       CONTENT_WIDTH * 0.12, CONTENT_WIDTH * 0.17, CONTENT_WIDTH * 0.28],
            repeatRows=1,
        )
        table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), NAVY),
                    ("GRID", (0, 0), (-1, -1), 0.4, RULE),
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 5),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                    ("TOPPADDING", (0, 0), (-1, -1), 4),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                    ("ROWBACKGROUNDS", (0, 1), (-1, -1), [WHITE, PANEL]),
                ]
            )
        )
        return table

    def _executive_summary(self, snapshot: "MarketSnapshot", plan: "TradePlan | None") -> str:
        side = "above" if snapshot.cmp >= snapshot.vwap else "below"
        parts = [
            f"{snapshot.display_name} is trading at {inr(snapshot.cmp)} ({signed_pct(snapshot.change_pct)}) "
            f"against the previous close of {inr(snapshot.prev_close)}, {side} the session VWAP of "
            f"{inr(snapshot.vwap)} by {abs(snapshot.vwap_distance_pct):.2f}%."
        ]
        parts.append(f"Market structure reads {snapshot.structure}: {snapshot.bias_reason}.")

        signals = sorted(snapshot.signals, key=lambda s: s.severity, reverse=True)
        if signals:
            headline = "; ".join(f"{s.label} ({s.timeframe}, severity {s.severity}/5)" for s in signals[:3])
            parts.append(f"The tape triggered {len(signals)} setup(s): {headline}.")
        else:
            parts.append(
                "No qualifying VWAP, volume, Open=High/Low or order-block event is live on this timeframe."
            )

        if isinstance(snapshot.volume_ratio, float) and math.isfinite(snapshot.volume_ratio):
            parts.append(
                f"Participation is {snapshot.volume_ratio:.2f}x the "
                f"{self.settings.volume_sma_period}-bar average, with the session range "
                f"{inr(snapshot.day_low)}-{inr(snapshot.day_high)}."
            )

        if plan is None:
            parts.append("No AI verdict is attached; treat the metric set as raw evidence.")
        elif plan.verdict == "NO TRADE":
            parts.append(
                "Desk stance is NO TRADE: the evidence does not clear the desk's risk contract."
            )
        else:
            parts.append(
                f"Desk stance is {plan.verdict} with a {inr(plan.entry_low)}-{inr(plan.entry_high)} "
                f"entry zone, invalidation at {inr(plan.stop_loss)} and a first target at "
                f"{inr(plan.target_1)} (1:{plan.risk_reward:.2f} reward-to-risk)."
            )
        return " ".join(parts)

    def _disclosures(self, snapshot: "MarketSnapshot") -> Flowable:
        lines = [
            f"Feed: {snapshot.ltp_source}; VWAP method: {snapshot.vwap_source}; "
            f"bars: {snapshot.timeframe} with {snapshot.confirm_timeframe or 'n/a'} confirmation.",
        ]
        if snapshot.warnings:
            lines.extend(f"Data note: {w}" for w in snapshot.warnings)
        lines.append(
            "This document is produced algorithmically from public market data for educational and "
            "research purposes. It is not investment advice, not a solicitation, and carries no "
            "guarantee of accuracy or timeliness. Derivatives and equities carry risk of loss."
        )
        return Paragraph("<br/>".join(_safe(line) for line in lines), self._styles["BodySmall"])


def _slug(text: str) -> str:
    cleaned = "".join(ch if ch.isalnum() else "_" for ch in text.upper())
    return "_".join(part for part in cleaned.split("_") if part) or "REPORT"
