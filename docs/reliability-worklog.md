# Reliability implementation ledger

Approved scope: fix the confirmed defects and inconsistent reliability paths from the
2026-11-17 audit, preserving author approval and the existing Telegram/YouTube workflows.
Base: 5a3fcd11c40f7c09659f68ae5594093ed4abada3.

Tasks:
1. Concurrent uploads and persistent working files.
2. Durable scheduled publication with explicit failed/uncertain states.
3. Process cancellation/timeouts and resumable remix jobs.
4. Version hashes, complete remix projects, final QC profiles and duplicate media.
5. Publication state/version tracking and interrupted budget reservations.
6. Rolling content plan, operations/restore documentation and final regression/review.

Ruling: retain existing author approval and platform privacy settings; no real posts or
YouTube uploads are performed by implementation tests — publication remains the owner's action.
Ruling: growth experiments and a wholesale bot.py rewrite are product work, not confirmed
defects; fix the documented failure paths first — new product behavior needs separate evaluation.

Baseline: audited source has 110 passing tests; concurrency reproduction lost one of two files.

Task 1: RED→GREEN concurrent uploads; persistent default DATA_DIR/work; UI button delegates
to the same /done validation; Remix-only event isolation serializes upload/done/cancel FSM changes. Other modes retain album concurrency.
Task 2: RED→GREEN scheduled-send failure/rate-limit regressions. Additive migration 012;
jobs are claimed, retained on failure, and only removed after acknowledged publication.
Unknown outcomes require channel review; an explicit rate limit retries at most five times.
Task 3 (processes): RED→GREEN real child cancellation. Shared process-group termination,
drained output and bounded timeouts now used by remix/daily/weekly/studio/QC/visual review.
Task 4 (versions/QC): RED→GREEN clip offset, changed file middle, duplicate uploaded file,
and selected 720p profile. Full streaming digests cache unchanged file stats.
Last completed full suite before version/QC changes: 114/114 passing.

Task 3 (recovery): persistent render_jobs, /projects and owner-scoped resume; completed
output checkpoint verified by SHA-256; transient DB failure retains finalization for retry.
Task 4: full remix manifest/retained project and final QC; duplicate asset content rejected.
Task 5: durable YouTube version claims and external outcome ID; stale budget reservation
recovery keeps its estimate. Vision builds a fallback sheet even with no reserve key.
Task 6: evergreen plan after dated schedule, legacy-work copy on deploy, optional streaming
file backup and restore instructions; no production restore or backup cron was performed.
Ruling: an interrupted provider call retains estimated cost rather than silently releasing
it — billing may already have occurred — account reconciliation can reduce the estimate later.
Ruling: an unknown publication outcome never retries automatically — this may require an
extra author check, but prevents accidental duplicates.

Verification before final review: 124/124 tests, Ruff F/E9, py_compile and shell syntax passed.
Review focus: real SQL claim semantics; unknown publication outcomes; old/legacy paths after
container replacement; cancellation of subprocess descendants and worker tasks; resume ownership;
runtime command wiring; file/metadata version claims; old migration checksums.


Final review: one independent reviewer found seven Important issues (no Critical).
Single fix pass:
1. FIXED — installer default and legacy .env migrate to /data/work.
2. FIXED — preservation helper skips aliases resolving to the destination.
3. FIXED — preservation runs in install.sh before container replacement, including first upgrade.
4. FIXED — startup recovers interrupted Telegram claims as uncertain; manual UI offers checked retry.
5. FIXED — isolation scoped to RemixState, preserving CustomState album debounce concurrency.
6. FIXED — streaming renderer now owns a process session and terminates/drains descendants.
7. FIXED — cancelled YouTube upload records uncertain before reraising cancellation.
Regression checks failed on pre-review code and pass after fixes; real child process and real
Telegram callback handlers exercised using local delivery/database substitutes.
Verification: 135/135 tests; Ruff F/E9, Python compile, shell syntax and git diff checks pass.
Ruling/limits: real PostgreSQL locking, provider delivery, production deployment and restore
are not exercised in this environment. Unknown delivery never implies safe automatic retry.
No second final review was requested. Existing author approvals and publish buttons retained.
