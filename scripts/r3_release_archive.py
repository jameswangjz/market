"""OPS-003 offline release manifest. Never fetches, copies proofs or changes tasks.

CLI: --root EXISTING --saas PATH --native PATH --file PATH --monthly PATH
--review PATH --snapshot PATH --output-dir NEW_RELATIVE_DIRECTORY.
All paths are relative to root. Output is manifest.json only; private manifest
references in proofs are metadata, never opened or copied.

Snapshot contract: status='passed', captured_at=aware ISO time, namespace=market,
deployed_commit=COMMIT, image_tag=TAG, health={console:200, api:200},
deployments=[{name,image,desired,ready,updated,available,observed_generation,
generation}], source_sha256={backend/app filename: SHA},
backup={path,sha256,size_bytes,scope:current_readonly_snapshot}.
All seven required deployments must be present; images end in ':' + TAG.
Source hashes must match every tracked backend/app/*.py in COMMIT. Backup hashes
are declarations, not independently verified backup contents. No freshness or
independent live verification is claimed. See constants for release identity.
"""

import argparse
from datetime import datetime
import hashlib
import io
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tarfile


COMMIT = "5706151e08a177f0a9219bcb182391dc114d3f7d"
TAG = "20261016005810-5706151"
KINDS = ("saas", "native", "file", "monthly", "review", "snapshot")
DEPLOYMENTS = {"market-api", "market-web", "refund-execution-worker",
               "api-charge-ledger-worker", "message-center-worker", "online-delivery-worker",
               "version-change-worker"}
SHA = re.compile(r"[0-9a-f]{64}\Z")
LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
SECRET_KEY = re.compile(r"token|secret|password|authorization|cookie|credential|privatekey|apikey", re.I)
SECRET_VALUE = re.compile(
    r"\b(?:Bearer|Basic)\s+\S+|-----BEGIN .*PRIVATE KEY-----|"
    r"\beyJ[\w-]+\.[\w-]+\.[\w-]+|://[^\s/@:]+:[^\s/@]+@|"
    r"\b(?:sk-|gh[pousr]_)[A-Za-z0-9_-]{16,}|"
    r"(?:password|api[_-]?key|access[_-]?token|client[_-]?secret)\s*[=:]\s*\S+", re.I)
BOUNDARIES = {
    "bank": "Simulated payment/refund records; no real bank movement certified.",
    "storage": "FILE storage is in-memory; no actual MinIO/upload/scan certified.",
    "monthly_provider": "In-memory CHANGE/STATUS/RENEW simulator; no supplier HTTP certified.",
    "browser": "No browser acceptance or browser coverage claimed.",
    "deployment": "Parent-supplied snapshot only; no live fetch or freshness certification.",
    "review": "Submitted for review is not approval or business release acceptance.",
    "backups": "Snapshot backup SHA declarations only; no restore or content verification.",
}


class ArchiveError(ValueError):
    """Fixed diagnostics only; input values must never escape to stdout."""


def require(ok, code):
    if not ok:
        raise ArchiveError(code)


def public(value, hash_map=False):
    if isinstance(value, dict):
        for key, item in value.items():
            # Source filenames may contain 'credentials'; only hashes are allowed.
            if hash_map:
                require(isinstance(item, str) and SHA.fullmatch(item), "source_hash")
            else:
                require(not SECRET_KEY.search(re.sub(r"[^a-z0-9]", "", key.lower()))
                        or item is None or type(item) in (bool, int, float), "secret_key")
            public(item, key.endswith("_sha256") and isinstance(item, dict))
    elif isinstance(value, list):
        for item in value:
            public(item)
    elif isinstance(value, str):
        require(not SECRET_VALUE.search(value), "secret_value")


def unique(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "duplicate_json_key")
        result[key] = value
    return result


def decode(raw):
    try:
        value = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=unique,
                           parse_constant=lambda _: require(False, "nonfinite_json"))
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        raise ArchiveError("invalid_json") from None
    require(isinstance(value, dict), "object_required")
    public(value)
    return value


def parts(value):
    require(isinstance(value, str) and value and not value.startswith("/"), "relative_path_required")
    components = value.split("/")
    require(all(LABEL.fullmatch(p) and p not in {".", ".."}
                and not re.search(r"private|secret|credential|password|token", p, re.I)
                for p in components), "unsafe_path")
    return components


def directory(path):
    # Walk from / using dirfds so even intermediate symlinks cannot be followed.
    absolute = Path(path).absolute()
    require(".." not in absolute.parts, "unsafe_root")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in absolute.parts[1:]:
            nxt = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = nxt
        return fd
    except BaseException:
        os.close(fd)
        raise


