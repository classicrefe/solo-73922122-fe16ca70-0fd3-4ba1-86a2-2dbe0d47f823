"""统计包测试：CSV 解析校验、Pearson 系数、制包/合并 API、失败路径与跨重启持久性。"""
from __future__ import annotations

import hashlib
import io
import statistics

import pytest

from app import main as main_mod
from app.stats import (
    CsvFormatError,
    ReleaseStats,
    parse_release_csv,
    pearson_from_stats,
)

HEADER = "station,ph,conductivity_us_cm,note"


def _csv_bytes(rows: list[str], header: str = HEADER) -> bytes:
    return ("\n".join([header, *rows]) + "\n").encode("utf-8")


def _publish(client, station: str, order: str, data: bytes) -> int:
    r = client.post(
        "/releases",
        data={"station_id": station, "sample_date": "2026-11-18", "order_no": order},
        files={"file": ("day.csv", io.BytesIO(data), "text/csv")},
    )
    assert r.status_code == 201, r.text
    return r.json()["release"]["id"]


def _create_package(client, release_ids: list[int]) -> dict:
    r = client.post("/stat-packages", json={"release_ids": release_ids})
    assert r.status_code == 201, r.text
    return r.json()


def _merge(client, package_ids: list[int]) -> dict:
    r = client.post("/stat-packages/merge", json={"package_ids": package_ids})
    assert r.status_code == 201, r.text
    return r.json()


def _direct_pearson(rows: list[tuple[float, float]]) -> float | None:
    """对原始行直接统计（float 参考值），用于与包的精确结果交叉验证。"""
    if len(rows) < 2:
        return None
    xs = [r[0] for r in rows]
    ys = [r[1] for r in rows]
    if len(set(xs)) == 1 or len(set(ys)) == 1:
        return None
    return statistics.correlation(xs, ys)


# --------------------------------------------------------------------------- #
# 解析与校验（纯函数）
# --------------------------------------------------------------------------- #
def test_parse_counts_only_complete_rows():
    data = _csv_bytes(
        [
            "S1,7.0,100",      # 有效
            "S2,,200",         # ph 空 -> 不计
            "S3,6.5,",         # 电导率空 -> 不计
            "S4,,",            # 均空 -> 不计
            "S5,8.25,1500.5",  # 有效
        ]
    )
    stats = parse_release_csv(data)
    assert stats.n == 2
    assert stats.sx == int((7.0 + 8.25) * 1_000_000)
    assert stats.sy == int((100 + 1500.5) * 1_000_000)


def test_parse_accepts_boundary_and_six_decimal_values():
    data = _csv_bytes(["S1,0,0", "S2,14,1000000", "S3,7.123456,0.000001"])
    stats = parse_release_csv(data)
    assert stats.n == 3


def test_parse_tolerates_bom_and_extra_columns_and_short_rows():
    data = (
        "﻿station,ph,extra,conductivity_us_cm\n"
        "S1,7.0,x,100\n"
        "S2,6.5\n"          # 短行：电导率按空处理 -> 不计
        "S3,.5,y,2.5\n"     # 省略前导零的小数
    ).encode("utf-8")
    stats = parse_release_csv(data)
    assert stats.n == 2


@pytest.mark.parametrize(
    "cell,col",
    [
        ("14.0000001", "ph"),          # pH 超上限
        ("15", "ph"),                  # pH 超上限
        ("-0.5", "ph"),                # 负数不在 0~14
        ("1000000.000001", "conductivity_us_cm"),  # 电导率超上限
        ("1000001", "conductivity_us_cm"),
        ("7.1234567", "ph"),           # 超过六位小数
        ("abc", "ph"),                 # 非数值
        ("1e3", "conductivity_us_cm"), # 科学计数法不是十进制小数写法
        ("7.", "ph"),                  # 小数点后无数字
        ("+7", "ph"),                  # 不接受符号
    ],
)
def test_parse_rejects_bad_values(cell: str, col: str):
    ph = cell if col == "ph" else "7.0"
    cond = cell if col == "conductivity_us_cm" else "100"
    with pytest.raises(CsvFormatError):
        parse_release_csv(_csv_bytes([f"S1,{ph},{cond}"]))


