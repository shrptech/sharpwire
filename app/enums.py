"""Every shared value list, declared once as a Literal. Imports nothing from the app.

A schema uses the alias as its field type, and the model builds its CHECK constraint from it,
so a new value is one edit plus a migration. The value order is the CHECK's order: add at the end.
"""

from typing import Literal

# What a plan op does to one object on a target (docs/03-pipeline-and-validation.md, step 5).
OpAction = Literal["add", "set", "remove", "move"]
