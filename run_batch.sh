#!/usr/bin/env bash
# 启动 N 个分片并行下载 CSV 中的文献 PDF。
# 用法: ./run_batch.sh [N] [CSV] [OUT]
set -euo pipefail
N="${1:-6}"
CSV="${2:-data/csv-ultrasound-set.csv}"
OUT="${3:-downloads}"
BASE_PORT=9320
cd "$(dirname "$0")"
mkdir -p "$OUT/logs"
pkill -x chrome 2>/dev/null || true
rm -rf /tmp/pubmed_dl_chrome_${BASE_PORT}*
for i in $(seq 0 $((N-1))); do
  .venv/bin/python -u -m pubmed_dl run --csv "$CSV" --out "$OUT" \
      --shard "$i/$N" --port "$BASE_PORT" >> "$OUT/logs/shard$i.log" 2>&1 &
  echo "started shard $i/$N (pid $!)"
done
wait
