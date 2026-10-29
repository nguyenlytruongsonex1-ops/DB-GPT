# D-Bot (DB-GPT) chạy local bằng Docker

Chẩn đoán lỗi PostgreSQL bằng LLM (OpenRouter). Chạy được trên Mac M4.

## Chạy

```bash
cp .env.example .env        # điền OPENAI_API_KEY = key OpenRouter
docker compose up -d --build
docker compose exec dbot python run_diagnose.py --anomaly_file test_cases/testing_cases_1.json
```

Lần build đầu mất khoảng 10 đến 20 phút. Mỗi lần chẩn đoán mất khoảng 3 đến 4 phút.

## Kết quả

Báo cáo nằm trong `alert_results/GPT4-0613/<timestamp>.jsonl`, ở trường `report`.
Muốn thử ca khác thì đổi sang `testing_cases_2..5.json`.

## Địa chỉ

- Prometheus: http://localhost:9090
- Alertmanager: http://localhost:9093
- Postgres: `localhost:5432`

## Đổi model

Sửa `LLM_MODEL` trong `.env` (mặc định `openai/gpt-4o-mini`), rồi chạy `docker compose up -d`.

## Lỗi hay gặp

- Chẩn đoán chạy mãi: trong log có `401` là key sai. Kiểm tra `OPENAI_API_KEY`.
- `Cannot connect to the Docker daemon`: Docker Desktop chưa bật.
- Tắt: `docker compose down`.
