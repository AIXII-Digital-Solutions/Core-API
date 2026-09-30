"""The insurance and reinsurance certificates (AVN 67B): gather every value they print and check them.

The document itself is drawn by `render.py`; this module decides WHAT it says. The rules are the
client's certificate guidelines (.misc/insurance/Reinsurance Certificates Info.xlsx, "cert guidelines
- summary" and the "Insurance certificate" differences), field by field:

  A3-A6   Insured / Reinsured / Retrocedent / Period — the policy. A Retrocedent exists only when the
          placement is a retrocession; without one the certificate says nothing about retrocession.
  A7-A9   Equipment, MSN, registration — the aircraft.
  A10     Agreed value — the lease (final, else depreciated to the date of issue), unless an e-mail,
          rider or mark-up says otherwise (`overrides.agreed_value`).
  A11-A18 Deductibles, reinsured amount, hull war limits, cut-through clause — the policy.
  A19     Combined single limit — the LESSER of policy and lease; alert when the lease asks for more.
  A20     War liability (AVN 52E) — the LESSER of policy and lease; alert when it differs from the
          CSL or falls short of the lease.
  A21     Hull deductible buy-down — the GREATER of policy and lease.
  A22     Hull deductible aggregate — the policy.
  A23-A27 Contract parties, contracts — the lease agreement, unless overridden.
  A28     Effective date — the lease terms' effective date, unless overridden.
  A29     Notice addresses — each contract party's contacts.

The INSURANCE certificate differs from the reinsurance one in what it names and how much it
certifies: the Insurer is the policy's reinsured, there is no Reinsured and no Retrocedent, and the
insured amount is 100 % of 100 % (direct insurance). Its wording depends on who signs it — the
Insurer, or us as insurance broker — which is its `variant`. Its date should match the reinsurance
certificate's; an alert says when it does not.

Values that must be on the document and are missing are ERRORS: the certificate cannot be issued.
Disagreements a person should look at are ALERTS: it can be issued, and they are kept with it.
"""
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import joinedload, selectinload

from Database.CertificateModels import ISSUED, MARKET_WORDING, ReinsuranceCertificate
from Database.FleetModels import Aircraft, AircraftType
from Database.LeasingModels import Agreement, AgreementParty, AircraftLease
from Database.PolicyModels import Coverage, Policy, PolicyParty, PartyRole
from Database.RefModels import Party
from Utils.DomainCommon import (AIRCRAFT_BRIEF, agreed_value_at, lease_in_force, covers,
                                policy_parties)
from .numbering import contract_year

REINSURANCE, INSURANCE = "reinsurance", "insurance"
KINDS = (REINSURANCE, INSURANCE)
TEMPLATE_VERSION = {REINSURANCE: "reinsurance-1", INSURANCE: "insurance-1"}
# Who signs an insurance certificate: us, as the Insured's insurance broker, or the Insurer.
SIGNED_BY = ("broker", "insurer")


@dataclass
class Overrides:
    """What an e-mail, rider or certificate mark-up may set in place of the stored value."""
    agreed_value: Optional[Decimal] = None
    equipment: Optional[str] = None
    effective_date: Optional[date] = None
    contract_parties: Optional[list[str]] = None
    contracts: Optional[list[str]] = None
    addressees: Optional[list[dict]] = None


@dataclass
class Draft:
    data: dict
    alerts: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    aircraft_id: Optional[int] = None
    policy_id: Optional[int] = None
    aircraft_lease_id: Optional[int] = None
    airline_code: Optional[str] = None
    contract_year: Optional[int] = None
    variant: str = "standard"
    registration: Optional[str] = None
    msn: Optional[str] = None


class NotFound(LookupError):
    """The aircraft, or the policy named, does not exist / does not cover it."""


def _num(v) -> Optional[float]:
    return None if v is None else float(v)


def _pick(rows, on: date, window):
    """The row in force on `on`, else the nearest one starting after it."""
    current = [r for r in rows if window(r, on)]
    if current:
        return current[0]
    upcoming = sorted((r for r in rows if r_start(r) > on), key=r_start)
    return upcoming[0] if upcoming else None


def r_start(row) -> date:
    return row.covered_from if isinstance(row, Coverage) else row.effective_date


