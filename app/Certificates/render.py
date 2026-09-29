"""Draw an insurance or reinsurance certificate as a PDF, from the values `assemble` resolved.

The layout follows the market form the client supplied (Reinsurance Certificates Info.xlsx, sheet
"Reinsurance certificate"): the certificate (sections 1-8, signature), the Letter of Undertaking and
the Schedule of Parties to whom Notice is to be Given — with the issuing company's header, footer
and images (certificate.settings / certificate.asset) and the issuing user as signatory, in place of
the broker's. `data["issuer"]` carries the text; `images` the logo, stamp and signature bytes.

Everything here is a pure function of its arguments: the same snapshot and images always draw the
same document, which is what lets the history keep the values beside the PDF and trust them.
"""
import io
from datetime import date
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (Image, KeepTogether, PageBreak, Paragraph, SimpleDocTemplate,
                                Spacer, Table, TableStyle)

from reportlab.graphics import renderPDF
from reportlab.lib.utils import ImageReader

from .formatting import join_names, long_date, money, percent_number, percent_words, roman

PAGE_W, PAGE_H = A4
MARGIN_X = 20 * mm
TOP = 38 * mm
BOTTOM = 25 * mm
INDENT = 16 * mm          # section body, after the section number
SUB = 28 * mm             # (a) / (b) item bodies
SUBSUB = 40 * mm          # (i) / (ii) list bodies

_BASE = ParagraphStyle("base", fontName="Helvetica", fontSize=10.5, leading=13.5, spaceAfter=7)
_BODY = ParagraphStyle("body", parent=_BASE, alignment=TA_JUSTIFY)
_CENTER_B = ParagraphStyle("center_b", parent=_BASE, fontName="Helvetica-Bold", fontSize=11.5,
                           alignment=TA_CENTER, spaceAfter=10)
_SECTION = ParagraphStyle("section", parent=_BASE, fontName="Helvetica-Bold", leftIndent=INDENT,
                          bulletIndent=0, spaceBefore=8, spaceAfter=6)
_SEC_BODY = ParagraphStyle("sec_body", parent=_BODY, leftIndent=INDENT)
_ITEM = ParagraphStyle("item", parent=_BODY, leftIndent=SUB, bulletIndent=INDENT)
_ITEM_BODY = ParagraphStyle("item_body", parent=_BODY, leftIndent=SUB)
_ITEM_ITALIC = ParagraphStyle("item_italic", parent=_ITEM_BODY, fontName="Helvetica-Oblique")
_ITEM_LEFT = ParagraphStyle("item_left", parent=_ITEM_BODY, alignment=0)
_ROMAN = ParagraphStyle("roman", parent=_BASE, leftIndent=SUBSUB, bulletIndent=SUB, spaceAfter=2)
_SMALL = ParagraphStyle("small", parent=_BASE, fontSize=7, leading=8.5, spaceAfter=0)
_LETTER = ParagraphStyle("letter", parent=_BODY)
_LETTER_NUM = ParagraphStyle("letter_num", parent=_BODY, leftIndent=INDENT, bulletIndent=0)
_LETTER_SUB = ParagraphStyle("letter_sub", parent=_BODY, leftIndent=INDENT + 9 * mm,
                             bulletIndent=INDENT, spaceAfter=2)


def _p(text: str, style=_BODY, bullet=None) -> Paragraph:
    return Paragraph(text, style, bulletText=bullet)


def _e(value) -> str:
    return escape(str(value)) if value is not None else ""


def _b(value) -> str:
    return f"<b>{_e(value)}</b>"


def _amount(data, value, currency_key="policy_currency") -> str:
    return _b(money(value, data[currency_key]))


