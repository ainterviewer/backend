# Changelog

Generated from [Conventional Commits](https://www.conventionalcommits.org) by
[git-cliff](https://git-cliff.org). Releases older than the earliest entry below
predate this changelog — see `git log` for their history.

## [0.10.6] - 2026-10-22

### Features

- (api) Tell an unsaved guide section apart with section_not_found

## [0.10.5] - 2026-10-22

### Breaking Changes

- (projects) ProjectPublic no longer has a tests field.
- (api) Checks a route makes itself are 400, not 422, which is
left to FastAPI's validation errors. Registration's invite errors are 400
with invite_required/invite_invalid/invite_expired rather than 406, a
duplicate access request is 409 with `detail` rather than `details`, a
keyword query error is a flat body with `position`, and the preview-feature
403 carries code preview_features_required instead of it as the detail.
- (auth) The sentence is `message`, not `detail`, which is
now only ever an error's.
- (api) The schema, and so the client type, is renamed.

### Features

- (api) Answer every error with an ApiError body [**breaking**]
- (auth) Type the auth routes' success bodies [**breaking**]
- (api) Declare response types on routes that returned untyped bodies

### Bug Fixes

- (auth) Keep a single-use invite when registration fails
- (tests) Refuse a shuffled-AI run without a model before starting it
- (api) Stop extra models overwriting the components FastAPI generated

### Performance

- (projects) Stop sending every test setup with each project [**breaking**]

### Internal

- (api) Rename UploadParticipantsResponse to ParticipantUploadResponse [**breaking**]
- (api) Point the error comments at add_error_handlers

## [0.10.4] - 2026-10-22

### Breaking Changes

- (analysis) The query parameter skip is now offset.

### Bug Fixes

- (schemas) Drop ProjectBase's odd config, read the default model per setup
- (analysis) Page the coding page's messages in a fixed order [**breaking**]

### Internal

- (api) Share the report and analysis query models, type from_hit
- (types) Keep app/db/types.py to column types, enums in app/types.py

## [0.10.3] - 2026-10-22

### Breaking Changes

- (schemas) OpenAPI schema names change (operation ids do not), so the
frontend SDK has to be regenerated and the types it names updated:
CodeBase -> CodeInput, CodebookPut -> CodebookReplace, CodingsForMessages ->
CodingLookupRequest, ContentVersionSummary -> ContentVersionSummaryPublic,
CreateInterviewRequest -> InterviewCreate, CreateProjectRequest ->
ProjectCreate, ExternalParamsRequest -> ExternalParamsReplace,
InterviewFilters -> InterviewFilterParams, InterviewResumeLinkCreated ->
InterviewResumeLinkResponse, InterviewResumeRedeemed ->
InterviewResumeResponse, LoginData -> LoginRequest, ParticipantEmailAttachment
-> ParticipantEmailAttachmentPublic, ProjectFolder{Create,Edit,Public,
WithProjects} -> Folder{Create,Update,Public,WithProjectsPublic},
SaveVersionRequest -> ContentVersionCreate, UserAdmin -> UserAdminPublic,
UserCreateRequest -> RegisterRequest.

### Internal

- (models) Replace the UNSET sentinel with pydantic's MISSING, drop dead models
- (schemas) Move the API's pydantic models into app/schemas by domain
- (api) Take query parameters as FastAPI parameter models
- (schemas) Name the API schemas by one convention [**breaking**]
- Collapse short imports the schema move left wrapped

## [0.10.2] - 2026-10-22

### Bug Fixes

- (assistance) Replay every part of a turn, and test the stream offline

### Internal

- (deps) Bump fastapi, sqlalchemy 2.1, polars 2, pydantic-ai 2
- (deps) Bump the ruff pre-commit hook to v0.17.1
- Bump lib dep

## [0.10.1] - 2026-10-22

### Internal

- Pin lib to released v0.7.0

## [0.10.0] - 2026-10-22

### Features

- Add safety-agent handling
- Add message reporting functionality
- Add reported messages notifications
- Add user facing report queues
- Flag safety-check interventions for review
- Record daily user activity and serve admin overview stats
- Flag safety interventions in manual tests for the project only
- Per-user demo limits
- Archive folders and projects
- (explore) Filter the turn unit to main-question answers
- (projects) Encode the link's query params in the project QR code
- (invitations) Readable aliases that replace special registration tokens

### Bug Fixes

- Record activity on role-gated routes and retry failed writes
- Leave last_login empty until a first login, seed activity from last_active
- Start the all-time admin stats on a Monday
- Pass QR output path by keyword, not as the embedded icon

### Internal

- Chain the report migration after the safety-agent ones
- Clean up gitignore
- Chain the user activity migration after the safety-flag ones
- Pin ainterviewer to the security check retry fix

## [0.9.1] - 2026-10-16

### Bug Fixes

- Keep concurrent writes to the content from undoing each other

## [0.9.0] - 2026-10-15

### Features

- A revision per part of the content
- Co-edit the guide over a Yjs document
- Co-edit the consent and welcome pages live
- Refuse stale saves of a test setup

### Bug Fixes

- Match a guide's list entries by content, not position
- Key fixed answers by question id

### Internal

- (deps) Ainterviewer v0.6.1

## [0.8.1] - 2026-10-14

### Features

- Languages in progress

### Bug Fixes

- Never move a model to another provider unasked
- Give the migrated first version no label

## [0.8.0] - 2026-10-14

### Breaking Changes

- The dashboard reads and writes the guide, consent,
welcome and agents at the project level (GET/PUT /projects/{id}/guide,
/consent, /welcome; GET/POST /projects/{id}/agents) in their localized
form. POST /projects/{id}/{lang}/guide, /{lang}/agents and
/guide/consent|welcome/{language} are gone; the welcome video uploads
through POST /projects/{id}/welcome/video; question generation takes a
section_id. POST /projects/{id}/{lang}/translate fills in a language's
missing text.
- Content saves need If-Match; projects must be published
before respondents can start interviews (migration 1a7c2e94b5d3 publishes
every existing project as 1.0.0).

### Features

- One multilingual content row per project [**breaking**]
- Versions of project content, publishing and revision checks [**breaking**]

### Bug Fixes

- Render the probing prompt preview again
- Rename the gpt-oss-120b model to openai/gpt-oss-120b

### Internal

- Move migration tests to the local-only tests/migrations
- Keep the live trigger clock tests apart from their migration test

## [0.7.0] - 2026-10-14

### Breaking Changes

- (guide) Conditions in the guide API carry `question_index` and a
required `question_id` instead of `section` and `question`.

### Features

- (interviews) Bulk delete and export by list filters
- (analysis) Let demo users comment and code
- (interviews) Export as CSV, Excel, Word or PDF, one file or a ZIP
- (guide) Stable ids on guide sections and questions [**breaking**]

### Bug Fixes

- (db) Read datetimes back aware and honour offsets on the way in

### Internal

- (deps) Bump ainterviewer to v0.5.0

## [0.6.7] - 2026-10-13

### Internal

- Seed an OpenAI key so the full router imports in CI

## [0.6.6] - 2026-10-13

### Features

- (releases) Serve preview highlights only to preview users

### Internal

- Route preview and graduation trailers in the changelog

## [0.6.5] - 2026-10-13

### Features

- Improved embedding batching
- (synthesize) Limit demo users' synthetic interviews per user
- (auth) Add password reset via emailed magic link
- (auth) Enforce preview features on the API
- (db) Enforce foreign keys by default
- (codebook) Carry merges in the codebook save
- (embed) Add a live flag to stop embedding during interviews

### Bug Fixes

- (export) Infer the messages dataframe schema from every row
- (migrations) Keep the testrun triggers through the started_by migration
- (migrations) Let alembic own the touch-last_updated triggers
- (participants) Return 404 for opt-out links that match nobody
- (websockets) End live sessions before deleting their interviews
- (interviews) Refuse stale test-run, participant and interview ids
- (interviews) End the session when a write hits a deleted interview
- (synthesize) Scope test-setup routes to the project in their path
- (db) Stamp trigger-bumped last_updated with the app clock
- (migrations) Register app_now on alembic's engine
- (migrations) Stop the orphan sweep deleting children of live parents
- (ws) Run AI interviews in the interview's own language

### Performance

- (db) Index every foreign-key column

### Internal

- Update just backup-db recipe to use sudo when running backup script on remote
- (migrations) Merge the release/0.4.x hotfix migrations into main
- (release) Let only the final-release rule set latest
- (migrations) Name e5f2a91c4d80 as the cause of the lost testrun triggers
- (users) Rename with_demo_features to with_preview_features
- Keep main linear with rebase and fast-forward merges
- (settings) Correct the database env-var prefix and Postgres notes
- (migrations) Delete foreign-key orphans again
- (settings) Regenerate config.schema.json
- Record the per-process session registry and last_updated clocks
- (just) Disable two-factor on fetched database copies
## [0.6.4] - 2026-09-22

### Internal

- Update db justfile recipes to match new ENV structure
- Fix service settings default values
## [0.6.3] - 2026-09-22

### Internal

- Remove old dead translations module
- Publish a staging tag on every release
- Preview a feature branch on staging
- Reenable pytest in release wf and run on pull requests
- Better release version detection in wf
- Bump lib version
## [0.6.2] - 2026-09-17

### Features

- Implement better code exploration integration
- Add similarity search based on coded turns
## [0.6.1] - 2026-09-16

### Features

- Improve duration trimming for interviews by capping answer gaps to 5min
## [0.6.0] - 2026-09-16

### Features

- Switch from old annotations to new codebook analytical framework
## [0.5.8] - 2026-09-14

### Features

- Implement shared interview filtering for report, and add deduplicate_by_pid argument

### Internal

- Move interview filtering to standalone module
## [0.5.7] - 2026-09-14

### Internal

- Bump lib version
## [0.5.6] - 2026-09-14

### Features

- Add condition carrier counts to report
## [0.5.5] - 2026-09-11

### Internal

- Move relabel resumed survey answers from scripts to alemibc migration
## [0.5.4] - 2026-09-11

### Internal

- Bump lib version
## [0.5.3] - 2026-09-11

### Internal

- (scripts) Relabel survey answers stored as free text
## [0.5.2] - 2026-09-11

### Features

- Add groupby controls (interview/guide) and question filtering to all units
## [0.5.1] - 2026-09-10

### Features

- Improve embeddings service
- Add chunks for interview kinds
## [0.5.0] - 2026-09-10

### Features

- Add report endpoint
- Let comments be author specific and implement comment replies
- Add comment authorship and permissions
- (report) Add conditions to report payload
- (monitoring) Change characters to log scale and duration from seconds -> minutes
- Add embedding components
- Implement new and better any-llm exception handling
- Improved embedding chunk turn identification and grouping
- Add umap embeddings and add language information to embedding chunks
- Improve language filtering for embeddings
- Implement pagination for embeddings endpoints
- Add question filtering for data exploration
- Add browsing with keywords
- Improve keyword search match
- Streamline transcript payloads
- Add survey filtering for embeddings
- Add order and filtering to browsing endpoints
- Add sections as embedding kind

### Bug Fixes

- Cascade delete annotations when interviews are deleted
- (build) Commit only release files in publish recipe
- Update a users last_active status on token refresh
- Better timeout handling for embedding client
- Harden language type across api
- Don't cast synthetic messages to ReceivedData before sumbission
- Harden API authorization for annotations

### Internal

- Move histogram utility tools from monitor.py to shared histogram.py module
- Bump ruff pre-commit
- Update dev dependencies
- Filter unnecessary umap warning
- Refine db backup commands
- Bump lib version

## [0.6.4] - 2026-09-22

### Internal

- Update db justfile recipes to match new ENV structure
- Fix service settings default values

## [0.6.3] - 2026-09-22

### Internal

- Remove old dead translations module
- Publish a staging tag on every release
- Preview a feature branch on staging
- Reenable pytest in release wf and run on pull requests
- Better release version detection in wf
- Bump lib version

## [0.6.2] - 2026-09-17

### Features

- Implement better code exploration integration
- Add similarity search based on coded turns

## [0.6.1] - 2026-09-16

### Features

- Improve duration trimming for interviews by capping answer gaps to 5min

## [0.6.0] - 2026-09-16

### Features

- Switch from old annotations to new codebook analytical framework

## [0.5.8] - 2026-09-14

### Features

- Implement shared interview filtering for report, and add deduplicate_by_pid argument

### Internal

- Move interview filtering to standalone module

## [0.5.7] - 2026-09-14

### Internal

- Bump lib version

## [0.5.6] - 2026-09-14

### Features

- Add condition carrier counts to report

## [0.5.5] - 2026-09-11

### Internal

- Move relabel resumed survey answers from scripts to alemibc migration

## [0.5.4] - 2026-09-11

### Internal

- Bump lib version

## [0.5.3] - 2026-09-11

### Internal

- (scripts) Relabel survey answers stored as free text

## [0.5.2] - 2026-09-11

### Features

- Add groupby controls (interview/guide) and question filtering to all units

## [0.5.1] - 2026-09-10

### Features

- Improve embeddings service
- Add chunks for interview kinds

## [0.5.0] - 2026-09-10

### Features

- Add report endpoint
- Let comments be author specific and implement comment replies
- Add comment authorship and permissions
- (report) Add conditions to report payload
- (monitoring) Change characters to log scale and duration from seconds -> minutes
- Add embedding components
- Implement new and better any-llm exception handling
- Improved embedding chunk turn identification and grouping
- Add umap embeddings and add language information to embedding chunks
- Improve language filtering for embeddings
- Implement pagination for embeddings endpoints
- Add question filtering for data exploration
- Add browsing with keywords
- Improve keyword search match
- Streamline transcript payloads
- Add survey filtering for embeddings
- Add order and filtering to browsing endpoints
- Add sections as embedding kind

### Bug Fixes

- Cascade delete annotations when interviews are deleted
- (build) Commit only release files in publish recipe
- Update a users last_active status on token refresh
- Better timeout handling for embedding client
- Harden language type across api
- Don't cast synthetic messages to ReceivedData before sumbission
- Harden API authorization for annotations

### Internal

- Move histogram utility tools from monitor.py to shared histogram.py module
- Bump ruff pre-commit
- Update dev dependencies
- Filter unnecessary umap warning
- Refine db backup commands
- Bump lib version

## [0.4.36] - 2026-08-27

### Features

- Add participant deduplication to monitor stats
- Add Participation Rate and Language Distribution to monitoring payload

### Internal

- (release) Add tag guard to docker release step
- Move tts/stt components into audio services

## [0.4.35] - 2026-08-26

### Internal

- Fix broken url in readme

## [0.4.34] - 2026-08-26

### Features

- (email) Implement template substitution in email subject line
- Add pid to InterviewSummaryPublic
- Add resumeable respondent links
- Improve dropout monitoring with full section/question range and guide content

### Bug Fixes

- Harden interview auth and remove unused and redundant guest scope

## [0.4.33] - 2026-08-26

### Features

- Better email template validation

## [0.4.32] - 2026-08-25

### Features

- (interviews) Filter, search and facet the interview list server-side

### Internal

- Add citation information

## [0.4.31] - 2026-08-21

### Features

- Update InterviewTable.last_updated on message insert

### Bug Fixes

- Sort interviews based on completed status correctly
- Sorting by last_updated on InterviewTable now correctly falls back to created_at when it is not set
- Auth session cookie for interviews now survives browser close. And an invalid/outdated auth now also displays a proper message before restarting.
- Better error handling for interview loop
- Move sqlite pragmas to engine
- When deleting an interview, also delete messages, tasks, interviewees
- Add cascade on testrun and experiment_project foreign keys
- Invitation.access_request_id on delete set null and remove cascade="all, delete-orphan"

### Internal

- (interview) Remove dead create argument for get_interview
- Remove outdated comment
- Bump lib version
- Improve alembic env with logger and better target url handling
- Remove orphaned table rows
- Move message loadin for interviews to improve speed

## [0.4.30] - 2026-08-20

### Bug Fixes

- (security) Harden file upload and file serving

## [0.4.29] - 2026-08-20

### Bug Fixes

- Add project storage helpers to manage storage such as email attachments and project deletion

## [0.4.28] - 2026-08-20

### Bug Fixes

- Interview creation external params should only be validated for DISTRIBUTED interviews

## [0.4.27] - 2026-08-20

### Features

- Add new validate_interview_params public api endpoint so we can gate the interview before any content is shown to the user

## [0.4.26] - 2026-08-20

### Internal

- Bump lib

## [0.4.25] - 2026-08-19

### Internal

- Bump lib

## [0.4.24] - 2026-08-19

### Bug Fixes

- Strip blank particinapts from uploads

## [0.4.23] - 2026-08-19

### Internal

- Remove unused analysis dependencies

## [0.4.22] - 2026-08-19

### Internal

- Update dependencies

## [0.4.21] - 2026-08-19

### Bug Fixes

- Improved prompt version overwriting based on lib content
- (api) Return 404 instead of 500 for a missing localization

### Internal

- Move project default language from config to project localization table column
- Update CLAUDE.md

## [0.4.20] - 2026-08-14

### Internal

- Bump lib version

## [0.4.19] - 2026-08-14

### Internal

- Bump lib version

## [0.4.18] - 2026-08-13

### Features

- (monitoring) Histogram ends now follow ranges that read as "round"

## [0.4.17] - 2026-08-13

### Features

- (db-optimization) Optimized db tables and calls to improve loading speed for heavy monitoring reads.

### Internal

- Add seed-release just command

## [0.4.16] - 2026-08-13

### Features

- (export) Exporting an interview now also exports external params

## [0.4.15] - 2026-08-12

### Features

- Add manifest release highlight

## [0.4.14] - 2026-08-12

### Features

- Update manifest to hold release notes and change range

## [0.4.13] - 2026-08-12

### Features

- (projects) Let a project be moved from one folder to another

### Bug Fixes

- (lint) Change models type from list to Sequence

### Internal

- (api) Use the custom patched openapi.json file as the one served by the api
- Update openapi.json file
- (justfile) Remove static openapi spec command and output file
- (justfile) Lint
- Implement cliff and release note strategy