def parent(root_fd, components):
    fd = os.dup(root_fd)
    try:
        for component in components[:-1]:
            nxt = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = nxt
        return fd
    except BaseException:
        os.close(fd)
        raise


def read(root_fd, path):
    components = parts(path)
    require(components[-1].endswith(".json"), "json_required")
    folder = parent(root_fd, components)
    try:
        fd = os.open(components[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=folder)
    finally:
        os.close(folder)
    try:
        before = os.fstat(fd)
        require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1, "regular_single_link_required")
        require(before.st_size <= 10_000_000, "input_too_large")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            raw = stream.read(10_000_001)
        after = os.fstat(fd)
        identity = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
        require(identity(before) == identity(after) and len(raw) == before.st_size, "unstable_input")
        return raw, identity(after)
    finally:
        os.close(fd)


def snapshot(value, source_hashes):
    finding = value.get("status") == "passed_with_findings"
    fields = {"status", "captured_at", "namespace", "deployed_commit", "image_tag", "health", "deployments", "backup", "source_sha256"}
    required = fields | ({"blockers"} if finding else set())
    require(required <= set(value) <= required | {"affected_worker", "release_ready", "limitations"}, "snapshot_fields")
    if "release_ready" in value:
        require(value["release_ready"] is (not finding), "release_readiness")
    if "limitations" in value:
        require(isinstance(value["limitations"], list) and all(isinstance(v, str) for v in value["limitations"]), "limitations")
    require(value["status"] in {"passed", "passed_with_findings"} and value["deployed_commit"] == COMMIT
            and value["image_tag"] == TAG and value["namespace"] == "market", "release_identity")
    if finding:
        require(value["blockers"] == ["api_charge_source_continuity_lost"]
                and value.get("affected_worker", "api-charge-ledger-worker") == "api-charge-ledger-worker", "finding_scope")
    elif "affected_worker" in value:
        require(False, "finding_scope")
    require(isinstance(value["health"], dict) and set(value["health"]) == {"console", "api"}
            and all(type(v) is int and v == 200 for v in value["health"].values()), "health")
    require(value["source_sha256"] == source_hashes and bool(source_hashes), "source_mismatch")
    try:
        require(datetime.fromisoformat(value["captured_at"].replace("Z", "+00:00")).utcoffset() is not None, "snapshot_time")
    except (ValueError, TypeError, AttributeError):
        raise ArchiveError("snapshot_time") from None
    for key, fields in (("deployments", {"name", "image", "desired", "ready", "updated", "available", "observed_generation", "generation"}),):
        items = value[key]
        require(isinstance(items, list) and items, "snapshot_list")
        names = set()
        for item in items:
            require(isinstance(item, dict) and set(item) == fields, "snapshot_entry")
            name = item["name"]
            require(isinstance(name, str) and LABEL.fullmatch(name) and name not in names, "snapshot_name")
            names.add(name)
            if key == "deployments":
                require(isinstance(item["image"], str) and re.fullmatch(r"[A-Za-z0-9./:_-]+", item["image"])
                        and (name not in DEPLOYMENTS or item["image"].endswith(":" + TAG)), "deployment_image")
                affected = finding and name == "api-charge-ledger-worker"
                require(type(item["desired"]) is int and item["desired"] > 0
                        and all(type(item[k]) is int and 0 <= item[k] <= item["desired"]
                                for k in ("ready", "updated", "available")), "deployment_readiness")
                require((affected and item["ready"] < item["desired"] and item["available"] < item["desired"])
                        or (not affected and all(item[k] == item["desired"] for k in ("ready", "updated", "available"))), "deployment_readiness")
                require(type(item["generation"]) is int and item["generation"] > 0
                        and type(item["observed_generation"]) is int
                        and item["observed_generation"] >= item["generation"], "deployment_generation")
        require(DEPLOYMENTS <= names, "missing_deployment")
    backup = value["backup"]
    require(isinstance(backup, dict) and set(backup) == {"path", "sha256", "size_bytes", "scope"}, "backup_fields")
    require(isinstance(backup["path"], str) and bool(backup["path"].strip())
            and isinstance(backup["sha256"], str) and SHA.fullmatch(backup["sha256"])
            and type(backup["size_bytes"]) is int and backup["size_bytes"] > 0
            and backup["scope"] == "current_readonly_snapshot", "backup_fields")