def test_parse_rejects_missing_columns_and_bad_encoding_and_empty():
    with pytest.raises(CsvFormatError):
        parse_release_csv(b"station,ph\nS1,7.0\n")  # 缺 conductivity_us_cm
    with pytest.raises(CsvFormatError):
        parse_release_csv(b"station,conductivity_us_cm\nS1,100\n")  # 缺 ph
    with pytest.raises(CsvFormatError):
        parse_release_csv(
            "station,ph,conductivity_us_cm,备注\nS1,7.0,100,河流\n".encode("gbk")
        )  # 非 UTF-8 编码
    with pytest.raises(CsvFormatError):
        parse_release_csv(b"")  # 无表头
    with pytest.raises(CsvFormatError):
        parse_release_csv(b'station,ph,conductivity_us_cm\n"S1,7.0,100\n')  # 引号未闭合


def test_pearson_null_rules():
    assert pearson_from_stats(ReleaseStats()) is None  # 0 行
    one = parse_release_csv(_csv_bytes(["S1,7.0,100"]))
    assert pearson_from_stats(one) is None  # 不足两行
    const_x = parse_release_csv(_csv_bytes(["S1,7.0,100", "S2,7.0,200"]))
    assert pearson_from_stats(const_x) is None  # ph 恒定
    const_y = parse_release_csv(_csv_bytes(["S1,6.0,100", "S2,7.0,100"]))
    assert pearson_from_stats(const_y) is None  # 电导率恒定


def test_pearson_perfect_linear_relationship():
    stats = parse_release_csv(
        _csv_bytes(["S1,6.0,100", "S2,7.0,200", "S3,8.0,300"])
    )
    assert pearson_from_stats(stats) == pytest.approx(1.0)
    neg = parse_release_csv(_csv_bytes(["S1,6.0,300", "S2,7.0,200", "S3,8.0,100"]))
    assert pearson_from_stats(neg) == pytest.approx(-1.0)


# --------------------------------------------------------------------------- #
# 制包 API
# --------------------------------------------------------------------------- #
def test_create_package_and_query(app_client):
    client, _, _ = app_client
    rows = [("6.5", "120.5"), ("7.0", "130"), ("7.5", "140.25"), ("8.0", "150")]
    rid1 = _publish(client, "SP-A", "ORD-SP-1", _csv_bytes([f"S,{p},{c}" for p, c in rows]))
    rid2 = _publish(
        client, "SP-B", "ORD-SP-2",
        _csv_bytes(["S,6.8,128.5", "S,,999", "S,7.2,"]),  # 仅 1 行有效
    )

    pkg = _create_package(client, [rid1, rid2, rid1])  # 请求内重复序号去重
    assert pkg["release_ids"] == sorted([rid1, rid2])
    assert pkg["row_count"] == 5
    expected = _direct_pearson(
        [(float(p), float(c)) for p, c in rows] + [(6.8, 128.5)]
    )
    assert pkg["pearson_r"] == pytest.approx(expected, abs=1e-12)

    # 按包号查询与列表查询结果一致。
    got = client.get(f"/stat-packages/{pkg['package_id']}")
    assert got.status_code == 200 and got.json() == pkg
    listed = client.get("/stat-packages").json()
    assert any(p["package_id"] == pkg["package_id"] for p in listed)


def test_create_package_unknown_release_404_and_no_partial_package(app_client):
    client, _, _ = app_client
    before = len(client.get("/stat-packages").json())
    rid = _publish(client, "SP-C", "ORD-SP-3", _csv_bytes(["S,7.0,100"]))
    r = client.post("/stat-packages", json={"release_ids": [rid, 999999]})
    assert r.status_code == 404
    assert len(client.get("/stat-packages").json()) == before  # 整次失败
    assert client.get("/stat-packages/999999").status_code == 404


