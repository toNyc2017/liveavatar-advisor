"""Render an analysis dict into a styled .pptx deck.

This module exists because Pandoc's default Markdown→PPTX output looks like
a 2003 academic handout. We instead use python-pptx with deliberate design
choices: a financial-services-credible palette, consistent typography, and
layouts that adapt to content (short stat vs. long quote, etc.).

If you want to retheme the deck, edit the STYLE dict at the top.
"""
from __future__ import annotations

from pathlib import Path
from datetime import date
from typing import Iterable

from pptx import Presentation
from pptx.util import Inches, Pt, Emu
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import PP_ALIGN, MSO_ANCHOR, MSO_AUTO_SIZE


# ---------------------------------------------------------------------------
# Style — change palette / fonts here.
# ---------------------------------------------------------------------------
STYLE = {
    # Colors
    "navy": RGBColor(0x0B, 0x2A, 0x4A),         # primary — title, callout fill, watermark
    "navy_deep": RGBColor(0x07, 0x1C, 0x33),    # darker variant for the callout band
    "gray_dark": RGBColor(0x33, 0x3E, 0x48),    # body text on white
    "gray_mid": RGBColor(0x6B, 0x76, 0x80),     # captions, subtle text, footer rule
    "gray_light": RGBColor(0xE6, 0xEA, 0xEE),   # watermark, hairline rules
    "accent": RGBColor(0xC0, 0x53, 0x21),       # accent rule under titles
    "white": RGBColor(0xFF, 0xFF, 0xFF),
    "off_white": RGBColor(0xF8, 0xF6, 0xF1),    # subtle warm paper, callout label

    # Typography — Calibri ships everywhere.
    "font_family": "Calibri",

    # Sizes
    "size_title": Pt(40),
    "size_subtitle": Pt(20),
    "size_slide_title": Pt(28),
    "size_body": Pt(18),
    "size_bullet": Pt(16),
    "size_caption": Pt(11),
    "size_watermark": Pt(120),
    "size_callout_label": Pt(11),

    # Slide geometry — 16:9 widescreen
    "slide_width": Inches(13.333),
    "slide_height": Inches(7.5),
    "band_width": Inches(0.35),     # navy left edge
    "margin_left": Inches(0.95),    # content starts after the band + padding
    "margin_right": Inches(0.6),
    "margin_top": Inches(0.5),
    "margin_bottom": Inches(0.5),
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _set_run(run, *, text=None, size=None, bold=False, italic=False, color=None, font=None):
    if text is not None:
        run.text = text
    if size is not None:
        run.font.size = size
    if bold:
        run.font.bold = True
    if italic:
        run.font.italic = True
    if color is not None:
        run.font.color.rgb = color
    run.font.name = font or STYLE["font_family"]


def _solid_fill(shape, color: RGBColor):
    shape.fill.solid()
    shape.fill.fore_color.rgb = color


def _no_line(shape):
    shape.line.fill.background()


def _add_rect(slide, left, top, width, height, color):
    rect = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, left, top, width, height)
    _solid_fill(rect, color)
    _no_line(rect)
    return rect


def _add_text(
    slide,
    text: str,
    *,
    left,
    top,
    width,
    height,
    size,
    color,
    bold=False,
    italic=False,
    align=PP_ALIGN.LEFT,
    anchor=MSO_ANCHOR.TOP,
    font=None,
    auto_shrink=False,
):
    tb = slide.shapes.add_textbox(left, top, width, height)
    tf = tb.text_frame
    tf.word_wrap = True
    if auto_shrink:
        # PowerPoint will shrink-to-fit if text overflows the box.
        tf.auto_size = MSO_AUTO_SIZE.TEXT_TO_FIT_SHAPE
    tf.margin_left = Emu(0)
    tf.margin_right = Emu(0)
    tf.margin_top = Emu(0)
    tf.margin_bottom = Emu(0)
    tf.vertical_anchor = anchor
    p = tf.paragraphs[0]
    p.alignment = align
    run = p.add_run()
    _set_run(run, text=text, size=size, bold=bold, italic=italic, color=color, font=font)
    return tb


