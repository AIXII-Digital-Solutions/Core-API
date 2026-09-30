# Certificates — integration brief for the portal

**Living document.** The certificates module is being built in stages; this brief is updated with
every change the portal has to follow. Read the [change log](#11-change-log) first to see what is new
since you last looked, and [open items](#10-open-items) for what is not built yet.

What it covers: the **Reinsurance certificate** and the **Insurance certificate** (both AVN 67B) —
drafting, editing, issuing, history — the issuing company's branding (admin panel), and the fields
elsewhere in the domain the certificates read.

The general API contract — base URL, the response envelope, error shapes, conventions — is in
[`insured-fleet-portal-brief.md`](insured-fleet-portal-brief.md). This document only adds to it.

---

## 1. Who is calling — the signatory

Every request carries the service token and the portal user it is made for:

| header | value |
|---|---|
| `X-Service-Token` | the portal's service token |
| `X-Portal-User-Id` | the portal user's UUID |
| `X-Portal-User-Email` | their e-mail |
| `X-Portal-User-Name` | their full name, percent-encoded UTF-8 (`urllib.parse.quote`) |

**A certificate is signed by hand by the portal user who issues it.** No signature is uploaded: the
printed certificate has an empty space above the signatory's name, and they sign the paper copy
after issuing. The certificate prints the signatory's details, which the portal **sends with the
request**:

```json
"signatory": {"full_name": "Jane Doe", "title": "Head of Insurance",
              "phone": "+7 700 000 0000", "email": "j.doe@ai12.example"}
```

Only `full_name` is required. Fill the block from the current user's portal profile and let them
edit it in the form. If it is not sent, the draft takes the name and e-mail from the
`X-Portal-User-*` headers when it is created and keeps them. With neither, the certificate cannot be
issued (`certificate.signatory`). The headers are trusted only together with the service token.

---

## 2. Draft → issued

Every certificate starts as a **draft** and ends as **issued**. `status` says which.

| | draft | issued |
|---|---|---|
| reference number | allocated when the draft is created, kept through every edit | the same |
| values | re-resolved from the policy, lease and aircraft **every time** it is read or drawn | frozen as printed |
| editing | `PATCH` its inputs; `DELETE` discards it | **impossible** — 409; the database refuses it too |
| PDF | drawn on request, **DRAFT across every page** | the file as issued, no mark |
| `errors` | what still blocks issuing (`can_issue: false`) | always `[]` |

Issuing is final. A correction to an issued certificate is a **new draft, with a new number**. A
discarded draft's number is not reused.

---

## 3. Endpoints

`{kind}` is `reinsurance` or `insurance`. Both have the same endpoints and bodies.

| method | path | what |
|---|---|---|
| `POST` | `/certificates/{kind}/preview` | what it would say, nothing saved (reference masked `CY25/SCAT/#####`) |
| `POST` | `/certificates/{kind}` | create a **draft** → 201 |
| `GET` | `/certificates/{kind}` | list: `aircraft_id`, `policy_id`, `registration`, `status=draft\|issued`, `limit`, `offset` → `{items, total}` |
| `GET` | `/certificates/{kind}/{id}` | one, with `data` |
| `PATCH` | `/certificates/{kind}/{id}` | change a draft's inputs → 409 if issued |
| `POST` | `/certificates/{kind}/{id}/issue` | issue → 409 if already issued, 422 while it has errors |
| `DELETE` | `/certificates/{kind}/{id}` | discard a draft → 409 if issued |
| `GET` | `/certificates/{kind}/{id}/pdf` | the PDF (`?download=true` to save) |

### 3.1 The body (create, preview; PATCH takes the same fields except `aircraft_id`)

| field | required | notes |
|---|---|---|
| `aircraft_id` | yes (create / preview) | cannot change on a draft: another aircraft is another certificate |
| `policy_id` | no | default: the policy covering the aircraft on the date of issue, else the next one to start |
| `date_of_issue` | no | default: the day it is **issued** (system-generated); a draft shows today until then |
| `signatory` | see §1 | `{full_name, title, phone, email}` |
| `signed_by` | insurance only | `broker` (default) or `insurer` — see §4 |

A collapsed block **"Overrides (from e-mail / rider / mark-up)"**. Each one replaces the stored value
for this certificate only, and is kept with it:

| field | replaces |
|---|---|
| `agreed_value` | the lease's agreed value (number) |
| `equipment` | "<manufacturer> <series>", e.g. to name the original engines |
| `effective_date` | the lease terms' effective date |
| `contract_parties` | the agreement's contract parties: a list of strings, in order |
| `contracts` | the contracts list: a list of strings, the full text of each item |
| `addressees` | the notice addresses: a list of `{company, contacts, email}` |

A second collapsed block **"Certificate wording"**: the seven wording fields of §7.3. Each one is
optional; a field left out takes the company default from `/certificates/settings`.

On `PATCH`, only the fields you send change. Sending a field as `null` drops that override, and the
stored value (for wording: the company default) applies again. `war_exclusion_exception: ""` is
not "empty": it is a real override meaning "no exception", and it is kept. The inputs as saved are returned in `request`, so the edit form fills
from it.

### 3.2 The record

`id, kind, status, reference_number, date_of_issue, date_of_issue_source (system|user), variant,
registration, msn, aircraft_id, policy_id, aircraft_lease_id, alerts[], errors[], can_issue,
created_by_user {id, name}, created_at, updated_at, issued_at, issued_by, issued_by_user {id, email,
name}, request, pdf_url` plus `data` (every value it prints) on everything except the list.

- `alerts[]` — `{code, msg}`, shown as yellow warnings. They do **not** block issuing (§8).
- `errors[]` — `{field: "certificate.<name>", msg}`, shown in red. While there are any, disable
  **Issue** and **PDF**.

Where to send the user for an error:

| `field` | fix it in |
|---|---|
| `certificate.certificate_code`, `certificate.airline` | the airline (§7.1). Without these no draft can be created (422): there is no reference number to give it |
| `certificate.lease`, `agreed_value`, `contract_parties`, `effective_date`, `agreement_start_date` | the aircraft's lease / agreement (§7.2) |
| policy fields (`hull_all_risks_deductible`, `combined_single_limit`, …) | the policy |
| `certificate.insured` / `reinsured` / `insurer` / `period_to` | the policy |
| `certificate.equipment` / `msn` | the aircraft |
| `certificate.signatory` | the signatory block of the form (§1) |

### 3.3 The screens

1. **Aircraft card / policy page** → "New reinsurance certificate" / "New insurance certificate".
   The form (§3.1) opens; use `preview` for a live check as the user fills it in (optional).
2. **Create** → `POST /certificates/{kind}`. You now have a draft with its number. Open its page.
3. **Draft page**:
   - the values (`data`) as a summary;
   - alerts and errors;
   - an **Edit** form (`PATCH`);
   - **View PDF** (opens inline, marked DRAFT);
   - **Issue** (confirm first: "This is final. The certificate can no longer be changed");
   - **Discard**.
4. **Issued page**:
   - read-only;
   - **PDF** and **Download**;
   - "Issued by {issued_by_user.name} on {issued_at}";
   - the alerts it was issued with.
5. **History tab** on the aircraft card and the policy page: `GET /certificates/{kind}?aircraft_id=`
   (or `policy_id=`). Columns: reference, status badge (Draft / Issued), date of issue, registration,
   created by / issued by, alert count, PDF.

PDF file names: `Reinsurance-Certificate-CY25-SCAT-00001.pdf`, a draft with `-DRAFT` appended.

---

## 4. The insurance certificate

It is the same document for the Insured's own insurance, with these differences:

- **INSURER** instead of REINSURED: the policy's `reinsured` party is printed as the Insurer. There is
  no Retrocedent. If the policy has no reinsured, the error is `certificate.insurer`.
- **INSURED AMOUNT: 100% of 100%** of Sums Insured.
- **Who signs it** — `signed_by`:
  - `broker` (default) — we sign, "in our capacity as insurance broker to the Insured"; the company
    stamp is printed if one is uploaded.
  - `insurer` — the Insurer signs. The wording is theirs, "AUTHORISED SIGNATORY" names the Insurer,
    and **our stamp is not printed**.

  `variant` shows which one applies. Show it as a radio button in the form.
- **Its date should match the reinsurance certificate's.** If an issued reinsurance certificate
  exists for the same aircraft and policy with another date, the alert
  `date_differs_from_reinsurance` says so. Suggest the reinsurance date in the form, taken from the
  latest issued reinsurance certificate in the history.
- It has its own number sequence (`CY25/SCAT/00001` exists for both types) unless the shared counter
  is switched on (§5).

The reinsurance certificate's `variant` is `retrocession` or `standard` (with or without a
retrocedent), as before.

---

## 5. The reference number

`CY<yy>/<airline code>/<nnnnn>`:
- the year of the policy's `period_from`;
- the airline's `certificate_code`;
- a sequence.

By default the sequence runs separately for each certificate type. A server setting can switch it to
one counter shared by both types; numbers never repeat either way. The number is allocated when the
draft is created. If a draft's policy or airline changes, the number follows it, keeping its
sequence. The portal never builds or changes it.

---

## 6. Admin panel → "Certificate settings" (the issuing company)

**This page belongs in the admin panel, not in the users' menu.** Show it only to administrators.

- **Load** — `GET /certificates/settings` returns:
  - `company_name`, `company_legal_name`, `address_line`, `legal_footer`;
  - `brand_primary`, `brand_accent`;
  - the seven wording fields of §7.3, the company defaults;
  - `logo`, `stamp`: each is `{url, content_type, size, sha256, updated_at}`, or `null`.
- **Form**

  | field | printed as | notes |
  |---|---|---|
  | `company_legal_name` | "as held on file by …", "AUTHORISED SIGNATORY …" | required |
  | `company_name` | text beside the logo in the header | empty = logo only (current) |
  | `address_line` | the line at the foot of page 1 | |
  | `legal_footer` | the regulatory small print on page 1 | multi-line |
  | `brand_primary`, `brand_accent` | header text colour, rule above page numbers | `#RRGGBB` |

  Save with `PATCH /certificates/settings`, sending only the fields that changed. `null` or `""`
  clears the optional ones.
- **Certificate wording** — a section of the same page with the seven fields of §7.3, the company
  defaults every certificate starts from. Show the section when the response has the key
  `hull_war_clause`. Sending a field as `null` returns it to the market text (offer "Reset to
  market"); `war_exclusion_exception: ""` means no exception.
- **Logo and stamp**
  - preview: `GET /certificates/settings/logo` (or `/stamp`) returns the image itself;
  - upload: `PUT /certificates/settings/logo` (or `/stamp`) as `multipart/form-data`, field `file`.
    PNG, JPEG or SVG up to 2 MB. SVG is preferred for the logo: it is drawn as vector and stays sharp
    in print. PNG with a transparent background is best for the stamp;
  - remove: `DELETE`;
  - 422 means a broken or unsupported file: show its `msg`.
- **The stamp is printed only if one is uploaded.** Without one, the signature block has no stamp.
- Tell the admin: **drafts pick up changes at once; issued certificates keep what they printed.**

Current values: legal name `AI12`, no header text, the AI12 logo (SVG), no stamp, no address line,
no footer.

---

## 7. Data elsewhere the certificates read

### 7.1 Airline — `certificate_code`
`POST` / `PATCH /ref/airlines` take `certificate_code`: letters, digits and hyphens, stored upper-case,
unique. It is the airline part of the reference number; without it the airline's aircraft cannot get
a certificate. SCAT is set (`SCAT`).

### 7.2 Lease agreement — contract parties and contracts
- `contract_parties` on `POST` / `PATCH /leasing/agreements`:
  - the parties the certificate names as Contract Party(ies), **in print order** (usually the lessor
    first), given as party ids or names;
  - returned on `/leasing/agreements*` as `contract_parties: [{id, name, details}]`;
  - edit it as a multi-select with drag-to-reorder;
  - empty = the lessor alone.
- The notice e-mails on the Schedule of Parties are those parties' contacts
  (`/ref/parties/{id}/contacts`). A party without contacts is printed without an e-mail, and an alert
  is raised.
- `other_contracts`: **one line = one item** of the certificate's Contracts list, after the lease
  agreement itself.

### 7.3 Certificate wording — fields of the certificate, company defaults in `/certificates/settings`
The wording a certificate quotes is **not on the policy any more**. Each field resolves as:
**the certificate's own override** (sent in its body, §3.1) → **the company default** (admin,
§6) → **the market text**.

| field | market text | limit |
|---|---|---|
| `period_wording` | both days inclusive, local standard time at the address of the Insured | 2000 |
| `geographical_limits` | Worldwide excluding Ukraine and the region of Crimea, Iran, North Korea and Syria. … (multi-line) | 2000 |
| `hull_war_clause` | LSW 555D | 256 |
| `war_exclusion_clause` | AVN 48B | 256 |
| `war_exclusion_exception` | sub-paragraph(s) (b) of AVN48B. `""` = no exception | 256 |
| `war_liability_clause` | AVN 52E | 256 |
| `fifty_fifty_clause` | AVS103A | 256 |

Where the certificate prints them (`data`): `period.wording`, `geographical_limits`,
`hull_war.clause`, `liability.war_exclusion_clause`, `liability.war_exclusion_exception` (`null` =
none), `liability.war_liability_clause`, `fifty_fifty_clause`.

**Remove the "Certificate wording" section from the policy form.** The policy endpoints no longer
return these fields, and ignore them if sent.

---

## 8. What the checks mean (`alerts[].code`)

| code | meaning |
|---|---|
| `csl_above_policy` | the lease requires a higher combined single limit than the policy provides; the certificate shows the policy's |
| `war_liability_below_lease` | the war liability limit is below what the lease requires |
| `war_liability_not_csl` | the war liability limit differs from the combined single limit |
| `no_deductible_aggregate` | a hull deductible buy-down is shown without an annual aggregate |
| `country_limit_without_country` | the policy has a country confiscation limit but no country; left off |
| `no_notice_address` | a contract party has no contact block; printed without an e-mail |
| `date_differs_from_reinsurance` | insurance only: dated differently from the issued reinsurance certificate for the same aircraft and policy |

---

## 9. What was removed

- `/certificates/signatory*` (the signatory profile and the signature upload): the signatory now
  comes with the request (§1), and the signature is made by hand.
- `POST /certificates/reinsurance/preview/pdf`: create a draft and open its PDF instead.
- The seven wording fields on `/policy/policies*` (request and response): they are now certificate
  fields, with company defaults (§7.3).
- `POST /certificates/reinsurance` no longer issues directly. It creates a draft, and
  `/{id}/issue` issues it.

---

## 10. Open items

- **Insurer-signed insurance certificate — whose details?** The certificate prints whatever
  `signatory` the request carries. When the Insurer signs, send the Insurer's signatory details, not
  the portal user's. To confirm with the client.
- **Real company details** — the legal name, address line and legal footer still have to be entered
  in the admin panel. No stamp has been uploaded yet.
- **Letter of Undertaking / Schedule of Parties** are always part of the certificate PDF (the last
  pages). There is no separate endpoint for them.
- **Insurance certificate: the client's sample.** It is built from the guidelines' list of
  differences. A real signed sample would confirm the insurer-signed wording.

---

## 11. Change log

| date | change |
|---|---|
| 2026-09-29 | Reinsurance certificate: preview, issue, history, PDF; reference numbers; airline `certificate_code`; policy certificate wording; agreement `contract_parties`. |
| 2026-09-29 | Signatory = the issuing portal user (+ profile at `/certificates/signatory`); company branding and images moved to the database (`/certificates/settings`); DRAFT PDF preview. |
| 2026-09-30 | **Draft → issued.** A certificate is created as a draft, edited (`PATCH`), drawn marked DRAFT, then issued (`/{id}/issue`) and frozen. **Insurance certificate** (`/certificates/insurance`, `signed_by`). Signatory details come with the request; the signature profile and signature upload were removed. The stamp is printed only when uploaded. "Certificate settings" moved to the admin panel. |
| 2026-09-30 | **Certificate wording moved off the policy.** `period_wording`, `geographical_limits` and the five clause fields are company defaults in `/certificates/settings` (admin), overridable per certificate in its body; the policy no longer takes or returns them. `war_exclusion_exception: ""` = no exception. |
