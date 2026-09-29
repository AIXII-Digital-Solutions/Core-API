# Certificates — integration brief for the portal

**Living document.** The certificates module is being built in stages; this brief is updated with
every change the portal has to follow. Read the [change log](#9-change-log) first to see what is new
since you last looked, and [open items](#8-open-items) for what is not built yet.

What it covers: issuing the **Reinsurance certificate (AVN 67B)**, its history, the issuing company's
branding, the signatory profile, and the fields elsewhere in the domain the certificate reads. The
**Insurance certificate** is the next document and will be added here.

The general API contract — base URL, the response envelope, error shapes, conventions — is in
[`insured-fleet-portal-brief.md`](insured-fleet-portal-brief.md). This document only adds to it.

---

## 1. Who is calling — the signatory rule

Every request carries the service token and the portal user it is made for:

| header | value |
|---|---|
| `X-Service-Token` | the portal's service token |
| `X-Portal-User-Id` | the portal user's UUID |
| `X-Portal-User-Email` | their e-mail |
| `X-Portal-User-Name` | their full name, percent-encoded UTF-8 (`urllib.parse.quote`) |

**A certificate is signed by the portal user who issues it.** Their name and e-mail come from these
headers; their title, phone and signature image come from their signatory profile (§5). Without the
headers, issuing a certificate and the signatory endpoints answer **422** (`certificate.signatory`
or `header.X-Portal-User-Id`). The headers are believed only next to the service token.

---

## 2. Issuing a reinsurance certificate

Entry point: the aircraft card or the policy page → **"Issue reinsurance certificate"**.

### 2.1 The form

| field | required | notes |
|---|---|---|
| `aircraft_id` | yes | |
| `policy_id` | no | default: the policy covering the aircraft on the date of issue, else the next one to start |
| `date_of_issue` | no | default: today (recorded as system-generated); sent = recorded as user-supplied |

A collapsed block **"Overrides (from e-mail / rider / mark-up)"** — each replaces the stored value
for this certificate only, and is kept with it:

| field | replaces |
|---|---|
| `agreed_value` | the lease's agreed value (number) |
| `equipment` | "<manufacturer> <series>" — e.g. to name the original engines |
| `effective_date` | the lease terms' effective date |
| `contract_parties` | the agreement's contract parties — list of strings, order matters |
| `contracts` | the contracts list — list of strings, the full text of each item |
| `addressees` | the notice addresses — list of `{company, contacts, email}` |

### 2.2 The flow

1. **Check** — `POST /certificates/reinsurance/preview` with the form body. `data`:

   | key | show as |
   |---|---|
   | `reference_number` | e.g. `CY25/SCAT/#####` — the number is allocated only on issue |
   | `variant` | `retrocession` or `standard` (the wording used) |
   | `data` | every value that will be printed — show a summary |
   | `alerts[]` | `{code, msg}` — yellow warnings; they do NOT block issuing |
   | `errors[]` | `{field: "certificate.<name>", msg}` — red; disable **Issue** |
   | `can_issue` | |

   Where to send the user for an error:

   | `field` | fix it in |
   |---|---|
   | `certificate.certificate_code` | the airline (§6.1) |
   | `certificate.lease`, `agreed_value`, `contract_parties`, `effective_date`, `agreement_start_date` | the aircraft's lease / agreement (§6.2) |
   | policy fields (`hull_all_risks_deductible`, `combined_single_limit`, …) | the policy |
   | `certificate.insured` / `reinsured` / `period_to` | the policy |
   | `certificate.equipment` / `msn` | the aircraft |
   | `certificate.signatory` | no portal user on the request |

2. **Preview PDF** — `POST /certificates/reinsurance/preview/pdf`, same body. Returns
   `application/pdf` marked **DRAFT** on every page, reference masked. Open it in a viewer / new tab.
   422 with the same `certificate.<field>` entries while something required is missing.

3. **Issue** — `POST /certificates/reinsurance`, same body. **201**, `data`:
   `reference_number` (e.g. `CY25/SCAT/00001`), `pdf_url`, `alerts`, `data` (the printed values).
   Errors: **422** — missing data or no signatory; **404** — the aircraft does not exist, or is not
   covered on the date of issue or later.

An issued certificate is never edited. A correction is a **new issue with a new number**.

### 2.3 The reference number

`CY<yy>/<airline code>/<nnnnn>` — the year of the policy's `period_from`, the airline's
`certificate_code`, and a sequence. The sequence runs per certificate type by default and can be
switched (server setting) to one counter shared with the insurance certificate; numbers never repeat
either way. The portal never builds or changes it.

---

## 3. Certificate history

A tab on the aircraft card and on the policy page.

- **List** — `GET /certificates/reinsurance?aircraft_id=` (or `policy_id=`, `registration=`
  separator-insensitive, `limit`, `offset`) → `{items, total}`, newest first. Columns: reference,
  date of issue, registration, issued by (`issued_by_user.name`), alert count, PDF button.
- **Detail** — `GET /certificates/reinsurance/{id}` — adds `data`: every value as printed, including
  `data.issuer` (company and signatory as they were at issue).
- **PDF** — `GET /certificates/reinsurance/{id}/pdf` opens inline; `?download=true` saves it as
  `Reinsurance-Certificate-CY25-SCAT-00001.pdf`.

---

## 4. Page "Certificate settings" — the issuing company

- **Load** — `GET /certificates/settings` → `company_name`, `company_legal_name`, `address_line`,
  `legal_footer`, `brand_primary`, `brand_accent`, `logo`, `stamp` (each image `{url, content_type,
  size, sha256, updated_at}` or `null`).
- **Form**

  | field | printed as | notes |
  |---|---|---|
  | `company_legal_name` | "as held on file by …", "AUTHORISED SIGNATORY …" | required |
  | `company_name` | text beside the logo in the header | empty = logo only (current) |
  | `address_line` | the line at the foot of page 1 | |
  | `legal_footer` | the regulatory small print on page 1 | multi-line |
  | `brand_primary`, `brand_accent` | header text colour, rule above page numbers | `#RRGGBB` |

  Save with `PATCH /certificates/settings` — only the fields changed; `null` / `""` clears the
  optional ones.
- **Logo and stamp** — preview `GET /certificates/settings/logo` (or `/stamp`: the image itself);
  upload `PUT /certificates/settings/logo` (or `/stamp`) as `multipart/form-data`, field `file`, PNG /
  JPEG / SVG up to 2 MB (SVG preferred — drawn as vector, sharp in print); remove with `DELETE`.
  422 = broken or unsupported file; show its `msg`.
- Tell the user: **changes apply to new certificates; issued ones keep what they printed.**

Current values: legal name `AI12`, no header text, the AI12 logo (SVG), no stamp, no address line,
no footer.

---

## 5. Page "My signatory profile" — the current user

- **Load** — `GET /certificates/signatory` → `title`, `phone`, `name`, `email`, `printed_as`,
  `signature`. `printed_as` is exactly what a certificate issued now prints under the signature —
  show it as a preview. `name` / `email` are optional overrides; empty = the portal's.
- **Save** — `PATCH /certificates/signatory` with `{title, phone, name?, email?}`.
- **Signature** — `PUT /certificates/signatory/signature` (field `file`, PNG with a transparent
  background preferred, JPEG or SVG, up to 2 MB); `GET` shows it; `DELETE` removes it.
- A user without a profile can still issue: the certificate then shows their portal name and e-mail
  with no signature image. Nudge them to fill it in before their first certificate.

---

## 6. Data elsewhere the certificate reads

### 6.1 Airline — `certificate_code`
`POST` / `PATCH /ref/airlines` take `certificate_code`: letters, digits and hyphens, stored upper-case,
unique. It is the airline part of the reference number; without it the airline's aircraft cannot get
a certificate. SCAT is set (`SCAT`).

### 6.2 Lease agreement — contract parties and contracts
- `contract_parties` on `POST` / `PATCH /leasing/agreements`: the parties the certificate names as
  Contract Party(ies), **in print order** (usually the lessor first) — party ids or names. Returned
  on `/leasing/agreements*` as `contract_parties: [{id, name, details}]`. Edit as a multi-select with
  drag-to-reorder. Empty = the lessor alone.
- The notice e-mails on the Schedule of Parties are those parties' contacts
  (`/ref/parties/{id}/contacts`). A party without contacts is printed without an e-mail and raises an
  alert.
- `other_contracts`: **one line = one item** of the certificate's Contracts list, after the lease
  agreement itself.

### 6.3 Policy — "Certificate wording"
New editable fields on the policy, each defaulting to the market-standard text from the client's
sample. Put them in a "Certificate wording" section of the policy form:

| field | default |
|---|---|
| `period_wording` | both days inclusive, local standard time at the address of the Insured |
| `geographical_limits` | Worldwide excluding Ukraine and the region of Crimea, Iran, North Korea and Syria. … (multi-line) |
| `hull_war_clause` | LSW 555D |
| `war_exclusion_clause` | AVN 48B |
| `war_exclusion_exception` | sub-paragraph(s) (b) of AVN48B — `""` = no exception |
| `war_liability_clause` | AVN 52E |
| `fifty_fifty_clause` | AVS103A |

---

## 7. What the checks mean (`alerts[].code`)

| code | meaning |
|---|---|
| `csl_above_policy` | the lease requires a higher combined single limit than the policy provides; the certificate shows the policy's |
| `war_liability_below_lease` | the war liability limit is below what the lease requires |
| `war_liability_not_csl` | the war liability limit differs from the combined single limit |
| `no_deductible_aggregate` | a hull deductible buy-down is shown without an annual aggregate |
| `country_limit_without_country` | the policy has a country confiscation limit but no country; left off |
| `no_notice_address` | a contract party has no contact block; printed without an e-mail |

---

## 8. Open items

- **Insurance certificate** — not built yet; will reuse the same flow, history, settings and
  signatory.
- **Real company details** — legal name, address line and legal footer still to be entered in
  "Certificate settings"; no stamp uploaded yet.
- **Letter of Undertaking / Schedule of Parties** are always part of the reinsurance certificate PDF
  (pages 5-7); there is no separate endpoint for them.

---

## 9. Change log

| date | change |
|---|---|
| 2026-09-29 | Reinsurance certificate: preview, issue, history, PDF; reference numbers; airline `certificate_code`; policy certificate wording; agreement `contract_parties`. |
| 2026-09-29 | Signatory = the issuing portal user (+ profile at `/certificates/signatory`); company branding and images moved to the database (`/certificates/settings`); DRAFT PDF preview. |
