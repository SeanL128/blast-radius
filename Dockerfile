# Same base image the commits were labeled in.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
COPY requirements.lock /app/requirements.lock
RUN pip install --no-cache-dir -r /app/requirements.lock
ARG SQLGLOT_SHA=eb11aec4be8cb2122afe713a4816662c3f0fb2fa
RUN python - <<'PY'
import io, tarfile, urllib.request, os
sha = os.environ.get("SQLGLOT_SHA", "eb11aec4be8cb2122afe713a4816662c3f0fb2fa")
data = urllib.request.urlopen(f"https://github.com/tobymao/sqlglot/archive/{sha}.tar.gz").read()
with tarfile.open(fileobj=io.BytesIO(data)) as tf:
    for m in tf.getmembers():
        parts = m.name.split("/", 1)
        if len(parts) < 2 or not parts[1]:
            continue
        m.name = parts[1]
        tf.extract(m, "/opt/sqlglot", filter="data")
PY
RUN python -m compileall -q /opt/sqlglot/sqlglot /opt/sqlglot/tests && chmod -R a+rX /opt/sqlglot
COPY selector.py map.json.gz /app/
RUN chmod -R a+rX /app
CMD ["python", "/app/selector.py"]
