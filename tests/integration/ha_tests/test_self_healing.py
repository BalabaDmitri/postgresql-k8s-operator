#!/usr/bin/env python3
# Copyright 2022 Canonical Ltd.
# See LICENSE file for licensing details.
import asyncio
import copy
import logging
import subprocess
from time import sleep
from typing import Any

import psycopg2
import pytest
import yaml
from lightkube import Client
from lightkube.core.client import GlobalResource
from lightkube.resources.core_v1 import PersistentVolumeClaim, PersistentVolume
from lightkube.types import PatchType
from pytest_operator.plugin import OpsTest
from tenacity import Retrying, stop_after_delay, wait_fixed

from .. import markers
from ..helpers import (
    APPLICATION_NAME,
    CHARM_SERIES,
    METADATA,
    app_name,
    build_and_deploy,
    db_connect,
    get_password,
    get_unit_address,
    run_command_on_unit,
    scale_application, DATABASE_APP_NAME, set_password,
)
from .helpers import (
    are_all_db_processes_down,
    are_writes_increasing,
    change_patroni_setting,
    change_wal_settings,
    check_writes,
    fetch_cluster_members,
    get_patroni_setting,
    get_primary,
    is_cluster_updated,
    is_connection_possible,
    is_member_isolated,
    is_postgresql_ready,
    isolate_instance_from_cluster,
    list_wal_files,
    modify_pebble_restart_delay,
    remove_instance_isolation,
    send_signal_to_process,
    start_continuous_writes, create_test_data, validate_test_data,
)
from ..new_relations.helpers import build_connection_string
from ..new_relations.test_new_relations import FIRST_DATABASE_RELATION_NAME, APPLICATION_APP_NAME

logger = logging.getLogger(__name__)

APP_NAME = METADATA["name"]
SECOND_APP_NAME = f"second-{APP_NAME}"
PATRONI_PROCESS = "/usr/bin/patroni"
POSTGRESQL_PROCESS = "postgres"
DB_PROCESSES = [POSTGRESQL_PROCESS, PATRONI_PROCESS]
MEDIAN_ELECTION_TIME = 10


class Storage:
    class Volume:
        def __init__(self, pv: PersistentVolume, pvc: PersistentVolumeClaim):
            self.pv = pv
            self.pvc = pvc

    def __init__(self, primary: Volume, secondary: Volume):
        self.original = copy.deepcopy(primary)
        self.primary = primary
        self.secondary = secondary

def delete_pvc(ops_test: OpsTest, pvc: PersistentVolumeClaim):
    client = Client(namespace=ops_test.model.name)
    client.delete(PersistentVolumeClaim, namespace=ops_test.model.name, name=pvc.metadata.name)


def get_pvc(ops_test: OpsTest, unit_name: str):
    client = Client(namespace=ops_test.model.name)
    pvc_list = client.list(PersistentVolumeClaim, namespace=ops_test.model.name)
    for pvc in pvc_list:
        if unit_name in pvc.metadata.name:
            return pvc
    return None


def get_pv(ops_test: OpsTest, unit_name: str):
    client = Client(namespace=ops_test.model.name)
    pv_list = client.list(PersistentVolume, namespace=ops_test.model.name)
    for pv in pv_list:
        if unit_name in str(pv.spec.hostPath.path):
            return pv
    return None


def change_pv_reclaim_policy(ops_test: OpsTest, pv_config: GlobalResource, policy: str):
    client = Client(namespace=ops_test.model.name)
    res = client.patch(PersistentVolume, pv_config.metadata.name,
                       {"spec": {"persistentVolumeReclaimPolicy": f"{policy}"}}, namespace=ops_test.model.name)
    return res


def remove_pv_claimref(ops_test: OpsTest, pv_config: GlobalResource):
    client = Client(namespace=ops_test.model.name)
    client.patch(PersistentVolume, pv_config.metadata.name, {"spec": {"claimRef": None}}, namespace=ops_test.model.name)


def change_pvc_pv_name(pvc_config: PersistentVolumeClaim, pv_name_new: str):
    pvc_config.spec.volumeName = pv_name_new
    del pvc_config.metadata.annotations['pv.kubernetes.io/bind-completed']
    del pvc_config.metadata.uid
    return pvc_config


