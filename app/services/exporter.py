"""Экспорт списков пользователей в CSV / Excel / JSON."""
import csv
import io
import json

from openpyxl import Workbook

from app.db.models import PetOwner

FIELDS = ["user_id", "username", "first_name", "last_name", "category",
          "confidence", "first_seen_at", "last_message_at"]


def _rows(owners: list[PetOwner]) -> list[dict]:
    return [
        {
            "user_id": o.user_id,
            "username": o.username,
            "first_name": o.first_name,
            "last_name": o.last_name,
            "category": o.category.value,
            "confidence": o.confidence,
            "first_seen_at": o.first_seen_at.isoformat() if o.first_seen_at else None,
            "last_message_at": o.last_message_at.isoformat() if o.last_message_at else None,
        }
        for o in owners
    ]


def to_csv(owners: list[PetOwner]) -> bytes:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=FIELDS)
    writer.writeheader()
    writer.writerows(_rows(owners))
    return buf.getvalue().encode("utf-8-sig")


def to_json(owners: list[PetOwner]) -> bytes:
    return json.dumps(_rows(owners), ensure_ascii=False, indent=2).encode()


def to_xlsx(owners: list[PetOwner]) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.append(FIELDS)
    for row in _rows(owners):
        ws.append([row[f] for f in FIELDS])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
