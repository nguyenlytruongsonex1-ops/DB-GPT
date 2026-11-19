# Mockup Bootstrap 5 cho giao diện D-Bot

Đây là bộ giao diện tĩnh để review trước khi sửa `demo/index.html`. Mockup không gọi API, không tạo workload PostgreSQL và không gọi LLM.

## Màn hình

1. [`index.html`](index.html) — LIVE sẵn sàng, chọn một trong năm kịch bản.
2. [`live-running.html`](live-running.html) — LIVE đang chạy, workflow E2E là trọng tâm.
3. [`live-result.html`](live-result.html) — kết quả, bằng chứng, hội thoại hội chẩn đa bot và báo cáo sau khi cleanup thành công.
4. [`offline.html`](offline.html) — replay dữ liệu đã lưu; không tạo workload mới.
5. [`history.html`](history.html) — lịch sử có lọc, nguồn dữ liệu và phân trang.

Các trang dùng bản Bootstrap 5.3.3 đã lưu cục bộ trong `bootstrap.min.css` và một lớp theme mỏng trong `mockups.css`. Đây là mockup bố cục, chưa phải frontend đã tích hợp API.

Ảnh review nhanh nằm trong `screenshots/`: `live-ready.png`, `live-running.png`, `live-result.png`, `live-result-consultation-desktop.png`, `live-result-consultation-mobile.png`, `offline.png` và `history.png`.

## Điểm cần review

- LIVE có nổi bật hơn Offline và Lịch sử không?
- Năm kịch bản có đủ dễ chọn nhưng không chiếm toàn bộ màn hình không?
- Workflow `Workload → Metric → Alert → Webhook → D-Bot → Report → Cleanup` có đọc được trong vài giây không?
- Ở trạng thái đang chạy, người xem có biết hệ thống đang ở bước nào không?
- Ở màn hình kết quả, kết luận và bằng chứng có xuất hiện trước log/JSON không?
- Hội thoại đa bot có cho thấy ai nêu bằng chứng, ai phản biện và hệ thống chốt đồng thuận ra sao mà không làm chìm kết luận chính không?
- Tên nút, tab, trạng thái và nguồn dữ liệu có đủ rõ cho người và browser/AI agent không?

## Phạm vi chưa thiết kế

- Các biến thể lỗi chi tiết như timeout, webhook không đến hoặc cleanup thất bại.
- Nội dung report dài và đồ thị metric hoàn chỉnh.
- Logic tải dữ liệu, polling, stop/cleanup và phân trang thật.