async def assemble(session, kind: str, *, aircraft_id: int, date_of_issue: date,
                   policy_id: Optional[int] = None, overrides: Optional[Overrides] = None,
                   signed_by: str = "broker", wording: Optional[dict] = None) -> Draft:
    """Everything a certificate of `kind` prints for this aircraft, as of `date_of_issue`.

    `wording` is the clauses, period wording and geographical limits, already resolved (the
    certificate's override, else the company's setting, else the market text — issuer.resolve_wording);
    the policy is not read for them."""
    if kind not in KINDS:
        raise ValueError(f"unknown certificate kind {kind!r}")
    o = overrides or Overrides()
    w = {**MARKET_WORDING, **(wording or {})}
    errors, alerts = [], []

    aircraft = (await session.execute(
        select(Aircraft).where(Aircraft.id == aircraft_id).options(*AIRCRAFT_BRIEF)
    )).unique().scalar_one_or_none()
    if aircraft is None:
        raise NotFound(f"Aircraft {aircraft_id} not found")

    # --- the policy: the one named, else the coverage in force on the date of issue ------------
    coverage_rows = (await session.execute(
        select(Coverage).where(Coverage.aircraft_id == aircraft_id)
        .options(joinedload(Coverage.policy).joinedload(Policy.parties)
                 .joinedload(PolicyParty.party))
        .order_by(Coverage.covered_from.desc())
    )).unique().scalars().all()
    if policy_id is not None:
        coverage_rows = [c for c in coverage_rows if c.policy_id == policy_id]
        if not coverage_rows:
            raise NotFound(f"Policy {policy_id} does not cover aircraft {aircraft.registration}")
    coverage = _pick(coverage_rows, date_of_issue, covers) or (coverage_rows[0] if policy_id else None)
    if coverage is None:
        raise NotFound(f"{aircraft.registration} is not insured on {date_of_issue.isoformat()} and "
                       f"no later coverage is recorded — a certificate is issued for insured "
                       f"aircraft only.")
    policy = coverage.policy

    # --- the lease terms: in force on the date of issue, else the next ones -------------------------
    lease_rows = (await session.execute(
        select(AircraftLease).where(AircraftLease.aircraft_id == aircraft_id)
        .options(joinedload(AircraftLease.agreement).joinedload(Agreement.lessor)
                 .selectinload(Party.contacts),
                 joinedload(AircraftLease.agreement).selectinload(Agreement.contract_parties)
                 .joinedload(AgreementParty.party).selectinload(Party.contacts))
        .order_by(AircraftLease.effective_date.desc(), AircraftLease.id.desc())
    )).unique().scalars().all()
    lease = lease_in_force(lease_rows, date_of_issue)
    if lease is None:
        upcoming = sorted((l for l in lease_rows if l.effective_date > date_of_issue),
                          key=lambda l: l.effective_date)
        lease = upcoming[0] if upcoming else None
    agreement = lease.agreement if lease else None
    if lease is None:
        errors.append({"field": "lease", "msg": f"{aircraft.registration} has no lease terms — the "
                                                 f"agreed value, lessor and contracts come from them."})

    # --- the parties --------------------------------------------------------------------------------
    insured = [x.party.name for x in policy_parties(policy, PartyRole.INSURED)]
    reinsured = [x.party.name for x in policy_parties(policy, PartyRole.REINSURED)]
    retrocedent = [x.party.name for x in policy_parties(policy, PartyRole.RETROCEDENT)]
    if not insured:
        errors.append({"field": "insured", "msg": "The policy names no insured."})
    if kind == REINSURANCE:
        variant = "retrocession" if retrocedent else "standard"
        if not reinsured:
            errors.append({"field": "reinsured", "msg": "A reinsurance certificate needs the policy's "
                                                         "reinsured; it names none."})
    else:
        variant = signed_by if signed_by in SIGNED_BY else "broker"
        if not reinsured:
            errors.append({"field": "insurer", "msg": "An insurance certificate names the policy's "
                                                       "reinsured as the Insurer; the policy names none."})

    # --- currencies -------------------------------------------------------------------------------
    pcur = policy.currency
    lcur = aircraft.service.lease_currency if aircraft.service else "USD"

    # --- equipment --------------------------------------------------------------------------------
    t: Optional[AircraftType] = aircraft.aircraft_type
    equipment = o.equipment or (" ".join(x for x in (t.manufacturer, t.master_series) if x) if t else None)
    if not equipment:
        errors.append({"field": "equipment", "msg": "The aircraft has no type."})
    if not aircraft.msn:
        errors.append({"field": "msn", "msg": "The aircraft has no MSN."})

    if o.agreed_value is not None:
        agreed_value, agreed_source = o.agreed_value, "override"
    elif lease is not None and lease.agreed_value_final is not None:
        agreed_value, agreed_source = lease.agreed_value_final, "lease_final"
    elif lease is not None:
        calculated = agreed_value_at(
            lease.agreed_value_preliminary, lease.depreciation_ratio, lease.depreciation_start_date,
            aircraft.service.agreed_value_fixed if aircraft.service else False, date_of_issue)
        agreed_value = None if calculated is None else Decimal(str(calculated))
        agreed_source = "lease_calculated"
    else:
        agreed_value, agreed_source = None, None
    if agreed_value is None:
        errors.append({"field": "agreed_value", "msg": "No agreed value: the lease states none and "
                                                        "none was given."})

    # --- policy amounts ---------------------------------------------------------------------------
    def need(value, name, what):
        if value is None:
            errors.append({"field": name, "msg": f"The policy has no {what}."})
        return _num(value)

    hull_deductible = need(policy.hull_all_risks_deductible, "hull_all_risks_deductible",
                           "hull all risks deductible")
    spares_deductible = need(policy.spares_deductible, "spares_deductible", "spares deductible")
    if kind == REINSURANCE:
        share = {"percent": need(policy.reinsured_amount, "reinsured_amount", "reinsured amount"),
                 "of": _num(policy.reinsured_amount_of) if policy.reinsured_amount_of is not None else 100.0}
    else:
        share = {"percent": 100.0, "of": 100.0}      # direct insurance: the whole risk
    confiscation = need(policy.hull_war_confiscation_limit, "hull_war_confiscation_limit",
                        "hull war confiscation limit")
    overall = need(policy.hull_war_overall_limit, "hull_war_overall_limit", "hull war overall limit")
    spares_limit = need(policy.hull_spares_limit, "hull_spares_limit", "hull spares limit")
    country_limit = _num(policy.hull_war_confiscation_limit_selected_country)
    if country_limit is not None and not policy.selected_country:
        alerts.append({"code": "country_limit_without_country",
                       "msg": "The policy has a confiscation limit for a selected country but no "
                              "country; it is left off the certificate."})
        country_limit = None

    # A19 — the lesser of policy and lease; the certificate never shows more than the policy.
    p_csl, l_csl = _num(policy.combined_single_limit), _num(lease.combined_single_limit) if lease else None
    if p_csl is None:
        errors.append({"field": "combined_single_limit", "msg": "The policy has no combined single limit."})
        csl = None
    else:
        csl = min(p_csl, l_csl) if l_csl is not None else p_csl
        if l_csl is not None and l_csl > p_csl:
            alerts.append({"code": "csl_above_policy",
                           "msg": f"The lease requires a combined single limit of {l_csl:,.0f} but the "
                                  f"policy provides {p_csl:,.0f}; the certificate shows the policy limit."})
    # A20 — war liability: the lesser of policy and lease.
    p_war = _num(policy.hull_spares_war_excess_liability)
    l_war = _num(lease.hull_spares_war_excess_liability) if lease else None
    war_csl = min(x for x in (p_war, l_war) if x is not None) if (p_war is not None or l_war is not None) else None
    if p_war is None:
        errors.append({"field": "hull_spares_war_excess_liability",
                       "msg": "The policy has no war liability (AVN 52E) limit."})
    else:
        if l_war is not None and p_war < l_war:
            alerts.append({"code": "war_liability_below_lease",
                           "msg": f"The war liability limit {p_war:,.0f} is below the lease requirement "
                                  f"{l_war:,.0f}."})
        if csl is not None and war_csl != csl:
            alerts.append({"code": "war_liability_not_csl",
                           "msg": f"The war liability limit {war_csl:,.0f} differs from the combined "
                                  f"single limit {csl:,.0f}."})
    # A21 — the greater of policy and lease. A22 — the policy.
    buy_downs = [x for x in (_num(policy.hull_deductible_buy_down),
                             _num(lease.hull_deductible_buy_down) if lease else None) if x is not None]
    buy_down = max(buy_downs) if buy_downs else None
    aggregate = _num(policy.hull_deductible_aggregate)
    if buy_down is not None and aggregate is None:
        alerts.append({"code": "no_deductible_aggregate",
                       "msg": "A hull deductible buy-down is shown without an annual aggregate: the "
                              "policy states none."})

    # --- AVN 67B: contract parties, contracts, effective date, addresses ------------------------------
    parties_rows = []
    if agreement is not None:
        parties_rows = [x.party for x in agreement.contract_parties] or \
            ([agreement.lessor] if agreement.lessor else [])
    contract_parties = o.contract_parties if o.contract_parties is not None else [p.name for p in parties_rows]
    if not contract_parties:
        errors.append({"field": "contract_parties", "msg": "No contract party: the agreement names no "
                                                            "lessor and none was given."})

    if o.contracts is not None:
        contracts = o.contracts
    elif agreement is not None:
        if not agreement.start_date:
            errors.append({"field": "agreement_start_date", "msg": f"The lease agreement "
                                                                    f"'{agreement.name}' has no date."})
        lessor = agreement.lessor.name if agreement.lessor else None
        first = agreement.name
        if agreement.start_date:
            first += f" dated {agreement.start_date.strftime('%d %B %Y')}"
        if lessor and insured:
            first += f" between {lessor} and {insured[0]}"
        contracts = [first] + [line.strip() for line in (agreement.other_contracts or "").splitlines()
                               if line.strip()]
    else:
        contracts = []

    effective = o.effective_date or (lease.effective_date if lease else None)
    if effective is None:
        errors.append({"field": "effective_date", "msg": "No effective date."})

    if o.addressees is not None:
        addressees = o.addressees
    else:
        addressees = []
        for party in parties_rows if o.contract_parties is None else []:
            blocks = list(party.contacts)
            if not blocks:
                alerts.append({"code": "no_notice_address",
                               "msg": f"{party.name} has no contact block: the schedule of parties "
                                      f"lists it without an e-mail."})
                addressees.append({"company": party.name, "contacts": None, "email": None})
            for c in blocks:
                addressees.append({"company": c.company or party.name, "contacts": c.contact,
                                   "email": c.email})

    # --- the reference ------------------------------------------------------------------------------
    airline = aircraft.airline
    code = airline.certificate_code if airline else None
    if airline is None:
        errors.append({"field": "airline", "msg": f"{aircraft.registration} has no airline, so the "
                                                   f"reference number has no airline code."})
    elif not code:
        errors.append({"field": "certificate_code",
                       "msg": f"Airline '{airline.airline_name}' has no certificate code — set it at "
                              f"PATCH /ref/airlines/{airline.id}."})
    if policy.period_to is None:
        errors.append({"field": "period_to", "msg": "The policy has no end date."})

    data = {
        "document": kind,
        "variant": variant,
        "insured": insured,
        "reinsured": reinsured,
        "retrocedent": retrocedent if kind == REINSURANCE else [],
        "insurer": reinsured if kind == INSURANCE else [],
        "period": {"from": policy.period_from.isoformat(),
                   "to": policy.period_to.isoformat() if policy.period_to else None,
                   "wording": w["period_wording"]},
        "equipment": {"description": equipment, "msn": aircraft.msn,
                      "registration": aircraft.registration,
                      "agreed_value": _num(agreed_value), "agreed_value_currency": lcur,
                      "agreed_value_source": agreed_source},
        "geographical_limits": w["geographical_limits"],
        "policy_currency": pcur,
        "hull": {"deductible": hull_deductible, "spares_deductible": spares_deductible},
        "share": share,
        "hull_war": {"clause": w["hull_war_clause"], "confiscation_limit": confiscation,
                     "selected_country": policy.selected_country if country_limit is not None else None,
                     "selected_country_limit": country_limit, "overall_limit": overall,
                     "spares_limit": spares_limit},
        "fifty_fifty_clause": w["fifty_fifty_clause"],
        "cut_through_clause": policy.cut_through_clause,
        "liability": {"combined_single_limit": csl,
                      "war_exclusion_clause": w["war_exclusion_clause"],
                      "war_exclusion_exception": w["war_exclusion_exception"] or None,
                      "war_liability_clause": w["war_liability_clause"],
                      "war_combined_single_limit": war_csl},
        "hull_deductible": ({"buy_down": buy_down, "aggregate": aggregate}
                            if buy_down is not None else None),
        "contract_parties": contract_parties,
        "contracts": contracts,
        "effective_date": effective.isoformat() if effective else None,
        "addressees": addressees,
        "sources": {"aircraft_id": aircraft.id, "policy_id": policy.id,
                    "coverage_id": coverage.id,
                    "aircraft_lease_id": lease.id if lease else None,
                    "agreement_id": agreement.id if agreement else None},
    }
    # "The date shall ideally coincide with the reinsurance certificate"
    if kind == INSURANCE:
        ri_date = (await session.execute(
            select(ReinsuranceCertificate.date_of_issue)
            .where(ReinsuranceCertificate.aircraft_id == aircraft.id,
                   ReinsuranceCertificate.policy_id == policy.id,
                   ReinsuranceCertificate.status == ISSUED)
            .order_by(ReinsuranceCertificate.issued_at.desc()).limit(1))).scalar_one_or_none()
        if ri_date is not None and ri_date != date_of_issue:
            alerts.append({"code": "date_differs_from_reinsurance",
                           "msg": f"The reinsurance certificate for this aircraft and policy is dated "
                                  f"{ri_date.isoformat()}; this one {date_of_issue.isoformat()}."})

    return Draft(data=data, alerts=alerts, errors=errors, aircraft_id=aircraft.id,
                 policy_id=policy.id, aircraft_lease_id=lease.id if lease else None,
                 airline_code=code, contract_year=contract_year(policy.period_from),
                 variant=variant, registration=aircraft.registration, msn=aircraft.msn)

