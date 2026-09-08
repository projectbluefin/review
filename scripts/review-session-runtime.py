#!/usr/bin/env python3
"""Build Kubernetes resources for a foreground review dashboard session."""

from __future__ import annotations

import json
import sys
from typing import Any, Sequence


NAMESPACE = "bluefin-system"


def secret_environment_refs(
    secret_name: str, env_names: Sequence[str]
) -> list[dict[str, Any]]:
    return [
        {
            "name": name,
            "valueFrom": {
                "secretKeyRef": {"name": secret_name, "key": name}
            },
        }
        for name in env_names
    ]


def build_kubernetes_dashboard_pod(
    session_id: str,
    image: str,
    args: Sequence[str],
    secret_name: str,
    state_claim: str,
    env_names: Sequence[str],
) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": f"review-queue-{session_id}",
            "namespace": NAMESPACE,
            "labels": {
                "app.kubernetes.io/name": "review-queue",
                "app.kubernetes.io/component": "dashboard",
                "app.kubernetes.io/part-of": "review",
            },
        },
        "spec": {
            "automountServiceAccountToken": False,
            "restartPolicy": "Never",
            "securityContext": {
                "runAsNonRoot": True,
                "runAsUser": 1000,
                "runAsGroup": 1000,
                "fsGroup": 1000,
                "fsGroupChangePolicy": "OnRootMismatch",
                "seccompProfile": {"type": "RuntimeDefault"},
            },
            "containers": [
                {
                    "name": "dashboard",
                    "image": image,
                    "imagePullPolicy": "Always",
                    "args": list(args),
                    "stdin": True,
                    "stdinOnce": True,
                    "tty": True,
                    "env": secret_environment_refs(secret_name, env_names),
                    "securityContext": {
                        "allowPrivilegeEscalation": False,
                        "capabilities": {"drop": ["ALL"]},
                        "runAsNonRoot": True,
                        "runAsUser": 1000,
                        "runAsGroup": 1000,
                        "seccompProfile": {"type": "RuntimeDefault"},
                    },
                    "resources": {
                        "requests": {"cpu": "500m", "memory": "1Gi"},
                        "limits": {"cpu": "2", "memory": "4Gi"},
                    },
                    "volumeMounts": [
                        {"name": "workspace", "mountPath": "/home/dev/workspace"},
                        {
                            "name": "state",
                            "mountPath": "/home/dev/.local/state/bluefin-review",
                        },
                    ],
                }
            ],
            "volumes": [
                {"name": "workspace", "emptyDir": {}},
                {
                    "name": "state",
                    "persistentVolumeClaim": {"claimName": state_claim},
                },
            ],
        },
    }


def kubectl_create_args(pod: dict[str, Any]) -> list[str]:
    del pod
    return ["create", "-f", "-"]


def main(argv: Sequence[str] | None = None) -> int:
    values = list(sys.argv[1:] if argv is None else argv)
    if len(values) < 5:
        raise SystemExit(
            "usage: review-session-runtime.py SESSION IMAGE SECRET STATE_CLAIM ENV_NAMES [DASHBOARD_ARGS...]"
        )
    session_id, image, secret_name, state_claim, env_names, *args = values
    pod = build_kubernetes_dashboard_pod(
        session_id,
        image,
        args,
        secret_name,
        state_claim,
        [name for name in env_names.split(",") if name],
    )
    print(json.dumps(pod, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
