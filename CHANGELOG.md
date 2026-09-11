# Changelog

Generated from [Conventional Commits](https://www.conventionalcommits.org) by
[git-cliff](https://git-cliff.org). Releases older than the earliest entry below
predate this changelog — see `git log` for their history.

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
