"""Plain data shapes for a database schema (validated by Pydantic)."""
from pydantic import BaseModel


class Column(BaseModel):
    name: str
    data_type: str
    nullable: bool
    default: str | None = None


class ForeignKey(BaseModel):
    name: str
    columns: list[str]
    ref_table: str
    ref_columns: list[str]


class Index(BaseModel):
    name: str
    unique: bool
    primary: bool
    definition: str


class Table(BaseModel):
    name: str
    columns: list[Column]
    primary_key: list[str] = []
    unique_constraints: list[list[str]] = []
    foreign_keys: list[ForeignKey] = []
    indexes: list[Index] = []
    estimated_rows: int | None = None  # None = Postgres has not counted yet


class SchemaSnapshot(BaseModel):
    schema_name: str
    server_version: str
    tables: list[Table]

    def table(self, name: str) -> Table | None:
        return next((t for t in self.tables if t.name == name), None)
