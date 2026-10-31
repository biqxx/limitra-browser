from __future__ import annotations

from httpx import AsyncClient


async def test_job_lifecycle_and_idempotency(
    async_client: AsyncClient, auth_headers: dict[str, str]
):
    req_payload = {
        "provider": "ebay",
        "query": "laptop stand",
        "page": 1,
        "page_size": 10,
    }
    idempotency_key = "idemp_test_key_123"

    headers = {**auth_headers, "Idempotency-Key": idempotency_key}

    # 1. Create Job
    resp_create = await async_client.post("/api/v1/jobs/search", json=req_payload, headers=headers)
    assert resp_create.status_code == 202
    job_id = resp_create.json()["job_id"]
    assert job_id is not None

    # 2. Duplicate Submission with same idempotency key returns identical job_id
    resp_dup = await async_client.post("/api/v1/jobs/search", json=req_payload, headers=headers)
    assert resp_dup.status_code == 202
    assert resp_dup.json()["job_id"] == job_id

    # 3. Get Job Status
    resp_status = await async_client.get(f"/api/v1/jobs/{job_id}", headers=auth_headers)
    assert resp_status.status_code == 200
    status_data = resp_status.json()
    assert status_data["job_id"] == job_id
    assert status_data["status"] in ("queued", "running", "completed", "failed")
    assert "progress" in status_data
    assert "stage" in status_data["progress"]

    # 4. Cancel Job
    resp_cancel = await async_client.delete(f"/api/v1/jobs/{job_id}", headers=auth_headers)
    assert resp_cancel.status_code == 204

    # 5. Check Cancelled Status
    resp_after_cancel = await async_client.get(f"/api/v1/jobs/{job_id}", headers=auth_headers)
    assert resp_after_cancel.status_code == 200
    assert resp_after_cancel.json()["status"] in ("cancelled", "completed", "failed")
