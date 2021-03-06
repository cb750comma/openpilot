#!/usr/bin/env python3

# Safe Update: A simple service that waits for network access and tries to
# update every 10 minutes. It's intended to make the OP update process more
# robust against Git repository corruption. This service DOES NOT try to fix
# an already-corrupt BASEDIR Git repo, only prevent it from happening.
#
# During normal operation, both onroad and offroad, the update process makes
# no changes to the BASEDIR install of OP. All update attempts are performed
# in a disposable staging area provided by OverlayFS. It assumes the deleter
# process provides enough disk space to carry out the process.
#
# If an update succeeds, a flag is set, and the update is swapped in at the
# next reboot. If an update is interrupted or otherwise fails, the OverlayFS
# upper layer and metadata can be discarded before trying again.
#
# The swap on boot is triggered by launch_chffrplus.sh
# gated on the existence of $FINALIZED/.overlay_consistent and also the
# existence and mtime of $BASEDIR/.overlay_init.
#
# Other than build byproducts, BASEDIR should not be modified while this
# service is running. Developers modifying code directly in BASEDIR should
# disable this service.

import os
import datetime
import subprocess
import psutil
import shutil
import signal
import fcntl
import threading
from cffi import FFI
from pathlib import Path

from common.basedir import BASEDIR
from common.params import Params
from selfdrive.swaglog import cloudlog

TEST_IP = os.getenv("UPDATER_TEST_IP", "8.8.8.8")
LOCK_FILE = os.getenv("UPDATER_LOCK_FILE", "/tmp/safe_staging_overlay.lock")
STAGING_ROOT = os.getenv("UPDATER_STAGING_ROOT", "/data/safe_staging")

OVERLAY_UPPER = os.path.join(STAGING_ROOT, "upper")
OVERLAY_METADATA = os.path.join(STAGING_ROOT, "metadata")
OVERLAY_MERGED = os.path.join(STAGING_ROOT, "merged")
FINALIZED = os.path.join(STAGING_ROOT, "finalized")

NICE_LOW_PRIORITY = ["nice", "-n", "19"]

# Workaround for lack of os.link in the NEOS/termux python
ffi = FFI()
ffi.cdef("int link(const char *oldpath, const char *newpath);")
libc = ffi.dlopen(None)
def link(src, dest):
  return libc.link(src.encode(), dest.encode())


class WaitTimeHelper:
  def __init__(self):
    self.ready_event = threading.Event()
    self.shutdown = False
    signal.signal(signal.SIGTERM, self.graceful_shutdown)
    signal.signal(signal.SIGINT, self.graceful_shutdown)
    signal.signal(signal.SIGHUP, self.update_now)

  def graceful_shutdown(self, signum, frame):
    # umount -f doesn't appear effective in avoiding "device busy" on NEOS,
    # so don't actually die until the next convenient opportunity in main().
    cloudlog.info("caught SIGINT/SIGTERM, dismounting overlay at next opportunity")
    self.shutdown = True
    self.ready_event.set()

  def update_now(self, signum, frame):
    cloudlog.info("caught SIGHUP, running update check immediately")
    self.ready_event.set()

  def sleep(self, t):
    self.ready_event.wait(timeout=t)


def run(cmd, cwd=None):
  return subprocess.check_output(cmd, cwd=cwd, stderr=subprocess.STDOUT, encoding='utf8')


def set_consistent_flag(consistent):
  os.system("sync")
  consistent_file = Path(os.path.join(FINALIZED, ".overlay_consistent"))
  if consistent:
    consistent_file.touch()
  elif not consistent and consistent_file.exists():
    consistent_file.unlink()
  os.system("sync")


def set_update_available_params(new_version=False):
  params = Params()

  t = datetime.datetime.utcnow().isoformat()
  params.put("LastUpdateTime", t.encode('utf8'))

  if new_version:
    try:
      with open(os.path.join(FINALIZED, "RELEASES.md"), "rb") as f:
        r = f.read()
      r = r[:r.find(b'\n\n')]  # Slice latest release notes
      params.put("ReleaseNotes", r + b"\n")
    except Exception:
      params.put("ReleaseNotes", "")
    params.put("UpdateAvailable", "1")


