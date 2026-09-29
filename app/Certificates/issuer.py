"""Who issues a certificate: the company (certificate.settings), its images (certificate.asset) and
the signatory — the portal user issuing it, with the details they keep in certificate.signatory.

Everything here is DATA, edited from the portal. A certificate copies what it printed into its own
`data["issuer"]` at the moment of issue (and the images into its PDF), so a later change of logo,
address or signatory never alters a certificate already issued.
"""
import hashlib
import io
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import undefer

from Database.CertificateModels import Asset, Settings, Signatory

# Used only when the settings row is missing (a fresh database before the migration seeded it).
DEFAULTS = {
    "company_name": None,
    "company_legal_name": "[Company Legal Name]",
    "address_line": None,
    "legal_footer": None,
    "brand_primary": "#1F3B33",
    "brand_accent": "#D5E28D",
}
SETTINGS_FIELDS = tuple(DEFAULTS)

MAX_IMAGE_BYTES = 2 * 1024 * 1024
IMAGE_TYPES = ("image/png", "image/jpeg", "image/svg+xml")
COMPANY_ASSETS = ("logo", "stamp")


def signature_key(portal_user_id: str) -> str:
    return f"signature:{portal_user_id}"


@dataclass
class Image:
    content_type: str
    data: bytes
    sha256: str


class InvalidImage(ValueError):
    pass


def validate_image(content_type: str, data: bytes) -> str:
    """The content type to store, or InvalidImage. The file is actually parsed — a broken or
    mislabelled image must fail here, not later while a certificate is being drawn."""
    content_type = (content_type or "").split(";")[0].strip().lower()
    if content_type == "image/jpg":
        content_type = "image/jpeg"
    if content_type not in IMAGE_TYPES:
        raise InvalidImage(f"Unsupported image type '{content_type}'. Use PNG, JPEG or SVG.")
    if not data:
        raise InvalidImage("The file is empty.")
    if len(data) > MAX_IMAGE_BYTES:
        raise InvalidImage(f"The file is larger than {MAX_IMAGE_BYTES // (1024 * 1024)} MB.")
    if content_type == "image/svg+xml":
        head = data[:4096].lower()
        # no DTDs: an entity declaration is how an XML file reads files it should not (XXE)
        if b"<!doctype" in head or b"<!entity" in head or b"<!entity" in data.lower():
            raise InvalidImage("SVG with a DOCTYPE or ENTITY declaration is not accepted.")
        from svglib.svglib import svg2rlg
        if svg2rlg(io.BytesIO(data)) is None:
            raise InvalidImage("The SVG could not be read.")
    else:
        from PIL import Image as PILImage
        try:
            with PILImage.open(io.BytesIO(data)) as im:
                im.verify()
                fmt = im.format
        except Exception as ex:
            raise InvalidImage(f"The image could not be read: {ex}") from None
        if {"PNG": "image/png", "JPEG": "image/jpeg"}.get(fmt) != content_type:
            raise InvalidImage(f"The file is {fmt}, not {content_type}.")
    return content_type


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


async def load_settings(session) -> dict:
    row = (await session.execute(select(Settings).where(Settings.id == 1))).scalar_one_or_none()
    return {k: getattr(row, k) for k in SETTINGS_FIELDS} if row else dict(DEFAULTS)


async def load(session, portal_user) -> tuple[dict, dict]:
    """(issuer, images) for a certificate issued by `portal_user` (an api_auth.PortalUser, or None
    for a preview without one). `issuer` is what the certificate keeps in its data; `images` the
    bytes the renderer draws. Two statements: the settings with the signatory, then the images."""
    settings = await load_settings(session)
    profile = None
    keys = list(COMPANY_ASSETS)
    if portal_user is not None:
        profile = (await session.execute(
            select(Signatory).where(Signatory.portal_user_id == portal_user.id))).scalar_one_or_none()
        keys.append(signature_key(portal_user.id))
    rows = (await session.execute(
        select(Asset).where(Asset.key.in_(keys)).options(undefer(Asset.data)))).scalars().all()
    images = {("signature" if r.key.startswith("signature:") else r.key):
              Image(r.content_type, bytes(r.data), r.sha256) for r in rows}

    signatory = None
    if portal_user is not None:
        signatory = {
            "portal_user_id": portal_user.id,
            "name": (profile.name if profile and profile.name else None) or portal_user.name,
            "email": (profile.email if profile and profile.email else None) or portal_user.email,
            "title": profile.title if profile else None,
            "phone": profile.phone if profile else None,
        }
    issuer = {**settings, "signatory": signatory,
              "images": {k: v.sha256 for k, v in images.items()}}
    return issuer, images
