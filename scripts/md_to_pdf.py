#!/usr/bin/env python3
"""Convert markdown files to a single styled PDF (Cyrillic + tables)."""

from __future__ import annotations

import re
import sys
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_JUSTIFY, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    HRFlowable,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

FONT_REGULAR = "/System/Library/Fonts/Supplemental/Arial.ttf"
FONT_BOLD = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"
FONT_ITALIC = "/System/Library/Fonts/Supplemental/Arial Italic.ttf"
FONT_BOLD_ITALIC = "/System/Library/Fonts/Supplemental/Arial Bold Italic.ttf"

FAMILY = "ArialRU"


def register_fonts() -> None:
    pdfmetrics.registerFont(TTFont(f"{FAMILY}", FONT_REGULAR))
    pdfmetrics.registerFont(TTFont(f"{FAMILY}-Bold", FONT_BOLD))
    pdfmetrics.registerFont(TTFont(f"{FAMILY}-Italic", FONT_ITALIC))
    pdfmetrics.registerFont(TTFont(f"{FAMILY}-BoldItalic", FONT_BOLD_ITALIC))
    pdfmetrics.registerFontFamily(
        FAMILY,
        normal=f"{FAMILY}",
        bold=f"{FAMILY}-Bold",
        italic=f"{FAMILY}-Italic",
        boldItalic=f"{FAMILY}-BoldItalic",
    )


def md_inline_to_xml(text: str) -> str:
  text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
  text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
  text = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"<i>\1</i>", text)
  return text


def is_table_row(line: str) -> bool:
  s = line.strip()
  return s.startswith("|") and s.endswith("|") and s.count("|") >= 2


def is_separator_row(line: str) -> bool:
  s = line.strip().strip("|")
  return bool(re.fullmatch(r"[\s:\-|]+", s))


def parse_table_row(line: str) -> list[str]:
  return [cell.strip() for cell in line.strip().strip("|").split("|")]


def build_styles() -> dict[str, ParagraphStyle]:
  base = getSampleStyleSheet()
  return {
    "title": ParagraphStyle(
      "Title",
      parent=base["Title"],
      fontName=f"{FAMILY}-Bold",
      fontSize=20,
      leading=24,
      spaceAfter=14,
      textColor=colors.HexColor("#1a1a2e"),
    ),
    "h1": ParagraphStyle(
      "H1",
      parent=base["Heading1"],
      fontName=f"{FAMILY}-Bold",
      fontSize=16,
      leading=20,
      spaceBefore=16,
      spaceAfter=10,
      textColor=colors.HexColor("#16213e"),
    ),
    "h2": ParagraphStyle(
      "H2",
      parent=base["Heading2"],
      fontName=f"{FAMILY}-Bold",
      fontSize=13,
      leading=17,
      spaceBefore=14,
      spaceAfter=8,
      textColor=colors.HexColor("#0f3460"),
    ),
    "h3": ParagraphStyle(
      "H3",
      parent=base["Heading3"],
      fontName=f"{FAMILY}-Bold",
      fontSize=11,
      leading=14,
      spaceBefore=10,
      spaceAfter=6,
      textColor=colors.HexColor("#533483"),
    ),
    "body": ParagraphStyle(
      "Body",
      parent=base["BodyText"],
      fontName=FAMILY,
      fontSize=10,
      leading=14,
      alignment=TA_JUSTIFY,
      spaceAfter=8,
    ),
    "quote": ParagraphStyle(
      "Quote",
      parent=base["BodyText"],
      fontName=f"{FAMILY}-Italic",
      fontSize=10,
      leading=14,
      leftIndent=12,
      textColor=colors.HexColor("#333333"),
      spaceAfter=8,
    ),
    "cell": ParagraphStyle(
      "Cell",
      parent=base["BodyText"],
      fontName=FAMILY,
      fontSize=8,
      leading=10,
      alignment=TA_LEFT,
    ),
    "cell_header": ParagraphStyle(
      "CellHeader",
      parent=base["BodyText"],
      fontName=f"{FAMILY}-Bold",
      fontSize=8,
      leading=10,
      alignment=TA_LEFT,
      textColor=colors.white,
    ),
  }


