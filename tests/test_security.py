SETTINGS = {"schedule_cron": "0 * * * *", "worker_concurrency": "2"}


async def test_cross_site_post_is_rejected(client):
    r = await client.post("/settings", data=SETTINGS, headers={"Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403


async def test_post_with_foreign_origin_is_rejected(client):
    r = await client.post("/settings", data=SETTINGS, headers={"Origin": "http://evil.example"})
    assert r.status_code == 403


async def test_same_origin_post_works(client):
    r = await client.post(
        "/settings", data=SETTINGS, headers={"Origin": "http://test", "Sec-Fetch-Site": "same-origin"}
    )
    assert r.status_code == 303


async def test_post_without_origin_headers_works(client):
    assert (await client.post("/settings", data=SETTINGS)).status_code == 303


async def test_cross_site_get_is_not_blocked(client):
    assert (await client.get("/", headers={"Sec-Fetch-Site": "cross-site"})).status_code == 200
