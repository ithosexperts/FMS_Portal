# FMS Portal — Dummy’s Guide to Onboarding a New FMS

This document is a simple onboarding guide for anyone who wants to bring a new FMS into the portal without needing deep code knowledge.

It is written from the practical perspective of the current portal structure in this project.

---

## 1. What this portal is doing

The FMS Portal is a FastAPI application that:

- reads task data from a backend table / API layer
- resolves which FMS a row belongs to
- calculates the current stage from planned vs actual values
- renders the stage form and application form
- saves updates to the table
- optionally calls stored procedures or linked procedures
- uses One Portal / JWT identity and employee/role metadata for access control

The real system is mostly metadata-driven. That means:

- the actual form behavior depends on config rows
- stage behavior depends on FMS_Config
- task visibility depends on application metadata
- API calls are routed through the shared API layer rather than direct SQL access

---

## 2. Key concept: an FMS is not just a form

An FMS is a complete workflow with:

- a table that stores task data
- an application id
- multiple stages
- rules for when each stage is editable
- read-only summary data from earlier stages
- stored procedure execution rules
- dashboard visibility mapping

So onboarding an FMS is not only “make a UI”. It means wiring the complete lifecycle.

---

## 3. Minimum checklist before onboarding

Before starting, make sure these things exist:

1. A table for the application data
   - example: Z_SalesOrder, Z_Lead_Time_Grant, etc.
2. A unique App ID
   - used to identify the FMS
3. FMS metadata entry
   - app name
   - table name
   - app ID
4. Stage configuration rows in FMS_Config
   - stage number
   - stage name
   - display order
   - planned column
   - actual column
   - linked stored procedure if any
5. Permissions / role mapping
   - role id or employee code must allow edit on current stage
6. A dashboard or task representation
   - so the task appears on the portal

---

## 4. Where the portal reads FMS configuration

The portal architecture is driven by a few main places:

### App entrypoint
- [app/main.py](app/main.py)

This file boots the app, mounts static files, and registers routers.

### App settings
- [app/config.py](app/config.py)

This file contains environment values like:

- API URLs
- auth token settings
- One Portal JWT config
- app name and environment

### Authentication layer
- [app/middleware/oneportal_auth.py](app/middleware/oneportal_auth.py)

This middleware verifies JWT sessions, loads employee data, checks role access, and populates request context.

### Task and stage resolution
- [app/services/task_service.py](app/services/task_service.py)
- [app/utils/stage_calculator.py](app/utils/stage_calculator.py)

These files determine:

- which table a task belongs to
- what stages exist for the FMS
- which stage is the current one
- whether a task is completed

---

## 5. The actual FMS onboarding flow

### Step 1: Create the backing table

The FMS should have a table that stores the task row.

For example:

- a sales order table
- a dispatch table
- a packing table
- or any custom task table

This table should have:

- a primary key / row id
- task-level information
- stage fields for planned/actual values
- enough columns to render form inputs

If the table does not have a clear primary key, the portal cannot route updates correctly.

### Step 2: Register the FMS metadata

The app expects application metadata to be available from the shared API / metadata setup.

At minimum, define:

- app id
- fms name
- table name
- maybe app label / display configuration

This is how the dashboard knows that a row belongs to this FMS.

### Step 3: Add FMS_Config rows for stages

This is the most important onboarding step.

Each stage row should define values like:

- STAGE_NO
- STAGE_NAME
- DISPLAY_ORDER
- PLANNED_COLUMN
- ACTUAL_COLUMN
- maybe DOER_EMP_CODE
- maybe Linked_Stored_Procedure
- maybe stage-specific metadata

Example stage shapes:

- S1: Pack to Dispatch Details
- S2: Dispatch details / vehicle / loading stage
- S3: final verification / closeout

The portal relies on these rows to decide:

- current stage
- which fields are planned vs actual
- which fields are editable
- which stored procedure should run after save

### Step 4: Make sure the planned/actual fields are mapped correctly

The stage rules in the portal are based on the formula:

- planned is not blank and actual is blank => current stage

That means a stage row must have correct field mappings.

For example:

- S1_PLTimeStamp => planned field
- S1_ALTimeStamp => actual field
- S2_PBaseTS => planned value
- S2_ALTimeStamp => actual value

If mapping is wrong, the portal will think the wrong stage is active or the task never progresses.

### Step 5: Choose the correct linked stored procedure

This is a core rule for downstream FMS flows.

When a stage is submitted, the system may call:

- a timestamp procedure
- a linked procedure for the next stage
- a business rule procedure for status or calculations

The linked procedure should be configured in the stage row or FMS metadata, not hardcoded in Python for a single case.

The system expects something like:

- stage 1 run `SP_FMS_ALTimeStampStatus`
- then after delay, run the configured linked proc for the next stage
- example: `SP_FMS_Pack2Dispatch_S2`

Important: the procedure input contract matters.

The real requirement is to pass the correct value for the sales-order entry, not just the sales order number. In the current project, the linked proc expects the sales-order entry value like `I_DocEntry` / `DocEntry`, not DocNum.

---

## 6. Form behavior: what gets rendered

The portal has two main form layers:

### Application form
- read-only form summary
- used to view task-level info

### Stage form
- editable based on current stage
- uses stage config plus row values

The form page logic lives in:

- [app/routers/forms.py](app/routers/forms.py)

It contains the flow for:

- page load
- detect custom form template
- build stage context
- save/submit form data
- update rows
- call stored procedures
- clear/reset the form

---

## 7. Stage-specific custom forms

