# Roadmap

Career Pilot grows in stages and build phases. The full plan and its reasons are in
[ARCHITECTURE.md](ARCHITECTURE.md) ("Rollout stages" and "Build phases").

## Done (Stage 0, October 2026)

- [x] Live answers: desktop capture with echo cancellation and VAD, the WebSocket gateway,
      streaming speech recognition, turn detection, the question detector, fast and deep answers
- [x] Career memory: resume extraction, the hot digest, session packs per interview, fact approval
- [x] Work mode: company workspaces, standup and meeting packs, commitments carried between calls
- [x] Post-call outputs: summary, feedback, transcript (only with consent), session history
- [x] Career-OS link: link an account and pull job applications and tailored resumes on demand
- [x] Stage 0 on Google Cloud: one VM, Terraform, deploy on merge, nightly backups
- [x] Windows installer with automatic updates, released on merge
- [x] Evals: call replay, answer quality, latency benchmark

## Next (finishing Stage 0)

- [ ] First words on screen 1 to 1.5 s after a question, on every turn (the latency benchmark
      tracks it; slow first tokens from the model are the main gap)
- [ ] Fewer missed turns in standups with one remote speaker
- [ ] Speech recognition for calls not held in English
- [ ] Record and label 50 calls, with everyone's consent, as the eval corpus
- [ ] TLS and an IP allow-list on the Stage 0 server
- [ ] Code signing for the Windows installer

## Later

- [ ] **Stage 1, beta (up to about 100 users):** load balancer with TLS, managed Postgres,
      Valkey and queues, a separate staging environment, blue/green deploys for the gateway
- [ ] A local question classifier trained on the Stage 0 calls, in front of the model detector
- [ ] GitHub, Jira, Slack and calendar sync into each company's memory
- [ ] Hardening: retention controls, a local-first option, SOC 2 preparation
- [ ] Billing and plans
- [ ] **Stage 2, production (1,000 users and more)**

## Open questions

- [ ] Claude through Bedrock or the direct Anthropic API?
- [ ] Speech recognition provider and fallback: Deepgram or AssemblyAI?
- [ ] Turn-detection model, and whether it runs on the client or the gateway
- [ ] Embeddings provider, decided by the eval set
- [ ] Pricing (per-minute credits or plan tiers) and payment provider
