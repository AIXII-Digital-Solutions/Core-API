"""The `data` payload of every JSON success, for the OpenAPI document — DOCUMENTATION ONLY.

Handlers build their payload as plain dicts (Utils/DomainCommon's `*_json`, each router's `_serialize`)
and return it through `success_response`, so FastAPI cannot see what they send. These models describe
exactly those dicts, and `RESPONSE_DATA` says which operation returns which. Utils/OpenAPIDocs wraps
each in the envelope. Nothing validates or serialises a response with them — they are read only when the
document is generated.

KEEP THEM IN STEP WITH THE SERIALISERS. How they were checked, and how to re-check after changing a
serializer: call the GETs (read-only) and validate each real `data` against its documented schema with
every object closed (`additionalProperties: false`), so a key the model does not name fails too; run the
serializers of the write endpoints on transient ORM objects and validate those the same way. A key that
is ALWAYS present is a required field (nullable when it can be null); a key present only SOMETIMES has a
default and is therefore optional.

`response_examples.json` beside this file holds one example per component, taken from real responses
and serializer output (lists trimmed to one item). Regenerate it when a shape changes — an example that
no longer matches its schema is worse than none.
"""
from typing import Annotated, Any, Generic, List, Literal, Optional, TypeVar, Union

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")

# Sent as ISO strings; the format tells the reader (and Swagger's example) which kind.
Date = Annotated[str, Field(json_schema_extra={"format": "date"})]
DateTime = Annotated[str, Field(json_schema_extra={"format": "date-time"})]


class Page(BaseModel, Generic[T]):
    """A page of a grid: the rows and the size of the whole filtered set."""
    items: List[T]
    total: int


# ── status / jobs ─────────────────────────────────────────────────────────────────────────────

class JobStatusOut(BaseModel):
    job_id: str
    kind: str = Field(description="file | external")
    ref: Optional[str]
    state: str = Field(description="queued | running | success | error | skipped | cancelled")
    progress: Optional[int]
    message: Optional[str]
    payload: Optional[dict[str, Any]] = Field(description="Job-specific: step, eta, summary …")
    created_at: Optional[DateTime]
    updated_at: Optional[DateTime]
    finished_at: Optional[DateTime]


class JobCancelled(BaseModel):
    job_id: str
    aborted: Optional[bool] = Field(None, description="Present when the job was still running.")
    state: Optional[str] = Field(None, description="Present when it had already finished: its final state.")


class FileQueued(BaseModel):
    job_id: str
    kind: str
    filename: str
    group: str


# ── scheduler / queues / tokens ───────────────────────────────────────────────────────────────

class ScheduleOut(BaseModel):
    name: str
    queue: str
    func_name: str
    kwargs: Optional[dict[str, Any]]
    interval_seconds: Optional[int]
    cron_expr: Optional[str]
    enabled: bool
    paused: bool
    run_now: bool
    next_run_at: Optional[DateTime]
    last_run_at: Optional[DateTime]
    last_status: Optional[str]
    description: Optional[str]


class ScheduleRun(BaseModel):
    name: str
    func_name: str
    queue: str
    job_id: str


class NameOnly(BaseModel):
    name: str


class QueueOut(BaseModel):
    queue: str = Field(description="The alias used in the URL.")
    name: str = Field(description="The ARQ queue name.")
    queued: int
    paused: bool


class QueueToggled(BaseModel):
    queue: str
    name: str
    paused: bool


class QueuePurged(BaseModel):
    queue: str
    name: str
    purged: int


class TokenOut(BaseModel):
    prefix: str
    name: str
    scopes: List[str]
    enabled: bool
    expires_at: Optional[DateTime]
    last_used_at: Optional[DateTime]
    created_by: Optional[str]
    created_at: Optional[DateTime]


class TokenCreated(TokenOut):
    api_key: str = Field(description="`<prefix>.<secret>` — shown ONCE, never retrievable again.")


class PrefixOnly(BaseModel):
    prefix: str


# ── reference search (Cirium-backed) ──────────────────────────────────────────────────────────

class AirlineSearchHit(BaseModel):
    airline: Optional[str]
    icao: Optional[str]
    iata: Optional[str]


class RegistrationSearchHit(BaseModel):
    registration: Optional[str]
    operator: Optional[str]
    status: Optional[str]


