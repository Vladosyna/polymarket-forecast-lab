# Ledger commitment incidents

`docs/ledger_commitments.jsonl` is append-only and is never edited or pruned —
including when a record in it turns out to be wrong. Editing that file to make
verification pass would defeat the only thing it is for. So when something
about the record needs explaining, the explanation is appended here instead,
dated, and written so it can be **checked** rather than taken on trust.

Run the verification yourself:

```bash
uv run lab verify-ledger
```

It exits non-zero only if some date has no valid commitment at all — the
condition that would actually indicate an edited ledger.

**Run it against the ledger of record.** Commitments are hashes over rows in
one specific database, so verification is only meaningful against that
database — currently the VPS's `data/lab.db`, mirrored to the private results
repo and the published replication export. Against a divergent copy the output
inverts and is badly misleading: run on the retired laptop database on
2026-07-28, the same command reported 7 of 21 dates verified and flagged the
*VPS's* records as the superseded ones. Nothing was wrong with either the
ledger or the tool; it was answering a question about the wrong database.
Expected output on the ledger of record is `verified` equal to `dates`.

---

## 2026-07-10 → 2026-07-14: duplicate records from a two-host cutover

**Status: explained, no data affected. Every date has a valid commitment.**

### What a verifier sees

Three of the 24 records in the file fail hash verification:

| date | first_id..last_id | row_count | verifies |
|---|---|---|---|
| 2026-07-10 | 89879..95062 | 5184 | yes |
| 2026-07-10 | 89879..93450 | 3572 | **no** |
| 2026-07-11 | 95063..102958 | 7896 | yes |
| 2026-07-11 | 93451..109587 | 16137 | **no** |
| 2026-07-14 | 130206..149111 | 18906 | yes |
| 2026-07-14 | 121808..137931 | 16124 | **no** |

Each of those three dates carries **two** records with different, partly
overlapping id ranges. Overlapping ranges are impossible within one database,
which is the tell.

### What happened

Collection moved from the laptop to the VPS on 2026-07-10 (the retained
pre-cutover backup `lab.db.pre_vps_primary_cutover_20260710T100234Z` marks the
date). For several days afterwards **both hosts ran the nightly ledger job**,
each against its own database, each pushing to this same public repo. Neither
host was wrong; they were describing two different ledgers.

The idempotency guard in `commit_pending_days` skips dates already present in
the ledger file, but each host reads its **own checkout** of that file. A host
that had not yet pulled the other's commit saw the date as uncommitted and
appended its own record; git then merged both.

This is directly checkable in git history — the two records for 2026-07-14
were authored twelve hours apart, in different timezones:

```bash
git show c775167 -- docs/ledger_commitments.jsonl   # 2026-07-15 02:15:41 +0000  (VPS, after its 02:00 UTC bundle)
git show 22266e0 -- docs/ledger_commitments.jsonl   # 2026-07-15 14:37:37 +0100  (laptop)
```

### Why the failing records can never verify, and why that is correct

Verification anchors to a record's own `first_id`/`last_id` range rather than
re-querying by date — deliberately, so a row arriving late for an already
committed date cannot silently change what that commitment covers. The three
laptop records name id ranges that describe rows in the laptop's database.
Evaluated against the VPS database — the one this project kept — those ranges
select different rows, so the hashes differ. They would verify against the
laptop's database and nowhere else.

The records are left in place, unverifiable, because that is what an
append-only audit trail means. Removing them would be an edit to the ledger,
performed to make verification pass, which is precisely the act the ledger
exists to make detectable.

### Scope

- **Forecast data: unaffected.** No forecast row was lost, altered or
  duplicated. The ledger is the record *of* the data, and only the record was
  ambiguous.
- **Coverage: complete.** All 21 distinct dates (2026-07-06 → 2026-07-26 at
  time of writing) have at least one commitment that verifies.
- **The pre-registration guarantee holds for every date**, because for each
  date a valid commitment exists whose git timestamp predates the outcomes it
  covers.

### Preventing a recurrence

- Commitment records written from 2026-07-28 onwards carry a `ledger_id`: a
  short hash of the database's own `meta.created_at`, which is written once at
  creation and never changes. Two hosts now produce visibly different ids, so
  a dual-host write is self-evident from the record instead of requiring an
  investigation to explain.
- The laptop no longer runs the orchestrator; the VPS is the sole writer.
  That is an operational arrangement, though, not an enforced one — the
  `ledger_id` field is what makes the situation legible if it ever happens
  again.
- `lab verify-ledger` makes the check runnable. The prose-only procedure is
  why this sat unnoticed for two weeks: nothing was executing it.
