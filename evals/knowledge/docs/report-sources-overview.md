# Report sources: technical overview

## 1. Status

The delivery log source is on the test environment. The audit trail source is built on the
delivery log branch, so the delivery log is released first.

## 2. How it is built

### 2.1 The design both sources share

A source is a resolver in report-engine with four calls: list fields, list rows, aggregate,
and list a field's values. The builder and the exports only talk to those four calls.

### 2.2 The audit trail source

- **The index.** It reads the `event-stream` alias: one index for every account, rolled over
  each month. Entries are deleted after 400 days.
- **Rows.** One row per changed field: the field, the old value and the new value, plus who
  changed it and when.
- **Old entries.** Entries written before 1 September hold one line of summary text. The
  resolver parses that line to show field, old and new, but those rows cannot be filtered by
  field.
- **Paging.** A page counts entries, not rows, because one entry can change several fields.
  The response carries `nextOffset`, where the next page starts.

### 2.3 The current-values join

Both sources can show the parcel's current values next to the logged ones. The join reads the
parcels of one page in one batch. It stops at 50,000 parcels per report run
(`MAX_JOIN_PARCELS`); past that the current columns are blank and the report says so.

## 3. Limits

- An audit trail export has at most 100,000 rows, and a person can run one export at a time.
- Filtering by old or new value works only for entries written since 1 September.

## 4. Risks

- The `event-stream` index is shared by every account. A slow audit trail query on a large
  account competes with every other account's writes.
