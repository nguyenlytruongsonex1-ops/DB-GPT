FROM python:3.10-slim
# JRE cho module query_rewrite (jpype). Đặt APT_PKGS="" để bỏ qua.
ARG APT_PKGS="default-jre-headless g++"
RUN if [ -n "$APT_PKGS" ]; then apt-get update && apt-get install -y --no-install-recommends $APT_PKGS && rm -rf /var/lib/apt/lists/*; fi
WORKDIR /app
COPY docker/requirements.txt /tmp/req.txt
RUN pip install --no-cache-dir -r /tmp/req.txt
COPY . .
RUN python copy_config_example.py && python -c "from sentence_transformers import SentenceTransformer as S; S('sentence-transformers/all-mpnet-base-v2'); S('BAAI/bge-small-en-v1.5')"
ENTRYPOINT ["docker/entrypoint.sh"]
CMD ["sleep", "infinity"]