def dismount_ovfs():
  if os.path.ismount(OVERLAY_MERGED):
    cloudlog.error("unmounting existing overlay")
    run(["umount", "-l", OVERLAY_MERGED])


def setup_git_options(cwd):
  # We sync FS object atimes (which NEOS doesn't use) and mtimes, but ctimes
  # are outside user control. Make sure Git is set up to ignore system ctimes,
  # because they change when we make hard links during finalize. Otherwise,
  # there is a lot of unnecessary churn. This appears to be a common need on
  # OSX as well: https://www.git-tower.com/blog/make-git-rebase-safe-on-osx/

  # We are using copytree to copy the directory, which also changes
  # inode numbers. Ignore those changes too.
  git_cfg = [
    ("core.trustctime", "false"),
    ("core.checkStat", "minimal"),
  ]
  for option, value in git_cfg:
    try:
      ret = run(["git", "config", "--get", option], cwd)
      config_ok = (ret.strip() == value)
    except subprocess.CalledProcessError:
      config_ok = False

    if not config_ok:
      cloudlog.info(f"Setting git '{option}' to '{value}'")
      run(["git", "config", option, value], cwd)


def init_ovfs():
  cloudlog.info("preparing new safe staging area")
  Params().put("UpdateAvailable", "0")

  set_consistent_flag(False)

  dismount_ovfs()
  if os.path.isdir(STAGING_ROOT):
    shutil.rmtree(STAGING_ROOT)

  for dirname in [STAGING_ROOT, OVERLAY_UPPER, OVERLAY_METADATA, OVERLAY_MERGED, FINALIZED]:
    os.mkdir(dirname, 0o755)

  if not os.lstat(BASEDIR).st_dev == os.lstat(OVERLAY_MERGED).st_dev:
    raise RuntimeError("base and overlay merge directories are on different filesystems; not valid for overlay FS!")

  # Remove consistent flag from current BASEDIR so it's not copied over
  if os.path.isfile(os.path.join(BASEDIR, ".overlay_consistent")):
    os.remove(os.path.join(BASEDIR, ".overlay_consistent"))

  # Leave a timestamped canary in BASEDIR to check at startup. The device clock
  # should be correct by the time we get here. If the init file disappears, or
  # critical mtimes in BASEDIR are newer than .overlay_init, continue.sh can
  # assume that BASEDIR has used for local development or otherwise modified,
  # and skips the update activation attempt.
  Path(os.path.join(BASEDIR, ".overlay_init")).touch()

  overlay_opts = f"lowerdir={BASEDIR},upperdir={OVERLAY_UPPER},workdir={OVERLAY_METADATA}"
  run(["mount", "-t", "overlay", "-o", overlay_opts, "none", OVERLAY_MERGED])


def finalize_from_ovfs():
  """Take the current OverlayFS merged view and finalize a copy outside of
  OverlayFS, ready to be swapped-in at BASEDIR. Copy using shutil.copytree"""

  cloudlog.info("creating finalized version of the overlay")
  shutil.rmtree(FINALIZED)
  shutil.copytree(OVERLAY_MERGED, FINALIZED, symlinks=True)
  cloudlog.info("done finalizing overlay")


