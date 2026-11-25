"""ORM models. Import every model module here, so `Base.metadata` (alembic, tests) sees all tables."""

from app.models.alert import Alert
from app.models.job import Job
from app.models.plan import Plan
from app.models.target import Target

__all__ = ["Alert", "Job", "Plan", "Target"]
