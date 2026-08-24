# EchoMuse Design Documents

This directory contains engineering plans, architecture proposals, technical
specifications, and implementation designs. User-facing instructions remain in
the surrounding `docs/` directory.

## Status

Design documents may use these status values:

- **Proposed** — under discussion; implementation is not committed.
- **Approved** — the design is settled and ready to implement.
- **In progress** — implementation is underway.
- **Implemented** — the design has shipped; the document remains as a reference.
- **Superseded** — replaced by a newer design; links should identify the replacement.

`JOURNAL.md` remains the chronological engineering record. `SETUP.md` remains
the operational architecture and hardware-troubleshooting reference.

## Controller And Voice

- [Controller specification](controller-spec.md) — superseded historical record
- [Full-duplex voice plan](full-duplex-plan.md) — implemented with changes
- [Sendspin implementation plan](sendspin-plan.md) — implemented with changes
- [Music Assistant Sendspin design](music-assistant-sendspin-design.md) — implemented with changes

## Wake-Word Training

- [Wake capture, labeling, and forge retraining](2026-08-23-wake-capture-labeling-design.md) — implemented with changes
- [oww_forge ROCm features](oww-forge-rocm-features.md) — implemented with changes
- [Mandatory stop word](stopword.md) — proposed

## Testing And Quality

- [70% test coverage plan](2026-08-23-test-coverage-70pct-plan.md) — in progress

## Project Direction

- [Project roadmap](../../ROADMAP.md)
- [Main into new_impl integration plan](main-into-new-impl-integration-plan.md) — approved

## Related References

- [User documentation](../README.md)
- [Architecture and hardware reference](../../SETUP.md)
- [Engineering journal](../../JOURNAL.md)
- [Developer orientation](../../CLAUDE.md)