def _share(data) -> str:
    """REINSURED AMOUNT: 97.5% of 100% (…) / INSURED AMOUNT: 100% of 100% (ONE HUNDRED PERCENT)."""
    sh = data["share"]
    label = "REINSURED AMOUNT" if data["document"] == "reinsurance" else "INSURED AMOUNT"
    return (f"{label}: {percent_number(sh['percent'])}% of {percent_number(sh['of'])}% "
            f"({percent_words(sh['percent'])}) of Sums Insured")


def _signer(data) -> tuple[str, bool]:
    """(the name under AUTHORISED SIGNATORY and after 'held on file by', whether it is us). An
    insurance certificate signed by the Insurer is the Insurer's document; every other is ours."""
    if data["document"] == "insurance" and data["variant"] == "insurer":
        return join_names(data["insurer"]), False
    return data["issuer"].get("company_legal_name") or "", True


# ==============================================================================================
# the fixed wording
# ==============================================================================================

def _preamble(data) -> str:
    if data["document"] == "insurance":
        if data["variant"] == "insurer":
            return (f"THIS IS TO CERTIFY that insurance has been placed in the name of "
                    f"{join_names(data['insured'])} and/or any affiliated, associated, inter-related "
                    f"subsidiary or controlled company, as now hereinafter constituted (hereinafter "
                    f"called the “Insured”) with {join_names(data['insurer'])} (hereinafter called “the "
                    f"Insurer”) covering their aviation operations in connection with their fleet of "
                    f"aircraft including all new and acquired aircraft from the moment they become the "
                    f"insurance responsibility of the Insured, against the following risks and up to "
                    f"the limits stated")
        return ("THIS IS TO CERTIFY that insurance has been placed in the name of the Insured (as "
                "defined below) with the Insurer (as defined below) and that we, in our capacity as "
                "insurance broker to the Insured have placed insurance with the below insurer for "
                "account of the Insured, covering their aviation operations in connection with their "
                "fleet of aircraft including all new and acquired aircraft from the moment they become "
                "the insurance responsibility of the Insured, against the following risks and up to "
                "the limits stated")
    if data["variant"] == "retrocession":
        return ("THIS IS TO CERTIFY that insurance has been placed in the name of the Insured (as "
                "defined below) with the Reinsured (as defined below) who, in turn, places a "
                "retrocession with the Retrocedent (as defined below) and that we, in our capacity as "
                "reinsurance broker to the Retrocedent have placed reinsurance in the London and "
                "international insurance markets in the name of the Retrocedent for account of the "
                "Insured, covering their aviation operations in connection with their fleet of "
                "aircraft including all new and acquired aircraft from the moment they become the "
                "insurance responsibility of the Insured, against the following risks and up to the "
                "limits stated")
    return ("THIS IS TO CERTIFY that insurance has been placed in the name of the Insured (as defined "
            "below) with the Reinsured (as defined below) and that we, in our capacity as "
            "reinsurance broker to the Reinsured have placed reinsurance in the London and "
            "international insurance markets in the name of the Reinsured for account of the "
            "Insured, covering their aviation operations in connection with their fleet of aircraft "
            "including all new and acquired aircraft from the moment they become the insurance "
            "responsibility of the Insured, against the following risks and up to the limits stated")


_SUCCESSORS = ("AND, in addition each of their respective successors and assigns, shareholders, "
               "subsidiaries, affiliates, trustees, transferees, partners, members, managers, "
               "contractors, directors, officers, servants, agents and employees.")
_SEVERAL = ("SEVERAL LIABILITY NOTICE – The subscribing (re)insurers’ obligations under the policies "
            "to which they subscribe are several and not joint and are limited solely to the extent "
            "of their individual subscriptions. The subscribing (re)insurers are not responsible for "
            "the subscription of any co-subscribing (re)insurer who for any reason does not satisfy "
            "all or part of its obligations.")
_SEVERAL_SMALL = ("SEVERAL LIABILITY NOTICE – The subscribing Insurer’s obligations under policies to "
                  "which they subscribe are several and not joint and are limited solely to the extent "
                  "of their individual operations. The subscribing insurers are not responsible for "
                  "the subscription of any co-subscribing insurer who for any reason does not satisfy "
                  "all or part of its obligations. Subject to (Re)Insurers Liability Clause LMA 3333.")