def apply_pvc_config(ops_test: OpsTest, pvc_config: PersistentVolumeClaim):
    client = Client(namespace=ops_test.model.name)
    pvc_config.metadata.managedFields = None
    client.apply(pvc_config, namespace=ops_test.model.name, field_manager="lightkube")


@pytest.mark.group(1)
@pytest.mark.abort_on_fail
async def test_build_and_deploy(ops_test: OpsTest) -> None:
    """Build and deploy three unit of PostgreSQL."""
    wait_for_apps: bool = False
    # It is possible for users to provide their own cluster for HA testing. Hence, check if there
    # is a pre-existing cluster.
    if not await app_name(ops_test):
        wait_for_apps = True
        await build_and_deploy(ops_test, 1, wait_for_idle=False)
        await build_and_deploy(ops_test, 1, database_app_name=SECOND_APP_NAME, wait_for_idle=False)
    # Deploy the continuous writes application charm if it wasn't already deployed.
    if not await app_name(ops_test, APPLICATION_NAME):
        wait_for_apps = True
        async with ops_test.fast_forward():
            await ops_test.model.deploy(
                APPLICATION_NAME,
                application_name=APPLICATION_NAME,
                series=CHARM_SERIES,
                channel="edge",
            )

    if wait_for_apps:
        async with ops_test.fast_forward():
            await ops_test.model.wait_for_idle(status="active", timeout=3000)

    await ops_test.model.relate(DATABASE_APP_NAME, f"{APPLICATION_NAME}:first-database")
    await ops_test.model.wait_for_idle(status="active", timeout=3000)

    for user in ["operator", "replication", "rewind"]:
        password = await get_password(ops_test, username=user, database_app_name=APP_NAME)
        second_primary = ops_test.model.applications[SECOND_APP_NAME].units[0].name
        await set_password(ops_test, second_primary, user, password)


@pytest.mark.group(1)
@markers.juju2
@pytest.mark.parametrize("process", DB_PROCESSES)
async def test_kill_db_process(
        ops_test: OpsTest, process: str, continuous_writes, primary_start_timeout
) -> None:
    # Locate primary unit.
    app = await app_name(ops_test)
    primary_name = await get_primary(ops_test, app)

    # Start an application that continuously writes data to the database.
    await start_continuous_writes(ops_test, app)

    # Kill the database process.
    await send_signal_to_process(ops_test, primary_name, process, "SIGKILL")

    # Wait some time to elect a new primary.
    sleep(MEDIAN_ELECTION_TIME * 2)

    async with ops_test.fast_forward():
        await are_writes_increasing(ops_test, primary_name)

        # Verify that the database service got restarted and is ready in the old primary.
        logger.info(f"waiting for the database service to restart on {primary_name}")
        assert await is_postgresql_ready(ops_test, primary_name)

    # Verify that a new primary gets elected (ie old primary is secondary).
    new_primary_name = await get_primary(ops_test, app, down_unit=primary_name)
    assert new_primary_name != primary_name

    await is_cluster_updated(ops_test, primary_name)


@pytest.mark.group(1)
@pytest.mark.parametrize("process", DB_PROCESSES)
async def test_freeze_db_process(
        ops_test: OpsTest, process: str, continuous_writes, primary_start_timeout
) -> None:
    # Locate primary unit.
    app = await app_name(ops_test)
    primary_name = await get_primary(ops_test, app)

    # Start an application that continuously writes data to the database.
    await start_continuous_writes(ops_test, app)

    # Freeze the database process.
    await send_signal_to_process(ops_test, primary_name, process, "SIGSTOP")

    # Wait some time to elect a new primary.
    sleep(MEDIAN_ELECTION_TIME * 6)

    async with ops_test.fast_forward():
        try:
            await are_writes_increasing(ops_test, primary_name)

            # Verify that a new primary gets elected (ie old primary is secondary).
            for attempt in Retrying(stop=stop_after_delay(60 * 3), wait=wait_fixed(3)):
                with attempt:
                    new_primary_name = await get_primary(ops_test, app, down_unit=primary_name)
                    assert new_primary_name != primary_name
        finally:
            # Un-freeze the old primary.
            for attempt in Retrying(stop=stop_after_delay(60 * 3), wait=wait_fixed(3)):
                with attempt:
                    use_ssh = (attempt.retry_state.attempt_number % 2) == 0
                    logger.info(f"unfreezing {process}")
                    await send_signal_to_process(
                        ops_test, primary_name, process, "SIGCONT", use_ssh
                    )
        # Verify that the database service got restarted and is ready in the old primary.
        logger.info(f"waiting for the database service to restart on {primary_name}")
        assert await is_postgresql_ready(ops_test, primary_name)

    await is_cluster_updated(ops_test, primary_name)


