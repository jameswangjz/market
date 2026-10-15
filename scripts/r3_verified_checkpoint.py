"""Apply an explicit, evidence-bound R3 checkpoint without accepting on the user's behalf."""
import argparse
import hashlib
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--task", action="append", default=[])
    parser.add_argument("--status", choices=("in_progress", "review"), required=True)
    parser.add_argument("--progress", type=int, required=True)
    parser.add_argument("--note", required=True)
    args = parser.parse_args()
    data = args.evidence.read_bytes()
    if hashlib.sha256(data).hexdigest() != args.sha256:
        raise ValueError("Evidence hash mismatch")
    proof = json.loads(data.decode("utf-8-sig"))
    if not 0 <= args.progress <= 100 or not args.note.strip() or not args.task:
        raise ValueError("Explicit bounded progress, tasks and note required")
    if args.status == "review" and args.progress != 100:
        raise ValueError("Review requires a complete verified scope")
    if any(not code.startswith("TRD-R3-") for code in args.task):
        raise ValueError("R3 tasks only")
    if proof.get("status") not in ("passed", "partial_scoped_acceptance", "partial"):
        raise ValueError("Unexpected evidence status")
    if args.status == "review" and (proof.get("status") != "passed" or
            not set(args.task).issubset(proof.get("coverage", []))):
        raise ValueError("Review requires explicit per-task passed coverage")
    from app import main as m
    from sqlalchemy import select
    with m.SessionLocal() as db:
        user = db.scalar(select(m.User).where(m.User.email == "admin@market.local"))
        if not user or user.platform_role != "super_admin":
            raise ValueError("Platform system administrator required")
        result = []
        for code in args.task:
            task = db.scalar(select(m.DevelopmentTask).where(m.DevelopmentTask.code == code).with_for_update())
            if task is None:
                raise ValueError("Task missing: " + code)
            if task.status == "done":
                result.append({"code": code, "status": "done", "preserved": True})
                continue
            before = {"status": task.status, "progress": task.progress}
            task.status, task.progress, task.updated_at = args.status, args.progress, m.now()
            note = args.note + "；证据SHA256=" + args.sha256
            task.acceptance = task.acceptance.split("\n当前执行记录：")[0] + "\n当前执行记录：" + task.updated_at.isoformat() + "；" + note
            m.audit(db, user.email, "r3_verified_checkpoint", "development_task", code,
                    note, before=before, after={"status": task.status, "progress": task.progress})
            result.append({"code": code, "status": task.status, "progress": task.progress})
        db.commit()
    print(json.dumps({"updated": result, "user_acceptance": "not performed"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