# ==============================================================================================
# page furniture
# ==============================================================================================

def _svg(data: bytes):
    from svglib.svglib import svg2rlg
    return svg2rlg(io.BytesIO(data))


def _fit(width, height, max_w, max_h):
    scale = min(max_w / width, max_h / height)
    return width * scale, height * scale, scale


def _flowable(img, max_w, max_h):
    """An image as a flowable inside a box, aspect kept: SVG as vector, PNG/JPEG as a bitmap."""
    if img is None:
        return None
    if img.content_type == "image/svg+xml":
        drawing = _svg(img.data)
        w, h, scale = _fit(drawing.width, drawing.height, max_w, max_h)
        drawing.scale(scale, scale)
        drawing.width, drawing.height = w, h
        return drawing
    reader = ImageReader(io.BytesIO(img.data))
    iw, ih = reader.getSize()
    w, h, _ = _fit(iw, ih, max_w, max_h)
    return Image(io.BytesIO(img.data), width=w, height=h)


def _draw_image(canvas, img, right_x, top_y, max_w, max_h):
    """Draw on the canvas, right-aligned at `right_x`, top at `top_y`."""
    if img.content_type == "image/svg+xml":
        drawing = _svg(img.data)
        w, h, scale = _fit(drawing.width, drawing.height, max_w, max_h)
        drawing.scale(scale, scale)
        renderPDF.draw(drawing, canvas, right_x - w, top_y - h)
        return
    reader = ImageReader(io.BytesIO(img.data))
    iw, ih = reader.getSize()
    w, h, _ = _fit(iw, ih, max_w, max_h)
    canvas.drawImage(reader, right_x - w, top_y - h, w, h, mask="auto")


def _page_callbacks(issuer: dict, images: dict, draft: bool):
    primary = colors.HexColor(issuer.get("brand_primary") or "#1F3B33")
    accent = colors.HexColor(issuer.get("brand_accent") or "#D5E28D")

    def header(canvas):
        canvas.saveState()
        top = PAGE_H - 14 * mm
        if issuer.get("company_name"):
            canvas.setFillColor(primary)
            canvas.setFont("Times-Roman", 20)
            canvas.drawString(MARGIN_X, top - 8 * mm, issuer["company_name"])
        if images.get("logo"):
            _draw_image(canvas, images["logo"], PAGE_W - MARGIN_X, top, 60 * mm, 13 * mm)
        if draft:
            canvas.setFillColor(colors.Color(0.85, 0.2, 0.2, alpha=0.18))
            canvas.setFont("Helvetica-Bold", 110)
            canvas.translate(PAGE_W / 2, PAGE_H / 2)
            canvas.rotate(45)
            canvas.drawCentredString(0, -35, "DRAFT")
        canvas.restoreState()

    def first_page(canvas, doc):
        header(canvas)
        canvas.saveState()
        if issuer.get("address_line"):
            canvas.setFont("Times-Roman", 11)
            canvas.setFillColor(colors.black)
            canvas.drawCentredString(PAGE_W / 2, 22 * mm, issuer["address_line"])
        if issuer.get("legal_footer"):
            footer = Paragraph(_e(issuer["legal_footer"]), _SMALL)
            w, h = footer.wrap(PAGE_W - 2 * MARGIN_X, 20 * mm)
            footer.drawOn(canvas, MARGIN_X, 18 * mm - h)
        canvas.restoreState()

    def later_page(canvas, doc):
        header(canvas)
        canvas.saveState()
        canvas.setStrokeColor(accent)
        canvas.setLineWidth(3)
        canvas.line(MARGIN_X, 20 * mm, PAGE_W - MARGIN_X, 20 * mm)
        canvas.setFont("Times-Roman", 10)
        canvas.drawRightString(PAGE_W - MARGIN_X, 13 * mm, str(doc.page))
        canvas.restoreState()

    return first_page, later_page


