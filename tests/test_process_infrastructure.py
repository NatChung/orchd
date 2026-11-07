"""Ownership stays fail-closed except for proven non-worker service infrastructure."""
import collections
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

import psutil

from orchd import processes


class ProcessEnvironmentTests(unittest.TestCase):
    def test_unreadable_zombie_is_exited_even_while_pid_is_running(self):
        proc = mock.Mock(spec=psutil.Process, pid=987654)
        proc.environ.side_effect = psutil.AccessDenied(proc.pid)
        proc.is_running.return_value = True
        proc.status.return_value = psutil.STATUS_ZOMBIE
        with self.assertRaises(psutil.NoSuchProcess):
            processes.environ_of(proc, attempts=1)

    def test_live_ambiguous_environment_remains_denied(self):
        proc = mock.Mock(spec=psutil.Process, pid=987654)
        proc.environ.side_effect = psutil.AccessDenied(proc.pid)
        proc.is_running.return_value = True
        proc.status.return_value = psutil.STATUS_SLEEPING
        with self.assertRaises(psutil.AccessDenied):
            processes.environ_of(proc, attempts=1, exclude_unreadable=lambda p: False)

    def test_unverified_service_is_classified_only_once_before_regular_retries(self):
        proc = mock.Mock(spec=psutil.Process, pid=987654)
        proc.environ.side_effect = psutil.AccessDenied(proc.pid)
        proc.is_running.return_value = True
        proc.status.return_value = psutil.STATUS_SLEEPING
        classify = mock.Mock(return_value=False)
        with mock.patch.object(processes.time, "sleep"), self.assertRaises(psutil.AccessDenied):
            processes.environ_of(proc, attempts=4, exclude_unreadable=classify)
        classify.assert_called_once_with(proc)
        self.assertEqual(proc.environ.call_count, 4)


@unittest.skipUnless(sys.platform.startswith("linux"), "Linux systemd and procfs evidence")
class SystemdProcessTests(unittest.TestCase):
    def setUp(self):
        self.uid = os.getuid()
        ids = collections.namedtuple("uids", "real effective saved")(self.uid, self.uid, self.uid)
        self.manager = mock.Mock(spec=psutil.Process, pid=201)
        self.manager.name.return_value = "systemd"
        self.manager.cmdline.return_value = ["/usr/lib/systemd/systemd", "--user"]
        self.manager.uids.return_value = ids
        self.manager.is_running.return_value = True
        self.manager.status.return_value = psutil.STATUS_SLEEPING
        self.manager.environ.side_effect = psutil.AccessDenied(201)
        self.pam = mock.Mock(spec=psutil.Process, pid=202)
        self.pam.name.return_value = "(sd-pam)"
        self.pam.uids.return_value = ids
        self.pam.is_running.return_value = True
        self.pam.status.return_value = psutil.STATUS_SLEEPING
        self.pam.environ.side_effect = psutil.AccessDenied(202)
        self.stats = {201: dict(ppid=1, group=201, session=201, flags=0, start=100),
                      202: dict(ppid=201, group=201, session=201, flags=0x40, start=110)}
        self.unit = dict(MainPID="201", Type="notify-reload", PAMName="systemd-user", ActiveState="active",
                         ControlGroup=f"/user.slice/user-{self.uid}.slice/user@{self.uid}.service",
                         ActiveEnterTimestampMonotonic="2000000")
        for patcher in (
            mock.patch.object(processes, "_user_manager_unit", return_value=self.unit),
            mock.patch.object(processes, "_linux_process_stat", side_effect=lambda pid: self.stats[pid]),
            mock.patch.object(processes.psutil, "Process", return_value=self.manager),
            mock.patch.object(processes.os, "sysconf", return_value=100),
            mock.patch.object(Path, "read_text", return_value="0::" + self.unit["ControlGroup"] + "/init.scope\n"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def scan(self, proc):
        with mock.patch.object(processes.psutil, "process_iter", return_value=[proc]), \
                mock.patch.object(processes.time, "sleep"):
            return processes.task_processes("/wt/task")

    def test_root_managed_manager_and_pre_ready_noexec_pam_do_not_block_cleanup(self):
        for proc in (self.manager, self.pam):
            with self.subTest(pid=proc.pid):
                self.assertEqual(self.scan(proc), {})
                self.assertEqual(proc.environ.call_count, 1)

    def test_readable_task_mark_wins_without_service_lookup(self):
        self.pam.environ.side_effect = None
        self.pam.environ.return_value = {"ORCHD_WORKTREE": "/wt/task"}
        self.pam.children.return_value = []
        with mock.patch.object(processes, "_user_manager_unit", side_effect=AssertionError("unnecessary lookup")):
            self.assertEqual(self.scan(self.pam), {self.pam.pid: self.pam})

    def test_later_pam_lookalike_is_ambiguous_despite_matching_cgroup_and_parent(self):
        self.stats[202]["start"] = 201
        with self.assertRaisesRegex(RuntimeError, "cannot inspect process 202"):
            self.scan(self.pam)

    def test_execed_pam_lookalike_and_wrong_parent_remain_ambiguous(self):
        for field, value in (("flags", 0), ("ppid", 999)):
            old = self.stats[202][field]
            self.stats[202][field] = value
            with self.subTest(field=field), self.assertRaisesRegex(RuntimeError, "cannot inspect process 202"):
                self.scan(self.pam)
            self.stats[202][field] = old

    def test_systemd_name_and_cgroup_do_not_override_system_manager_identity(self):
        self.unit["MainPID"] = "999"
        with mock.patch.object(processes.psutil, "Process", side_effect=psutil.NoSuchProcess(999)):
            with self.assertRaisesRegex(RuntimeError, "cannot inspect process 201"):
                self.scan(self.manager)

    def test_missing_root_service_evidence_leaves_failure_closed(self):
        with mock.patch.object(processes, "_user_manager_unit", side_effect=OSError("system bus unavailable")):
            with self.assertRaisesRegex(RuntimeError, "cannot inspect process 202"):
                self.scan(self.pam)


@unittest.skipUnless(sys.platform.startswith("linux"), "Linux zombie lifecycle")
class ZombieLifecycleTests(unittest.TestCase):
    def test_real_unreaped_child_is_not_a_live_unknown_process(self):
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        try:
            proc = psutil.Process(child.pid)
            deadline = time.monotonic() + 5
            while proc.status() != psutil.STATUS_ZOMBIE and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertEqual(proc.status(), psutil.STATUS_ZOMBIE)
            self.assertTrue(proc.is_running())
            with mock.patch.object(proc, "environ", side_effect=psutil.AccessDenied(proc.pid)):
                with self.assertRaises(psutil.NoSuchProcess):
                    processes.environ_of(proc, attempts=1)
        finally:
            child.wait(timeout=5)
