#!/usr/bin/env python
# SPDX-License-Identifier: ISC

#
# test_zebra_nhg_delete_race.py
#
# Copyright (c) 2026 by
# Alibaba Inc.
# Yuqing Zhao
#

"""
test_zebra_nhg_delete_race.py:

Verify that zebra correctly tolerates a race where the kernel has already
removed a Nexthop Group (NHG) by the time zebra issues RTM_DELNEXTHOP.
The kernel responds with ENOENT/ESRCH; without the fix that error is
propagated as ZEBRA_DPLANE_REQUEST_FAILURE, which causes
dplane_thread_loop() to remove the ctx from the work_list and never hand
it to the FPM provider. The result is a permanently leaked NHG entry on
the FPM side.

Topology:
    r1 ---eth0--- s1
    r1 ---eth1--- s2

r1 has an ECMP static route 10.0.0.0/24 with two nexthops, one out each
interface. Bringing r1-eth0 down causes the kernel to garbage-collect the
nexthops referencing that interface and the parent NHG, so the subsequent
zebra-driven RTM_DELNEXTHOP races against an "already gone" object.

The test asserts (with the fix in place):
  - no "Failed to uninstall Nexthop ID" error in zebra.log
  - the NHG id no longer appears in `show nexthop-group rib`
  - the NHG id no longer appears in the fpm_listener dump
"""

import os
import sys
import json
import pytest
from functools import partial

CWD = os.path.dirname(os.path.realpath(__file__))
sys.path.append(os.path.join(CWD, "../"))

# pylint: disable=C0413
from lib import topotest
from lib.topogen import Topogen, TopoRouter, get_topogen
from lib.topolog import logger
from lib.common_config import step

pytestmark = [pytest.mark.fpm, pytest.mark.staticd]


def build_topo(tgen):
    "Build function"
    tgen.add_router("r1")

    switch = tgen.add_switch("s1")
    switch.add_link(tgen.gears["r1"])

    switch = tgen.add_switch("s2")
    switch.add_link(tgen.gears["r1"])


def setup_module(mod):
    "Sets up the pytest environment"
    tgen = Topogen(build_topo, mod.__name__)
    tgen.start_topology()

    router_list = tgen.routers()
    for rname, router in router_list.items():
        router.load_config(
            TopoRouter.RD_ZEBRA,
            os.path.join(CWD, "{}/zebra.conf".format(rname)),
            "-M dplane_fpm_nl",
        )
        router.load_config(TopoRouter.RD_STATIC, None)

        fpm_data_path = os.path.join(router.gearlogdir, "fpm_test.data")
        router.load_config(
            TopoRouter.RD_FPM_LISTENER,
            None,
            "-r -z {}".format(fpm_data_path),
        )

    tgen.start_router()


def teardown_module():
    "Teardown the pytest environment"
    tgen = get_topogen()
    tgen.stop_topology()


def _fpm_listener_dump(router):
    """Send SIGUSR1 to fpm_listener so it rewrites its dump file."""
    pid_file = os.path.join(router.gearlogdir, "fpm_listener.pid")
    try:
        with open(pid_file, "r") as f:
            pid = f.read().strip()
        router.run("kill -SIGUSR1 {}".format(pid))
        return True
    except FileNotFoundError:
        return False


def _read_fpm_dump(router):
    fpm_data_file = os.path.join(router.gearlogdir, "fpm_test.data")
    try:
        with open(fpm_data_file, "r") as f:
            return f.read()
    except FileNotFoundError:
        return ""


def _fpm_dump_has_nhg(router, nhg_id):
    if not _fpm_listener_dump(router):
        return False
    return "  ID: {},".format(nhg_id) in _read_fpm_dump(router)


def _get_nhg_id_for_prefix(router, prefix):
    output = router.vtysh_cmd("show ip route {} json".format(prefix))
    try:
        rj = json.loads(output)
    except json.JSONDecodeError:
        return None
    if prefix not in rj:
        return None
    return rj[prefix][0].get("nexthopGroupId")


def _zebra_log_path(tgen):
    return os.path.join(tgen.logdir, "r1", "zebra.log")


def _count_zebra_log(tgen, needle):
    path = _zebra_log_path(tgen)
    if not os.path.isfile(path):
        return 0
    with open(path) as f:
        return sum(1 for line in f if needle in line)