def _signature_block(data: dict, images: dict, date_of_issue: str, lead=None):
    """Room for the signatory to sign by hand, the company stamp beside it when one is uploaded and
    the document is ours to stamp, then who signs."""
    signer, ours = _signer(data)
    stamp = _flowable(images.get("stamp"), 24 * mm, 24 * mm) if ours else None
    if stamp is not None:
        image_row = Table([[Spacer(48 * mm, 20 * mm), stamp]], hAlign="LEFT")
        image_row.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                                       ("LEFTPADDING", (0, 0), (-1, -1), 0)]))
    else:
        image_row = Spacer(1, 20 * mm)
    who = data.get("signatory") or {}
    lines = [f"<b>{_e(who.get('full_name') or '[Signatory]')}</b>"]
    if who.get("title"):
        lines.append(_e(who["title"]))
    if who.get("phone"):
        lines.append(f"<b>Tel: {_e(who['phone'])}</b>")
    if who.get("email"):
        lines.append(f"<font color='#1F5FBF'><b>{_e(who['email'])}</b></font>")
    right = [image_row, _p("Authorised Signatory", _BASE), _p("<br/>".join(lines), _BASE)]
    left = _p(f"Date of issue {_e(date_of_issue)}", _BASE)
    table = Table([[left, right]], colWidths=[(PAGE_W - 2 * MARGIN_X) * 0.5] * 2)
    table.setStyle(TableStyle([("VALIGN", (0, 0), (0, 0), "BOTTOM"),
                               ("VALIGN", (1, 0), (1, 0), "TOP"),
                               ("LEFTPADDING", (0, 0), (-1, -1), 0)]))
    return KeepTogether([
        *(lead or []),
        table, Spacer(1, 4 * mm),
        _p(f"AUTHORISED SIGNATORY<br/>{_e(signer.upper())}",
           ParagraphStyle("sig", parent=_BASE, alignment=TA_CENTER)),
    ])