@pytest.mark.group(1)
@pytest.mark.parametrize("process", DB_PROCESSES)
async def test_restart_db_process(
        ops_test: OpsTest, process: str, continuous_writes, primary_start_timeout
) -> None:
    # Locate primary unit.
    app = await app_name(ops_test)
    primary_name = await get_primary(ops_test, app)

    # Start an application that continuously writes data to the database.
    await start_continuous_writes(ops_test, app)

    # Restart the database process.
    await send_signal_to_process(ops_test, primary_name, process, "SIGTERM")

    # Wait some time to elect a new primary.
    sleep(MEDIAN_ELECTION_TIME * 2)

    async with ops_test.fast_forward():
        await are_writes_increasing(ops_test, primary_name)

        # Verify that the database service got restarted and is ready in the old primary.
        logger.info(f"waiting for the database service to restart on {primary_name}")
        assert await is_postgresql_ready(ops_test, primary_name)

    # Verify that a new primary gets elected (ie old primary is secondary).
    new_primary_name = await get_primary(ops_test, app, down_unit=primary_name)
    assert new_primary_name != primary_name

    await is_cluster_updated(ops_test, primary_name)


@pytest.mark.group(1)
@pytest.mark.unstable
@pytest.mark.parametrize("process", DB_PROCESSES)
@pytest.mark.parametrize("signal", ["SIGTERM", "SIGKILL"])
async def test_full_cluster_restart(
        ops_test: OpsTest, process: str, signal: str, continuous_writes, restart_policy, loop_wait
) -> None:
    """This tests checks that a cluster recovers from a full cluster restart.

    The test can be called a full cluster crash when the signal sent to the OS process
    is SIGKILL.
    """
    # Locate primary unit.
    app = await app_name(ops_test)

    # Change the loop wait setting to make Patroni wait more time before restarting PostgreSQL.
    initial_loop_wait = await get_patroni_setting(ops_test, "loop_wait")
    await change_patroni_setting(ops_test, "loop_wait", 300)

    # Start an application that continuously writes data to the database.
    await start_continuous_writes(ops_test, app)

    # Restart all units "simultaneously".
    await asyncio.gather(*[
        send_signal_to_process(ops_test, unit.name, process, signal)
        for unit in ops_test.model.applications[app].units
    ])

    # This test serves to verify behavior when all replicas are down at the same time that when
    # they come back online they operate as expected. This check verifies that we meet the criteria
    # of all replicas being down at the same time.
    try:
        assert await are_all_db_processes_down(
            ops_test, process
        ), "Not all units down at the same time."
    finally:
        for unit in ops_test.model.applications[app].units:
            modify_pebble_restart_delay(
                ops_test,
                unit.name,
                "tests/integration/ha_tests/manifests/restore_pebble_restart_delay.yml",
                ensure_replan=True,
            )
        await change_patroni_setting(ops_test, "loop_wait", initial_loop_wait)

    # Verify all units are up and running.
    for unit in ops_test.model.applications[app].units:
        assert await is_postgresql_ready(
            ops_test, unit.name
        ), f"unit {unit.name} not restarted after cluster restart."

    await are_writes_increasing(ops_test)

    # Verify that all units are part of the same cluster.
    member_ips = await fetch_cluster_members(ops_test)
    ip_addresses = [
        await get_unit_address(ops_test, unit.name)
        for unit in ops_test.model.applications[app].units
    ]
    assert set(member_ips) == set(ip_addresses), "not all units are part of the same cluster."

    # Verify that no writes to the database were missed after stopping the writes.
    await check_writes(ops_test)


