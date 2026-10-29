# D-Bot (DB-GPT) chạy local bằng Docker

Dùng LLM (gọi qua OpenRouter) để chẩn đoán lỗi PostgreSQL. Đã chạy thử trên Mac mini M4.

![Kiến trúc](docs/architecture.png)

Bản PDF: [docs/DBGPT_architecture_vi.pdf](docs/DBGPT_architecture_vi.pdf)

## Chạy nhanh

```bash
cp .env.example .env        # điền OPENAI_API_KEY = key OpenRouter
docker compose up -d --build
docker compose exec dbot python run_diagnose.py --anomaly_file test_cases/testing_cases_1.json
```

## Hướng dẫn từng bước cho người mới

1. **Cài Docker Desktop** rồi mở app lên. Đợi đến khi biểu tượng cá voi báo "running".
2. **Lấy key OpenRouter** tại https://openrouter.ai/keys. Nạp vài USD vào tài khoản.
3. **Tải code về:**
   ```bash
   git clone https://github.com/nguyenlytruongsonex1-ops/DB-GPT.git && cd DB-GPT
   ```
4. **Tạo file `.env`:** chạy `cp .env.example .env`, mở file ra và thay `sk-or-xxxx` bằng key của bạn. Không commit file này lên git.
5. **Build và chạy:** `docker compose up -d --build`. Lần đầu mất khoảng 10 đến 20 phút.
6. **Kiểm tra:** `docker compose ps`. Cả 7 service đều phải ở trạng thái `Up`.
7. **Chẩn đoán thử:** chạy lệnh thứ 3 ở mục trên, mất khoảng 3 đến 4 phút. Thấy dòng `Diagnose Finished!` là xong.
8. **Xem báo cáo:** mở file mới nhất trong `alert_results/GPT4-0613/`. Trường `report` là báo cáo dạng Markdown.
9. **Tắt:** `docker compose down`.

## Các service

| Service | Cổng | Vai trò | Cách dùng |
|---|---|---|---|
| `postgres_db` | 5432 | PostgreSQL 12 cần chẩn đoán, có bật `pg_stat_statements` | Kết nối bằng `psql -h localhost -U test sysbench`, mật khẩu `Test123_456` |
| `node_exporter` | 9100 | Đo CPU, RAM, IO, load của máy | http://localhost:9100/metrics |
| `pg_exporter` | 9187 | Đo số liệu của DB: kết nối, lock, query… | http://localhost:9187/metrics |
| `prometheus` | 9090 | Thu metric từ 2 exporter, áp luật cảnh báo | http://localhost:9090: mục Graph để vẽ metric, Status → Targets để kiểm tra exporter, Alerts để xem cảnh báo |
| `alertmanager` | 9093 | Gom cảnh báo rồi gửi webhook | http://localhost:9093 để xem cảnh báo đang bật |
| `python_app` | 8023 | Nhận webhook từ Alertmanager và ghi lại ca bất thường | Chạy ngầm, xem log bằng `docker compose logs -f python_app` |
| `dbot` | — | D-Bot: chọn expert, gọi tool và LLM, viết báo cáo | `docker compose exec dbot python run_diagnose.py --anomaly_file <file>` |

**Các ca mẫu:** từ `test_cases/testing_cases_1.json` đến `testing_cases_5.json`. Mỗi file là một ca bất thường đã có sẵn metric.

**Luật cảnh báo:** `prometheus_service_docker/node_rules.yml` và `pgsql_rules.yml`.

## Đổi model

Sửa `LLM_MODEL` trong `.env`, mặc định là `openai/gpt-4o-mini`, rồi chạy `docker compose up -d`. Nên dùng model mạnh vì prompt gốc viết cho GPT-4.

## Lỗi hay gặp

- **Chẩn đoán chạy mãi không xong:** xem log, nếu có `401` thì key sai. Kiểm tra `OPENAI_API_KEY`.
- **`Cannot connect to the Docker daemon`:** Docker Desktop chưa bật.
- **Build lỗi ở `pip install`:** kiểm tra mạng rồi chạy lại `docker compose build`.
- **Xem log một service:** `docker compose logs -f <service>`.

## Giới hạn

- Các ca mẫu dùng metric có sẵn trong file. Luồng tự động từ cảnh báo thật đến chẩn đoán chưa được nối hoàn chỉnh.
- Chưa có giao diện web, hiện chỉ chạy qua dòng lệnh.
