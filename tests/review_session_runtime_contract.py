import importlib.util
import pathlib
import unittest


runtime_path = (
    pathlib.Path(__file__).resolve().parents[1]
    / "scripts"
    / "review-session-runtime.py"
)
runtime_spec = importlib.util.spec_from_file_location(
    "review_session_runtime", runtime_path
)
assert runtime_spec and runtime_spec.loader
runtime = importlib.util.module_from_spec(runtime_spec)
runtime_spec.loader.exec_module(runtime)
build_kubernetes_dashboard_pod = runtime.build_kubernetes_dashboard_pod
kubectl_create_args = runtime.kubectl_create_args


class ReviewSessionRuntimeContractTest(unittest.TestCase):
    def test_dashboard_pod_has_tty_secret_env_and_no_service_token(self):
        pod = build_kubernetes_dashboard_pod(
            "session-a",
            "ghcr.io/projectbluefin/review:stable",
            ["queue"],
            "review-session-a",
            "review-queue-state",
            ["GH_TOKEN", "GITHUB_COPILOT_TOKEN"],
        )

        self.assertIs(pod["spec"]["automountServiceAccountToken"], False)
        self.assertIs(pod["spec"]["containers"][0]["tty"], True)
        self.assertTrue(
            all("value" not in env for env in pod["spec"]["containers"][0]["env"])
        )

        self.assertEqual(pod["metadata"]["name"], "review-queue-session-a")
        self.assertEqual(pod["metadata"]["namespace"], "bluefin-system")
        self.assertEqual(pod["spec"]["restartPolicy"], "Never")
        self.assertEqual(
            pod["spec"]["securityContext"],
            {
                "runAsNonRoot": True,
                "runAsUser": 1000,
                "runAsGroup": 1000,
                "fsGroup": 1000,
                "fsGroupChangePolicy": "OnRootMismatch",
                "seccompProfile": {"type": "RuntimeDefault"},
            },
        )

        container = pod["spec"]["containers"][0]
        self.assertEqual(container["name"], "dashboard")
        self.assertEqual(container["image"], "ghcr.io/projectbluefin/review:stable")
        self.assertEqual(container["imagePullPolicy"], "Always")
        self.assertEqual(container["args"], ["queue"])
        self.assertIs(container["stdin"], True)
        self.assertIs(container["stdinOnce"], True)
        self.assertEqual(
            container["env"],
            [
                {
                    "name": "GH_TOKEN",
                    "valueFrom": {
                        "secretKeyRef": {
                            "name": "review-session-a",
                            "key": "GH_TOKEN",
                        }
                    },
                },
                {
                    "name": "GITHUB_COPILOT_TOKEN",
                    "valueFrom": {
                        "secretKeyRef": {
                            "name": "review-session-a",
                            "key": "GITHUB_COPILOT_TOKEN",
                        }
                    },
                },
            ],
        )
        self.assertEqual(
            container["securityContext"],
            {
                "allowPrivilegeEscalation": False,
                "capabilities": {"drop": ["ALL"]},
                "runAsNonRoot": True,
                "runAsUser": 1000,
                "runAsGroup": 1000,
                "seccompProfile": {"type": "RuntimeDefault"},
            },
        )
        self.assertEqual(
            container["resources"],
            {
                "requests": {"cpu": "500m", "memory": "1Gi"},
                "limits": {"cpu": "2", "memory": "4Gi"},
            },
        )
        self.assertEqual(
            container["volumeMounts"],
            [
                {"name": "workspace", "mountPath": "/home/dev/workspace"},
                {
                    "name": "state",
                    "mountPath": "/home/dev/.local/state/bluefin-review",
                },
            ],
        )
        self.assertEqual(
            pod["spec"]["volumes"],
            [
                {"name": "workspace", "emptyDir": {}},
                {
                    "name": "state",
                    "persistentVolumeClaim": {"claimName": "review-queue-state"},
                },
            ],
        )

    def test_kubectl_create_args_create_a_manifest_from_standard_input(self):
        pod = build_kubernetes_dashboard_pod(
            "session-a",
            "ghcr.io/projectbluefin/review:stable",
            ["queue", "--repo", "bluefin"],
            "review-session-a",
            "review-queue-state",
            ["GH_TOKEN"],
        )

        self.assertEqual(kubectl_create_args(pod), ["create", "-f", "-"])


if __name__ == "__main__":
    unittest.main()