def _add_bullets(
    slide,
    items: Iterable[str],
    *,
    left,
    top,
    width,
    height,
    size,
    color,
):
    tb = slide.shapes.add_textbox(left, top, width, height)
    tf = tb.text_frame
    tf.word_wrap = True
    tf.auto_size = MSO_AUTO_SIZE.TEXT_TO_FIT_SHAPE
    tf.margin_left = Emu(0)
    tf.margin_right = Emu(0)
    tf.margin_top = Emu(0)
    tf.margin_bottom = Emu(0)
    first = True
    for item in items:
        if first:
            p = tf.paragraphs[0]
            first = False
        else:
            p = tf.add_paragraph()
        p.alignment = PP_ALIGN.LEFT
        p.space_after = Pt(8)
        run = p.add_run()
        _set_run(run, text=f"•  {item}", size=size, color=color)
    return tb


def _callout_text_style(text: str) -> tuple:
    """Pick a font size + style mode for a callout based on text length.

    Short strings ("5.3%", "$300B") get the big-number treatment.
    Longer strings get progressively smaller and shift into pull-quote mode
    (italic, with quote marks) once they're clearly sentences rather than
    statistics. This is the core overflow fix — instead of hoping a 36pt
    number fits in a 4" box, we measure the source and scale.
    """
    s = text.strip().strip('"“”')
    n = len(s)
    if n <= 12:
        return Pt(44), False        # big number / short stat
    if n <= 24:
        return Pt(34), False        # medium stat
    if n <= 60:
        return Pt(22), False        # short phrase
    if n <= 140:
        return Pt(16), True         # pull-quote (italic)
    return Pt(13), True             # long pull-quote (italic, smaller)


# ---------------------------------------------------------------------------
# Slide chrome — the navy band + watermark + footer that every content slide gets
# ---------------------------------------------------------------------------
def _add_content_chrome(slide, *, page_label: str | None = None, watermark: str | None = None):
    """Decorations consistent across every content slide.

    - Navy band on the left edge (continuity with title slide)
    - Optional huge translucent slide-number watermark in the upper right
    - Hairline gray rule + page label in the lower right
    """
    # Navy left band
    _add_rect(
        slide,
        Inches(0), Inches(0),
        STYLE["band_width"], STYLE["slide_height"],
        STYLE["navy"],
    )

    # Slide-number watermark (light gray) in the upper right
    if watermark:
        _add_text(
            slide,
            watermark,
            left=Inches(11.0),
            top=Inches(-0.4),
            width=Inches(2.2),
            height=Inches(2.0),
            size=STYLE["size_watermark"],
            color=STYLE["gray_light"],
            bold=True,
            align=PP_ALIGN.RIGHT,
        )

    # Footer: hairline rule + page label
    _add_rect(
        slide,
        STYLE["margin_left"],
        Inches(7.05),
        Inches(11.7),
        Emu(9525),  # ~0.01"
        STYLE["gray_light"],
    )
    if page_label:
        _add_text(
            slide,
            page_label,
            left=Inches(10.5),
            top=Inches(7.1),
            width=Inches(2.2),
            height=Inches(0.3),
            size=STYLE["size_caption"],
            color=STYLE["gray_mid"],
            align=PP_ALIGN.RIGHT,
        )


