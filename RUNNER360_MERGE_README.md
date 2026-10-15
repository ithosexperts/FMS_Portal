# Runner360 merge into current FMS Portal

This build uses the newer FMS Portal as its base and adds the Runner360-specific implementation from the uploaded Runner360 source. Newer One Portal SSO and shared Comments settings/templates are retained.

## Runner360 features included
- Team Lead Input Form with branch/runner data, busy-runner blocking, expected-return calculation, VM_HolidaysMaster holidays, Sundays, and past-date rules.
- Z_Runner360 task insertion using live API table metadata and the insert-success response fix.
- Stage S1 Runner Return form, Check In/IN GPS and branch checks, task revoke, and manager-only planned timing edit.
- `BusyTimeMins` storage, daily/weekly Runner360 dashboard, runner/year/week filters, fixed 0–480 chart scale, and Extra Busy Time column in workflow.
- CEOITBOX WhatsApp notification service configured from environment variables.
- Runner hierarchy and busy status from employee/team/KPI data.
- Runner360-aware Stage 1 visibility (does not require PickID for Runner360).
- Correct table/app/primary-key context passed into stage forms and task details to prevent opening another FMS form where IDs overlap.
- Runner360 Lead Time Extension button carries the shared `lead-time-extension-btn` class so the existing Comments embed can place the Comments icon next to it.

## Apply
- This archive is a complete merged project tree under `FMS_Portal-main/`. The archive intentionally omits `.env`; copy your existing authorized local `.env` into the extracted project root, or set the environment values in your host.
- Do not commit `.env`. `.gitignore` is included, but it does not untrack a file already tracked by Git. If any real password/token was exposed in a URL/log or source control, rotate it.
- The target's shared SSO, comments base template/config, and SSO router were retained.

## Validation status
Python compilation, template rendering, import/route checks, Stage 1 visibility tests, and sample Busy/Extra Busy Time calculations are run before delivery. Live SQL/API, authentication, GPS location, Comments embed, and WhatsApp delivery must be validated in the actual environment. The mobile WhatsApp Check In page is not included as completed functionality; current message text still marks that link as future deployment work.
