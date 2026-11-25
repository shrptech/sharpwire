"""Every shared value list, declared once as a Literal. Imports nothing from the app.

A schema uses the alias as its field type, and the model builds its CHECK constraint from it,
so a new value is one edit plus a migration. The value order is the CHECK's order: add at the end.
"""

from typing import Literal

# What a plan op does to one object on a target (docs/03-pipeline-and-validation.md, step 5).
OpAction = Literal["add", "set", "remove", "move"]

# A target: one thing sharpwire plans for (docs/04-drivers.md). Proxmox kinds arrive with steps 3d to 3f.
TargetKind = Literal["core", "leaf"]

# The Postgres job queue (app/jobs/).
JobKind = Literal["reconcile"]
JobStatus = Literal["queued", "running", "done", "failed"]

# A plan's life (docs/03-pipeline-and-validation.md#plan-lifecycle). Approval and apply add values in step 5.
PlanStatus = Literal["open", "superseded"]

AlertSeverity = Literal["warning", "critical"]
