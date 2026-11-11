# Documentation

## Guides

| Document | Purpose |
| --- | --- |
| [Project README](../README.md) | Overview, requirements and quick start |
| [User guide](user-guide.md) | Full application behavior, providers, configuration, storage and source provenance |
| [Workspaces](workspaces.md) | Directory access, references, and document output |
| [Citation rigor](citation-rigor.md) | Optional claim auditing, exact saved supporting passages, modes and evaluation |
| [Persistent memory](memory.md) | Optional cross-conversation memory in a self-hosted Honcho |
| [Skill scripts](skill-scripts.md) | Separate execution review, optional Linux runtime, confinement, inputs and report artifacts |
| [Interactive answers](interactive-ui.md) | Opt-in semantic UI, feature flags, protocol, ownership and validation |
| [Mermaid diagrams](diagrams.md) | Local diagram rendering, viewing controls, model overhead, limits and verification |
| [Animations](animations.md) | Authoring availability, validation, and saved-animation compatibility |
| [Motion Canvas](motion-canvas.md) | Current model authoring, playback, setup, and verification |
| [Sharing a llama-router](shared-router.md) | Running the router as an independent service, persistent credentials, idle sleep and client contention |
| [Backup and recovery](recovery.md) | Complete stopped-application snapshot, isolated restoration and synthetic drill |
| [Troubleshooting](troubleshooting.md) | Bounded diagnostics, service/runtime failures, safe cache cleanup and GPU recovery |
| [Artifact cleanup](artifact-cleanup.md) | Retryable conversation deletion, read-only reconciliation and explicit orphan repair |
| [Distribution](distribution.md) | Release builds, wheel resource checks, and installed animation/skill setup |
| [Licenses and provenance](../THIRD_PARTY_NOTICES.md) | MIT project license, asset origins, and third-party notices |

## For contributors

| Document | Purpose |
| --- | --- |
| [Contributing](../CONTRIBUTING.md) | Repository layout, development setup and checks |
| [Architecture](architecture.md) | Runtime ownership, invariants, and verification boundaries |
| [Engineering standards](engineering-standards.md) | Ratified review baseline: established conventions, standards for changed code and proposed directions |
| [Domain glossary](../CONTEXT.md) | Shared meanings of conversation, turn, evidence, workspace, and other terms |
| [State machines](state-machines/README.md) | Derived states, transition tables, invariants and recorded hazards for the implicit machines across the runtime |
| [Desktop ownership ADR](adr/0001-desktop-ownership.md) | One desktop owner per chat database |
| [Artifact cleanup ADR](adr/0002-artifact-cleanup.md) | Atomic cleanup obligations and controller lifecycle ownership |
| [Interactive-answer ADR](adr/0003-interactive-answer-ownership.md) | Typed contracts, durable acceptance/cancellation and compatible replay |
| [Skill execution ADR](adr/0004-skill-script-execution.md) | Immutable reviewed packages, native permission and subprocess supervision |
| [Dependency and CI maintenance](dependency-maintenance.md) | Manual monthly review, advisory disposition, action pins and trial-update checks |
| [Design language](../GOTHIC_BLOOM_DESIGN_LANGUAGE.md) | Canonical Gothic Bloom design reference |
| [Design studies](design-previews/README.md) | Three static design directions and review screenshots |
| [Scripts](../scripts/README.md) | Every development check, probe, measurement and tool, by what running it costs |

## Preserved reviews and evidence

Dated records of what was found and verified at the time. They are evidence, not
current guarantees; the guides above and the tests describe the code as it is.

| Document | Purpose |
| --- | --- |
| [Stabilization](stabilization.md) | Reliability findings and verification from 2026-10-15 |

Keep raw exploratory output in `/tmp` or ignored `artifacts/`. Add new guides to
this index and link them from the relevant feature documentation; dated
investigations go under preserved reviews.