def test_route_and_nhg_installed():
    "Static ECMP route is installed in zebra, kernel and FPM"
    tgen = get_topogen()
    if tgen.routers_have_failure():
        pytest.skip(tgen.errors)

    r1 = tgen.gears["r1"]

    step("Wait for ECMP route 10.0.0.0/24 to be FIB-installed with 2 nexthops")

    def check_installed():
        output = r1.vtysh_cmd("show ip route 10.0.0.0/24 json")
        try:
            rj = json.loads(output)
        except json.JSONDecodeError:
            return "json decode error"
        if "10.0.0.0/24" not in rj:
            return "route not present"
        route = rj["10.0.0.0/24"][0]
        if not route.get("installed", False):
            return "route not installed"
        fib = [nh for nh in route.get("nexthops", []) if nh.get("fib", False)]
        if len(fib) != 2:
            return "expected 2 fib nexthops, got {}".format(len(fib))
        return None

    _, result = topotest.run_and_expect(check_installed, None, count=30, wait=1)
    assert result is None, "ECMP route not installed: {}".format(result)

    nhg_id = _get_nhg_id_for_prefix(r1, "10.0.0.0/24")
    assert nhg_id is not None, "Could not get NHG id for 10.0.0.0/24"
    logger.info("Route 10.0.0.0/24 uses NHG id %d", nhg_id)

    step("Verify the NHG is reflected in the kernel")

    def check_kernel_nhg():
        output = r1.run("ip nexthop show id {}".format(nhg_id))
        return "id {} ".format(nhg_id) in output or "id {}".format(nhg_id) in output

    success, _ = topotest.run_and_expect(check_kernel_nhg, True, count=30, wait=1)
    assert success, "NHG id {} not seen in kernel".format(nhg_id)

    step("Verify the NHG was forwarded to the FPM listener")

    success, _ = topotest.run_and_expect(
        lambda: _fpm_dump_has_nhg(r1, nhg_id), True, count=60, wait=1
    )
    assert success, (
        "NHG id {} not received by FPM listener.\nFPM dump tail:\n{}".format(
            nhg_id, _read_fpm_dump(r1)[-2000:]
        )
    )


def test_nhg_delete_race_with_kernel_cleanup():
    """
    Trigger a race where the kernel removes the NHG before zebra's
    RTM_DELNEXTHOP arrives, then verify zebra still cleans up properly
    and propagates the delete to the FPM provider.
    """
    tgen = get_topogen()
    if tgen.routers_have_failure():
        pytest.skip(tgen.errors)

    r1 = tgen.gears["r1"]

    nhg_id = _get_nhg_id_for_prefix(r1, "10.0.0.0/24")
    assert nhg_id is not None, "NHG id for 10.0.0.0/24 unexpectedly gone"

    err_before = _count_zebra_log(tgen, "Failed to uninstall Nexthop ID")

    step("Bring both nexthop interfaces down so kernel garbage-collects the NHG")
    # Shutting interfaces forces the kernel to drop the nexthops that
    # reference them; the parent NHG becomes orphaned and the kernel
    # purges it. Zebra will subsequently issue RTM_DELNEXTHOP for an
    # already-removed object => ENOENT.
    r1.run("ip link set dev r1-eth0 down")
    r1.run("ip link set dev r1-eth1 down")

    step("Remove the static route to make zebra retire the NHG")
    r1.vtysh_cmd(
        """
        configure terminal
        no ip route 10.0.0.0/24 192.168.1.2
        no ip route 10.0.0.0/24 192.168.2.2
        end
        """
    )

    step("Verify zebra fully releases the NHG (no leak in `show nexthop-group rib`)")

    def nhg_gone_in_zebra():
        out = r1.vtysh_cmd("show nexthop-group rib {} json".format(nhg_id))
        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            return out.strip() == "" or "{}" in out
        return not data or str(nhg_id) not in data

    success, _ = topotest.run_and_expect(nhg_gone_in_zebra, True, count=30, wait=1)
    assert success, (
        "NHG id {} still present in zebra after route removal".format(nhg_id)
    )

    step("Verify FPM listener was told the NHG is gone")

    def nhg_gone_in_fpm():
        return not _fpm_dump_has_nhg(r1, nhg_id)

    success, _ = topotest.run_and_expect(nhg_gone_in_fpm, True, count=60, wait=1)
    assert success, (
        "NHG id {} leaked on the FPM side; the delete was not propagated "
        "to the FPM provider. Without the kernel_netlink fix, a FAILURE "
        "ctx is removed from the dplane work_list and never reaches FPM. "
        "FPM dump tail:\n{}".format(nhg_id, _read_fpm_dump(r1)[-2000:])
    )

    step("Verify zebra log has no new 'Failed to uninstall Nexthop ID' errors")
    err_after = _count_zebra_log(tgen, "Failed to uninstall Nexthop ID")
    assert err_after == err_before, (
        "Found {} new 'Failed to uninstall Nexthop ID' errors in zebra.log "
        "(before={} after={}); the kernel ENOENT/ESRCH on RTM_DELNEXTHOP "
        "is not being treated as success.".format(
            err_after - err_before, err_before, err_after
        )
    )


if __name__ == "__main__":
    args = ["-s"] + sys.argv[1:]
    sys.exit(pytest.main(args))
