# Dispatch platform: architecture notes

The dispatch service assigns parcels to linehaul routes. It is written in Go, runs on ECS
Fargate and reads jobs from an SQS FIFO queue (the dispatch queue).

The label service prints carrier labels. It calls each carrier's API and retries failed
calls with exponential backoff.

Owners: Priya Raman (engineering manager, routing), Sam Okafor (rate service).

Decision (2026-09-14): keep SQS for the dispatch queue this quarter; revisit Kafka in Q1.

Glossary: linehaul = the long-distance leg between two sort centers.
