FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/opt/sqlglot SKIP_INTEGRATION=0 OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 PYTHON_CPU_COUNT=2
WORKDIR /app
COPY requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock
# The base image ships no stdlib bytecode and the run is read-only, so every
# interpreter would recompile unittest, argparse and friends from source.
RUN python -m compileall -q -x '/site-packages/' /usr/local/lib/python3.13 || true
ADD https://codeload.github.com/tobymao/sqlglot/tar.gz/eb11aec4be8cb2122afe713a4816662c3f0fb2fa /tmp/sqlglot.tar.gz
RUN python -c "import tarfile; assert tarfile.open('/tmp/sqlglot.tar.gz').pax_headers['comment'] == 'eb11aec4be8cb2122afe713a4816662c3f0fb2fa'"
RUN mkdir -p /opt/sqlglot && tar -xzf /tmp/sqlglot.tar.gz -C /opt/sqlglot --strip-components=1 && rm /tmp/sqlglot.tar.gz
RUN python -m compileall -q /opt/sqlglot/sqlglot /opt/sqlglot/tests
COPY . /app/
RUN python -m compileall -q /app || true
RUN chmod -R a+rX /app /opt/sqlglot /usr/local/lib/python3.13
CMD ["python", "/app/diag6.py"]
