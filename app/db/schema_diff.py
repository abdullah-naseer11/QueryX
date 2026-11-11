"""Compares two schema snapshots and lists what changed."""
from pydantic import BaseModel

from app.db.models import SchemaSnapshot, Table


class Change(BaseModel):
    kind: str      # for example "column_added"
    table: str
    detail: str

    def __str__(self) -> str:
        sign = {"added": "+", "removed": "-", "changed": "~"}[self.kind.rsplit("_", 1)[1]]
        return f"{sign} {self.kind.rsplit('_', 1)[0]} {self.table}: {self.detail}"


class SchemaDiff(BaseModel):
    changes: list[Change] = []

    @property
    def is_empty(self) -> bool:
        return not self.changes

    def text(self) -> str:
        return "\n".join(str(c) for c in self.changes) or "(no changes)"


def _diff_table(name: str, a: Table, b: Table) -> list[Change]:
    out: list[Change] = []
    ca, cb = {c.name: c for c in a.columns}, {c.name: c for c in b.columns}
    for n in cb.keys() - ca.keys():
        c = cb[n]
        out.append(Change(kind="column_added", table=name, detail=f"{n} {c.data_type}{'' if c.nullable else ' NOT NULL'}"))
    for n in ca.keys() - cb.keys():
        out.append(Change(kind="column_removed", table=name, detail=n))
    for n in ca.keys() & cb.keys():
        x, y = ca[n], cb[n]
        for label, old, new in (("type", x.data_type, y.data_type), ("nullable", x.nullable, y.nullable), ("default", x.default, y.default)):
            if old != new:
                out.append(Change(kind="column_changed", table=name, detail=f"{n} {label}: {old} -> {new}"))

    ia, ib = {i.name: i for i in a.indexes}, {i.name: i for i in b.indexes}
    for n in ib.keys() - ia.keys():
        out.append(Change(kind="index_added", table=name, detail=ib[n].definition))
    for n in ia.keys() - ib.keys():
        out.append(Change(kind="index_removed", table=name, detail=ia[n].definition))
    for n in ia.keys() & ib.keys():
        if ia[n].definition != ib[n].definition:
            out.append(Change(kind="index_changed", table=name, detail=f"{n}: {ia[n].definition} -> {ib[n].definition}"))

    fa = {f.name: f for f in a.foreign_keys}
    fb = {f.name: f for f in b.foreign_keys}
    for n in fb.keys() - fa.keys():
        f = fb[n]
        out.append(Change(kind="foreign_key_added", table=name, detail=f"{n} ({', '.join(f.columns)}) -> {f.ref_table}"))
    for n in fa.keys() - fb.keys():
        out.append(Change(kind="foreign_key_removed", table=name, detail=n))

    ka = {c.name: c.definition for c in a.check_constraints}
    kb = {c.name: c.definition for c in b.check_constraints}
    for n in kb.keys() - ka.keys():
        out.append(Change(kind="check_added", table=name, detail=f"{n} {kb[n]}"))
    for n in ka.keys() - kb.keys():
        out.append(Change(kind="check_removed", table=name, detail=n))
    for n in ka.keys() & kb.keys():
        if ka[n] != kb[n]:
            out.append(Change(kind="check_changed", table=name, detail=f"{n}: {ka[n]} -> {kb[n]}"))

    if a.primary_key != b.primary_key:
        out.append(Change(kind="primary_key_changed", table=name, detail=f"{a.primary_key} -> {b.primary_key}"))
    if sorted(a.unique_constraints) != sorted(b.unique_constraints):
        out.append(Change(kind="unique_changed", table=name, detail=f"{a.unique_constraints} -> {b.unique_constraints}"))
    return out


def diff_schemas(before: SchemaSnapshot, after: SchemaSnapshot) -> SchemaDiff:
    ta, tb = {t.name: t for t in before.tables}, {t.name: t for t in after.tables}
    changes: list[Change] = []
    for n in sorted(tb.keys() - ta.keys()):
        changes.append(Change(kind="table_added", table=n, detail=f"{len(tb[n].columns)} columns"))
    for n in sorted(ta.keys() - tb.keys()):
        changes.append(Change(kind="table_removed", table=n, detail="gone"))
    for n in sorted(ta.keys() & tb.keys()):
        changes += _diff_table(n, ta[n], tb[n])
    return SchemaDiff(changes=changes)
