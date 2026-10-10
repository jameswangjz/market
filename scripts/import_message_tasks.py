"""Run inside the API container with the task CSV path as its argument."""
import csv
import sys

from sqlalchemy import select
from app.main import DevelopmentTask, SessionLocal


def main():
    with open(sys.argv[1], encoding="utf-8-sig", newline="") as source:
        rows = list(csv.DictReader(source))
    with SessionLocal() as db:
        created = 0
        for row in rows:
            code = row["任务编号"]
            if db.scalar(select(DevelopmentTask).where(DevelopmentTask.code == code)):
                continue
            db.add(DevelopmentTask(
                code=code, owner=row["负责人"], title=row["详细任务与验收条件"],
                area="消息中心", priority="P0", status="todo",
                dependencies=row["依赖"], acceptance=row["详细任务与验收条件"], progress=0,
            ))
            created += 1
        db.commit()
    print(f"Tasks: {len(rows)}; inserted: {created}; existing states preserved")


if __name__ == "__main__":
    main()
