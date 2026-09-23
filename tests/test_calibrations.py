"""设备指标校准配置测试：事务内区间不重叠校验 + 按时刻解析与线性计算。"""

import pytest

from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


def cal(device, metric, frm, to, offset=0.0, scale=1.0):
    body = {
        "device_id": device,
        "metric": metric,
        "valid_from": frm,
        "valid_to": to,
        "offset": offset,
        "scale": scale,
    }
    return body


def test_create_and_resolve_basic(client):
    # calibrated = raw * 2 + 1
    r = client.post(
        "/api/v1/calibrations",
        json=cal(
            "dev-1", "temperature",
            "2026-01-01T00:00:00Z", "2026-02-01T00:00:00Z",
            offset=1.0, scale=2.0,
        ),
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["id"] == 1
    assert body["valid_to"] == "2026-02-01T00:00:00Z"
    assert body["offset"] == 1.0 and body["scale"] == 2.0

    r = client.get(
        "/api/v1/devices/dev-1/calibrations/resolve",
        params={"metric": "temperature", "at": "2026-01-15T12:00:00Z", "value": 10.0},
    )
    assert r.status_code == 200
    out = r.json()
    assert out["calibrated_value"] == pytest.approx(21.0)
    assert out["calibration"]["id"] == 1
    assert out["value"] == 10.0


def test_half_open_boundaries(client):
    client.post(
        "/api/v1/calibrations",
        json=cal("dev-2", "ph", "2026-01-01T00:00:00Z", "2026-02-01T00:00:00Z", 0.0, 1.0),
    )
    # valid_from（含）生效
    out = client.get(
        "/api/v1/devices/dev-2/calibrations/resolve",
        params={"metric": "ph", "at": "2026-01-01T00:00:00Z", "value": 7.0},
    ).json()
    assert out["calibrated_value"] == pytest.approx(7.0)
    assert out["calibration"] is not None

    # valid_to（不含）已失效
    out = client.get(
        "/api/v1/devices/dev-2/calibrations/resolve",
        params={"metric": "ph", "at": "2026-02-01T00:00:00Z", "value": 7.0},
    ).json()
    assert out["calibration"] is None
    assert out["calibrated_value"] is None


def test_back_to_back_ranges_are_allowed_and_resolve_unique(client):
    # 端点相接（前段 to == 后段 from）不算重叠
    r1 = client.post(
        "/api/v1/calibrations",
        json=cal("dev-3", "temperature", "2026-01-01T00:00:00Z", "2026-02-01T00:00:00Z", 0.0, 1.0),
    )
    assert r1.status_code == 201
    r2 = client.post(
        "/api/v1/calibrations",
        json=cal("dev-3", "temperature", "2026-02-01T00:00:00Z", "2026-03-01T00:00:00Z", 5.0, 2.0),
    )
    assert r2.status_code == 201

    first = client.get(
        "/api/v1/devices/dev-3/calibrations/resolve",
        params={"metric": "temperature", "at": "2026-01-31T23:59:59Z", "value": 3.0},
    ).json()
    assert first["calibration"]["id"] == r1.json()["id"]
    assert first["calibrated_value"] == pytest.approx(3.0)

    second = client.get(
        "/api/v1/devices/dev-3/calibrations/resolve",
        params={"metric": "temperature", "at": "2026-02-01T00:00:00Z", "value": 3.0},
    ).json()
    assert second["calibration"]["id"] == r2.json()["id"]
    assert second["calibrated_value"] == pytest.approx(11.0)


@pytest.mark.parametrize(
    "frm,to",
    [
        ("2026-01-10T00:00:00Z", "2026-01-05T00:00:00Z"),  # 倒序
        ("2026-01-10T00:00:00Z", "2026-01-10T00:00:00Z"),  # 零长度
    ],
)
def test_invalid_range_rejected_422(client, frm, to):
    r = client.post("/api/v1/calibrations", json=cal("dev-x", "m", frm, to))
    assert r.status_code == 422


def test_scale_zero_rejected_422(client):
    r = client.post(
        "/api/v1/calibrations",
        json=cal("dev-x", "m", "2026-01-01T00:00:00Z", None, scale=0.0),
    )
    assert r.status_code == 422


@pytest.mark.parametrize(
    "new_from,new_to",
    [
        ("2025-12-15T00:00:00Z", "2026-01-15T00:00:00Z"),  # 头部重叠
        ("2026-01-15T00:00:00Z", "2026-02-15T00:00:00Z"),  # 尾部重叠
        ("2026-01-10T00:00:00Z", "2026-01-20T00:00:00Z"),  # 完全内含
        ("2025-12-01T00:00:00Z", "2026-03-01T00:00:00Z"),  # 完全包含
    ],
)
def test_overlapping_ranges_rejected_409(client, new_from, new_to):
    client.post(
        "/api/v1/calibrations",
        json=cal("dev-4", "temperature", "2026-01-01T00:00:00Z", "2026-02-01T00:00:00Z"),
    )
    r = client.post(
        "/api/v1/calibrations",
        json=cal("dev-4", "temperature", new_from, new_to),
    )
    assert r.status_code == 409, r.text


def test_open_ended_range_and_overlap(client):
    r = client.post(
        "/api/v1/calibrations",
        json=cal("dev-5", "pressure", "2026-01-01T00:00:00Z", None, offset=2.5),
    )
    assert r.status_code == 201
    assert r.json()["valid_to"] is None

    # 远未来仍生效
    out = client.get(
        "/api/v1/devices/dev-5/calibrations/resolve",
        params={"metric": "pressure", "at": "2030-01-01T00:00:00Z", "value": 100.0},
    ).json()
    assert out["calibrated_value"] == pytest.approx(102.5)

    # 与开放区间重叠 -> 409
    r = client.post(
        "/api/v1/calibrations",
        json=cal("dev-5", "pressure", "2025-01-01T00:00:00Z", "2026-06-01T00:00:00Z"),
    )
    assert r.status_code == 409
    # 开放区间之前、端点相接允许
    r = client.post(
        "/api/v1/calibrations",
        json=cal("dev-5", "pressure", "2025-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
    )
    assert r.status_code == 201


def test_isolation_between_device_and_metric(client):
    client.post(
        "/api/v1/calibrations",
        json=cal("dev-6", "temperature", "2026-01-01T00:00:00Z", "2026-02-01T00:00:00Z"),
    )
    # 不同指标可重叠
    r = client.post(
        "/api/v1/calibrations",
        json=cal("dev-6", "ph", "2026-01-01T00:00:00Z", "2026-02-01T00:00:00Z"),
    )
    assert r.status_code == 201
    # 不同设备可重叠
    r = client.post(
        "/api/v1/calibrations",
        json=cal("dev-7", "temperature", "2026-01-01T00:00:00Z", "2026-02-01T00:00:00Z"),
    )
    assert r.status_code == 201

    out = client.get(
        "/api/v1/devices/dev-6/calibrations/resolve",
        params={"metric": "ph", "at": "2026-01-10T00:00:00Z", "value": 1.0},
    ).json()
    assert out["calibration"] is not None and out["calibration"]["metric"] == "ph"


def test_no_configuration_returns_null(client):
    # 设备不存在 / 指标无配置 / 时间不匹配 一律 null（非 404）
    out = client.get(
        "/api/v1/devices/unknown-device/calibrations/resolve",
        params={"metric": "temperature", "at": "2026-01-10T00:00:00Z", "value": 1.0},
    ).json()
    assert out["calibration"] is None
    assert out["calibrated_value"] is None
    assert out["value"] == 1.0


def test_timezone_normalized_to_utc(client):
    # 用 +08:00 创建：[2026-01-01T00:00+08, 2026-01-02T00:00+08)
    # 即 UTC [2025-12-31T16:00Z, 2026-01-01T16:00Z)
    client.post(
        "/api/v1/calibrations",
        json=cal("dev-8", "temperature", "2026-01-01T00:00:00+08:00", "2026-01-02T00:00:00+08:00", 1.0, 1.0),
    )
    # 起点含：UTC 2025-12-31T16:00 生效
    out = client.get(
        "/api/v1/devices/dev-8/calibrations/resolve",
        params={"metric": "temperature", "at": "2025-12-31T16:00:00Z", "value": 0.0},
    ).json()
    assert out["calibration"] is not None
    # naive at 按 UTC 处理，同样落在区间内
    out = client.get(
        "/api/v1/devices/dev-8/calibrations/resolve",
        params={"metric": "temperature", "at": "2026-01-01T00:00:00", "value": 0.0},
    ).json()
    assert out["calibration"] is not None
    # 终点不含：UTC 2026-01-01T16:00 已失效
    out = client.get(
        "/api/v1/devices/dev-8/calibrations/resolve",
        params={"metric": "temperature", "at": "2026-01-01T16:00:00Z", "value": 0.0},
    ).json()
    assert out["calibration"] is None


def test_calibration_does_not_rewrite_stored_measurements(client):
    # 上报原始值 12.34，配置 2x+1 后，已提交载荷仍是原始值
    pkg = {
        "device_id": "dev-9",
        "seq": 1,
        "idempotency_key": "dev-9-1",
        "measurements": [
            {"metric": "temperature", "value": 12.34, "ts": "2026-01-10T00:00:00Z"}
        ],
    }
    assert client.post("/api/v1/packages", json=pkg).status_code == 200
    client.post(
        "/api/v1/calibrations",
        json=cal("dev-9", "temperature", "2026-01-01T00:00:00Z", "2026-02-01T00:00:00Z", 1.0, 2.0),
    )
    out = client.get(
        "/api/v1/devices/dev-9/calibrations/resolve",
        params={"metric": "temperature", "at": "2026-01-10T00:00:00Z", "value": 12.34},
    ).json()
    assert out["calibrated_value"] == pytest.approx(25.68)

    # 已提交的原始载荷未被改写
    export = client.post(
        "/api/v1/exports",
        json={"device_id": "dev-9", "from_seq": 1, "to_seq": 1},
    ).json()
    page = client.get(
        f"/api/v1/exports/{export['export_id']}/pages"
    ).text
    assert '"value": 12.34' in page
    assert "25.68" not in page


def test_missing_required_query_params(client):
    r = client.get(
        "/api/v1/devices/dev-1/calibrations/resolve",
        params={"metric": "temperature", "value": 1.0},  # 缺 at
    )
    assert r.status_code == 422
