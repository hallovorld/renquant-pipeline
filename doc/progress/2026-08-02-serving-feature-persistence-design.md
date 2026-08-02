# Serving feature persistence: the design that unblocks Stage-3 and attribution

STATUS: proposal (design-only PR; no code).
WHAT: doc/design/2026-08-02-serving-feature-persistence.md — persist the
served feature matrix (~sub-MB parquet per run) + an additive digest sidecar
block in the run bundle; never-raises writer; three contracts satisfied
(Stage-3 producer becomes a formatting step; per-name attribution becomes
answerable forever; the recipe-vs-serving transform divergence becomes
measurable per run).
WHY/DIR: the daily run persists conclusions and discards the matrix that
produced them (0 feature keys in the bundle vs ~290 decision rows,
orch#678's measurement `[VERIFIED — prior work, orch#678; structural shape
re-confirmed on today's main by the absence of any feature writer in the
serving path]`); input vintages are not byte-reproducible after rebuilds
(`[VERIFIED — the Job B golden failure, model#185 record]`), so what is not
persisted on day T is unrecoverable.
EVIDENCE:
  artifact:      doc/design/2026-08-02-serving-feature-persistence.md
  prod or exp:   exp — design doc only
  existing data: orch#678 (0 feature keys / 290 decision rows) and the
                 model#185 golden vintage finding, both cited as prior work
                 with their homes named; no new measurement claimed
  best-known?:   yes — first persistence design for the serving matrix; the
                 alternative (reconstruct later) is impossible in principle
                 per the vintage finding
  scope:         docs-only; implementation is rollout step 2 with a
                 byte-equality test against the scorer's consumed matrix
NEXT: review → implementation PR here (step 2) → orchestrator sidecar
consumption (step 3) → operator pin batch (step 4). AC6: N/A — a recorder,
no gate touched.
