"""POST /unregistered-load 的端到端测试。"""

import json

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def base_payload(**overrides):
    """与 test_evaluate 相同的小树，外加实测电流列表：

    source --s1(0.1Ω, 20A)--> a --s2(0.2Ω, 10A)--> b
                              |
                              +--s3(0.05Ω, 15A)--> c

    基线（未舍入）段电流：s1=15, s2=5, s3=8
    """
    payload = {
        "source_voltage": 48.0,
        "min_voltage": 40.0,
        "segments": [
            {"id": "s1", "parent": "source", "child": "a", "resistance": 0.1, "rated_current": 20.0},
            {"id": "s2", "parent": "a", "child": "b", "resistance": 0.2, "rated_current": 10.0},
            {"id": "s3", "parent": "a", "child": "c", "resistance": 0.05, "rated_current": 15.0},
        ],
        "loads": [
            {"node": "a", "current": 2.0},
            {"node": "b", "current": 5.0},
            {"node": "c", "current": 8.0},
        ],
        "measurements": [],
    }
    payload.update(overrides)
    return payload


# ---------- 定位（located） ----------


def test_located_unique_candidate():
    """未登记负载 3A 挂在 b：s1/s2 测到 +3，s3 不变，唯一候选为 b。"""
    payload = base_payload(
        measurements=[
            {"segment_id": "s1", "current": 18.0},
            {"segment_id": "s2", "current": 8.0},
            {"segment_id": "s3", "current": 8.0},
        ]
    )
    resp = client.post("/unregistered-load", json=payload)
    assert resp.status_code == 200
    data = resp.json()

    assert data["status"] == "located"
    assert data["load_current"] == pytest.approx(3.0)
    assert data["candidate_nodes"] == ["b"]

    diffs = {m["segment_id"]: m["difference"] for m in data["measurements"]}
    assert diffs == {"s1": pytest.approx(3.0), "s2": pytest.approx(3.0), "s3": pytest.approx(0.0)}
    baselines = {m["segment_id"]: m["baseline"] for m in data["measurements"]}
    assert baselines == {"s1": pytest.approx(15.0), "s2": pytest.approx(5.0), "s3": pytest.approx(8.0)}


def test_unmeasured_branches_allow_multiple_candidates():
    """仅测 s1：a 的子树内所有节点均相容（未测支路允许多解）。"""
    payload = base_payload(measurements=[{"segment_id": "s1", "current": 17.0}])
    resp = client.post("/unregistered-load", json=payload)
    data = resp.json()

    assert data["status"] == "located"
    assert data["load_current"] == pytest.approx(2.0)
    assert data["candidate_nodes"] == ["a", "b", "c"]


def test_unmeasured_downstream_branch_keeps_descendant_candidates():
    """b 下挂未测支路 b->d：测 s1/s2 均 +2 时，b 与 d 均相容。"""
    payload = base_payload()
    payload["segments"].append(
        {"id": "s4", "parent": "b", "child": "d", "resistance": 0.1, "rated_current": 10.0}
    )
    payload["measurements"] = [
        {"segment_id": "s1", "current": 17.0},
        {"segment_id": "s2", "current": 7.0},
    ]
    resp = client.post("/unregistered-load", json=payload)
    data = resp.json()

    assert data["status"] == "located"
    assert data["load_current"] == pytest.approx(2.0)
    assert data["candidate_nodes"] == ["b", "d"]


def test_full_coverage_pins_intermediate_node():
    """负载挂在中间节点 a：全覆盖测量下 s2/s3 无增量，候选只剩 a。"""
    payload = base_payload(
        measurements=[
            {"segment_id": "s1", "current": 19.0},
            {"segment_id": "s2", "current": 5.0},
            {"segment_id": "s3", "current": 8.0},
        ]
    )
    resp = client.post("/unregistered-load", json=payload)
    data = resp.json()

    assert data["status"] == "located"
    assert data["load_current"] == pytest.approx(4.0)
    assert data["candidate_nodes"] == ["a"]


def test_positive_diffs_within_tolerance_are_consistent():
    """两个正差相差 5e-7（≤ 1e-6），判定一致并取均值。"""
    payload = base_payload(
        measurements=[
            {"segment_id": "s1", "current": 17.0000005},
            {"segment_id": "s2", "current": 7.0},
        ]
    )
    resp = client.post("/unregistered-load", json=payload)
    data = resp.json()

    assert data["status"] == "located"
    assert data["load_current"] == pytest.approx(2.0)  # 均值 2.00000025 六位小数为 2.0
    assert data["candidate_nodes"] == ["b"]


def test_load_current_rounded_to_six_decimals():
    payload = base_payload(measurements=[{"segment_id": "s1", "current": 16.23456789}])
    resp = client.post("/unregistered-load", json=payload)
    data = resp.json()

    assert data["status"] == "located"
    assert data["load_current"] == pytest.approx(1.234568)


def test_auto_generated_segment_ids_are_measurable():
    """线段缺省 id 自动生成 seg-<序号>，测点可引用。"""
    payload = base_payload(measurements=[{"segment_id": "seg-2", "current": 6.5}])
    for seg in payload["segments"]:
        del seg["id"]
    resp = client.post("/unregistered-load", json=payload)
    data = resp.json()

    assert data["status"] == "located"
    assert data["load_current"] == pytest.approx(1.5)
    assert data["candidate_nodes"] == ["b"]


# ---------- 无法确定（undetermined） ----------


def test_no_positive_diff_is_undetermined():
    payload = base_payload(
        measurements=[
            {"segment_id": "s1", "current": 15.0},
            {"segment_id": "s2", "current": 5.0},
        ]
    )
    resp = client.post("/unregistered-load", json=payload)
    data = resp.json()

    assert data["status"] == "undetermined"
    assert data["load_current"] is None
    assert data["candidate_nodes"] == []