@pytest.mark.group(1)
async def test_forceful_restart_without_data_and_transaction_logs(
        ops_test: OpsTest,
        continuous_writes,
        primary_start_timeout,
        wal_settings,
) -> None:
    """A forceful restart with deleted data and without transaction logs (forced clone)."""
    # Locate primary unit.
    app = await app_name(ops_test)
    primary_name = await get_primary(ops_test, app)

    # Start an application that continuously writes data to the database.
    await start_continuous_writes(ops_test, app)

    # Copy data dir content removal script.
    await ops_test.juju(
        "scp", "tests/integration/ha_tests/clean-data-dir.sh", f"{primary_name}:/tmp"
    )

    # Stop the systemd service on the primary unit.
    logger.info(f"stopping database from {primary_name}")
    await run_command_on_unit(ops_test, primary_name, "/charm/bin/pebble stop postgresql")

    # Data removal runs within a script, so it allows `*` expansion.
    logger.info(f"removing data from {primary_name}")
    return_code, _, _ = await ops_test.juju(
        "ssh",
        primary_name,
        "bash",
        "/tmp/clean-data-dir.sh",
    )
    assert return_code == 0, "Failed to remove data directory"

    # Wait some time to elect a new primary.
    sleep(MEDIAN_ELECTION_TIME * 2)

    async with ops_test.fast_forward():
        logger.info("checking whether writes are increasing")
        await are_writes_increasing(ops_test, primary_name)

        # Verify that a new primary gets elected (ie old primary is secondary).
        for attempt in Retrying(stop=stop_after_delay(60), wait=wait_fixed(3)):
            with attempt:
                logger.info("checking whether a new primary was elected")
                new_primary_name = await get_primary(ops_test, app)
                assert new_primary_name != primary_name

        # Change some settings to enable WAL rotation and remove the old WAL files.
        for unit in ops_test.model.applications[app].units:
            if unit.name == primary_name:
                continue
            logger.info(f"enabling WAL rotation on {primary_name}")
            await change_wal_settings(ops_test, unit.name, 32, 32, 1)

        # Rotate the WAL segments.
        logger.info(f"rotating WAL segments on {new_primary_name}")
        files = await list_wal_files(ops_test, app)
        host = await get_unit_address(ops_test, new_primary_name)
        password = await get_password(ops_test, down_unit=primary_name)
        with db_connect(host, password) as connection:
            connection.autocommit = True
            with connection.cursor() as cursor:
                # Run some commands to make PostgreSQL do WAL rotation.
                cursor.execute("SELECT pg_switch_wal();")
                cursor.execute("CHECKPOINT;")
                cursor.execute("SELECT pg_switch_wal();")
        connection.close()
        new_files = await list_wal_files(ops_test, app)

        # Check that the WAL was correctly rotated.

        for unit_name in files:
            assert not files[unit_name].intersection(
                new_files
            ), f"WAL segments weren't correctly rotated on {unit_name}"

        # Start the systemd service in the old primary.
        logger.info(f"starting database on {primary_name}")
        await run_command_on_unit(ops_test, primary_name, "/charm/bin/pebble start postgresql")

        # Verify that the database service got restarted and is ready in the old primary.
        logger.info(f"waiting for the database service to restart on {primary_name}")
        assert await is_postgresql_ready(ops_test, primary_name)

    await is_cluster_updated(ops_test, primary_name)