def make_table(rows: list[list[str]], styles: dict[str, ParagraphStyle], available_width: float):
  if not rows:
    return None

  col_count = max(len(r) for r in rows)
  data: list[list[Paragraph]] = []
  for i, row in enumerate(rows):
    padded = row + [""] * (col_count - len(row))
    style = styles["cell_header"] if i == 0 else styles["cell"]
    data.append([Paragraph(md_inline_to_xml(cell), style) for cell in padded])

  col_width = available_width / col_count
  table = Table(data, colWidths=[col_width] * col_count, repeatRows=1)
  table.setStyle(
    TableStyle(
      [
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0f3460")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f4f6fb")]),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#c5cee0")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
      ]
    )
  )
  return table


def parse_markdown(md_text: str, styles: dict[str, ParagraphStyle], available_width: float):
  flow = []
  lines = md_text.splitlines()
  i = 0

  while i < len(lines):
    line = lines[i]
    stripped = line.strip()

    if not stripped:
      i += 1
      continue

    if stripped == "---":
      flow.append(Spacer(1, 6))
      flow.append(HRFlowable(width="100%", thickness=0.6, color=colors.HexColor("#c5cee0")))
      flow.append(Spacer(1, 6))
      i += 1
      continue

    if stripped.startswith("# "):
      flow.append(Paragraph(md_inline_to_xml(stripped[2:].strip()), styles["title"]))
      i += 1
      continue

    if stripped.startswith("## "):
      flow.append(Paragraph(md_inline_to_xml(stripped[3:].strip()), styles["h1"]))
      i += 1
      continue

    if stripped.startswith("### "):
      flow.append(Paragraph(md_inline_to_xml(stripped[4:].strip()), styles["h3"]))
      i += 1
      continue

    if is_table_row(line):
      table_rows: list[list[str]] = []
      while i < len(lines) and is_table_row(lines[i]):
        if not is_separator_row(lines[i]):
          table_rows.append(parse_table_row(lines[i]))
        i += 1
      table = make_table(table_rows, styles, available_width)
      if table:
        flow.append(Spacer(1, 4))
        flow.append(table)
        flow.append(Spacer(1, 8))
      continue

    para_lines = [stripped]
    i += 1
    while i < len(lines):
      nxt = lines[i].strip()
      if (
        not nxt
        or nxt == "---"
        or nxt.startswith("#")
        or is_table_row(lines[i])
      ):
        break
      para_lines.append(nxt)
      i += 1

    text = " ".join(para_lines)
    style = styles["quote"] if text.startswith('"') else styles["body"]
    flow.append(Paragraph(md_inline_to_xml(text), style))

  return flow


def build_pdf(sources: list[tuple[str, Path]], output: Path) -> None:
  register_fonts()
  styles = build_styles()

  doc = SimpleDocTemplate(
    str(output),
    pagesize=A4,
    leftMargin=2 * cm,
    rightMargin=2 * cm,
    topMargin=2 * cm,
    bottomMargin=2 * cm,
    title="Сюжет 1 — Королевская биржа",
    author="Королевская биржа",
  )

  available_width = A4[0] - doc.leftMargin - doc.rightMargin
  story = []

  for idx, (section_title, path) in enumerate(sources):
    if idx > 0:
      story.append(PageBreak())
      story.append(Paragraph(section_title, styles["title"]))
      story.append(Spacer(1, 10))

    md_text = path.read_text(encoding="utf-8")
    story.extend(parse_markdown(md_text, styles, available_width))

  doc.build(story)


def main() -> int:
  root = Path(__file__).resolve().parents[1]
  sources = [
    ("Сюжет 1", root / "Сюжет 1.md"),
    ("Сюжет 1. Речь", root / "Сюжет 1. Речь.md"),
  ]
  output = root / "Сюжет 1 — полный.pdf"

  for _, path in sources:
    if not path.exists():
      print(f"Файл не найден: {path}", file=sys.stderr)
      return 1

  build_pdf(sources, output)
  print(output)
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
