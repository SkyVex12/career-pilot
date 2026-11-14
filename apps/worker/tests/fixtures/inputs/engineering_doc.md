# Dispatch platform: architecture notes

## Components

The dispatch service assigns parcels to linehaul routes. It is written in Go, runs on ECS
Fargate and reads jobs from an SQS FIFO queue (the dispatch queue). It asks the rate service
for a price before it assigns a route.

The label service prints carrier labels. It calls each carrier's API and retries failed
calls with exponential backoff, at most 5 times.

Owners: Priya Raman (engineering manager, routing), Sam Okafor (rate service).

## How a parcel is dispatched

1. The intake API writes one job per parcel to the dispatch queue, grouped by route.
2. The dispatch service reads the job, asks the rate service for a price and picks the
   cheapest linehaul route with room.
3. It writes the assignment to the routes table and asks the label service for a label.

```mermaid
graph LR
  intake[Intake API] --> queue[(Dispatch queue)]
  # this line is a comment in the diagram, not a heading
  queue --> dispatch[Dispatch service]
  dispatch --> rate[Rate service]
  dispatch --> label[Label service]
```

## Decisions

### Keep SQS for the dispatch queue (2026-09-14)

We keep SQS FIFO for the dispatch queue this quarter and revisit in Q1. Kafka was rejected:
nobody on the team runs it, and ordering per route is all we need. The cost we accept is
the FIFO limit of 300 messages a second per route group.

## Limits

- A route takes at most 120 parcels; the 121st goes to the next route.

## Open risks

- The rate service has no fallback price: when it is down, dispatch stops assigning.

## Glossary

linehaul = the long-distance leg between two sort centers.
