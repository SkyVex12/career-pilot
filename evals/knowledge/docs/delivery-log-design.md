# Delivery log report: design

## 1. What it is

A report source for the builder that lists delivery scans: who scanned which parcel, when,
and what the driver entered. It replaces the fixed "Scan history" page.

## 2. Current state

### 2.1 Where scans are stored

Scans live in three places:

| Store | Grain | Can it be queried by field? |
| --- | --- | --- |
| The event stream | one entry per scan event | No: entered values are not indexed there |
| Object store files | one file per scan | No |
| The delivery-record index | one document per scan and parcel | Yes |

### 2.2 The delivery-record index

The index is named `fh-{region}_{account}_delivery_record`. Each document holds the values the
driver entered, frozen at scan time, the parcel id, the depot, `scanEventId_text` and the scan
time. Documents stay after a parcel is deleted.

### 2.3 When rows share a scan id

A scan of one parcel writes one document. A multi-parcel scan (a driver scans a pallet of 12)
writes 12 documents, and each gets its own `scanEventId_text`: there is no id for the pallet
scan as a whole. The entered values repeat on every row.

### 2.4 Known data defects

- An edit scan records the time it was processed, not the time the driver scanned. For edit
  scans the scan time can be minutes late.
- Columns that come from a pallet table inside the scan form are written under the pallet's
  own field ids. The report's field list reads the form's top-level fields only, so pallet
  table columns do not appear as report fields yet.

## 3. Decisions

### D1. Read from the delivery-record index

Chosen because its documents are frozen, can be queried by field and survive deletion.
Rejected: the event stream (entered values are not indexed there) and the object store files
(they cannot be queried at all). The cost: the report can only show what the indexer wrote.

### D2. A Scan id column, on purpose

The report shows a Scan id column backed by `scanEventId_text`, the id the old page already
used. It was added on purpose so a user can filter, sort and group by one scan. Using the
existing field means old rows work with no migration and no backfill. Rejected: a new
pallet-level id, because it would need a backfill of every historical document. Tradeoff:
rows of one pallet scan keep separate ids.

### D3. Current fields are display-only

Columns that show the parcel's current values (its depot today, its status today) can be
shown as columns but cannot be used to filter, sort, group or aggregate. Their live values
are in another index, and the join happens per page after the query. Rejected: copying
current values into the delivery-record index, because every parcel edit would rewrite all
of its scan documents.

### D4. The proof-of-delivery link column

A "Proof link" column is offered when the depot has ever had proof-of-delivery files turned
on (`hasEverHadPod`), not only while it is on now, because old scans may still have a file.
For a depot that turned it off, the column is listed and its cells are blank.

## 4. Edge cases

| Case | Behaviour |
| --- | --- |
| The parcel was deleted | The row stays; current fields say "Parcel no longer available" |
| A reader cannot see the parcel | The row stays; current fields are blank |
| First scan per parcel only | Not built. The data supports it: group by parcel, take the earliest scan time. It needs a new query step, and edit scans carry processing time (2.4) |

## 5. Open questions for product

- Should a pallet scan count as one scan or twelve in a chart?
