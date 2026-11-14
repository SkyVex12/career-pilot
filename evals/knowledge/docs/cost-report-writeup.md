# Carrier cost report: work write-up

Written by Alex for the reports team. Everything here is about the Fernhill report builder.

## 1. Summary

The report builder can now show what each carrier cost over a period. A report on a costed
lane has an "Include costs" switch with a start date and an end date. When the report runs,
the search cluster calculates the cost columns for every matching row.

## 2. How it works end to end

1. The builder saves the switch, the two dates and the chosen columns in the report definition.
2. On a run, report-engine sends one query to the search cluster with a cost script.
3. The script reads each row's rate history from the row's own document and calculates the
   period values, so sorting and totals cover every row without fetching them.

## 3. Calculation rules

- **Period cost** is the sum of the daily amounts from the start date to the end date. It is
  a sum, never an average.
- **Running cost** adds every day from the lane's first day up to the day before the end date.
- A lane that started after the end date shows a blank, not a zero.
- A mid-period rate change is stored on the row as an adjustment and applied from its own day.

## 4. Builder rules

- Cost reports are tables only. Charts are switched off while "Include costs" is on.
- **Filters: locked and skipped.** The calculated columns (Period cost, Running cost) are
  removed from the filter picker. A saved filter on one of them shows as locked, and the
  server skips it while it calculates, because the filter would match stored values that the
  report no longer shows.
- Filtering on a calculated column would mean calculating before filtering and before
  paging. That is possible but is a second query plan, and it was left out of scope.
- A calculated column is hidden when one of its inputs is hidden for the reader's role.

## 5. Rollout order

Deploy the index mapping first, then the functions, then run the adjustments backfill, and
ship the web client last. If the client goes first, rows with a rate change show the old cost.

## 6. Known gaps

- The cost script exists twice: once in TypeScript for tests and once in the cluster's script
  language. A change to one must be made in the other by hand.
- Old cost reports made in the legacy screen are hidden, not migrated. Migrating them is
  ticket FH-540 and has not started.

## 7. Likely questions

- "Is the cost an average?" No: Period cost is the sum of the days in range.
- "Why not calculate in a function?" The function would have to fetch every row to sort or
  total them; the cluster does it in one query.
