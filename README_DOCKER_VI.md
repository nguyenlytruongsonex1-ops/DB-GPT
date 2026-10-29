# D-Bot (DB-GPT) chạy local bằng Docker + OpenRouter

Chạy được trên Mac M1/M4 (arm64) và x86. Không cần model local: LLM gọi qua OpenRouter, embedding `bge-small-en-v1.5` chạy CPU (đã tải sẵn trong image).

Gồm: `dbot` (D-Bot), `postgres_db` (PostgreSQL 12 + pg_stat_statements), `prometheus`, `alertmanager`, `node_exporter`, `pg_exporter`, `python_app` (nhận alert).

## Chạy

```bash
cp .env.example .env            # điền OPENAI_API_KEY = key OpenRouter, chọn LLM_MODEL
docker compose up -d --build    # lần đầu build ~5-10 phút
docker compose exec dbot python run_diagnose.py --anomaly_file test_cases/testing_cases_1.json
```

Lệnh 3 chạy một chẩn đoán thử (khoảng 3 phút) trên ca mẫu "load average cao". Kết quả ở `alert_results/GPT4-0613/<timestamp>.jsonl`, phần báo cáo nằm ở trường `report` (Markdown). Có thể đổi sang `testing_cases_2..5.json`.

Kiểm tra: Prometheus ở http://localhost:9090/targets, Postgres ở `localhost:5432` (user `test`, mật khẩu `Test123_456`, db `sysbench`).

## Ghi chú
- `LLM_MODEL` là tên model trên OpenRouter, ví dụ `openai/gpt-4o-mini`. Prompt gốc được viết cho GPT-4, nên model nhỏ có thể trả sai định dạng.
- Ca mẫu dùng metric có sẵn trong file test (`enable_prometheus=False`). Muốn chẩn đoán trên metric thật thì cần sửa code (xem `multiagents/our_argparse.py`).
- Không commit `.env`.