def test_create_package_rejects_empty_and_invalid_ids(app_client):
    client, _, _ = app_client
    assert client.post("/stat-packages", json={"release_ids": []}).status_code == 422
    assert client.post("/stat-packages", json={"release_ids": [0]}).status_code == 422
    assert client.post("/stat-packages", json={"release_ids": [-3]}).status_code == 422
    assert client.post("/stat-packages/merge", json={"package_ids": []}).status_code == 422


def test_create_package_format_error_fails_entirely(app_client):
    client, _, _ = app_client
    good = _publish(client, "SP-D", "ORD-SP-4", _csv_bytes(["S,7.0,100"]))
    bad = _publish(client, "SP-E", "ORD-SP-5", _csv_bytes(["S,99,100"]))  # pH 超界
    before = len(client.get("/stat-packages").json())
    r = client.post("/stat-packages", json={"release_ids": [good, bad]})
    assert r.status_code == 422 and "格式错误" in r.json()["detail"]
    assert len(client.get("/stat-packages").json()) == before
    # 单独对合格原件制包不受影响。
    assert _create_package(client, [good])["row_count"] == 1


# --------------------------------------------------------------------------- #
# 合并：去重、异单同字节、与直接统计一致、合并包可再合并
# --------------------------------------------------------------------------- #
def test_merge_dedups_releases_and_matches_direct_stats(app_client):
    client, _, _ = app_client
    spec = {
        "r1": [("6.1", "100.25"), ("6.9", "110.5"), ("7.4", "121")],
        "r2": [("7.7", "131.75"), ("8.2", "142.125")],
        "r3": [("5.9", "95.5"), ("6.4", "104"), ("7.1", "118.5"), ("8.8", "160")],
    }
    ids = {
        k: _publish(client, f"SP-M-{k}", f"ORD-M-{k}",
                    _csv_bytes([f"S,{p},{c}" for p, c in rows]))
        for k, rows in spec.items()
    }
    pkg_a = _create_package(client, [ids["r1"], ids["r2"]])
    pkg_b = _create_package(client, [ids["r2"], ids["r3"]])  # 与 A 重叠 r2

    merged = _merge(client, [pkg_a["package_id"], pkg_b["package_id"]])
    assert merged["release_ids"] == sorted(ids.values())  # r2 只计一次
    assert merged["row_count"] == 3 + 2 + 4

    all_rows = [tuple(map(float, rc)) for rows in spec.values() for rc in rows]
    assert merged["pearson_r"] == pytest.approx(
        _direct_pearson(all_rows), abs=1e-12
    )

    # 与“对去重后的全部序号一次性制包”逐位一致（精确整数和，非近似）。
    one_shot = _create_package(client, sorted(ids.values()))
    assert merged["pearson_r"] == one_shot["pearson_r"]
    assert merged["row_count"] == one_shot["row_count"]

    # 合出的新包可继续合并，且与直接统计仍一致。
    pkg_c = _create_package(client, [ids["r1"]])
    again = _merge(client, [merged["package_id"], pkg_c["package_id"]])
    assert again["release_ids"] == sorted(ids.values())
    assert again["pearson_r"] == one_shot["pearson_r"]


def test_merge_same_bytes_different_releases_count_separately(app_client):
    client, _, _ = app_client
    data = _csv_bytes(["S,6.0,100", "S,7.0,200", "S,8.0,300"])
    rid1 = _publish(client, "SP-SAME-1", "ORD-SAME-1", data)
    rid2 = _publish(client, "SP-SAME-2", "ORD-SAME-2", data)  # 异单同字节
    assert rid1 != rid2
    pkg = _create_package(client, [rid1, rid2])
    assert pkg["row_count"] == 6  # 两行序号分别计入
    # 合并含同一原件的两个包：各自序号仍分别计入，同一序号才只计一次。
    pkg2 = _create_package(client, [rid1])
    merged = _merge(client, [pkg["package_id"], pkg2["package_id"]])
    assert merged["release_ids"] == sorted([rid1, rid2])
    assert merged["row_count"] == 6


