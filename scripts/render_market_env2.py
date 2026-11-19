#!/usr/bin/env python3
"""Render K8S2 resources from a verified release and the matching Git snapshot."""

import argparse
import base64
import copy
import json
import os
from pathlib import Path
import secrets
import subprocess

import yaml

from market_database import add_database_options, resolve_database


def load(path):
    return [obj for obj in yaml.safe_load_all(path.read_text(encoding="utf-8")) if obj]


def dump(path, objects):
    path.write_text(yaml.safe_dump_all(objects, sort_keys=False, allow_unicode=True), encoding="utf-8")


def secret(name, data):
    return {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": name, "namespace": "market"},
            "type": "Opaque", "stringData": data}


def new_key():
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()


def credentials(root):
    path = root / "site-credentials.json"
    if not path.exists():
        values = {key: secrets.token_hex(24) for key in ("POSTGRES_PASSWORD", "JWT_SECRET",
                  "MINIO_ROOT_PASSWORD", "APISIX_ADMIN_KEY", "SLA_GATEWAY_INGEST_SECRET", "SLA_PROBE_SECRET")}
        values.update(MINIO_ROOT_USER="market", API_CREDENTIAL_ENCRYPTION_KEY=new_key(),
                      OIDC_CREDENTIAL_ENCRYPTION_KEY=new_key(), OIDC_SIGNING_KEY_PEM=subprocess.check_output(
                          ["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048"],
                          stderr=subprocess.DEVNULL, text=True))
        with path.open("x", encoding="utf-8") as file:
            json.dump(values, file)
    return json.loads(path.read_text())


def env_ref(name, source, key=None):
    return {"name": name, "valueFrom": {"secretKeyRef": {"name": source, "key": key or name}}}


def pod_template(obj):
    spec = obj["spec"]
    if obj["kind"] == "CronJob":
        spec = spec["jobTemplate"]["spec"]
    return spec["template"]["spec"]


