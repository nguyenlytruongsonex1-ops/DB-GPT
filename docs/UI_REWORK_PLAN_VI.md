# Kế hoạch rework giao diện demo D-Bot

## Mục tiêu

Đưa **LIVE** thành hành trình chính. Người xem chọn một kịch bản, theo dõi workflow E2E và đọc kết quả mà không bị Offline, Lịch sử, JSON hay log cạnh tranh sự chú ý. Giao diện phải rõ với cả người dùng và browser/AI agent.

Offline và Lịch sử chuyển thành tab phụ, chỉ tải dữ liệu khi người dùng mở tab.

## Bằng chứng từ giao diện hiện tại

- Trang gốc khoảng **33 KB** và phản hồi nhanh trong môi trường local; nút thắt chính không nằm ở tốc độ trả HTML.
- Trang gọi `/api/history` ngay khi mở, tải khoảng **25 KB / 113 báo cáo**, dù người dùng chưa vào Lịch sử.
- `/api/live/run/config` bị gọi hai lần trong quá trình khởi tạo.
- LIVE, Offline và Lịch sử cùng tồn tại trên một trang dài. Năm kịch bản bị trình bày lặp lại, còn các vùng log, JSON và báo cáo kỹ thuật xuất hiện quá gần luồng chính.
- Nhiều phần tử tương tác dựa vào card hoặc dòng bảng có `onclick`; tên hành động chưa gắn rõ với đối tượng nên AI agent phải dựa vào vị trí và đoán ngữ cảnh.

Kết luận: cần giảm **tải nhận thức**, accessibility tree và dữ liệu tải sớm. Không cần đổi framework chỉ để giải quyết vấn đề này.

## Kiến trúc thông tin đích

Ba tab ở cấp cao nhất:

1. **LIVE** — mặc định và là màn hình chính.
2. **Offline** — chỉ render và tải dữ liệu khi mở.
3. **Lịch sử** — chỉ tải khi mở; có phân trang.

Tab LIVE có một luồng dọc duy nhất:

1. Trạng thái hệ thống và model.
2. Chọn một trong năm kịch bản.
3. Một nút chính: **Bắt đầu chẩn đoán**.
4. Workflow: `Workload → Metric → Alert → Webhook → D-Bot → Report → Cleanup`.
5. Kết quả: trạng thái E2E, root cause, hai bằng chứng chính và báo cáo.
6. Chi tiết kỹ thuật: log, JSON, model, số lần gọi và đường dẫn artifact nằm trong disclosure đóng mặc định.

## State machine của LIVE

| Trạng thái | Nội dung chính | Hành động |
|---|---|---|
| `idle` | hệ thống sẵn sàng, năm lựa chọn | chọn kịch bản |
| `selected` | tóm tắt kịch bản và chi phí model | bắt đầu chẩn đoán |
| `starting` | đang khởi tạo workload | chưa cho chạy ca khác |
| `running` | bước hiện tại, thời gian, bằng chứng mới nhất | dừng lượt chạy |
| `stopping` | đang kết thúc workload và cleanup | không cho chạy lại |
| `success` | đủ các điều kiện E2E, root cause và báo cáo | chạy ca khác / xem chi tiết |
| `error` | bước lỗi, cleanup và hướng phục hồi | thử lại khi an toàn |

Một ca chỉ thành công khi trace xác nhận: signal đã được quan sát, alert Prometheus firing, webhook đã nhận, `diagnosis.returncode: 0`, có báo cáo và cleanup thành công.

## Hợp đồng cho browser/AI agent

