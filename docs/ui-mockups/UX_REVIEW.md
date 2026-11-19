# D-Bot — UX review cho mockup Bootstrap 5

## Hierarchy đề xuất

- **LIVE là điểm vào mặc định** và chiếm toàn bộ trọng tâm nội dung. Offline và Lịch sử là tab phụ, không trình bày như ba lựa chọn ngang hàng trên dashboard.
- Tab LIVE theo một đường đọc từ trên xuống: trạng thái môi trường/model và ghi chú chi phí → chọn một workload → một CTA chính → tiến trình E2E → kết quả/report → chi tiết kỹ thuật thu gọn.
- Giữ nguyên thứ tự workflow và luôn cho thấy trạng thái từng bước: **Workload → Metric → Alert → Webhook → D-Bot → Report → Cleanup**. Nêu rõ bước đang chạy, bước thành công, bước lỗi và cleanup chưa xác nhận.
- Kết quả tóm tắt đặt trước log/trace: E2E pass/fail, nguyên nhân gốc, bằng chứng liên quan, report. Phân biệt dữ liệu LIVE với replay/history và không trộn ground truth vào chẩn đoán.
- Offline và Lịch sử chỉ tải/render khi mở tab; lịch sử có tìm kiếm, trạng thái rỗng/lỗi/tải và phân trang.

## Màn hình cần có

1. **LIVE — sẵn sàng/chọn workload:** năm lựa chọn đơn, mô tả tác động/metric cần quan sát, trạng thái sẵn sàng của PostgreSQL/Prometheus/model và thông tin chi phí; một nút “Bắt đầu chẩn đoán”.
2. **LIVE — đang chạy/dừng:** stepper hoặc ordered list bảy bước với bước hiện tại; thời gian và thông điệp tiến độ; chỉ một nút hành động phù hợp trạng thái (Dừng khi chạy, không cho khởi chạy lượt thứ hai). Khi dừng, thể hiện rõ đang cleanup.
3. **LIVE — kết thúc:** kết quả E2E rõ ràng, gồm cả trường hợp diagnosis/report thành công nhưng cleanup thất bại; tóm tắt nguyên nhân và evidence, report đọc được, hành động chạy lại/xem chi tiết.
4. **Offline:** các replay có sẵn và trạng thái không có dữ liệu; ghi rõ đây là dữ liệu phát lại, không chạy workload.
5. **Lịch sử:** bảng báo cáo có thời gian, scenario, trạng thái, model/thời lượng; tìm kiếm, phân trang và mở report. Có loading/empty/error/retry.
6. **Chi tiết kỹ thuật (trong LIVE/report):** log, trace/JSON, metric chi tiết, cấu hình model và metadata trong accordion/disclosure đóng mặc định.

## Bootstrap 5 components phù hợp

- `nav nav-tabs` với semantics tab đầy đủ (`tablist`, `tab`, `tabpanel`), `aria-selected`, phím điều hướng; panel không hoạt động dùng `hidden`.
- `fieldset`/`legend` + `form-check` radio cho lựa chọn workload; `card` chỉ làm bố cục, không thay radio bằng vùng click không ngữ nghĩa.
- `btn btn-primary` cho CTA duy nhất; `btn-outline-secondary` hoặc `btn-outline-danger` cho hành động phụ/dừng. Đặt tên truy cập rõ, gắn scenario khi cần.
- `list-group` dạng ordered list hoặc stepper tự dựng bằng danh sách có thứ tự cho bảy bước; bước hiện tại dùng `aria-current="step"`, không chỉ đổi màu.
- `progress`/`progress-bar` cho tiến độ tổng thể kèm nhãn văn bản; `alert` cho lỗi/cảnh báo, `badge` cho trạng thái có cả chữ.
- `card`/`row`/`col` cho summary và report; `accordion` cho log/trace/JSON; `table-responsive` cho lịch sử, với điều khiển mở report riêng từng hàng.
- `spinner-border` chỉ kèm nội dung “Đang …”; `visually-hidden` cho nhãn bổ sung; dùng `toast` chỉ cho thông báo phụ không thay status chính.

## Lỗi cần tránh

- Cho Offline/Lịch sử cạnh tranh với LIVE; lặp lại danh sách scenario hoặc đặt nhiều CTA ngang cấp.
- Dùng card/div/dòng bảng có `onclick` làm điều khiển duy nhất; thiếu radio, label, focus bàn phím hoặc accessible name duy nhất.
- Dùng màu, icon, spinner hay phần trăm đơn lẻ để biểu thị trạng thái; gộp mọi lỗi thành “hoàn tất” hoặc báo pass khi cleanup chưa thành công.
- Đánh tráo thứ tự workflow; đưa step nội bộ D-Bot thay cho chuỗi E2E; giấu trạng thái webhook/report/cleanup.
- Mặc định phơi log/JSON dài, khiến nội dung chính bị đẩy xuống; dùng bảng/console khó đọc trên màn hình nhỏ.
- Đưa ground truth vào prompt/đầu vào chẩn đoán hoặc làm người xem nhầm replay với LIVE.
- Dùng selector tự động hóa dựa vào class CSS, vị trí, thứ tự hàng hay nội dung thay đổi; thiếu stable `data-testid`/`data-agent-id`.
- Render control của tab ẩn, tải lịch sử ở lần mở đầu, hoặc để polling tiếp tục sau trạng thái kết thúc.
- Chỉ thiết kế desktop: kiểm tra reflow, focus, tương phản và nội dung dài trên viewport hẹp.

## Checklist review

- [ ] LIVE là tab mặc định; Offline/Lịch sử là tab phụ và panel inactive bị ẩn khỏi accessibility tree.
- [ ] Có đúng một `h1`, landmark rõ, thứ tự DOM khớp thứ tự đọc và một CTA chính cho mỗi trạng thái.
- [ ] Năm scenario chọn được bằng radio/keyboard; mỗi lựa chọn và nút có tên truy cập phân biệt.
- [ ] Bảy bước E2E hiện đúng thứ tự; active step có ngữ nghĩa; tiến độ có nhãn chữ.
- [ ] Idle/running/stopping/success/error/cleanup-failure đều phân biệt được; không khởi chạy đồng thời ngoài ý muốn.
- [ ] Summary/report dễ tìm trước kỹ thuật; log/JSON thu gọn; LIVE, offline replay và history được gắn nhãn nguồn.
- [ ] Loading, empty, error/retry và stale state có thiết kế cho từng màn hình cần dữ liệu.
- [ ] Tab phụ lazy-load; history có phân trang; polling kết thúc ở mọi trạng thái cuối.
- [ ] Agent có thể thao tác bằng semantic controls và selector ổn định, không dựa vào tọa độ/vị trí.
- [ ] Bàn phím, focus, contrast, zoom/reflow và viewport mobile được review; không tràn ngang ở 390 × 844.
