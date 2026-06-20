"""Inference server scripts for the DiffusersEngine HTTP contract.

Server scripts run on the GPU pod and expose:
  POST /generate, GET /status/{id}, GET /artifacts/{name}, GET /health.

Each server is a self-contained module. The cfg's
``engine.diffusers.server_cmd`` points at the module to run.
"""