- Dùng landmark và heading đúng nghĩa: `header`, `nav`, `main`, một `h1`, sau đó là các section theo đúng thứ tự đọc.
- Ba mode dùng `tablist`, `tab`, `tabpanel`. Tab không hoạt động phải `hidden` và không xuất hiện trong accessibility tree.
- Năm kịch bản là một nhóm lựa chọn đơn bằng radio hoặc pattern tương đương, không chỉ là card click được.
- Nút và link dùng phần tử native. Không dùng dòng bảng hoặc `div` làm mục tiêu click duy nhất.
- Mỗi hành động có accessible name duy nhất; ví dụ `Chạy LIVE: Ghi đồng thời`.
- Workflow dùng danh sách có thứ tự; bước hiện tại có `aria-current="step"`.
- Chỉ có một status region chính cho tiến trình; phân biệt rõ running, success, error, stopping và cleanup failure.
- Thêm selector ổn định cho các điểm chính: tab, lựa chọn kịch bản, nút chạy/dừng, status, workflow, result và technical details.
- DOM đi cùng thứ tự người dùng nhìn thấy. Không dùng vị trí, nội dung động hoặc class trình bày làm selector duy nhất.

Hợp đồng chi tiết đã được ghi trong `demo/AGENTS.md` để agent khác đọc trước khi sửa frontend.

## Kế hoạch kỹ thuật

### P0 — Tạo lại luồng chính

- Giữ FastAPI và JavaScript hiện có; tách `index.html`, CSS và JavaScript để giảm file đơn khối.
- Tạo shell ba tab, LIVE mặc định.
- Chuyển năm kịch bản thành một selection group và một CTA duy nhất.
- Đưa workflow E2E thật vào vùng trung tâm; không dùng stepper nội bộ D-Bot làm workflow chính.
- Gom JSON, log, expert timeline, knowledge và ground truth vào **Chi tiết kỹ thuật**.
- Cài state machine LIVE và ánh xạ từng API response vào trạng thái rõ ràng.

### P1 — Giảm tải ban đầu

- Bỏ `loadHist()` khỏi startup.
- Chỉ tải Offline và History khi tab tương ứng được mở lần đầu; cache dữ liệu đã tải.
- Gộp hoặc điều phối bootstrap để `/api/live/run/config` không bị gọi hai lần.
- Thêm phân trang cho `/api/history`, mặc định 25 dòng; trả tổng số dòng riêng.
- Chỉ polling khi có lượt chạy; dừng timer ở mọi terminal state và khi mất ownership.
- Có loading, empty, unavailable, retry và stale-data state cho từng tab.

### P2 — Khả năng truy cập và QA cho agent

- Thêm semantics, keyboard flow, focus management, unique accessible names và stable selectors.
- Thêm kiểm thử hợp đồng UI: tab ẩn không tải dữ liệu, chọn scenario, start/stop, tiến trình, success/error và pagination.
- Kiểm tra desktop và mobile 390 × 844; không overflow ngang và không có lỗi console.
- Chạy một bài kiểm tra browser-agent: xác định trang, chọn `concurrent_writes`, nhận ra CTA, đọc bước hiện tại và tìm kết quả mà không dùng tọa độ cố định.

## Tiêu chí nghiệm thu

- Khi mở trang, chỉ LIVE hiện và không có request tới `/api/history`.
- Người dùng thấy đúng một nhóm năm kịch bản và một CTA chính.
- Offline và Lịch sử chỉ tải một lần khi mở tab; Lịch sử không mount quá 25 dòng mỗi trang.
- Mọi control chính có tên truy cập duy nhất và dùng được bằng bàn phím.
- Accessibility tree không chứa control của tab ẩn.
- Workflow hiển thị đúng bước hiện tại và phân biệt được success, error, stopping và cleanup failure.
- Không có fetch bootstrap trùng, polling thừa, overflow ngang hoặc lỗi browser console.
- Browser/AI agent hoàn thành được hành trình chọn kịch bản → chạy → đọc trạng thái → tìm kết quả bằng semantic DOM và selector ổn định.

## Phạm vi không đổi

- Không thay đổi logic năm workload PostgreSQL và pipeline D-Bot chỉ để phục vụ trình bày.
- Không gửi ground truth vào đầu vào chẩn đoán.
- Không coi một báo cáo được render là thành công nếu trace chưa đủ điều kiện E2E.
