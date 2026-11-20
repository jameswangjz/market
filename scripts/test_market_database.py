"""Database installation profiles for local Kubernetes and offline Flux."""
import argparse
import base64
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import yaml

from market_database import PG_RESOURCES, add_database_options, render_local, resolve_database
from market_release_bundle import IMAGES, STAGES, build, containers, environment_refs, pod_spec
from render_market_env2 import make_site


K8S = Path(__file__).resolve().parents[1] / "k8s"
DB_REFERENCE = ("Secret", "market-db-connection")


def identities(objects):
    return {(obj["kind"], obj["metadata"]["name"]) for obj in objects if obj}


def dsn(secret):
    return base64.b64decode(secret["data"]["DATABASE_URL"]).decode()


class DatabaseInstallationTests(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.parser = argparse.ArgumentParser()
        add_database_options(self.parser)
        self.secret_input = self.root / "test-password"
        self.secret_input.write_text("complex:/@ #!秘密\n", encoding="utf-8")
        self.base = self.root / "base.yaml"
        self.secret = self.root / "secret.yaml"

    def external_args(self, db_type, address):
        return ["--db-type", db_type, "--db-address", address, "--db-user", "market-user",
                "--db-password-file", str(self.secret_input)]

    def local_render(self, options, existing=None):
        args = self.parser.parse_args(options)
        args.manifest = K8S / "all-in-one.yaml"
        args.output = self.base
        args.secret_output = self.secret
        with patch("market_database.kubernetes_get",
                   side_effect=lambda kind, name: existing if (kind, name) == ("secret", "market-secrets") else None):
            render_local(args)
        return list(yaml.safe_load_all(self.base.read_text())), yaml.safe_load(self.secret.read_text())

    def test_default_postgres_creates_internal_resources_and_reuses_existing_password(self):
        manifests, secret = self.local_render(["--db-type", "Postgres"])
        self.assertTrue(PG_RESOURCES <= identities(manifests))
        market_secrets = next(obj for obj in manifests if obj["metadata"]["name"] == "market-secrets")
        password = market_secrets["stringData"]["POSTGRES_PASSWORD"]
        self.assertIn(password, dsn(secret))
        existing = {"data": {"POSTGRES_PASSWORD": base64.b64encode(b"existing/pass@").decode()}}
        manifests, secret = self.local_render([], existing)
        self.assertNotIn(("Secret", "market-secrets"), identities(manifests))
        self.assertEqual(dsn(secret), "postgresql+psycopg://market:existing%2Fpass%40@market-postgres:5432/market")

    def test_external_postgres_and_dameng_render_no_bundled_database(self):
        for db_type, address, opts, expected in (
            ("Postgres", "db.example:15432", ["--db-name", "market_prod"],
             "postgresql+psycopg://market-user:complex%3A%2F%40%20%23%21%E7%A7%98%E5%AF%86@db.example:15432/market_prod"),
            ("Dameng", "[2001:db8::1]", [],
             "dm+dmPython://market-user:complex%3A%2F%40%20%23%21%E7%A7%98%E5%AF%86@[2001:db8::1]:5236"),
        ):
            with self.subTest(db_type=db_type):
                manifests, secret = self.local_render(self.external_args(db_type, address) + opts)
                self.assertFalse(PG_RESOURCES & identities(manifests))
                self.assertEqual(dsn(secret), expected)
                for config in (obj for obj in manifests if obj["kind"] == "ConfigMap"):
                    self.assertNotIn("DATABASE_URL", config["data"])
                self.assertNotIn("complex", self.base.read_text())

    def test_invalid_config_fails_before_contacting_kubernetes(self):
        with patch("market_database.kubernetes_get") as kube:
            for options in (["--db-type", "oracle"], ["--db-type", "Dameng"],
                            ["--db-address", "db.example"], self.external_args("Postgres", "postgresql://host"),
                            self.external_args("Postgres", "db.example:65536"),
                            self.external_args("Dameng", "db.example") + ["--db-name", "market"]):
                with self.subTest(options=options), self.assertRaises(ValueError):
                    args = self.parser.parse_args(options)
                    render_local(args)
            kube.assert_not_called()

    def prepare_oci(self):
        source = self.root / "source"
        source.mkdir()
        (source / "k8s").symlink_to(K8S, target_is_directory=True)
        metadata = self.root / "images"
        metadata.mkdir()
        registry = "harbor.example:30443"
        commit = "a" * 40
        version = "20261119152425-" + commit[:7]
        for image in IMAGES:
            data = {"image.name": f"{registry}/market/{image}:{version}", "containerimage.digest": "sha256:" + "1" * 64}
            (metadata / (image + ".json")).write_text(json.dumps(data))
        build(argparse.Namespace(commit=commit, version=version, registry=registry, project="market",
                                 manifest_dir=K8S, metadata=metadata, output=self.root / "release"))
        infrastructure = ("postgres", "redis", "clamav", "minio", "presidio-analyzer",
                          "etcd", "apisix", "prometheus", "alertmanager", "mailpit")
        lock = [{"reference": f"192.168.10.10:30443/market/infra/{name}:test", "digest": "sha256:" + "2" * 64}
                for name in infrastructure]
        (self.root / "market-infra-images-lock.json").write_text(json.dumps(lock))

    def test_offline_flux_render_and_release_reference(self):
        self.prepare_oci()
        release_dir = self.root / "release"
        release = json.loads((release_dir / "release.json").read_text())
        self.assertIn({"kind": "Secret", "name": "market-db-connection"}, release["environment_resources"])
        for stage in STAGES:
            resources = yaml.safe_load_all((release_dir / "apps" / stage / "resources.yaml").read_text())
            for obj in resources:
                if obj["kind"] not in {"Deployment", "CronJob"}:
                    continue
                spec = pod_spec(obj)
                for container in containers(spec):
                    if any(ref.get("configMapRef", {}).get("name") == "market-config" for ref in container.get("envFrom", [])):
                        self.assertIn(DB_REFERENCE, environment_refs(spec), obj["metadata"]["name"])
        for options, expected in [
            ([], "@market-postgres:5432/market"),
            (self.external_args("Postgres", "pg.example"), "@pg.example:5432/market"),
            (self.external_args("Dameng", "dm.example"), "@dm.example:5236"),
        ]:
            args = self.parser.parse_args(options)
            vars(args).update(root=self.root, registry="harbor.example:30443", oci_digest="sha256:" + "1"*64,
                              pod_cidr="10.244.0.0/16", web_url="http://market.example",
                              gateway_url="http://gateway.example", clamav_seeded=False)
            make_site(args)
            manifests = list(yaml.safe_load_all((self.root / "foundation.yaml").read_text()))
            self.assertEqual(PG_RESOURCES & identities(manifests), PG_RESOURCES if not options else set())
            secrets = list(yaml.safe_load_all((self.root / "site-secrets.yaml").read_text()))
            database = next(obj for obj in secrets if obj["metadata"]["name"] == DB_REFERENCE[1])
            self.assertTrue(dsn(database).endswith(expected))


if __name__ == "__main__":
    unittest.main()