@pytest.mark.group(1)
async def test_network_cut(
        ops_test: OpsTest, continuous_writes, primary_start_timeout, chaos_mesh
) -> None:
    """Completely cut and restore network."""
    # Locate primary unit.
    app = await app_name(ops_test)
    primary_name = await get_primary(ops_test, app)

    # Start an application that continuously writes data to the database.
    await start_continuous_writes(ops_test, app)

    # Verify that connection is possible.
    logger.info("checking whether the connectivity to the database is working")
    assert await is_connection_possible(
        ops_test, primary_name
    ), f"Connection {primary_name} is not possible"

    # Confirm that the primary is not isolated from the cluster.
    logger.info("confirming that the primary is not isolated from the cluster")
    assert not await is_member_isolated(ops_test, primary_name, primary_name)

    # Create network chaos policy to isolate instance from cluster
    logger.info(f"Cutting network for {primary_name}")
    isolate_instance_from_cluster(ops_test, primary_name)

    # Verify that connection is not possible.
    logger.info("checking whether the connectivity to the database is not working")
    assert not await is_connection_possible(
        ops_test, primary_name
    ), "Connection is possible after network cut"

    logger.info("checking whether writes are increasing")
    await are_writes_increasing(ops_test, primary_name)

    logger.info("checking whether a new primary was elected")
    async with ops_test.fast_forward():
        # Verify that a new primary gets elected (ie old primary is secondary).
        for attempt in Retrying(stop=stop_after_delay(60), wait=wait_fixed(3)):
            with attempt:
                new_primary_name = await get_primary(ops_test, app, down_unit=primary_name)
                assert new_primary_name != primary_name

    # Confirm that the former primary is isolated from the cluster.
    logger.info("confirming that the former primary is isolated from the cluster")
    assert await is_member_isolated(ops_test, new_primary_name, primary_name)

    # Remove network chaos policy isolating instance from cluster.
    logger.info(f"Restoring network for {primary_name}")
    remove_instance_isolation(ops_test)

    # Verify that the database service got restarted and is ready in the old primary.
    logger.info(f"waiting for the database service to restart on {primary_name}")
    assert await is_postgresql_ready(ops_test, primary_name)

    # Verify that connection is possible.
    logger.info("checking whether the connectivity to the database is working")
    assert await is_connection_possible(
        ops_test, primary_name
    ), f"Connection is not possible to {primary_name} after network restore"

    await is_cluster_updated(ops_test, primary_name)


@pytest.mark.group(1)
async def test_scaling_to_zero(ops_test: OpsTest, continuous_writes) -> None:
    """Scale the database to zero units and scale up again."""
    # Locate primary unit.
    app = await app_name(ops_test)

    # # Start an application that continuously writes data to the database.
    await start_continuous_writes(ops_test, app)

    # Get the connection string to connect to the database using the read/write endpoint.
    connection_string = await build_connection_string(
        ops_test, APPLICATION_APP_NAME, FIRST_DATABASE_RELATION_NAME
    )

    logger.info("connect to DB and create test table")
    await create_test_data(connection_string)

    # Scale the database to zero units.
    logger.info("scaling database to zero units")
    await scale_application(ops_test, app, 0)
    await scale_application(ops_test, SECOND_APP_NAME, 0)
    logger.info("---------------------------------------------  re use")

    storage, updated_pvc = await reuse_storage(ops_test, application=app, secondary_application=SECOND_APP_NAME)
    logger.info(f" ------------scale-appp----------")
    await scale_application(ops_test, app, 1, is_blocked=True)
    logger.info(f" ------------scale-appp blocked----------")
    assert "blocked" in [
        unit.workload_status
        for unit in ops_test.model.applications[app].units
    ], "Application not blocked wit third-party of storage"
    logger.info(f"---------- scale 0")
    await scale_application(ops_test, app, 0)
    logger.info(f"---------- apply get_pv")
    pv = get_pv(ops_test, app)
    logger.info(f"---------- sleep")
    sleep(60 * 10)

    logger.info(f"---------- updated pvc")
    delete_pvc(ops_test, updated_pvc)

    logger.info(f"---------- remove claimref {pv.metadata.name}")
    remove_pv_claimref(ops_test, pv_config=pv)

    logger.info(f"---------- apply original")
    apply_pvc_config(ops_test, pvc_config=storage.original.pvc)
    change_pv_reclaim_policy(ops_test, pv_config=storage.secondary.pv, policy="Delete")

    logger.info(f"---------- scale 1")
    await scale_application(ops_test, app, 1)

    # logger.info("check test database data")
    # await validate_test_data(connection_string)

    logger.info(f"---------- check_writes")
    await check_writes(ops_test)

    # second_volume_data = get_pv_and_pvc(ops_test, second_app)
    # app_volume_data =get_pv_and_pvc(ops_test, app)
    #
    # retain_volume(
    #     ops_test,
    #     pv_name=second_volume_data.get("pv_name"),
    #     patch_data="'{\"spec\":{\"persistentVolumeReclaimPolicy\":\"Retain\"}}'",
    # )
    # await ops_test.model.remove_application(SECOND_APP_NAME, block_until_done=True)
    # delete_pvc(ops_test, pvc_name=second_volume_data["pvc_name"])
    #
    # pvc_config = get_pvc_config(ops_test, pvc_name=app_volume_data["pvc_name"])
    # logger.info(pvc_config)
    # logger.info("---------------------------------------------------")
    # pvc_config["spec"]["volumeName"] = second_volume_data["pv_name"]
    # del pvc_config["pv.kubernetes.io/bind-completed"]
    # delete_pvc(ops_test, pvc_name=second_volume_data["pvc_name"])
    # logger.info(pvc_config)
    # logger.info("---------------------------------------------------")
    #
    # logger.info(f"----- apply conf {pvc_config}")
    # retain_volume(
    #     ops_test,
    #     pv_name=second_volume_data.get("pv_name"),
    #     patch_data="'{\"spec\":{\"claimRef\":null}}'",
    # )
    #
    # logger.info(f"----- apply conf {pvc_config}")
    # apply_conf(pvc_config)

    # # Scale the database to three units.
    # logger.info("scaling database to three units")
    # await scale_application(ops_test, app, 3)
    #
    # # Verify all units are up and running.
    # logger.info("waiting for the database service to start in all units")
    # for unit in ops_test.model.applications[app].units:
    #     assert await is_postgresql_ready(
    #         ops_test, unit.name
    #     ), f"unit {unit.name} not restarted after cluster restart."
    #
    # logger.info("checking whether writes are increasing")
    # await are_writes_increasing(ops_test)
    #
    # # Verify that all units are part of the same cluster.
    # logger.info("checking whether all units are part of the same cluster")
    # member_ips = await fetch_cluster_members(ops_test)
    # ip_addresses = [
    #     await get_unit_address(ops_test, unit.name)
    #     for unit in ops_test.model.applications[app].units
    # ]
    # assert set(member_ips) == set(ip_addresses), "not all units are part of the same cluster."
    #
    # # Verify that no writes to the database were missed after stopping the writes.
    # logger.info("checking whether no writes to the database were missed after stopping the writes")
    # await check_writes(ops_test)