def test_empty_measurements_is_undetermined():
    resp = client.post("/unregistered-load", json=base_payload())
    assert resp.status_code == 200
    assert resp.json()["status"] == "undetermined"


def test_diff_within_tolerance_is_not_positive():
    """差值 5e-7（≤ 1e-6）不算正差；轻微负差同样在容差内。"""
    payload = base_payload(
        measurements=[
            {"segment_id": "s1", "current": 15.0000005},
            {"segment_id": "s2", "current": 4.9999995},
        ]
    )
    resp = client.post("/unregistered-load", json=payload)
    assert resp.json()["status"] == "undetermined"


# ---------- 矛盾（inconsistent） ----------


def test_negative_diff_is_inconsistent():
    """实测明显低于基线（正负载不可能使电流变小）。"""
    payload = base_payload(measurements=[{"segment_id": "s2", "current": 4.0}])
    resp = client.post("/unregistered-load", json=payload)
    data = resp.json()

    assert data["status"] == "inconsistent"
    assert data["load_current"] is None
    assert data["candidate_nodes"] == []


def test_conflicting_positive_diffs_are_inconsistent():
    """s1 正差 2A 与 s2 正差 1A 相差超过 1e-6，互相矛盾。"""
    payload = base_payload(
        measurements=[
            {"segment_id": "s1", "current": 17.0},
            {"segment_id": "s2", "current": 6.0},
        ]
    )
    resp = client.post("/unregistered-load", json=payload)
    assert resp.json()["status"] == "inconsistent"


def test_divergent_branches_have_no_compatible_node():
    """s2 与 s3 同为正差：单一负载不可能同时位于两条支路，无相容节点。"""
    payload = base_payload(
        measurements=[
            {"segment_id": "s2", "current": 6.0},
            {"segment_id": "s3", "current": 9.0},
        ]
    )
    resp = client.post("/unregistered-load", json=payload)
    data = resp.json()

    assert data["status"] == "inconsistent"
    assert data["candidate_nodes"] == []


def test_positive_diff_contradicted_by_zero_reading_on_path():
    """s1 正差但 s2/s3 实测回到基线：负载不可能在两条支路下游，只能在 a。"""
    payload = base_payload(
        measurements=[
            {"segment_id": "s1", "current": 17.0},
            {"segment_id": "s2", "current": 5.0},
            {"segment_id": "s3", "current": 8.0},
        ]
    )
    resp = client.post("/unregistered-load", json=payload)
    data = resp.json()

    assert data["status"] == "located"
    assert data["candidate_nodes"] == ["a"]


# ---------- 输入校验 ----------


def test_duplicate_measurement_segment_rejected():
    payload = base_payload(
        measurements=[
            {"segment_id": "s1", "current": 16.0},
            {"segment_id": "s1", "current": 17.0},
        ]
    )
    resp = client.post("/unregistered-load", json=payload)
    assert resp.status_code == 400
    error = resp.json()["error"]
    assert error["code"] == "invalid_input"
    assert any("重复" in d["message"] for d in error["details"])


def test_unknown_measurement_segment_rejected():
    payload = base_payload(measurements=[{"segment_id": "ghost", "current": 1.0}])
    resp = client.post("/unregistered-load", json=payload)
    assert resp.status_code == 400
    error = resp.json()["error"]
    assert error["code"] == "invalid_input"
    assert any("不存在" in d["message"] for d in error["details"])


def test_negative_measurement_rejected():
    payload = base_payload(measurements=[{"segment_id": "s1", "current": -1.0}])
    resp = client.post("/unregistered-load", json=payload)
    assert resp.status_code == 422
    error = resp.json()["error"]
    assert error["code"] == "validation_error"
    assert any("实测电流不能为负" in d["message"] for d in error["details"])


@pytest.mark.parametrize("bad", [float("inf"), float("-inf"), float("nan")])
def test_non_finite_measurement_rejected(bad):
    # httpx 的 json= 序列化拒绝非有限数，改为手工序列化（服务端 json.loads 可解析）
    payload = base_payload(measurements=[{"segment_id": "s1", "current": bad}])
    resp = client.post(
        "/unregistered-load",
        content=json.dumps(payload),
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 422
    error = resp.json()["error"]
    assert error["code"] == "validation_error"
    assert any("有限数" in d["message"] for d in error["details"])


def test_missing_measurements_rejected():
    payload = base_payload()
    del payload["measurements"]
    resp = client.post("/unregistered-load", json=payload)
    assert resp.status_code == 422
    assert any("实测电流" in d["message"] for d in resp.json()["error"]["details"])


def test_missing_segment_id_rejected():
    payload = base_payload(measurements=[{"current": 1.0}])
    resp = client.post("/unregistered-load", json=payload)
    assert resp.status_code == 422
    assert any("线段标识" in d["message"] for d in resp.json()["error"]["details"])


def test_invalid_tree_still_rejected():
    payload = base_payload(measurements=[{"segment_id": "s1", "current": 16.0}])
    payload["segments"].append(
        {"parent": "x", "child": "y", "resistance": 0.1, "rated_current": 10.0}
    )
    resp = client.post("/unregistered-load", json=payload)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_input"


def test_unknown_load_node_still_rejected():
    payload = base_payload(measurements=[{"segment_id": "s1", "current": 16.0}])
    payload["loads"].append({"node": "ghost", "current": 1.0})
    resp = client.post("/unregistered-load", json=payload)
    assert resp.status_code == 400
    assert any("不存在" in d["message"] for d in resp.json()["error"]["details"])