def attempt_update():
  cloudlog.info("attempting git update inside staging overlay")

  setup_git_options(OVERLAY_MERGED)

  git_fetch_output = run(NICE_LOW_PRIORITY + ["git", "fetch"], OVERLAY_MERGED)
  cloudlog.info("git fetch success: %s", git_fetch_output)

  cur_hash = run(["git", "rev-parse", "HEAD"], OVERLAY_MERGED).rstrip()
  upstream_hash = run(["git", "rev-parse", "@{u}"], OVERLAY_MERGED).rstrip()
  new_version = cur_hash != upstream_hash

  err_msg = "Failed to add the host to the list of known hosts (/data/data/com.termux/files/home/.ssh/known_hosts).\n"
  git_fetch_result = len(git_fetch_output) > 0 and (git_fetch_output != err_msg)

  cloudlog.info("comparing %s to %s" % (cur_hash, upstream_hash))
  if new_version or git_fetch_result:
    cloudlog.info("Running update")
    if new_version:
      cloudlog.info("git reset in progress")
      r = [
        run(NICE_LOW_PRIORITY + ["git", "reset", "--hard", "@{u}"], OVERLAY_MERGED),
        run(NICE_LOW_PRIORITY + ["git", "clean", "-xdf"], OVERLAY_MERGED),
        run(NICE_LOW_PRIORITY + ["git", "submodule", "init"], OVERLAY_MERGED),
        run(NICE_LOW_PRIORITY + ["git", "submodule", "update"], OVERLAY_MERGED),
      ]
      cloudlog.info("git reset success: %s", '\n'.join(r))

    # Un-set the validity flag to prevent the finalized tree from being
    # activated later if the finalize step is interrupted
    set_consistent_flag(False)

    finalize_from_ovfs()

    # Make sure the validity flag lands on disk LAST, only when the local git
    # repo and OP install are in a consistent state.
    set_consistent_flag(True)

    cloudlog.info("update successful!")
  else:
    cloudlog.info("nothing new from git at this time")

  set_update_available_params(new_version=new_version)


def main():
  params = Params()

  if params.get("DisableUpdates") == b"1":
    raise RuntimeError("updates are disabled by param")

  if os.geteuid() != 0:
    raise RuntimeError("updated must be launched as root!")

  # Set low io priority
  p = psutil.Process()
  if psutil.LINUX:
    p.ionice(psutil.IOPRIO_CLASS_BE, value=7)

  ov_lock_fd = open(LOCK_FILE, 'w')
  try:
    fcntl.flock(ov_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
  except IOError:
    raise RuntimeError("couldn't get overlay lock; is another updated running?")

  # Wait for IsOffroad to be set before our first update attempt
  wait_helper = WaitTimeHelper()
  wait_helper.sleep(30)

  update_failed_count = 0
  overlay_initialized = False
  while not wait_helper.shutdown:
    update_failed_count += 1
    wait_helper.ready_event.clear()

    # Check for internet every 30s
    time_wrong = datetime.datetime.utcnow().year < 2019
    ping_failed = os.system(f"ping -W 4 -c 1 {TEST_IP}") != 0
    if ping_failed or time_wrong:
      wait_helper.sleep(30)
      continue

    # Attempt an update
    try:
      # Re-create the overlay if BASEDIR/.git has changed since we created the overlay
      if overlay_initialized:
        overlay_init_fn = os.path.join(BASEDIR, ".overlay_init")
        git_dir_path = os.path.join(BASEDIR, ".git")
        new_files = run(["find", git_dir_path, "-newer", overlay_init_fn])

        if len(new_files.splitlines()):
          cloudlog.info(".git directory changed, recreating overlay")
          overlay_initialized = False

      if not overlay_initialized:
        init_ovfs()
        overlay_initialized = True

      if params.get("IsOffroad") == b"1":
        attempt_update()
        update_failed_count = 0
      else:
        cloudlog.info("not running updater, openpilot running")

    except subprocess.CalledProcessError as e:
      cloudlog.event(
        "update process failed",
        cmd=e.cmd,
        output=e.output,
        returncode=e.returncode
      )
      overlay_initialized = False
    except Exception:
      cloudlog.exception("uncaught updated exception, shouldn't happen")

    params.put("UpdateFailedCount", str(update_failed_count))

    # Wait 10 minutes between update attempts
    wait_helper.sleep(60*10)

  # We've been signaled to shut down
  dismount_ovfs()

if __name__ == "__main__":
  main()
