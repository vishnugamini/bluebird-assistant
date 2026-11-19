#!/usr/bin/env python3
"""Deploy Bluebird to a Cloud Run Instance. Run from the project root."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time

import httpx

from assistant.cli import load_env
from assistant.config import Settings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--memory", choices=("1Gi", "2Gi"), default="1Gi")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--prepare", action="store_true", help="Enable APIs and create the base project resources.")
    mode.add_argument("--image", help="Deploy an existing Bluebird image with the provisioned secrets.")
    parser.add_argument("--without-telegram", action="store_true", help="Keep the existing poller active during cloud verification.")
    args = parser.parse_args()
    load_env()
    settings = Settings.from_env()
    settings.require_runtime_keys()
    project = os.getenv("GCP_PROJECT", "").strip()
    if not project:
        parser.error("Set GCP_PROJECT to your Google Cloud project ID.")
    region = os.getenv("GCP_REGION", "us-west1")
    instance = os.getenv("GCP_INSTANCE", "bluebird")
    bucket = settings.bucket or f"{project}-bluebird-state"
    repository = "bluebird"
    image_repository = f"{region}-docker.pkg.dev/{project}/{repository}/assistant"
    if args.image and not args.image.startswith((f"{image_repository}:", f"{image_repository}@sha256:")):
        parser.error("--image must be in the Bluebird Artifact Registry repository.")
    account = f"{instance}@{project}.iam.gserviceaccount.com"
    credentials = {
        "bluebird-openai": settings.openai_key.encode(),
        "bluebird-telegram": settings.telegram_token.encode(),
        "bluebird-gateway": settings.gateway_key.encode(),
        "bluebird-internal": settings.internal_key.encode(),
        "bluebird-tailnet": os.getenv("TAILSCALE_AUTHKEY", "").encode(),
        "bluebird-google": settings.google_token_file.read_bytes() if settings.google_token_file.is_file() else b"",
    }
    if not args.prepare:
        missing = [name for name, value in credentials.items() if not value]
        if missing:
            raise SystemExit("Configure these credentials before deploying: " + ", ".join(missing))
        if not settings.ollama_url.startswith("https://") or not settings.ollama_url.rstrip("/").endswith(".ts.net"):
            raise SystemExit("Set A3_OLLAMA_URL to the Mac's private HTTPS Tailscale Serve URL.")
        if not settings.telegram_chat.isdigit() or not settings.telegram_user.isdigit():
            raise SystemExit("Set Telegram owner and home channel IDs to the authorized private chat.")
        if not settings.test_email or not settings.calendar_id or settings.email_allowlist != (settings.test_email.lower(),):
            raise SystemExit("Set the dedicated mailbox and calendar. Initially allow only that mailbox as a recipient.")

    local = Path(".runtime/google-cloud-sdk/bin/gcloud").resolve()
    executable = shutil.which("gcloud") or (str(local) if local.is_file() else None)
    if not executable:
        raise SystemExit("Install Google Cloud CLI and its beta component.")
    if executable == str(local):
        os.environ.setdefault("CLOUDSDK_CONFIG", str(Path(".runtime/gcloud-config").resolve()))

    def gcloud(*words, capture=False, data=None, probe=False):
        result = subprocess.run([executable, *words, f"--project={project}", "--quiet"],
                                input=data, capture_output=capture or probe, check=not probe)
        if probe:
            return result.returncode == 0
        return result.stdout.decode().strip() if capture else None

    if not gcloud("auth", "list", "--filter=status:ACTIVE", "--format=value(account)", capture=True):
        raise SystemExit("Sign into the Google Cloud CLI first with gcloud auth login.")
    if gcloud("billing", "projects", "describe", project, "--format=value(billingEnabled)", capture=True).lower() != "true":
        raise SystemExit(f"Enable billing for {project} before deploying.")
    gcloud("services", "enable", "run.googleapis.com", "artifactregistry.googleapis.com",
           "cloudbuild.googleapis.com", "secretmanager.googleapis.com", "gmail.googleapis.com",
           "calendar-json.googleapis.com", "iam.googleapis.com", "storage.googleapis.com")
    if not gcloud("iam", "service-accounts", "describe", account, probe=True):
        gcloud("iam", "service-accounts", "create", instance, "--display-name=Bluebird assistant runtime")
    if not gcloud("artifacts", "repositories", "describe", repository, f"--location={region}", probe=True):
        gcloud("artifacts", "repositories", "create", repository, "--repository-format=docker", f"--location={region}")
    if not gcloud("storage", "buckets", "describe", f"gs://{bucket}", probe=True):
        gcloud("storage", "buckets", "create", f"gs://{bucket}", f"--location={region}",
               "--uniform-bucket-level-access", "--public-access-prevention")
    gcloud("storage", "buckets", "add-iam-policy-binding", f"gs://{bucket}", f"--member=serviceAccount:{account}",
           "--role=roles/storage.objectUser")
    if args.prepare:
        print(f"Prepared APIs, registry, service account, and gs://{bucket}.")
        return
    image = args.image
    if image is None:
        build_account = gcloud("builds", "get-default-service-account", "--format=value(serviceAccountEmail)", capture=True)
        if "@" not in build_account:
            raise SystemExit("Cloud Build has no default service account.")
        build_account = build_account.rsplit("/", 1)[-1]
        staging = f"gs://{project}_cloudbuild"
        if not gcloud("storage", "buckets", "describe", staging, probe=True):
            gcloud("storage", "buckets", "create", staging, f"--location={region}",
                   "--uniform-bucket-level-access", "--public-access-prevention")
        for role in ("roles/storage.bucketViewer", "roles/storage.objectUser"):
            gcloud("storage", "buckets", "add-iam-policy-binding", staging, f"--member=serviceAccount:{build_account}",
                   f"--role={role}")
        gcloud("artifacts", "repositories", "add-iam-policy-binding", repository, f"--location={region}",
               f"--member=serviceAccount:{build_account}", "--role=roles/artifactregistry.writer")
        for name, data in credentials.items():
            if not gcloud("secrets", "describe", name, probe=True):
                gcloud("secrets", "create", name, "--replication-policy=automatic")
            gcloud("secrets", "versions", "add", name, "--data-file=-", data=data)
            gcloud("secrets", "add-iam-policy-binding", name, f"--member=serviceAccount:{account}",
                   "--role=roles/secretmanager.secretAccessor")
        tag = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        image = f"{image_repository}:{tag}"
        gcloud("builds", "submit", ".", f"--tag={image}", "--ignore-file=.gcloudignore",
               f"--gcs-source-staging-dir={staging}/source", f"--gcs-log-dir={staging}/logs")
    environment = {
        "A3_BUCKET": bucket, "A3_CLOUD_MODEL": settings.cloud_model, "A3_LOCAL_MODEL": settings.local_model,
        "A3_OLLAMA_URL": settings.ollama_url, "A3_TEST_EMAIL": settings.test_email, "A3_CALENDAR_ID": settings.calendar_id,
        "A3_EMAIL_ALLOWLIST": ",".join(settings.email_allowlist), "TELEGRAM_ALLOWED_USERS": settings.telegram_user,
        "TELEGRAM_HOME_CHANNEL": settings.telegram_chat, "A3_TIMEZONE": settings.timezone,
    }
    bindings = ["OPENAI_API_KEY=bluebird-openai:latest",
                "API_SERVER_KEY=bluebird-gateway:latest", "A3_INTERNAL_KEY=bluebird-internal:latest",
                "/secrets/tailscale/key=bluebird-tailnet:latest", "/secrets/google/token.json=bluebird-google:latest"]
    if not args.without_telegram:
        bindings.append("TELEGRAM_BOT_TOKEN=bluebird-telegram:latest")
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml") as config:
        json.dump(environment, config)
        config.flush()
        gcloud("beta", "run", "instances", "deploy", instance, f"--region={region}", f"--image={image}",
               f"--service-account={account}", "--cpu=1", f"--memory={args.memory}", "--port=8080",
               "--restart-policy=always", "--public", f"--env-vars-file={config.name}",
               "--set-secrets=" + ",".join(bindings),
               "--startup-probe=httpGet.path=/health,httpGet.port=8080,periodSeconds=5,timeoutSeconds=4,failureThreshold=48")
    instance_url = gcloud("beta", "run", "instances", "describe", instance, f"--region={region}",
                          "--format=value(status.urls[0])", capture=True)
    deadline = time.monotonic() + 360
    with httpx.Client(timeout=8, follow_redirects=False) as client:
        while time.monotonic() < deadline:
            try:
                response = client.get(instance_url.rstrip("/") + "/health")
                if response.status_code == 200 and response.json() == {"status": "ok"}:
                    print(instance_url)
                    return
            except (httpx.HTTPError, ValueError):
                pass
            time.sleep(5)
    raise SystemExit("The instance update was accepted, but Hermes has not passed its health check. Inspect Cloud Run logs.")


if __name__ == "__main__":
    main()
