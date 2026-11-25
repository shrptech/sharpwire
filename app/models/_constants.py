from typing import Any, get_args

from sqlalchemy import CheckConstraint


def check_in(column: str, literal: Any, name: str, *, nullable: bool = False) -> CheckConstraint:
    """The CHECK that keeps `column` to the values of `literal`, a `Literal` from `app/enums.py` (as in sharpstack).

    The schema's field type is the same alias, so what the API serves and what the database stores can't differ.
    """
    values = ",".join(repr(value) for value in get_args(literal))
    prefix = f"{column} IS NULL OR " if nullable else ""
    return CheckConstraint(f"{prefix}{column} IN ({values})", name=name)
