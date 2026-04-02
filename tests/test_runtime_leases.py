from simpleclaw.runtime.leases import LeaseRepository


def test_lease_acquire_and_release(tmp_path) -> None:
    repo = LeaseRepository(owner_id="owner-a", runtime_root=tmp_path)

    assert repo.acquire("heartbeat-tenant", "tenant-a", ttl_s=60) is True
    assert repo.is_active("heartbeat-tenant", "tenant-a") is True

    repo.release("heartbeat-tenant", "tenant-a")

    assert repo.is_active("heartbeat-tenant", "tenant-a") is False


def test_lease_blocks_other_owner_until_release(tmp_path) -> None:
    repo_a = LeaseRepository(owner_id="owner-a", runtime_root=tmp_path)
    repo_b = LeaseRepository(owner_id="owner-b", runtime_root=tmp_path)

    assert repo_a.acquire("heartbeat-tenant", "tenant-a", ttl_s=60) is True
    assert repo_b.acquire("heartbeat-tenant", "tenant-a", ttl_s=60) is False

    repo_a.release("heartbeat-tenant", "tenant-a")

    assert repo_b.acquire("heartbeat-tenant", "tenant-a", ttl_s=60) is True
