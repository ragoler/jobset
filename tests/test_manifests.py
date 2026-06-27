"""Manifest + descriptor checks: every ${VAR} is declared, refs are consistent.

Mirrors the Hub pre-merge checklist so a broken descriptor fails locally.
"""

import pathlib
import re

import pytest

yaml = pytest.importorskip("yaml")

ROOT = pathlib.Path(__file__).resolve().parents[1]
INFRA = ROOT / "infra"

# Hub-provided variables (see feature.md §3).
HUB_VARS = {
    "NAMESPACE", "PROJECT_NAME", "REGION", "ARTIFACT_REGISTRY_REPO",
    "GOOGLE_GENAI_USE_VERTEXAI", "OPENAI_API_BASE", "GCS_MODEL_BUCKET",
}


def _feature():
    return yaml.safe_load((ROOT / "feature.yaml").read_text())


def _docs(path):
    return [d for d in yaml.safe_load_all(path.read_text()) if d]


def test_feature_yaml_required_keys():
    f = _feature()
    for key in ("name", "paths", "deployment_name", "gateway"):
        assert key in f, f"feature.yaml missing {key}"
    assert f["name"] == "jobset"
    assert f["gateway"]["name"] == "jobset-gw"
    assert f["gateway"]["class"] == "gke-l7-global-external-managed"


def test_exactly_one_ui_model():
    f = _feature()
    has_playroom = "frontend_dir" in f["paths"] and "playroom_slug" in f["paths"]
    has_linkout = "entrypoint_service" in f
    assert has_playroom and not has_linkout, "must be hub-hosted playroom only"


def test_hub_router_declared():
    assert _feature().get("hub_router") == "hub_router:router"


def test_every_var_is_hub_standard_or_defaulted():
    """Grep all manifests for ${VAR}; each must be Hub-standard or defaulted."""
    f = _feature()
    declared = HUB_VARS | set(f.get("template_defaults", {}).keys())
    pattern = re.compile(r"\$\{([A-Z_]+)\}")
    missing = {}
    for path in list(INFRA.glob("*.yaml")) + list((ROOT / "cluster").rglob("*.yaml")):
        for var in pattern.findall(path.read_text()):
            if var not in declared:
                missing.setdefault(path.name, set()).add(var)
    assert not missing, f"undeclared template vars: {missing}"


def test_deployment_name_matches_descriptor():
    f = _feature()
    name = f["deployment_name"]
    found = any(
        doc.get("kind") == "Deployment" and doc["metadata"]["name"] == name
        for path in INFRA.glob("*.yaml")
        for doc in _docs(path)
    )
    assert found, f"no Deployment named {name}"


def test_gateway_name_matches_descriptor():
    f = _feature()
    gw = f["gateway"]["name"]
    names = [
        doc["metadata"]["name"]
        for path in INFRA.glob("*.yaml")
        for doc in _docs(path)
        if doc.get("kind") == "Gateway"
    ]
    assert gw in names, f"Gateway {gw} not found (found {names})"


def test_no_hardcoded_default_namespace():
    """Resource namespaces must be templated, never literally 'default'."""
    for path in INFRA.glob("*.yaml"):
        for doc in _docs(path):
            ns = doc.get("metadata", {}).get("namespace")
            if ns is not None:
                assert ns == "${NAMESPACE}", f"{path.name}: hardcoded namespace {ns}"


def test_httproute_is_filter_free_to_controller():
    route = next(
        d for d in _docs(INFRA / "http-route.yaml") if d.get("kind") == "HTTPRoute"
    )
    backends = {
        b["name"]
        for rule in route["spec"]["rules"]
        for b in rule.get("backendRefs", [])
    }
    assert backends == {"jobset-controller"}
    assert all("filters" not in rule for rule in route["spec"]["rules"])


def test_rbac_grants_required_verbs():
    """The controller Role must allow jobsets, jobs, pods(+log+delete), services."""
    role = next(d for d in _docs(INFRA / "rbac.yaml") if d.get("kind") == "Role")
    grants = {}
    for rule in role["rules"]:
        for group in rule["apiGroups"]:
            for res in rule["resources"]:
                grants.setdefault((group, res), set()).update(rule["verbs"])
    assert {"create", "delete"} <= grants[("jobset.x-k8s.io", "jobsets")]
    assert {"create", "delete"} <= grants[("batch", "jobs")]
    assert {"get", "list", "delete"} <= grants[("", "pods")]
    assert "get" in grants[("", "pods/log")]
    assert {"create", "delete"} <= grants[("", "services")]


def test_leader_service_selects_jobset_leader():
    svcs = [d for d in _docs(INFRA / "service.yaml") if d.get("kind") == "Service"]
    leader = next(s for s in svcs if s["metadata"]["name"] == "jobset-leader")
    sel = leader["spec"]["selector"]
    assert sel["jobset.sigs.k8s.io/replicatedjob-name"] == "leader"
    assert leader["spec"]["ports"][0]["port"] == 9000
    assert leader["spec"]["publishNotReadyAddresses"] is True


def test_cluster_kustomization_pins_jobset_operator():
    kust = yaml.safe_load((ROOT / "cluster" / "jobset-operator" / "kustomization.yaml").read_text())
    res = "\n".join(kust["resources"])
    assert "kubernetes-sigs/jobset" in res
    # Pinned to a real released tag, not a moving ref.
    assert re.search(r"/v\d+\.\d+\.\d+/", res), "operator must be pinned to a release tag"


def test_computeclass_is_spot_cpu_with_autocreation():
    cc = yaml.safe_load((ROOT / "cluster" / "cpu-computeclass.yaml").read_text())
    assert cc["kind"] == "ComputeClass"
    assert cc["metadata"]["name"] == "jobset-cpu"
    assert cc["spec"]["nodePoolAutoCreation"]["enabled"] is True
    assert all(p.get("spot") for p in cc["spec"]["priorities"])
