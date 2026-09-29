"""The company that issues the certificates (certificate.settings) and its images (certificate.asset:
logo, stamp) — data, edited from the portal's admin.

The signatory is not here: the portal sends the issuing user's name, title, phone and e-mail with the
request, and they sign the printed certificate by hand. An issued certificate keeps what it printed in
its own `data` (and the images in its PDF), so a later change here never alters it.
"""
import hashlib
import io
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import undefer

from Database.CertificateModels import Asset, Settings

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


async def load(session) -> tuple[dict, dict]:
    """(issuer, images): the company as a certificate prints it, and the logo / stamp bytes the
    renderer draws. `issuer` names the images by sha256 so a certificate records which it used."""
    settings = await load_settings(session)
    rows = (await session.execute(
        select(Asset).where(Asset.key.in_(COMPANY_ASSETS)).options(undefer(Asset.data))
    )).scalars().all()
    images = {r.key: Image(r.content_type, bytes(r.data), r.sha256) for r in rows}
    return {**settings, "images": {k: v.sha256 for k, v in images.items()}}, images