async def reuse_storage(ops_test, application: str, secondary_application: str):
    logger.info("-- second_volume_data")
    storage = Storage(
        primary=Storage.Volume(
            pvc=get_pvc(ops_test, f"{application}-0"),
            pv=get_pv(ops_test, f"{application}-0")
        ),
        secondary=Storage.Volume(
            pv=get_pv(ops_test, f"{secondary_application}-0"),
            pvc=get_pvc(ops_test, f"{secondary_application}-0"),
        ),
    )

    logger.info(f" second volumeName = {storage.secondary.pvc.spec.volumeName}")
    logger.info(f" second path = {storage.secondary.pv.spec.hostPath.path}")
    logger.info(f" second pvc namespace = {storage.secondary.pvc.metadata.namespace}")

    logger.info(f" app volumeName = {storage.primary.pvc.spec.volumeName}")
    logger.info(f" app path = {storage.primary.pv.spec.hostPath.path}")
    logger.info(f" app pvc namespace = {storage.primary.pvc.metadata.namespace}")


    changed_secondary_pv = change_pv_reclaim_policy(ops_test, pv_config=storage.secondary.pv, policy="Retain")
    logger.info("-- remove_application")
    await ops_test.model.remove_application(secondary_application, block_until_done=True)
    # logger.info("=---------------------------- sleep")
    # sleep(60*3)
    delete_pvc(ops_test, pvc=storage.secondary.pvc)
    logger.info(f"-- change_pvc_pv volumeName ={storage.original.pvc.spec.volumeName}")
    # logger.info("=---------------------------- sleep")
    # sleep(60 * 3)
    pvc_config = change_pvc_pv_name(storage.primary.pvc, changed_secondary_pv.metadata.name)
    logger.info(f"-- volumeName ={pvc_config.spec.volumeName}")
    # logger.info("=---------------------------- sleep")
    # sleep(60 * 3)
    delete_pvc(ops_test, pvc=storage.primary.pvc)
    # logger.info("=---------------------------- sleep")
    # sleep(60 * 5)
    remove_pv_claimref(ops_test, pv_config=storage.secondary.pv)
    # logger.info("=---------------------------- sleep")
    # sleep(60 * 5)

    logger.info(f" ---------------------------")
    apply_pvc_config(ops_test, pvc_config=pvc_config)

    logger.info(f" updated volumeName = {pvc_config.spec.volumeName}")
    logger.info(f" original volumeName = {storage.original.pvc.spec.volumeName}")
    return storage, pvc_config

