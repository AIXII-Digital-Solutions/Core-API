"""Who issues the certificates: the company, the signatory, the logo, signature and stamp.

PLACEHOLDERS until the real details are supplied. Every value marked TO_BE_SUPPLIED prints on the
document as written, so a certificate issued before they are replaced is visibly a draft. The image
files are optional: a missing logo prints the company name as a wordmark, a missing signature or
stamp leaves its space empty.

Assets live in `assets/` beside this file:
    logo.png        the header logo, top right of every page (transparent background, ~4:1)
    signature.png   the authorised signatory's signature
    stamp.png       the company stamp
"""
from pathlib import Path

ASSETS = Path(__file__).resolve().parent / "assets"

TO_BE_SUPPLIED = "[TO BE SUPPLIED]"

# The company, as it is named on the documents.
COMPANY_NAME = "[Company Name]"                 # e.g. the trading name shown in the header wordmark
COMPANY_LEGAL_NAME = "[Company Legal Name]"     # "as held on file by …", "AUTHORISED SIGNATORY …"
COMPANY_ADDRESS_LINE = "[Address] · [Phone] · [Website]"
# The regulatory small print at the foot of page 1.
COMPANY_LEGAL_FOOTER = ("[Registered name, regulator and registration details of the issuing "
                        "company.]")

# The authorised signatory printed under the signature.
SIGNATORY_NAME = "[Signatory Name]"
SIGNATORY_PHONE = "[Phone]"
SIGNATORY_EMAIL = "[email@example.com]"

# Colours of the header wordmark and the rule above the page number (hex).
BRAND_PRIMARY = "#1F3B33"
BRAND_ACCENT = "#D5E28D"


def asset(name: str):
    """Path to an asset if it exists, else None."""
    path = ASSETS / name
    return path if path.is_file() else None
