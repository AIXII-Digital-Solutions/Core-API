"""Issuing a certificate by electronic signature: what was approved, the token, and the signed file.

THE APPROVED VALUES. Approving a certificate records `approved_hash`: the sha256 of the canonical JSON
of its resolved values. Issuing re-resolves them and compares; a difference means something under the
certificate (policy, lease, aircraft, the company's wording) changed after the reviewer looked, and it
goes back to review. Two things are left out of the hash because they are not what the reviewer
approves: the signatory (chosen when the PDF is prepared — whoever signs) and the image hashes (a new
logo does not change what the certificate says).

THE SIGNED FILE must be the prepared PDF plus an incremental update that signs the `Signatory` field
and does nothing else:
  1. its first `length` bytes hash to the prepared sha256, and it is longer;
  2. the field `Signatory` carries a signature whose digest matches (intact) and whose cryptography
     checks out (valid) — the certificate chain is NOT required to be trusted;
  3. that signature covers the revision right after the prepared one — nothing was slipped in
     between and then signed over;
  4. pyHanko's diff analysis from the prepared revision to the end of the file finds nothing beyond
     filling in the form field (a later timestamp / LTV update is allowed).
"""
import hashlib
import io
import json
import secrets
from datetime import datetime, timedelta, timezone

from .render import SIGNATURE_FIELD

TOKEN_LIFETIME = timedelta(minutes=15)


class SignatureInvalid(ValueError):
    """The uploaded file is not the prepared PDF validly signed in `Signatory`; the message says why."""


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def approved_hash(data: dict) -> str:
    """sha256 of the canonical JSON of the values a reviewer approves (see the module docstring)."""
    values = {k: v for k, v in data.items() if k != "signatory"}
    if isinstance(values.get("issuer"), dict):
        values["issuer"] = {k: v for k, v in values["issuer"].items() if k != "images"}
    canonical = json.dumps(values, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return sha256(canonical.encode("utf-8"))


def new_token() -> tuple[str, str, datetime]:
    """(token handed to the portal, its sha256 kept in the row, when it expires)."""
    token = secrets.token_urlsafe(32)
    return token, sha256(token.encode()), datetime.now(timezone.utc) + TOKEN_LIFETIME


def token_matches(token: str, token_hash: str) -> bool:
    return secrets.compare_digest(sha256(token.encode()), token_hash)


async def verify_signed(signed: bytes, *, prepared_sha256: str, prepared_length: int) -> None:
    """Raise SignatureInvalid unless `signed` is the prepared PDF validly signed in `Signatory`."""
    from pyhanko.pdf_utils.reader import PdfFileReader
    from pyhanko.sign.diff_analysis import DEFAULT_DIFF_POLICY, ModificationLevel
    from pyhanko.sign.validation import async_validate_pdf_signature
    from pyhanko_certvalidator import ValidationContext

    if len(signed) <= prepared_length or sha256(signed[:prepared_length]) != prepared_sha256:
        raise SignatureInvalid("The file is not the prepared PDF with a signature added: it must start "
                               "byte for byte with the prepared PDF and be longer than it.")
    try:
        base = PdfFileReader(io.BytesIO(signed[:prepared_length])).xrefs.total_revisions - 1
        reader = PdfFileReader(io.BytesIO(signed))
        found = [s for s in reader.embedded_signatures if s.field_name == SIGNATURE_FIELD]
    except Exception as ex:
        raise SignatureInvalid(f"The file could not be read as a PDF: {ex}") from None
    if not found:
        raise SignatureInvalid(f"The field {SIGNATURE_FIELD} is not signed.")
    sig = found[0]

    status = await async_validate_pdf_signature(sig, ValidationContext(allow_fetching=False))
    if not (status.intact and status.valid):
        raise SignatureInvalid(f"The signature in {SIGNATURE_FIELD} is broken: the document was "
                               f"changed after signing, or the signature does not verify.")
    if sig.signed_revision != base + 1:
        raise SignatureInvalid("The signature does not sign the prepared PDF directly: other changes "
                               "were made to it before it was signed.")
    try:
        diff = DEFAULT_DIFF_POLICY.review_file(reader, base, field_mdp_spec=None, doc_mdp=None)
        level = getattr(diff, "modification_level", None)
    except Exception as ex:
        raise SignatureInvalid(f"The signed file changes more than the signature field: {ex}") from None
    if level is None or level > ModificationLevel.FORM_FILLING:
        raise SignatureInvalid("The signed file changes more than the signature field.")
