"""Curated archetype reference labels for calibrating / validating the score.

Brief sec. 7.4 / 7.6: known-archetype carriers anchor the meaning of the score
axis (does PC1 order carriers schedule->on-demand?) and the dimensionality litmus
(do fractional/charter operators dissociate on PC2?).

Keys are ``carrier_key`` exactly as the SQL builds it:
``COALESCE(NULLIF("Operator ICAO",''), upper(btrim("Operator")))`` — so an ICAO
code where Cirium has one, else the upper-cased operator name (e.g. ``VISTAJET``,
whose Operator ICAO is NULL in this dataset).

Only carriers actually present in the panel get used; the rest are ignored. These
are *reference anchors*, not the universe — most cells stay unlabeled (grey).
"""
from __future__ import annotations

# category -> expected position on the regularity axis (higher = more schedule-regular).
# Used only to sanity-check PC1 ordering, never to fit.
CATEGORY_REGULARITY = {
    "fsc": 1.0,            # full-service / flag scheduled pax
    "lcc": 0.95,          # low-cost scheduled pax
    "regional": 0.9,      # regional scheduled feeder
    "cargo_regular": 0.85,  # scheduled cargo (network, fixed lanes)
    "charter_acmi": 0.25,   # ad-hoc ACMI / charter broker
    "business_fractional": 0.15,  # fractional / business-jet on-demand
}

# Distinct, colour-blind-ish palette for the diagnostic scatter.
CATEGORY_COLOR = {
    "fsc": "#1f77b4",
    "lcc": "#2ca02c",
    "regional": "#17becf",
    "cargo_regular": "#9467bd",
    "charter_acmi": "#ff7f0e",
    "business_fractional": "#d62728",
}

# carrier_key -> category. Curated from the panel profile + aviation domain knowledge.
REFERENCE_LABELS: dict[str, str] = {}


def _add(category: str, *keys: str) -> None:
    for k in keys:
        REFERENCE_LABELS[k.upper()] = category


# --- Low-cost scheduled pax (expect S1/S2/S3/S4 all high) ---
_add("lcc",
     "IGO",  # IndiGo
     "PGT",  # Pegasus
     "VJC",  # Vietjet
     "FDB",  # flydubai
     "ABY",  # Air Arabia
     "AKJ",  # Akasa Air
     "KNE",  # flynas
     "WZZ", "RYR", "EZY", "EJU", "VLG", "TRA", "NSZ", "JBU", "SWA",
     "FFT", "NKS", "CEB", "JST", "TGW", "AXM", "SJX", "GOW", "SEJ")

# --- Full-service / flag scheduled pax (the prototypical "schedule" corner) ---
_add("fsc",
     "UAE",  # Emirates
     "BAW",  # British Airways
     "THY",  # Turkish
     "SVA",  # Saudia
     "ETD",  # Etihad
     "TAP",  # TAP Air Portugal
     "GFA",  # Gulf Air
     "ELY",  # El Al
     "RAM",  # Royal Air Maroc
     "AVA",  # Avianca
     "CMP",  # Copa
     "UZB",  # Uzbekistan Airways
     "AHY",  # Azerbaijan Airlines
     "PIA",  # Pakistan Intl
     "QTR", "DLH", "AFR", "KLM", "IBE", "SWR", "AUA", "SAS", "FIN", "LOT",
     "MSR", "ETH", "KQA", "SAA", "ACA", "AAL", "DAL", "UAL", "ANA", "JAL",
     "SIA", "CPA", "QFA", "ANZ", "AIC", "GIA", "MAS", "CES", "CCA", "CSN",
     # LATAM group (several legal carriers, all scheduled FSC)
     "LAN", "TAM", "LPE", "ARE", "LXP", "LNE", "LATAM AIRLINES")

# --- Regional scheduled feeder (regular, smaller metal) ---
_add("regional",
     "LNK",  # Airlink
     "PGA",  # Portugalia
     "ASH", "SKW", "RPA", "EDV", "JIA", "ENY", "GJS", "QXE")

# --- Scheduled cargo (network cargo, fixed lanes; regular but freight) ---
_add("cargo_regular",
     "FDX",  # FedEx
     "UPS",  # UPS
     "CLX",  # Cargolux
     "GEC",  # Lufthansa Cargo
     "BOX",  # AeroLogic
     "CKS",  # Kalitta
     "BCS",  # European Air Transport / DHL
     "DHK", "DAE", "QAC", "CAO", "GTI", "ABW", "MPH", "TAY")

# --- Fractional / business-jet on-demand (the PC2 litmus: high S3, low S1/S4) ---
_add("business_fractional",
     "VISTAJET",  # VistaJet (Operator ICAO NULL in this dataset -> name key)
     "VJT", "EJA", "NJE", "LXJ", "XOJ", "NETJETS", "FLEXJET")

# --- Ad-hoc ACMI / charter brokers (short presence inflates singletons; on-demand) ---
_add("charter_acmi",
     "MLH",  # Avion Express Malta
     "AXL", "VEX",  # Avion Express
     "ART", "SLU",  # SmartLynx
     "HFY",  # Hi Fly
     "GJT",  # GetJet
     "PLM",  # Wamos
     "BBG", "CES2", "ENT", "SDR", "AVION EXPRESS MALTA")


def label_for(carrier_key: str | None) -> str | None:
    if not carrier_key:
        return None
    return REFERENCE_LABELS.get(str(carrier_key).strip().upper())
