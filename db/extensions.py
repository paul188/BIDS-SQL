"""
db/extensions.py
----------------
Lets an application add its own columns to a BIDS-SQL table.

BIDS-SQL's models hold what a BIDS dataset itself says. An application
built on them may keep more on the same rows: a search engine its search
vectors, a catalogue the fields of the archive it crawls. It declares those
columns in its own code and registers them here:

    from db.extensions import extend
    from db.models import BIDSDataset

    datasets = extend(BIDSDataset, Column("search_tsv", TSVECTOR))
    select(datasets.c.name).where(datasets.c.search_tsv.op("@@")(query))

The model's table then has the columns for SQLAlchemy Core queries. BIDS-SQL
itself does not use them:

  The ORM class is not mapped to them, so loading and saving a BIDSDataset
  neither reads nor writes them.

  create_all_tables() leaves them out of CREATE TABLE. The application
  creates them with its own migrations.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import Column, Table
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.schema import CreateColumn

from db.base import Base

# Key of Column.info that marks a column added by extend().
EXTENSION = "extension"


class ColumnExistsError(ValueError):
    """extend() was given a column name the table already has."""


def extend(model: type[Base], *columns: Column[Any]) -> Table:
    """Add *columns* to the table of *model* and return that table."""
    table = model.__table__
    assert isinstance(table, Table)
    for column in columns:
        if column.name in table.c:
            raise ColumnExistsError(f"{table.name} already has a column {column.name}")
    for column in columns:
        column.info[EXTENSION] = True
        table.append_column(column)
    return table


@compiles(CreateColumn)
def _leave_extension_columns_out(element: CreateColumn, compiler: Any, **kw: Any) -> str | None:
    """Compile CREATE TABLE without the columns extend() added."""
    if element.element.info.get(EXTENSION):
        return None
    return compiler.visit_create_column(element, **kw)
