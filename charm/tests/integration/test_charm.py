# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Smoke integration test — deploy the charm and verify it starts."""

import asyncio
from pathlib import Path

import pytest
from pytest_operator.plugin import OpsTest


@pytest.mark.abort_on_fail
async def test_deploy(ops_test: OpsTest, pytestconfig):
    """Build and deploy the charm, then verify it reaches active/idle."""
    assert ops_test.model

    charm = Path(pytestconfig.getoption("--charm-file")).resolve()
    rock_image = pytestconfig.getoption("--roadmap-web-rock-image")
    resources = {"app-image": rock_image}

    await asyncio.gather(
        ops_test.model.deploy(
            str(charm),
            resources=resources,
            application_name="canonical-roadmap",
            trust=True,
        ),
        ops_test.model.wait_for_idle(
            apps=["canonical-roadmap"],
            status="blocked",  # will be blocked waiting for postgresql relation
            raise_on_error=False,
            timeout=600,
        ),
    )
    # The charm should be blocked because postgresql is a required relation.
    unit = ops_test.model.applications["canonical-roadmap"].units[0]
    assert unit.workload_status == "blocked"