# ---------------------------------------------------------------------------
# Visual compositions — shape-based diagrams drawn on the concept slides.
# Inherit the deck palette automatically; no images, no external assets.
# ---------------------------------------------------------------------------
def _draw_process_flow(slide, steps: list[dict], left, top, width, height):
    """Horizontal sequence: N labeled boxes with arrows between them.

    Each step is {label, note?}. The label sits inside a navy box; the
    note (optional) is rendered below the box in a smaller gray font.
    Arrows are simple rust right-arrows in the gap between boxes.
    """
    steps = [s for s in steps if s and s.get("label")][:5]
    if not steps:
        return

    n = len(steps)
    # 75% of width is boxes, 25% is gaps for arrows
    gap_total = width * 0.18
    box_total = width - gap_total
    box_w = int(box_total / n)
    gap_w = int(gap_total / max(n - 1, 1)) if n > 1 else 0

    box_h = Inches(1.0)
    label_h = box_h
    note_h = Inches(0.6)

    cur_left = left
    for i, step in enumerate(steps):
        # Navy box
        rect = slide.shapes.add_shape(
            MSO_SHAPE.ROUNDED_RECTANGLE,
            cur_left, top, box_w, box_h,
        )
        _solid_fill(rect, STYLE["navy"])
        _no_line(rect)
        # Label inside the box
        _add_text(
            slide,
            str(step["label"]).strip(),
            left=cur_left,
            top=top,
            width=box_w,
            height=label_h,
            size=Pt(16),
            color=STYLE["white"],
            bold=True,
            align=PP_ALIGN.CENTER,
            anchor=MSO_ANCHOR.MIDDLE,
            auto_shrink=True,
        )
        # Optional sub-caption beneath
        note = (step.get("note") or "").strip()
        if note:
            _add_text(
                slide,
                note,
                left=cur_left,
                top=top + box_h + Inches(0.1),
                width=box_w,
                height=note_h,
                size=Pt(10),
                color=STYLE["gray_mid"],
                align=PP_ALIGN.CENTER,
                anchor=MSO_ANCHOR.TOP,
                auto_shrink=True,
            )
        # Arrow to the next box (if any)
        if i < n - 1:
            arrow_left = cur_left + box_w
            arrow_top = top + (box_h // 2) - Inches(0.12)
            arrow = slide.shapes.add_shape(
                MSO_SHAPE.RIGHT_ARROW,
                arrow_left + Inches(0.05),
                arrow_top,
                gap_w - Inches(0.1),
                Inches(0.24),
            )
            _solid_fill(arrow, STYLE["accent"])
            _no_line(arrow)
        cur_left += box_w + gap_w


def _draw_comparison(slide, left_side: dict, right_side: dict, left, top, width, height):
    """Two columns side by side: title + bullet list each side, separated by
    a thin gray rule. Designed for before/after, without/with, vendor A vs B."""
    if not left_side or not right_side:
        return
    col_w = (width - Inches(0.4)) // 2
    rule_left = left + col_w + Inches(0.15)

    # Thin vertical divider
    _add_rect(
        slide,
        rule_left, top + Inches(0.2),
        Emu(9525), height - Inches(0.4),
        STYLE["gray_light"],
    )

    for col_idx, (side, col_left) in enumerate(
        [(left_side, left), (right_side, left + col_w + Inches(0.4))]
    ):
        title = (side.get("title") or "").strip().upper()
        points = [p for p in (side.get("points") or []) if p][:4]

        # Column eyebrow (rust)
        if title:
            _add_text(
                slide,
                title,
                left=col_left,
                top=top,
                width=col_w,
                height=Inches(0.3),
                size=Pt(11),
                color=STYLE["accent"],
                bold=True,
                align=PP_ALIGN.LEFT,
            )

        # Bullets
        if points:
            tb = slide.shapes.add_textbox(
                col_left,
                top + Inches(0.4),
                col_w,
                height - Inches(0.5),
            )
            tf = tb.text_frame
            tf.word_wrap = True
            tf.auto_size = MSO_AUTO_SIZE.TEXT_TO_FIT_SHAPE
            tf.margin_left = Emu(0)
            tf.margin_right = Emu(0)
            tf.margin_top = Emu(0)
            tf.margin_bottom = Emu(0)
            for i, point in enumerate(points):
                p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
                p.alignment = PP_ALIGN.LEFT
                p.space_after = Pt(6)
                run = p.add_run()
                _set_run(
                    run,
                    text=f"•  {point}",
                    size=Pt(13),
                    color=STYLE["gray_dark"],
                )


def _draw_triangle(slide, tiers: list[str], left, top, width, height):
    """Pyramid with 3-4 tiers. Top tier is narrowest (navy), middle is mid-
    width (mid-tone), base is widest (light). Tier labels are centered."""
    tiers = [str(t).strip() for t in (tiers or []) if t]
    if not tiers or len(tiers) < 2:
        return
    n = len(tiers[:4])
    tiers = tiers[:n]

    tier_h = (height - Inches(0.2)) // n
    base_w = min(width, Inches(7.5))
    centerline = left + width // 2

    colors = [STYLE["navy"], STYLE["gray_dark"], STYLE["gray_mid"], STYLE["gray_light"]]
    text_colors = [STYLE["white"], STYLE["white"], STYLE["white"], STYLE["gray_dark"]]

    for i, label in enumerate(tiers):
        # Tier width grows linearly from top (narrowest) to base (widest)
        frac = (i + 1) / n
        tw = int(base_w * (0.35 + 0.65 * frac))
        tl = centerline - tw // 2
        tt = top + i * tier_h
        rect = slide.shapes.add_shape(
            MSO_SHAPE.TRAPEZOID if i == 0 else MSO_SHAPE.RECTANGLE,
            tl, tt, tw, tier_h - Inches(0.05),
        )
        _solid_fill(rect, colors[i % len(colors)])
        _no_line(rect)
        _add_text(
            slide,
            label,
            left=tl,
            top=tt,
            width=tw,
            height=tier_h - Inches(0.05),
            size=Pt(14),
            color=text_colors[i % len(text_colors)],
            bold=True,
            align=PP_ALIGN.CENTER,
            anchor=MSO_ANCHOR.MIDDLE,
            auto_shrink=True,
        )


def _draw_stack(slide, items: list[dict], left, top, width, height):
    """Horizontal row of N blocks (3-5). Each block has a bold label and an
    optional small note beneath. Good for parallel-component frameworks
    like Stan's PILL or 'the four enterprise categories.'"""
    items = [it for it in items if it and it.get("label")][:5]
    if not items:
        return
    n = len(items)
    gap = Inches(0.12)
    block_w = (width - gap * (n - 1)) // n

    # Alternating navy and accent for visual rhythm. Two-tone keeps it from
    # looking like a generic bar chart.
    fills = [STYLE["navy"], STYLE["gray_dark"]]
    text_color = STYLE["white"]

    label_h = Inches(1.4)
    note_h = height - label_h - Inches(0.1)

    for i, item in enumerate(items):
        bl = left + i * (block_w + gap)
        # Block
        rect = slide.shapes.add_shape(
            MSO_SHAPE.RECTANGLE,
            bl, top, block_w, label_h,
        )
        _solid_fill(rect, fills[i % len(fills)])
        _no_line(rect)
        # Rust top-edge accent
        _add_rect(
            slide, bl, top, block_w, Inches(0.08), STYLE["accent"],
        )
        # Label centered in block
        _add_text(
            slide,
            str(item["label"]).strip(),
            left=bl,
            top=top,
            width=block_w,
            height=label_h,
            size=Pt(16),
            color=text_color,
            bold=True,
            align=PP_ALIGN.CENTER,
            anchor=MSO_ANCHOR.MIDDLE,
            auto_shrink=True,
        )
        # Optional note below the block
        note = (item.get("note") or "").strip()
        if note:
            _add_text(
                slide,
                note,
                left=bl,
                top=top + label_h + Inches(0.1),
                width=block_w,
                height=note_h,
                size=Pt(10),
                color=STYLE["gray_mid"],
                align=PP_ALIGN.CENTER,
                anchor=MSO_ANCHOR.TOP,
                auto_shrink=True,
            )


def _draw_visual(slide, visual: dict, left, top, width, height) -> bool:
    """Dispatch to the appropriate visual drawer. Returns True if anything
    was drawn — False if the visual dict was malformed or had an unsupported
    kind. The caller can use that to decide whether to fall back to the
    supporting_data callout."""
    if not visual:
        return False
    kind = (visual.get("kind") or "").strip().lower()
    try:
        if kind == "process_flow":
            _draw_process_flow(slide, visual.get("steps") or [], left, top, width, height)
            return True
        if kind == "comparison":
            _draw_comparison(slide, visual.get("left") or {}, visual.get("right") or {},
                             left, top, width, height)
            return True
        if kind == "triangle":
            _draw_triangle(slide, visual.get("tiers") or [], left, top, width, height)
            return True
        if kind == "stack":
            _draw_stack(slide, visual.get("items") or [], left, top, width, height)
            return True
    except Exception:
        # A malformed visual should never break the deck. Fall through and let
        # the caller render the callout instead.
        return False
    return False


# ---------------------------------------------------------------------------
# Slide builders
# ---------------------------------------------------------------------------
def _build_title_slide(prs, analysis: dict, source: dict | None):
    blank = prs.slide_layouts[6]
    slide = prs.slides.add_slide(blank)

    # Navy band on the left
    _add_rect(
        slide,
        Inches(0), Inches(0),
        STYLE["band_width"], STYLE["slide_height"],
        STYLE["navy"],
    )

    title = analysis.get("title", "Untitled")
    core = analysis.get("core_message", "")

    # Eyebrow tag at the top
    _add_text(
        slide,
        "EXECUTIVE SUMMARY",
        left=STYLE["margin_left"],
        top=Inches(1.0),
        width=Inches(8),
        height=Inches(0.3),
        size=STYLE["size_caption"],
        color=STYLE["gray_mid"],
        bold=True,
    )

    _add_text(
        slide,
        title,
        left=STYLE["margin_left"],
        top=Inches(1.55),
        width=Inches(11.4),
        height=Inches(2.6),
        size=STYLE["size_title"],
        color=STYLE["navy"],
        bold=True,
        auto_shrink=True,
    )

    # Accent rule
    _add_rect(
        slide,
        STYLE["margin_left"],
        Inches(4.25),
        Inches(1.6),
        Emu(57150),  # ~0.06"
        STYLE["accent"],
    )

    _add_text(
        slide,
        core,
        left=STYLE["margin_left"],
        top=Inches(4.55),
        width=Inches(11.4),
        height=Inches(1.8),
        size=STYLE["size_subtitle"],
        color=STYLE["gray_dark"],
        auto_shrink=True,
    )

    # Footer: source + date
    today = date.today().strftime("%B %Y")
    footer_bits = []
    if source:
        if source.get("title"):
            footer_bits.append(str(source["title"]))
        if source.get("url"):
            footer_bits.append(str(source["url"]))
    footer_bits.append(today)
    footer = "  ·  ".join(footer_bits)
    _add_text(
        slide,
        footer,
        left=STYLE["margin_left"],
        top=Inches(6.85),
        width=Inches(11.7),
        height=Inches(0.4),
        size=STYLE["size_caption"],
        color=STYLE["gray_mid"],
    )


def _build_concept_slide(prs, concept: dict, index: int, total: int, total_pages: int):
    blank = prs.slide_layouts[6]
    slide = prs.slides.add_slide(blank)

    # Page number = title slide (1) + this concept's 1-based index.
    page_number = 1 + index
    # Slide chrome (navy band + watermark + footer)
    _add_content_chrome(
        slide,
        page_label=f"{page_number} / {total_pages}",
        watermark=f"{index:02d}",
    )

    headline = concept.get("headline", "")
    explanation = concept.get("explanation", "")
    data = concept.get("supporting_data")
    visual = concept.get("visual")

    # Decide layout. A valid `visual` reorganizes the slide: title shrinks,
    # explanation gets the upper half full-width, visual fills the lower
    # half. supporting_data demotes to a small caption beneath the
    # explanation. When no visual is provided we keep the existing
    # right-column callout (or full-width prose) layout.
    has_visual = bool(visual and (visual.get("kind") or "").strip())

    # Eyebrow
    _add_text(
        slide,
        f"INSIGHT {index} / {total}",
        left=STYLE["margin_left"],
        top=Inches(0.55),
        width=Inches(6),
        height=Inches(0.3),
        size=STYLE["size_caption"],
        color=STYLE["accent"],
        bold=True,
    )

    if has_visual:
        # Compact title to give the visual room
        _add_text(
            slide,
            headline,
            left=STYLE["margin_left"],
            top=Inches(0.95),
            width=Inches(10.5),
            height=Inches(1.0),
            size=STYLE["size_slide_title"],
            color=STYLE["navy"],
            bold=True,
            auto_shrink=True,
        )
        _add_rect(
            slide,
            STYLE["margin_left"], Inches(2.0),
            Inches(1.2), Emu(57150),
            STYLE["accent"],
        )

        # Explanation paragraph
        _add_text(
            slide,
            explanation,
            left=STYLE["margin_left"],
            top=Inches(2.3),
            width=Inches(11.4),
            height=Inches(1.5),
            size=STYLE["size_body"],
            color=STYLE["gray_dark"],
            auto_shrink=True,
        )

        # Optional caption row carrying supporting_data
        next_top = Inches(3.85)
        if data:
            caption = f"KEY DATA  ·  {str(data).strip()}"
            _add_text(
                slide,
                caption,
                left=STYLE["margin_left"],
                top=next_top,
                width=Inches(11.4),
                height=Inches(0.3),
                size=Pt(11),
                color=STYLE["accent"],
                bold=True,
                auto_shrink=True,
            )
            next_top = Inches(4.2)

        # Visual fills the lower half
        drew = _draw_visual(
            slide, visual,
            left=STYLE["margin_left"],
            top=next_top,
            width=Inches(11.4),
            height=Inches(6.8) - next_top,
        )
        if not drew and data:
            # The visual was malformed AND we already drew the caption — that
            # would leave the slide bottom empty. Fall through to a callout box.
            has_visual = False  # signal the fallback below
        elif drew:
            return

    # No visual (or fallback): keep the prior layout.
    _add_text(
        slide,
        headline,
        left=STYLE["margin_left"],
        top=Inches(0.95),
        width=Inches(10.5),
        height=Inches(1.5),
        size=STYLE["size_slide_title"],
        color=STYLE["navy"],
        bold=True,
        auto_shrink=True,
    )
    _add_rect(
        slide,
        STYLE["margin_left"], Inches(2.45),
        Inches(1.2), Emu(57150),
        STYLE["accent"],
    )

    if data:
        # Right-column navy callout (unchanged behavior)
        _add_text(
            slide,
            explanation,
            left=STYLE["margin_left"],
            top=Inches(2.9),
            width=Inches(7.2),
            height=Inches(3.8),
            size=STYLE["size_body"],
            color=STYLE["gray_dark"],
            auto_shrink=True,
        )
        callout_left = Inches(9.0)
        callout_top = Inches(2.9)
        callout_w = Inches(3.85)
        callout_h = Inches(3.8)
        _add_rect(slide, callout_left, callout_top, callout_w, callout_h, STYLE["navy"])
        _add_rect(slide, callout_left, callout_top, callout_w, Inches(0.18), STYLE["accent"])
        _add_text(
            slide, "KEY DATA",
            left=callout_left + Inches(0.35),
            top=callout_top + Inches(0.45),
            width=callout_w - Inches(0.7),
            height=Inches(0.35),
            size=STYLE["size_callout_label"],
            color=STYLE["off_white"],
            bold=True,
        )
        data_str = str(data).strip()
        size, is_quote = _callout_text_style(data_str)
        display = f"“{data_str}”" if is_quote else data_str
        _add_text(
            slide, display,
            left=callout_left + Inches(0.35),
            top=callout_top + Inches(1.0),
            width=callout_w - Inches(0.7),
            height=callout_h - Inches(1.2),
            size=size,
            color=STYLE["white"],
            bold=not is_quote,
            italic=is_quote,
            anchor=MSO_ANCHOR.TOP,
            auto_shrink=True,
        )
    else:
        _add_text(
            slide,
            explanation,
            left=STYLE["margin_left"],
            top=Inches(2.9),
            width=Inches(11.7),
            height=Inches(3.8),
            size=STYLE["size_body"],
            color=STYLE["gray_dark"],
            auto_shrink=True,
        )


def _build_critical_data_slide(prs, data_points: list[str], page_index: int, total_pages: int):
    if not data_points:
        return
    blank = prs.slide_layouts[6]
    slide = prs.slides.add_slide(blank)
    _add_content_chrome(
        slide,
        page_label=f"{page_index} / {total_pages}",
        watermark="#",
    )

    _add_text(
        slide,
        "BY THE NUMBERS",
        left=STYLE["margin_left"],
        top=Inches(0.55),
        width=Inches(6),
        height=Inches(0.3),
        size=STYLE["size_caption"],
        color=STYLE["accent"],
        bold=True,
    )
    _add_text(
        slide,
        "The numbers worth remembering",
        left=STYLE["margin_left"],
        top=Inches(0.95),
        width=Inches(11.5),
        height=Inches(1.0),
        size=STYLE["size_slide_title"],
        color=STYLE["navy"],
        bold=True,
    )
    _add_rect(
        slide,
        STYLE["margin_left"],
        Inches(2.1),
        Inches(1.2),
        Emu(57150),
        STYLE["accent"],
    )

    _add_bullets(
        slide,
        data_points[:8],
        left=STYLE["margin_left"],
        top=Inches(2.55),
        width=Inches(11.7),
        height=Inches(4.4),
        size=STYLE["size_bullet"],
        color=STYLE["gray_dark"],
    )


def _build_takeaways_slide(
    prs,
    takeaways: list[str],
    questions: list[str] | None,
    page_index: int,
    total_pages: int,
):
    blank = prs.slide_layouts[6]
    slide = prs.slides.add_slide(blank)
    _add_content_chrome(
        slide,
        page_label=f"{page_index} / {total_pages}",
        watermark="→",  # → arrow watermark for the "what to do" slide
    )

    _add_text(
        slide,
        "WHAT TO DO",
        left=STYLE["margin_left"],
        top=Inches(0.55),
        width=Inches(6),
        height=Inches(0.3),
        size=STYLE["size_caption"],
        color=STYLE["accent"],
        bold=True,
    )
    _add_text(
        slide,
        "Takeaways",
        left=STYLE["margin_left"],
        top=Inches(0.95),
        width=Inches(11.5),
        height=Inches(1.0),
        size=STYLE["size_slide_title"],
        color=STYLE["navy"],
        bold=True,
    )
    _add_rect(
        slide,
        STYLE["margin_left"],
        Inches(2.1),
        Inches(1.2),
        Emu(57150),
        STYLE["accent"],
    )

    if questions:
        _add_bullets(
            slide,
            takeaways,
            left=STYLE["margin_left"],
            top=Inches(2.55),
            width=Inches(6.0),
            height=Inches(4.4),
            size=STYLE["size_bullet"],
            color=STYLE["gray_dark"],
        )
        _add_text(
            slide,
            "QUESTIONS TO DISCUSS",
            left=Inches(7.2),
            top=Inches(2.55),
            width=Inches(5.7),
            height=Inches(0.35),
            size=STYLE["size_callout_label"],
            color=STYLE["accent"],
            bold=True,
        )
        _add_bullets(
            slide,
            questions,
            left=Inches(7.2),
            top=Inches(3.0),
            width=Inches(5.7),
            height=Inches(4.0),
            size=STYLE["size_bullet"],
            color=STYLE["gray_dark"],
        )
    else:
        _add_bullets(
            slide,
            takeaways,
            left=STYLE["margin_left"],
            top=Inches(2.55),
            width=Inches(11.7),
            height=Inches(4.4),
            size=STYLE["size_bullet"],
            color=STYLE["gray_dark"],
        )


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------
def render_pptx(analysis: dict, output_path: Path, source: dict | None = None) -> Path:
    """Build slides.pptx from a structured analysis dict.

    Layout:
      - Slide 1: title + core message + source/date
      - Slide 2..N: one concept per slide (navy callout box if data present)
      - Optional: "By the Numbers" slide if critical_data has items
      - Final slide: takeaways (and questions if present)
    """
    prs = Presentation()
    prs.slide_width = STYLE["slide_width"]
    prs.slide_height = STYLE["slide_height"]

    # Count total content slides ahead of time so the footer page labels are right.
    concepts = analysis.get("key_concepts") or []
    critical = analysis.get("critical_data") or []
    takeaways = analysis.get("actionable_takeaways") or []
    questions = analysis.get("discussion_questions") or []

    total_pages = 1 + len(concepts) + (1 if critical else 0) + (1 if (takeaways or questions) else 0)

    _build_title_slide(prs, analysis, source)

    for i, concept in enumerate(concepts, start=1):
        _build_concept_slide(prs, concept, i, len(concepts), total_pages)

    next_page = 1 + len(concepts) + 1  # +1 for title, +1 for next visible page number
    if critical:
        _build_critical_data_slide(prs, critical, next_page, total_pages)
        next_page += 1

    if takeaways or questions:
        _build_takeaways_slide(prs, takeaways, questions or None, next_page, total_pages)

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    prs.save(str(output_path))
    return output_path