The portal supports custom form templates for heavy or business-specific stages.

Example in this project:

- [app/templates/forms/stage_custom_s1.html](app/templates/forms/stage_custom_s1.html)

This is the Stage 1 custom page for an FMS like Pack to Dispatch Details.

For a new FMS, you typically create:

- a custom template for that stage
- custom data context for read-only summary values
- editable fields for the stage-specific questions
- append behavior for batch rows if needed

The custom template should do three things:

1. show inherited data from previous stage / top row
2. show current stage editable fields
3. submit back to the same stage handler

---

## 8. How append / batch rows work

For some FMS flows, one task can involve multiple child rows or appended batch rows.

The pattern in this project is:

- read the main row
- find related rows by sales order + create timestamp
- append or update child records in another table (like batch details)
- save the main form data
- run the relevant SP for all affected batch rows
- clear/reset related rows when needed

For onboarding, if a new FMS needs batch append logic, the implementation should be designed around:

- same sales order no
- same create timestamp
- same doc entry / master row context
- one group of rows treated as one submission batch

This is how the app avoids updating only the current row while ignoring related rows.

---

## 9. Clear & reset behavior

The clear action is not just “erase fields on the page”. It must also reset:

- form-level data
- related appended rows
- stage runtime values
- linked-procedure input values
- stage-specific result fields like delay / task status / helper values

However, some values should not be cleared, especially the planning/input values.

In this project, Stage 1 preserves fields such as:

- S1_PLTimeStamp
- S1_KPIApplicable

because they are planning / user input values, not generated runtime values.

This distinction is important while onboarding new FMS stages.

---

## 10. Typical onboarding example

Let us assume a new FMS is called “Dispatch Flow”.

### Example flow

1. Create table: Z_DispatchFlow
2. Register FMS metadata with App ID `DISPATCHFLOW`
3. Add stage config:
   - S1: Create Dispatch Plan
   - S2: Vehicle and Loading Details
   - S3: Final Dispatch Confirmation
4. Map fields:
   - `S1_PLTimeStamp` planned timestamp
   - `S1_ALTimeStamp` actual timestamp
   - `S2_Vehicle_Origin` new field
   - `S2_Vehicle_Type` new field
   - `S2_Loading_Image1` attachment field
5. Set linked procedure for S2 if needed
6. Build custom template for S2 with read-only Stage 1 summary and editable S2 fields
7. Add role / employee access for stage editing
8. Run test case and verify:
   - task appears in dashboard
   - stage order is correct
   - current stage resolves properly
   - save works
   - procedure execution works
   - clear/reset works without deleting planned fields

---

## 11. Best practice for any new FMS

When onboarding a new FMS, follow this order:

### A. Data model
- create table
- ensure primary key exists
- ensure stage field columns exist

### B. Metadata config
- register app
- assign table name
- configure stage rows

### C. Stage logic
- map planned / actual columns
- define display order
- set linked procedures

### D. Form UX
- create custom template if needed
- show parent-stage summary at top
- keep generic questions same
- add only new stage editing fields

### E. Validation
- verify save
- verify current stage changes
- verify SP execution
- verify linked procedure call
- verify clear/reset rules
- verify attachments and multi-image fields

---

## 12. Critical rules to remember

1. Do not hardcode a single table or single stage for all FMS
2. Always use metadata-driven configuration
3. Use `FMS_Config` as the source of truth for stage flow
4. Linked procedure should come from the correct downstream stage, not the current stage
5. Always pass the correct value required by SQL, not the first convenient value
6. Keep planning fields and generated runtime fields separate
7. Reset logic must clear runtime-generated values, not user-planned values
8. Validate the form with real data and actual SQL callback behavior

---

## 13. Quick test checklist for a new FMS

After onboarding, test this sequence:

- [ ] Task appears in dashboard
- [ ] Stage page opens correctly
- [ ] Current stage is correct
- [ ] Save works
- [ ] Submit works
- [ ] Stored procedure is triggered
- [ ] Linked stored procedure is triggered for the correct downstream stage
- [ ] Runtime values update
- [ ] Child / appended rows update correctly
- [ ] Clear & Reset clears generated values only
- [ ] Planned values remain intact
- [ ] Attachments upload and display correctly
- [ ] Role permissions restrict editing as expected

---

## 14. Final note

An FMS is not just a screen. It is a business workflow.

To onboard a new FMS successfully, the developer must connect:

- database table
- metadata config
- stage mapping
- custom form UI
- SQL / procedure contract
- role-based access
- clear/reset semantics

If those are aligned, the portal behaves like a real FMS orchestrator instead of a static form app.

---

## 15. Recommended next step

For the next FMS, create a small matrix like this:

| Item | Example | Notes |
| --- | --- | --- |
| App ID | DISPATCHFLOW | Unique ID |
| Table Name | Z_DispatchFlow | Storage table |
| Stage 1 | S1 | Planning stage |
| Stage 2 | S2 | Vehicle / Loading stage |
| Stage 3 | S3 | Final dispatch |
| Linked SP | SP_FMS_Pack2Dispatch_S2 | Stage 2 downstream process |
| Protected values | Planned timestamp, KPI input | Keep on clear |
| Runtime values | Delay, Task Status, AL helper | Clear on reset |

This gives the entire team a simple starting checklist.

---

## 16. Summary

If someone reads this document and follows the checklist, they should be able to onboard a new FMS into this portal in a predictable, structured way.

The portal is designed to be metadata-driven, so the key is always to keep the data contract, stage configuration, and custom form design aligned.
