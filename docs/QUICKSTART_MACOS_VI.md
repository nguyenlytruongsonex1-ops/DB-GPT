# Chạy D-Bot trên macOS

## Cần có

- macOS, Git và Docker Desktop cho Mac (Apple Silicon hoặc Intel); mở Docker Desktop và đợi trạng thái **Running**.
- Tài khoản OpenRouter có API key. Các lượt LIVE gọi model và có thể phát sinh phí.
- Dung lượng trống đủ để tải image và build các image lần đầu.

## Cài và cấu hình

Mở **Terminal**, rồi chạy:

~~~sh
git clone https://github.com/nguyenlytruongsonex1-ops/DB-GPT.git
cd DB-GPT
cp .env.example .env
open -e .env
~~~

Trong `.env`, thay giá trị mẫu bằng key của bạn:

~~~dotenv
OPENAI_API_KEY=<OpenRouter key>
DEMO_LLM_ENABLED=true
DEMO_LLM_API_KEY=<OpenRouter key>
~~~

Giữ nguyên `LLM_MODEL` và `DEMO_LLM_MODEL` nếu chưa muốn đổi model. Hai biến key phục vụ các luồng khác nhau; muốn chạy đủ LIVE + pipeline D-Bot thì cần bật `DEMO_LLM_ENABLED` và đặt `DEMO_LLM_API_KEY`. Không gửi key vào lệnh, log, ảnh chụp màn hình hoặc commit file `.env`.

Build và khởi động:

~~~sh
docker compose up -d --build
docker compose ps
~~~

Lần build đầu có thể mất thời gian. Kiểm tra các service đã chạy (đặc biệt `postgres_db`, `python_app`, `demo`) và trạng thái healthy nếu có healthcheck. Mở [http://localhost:8080](http://localhost:8080). API xác minh cấu hình LIVE là [http://localhost:8080/api/live/run/config](http://localhost:8080/api/live/run/config): cần thấy `llm_ready: true`, model và đủ năm ca. Nếu chưa đúng, sửa `.env`, rồi áp dụng lại:

~~~sh
docker compose up -d
~~~

## Chạy đủ 5 kịch bản LIVE

Trong giao diện, vào phần **LIVE — 5 kịch bản PostgreSQL thật**. Chạy **từng ca một**, theo thứ tự dưới đây; đợi ca hiện kết quả xong rồi mới bấm ca kế tiếp:

1. Ca 1 — `large_read` (đọc dữ liệu lớn).
2. Ca 2 — `bulk_delete` (xóa hàng loạt).
3. Ca 3 — `many_indexes` (nhiều index).
4. Ca 4 — `concurrent_writes` (ghi đồng thời).
5. Ca 5 — `slow_join` (join chậm).

Một ca chỉ **đạt** khi trace có `signal.observed: true`, cảnh báo Prometheus ở trạng thái firing, webhook được nhận, `diagnosis.returncode: 0`, có báo cáo D-Bot và `cleanup: "ok"`. Đọc báo cáo và đối chiếu phần nguyên nhân với đáp án chuẩn chỉ hiển thị để kiểm tra; đáp án chuẩn không được gửi vào đầu vào chẩn đoán. Nếu thiếu một tiêu chí, báo cáo, hoặc cleanup thất bại thì không tính ca đạt. Mỗi trace được lưu tại `alert_results/live_scenarios/<run_id>/<scenario>.json`; artifact báo cáo nằm dưới `alert_results/`.

## Log và dừng

Theo dõi log:

~~~sh
docker compose logs -f demo python_app dbot
~~~

Nhấn `Ctrl+C` để thoát chế độ theo dõi log (không dừng container). Dừng lượt LIVE bằng nút **Dừng** trong giao diện nếu cần; đợi quá trình dọn dữ liệu kết thúc. Dừng stack:

~~~sh
docker compose down
~~~

Lệnh trên giữ dữ liệu trong các thư mục gắn từ máy chủ và volume. **Không dùng `docker compose down -v` nếu muốn giữ dữ liệu.**
