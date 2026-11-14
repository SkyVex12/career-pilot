# Report code and the shared libraries folder

## 1. The rule

Code that one deployable needs stays in that deployable's folder. It moves to the shared
libraries folder only when a second deployable needs it, because functions cannot import each
other. Anything in a shared library can end up in the package of every function that imports
the library, so a library must depend only on what all its users need.

## 2. The three report libraries

| Library | What it holds | Heavy dependencies |
| --- | --- | --- |
| report-store | Saved definitions, schedules, the export job records | The table and queue clients |
| report-engine | Runs a report: grouping, subtotals, measures | The domain libraries |
| report-render | Draws the file: PDF, spreadsheet, CSV | pdfsmith (PDF) and sheetwriter (spreadsheets) |

## 3. Who imports what

- report-api (the screen's API) imports report-store and report-engine.
- report-delivery (scheduled sends and downloads) imports all three.
- report-export-worker, the function added in October, renders one part of a large export. It
  imports report-render and report-engine.
- automations imports report-store only, to check that an automation may send a report.

Only report-delivery and report-export-worker import report-render, so only those two
functions carry pdfsmith.

## 4. Why three libraries and not one

They have different dependencies. One library would put pdfsmith into the package of every
function that only needs a saved definition, automations among them. Three libraries let each
function take only what it uses. The cost is three places to look and a fixed direction of
imports: store, then engine, then render.

## 5. Likely questions

- "Does report code in the shared folder make every function bigger?" No. A function gets a
  library only if it imports it. The report libraries are already module-scoped: 4 functions
  import any of them.
- "Is there a library just for reports?" Yes, three of them (section 2).
- "Which function was added recently?" report-export-worker.
