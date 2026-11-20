#!/usr/bin/env bash
# 端到端演示：发布 -> 幂等重试 -> 冲突拒绝 -> 取件 -> 两类证明 -> 离线核验 -> 篡改拒绝。
# 用法：BASE=http://127.0.0.1:8001 bash scripts/demo.sh
set -euo pipefail

BASE="${BASE:-http://127.0.0.1:8001}"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

cat > "$WORK/day1.csv" <<'CSV'
station,time,ph,do
ST01,2026-11-16T08:00,7.2,8.1
CSV
cat > "$WORK/day2.csv" <<'CSV'
station,time,ph,do
ST02,2026-11-16T08:00,7.0,8.4
CSV

echo "== 1. 首次发布 =="
curl -s -X POST "$BASE/releases" \
  -F station_id=ST01 -F sample_date=2026-11-16 -F order_no=ORD-001 \
  -F file=@"$WORK/day1.csv" | python3 -m json.tool

echo "== 2. 同单同输入重试（期望 reused=true）=="
curl -s -X POST "$BASE/releases" \
  -F station_id=ST01 -F sample_date=2026-11-16 -F order_no=ORD-001 \
  -F file=@"$WORK/day1.csv" | python3 -m json.tool

echo "== 3. 同单异输入（期望 HTTP 409）=="
curl -s -w "\nHTTP %{http_code}\n" -X POST "$BASE/releases" \
  -F station_id=ST01 -F sample_date=2026-11-16 -F order_no=ORD-001 \
  -F file=@"$WORK/day2.csv"

echo "== 4. 第二件发布 =="
curl -s -X POST "$BASE/releases" \
  -F station_id=ST02 -F sample_date=2026-11-16 -F order_no=ORD-002 \
  -F file=@"$WORK/day2.csv" | python3 -m json.tool

echo "== 5. 下载并比对原件 =="
curl -s "$BASE/releases/ST01/2026-11-16/ORD-001/download" -o "$WORK/back.csv"
cmp "$WORK/day1.csv" "$WORK/back.csv" && echo "字节一致"

echo "== 6. 纳入证明与离线核验 =="
curl -s "$BASE/releases/ST01/2026-11-16/ORD-001/inclusion-proof" \
  > "$WORK/inclusion.json"
python3 -m app.verify inclusion \
  --proof "$WORK/inclusion.json" --file "$WORK/day1.csv"

echo "== 7. 追加证明（1 号 -> 最新）与离线核验 =="
curl -s "$BASE/checkpoints/1" > "$WORK/cp1.json"
curl -s "$BASE/checkpoints/latest" > "$WORK/cp-latest.json"
curl -s "$BASE/checkpoints/1/append-proof" > "$WORK/append.json"
python3 -m app.verify append \
  --proof "$WORK/append.json" \
  --old-checkpoint "$WORK/cp1.json" \
  --new-checkpoint "$WORK/cp-latest.json"

echo "== 8. 篡改原件（期望离线拒绝，退出码 1）=="
cp "$WORK/day1.csv" "$WORK/tampered.csv"
printf 'X' >> "$WORK/tampered.csv"
python3 -m app.verify inclusion \
  --proof "$WORK/inclusion.json" --file "$WORK/tampered.csv" || true

echo "== 9. 统计包：发布含 pH/电导率的 CSV 并制包 =="
cat > "$WORK/stat1.csv" <<'CSV'
station,ph,conductivity_us_cm
ST01,6.5,130
ST01,7.0,140
CSV
cat > "$WORK/stat2.csv" <<'CSV'
station,ph,conductivity_us_cm
ST02,7.5,150
ST02,8.0,160
ST02,,999
CSV
R1=$(curl -s -X POST "$BASE/releases" \
  -F station_id=ST01 -F sample_date=2026-11-18 -F order_no=ORD-STAT-1 \
  -F file=@"$WORK/stat1.csv" | python3 -c 'import json,sys; print(json.load(sys.stdin)["release"]["id"])')
R2=$(curl -s -X POST "$BASE/releases" \
  -F station_id=ST02 -F sample_date=2026-11-18 -F order_no=ORD-STAT-2 \
  -F file=@"$WORK/stat2.csv" | python3 -c 'import json,sys; print(json.load(sys.stdin)["release"]["id"])')
echo "发布序号：$R1, $R2（stat2.csv 第三行 pH 留空，不参与统计）"

PKG1=$(curl -s -X POST "$BASE/stat-packages" \
  -H 'Content-Type: application/json' -d "{\"release_ids\": [$R1]}")
echo "$PKG1" | python3 -m json.tool
PKG1_ID=$(echo "$PKG1" | python3 -c 'import json,sys; print(json.load(sys.stdin)["package_id"])')

PKG2=$(curl -s -X POST "$BASE/stat-packages" \
  -H 'Content-Type: application/json' -d "{\"release_ids\": [$R1, $R2]}")
PKG2_ID=$(echo "$PKG2" | python3 -c 'import json,sys; print(json.load(sys.stdin)["package_id"])')

echo "== 10. 合并统计包（$PKG1_ID 与 $PKG2_ID 重叠的序号 $R1 只计一次）=="
curl -s -X POST "$BASE/stat-packages/merge" \
  -H 'Content-Type: application/json' \
  -d "{\"package_ids\": [$PKG1_ID, $PKG2_ID]}" | python3 -m json.tool

echo "== 11. 按包号查询（另一实例端口同样可查，如 8002）=="
curl -s "$BASE/stat-packages/$PKG2_ID" | python3 -m json.tool
