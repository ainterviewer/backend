# Changelog

Generated from [Conventional Commits](https://www.conventionalcommits.org) by
[git-cliff](https://git-cliff.org). Releases older than the earliest entry below
predate this changelog — see `git log` for their history.

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