def _label_table(rows, label_width, indent=0, bold_values=True):
    body = [[_p(_e(label), _BASE), _p(_b(value) if bold_values else _e(value), _BASE)]
            for label, value in rows]
    table = Table(body, colWidths=[label_width, PAGE_W - 2 * MARGIN_X - indent - label_width],
                  hAlign="LEFT")
    table.setStyle(TableStyle([("LEFTPADDING", (0, 0), (-1, -1), 0),
                               ("TOPPADDING", (0, 0), (-1, -1), 0),
                               ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
                               ("VALIGN", (0, 0), (-1, -1), "TOP")]))
    if indent:
        wrapper = Table([[Spacer(indent, 1), table]], colWidths=[indent, None], hAlign="LEFT")
        wrapper.setStyle(TableStyle([("LEFTPADDING", (0, 0), (-1, -1), 0),
                                     ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))
        return wrapper
    return table


# ==============================================================================================
# the document
# ==============================================================================================

def render(data: dict, *, reference_number: str, date_of_issue: date, images: dict,
           draft: bool = False) -> bytes:
    """`images`: {"logo"|"stamp": issuer.Image}. `draft` marks DRAFT across every page — every PDF
    of a certificate not yet issued."""
    issuer = data["issuer"]
    insurance = data["document"] == "insurance"
    signer, _ours = _signer(data)
    issued = long_date(date_of_issue)
    story = []
    add = story.append

    # ---------------------------------------------------------------- the certificate
    add(_p(f"Date: {issued}", _BASE))
    add(Spacer(1, 3 * mm))
    add(_p("CERTIFICATE OF INSURANCE" if insurance else "CERTIFICATE OF REINSURANCE", _CENTER_B))
    add(_p(f"Reference No. {_e(reference_number)}", _CENTER_B))
    add(_p("<b>TO WHOM IT MAY CONCERN</b>", _BASE))
    add(_p(_e(_preamble(data))))

    n = 0

    def section(title):
        nonlocal n
        n += 1
        add(_p(f"<u>{_e(title)}:</u>", _SECTION, bullet=f"{n}."))

    section("INSURED")
    add(_p(f"{_e(join_names(data['insured']))} and/or affiliated, associated, inter-related, "
           f"subsidiary or controlled company, as now or hereinafter acquired or constituted jointly "
           f"and severally for their respective rights and interests (the “<b>Insured</b>”).",
           _SEC_BODY))
    if insurance:
        section("INSURER")
        add(_p(f"{_e(join_names(data['insurer']))} (the “<b>Insurer</b>”).", _SEC_BODY))
    else:
        section("REINSURED")
        add(_p(f"{_e(join_names(data['reinsured']))} (the “<b>Reinsured</b>”).", _SEC_BODY))
    if not insurance and data["variant"] == "retrocession":
        section("RETROCEDENT")
        add(_p(_e(join_names(data["retrocedent"])), _SEC_BODY))

    section("POLICY PERIOD")
    period = data["period"]
    add(_p(f"From {long_date(date.fromisoformat(period['from']))} to "
           f"{long_date(date.fromisoformat(period['to']))} {_e(period['wording'])}.", _SEC_BODY))

    section("EQUIPMENT")
    eq = data["equipment"]
    add(_label_table([("Equipment:", eq["description"]),
                      ("Manufacturer’s serial number:", eq["msn"]),
                      ("Registration:", eq["registration"]),
                      ("Agreed Value:", money(eq["agreed_value"], eq["agreed_value_currency"]))],
                     label_width=58 * mm, indent=INDENT))
    add(_p("(hereinafter referred to as the “<b>Equipment</b>”)", _SEC_BODY))

    section("GEOGRAPHICAL LIMITS")
    add(_p(_e(data["geographical_limits"]), _SEC_BODY))

    section("COVERAGE")
    cov = n     # "as detailed in 7(a) and 7(b)" — 7 on the reinsurance certificate, 6 on the insurance one
    hull, war, liab = data["hull"], data["hull_war"], data["liability"]
    add(_p("<u>HULL (including spares) ALL RISKS</u> covering loss or damage whilst flying and / or "
           "on the ground for an agreed value each aircraft. This coverage is subject to the "
           "following deductibles:", _ITEM, bullet="(a)"))
    add(_p(f"In respect of hull – {_amount(data, hull['deductible'])} each and every loss other than "
           f"in respect of total loss, constructive total loss or arranged total loss.", _ITEM_BODY))
    add(_p(f"In respect of spares – {_amount(data, hull['spares_deductible'])} each and every claim. "
           f"In respect of engine test running the above-mentioned hull deductible will apply.",
           _ITEM_BODY))
    add(_p(_share(data), _ITEM_LEFT))

    clause = _b(war["clause"])
    country = ""
    if war.get("selected_country_limit") is not None:
        country = (f", but {_amount(data, war['selected_country_limit'])} in the annual aggregate "
                   f"for flights into {_e(war['selected_country'])}")
    add(_p(f"<u>HULL (including spares) WAR AND ALLIED RISKS</u> covering loss or damage in "
           f"accordance with {clause} for an agreed value as set out above. Cover includes "
           f"confiscation and other perils detailed in Section 1(e) of {clause} by the government of "
           f"registration, subject to an annual aggregate sub-limit of not less than "
           f"{_amount(data, war['confiscation_limit'])}{country}. Coverage under Section 1(a) of "
           f"{clause} in respect of spares is restricted to air and sea transits in accordance with "
           f"the applicable transit clause(s). Subject to an overall annual aggregate policy limit of "
           f"not less than {_amount(data, war['overall_limit'])}.", _ITEM, bullet="(b)"))
    add(_p(f"The coverage in respect of spares (as detailed in {cov}(a) and {cov}(b) above) is subject to a "
           f"limit of {_amount(data, war['spares_limit'])} any one occurrence.", _ITEM_BODY))
    add(_p(_share(data), _ITEM_LEFT))
    if data.get("cut_through_clause"):
        add(_p(f"The coverage detailed in {cov}(a) and {cov}(b) above includes a 50/50 clause in accordance "
               f"with {_b(data['fifty_fifty_clause'])} and the following <b>Cut Through Clause</b>:",
               _ITEM_BODY))
        for para in data["cut_through_clause"].splitlines():
            if para.strip():
                add(_p(_e(para.strip()), _ITEM_ITALIC))
    else:
        add(_p(f"The coverage detailed in {cov}(a) and {cov}(b) above includes a 50/50 clause in accordance "
               f"with {_b(data['fifty_fifty_clause'])}.", _ITEM_BODY))

    exception = (f" (except {_e(liab['war_exclusion_exception'])})"
                 if liab.get("war_exclusion_exception") else "")
    add(_p(f"<u>AVIATION LEGAL LIABILITY</u> covering the Insured’s aircraft third party, passenger, "
           f"baggage, cargo, mail and airline general third party (including hangarkeepers, premises "
           f"and products) legal liability for a combined single limit (bodily injury/property "
           f"damage) of not less than {_amount(data, liab['combined_single_limit'])} any one "
           f"occurrence (including war and allied perils as excluded by "
           f"{_b(liab['war_exclusion_clause'])}{exception} in accordance with "
           f"{_b(liab['war_liability_clause'])} for a combined single limit (bodily injury/property "
           f"damage) of not less than {_amount(data, liab['war_combined_single_limit'])} any one "
           f"occurrence), but in the annual aggregate in respect of products and war and allied "
           f"perils legal liability.", _ITEM, bullet="(c)"))
    add(_p("The above aggregate limit(s) may be reduced or exhausted by claims made under the "
           "policy(ies).", _ITEM_BODY))
    add(_p(_share(data), _ITEM_LEFT))

    if data.get("hull_deductible"):
        hd = data["hull_deductible"]
        add(_p(f"<u>HULL DEDUCTIBLE</u> paying the difference between the hull deductible as stated "
               f"above and {_amount(data, hd['buy_down'])} each and every loss.", _ITEM, bullet="(d)"))
        if hd.get("aggregate") is not None:
            add(_p(f"This coverage is subject to a policy annual aggregate limit of "
                   f"{_amount(data, hd['aggregate'])}", _ITEM_BODY))
        add(_p("The above aggregate limit(s) may be reduced or exhausted by claims made under the "
               "policy(ies).", _ITEM_BODY))
        add(_p(_share(data), _ITEM_LEFT))

    add(_p(f"Subject to the coverage, terms, conditions, limitations, exclusions and cancellation "
           f"provisions of the relative policy(ies) as held on file by {_e(signer)}.", _SEC_BODY))

    section("AVN 67B")
    add(_p("It is hereby certified that the following insurance provisions apply:", _SEC_BODY))
    add(_p("The attachment of the Equipment is hereby certified in accordance with the provisions of "
           "<b>AVN67B Airline Finance / Lease Contract Endorsement and AVN67B (Hull War) Airline "
           "Finance Lease Contract Endorsement (Hull War)</b> providing coverage to the following "
           "Contract Party(ies) in relation to the following Contract(s):", _SEC_BODY))
    add(_p("Contract Party(ies):", _ITEM, bullet="(a)"))
    for i, name in enumerate(data["contract_parties"], 1):
        add(_p(_e(name), _ROMAN, bullet=f"({roman(i)})"))
    add(Spacer(1, 3 * mm))
    add(_p(_SUCCESSORS, _ITEM_BODY))
    add(_p("Contract(s):", _ITEM, bullet="(b)"))
    for i, line in enumerate(data["contracts"], 1):
        add(_p(_e(line), _ROMAN, bullet=f"({roman(i)})"))
    add(Spacer(1, 3 * mm))
    add(_p("Effective Date:", _ITEM, bullet="(c)"))
    add(_p(long_date(date.fromisoformat(data["effective_date"])), _ITEM_BODY))

    add(Spacer(1, 4 * mm))
    add(_p(_e(_SEVERAL)))
    add(Spacer(1, 5 * mm))
    add(_signature_block(data, images, issued))
    add(Spacer(1, 3 * mm))
    add(_p(_e(_SEVERAL_SMALL), _SMALL))

    # ---------------------------------------------------------------- the letter of undertaking
    add(PageBreak())
    add(_p("<u>LETTER OF UNDERTAKING</u><br/>"
           f"<u>ATTACHING TO CERTIFICATE REFERENCE NUMBER</u> {_e(reference_number)}", _CENTER_B))
    add(_p("<b>To:&nbsp;&nbsp;&nbsp;&nbsp;As per Contract Party(ies) mentioned above.</b>", _LETTER))
    add(_p("Dear Sirs,", _LETTER))
    add(Spacer(1, 3 * mm))
    same = "As stated in the above referenced certificate"
    rows = [[_p(f"<b>{label}</b>", _BASE), _p(f"<b>{same}</b>", _BASE)]
            for label in ("Equipment:", "Manufacturer’s serial number:", "Registration marks:",
                          "Insured:")]
    t = Table(rows, colWidths=[62 * mm, None], hAlign="LEFT")
    t.setStyle(TableStyle([("LEFTPADDING", (0, 0), (-1, -1), 0),
                           ("TOPPADDING", (0, 0), (-1, -1), 0),
                           ("BOTTOMPADDING", (0, 0), (-1, -1), 0)]))
    add(t)
    add(Spacer(1, 5 * mm))
    if not insurance:
        role, cover, principal = "Reinsurance Brokers", "Reinsurances", "Reinsured"
        confirm = ("We confirm that as Reinsurance Brokers, we have effected reinsurances in the name "
                   "of the Reinsured and confirm that the Reinsurances are in effect on and in respect "
                   "of the Equipment as set out herein")
        appointment = "Reinsurance Broker to the Reinsured"
    elif data["variant"] == "insurer":
        role, cover, principal = "Insurer", "Insurances", "Insured"
        confirm = ("We confirm that as Insurer, we have effected insurances for the account of the "
                   "Insured, and confirm that the Insurances are in effect on and in respect of the "
                   "Equipment as set out herein.")
        appointment = "Insurer to the Insured"
    else:
        role, cover, principal = "Insurance Brokers", "Insurances", "Insured"
        confirm = ("We confirm that as Insurance Brokers, we have effected insurances in the name of "
                   "the Insured and confirm that the Insurances are in effect on and in respect of the "
                   "Equipment as set out herein")
        appointment = "Insurance Broker to the Insured"
    add(_p(confirm, _LETTER))
    add(_p(f"Pursuant to instructions from the {principal}, we hereby undertake the following in "
           f"relation to your interest(s) in the Equipment:", _LETTER))
    add(_p("In relation to the hull (including hull war risks) Insurances, to hold the benefit of "
           "those Insurances to your order in accordance with the loss payable provisions as "
           "contained with the Contracts, but subject always to the requirements to manage the "
           "policy(ies) as they relate to any other aircraft insured as part of the fleet.",
           _LETTER_NUM, bullet="1."))
    add(_p("To advise you as soon as reasonably practicable at the e-mail address included within the "
           "Schedule of Addressees:", _LETTER_NUM, bullet="2."))
    add(_p(f"of the receipt by us of any notice of cancellation or material change in the {cover};",
           _LETTER_SUB, bullet="2.1."))
    if insurance:
        add(_p("upon written request from you, of the premium payment status relating to the "
               "insurances, and;", _LETTER_SUB, bullet="2.2."))
        add(_p(f"if we cease to be {role} to the Insured during the policy period.", _LETTER_SUB,
               bullet="2.3."))
    else:
        add(_p("if we cease to be Reinsurance Brokers to the Reinsured;", _LETTER_SUB, bullet="2.2."))
    add(Spacer(1, 3 * mm))
    add(_p(f"Following a written application received from you not later than 21 days before expiry "
           f"of the {cover}, to notify you at the e-mail address contained within the Schedule of "
           f"Addressees attached to Certificate Addendum, within fourteen days of the receipt of such "
           f"application in the event of our not having received renewal instructions from the "
           f"{principal}.", _LETTER_NUM, bullet="3."))
    add(_p("The above undertakings are given:-", _LETTER))
    add(_p(f"subject to our lien, if any, in respect of the {cover} for which premiums are due.",
           _LETTER_NUM, bullet="(a)"))
    add(_p(f"subject to our continuing appointment for the time being as {appointment}.",
           _LETTER_NUM, bullet="(b)"))
    add(_p("This letter shall be governed by and construed in accordance with English Law and any "
           "disputes arising out of or in any way connected with this undertaking shall be submitted "
           "to the exclusive jurisdiction of the English courts.", _LETTER))
    add(Spacer(1, 8 * mm))
    add(_signature_block(data, images, issued, lead=[
        _p("Yours faithfully,", ParagraphStyle("yf", parent=_BASE, alignment=TA_CENTER)),
        Spacer(1, 4 * mm)]))

    # ---------------------------------------------------------------- the schedule of parties
    add(PageBreak())
    add(_p("SCHEDULE OF PARTIES TO WHOM NOTICE IS TO BE GIVEN", _CENTER_B))
    rows = [("Certificate:", reference_number), ("Insured:", same)]
    if not insurance:
        rows.append(("Reinsured:", same))
    rows.append(("Subject:", same))
    add(_label_table(rows, label_width=28 * mm))
    add(Spacer(1, 4 * mm))
    add(_p("<u><b>PLEASE READ CAREFULLY</b></u>", ParagraphStyle("prc", parent=_BASE,
                                                                  alignment=TA_CENTER)))
    add(_p("Under the attached Certificate, Underwriters have agreed to give notice in certain "
           "circumstances. <b>However, please be aware that notwithstanding anything contained in "
           "the attached certificate, notice will only be passed on to the parties detailed below "
           "utilising the contact details shown.</b>", _LETTER))
    add(_p("<b>As e-mail is the most efficient way for notice to be forwarded to you, please note "
           "that failure to advise us of your current details will severely inhibit our ability to "
           "pass on any notice received.</b>", _LETTER))
    add(_p("<b>PLEASE NOTE:</b> We would remind you that notices are effective from the time of "
           "issuance by Underwriters.", _LETTER))
    add(Spacer(1, 4 * mm))
    for a in data["addressees"]:
        add(KeepTogether([
            _label_table([("COMPANY:", a.get("company") or ""),
                          ("CONTACTS:", a.get("contacts") or ""),
                          ("EMAIL:", a.get("email") or "")],
                         label_width=28 * mm, bold_values=False),
            Spacer(1, 6 * mm)]))

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, leftMargin=MARGIN_X, rightMargin=MARGIN_X,
                            topMargin=TOP, bottomMargin=BOTTOM,
                            title=f"Certificate of {'Insurance' if insurance else 'Reinsurance'} "
                                  f"{reference_number}",
                            author=issuer.get("company_legal_name") or "")
    first_page, later_page = _page_callbacks(issuer, images, draft)
    doc.build(story, onFirstPage=first_page, onLaterPages=later_page)
    return buffer.getvalue()
