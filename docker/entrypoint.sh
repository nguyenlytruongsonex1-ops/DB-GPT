#!/bin/sh
# Lần đầu: tạo knowledge base (embedding bge-small chạy CPU)
[ -f knowledge_base/info.db ] || python init_database.py --recreate-vs
exec "$@"