def make_site(args):
    database, external_password = resolve_database(args)
    root = args.root
    tree = root / "source" / "k8s"
    bundle = root / "release"
    release = json.loads((bundle / "release.json").read_text())
    if database.driver == "dm+dmPython" and "Dameng" not in release.get("database_types", []):
        raise ValueError("This OCI release does not include Dameng driver support")
    locks = json.loads((root / "market-infra-images-lock.json").read_text())
    mirror = {
        item["reference"].split("/")[-1].split(":")[0]:
        item["reference"].replace("192.168.10.10:30443", args.registry) + "@" + item["digest"]
        for item in locks
    }
    stored = credentials(root)
    base = {(obj["kind"], obj["metadata"]["name"]): obj for obj in load(tree / "all-in-one.yaml")}
    get = lambda kind, name: copy.deepcopy(base[(kind, name)])
    password = stored["POSTGRES_PASSWORD"] if database.local else external_password

    config = get("ConfigMap", "market-config")
    dat = config["data"]
    dat.pop("DATABASE_URL", None)
    dat.update(
        CORS_ORIGINS=args.web_url, PUBLIC_BASE_URL=args.web_url, API_GATEWAY_PUBLIC_URL=args.gateway_url,
        OIDC_ISSUER=args.web_url, OIDC_ALLOW_HTTP_DEVELOPMENT="true", SLA_PROBE_GATEWAY_ORIGIN="http://market-apisix:9080",
        SLA_PROBE_SOURCE_CIDRS=args.pod_cidr,
        UPSTREAM_ALLOWED_HOSTS="", UPSTREAM_ALLOWED_CIDRS="", PLATFORM_OAUTH_ISSUER=args.web_url,
        PLATFORM_OAUTH_TOKEN_URL=f"{args.web_url}/oauth/token",
    )
    secrets_yaml = [
        secret("market-secrets", {key: stored[key] for key in ("POSTGRES_PASSWORD", "JWT_SECRET",
               "MINIO_ROOT_USER", "MINIO_ROOT_PASSWORD", "APISIX_ADMIN_KEY")}),
        database.connection_secret(password),
        secret("market-sla-telemetry", {key: stored[key] for key in ("SLA_GATEWAY_INGEST_SECRET", "SLA_PROBE_SECRET")}),
        *[secret(name, {key: stored[key]}) for name, key in [
            ("market-api-credentials", "API_CREDENTIAL_ENCRYPTION_KEY"),
            ("market-oidc-signing", "OIDC_SIGNING_KEY_PEM"),
            ("market-oidc-credentials", "OIDC_CREDENTIAL_ENCRYPTION_KEY"),
        ]],
    ]
    size = {
        "market-clamav-data": "1Gi", "market-postgres-data": "5Gi", "market-redis-data": "2Gi",
        "market-minio-data": "5Gi", "redis-maintenance-data": "1Gi", "market-etcd-data": "1Gi",
    }

    redis = load(tree / "redis-persistence.yaml")
    maintenance = load(tree / "redis-maintenance.yaml")
    claims = [get("PersistentVolumeClaim", name) for name in
              ("market-clamav-data", "market-minio-data")]
    if database.local:
        claims.append(get("PersistentVolumeClaim", "market-postgres-data"))
    claims.extend(x for x in redis + maintenance if x["kind"] == "PersistentVolumeClaim")
    etcd_vol = copy.deepcopy(claims[0])
    etcd_vol["metadata"]["name"] = "market-etcd-data"
    claims.append(etcd_vol)
    for claim in claims:
        claim["spec"]["resources"]["requests"]["storage"] = size[claim["metadata"]["name"]]
    scanners = ("market-clamav", "market-presidio-analyzer")
    deps = (*scanners, "market-redis", "market-minio")
    if database.local:
        deps += ("market-postgres",)
    infrastructure = [get("Deployment", name) for name in deps]
    for obj in infrastructure:
        if obj["metadata"]["name"] == "market-clamav":
            obj["spec"]["replicas"] = 1 if args.clamav_seeded else 0
            container = pod_template(obj)["containers"][0]
            container["env"] = [{"name": "CLAMAV_NO_FRESHCLAMD", "value": "true"}]
            container["resources"] = {"requests": {"memory": "768Mi", "cpu": "200m"},
                                      "limits": {"memory": "2400Mi"}}
            container["startupProbe"] = {"tcpSocket": {"port": 3310}, "periodSeconds": 5, "failureThreshold": 60}
        obj["spec"]["strategy"] = {"type": "Recreate"}
    api_gateway = load(tree / "apisix.yaml")
    for obj in api_gateway:
        name = obj["metadata"]["name"]
        if name == "market-etcd" and obj["kind"] == "Deployment":
            pod_template(obj)["volumes"] = [{"name": "data", "persistentVolumeClaim": {"claimName": "market-etcd-data"}}]
            obj["spec"]["strategy"] = {"type": "Recreate"}
        if name == "market-apisix" and obj["kind"] == "Deployment":
            obj["spec"]["replicas"] = 1
            for volume in pod_template(obj)["volumes"]:
                if volume["name"] == "config":
                    volume.pop("configMap")
                    volume["secret"] = {"secretName": "market-apisix-config"}
        if obj["kind"] == "PodDisruptionBudget":
            obj["spec"]["minAvailable"] = 0
        if obj["kind"] == "ConfigMap":
            conf = yaml.safe_load(obj["data"]["config.yaml"])
            admin = conf["deployment"]["admin"]
            admin["admin_key"][0]["key"] = stored["APISIX_ADMIN_KEY"]
            admin["allow_admin"] = [args.pod_cidr, "127.0.0.1/32"]
            secrets_yaml.append(secret("market-apisix-config", {"config.yaml": yaml.safe_dump(conf, sort_keys=False)}))
    api_gateway = [x for x in api_gateway if x["kind"] != "ConfigMap"]
    lua = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "market-apisix-policy-plugin", "namespace": "market"},
           "data": {f: (tree / f).read_text() for f in
                    ("market-gateway-quota.lua", "market-gateway-oauth.lua", "market-sla-logger.lua")}}
    monitors = load(tree / "monitoring.yaml")
    mailpit = [x for x in load(tree / "message-center.yaml") if x["metadata"]["name"] == "market-mailpit"]
    services = [get("Service", name) for name in (*deps, "market-api", "market-web", "market-gateway")]
    freshclam = get("CronJob", "market-clamav-freshclam")
    freshclam["spec"]["suspend"] = True
    infrastructure += [freshclam] + api_gateway + monitors + mailpit
    infra_images = {"market-postgres": "postgres", "market-redis": "redis", "market-clamav": "clamav",
        "market-clamav-freshclam": "clamav", "market-minio": "minio", "market-presidio-analyzer": "presidio-analyzer",
        "market-etcd": "etcd", "market-apisix": "apisix", "market-prometheus": "prometheus",
        "market-alertmanager": "alertmanager", "market-mailpit": "mailpit"}
    for obj in infrastructure:
        if obj["kind"] in {"Deployment", "CronJob"}:
            pod = pod_template(obj)
            pod["imagePullSecrets"] = [{"name": "market-harbor-pull"}]
            pod["containers"][0]["image"] = mirror[infra_images[obj["metadata"]["name"]]]
    dump(root / "foundation.yaml", [config, lua] + claims + [x for x in redis if x["kind"] == "ConfigMap"] + services + infrastructure)
    dump(root / "site-secrets.yaml", secrets_yaml)
    (root / "clamav-image.txt").write_text(mirror["clamav"] + "\n")

    flux = [{"apiVersion": "source.toolkit.fluxcd.io/v1", "kind": "OCIRepository",
             "metadata": {"name": "market-release", "namespace": "flux-system"},
             "spec": {"interval": "5m", "url": f"oci://{args.registry}/market/market-release",
                      "ref": {"digest": args.oci_digest}, "secretRef": {"name": "market-harbor-pull"},
                      "certSecretRef": {"name": "market-harbor-ca"}}}]
    for stage, data in release["stages"].items():
        if data["optional"]:
            continue
        spec = {"interval": "5m", "timeout": "5m", "wait": True, "prune": True, "targetNamespace": "market",
                "sourceRef": {"kind": "OCIRepository", "name": "market-release"}, "path": data["path"],
                "images": [{"name": f"{release['source_repository']}/{name}",
                            "newName": f"{args.registry}/market/{name}", "digest": digest}
                           for name, digest in release["images"].items()]}
        if data["depends_on"]:
            spec["dependsOn"] = [{"name": f"market-{name}"} for name in data["depends_on"]]
        patches = []
        for obj in load(bundle / data["path"] / "resources.yaml"):
            kind, name = obj["kind"], obj["metadata"]["name"]
            if kind not in {"Deployment", "CronJob"}:
                continue
            containers = []
            for c in pod_template(obj)["containers"]:
                env = []
                # Older releases inherited the DSN from the ConfigMap.
                if (any(key.get("configMapRef", {}).get("name") == "market-config" for key in c.get("envFrom", []))
                        and not any(entry["name"] == "DATABASE_URL" for entry in c.get("env", []))):
                    env.append(env_ref("DATABASE_URL", "market-db-connection"))
                if any(entry["name"] == "OIDC_ISSUER" for entry in c.get("env", [])):
                    env.append({"name": "OIDC_ISSUER", "value": args.web_url})
                if name == "market-api":
                    env.extend(env_ref(key, src) for key, src in [
                        ("OIDC_SIGNING_KEY_PEM", "market-oidc-signing"),
                        ("OIDC_CREDENTIAL_ENCRYPTION_KEY", "market-oidc-credentials"),
                        ("SLA_GATEWAY_INGEST_SECRET", "market-sla-telemetry"),
                        ("SLA_PROBE_SECRET", "market-sla-telemetry")])
                if env:
                    containers.append({"name": c["name"], "env": env})
            template = {"template": {"spec": {"containers": containers}}} if containers else {}
            pod_spec = {"jobTemplate": {"spec": template}} if template and kind == "CronJob" else template
            if name == "message-center-worker":
                pod_spec["replicas"] = 1
            if name == "sla-calendar-sync":
                pod_spec["suspend"] = True
            if pod_spec:
                patch = {"apiVersion": obj["apiVersion"], "kind": kind, "metadata": {"name": name}, "spec": pod_spec}
                patches.append({"target": {"kind": kind, "name": name}, "patch": yaml.safe_dump(patch, sort_keys=False)})
        if patches:
            spec["patches"] = patches
        flux.append({"apiVersion": "kustomize.toolkit.fluxcd.io/v1", "kind": "Kustomization",
                     "metadata": {"name": f"market-{stage}", "namespace": "flux-system"}, "spec": spec})
    dump(root / "market-flux.yaml", flux)
    infrastructure_count = sum(obj["kind"] in {"Deployment", "CronJob"} for obj in infrastructure)
    print(f"Rendered {len(claims)} PVCs, {infrastructure_count} infrastructure deployments/jobs, "
          f"and {len(flux)-1} application stages for {release['version']}")


if __name__ == "__main__":
    os.umask(0o077)
    p = argparse.ArgumentParser()
    p.add_argument("--root", type=Path, default=Path("."))
    p.add_argument("--registry", required=True)
    p.add_argument("--oci-digest", required=True)
    p.add_argument("--pod-cidr", required=True)
    p.add_argument("--web-url", required=True)
    p.add_argument("--gateway-url", required=True)
    p.add_argument("--clamav-seeded", action="store_true")
    add_database_options(p)
    try:
        make_site(p.parse_args())
    except (ValueError, OSError) as error:
        p.exit(2, f"Site configuration failed: {error}\n")