class CapacityState(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    status: str = Field(description="live | paused | transitioning | failed")
    embeddable: bool
    raw_state: Optional[str] = Field(alias="_raw_state", description="ARM's own value — for support, not UI copy.")
    provisioning_state: Optional[str] = Field(alias="_provisioning_state")


# ── forecast ──────────────────────────────────────────────────────────────────────────────────

class SnapshotOut(BaseModel):
    id: int = Field(description="What POST /forecast/ takes as `snapshot_id`.")
    created_at: Optional[DateTime]
    job_id: Optional[str]
    request_type: Optional[str]
    operators: List[str] = Field(description="What was ASKED FOR.")
    registrations: List[str]
    covered_operators: List[str] = Field(description="What the dataset actually HOLDS.")
    covered_registration_count: int
    as_of: Optional[Date]
    profile: Optional[str]
    row_count: int
    restored_at: Optional[DateTime]
    restore_count: int
    edits_applied_at: Optional[DateTime] = Field(description="When the fleet-sheet edits were last laid over this run.")
    is_live: bool = Field(description="This is the run the report shows right now.")


class SnapshotFilters(BaseModel):
    registration: Optional[List[str]]
    operator: Optional[List[str]]
    date: Optional[Date]
    as_of: Optional[Date]


class SnapshotsPage(BaseModel):
    total: int
    limit: int
    offset: int
    retention_days: int
    filters: SnapshotFilters
    items: List[SnapshotOut]


class ForecastStarted(BaseModel):
    job_id: str = Field(description="Follow it on /status/{job_id} or /status/stream.")
    operators: List[str]
    registrations: Optional[List[str]]
    as_of: Optional[Date]
    profile: Optional[str]
    force: bool


class ForecastRestoring(BaseModel):
    job_id: str
    mode: Literal["snapshot"]
    snapshot: SnapshotOut


class ForecastLast(BaseModel):
    datetime: Optional[DateTime]
    request_type: Optional[str]
    request_params: Optional[dict[str, Any]]


class ParamSpec(BaseModel):
    name: str
    type: str = Field(description="date | int | float")
    default: Union[str, int, float]
    group: str
    label: str
    description: str
    min: Optional[Union[int, float]] = None
    max: Optional[Union[int, float]] = None


class ParamsSchema(BaseModel):
    model_version: str
    groups: List[str]
    params: List[ParamSpec]


class ProfileOut(BaseModel):
    name: str
    description: Optional[str]
    model_version: str
    params: dict[str, Any] = Field(description="The overrides stored on the profile.")
    effective: Optional[dict[str, Any]] = Field(description="Every knob as a run will use it; null if the stored params no longer resolve.")
    error: Optional[str]
    is_default: bool
    enabled: bool
    updated_at: Optional[DateTime]
    updated_by: Optional[str]


class ClaimOut(BaseModel):
    id: int
    airline: str
    calendar_year: int
    number_of_claims: int
    claims_amount_total: Optional[float]
    claims_amount_outstanding: Optional[float]
    currency_rate: Optional[float]
    claims_amount_total_usd: Optional[float]
    claims_amount_outstanding_usd: Optional[float]
    currency: Optional[str] = Field(description="USD | EUR | GBP")
    policy_type: Optional[str] = Field(description="HD | HSL | HW | WXS")
    created_at: Optional[DateTime]
    updated_at: Optional[DateTime]


class ClaimWritten(ClaimOut):
    airline_resolved_from: Optional[str] = Field(None, description="The name as sent, when the reference corrected it.")
    airline_unresolved: Optional[str] = Field(None, description="Set when the reference could not settle the name: stored as sent.")


class ClaimsPage(BaseModel):
    total: int
    limit: int
    offset: int
    items: List[ClaimOut]


class NameCorrection(BaseModel):
    typed: str
    resolved: str


class NameUnresolved(BaseModel):
    typed: str
    reason: str
    rows: int
    candidates: List[str]
    note: str


class RateUsed(BaseModel):
    currency: str
    calendar_year: int
    rate: float


class ClaimsLoaded(BaseModel):
    received: int
    inserted: int
    ids: List[int]
    airlines_resolved: List[NameCorrection]
    airlines_unresolved: List[NameUnresolved]
    rates_used: List[RateUsed]


class FleetSheetRow(BaseModel):
    """One row of forecast.detailed_aircraft_information. Keys are the view's column names."""
    model_config = ConfigDict(populate_by_name=True)
    id: int = Field(description="Derived from (Airline, Registration, Contract Year); what PATCH takes.")
    airline: Optional[str] = Field(alias="Airline")
    aircraft_type: Optional[str] = Field(alias="Aircraft Type")
    manufacturer: Optional[str] = Field(alias="Manufacturer")
    master_series: Optional[str] = Field(alias="Master Series")
    current_family: Optional[str] = Field(alias="Current Family")
    registration: str = Field(alias="Registration")
    contract_year: str = Field(alias="Contract Year")
    data_type: Optional[str] = Field(alias="Data Type", description="Actuals | Forecast")
    msn: Optional[str] = Field(alias="MSN")
    yom: Optional[str] = Field(alias="YOM")
    seats: Optional[int] = Field(alias="Seats")
    av_inc: Optional[str] = Field(alias="Agreed Value / INC / mUSD", description="Decimal as a string")
    av_ave: Optional[str] = Field(alias="Agreed Value / AVE / mUSD")
    av_aw_ave: Optional[str] = Field(alias="Agreed Value / AW AVE / mUSD")
    av_exp: Optional[str] = Field(alias="Agreed Value / EXP / mUSD")
    csl: Optional[int] = Field(alias="CSL / mUSD")
    lessor: Optional[str] = Field(alias="Lessor")
    manager: Optional[str] = Field(alias="Manager")
    owner: Optional[str] = Field(alias="Owner")
    lease: Optional[str] = Field(alias="Lease")
    lease_type: Optional[str] = Field(alias="Lease Type")
    edited: bool = Field(alias="Edited")
    edited_fields: List[str] = Field(alias="Edited Fields")
    original_values: Optional[dict[str, Any]] = Field(alias="Original Values", description="The model's value of each edited field.")
    edited_at: Optional[DateTime] = Field(alias="Edited At")
    edited_by: Optional[str] = Field(alias="Edited By")
    edits_inherited_from: Optional[str] = Field(alias="Edits Inherited From", description="A projected year takes carried fields from this earlier edited year.")
    recalculation_pending: bool = Field(alias="Recalculation Pending", description="Edited, but the report has not taken it in yet.")


class FleetSheetPage(BaseModel):
    items: List[FleetSheetRow]
    total: int
    limit: int
    offset: int


class FieldSpec(BaseModel):
    field: str
    type: str = Field(description="text | year | integer | musd | choice")
    nullable: bool
    affects_report: bool
    max_length: Optional[int] = None
    min: Optional[Union[int, str]] = None
    max: Optional[Union[int, str]] = None
    decimals: Optional[int] = None
    options: Optional[List[str]] = None


class DerivedField(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    field: str
    from_: str = Field(alias="from")


class FleetSheetFields(BaseModel):
    fields: List[FieldSpec]
    derived: List[DerivedField]


class FleetEditsReverted(BaseModel):
    airline: str
    rows_reverted: int
    ids: List[int]


class FleetEditsStatus(BaseModel):
    snapshot_id: Optional[int]
    snapshot_created_at: Optional[DateTime]
    as_of: Optional[Date]
    covered_operators: List[str]
    refreshed_at: Optional[DateTime]
    edits_applied_at: Optional[DateTime]
    pending: bool
    pending_airlines: List[str]
    pending_since: Optional[DateTime]
    last_change_at: Optional[DateTime]


# ── insured fleet: reference ──────────────────────────────────────────────────────────────────

class AirlineOut(BaseModel):
    id: int
    airline_name: str
    icao: Optional[str]
    iata: Optional[str]
    is_asg: bool
    logo_url: Optional[str]


class ContactOut(BaseModel):
    id: int
    company: Optional[str]
    contact: Optional[str]
    email: Optional[str]
    phone: Optional[str]
    note: Optional[str]


class PartyBrief(BaseModel):
    id: int
    name: str
    details: Optional[str]


class PartyOut(PartyBrief):
    contacts: List[ContactOut]


# ── insured fleet: fleet ──────────────────────────────────────────────────────────────────────

class TemplateUrls(BaseModel):
    airborne: Optional[str]
    on_the_ground: Optional[str]


class EngineTypeOut(BaseModel):
    id: int
    manufacturer: Optional[str]
    master_series: str


class AircraftTypeOut(EngineTypeOut):
    category: str = Field(description="passenger | cargo | other")
    label: str = Field(description="Manufacturer, series and category, ready for a dropdown.")
    template_url: TemplateUrls


class ServiceOut(BaseModel):
    id: Optional[int] = Field(description="null when the aircraft has no row yet (the defaults are shown).")
    agreed_value_fixed: bool
    source: str
    status: str
    usage_status: Optional[str]
    lease_currency: str
    policy_currency: str
    recorded: bool


class EngineOut(BaseModel):
    id: int
    position: Optional[int]
    engine_type: Optional[EngineTypeOut]
    msn: Optional[str]
    installed_on: Optional[Date]
    details: Optional[str]
    fitted: bool = Field(description="The newest installation at its position — what is bolted on now.")


class AircraftBrief(BaseModel):
    id: int
    registration: Optional[str]
    msn: Optional[str]
    aircraft_type: Optional[AircraftTypeOut]
    airline: Optional[AirlineOut]
    service: ServiceOut


class AircraftOut(AircraftBrief):
    engines: List[EngineOut]


# ── insured fleet: leasing & policy ───────────────────────────────────────────────────────────

class AgreementOut(BaseModel):
    id: int
    name: str
    start_date: Optional[Date]
    lessor: Optional[PartyBrief]
    alternative_contract_party: Optional[str]
    other_contracts: Optional[str]


class LeaseOut(BaseModel):
    id: int
    aircraft: Optional[AircraftBrief]
    aircraft_id: int
    agreement: Optional[AgreementOut]
    effective_date: Optional[Date]
    agreed_value_preliminary: Optional[float]
    agreed_value_final: Optional[float]
    agreed_value_calculated: Optional[float] = Field(description="The depreciation formula applied at `agreed_value_as_of`.")
    agreed_value_as_of: Optional[Date]
    depreciation_ratio: Optional[float]
    depreciation_start_date: Optional[Date]
    combined_single_limit: Optional[float]
    hull_spares_war_excess_liability: Optional[float]
    hull_deductible_buy_down: Optional[float]
    currency: Optional[str]
    created_at: Optional[DateTime]
    updated_at: Optional[DateTime]


class AgreementWithLeases(AgreementOut):
    aircraft: List[LeaseOut] = Field(description="Every lease record under the agreement, with its aircraft.")


class PolicyOut(BaseModel):
    id: int
    insured: Optional[PartyBrief]
    reinsured: Optional[PartyBrief]
    retrocedent: Optional[PartyBrief]
    period_from: Optional[Date]
    period_to: Optional[Date]
    period: str = Field(description="`from..to`, open-ended when there is no end date.")
    hull_all_risks_deductible: Optional[float]
    spares_deductible: Optional[float]
    hull_deductible_buy_down: Optional[float]
    hull_deductible_aggregate: Optional[float]
    combined_single_limit: Optional[float]
    hull_war_overall_limit: Optional[float]
    hull_spares_limit: Optional[float]
    hull_spares_war_excess_liability: Optional[float]
    hull_war_confiscation_limit: Optional[float]
    hull_war_confiscation_limit_selected_country: Optional[float]
    selected_country: Optional[str]
    reinsured_amount: Optional[float]
    cut_through_clause: Optional[str]
    created_at: Optional[DateTime]
    updated_at: Optional[DateTime]


class PolicyAircraft(BaseModel):
    coverage_id: int
    covered_from: Date
    covered_to: Optional[Date]
    aircraft: AircraftBrief


class PolicyWithAircraft(PolicyOut):
    aircraft: List[PolicyAircraft]


class PolicyRenewed(PolicyOut):
    aircraft_carried: int = Field(description="How many coverage rows were carried onto the new policy.")


class CoverageOut(BaseModel):
    id: int
    aircraft_id: int
    aircraft: Optional[AircraftBrief]
    policy: Optional[PolicyOut]
    covered_from: Optional[Date]
    covered_to: Optional[Date]


class AircraftCard(AircraftOut):
    as_of: Date
    lease: Optional[LeaseOut] = Field(description="The lease terms in force on `as_of`.")
    coverage: Optional[CoverageOut] = Field(description="The coverage in force on `as_of`.")
    lease_history: Optional[List[LeaseOut]] = Field(None, description="Absent with history=false.")
    coverage_history: Optional[List[CoverageOut]] = Field(None, description="Absent with history=false.")


class ComparedField(BaseModel):
    required: Optional[float] = Field(description="What the lease asks for.")
    provided: Optional[float] = Field(description="What the policy gives.")
    match: bool


class CoverCompareRow(BaseModel):
    aircraft: AircraftBrief
    has_lease: bool
    has_policy: bool
    policy_id: Optional[int]
    lease_id: Optional[int]
    status: str
    usage_status: Optional[str]
    match: bool
    fields: dict[str, ComparedField] = Field(description="combined_single_limit, hull_spares_war_excess_liability, hull_deductible_buy_down")


class CoverComparePage(BaseModel):
    items: List[CoverCompareRow]
    total: int
    as_of: Date


class FieldChange(BaseModel):
    field: str
    old: Any
    new: Any


class AuditEntry(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    id: int
    schema_: str = Field(alias="schema")
    table: str
    row_id: Optional[int]
    operation: str = Field(description="INSERT | UPDATE | DELETE")
    changed_at: Optional[DateTime]
    changed_by: Optional[str]
    changes: List[FieldChange]
    old_row: Optional[dict[str, Any]]
    new_row: Optional[dict[str, Any]]


# ── which operation returns what ──────────────────────────────────────────────────────────────
# (METHOD, path as in the OpenAPI document) -> the `data` type. None = the body is literally `null`
# (not the envelope). Utils/OpenAPIDocs wraps everything else in the envelope.
_EMPTY = List[None]

RESPONSE_DATA: dict[tuple[str, str], Any] = {
    ("GET", "/health/"): None,

    ("GET", "/status"): List[JobStatusOut],
    ("GET", "/status/{job_id}"): JobStatusOut,
    ("POST", "/status/{job_id}/cancel"): JobCancelled,
    ("POST", "/files"): FileQueued,
    ("POST", "/webhooks/microsoft"): _EMPTY,
    ("POST", "/webhooks/microsoft/lifecycle"): _EMPTY,
    ("GET", "/flightradar/flightsummary"): _EMPTY,
    ("GET", "/flightradar/airports"): _EMPTY,

    ("GET", "/scheduler"): List[ScheduleOut],
    ("GET", "/scheduler/{name}"): ScheduleOut,
    ("PATCH", "/scheduler/{name}"): ScheduleOut,
    ("DELETE", "/scheduler/{name}"): NameOnly,
    ("POST", "/scheduler/{name}/run"): ScheduleRun,
    ("GET", "/queues"): List[QueueOut],
    ("POST", "/queues/{queue}/pause"): QueueToggled,
    ("POST", "/queues/{queue}/resume"): QueueToggled,
    ("POST", "/queues/{queue}/purge"): QueuePurged,
    ("GET", "/tokens"): List[TokenOut],
    ("POST", "/tokens"): TokenCreated,
    ("PATCH", "/tokens/{prefix}"): TokenOut,
    ("DELETE", "/tokens/{prefix}"): PrefixOnly,

    ("GET", "/airlines/"): List[AirlineSearchHit],
    ("GET", "/registrations/"): List[RegistrationSearchHit],
    ("GET", "/capacity/state"): CapacityState,
    ("POST", "/capacity/pause"): CapacityState,
    ("POST", "/capacity/resume"): CapacityState,

    ("GET", "/forecast/claims"): ClaimsPage,
    ("POST", "/forecast/claims"): ClaimWritten,
    ("GET", "/forecast/claims/{claim_id}"): ClaimOut,
    ("PATCH", "/forecast/claims/{claim_id}"): ClaimWritten,
    ("POST", "/forecast/claims/bulk"): ClaimsLoaded,
    ("GET", "/forecast/aircraft-details"): FleetSheetPage,
    ("GET", "/forecast/aircraft-details/fields"): FleetSheetFields,
    ("DELETE", "/forecast/aircraft-details/edits"): FleetEditsReverted,
    ("GET", "/forecast/aircraft-details/status"): FleetEditsStatus,
    ("POST", "/forecast/aircraft-details/apply"): ForecastRestoring,
    ("GET", "/forecast/aircraft-details/{row_id}"): FleetSheetRow,
    ("PATCH", "/forecast/aircraft-details/{row_id}"): FleetSheetRow,
    ("DELETE", "/forecast/aircraft-details/{row_id}/edits"): FleetSheetRow,
    ("POST", "/forecast/"): Union[ForecastStarted, ForecastRestoring],
    ("GET", "/forecast/last"): ForecastLast,
    ("GET", "/forecast/snapshots"): SnapshotsPage,
    ("GET", "/forecast/params/schema"): ParamsSchema,
    ("GET", "/forecast/profiles"): List[ProfileOut],
    ("POST", "/forecast/profiles"): ProfileOut,
    ("PATCH", "/forecast/profiles/{name}"): ProfileOut,
    ("DELETE", "/forecast/profiles/{name}"): NameOnly,
    ("POST", "/forecast/profiles/{name}/default"): ProfileOut,

    ("GET", "/ref/airlines"): Page[AirlineOut],
    ("POST", "/ref/airlines"): AirlineOut,
    ("GET", "/ref/airlines/{airline_id}"): AirlineOut,
    ("PATCH", "/ref/airlines/{airline_id}"): AirlineOut,
    ("DELETE", "/ref/airlines/{airline_id}"): AirlineOut,
    ("GET", "/ref/parties"): Page[PartyOut],
    ("POST", "/ref/parties"): PartyOut,
    ("GET", "/ref/parties/{party_id}"): PartyOut,
    ("PATCH", "/ref/parties/{party_id}"): PartyOut,
    ("DELETE", "/ref/parties/{party_id}"): PartyOut,
    ("POST", "/ref/parties/{party_id}/contacts"): ContactOut,
    ("PATCH", "/ref/contacts/{contact_id}"): ContactOut,
    ("DELETE", "/ref/contacts/{contact_id}"): ContactOut,

    ("GET", "/fleet/aircraft-types"): Page[AircraftTypeOut],
    ("POST", "/fleet/aircraft-types"): AircraftTypeOut,
    ("PATCH", "/fleet/aircraft-types/{type_id}"): AircraftTypeOut,
    ("DELETE", "/fleet/aircraft-types/{type_id}"): AircraftTypeOut,
    ("GET", "/fleet/engine-types"): Page[EngineTypeOut],
    ("POST", "/fleet/engine-types"): EngineTypeOut,
    ("PATCH", "/fleet/engine-types/{type_id}"): EngineTypeOut,
    ("DELETE", "/fleet/engine-types/{type_id}"): EngineTypeOut,
    ("GET", "/fleet/aircraft"): Page[AircraftOut],
    ("POST", "/fleet/aircraft"): AircraftOut,
    ("GET", "/fleet/aircraft/by-registration/{registration}"): AircraftCard,
    ("GET", "/fleet/aircraft/{aircraft_id}"): AircraftCard,
    ("PATCH", "/fleet/aircraft/{aircraft_id}"): AircraftOut,
    ("DELETE", "/fleet/aircraft/{aircraft_id}"): AircraftOut,
    ("GET", "/fleet/aircraft/{aircraft_id}/service"): ServiceOut,
    ("PATCH", "/fleet/aircraft/{aircraft_id}/service"): ServiceOut,
    ("GET", "/fleet/aircraft/{aircraft_id}/engines"): Page[EngineOut],
    ("POST", "/fleet/aircraft/{aircraft_id}/engines"): EngineOut,
    ("PATCH", "/fleet/engines/{engine_id}"): EngineOut,
    ("DELETE", "/fleet/engines/{engine_id}"): EngineOut,

    ("GET", "/leasing/agreements"): Page[AgreementOut],
    ("POST", "/leasing/agreements"): AgreementOut,
    ("GET", "/leasing/agreements/{agreement_id}"): AgreementWithLeases,
    ("PATCH", "/leasing/agreements/{agreement_id}"): AgreementOut,
    ("DELETE", "/leasing/agreements/{agreement_id}"): AgreementOut,
    ("GET", "/leasing/leases"): Page[LeaseOut],
    ("POST", "/leasing/leases"): LeaseOut,
    ("GET", "/leasing/leases/{lease_id}"): LeaseOut,
    ("PATCH", "/leasing/leases/{lease_id}"): LeaseOut,
    ("DELETE", "/leasing/leases/{lease_id}"): LeaseOut,

    ("GET", "/policy/policies"): Page[PolicyOut],
    ("POST", "/policy/policies"): PolicyOut,
    ("GET", "/policy/policies/{policy_id}"): PolicyWithAircraft,
    ("PATCH", "/policy/policies/{policy_id}"): PolicyOut,
    ("DELETE", "/policy/policies/{policy_id}"): PolicyOut,
    ("POST", "/policy/policies/{policy_id}/renew"): PolicyRenewed,
    ("GET", "/policy/coverage"): Page[CoverageOut],
    ("POST", "/policy/coverage"): CoverageOut,
    ("GET", "/policy/coverage/compare"): CoverComparePage,
    ("PATCH", "/policy/coverage/{coverage_id}"): CoverageOut,
    ("DELETE", "/policy/coverage/{coverage_id}"): CoverageOut,

    ("GET", "/history/"): Page[AuditEntry],
    ("GET", "/history/aircraft/{aircraft_id}"): Page[AuditEntry],
}
