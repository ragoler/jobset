# Single image for ALL THREE roles (controller | leader | worker). The role is
# chosen at runtime by the command:
#   * controller -> uvicorn controller:app   (this image's default CMD)
#   * leader     -> python3 leader.py        (set by the JobSet pod spec)
#   * worker     -> python3 worker.py        (set by the JobSet pod spec)
# Keeping one image means the leader and workers run the EXACT same Monte Carlo
# code, and the controller shares the spec-construction module.
FROM python:3.11-slim

WORKDIR /app

COPY app/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Application code + the playroom UI. Build context is the REPO ROOT (so the
# frontend/ dir, a sibling of app/, can be baked in for standalone UI serving).
# All modules live flat in /app so `python3 leader.py` / `worker.py` and the
# controller's imports (montecarlo, jobset_spec) resolve without a package.
COPY app/montecarlo.py app/jobset_spec.py app/leader.py app/worker.py app/controller.py ./
COPY frontend ./frontend
ENV PYTHONPATH=/app PYTHONUNBUFFERED=1

EXPOSE 8000

# Default = the controller. JobSet overrides the command for the leader/worker
# pods, so this CMD only applies to the controller Deployment.
CMD ["uvicorn", "controller:app", "--host", "0.0.0.0", "--port", "8000"]
