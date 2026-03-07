Đánh giá CODING_AGENT_PROMPT.mdNhững điểm tốt:

Cấu trúc project rõ ràng, tách biệt responsibility tốt
Coding standards chặt chẽ (type hints, docstring, logging, async-first)
Pydantic models được define sẵn rất chi tiết
Test coverage plan cho từng component
Đề xuất tối ưu:
Tách models.py riêng — Prompt nói "define trong server.py hoặc runtime/models.py" nhưng nên bắt buộc tách riêng để tránh circular imports và dễ maintain.

Config nên có actions_dir field — Thay vì hardcode logic "nếu node-0 thì load seed/actions/", nên thêm field actions_dir trong node.yaml để bất kỳ node nào cũng tự khai báo actions path. Linh hoạt hơn nhiều.

Thiếu health check endpoint — Nên thêm GET /health trả về node status, uptime, số actions loaded. Rất cần cho monitoring và orchestration.

Thiếu graceful shutdown — Cần xử lý SIGTERM/SIGINT để cleanup running jobs trước khi tắt.

File I/O nên wrap asyncio.to_thread() — write_file và read_file tuy sync nhưng vẫn là blocking I/O. Trong async-first architecture, nên wrap để không block event loop.

Resolver cache nên dùng cachetools.TTLCache — Thay vì tự implement TTL cache + lock, dùng library đã battle-tested sẽ đáng tin cậy hơn.

Thiếu CORS middleware — Cloud AI Planner gọi qua HTTP, cần CORS cho cross-origin.

Thiếu request_id tracking — Mỗi incoming request nên có unique request_id để trace xuyên suốt.