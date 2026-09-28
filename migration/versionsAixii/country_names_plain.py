"""ref.country names: official, but plain English — no parentheses, no inverted commas, no accents

ref_country loaded the ISO 3166-1 English short names verbatim, and several of them read badly on
a contract or in a picker: 'United Kingdom of Great Britain and Northern Ireland', 'Iran (Islamic
Republic of)', 'Tanzania, United Republic of', 'Türkiye'. The house style is the OFFICIAL form in
natural word order ('Russian Federation', 'Islamic Republic of Iran', 'Republic of Korea'), the
longest ones shortened ('United Kingdom', 'Netherlands'), and strictly English letters ('Turkey',
'Cote d'Ivoire'). `common_name` keeps the everyday form for search where it still differs.

Only the rows below change; the codes, and so every reference by id, stay as they are.

Revision ID: country_names_plain
Revises: ref_country
Create Date: 2026-09-28
"""
from alembic import op
from sqlalchemy import text

revision = "country_names_plain"
down_revision = "ref_country"
branch_labels = None
depends_on = None

# iso2: (new name, new common name, the ISO name and common name it replaces)
RENAMES = {
    "AX": ("Aland Islands", None, "Åland Islands", "Aland Islands"),
    "BO": ("Plurinational State of Bolivia", "Bolivia", "Bolivia (Plurinational State of)", "Bolivia"),
    "CC": ("Cocos Islands", "Keeling Islands", "Cocos (Keeling) Islands", "Cocos Islands"),
    "CG": ("Republic of the Congo", "Congo", "Congo", "Republic of the Congo"),
    "CD": ("Democratic Republic of the Congo", "DR Congo", "Congo (Democratic Republic of the)", "DR Congo"),
    "CI": ("Cote d'Ivoire", "Ivory Coast", "Côte d'Ivoire", "Ivory Coast"),
    "CW": ("Curacao", None, "Curaçao", "Curacao"),
    "FK": ("Falkland Islands", "Malvinas", "Falkland Islands (Malvinas)", "Falkland Islands"),
    "IR": ("Islamic Republic of Iran", "Iran", "Iran (Islamic Republic of)", "Iran"),
    "KP": ("Democratic People's Republic of Korea", "North Korea",
           "Korea (Democratic People's Republic of)", "North Korea"),
    "KR": ("Republic of Korea", "South Korea", "Korea (Republic of)", "South Korea"),
    "FM": ("Federated States of Micronesia", "Micronesia", "Micronesia (Federated States of)", "Micronesia"),
    "MD": ("Republic of Moldova", "Moldova", "Moldova (Republic of)", "Moldova"),
    "NL": ("Netherlands", "Holland", "Netherlands (Kingdom of the)", "Netherlands"),
    "PS": ("State of Palestine", "Palestine", "Palestine, State of", "Palestine"),
    "RE": ("Reunion", None, "Réunion", "Reunion"),
    "BL": ("Saint Barthelemy", None, "Saint Barthélemy", "Saint Barthelemy"),
    "MF": ("Saint Martin", "Saint-Martin", "Saint Martin (French part)", "Saint Martin"),
    "SX": ("Sint Maarten", None, "Sint Maarten (Dutch part)", "Sint Maarten"),
    "TW": ("Taiwan", None, "Taiwan, Province of China", "Taiwan"),
    "TZ": ("United Republic of Tanzania", "Tanzania", "Tanzania, United Republic of", "Tanzania"),
    "TR": ("Turkey", "Turkiye", "Türkiye", "Turkey"),
    "GB": ("United Kingdom", "Great Britain", "United Kingdom of Great Britain and Northern Ireland",
           "United Kingdom"),
    "VE": ("Bolivarian Republic of Venezuela", "Venezuela", "Venezuela (Bolivarian Republic of)", "Venezuela"),
    "VG": ("British Virgin Islands", None, "Virgin Islands (British)", "British Virgin Islands"),
    "VI": ("United States Virgin Islands", "US Virgin Islands", "Virgin Islands (U.S.)", "US Virgin Islands"),
}


def _apply(pairs) -> None:
    bind = op.get_bind()
    bind.execute(
        text("UPDATE ref.country SET name = :name, common_name = :common_name, updated_at = now() "
             "WHERE iso2 = :iso2"),
        [{"iso2": iso2, "name": name, "common_name": common} for iso2, name, common in pairs])


def upgrade() -> None:
    _apply((iso2, new, common) for iso2, (new, common, _, _) in RENAMES.items())
    op.execute("COMMENT ON TABLE ref.country IS 'ISO 3166-1 countries and territories (+ Kosovo, "
               "XK). name = the official form in plain English; common_name = the everyday form "
               "for search.'")
    bad = op.get_bind().execute(text(
        "SELECT name FROM ref.country WHERE name ~ '[()]' OR name ~ '[^\\x20-\\x7E]' "
        "OR name LIKE '%, %of%'")).scalars().all()
    assert not bad, f"names still off-style: {bad}"


def downgrade() -> None:
    _apply((iso2, old, old_common) for iso2, (_, _, old, old_common) in RENAMES.items())
    op.execute("COMMENT ON TABLE ref.country IS 'ISO 3166-1 countries and territories (+ Kosovo, "
               "XK). name = the ISO English short name, i.e. the full form; common_name = the "
               "everyday form for search.'")