def validate(values, digests, source_hashes):
    for kind in KINDS:
        allowed = {"submitted_for_review"} if kind == "review" else {"passed"}
        if kind == "snapshot":
            allowed.add("passed_with_findings")
        require(values[kind].get("status") in allowed, "proof_status")
    snapshot(values["snapshot"], source_hashes)
    require(values["saas"].get("browser") is False and values["saas"].get("funds") == "simulated", "saas_boundary")
    require(values["native"].get("proof", {}).get("simulated_payment_legs", 0) > 0, "native_boundary")
    for kind in ("file", "monthly"):
        v = values[kind]
        require(v.get("deployed") is False and isinstance(v.get("gaps"), list)
                and any("browser" in str(g).lower() for g in v["gaps"]), "pg_boundary")
        adapters = v.get("external_adapters", {})
        require("simulated" in adapters.get("funds", "").lower(), "bank_boundary")
        require("in-memory" in adapters.get("storage" if kind == "file" else "supplier", "").lower(), "adapter_boundary")
    review = values["review"]
    bound = review.get("evidence")
    require(isinstance(bound, dict) and set(bound) == set(KINDS[:4]), "review_bindings")
    for kind in KINDS[:4]:
        require(isinstance(bound[kind], dict) and set(bound[kind]) == {"path", "sha256"}
                and isinstance(bound[kind]["path"], str)
                and bound[kind]["sha256"] == digests[kind], "review_sha_mismatch")
    require(review.get("evidence_bundle_sha256") == hashlib.sha256(json.dumps(bound, sort_keys=True).encode()).hexdigest(), "review_bundle_mismatch")
    require(review.get("protected_before") == review.get("protected_after")
            and isinstance(review.get("protected_before"), dict)
            and review["protected_before"].get("count") == 1
            and isinstance(review["protected_before"].get("sha256"), str)
            and SHA.fullmatch(review["protected_before"]["sha256"]), "protected_changed")


def committed_sources(root):
    def git(*args):
        result = subprocess.run(["git", "--no-replace-objects", "-C", str(root), *args],
                                capture_output=True, check=False)
        require(result.returncode == 0, "commit_unavailable")
        return result.stdout
    hashes = {}
    with tarfile.open(fileobj=io.BytesIO(git("archive", "--format=tar", COMMIT, "backend/app"))) as tree:
        for entry in tree:
            path = Path(entry.name)
            if path.parent.as_posix() == "backend/app" and path.suffix == ".py":
                require(entry.isfile(), "source_not_regular")
                hashes[path.name] = hashlib.sha256(tree.extractfile(entry).read()).hexdigest()
    return hashes


def archive(root, paths, output_dir):
    require(set(paths) == set(KINDS) and len(set(paths.values())) == len(KINDS), "input_set")
    output_parts = parts(output_dir)
    root_fd = directory(root)
    try:
        inputs = {k: read(root_fd, paths[k]) for k in KINDS}
        values = {k: decode(inputs[k][0]) for k in KINDS}
        digests = {k: hashlib.sha256(inputs[k][0]).hexdigest() for k in KINDS}
        validate(values, digests, committed_sources(root))
        finding = values["snapshot"]["status"] == "passed_with_findings"
        report = {"status": values["snapshot"]["status"], "release_ready": not finding,
                  "remaining_condition": ("Recover API charge source continuity or obtain explicit approval for a new epoch; offline archive does not authorize reset." if finding else None),
                  "blockers": (["api_charge_source_continuity_lost"] if finding else []),
                  "affected_worker": ("api-charge-ledger-worker" if finding else None),
                  "schema_version": 1, "deployed_commit": COMMIT,
                  "tag": TAG, "claim_boundaries": BOUNDARIES,
                  "all_business_release_passed": False, "task_states_changed": False,
                  "evidence": {k: {"path": paths[k], "sha256": digests[k],
                                     "status": values[k]["status"]} for k in KINDS}}
        # Reopen every path: catches replacement as well as mutation during validation.
        for kind in KINDS:
            require(read(root_fd, paths[kind]) == inputs[kind], "unstable_input")
        folder = parent(root_fd, output_parts)
        try:
            os.mkdir(output_parts[-1], mode=0o700, dir_fd=folder)
            out = os.open(output_parts[-1], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=folder)
            try:
                fd = os.open("manifest.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=out)
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    json.dump(report, stream, sort_keys=True, indent=2)
                    stream.write("\n")
            finally:
                os.close(out)
        finally:
            os.close(folder)
        return report
    finally:
        os.close(root_fd)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True)
    parser.add_argument("--output-dir", required=True)
    for kind in KINDS:
        parser.add_argument("--" + kind, required=True)
    args = parser.parse_args(argv)
    try:
        report = archive(args.root, {k: getattr(args, k) for k in KINDS}, args.output_dir)
    except (ArchiveError, OSError, ValueError, TypeError, AttributeError, RecursionError):
        print(json.dumps({"status": "invalid", "error": "archive_validation_failed"}))
        return 1
    print(json.dumps({"status": report["status"], "release_ready": report["release_ready"], "all_business_release_passed": False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
