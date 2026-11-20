"""Validate installation database options and render Kubernetes connection data."""

import argparse
import base64
from dataclasses import dataclass
from ipaddress import IPv6Address
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
from urllib.parse import quote

import yaml


PG_RESOURCES = {
    ("Deployment", "market-postgres"),
    ("Service", "market-postgres"),
    ("PersistentVolumeClaim", "market-postgres-data"),
}


def add_database_options(parser):
    parser.add_argument("--db-type", default="Postgres", help="Postgres (default) or Dameng")
    parser.add_argument("--db-address", help="External database HOST[:PORT], or [IPv6]:PORT")
    parser.add_argument("--db-user", help="External database username")
    parser.add_argument("--db-password-file", type=Path, help="File containing the external database password")
    parser.add_argument("--db-name", help="External PostgreSQL database name (default: market)")


@dataclass(frozen=True)
class Database:
    driver: str
    local: bool
    address: str
    user: str
    name: str = ""

    def connection_secret(self, password):
        if not password:
            raise ValueError("Database password must not be empty")
        url = f"{self.driver}://{quote(self.user, safe='')}:{quote(password, safe='')}@{self.address}"
        if self.name:
            url += "/" + quote(self.name, safe="")
        return {"apiVersion": "v1", "kind": "Secret",
                "metadata": {"name": "market-db-connection", "namespace": "market"},
                "type": "Opaque", "data": {"DATABASE_URL": base64.b64encode(url.encode()).decode()}}


def address_with_port(value, default_port):
    host_port = re.fullmatch(r"(?P<host>[a-zA-Z0-9_][a-zA-Z0-9_.-]*|\[[a-fA-F0-9:.]+\])(?::(?P<port>[0-9]{1,5}))?", value)
    if not host_port:
        raise ValueError("Database address must be HOST[:PORT] or [IPv6]:PORT")
    host = host_port["host"]
    if host.startswith("["):
        try:
            IPv6Address(host[1:-1])
        except ValueError:
            raise ValueError("Invalid IPv6 database address") from None
    port = int(host_port["port"] or default_port)
    if not 0 < port <= 65535:
        raise ValueError("Invalid database port")
    return f"{host}:{port}"


def resolve_database(args):
    db_type = args.db_type.lower()
    if db_type not in {"postgres", "postgresql", "dameng", "dm", "dm8"}:
        raise ValueError("Database type must be Postgres or Dameng")
    external = (args.db_address, args.db_user, args.db_password_file, args.db_name)
    postgres = db_type in {"postgres", "postgresql"}
    if postgres and all(value is None for value in external):
        return Database("postgresql+psycopg", True, "market-postgres:5432", "market", "market"), None
    if not args.db_address or not args.db_user or args.db_password_file is None:
        raise ValueError("External database requires --db-address, --db-user and --db-password-file")
    if args.db_name is not None and (not postgres or not re.fullmatch(r"[A-Za-z0-9_.$-]+", args.db_name)):
        raise ValueError("--db-name must be a PostgreSQL database name")
    password = args.db_password_file.read_text(encoding="utf-8").rstrip("\r\n")
    if not password or any(separator in password for separator in ("\0", "\r", "\n")):
        raise ValueError("Database password file must contain one non-empty line")
    address = address_with_port(args.db_address, 5432 if postgres else 5236)
    if postgres:
        return Database("postgresql+psycopg", False, address, args.db_user, args.db_name or "market"), password
    return Database("dm+dmPython", False, address, args.db_user), password


def kubernetes_get(kind, name):
    content = subprocess.check_output(["kubectl", "-n", "market", "get", kind, name,
                                       "--ignore-not-found", "-o", "json"], text=True)
    return json.loads(content) if content else None


def render_local(args):
    database, password = resolve_database(args)
    existing = kubernetes_get("secret", "market-secrets")
    documents = []
    for obj in yaml.safe_load_all(args.manifest.read_text(encoding="utf-8")):
        if not obj:
            continue
        identity = (obj["kind"], obj["metadata"]["name"])
        if identity == ("Secret", "market-secrets"):
            if existing:
                continue
            if database.local and kubernetes_get("pvc", "market-postgres-data"):
                raise ValueError("Existing PostgreSQL PVC requires the original market-secrets credentials")
            obj["stringData"]["POSTGRES_PASSWORD"] = secrets.token_urlsafe(32)
            if database.local:
                password = obj["stringData"]["POSTGRES_PASSWORD"]
        if database.local or identity not in PG_RESOURCES:
            documents.append(obj)
    if database.local and existing:
        encoded = existing.get("data", {}).get("POSTGRES_PASSWORD")
        if not encoded:
            raise ValueError("Existing market-secrets is missing POSTGRES_PASSWORD")
        password = base64.b64decode(encoded, validate=True).decode()
    if not kubernetes_get("secret", "market-api-credentials"):
        documents.append({"apiVersion": "v1", "kind": "Secret",
                          "metadata": {"name": "market-api-credentials", "namespace": "market"}, "type": "Opaque",
                          "stringData": {"API_CREDENTIAL_ENCRYPTION_KEY":
                                         base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()}})
    args.output.write_text(yaml.safe_dump_all(documents, sort_keys=False), encoding="utf-8")
    args.secret_output.write_text(yaml.safe_dump(database.connection_secret(password), sort_keys=False), encoding="utf-8")
    print("internal-postgres" if database.local else "external-postgres" if database.name else "dameng")


def main():
    parser = argparse.ArgumentParser(prog="scripts/deploy.sh", description=__doc__)
    add_database_options(parser)
    parser.add_argument("--manifest", required=True, type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--output", required=True, type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--secret-output", required=True, type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        render_local(args)
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        parser.exit(2, f"Database configuration failed: {error}\n")


if __name__ == "__main__":
    main()
