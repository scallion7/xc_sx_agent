"""Authenticated dataset/candidate control plane; credentials come from the environment."""

import argparse
import json
import os
from pathlib import Path

import httpx


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--output")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("datasets")
    commands.add_parser("candidates")
    publish = commands.add_parser("freeze")
    publish.add_argument("file")
    export = commands.add_parser("export")
    export.add_argument("project_id")
    capture = commands.add_parser("capture")
    capture.add_argument("run_id")
    capture.add_argument("--classification", required=True)
    curate = commands.add_parser("curate")
    curate.add_argument("candidate_id")
    curate.add_argument("file", help="JSON: expected_revision, query, notes")
    args = parser.parse_args()
    token = os.environ.get("EVALUATION_ADMIN_TOKEN")
    if not token:
        parser.error(
            "Set EVALUATION_ADMIN_TOKEN; never pass credentials on the command line"
        )
    path, method, body = "/v1/admin/evaluation/", "GET", None
    if args.command in {"datasets", "candidates"}:
        path += args.command
    elif args.command == "export":
        path += "datasets/" + args.project_id
    elif args.command == "capture":
        path += "candidates"
        method, body = (
            "POST",
            {"run_id": args.run_id, "classification": args.classification},
        )
    elif args.command == "freeze":
        path += "datasets"
        method, body = "POST", json.loads(Path(args.file).read_text())
    else:
        path += "candidates/" + args.candidate_id
        method, body = "PUT", json.loads(Path(args.file).read_text())
    response = httpx.request(
        method,
        args.url.rstrip("/") + path,
        headers={"X-API-Key": token},
        json=body,
        timeout=60,
    )
    response.raise_for_status()
    text = json.dumps(response.json(), ensure_ascii=False, indent=2) + "\n"
    if args.output:
        Path(args.output).write_text(text)
    else:
        print(text, end="")


if __name__ == "__main__":
    main()