def test_merge_unknown_package_404(app_client):
    client, _, _ = app_client
    before = len(client.get("/stat-packages").json())
    assert client.post(
        "/stat-packages/merge", json={"package_ids": [999999]}
    ).status_code == 404
    assert len(client.get("/stat-packages").json()) == before


# --------------------------------------------------------------------------- #
# 缺件/摘要不符：整次失败；已有包仍可查、可合并；合并不重读原件
# --------------------------------------------------------------------------- #
def test_missing_blob_blocks_create_but_packages_survive(app_client):
    client, _, _ = app_client
    ok_rows = _csv_bytes(["S,6.0,100", "S,7.0,200", "S,8.0,300"])
    rid_ok = _publish(client, "SP-OK", "ORD-OK-1", ok_rows)
    rid_gone = _publish(client, "SP-GONE", "ORD-GONE-1", ok_rows)
    digest = hashlib.sha256(ok_rows).hexdigest()

    existing = _create_package(client, [rid_ok])
    merged_before = _merge(client, [existing["package_id"]])

    # 模拟共享卷缺件（两单同字节，同一 blob 被删）。
    blob_path = main_mod.blob_store.path_for(digest)
    blob_path.unlink()
    try:
        before = len(client.get("/stat-packages").json())
        r = client.post("/stat-packages", json={"release_ids": [rid_gone]})
        assert r.status_code == 503 and "整次制包失败" in r.json()["detail"]
        assert len(client.get("/stat-packages").json()) == before

        # 已有包仍可查询、可合并（合并不重读原件）。
        got = client.get(f"/stat-packages/{existing['package_id']}")
        assert got.status_code == 200 and got.json()["row_count"] == 3
        merged = _merge(
            client, [existing["package_id"], merged_before["package_id"]]
        )
        assert merged["release_ids"] == [rid_ok]
        assert merged["row_count"] == 3
        assert merged["pearson_r"] == pytest.approx(1.0)
    finally:
        blob_path.parent.mkdir(parents=True, exist_ok=True)
        blob_path.write_bytes(ok_rows)
    assert main_mod.database.integrity_problems(
        main_mod.blob_store.verify_present
    ) == []


def test_digest_mismatch_blocks_create(app_client):
    client, _, _ = app_client
    data = _csv_bytes(["S,7.0,100"])
    rid = _publish(client, "SP-TAMP", "ORD-TAMP-1", data)
    digest = hashlib.sha256(data).hexdigest()
    target = main_mod.blob_store.path_for(digest)
    target.write_bytes(data + b"tampered")  # 卷上字节被改 -> 摘要不符
    try:
        before = len(client.get("/stat-packages").json())
        r = client.post("/stat-packages", json={"release_ids": [rid]})
        assert r.status_code == 503
        assert len(client.get("/stat-packages").json()) == before
    finally:
        target.write_bytes(data)
    assert main_mod.database.integrity_problems(
        main_mod.blob_store.verify_present
    ) == []


# --------------------------------------------------------------------------- #
# 持久性：换数据库连接（等价重启/另一实例）后包仍可查
# --------------------------------------------------------------------------- #
def test_packages_persist_across_connections(app_client):
    client, _, settings = app_client
    rid = _publish(client, "SP-PERSIST", "ORD-PERSIST-1",
                   _csv_bytes(["S,6.0,100", "S,7.0,200"]))
    pkg = _create_package(client, [rid])

    from app.db import Database
    from app.stats import total_of

    fresh = Database(settings)  # 新连接等价于另一实例/重启后的视角
    reloaded = fresh.get_stat_package(pkg["package_id"])
    assert reloaded is not None
    assert sorted(reloaded["items"]) == [rid]
    assert total_of(reloaded["items"]).n == 2
