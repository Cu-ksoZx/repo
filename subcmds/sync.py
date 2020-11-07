#
# Copyright (C) 2008 The Android Open Source Project
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from optparse import SUPPRESS_HELP
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import xmlrpclib

try:
  import threading as _threading
except ImportError:
  import dummy_threading as _threading

from git_command import GIT
from git_refs import R_HEADS
from project import HEAD
from project import Project
from project import RemoteSpec
from command import Command, MirrorSafeCommand
from error import RepoChangedException, GitError
from project import R_HEADS
from project import SyncBuffer
from progress import Progress

class Sync(Command, MirrorSafeCommand):
  jobs = 1
  common = True
  helpSummary = "Update working tree to the latest revision"
  helpUsage = """
%prog [<project>...]
"""
  helpDescription = """
The '%prog' command synchronizes local project directories
with the remote repositories specified in the manifest.  If a local
project does not yet exist, it will clone a new local directory from
the remote repository and set up tracking branches as specified in
the manifest.  If the local project already exists, '%prog'
will update the remote branches and rebase any new local changes
on top of the new remote changes.

'%prog' will synchronize all projects listed at the command
line.  Projects can be specified either by name, or by a relative
or absolute path to the project's local directory. If no projects
are specified, '%prog' will synchronize all projects listed in
the manifest.

The -d/--detach option can be used to switch specified projects
back to the manifest revision.  This option is especially helpful
if the project is currently on a topic branch, but the manifest
revision is temporarily needed.

The -s/--smart-sync option can be used to sync to a known good
build as specified by the manifest-server element in the current
manifest.

The -f/--force-broken option can be used to proceed with syncing
other projects if a project sync fails.

SSH Connections
---------------

If at least one project remote URL uses an SSH connection (ssh://,
git+ssh://, or user@host:path syntax) repo will automatically
enable the SSH ControlMaster option when connecting to that host.
This feature permits other projects in the same '%prog' session to
reuse the same SSH tunnel, saving connection setup overheads.

To disable this behavior on UNIX platforms, set the GIT_SSH
environment variable to 'ssh'.  For example:

  export GIT_SSH=ssh
  %prog

Compatibility
~~~~~~~~~~~~~

This feature is automatically disabled on Windows, due to the lack
of UNIX domain socket support.

This feature is not compatible with url.insteadof rewrites in the
user's ~/.gitconfig.  '%prog' is currently not able to perform the
rewrite early enough to establish the ControlMaster tunnel.

If the remote SSH daemon is Gerrit Code Review, version 2.0.10 or
later is required to fix a server side protocol bug.

"""

  def _Options(self, p, show_smart=True):
    p.add_option('-f', '--force-broken',
                 dest='force_broken', action='store_true',
                 help="continue sync even if a project fails to sync")
    p.add_option('-l','--local-only',
                 dest='local_only', action='store_true',
                 help="only update working tree, don't fetch")
    p.add_option('-n','--network-only',
                 dest='network_only', action='store_true',
                 help="fetch only, don't update working tree")
    p.add_option('-d','--detach',
                 dest='detach_head', action='store_true',
                 help='detach projects back to manifest revision')
    p.add_option('-q','--quiet',
                 dest='quiet', action='store_true',
                 help='be more quiet')
    p.add_option('-j','--jobs',
                 dest='jobs', action='store', type='int',
                 help="number of projects to fetch simultaneously")
    if show_smart:
      p.add_option('-s', '--smart-sync',
                   dest='smart_sync', action='store_true',
                   help='smart sync using manifest from a known good build')

    g = p.add_option_group('repo Version options')
    g.add_option('--no-repo-verify',
                 dest='no_repo_verify', action='store_true',
                 help='do not verify repo source code')
    g.add_option('--repo-upgraded',
                 dest='repo_upgraded', action='store_true',
                 help=SUPPRESS_HELP)

  def _FetchHelper(self, opt, project, lock, fetched, pm, sem):
      if not project.Sync_NetworkHalf(quiet=opt.quiet):
        print >>sys.stderr, 'error: Cannot fetch %s' % project.name
        if opt.force_broken:
          print >>sys.stderr, 'warn: --force-broken, continuing to sync'
        else:
          sem.release()
          sys.exit(1)

      lock.acquire()
      fetched.add(project.gitdir)
      pm.update()
      lock.release()
      sem.release()

  def _Fetch(self, projects, opt):
    fetched = set()
    pm = Progress('Fetching projects', len(projects))

    if self.jobs == 1:
      for project in projects:
        pm.update()
        if project.Sync_NetworkHalf(quiet=opt.quiet):
          fetched.add(project.gitdir)
        else:
          print >>sys.stderr, 'error: Cannot fetch %s' % project.name
          if opt.force_broken:
            print >>sys.stderr, 'warn: --force-broken, continuing to sync'
          else:
            sys.exit(1)
    else:
      threads = set()
      lock = _threading.Lock()
      sem = _threading.Semaphore(self.jobs)
      for project in projects:
        sem.acquire()
        t = _threading.Thread(target = self._FetchHelper,
                              args = (opt,
                                      project,
                                      lock,
                                      fetched,
                                      pm,
                                      sem))
        threads.add(t)
        t.start()

      for t in threads:
        t.join()

    pm.end()
    return fetched

  def UpdateProjectList(self):
    new_project_paths = []
    for project in self.manifest.projects.values():
      if project.relpath:
        new_project_paths.append(project.relpath)
    file_name = 'project.list'
    file_path = os.path.join(self.manifest.repodir, file_name)
    old_project_paths = []

    if os.path.exists(file_path):
      fd = open(file_path, 'r')
      try:
        old_project_paths = fd.read().split('\n')
      finally:
        fd.close()
      for path in old_project_paths:
        if not path:
          continue
        if path not in new_project_paths:
          """If the path has already been deleted, we don't need to do it
          """
          if os.path.exists(self.manifest.topdir + '/' + path):
              project = Project(
                             manifest = self.manifest,
                             name = path,
                             remote = RemoteSpec('origin'),
                             gitdir = os.path.join(self.manifest.topdir,
                                                   path, '.git'),
                             worktree = os.path.join(self.manifest.topdir, path),
                             relpath = path,
                             revisionExpr = 'HEAD',
                             revisionId = None)

              if project.IsDirty():
                print >>sys.stderr, 'error: Cannot remove project "%s": \
uncommitted changes are present' % project.relpath
                print >>sys.stderr, '       commit changes, then run sync again'
                return -1
              else:
                print >>sys.stderr, 'Deleting obsolete path %s' % project.worktree
                shutil.rmtree(project.worktree)
                # Try deleting parent subdirs if they are empty
                dir = os.path.dirname(project.worktree)
                while dir != self.manifest.topdir:
                  try:
                    os.rmdir(dir)
                  except OSError:
                    break
                  dir = os.path.dirname(dir)

    new_project_paths.sort()
    fd = open(file_path, 'w')
    try:
      fd.write('\n'.join(new_project_paths))
      fd.write('\n')
    finally:
      fd.close()
    return 0

  def Execute(self, opt, args):
    if opt.jobs:
      self.jobs = opt.jobs
    if opt.network_only and opt.detach_head:
      print >>sys.stderr, 'error: cannot combine -n and -d'
      sys.exit(1)
    if opt.network_only and opt.local_only:
      print >>sys.stderr, 'error: cannot combine -n and -l'
      sys.exit(1)

    if opt.smart_sync:
      if not self.manifest.manifest_server:
        print >>sys.stderr, \
            'error: cannot smart sync: no manifest server defined in manifest'
        sys.exit(1)
      try:
        server = xmlrpclib.Server(self.manifest.manifest_server)
        p = self.manifest.manifestProject
        b = p.GetBranch(p.CurrentBranch)
        branch = b.merge
        if branch.startswith(R_HEADS):
          branch = branch[len(R_HEADS):]

        env = dict(os.environ)
        if (env.has_key('TARGET_PRODUCT') and
            env.has_key('TARGET_BUILD_VARIANT')):
          target = '%s-%s' % (env['TARGET_PRODUCT'],
                              env['TARGET_BUILD_VARIANT'])
          [success, manifest_str] = server.GetApprovedManifest(branch, target)
        else:
          [success, manifest_str] = server.GetApprovedManifest(branch)

        if success:
          manifest_name = "smart_sync_override.xml"
          manifest_path = os.path.join(self.manifest.manifestProject.worktree,
                                       manifest_name)
          try:
            f = open(manifest_path, 'w')
            try:
              f.write(manifest_str)
            finally:
              f.close()
          except IOError:
            print >>sys.stderr, 'error: cannot write manifest to %s' % \
                manifest_path
            sys.exit(1)
          self.manifest.Override(manifest_name)
        else:
          print >>sys.stderr, 'error: %s' % manifest_str
          sys.exit(1)
      except socket.error:
        print >>sys.stderr, 'error: cannot connect to manifest server %s' % (
            self.manifest.manifest_server)
        sys.exit(1)

    rp = self.manifest.repoProject
    rp.PreSync()

    mp = self.manifest.manifestProject
    mp.PreSync()

    if opt.repo_upgraded:
      _PostRepoUpgrade(self.manifest)

    if not opt.local_only:
      mp.Sync_NetworkHalf(quiet=opt.quiet)

    if mp.HasChanges:
      syncbuf = SyncBuffer(mp.config)
      mp.Sync_LocalHalf(syncbuf)
      if not syncbuf.Finish():
        sys.exit(1)
      self.manifest._Unload()
    all = self.GetProjects(args, missing_ok=True)

    if not opt.local_only:
      to_fetch = []
      now = time.time()
      if (24 * 60 * 60) <= (now - rp.LastFetch):
        to_fetch.append(rp)
      to_fetch.extend(all)

      fetched = self._Fetch(to_fetch, opt)
      _PostRepoFetch(rp, opt.no_repo_verify)
      if opt.network_only:
        # bail out now; the rest touches the working tree
        return

        self.manifest._Unload()
        all = self.GetProjects(args, missing_ok=True)
        missing = []
        for project in all:
          if project.gitdir not in fetched:
            missing.append(project)
        self._Fetch(missing, opt)

    if self.manifest.IsMirror:
      # bail out now, we have no working tree
      return

    if self.UpdateProjectList():
      sys.exit(1)

    syncbuf = SyncBuffer(mp.config,
                         detach_head = opt.detach_head)
    pm = Progress('Syncing work tree', len(all))
    for project in all:
      pm.update()
      if project.worktree:
        project.Sync_LocalHalf(syncbuf)
    pm.end()
    print >>sys.stderr
    if not syncbuf.Finish():
      sys.exit(1)

    # If there's a notice that's supposed to print at the end of the sync, print
    # it now...
    if self.manifest.notice:
      print self.manifest.notice

def _PostRepoUpgrade(manifest):
  for project in manifest.projects.values():
    if project.Exists:
      project.PostRepoUpgrade()

def _PostRepoFetch(rp, no_repo_verify=False, verbose=False):
  if rp.HasChanges:
    print >>sys.stderr, 'info: A new version of repo is available'
    print >>sys.stderr, ''
    if no_repo_verify or _VerifyTag(rp):
      syncbuf = SyncBuffer(rp.config)
      rp.Sync_LocalHalf(syncbuf)
      if not syncbuf.Finish():
        sys.exit(1)
      print >>sys.stderr, 'info: Restarting repo with latest version'
      raise RepoChangedException(['--repo-upgraded'])
    else:
      print >>sys.stderr, 'warning: Skipped upgrade to unverified version'
  else:
    if verbose:
      print >>sys.stderr, 'repo version %s is current' % rp.work_git.describe(HEAD)

def _VerifyTag(project):
  gpg_dir = os.path.expanduser('~/.repoconfig/gnupg')
  if not os.path.exists(gpg_dir):
    print >>sys.stderr,\
"""warning: GnuPG was not available during last "repo init"
warning: Cannot automatically authenticate repo."""
    return True

  try:
    cur = project.bare_git.describe(project.GetRevisionId())
  except GitError:
    cur = None

  if not cur \
     or re.compile(r'^.*-[0-9]{1,}-g[0-9a-f]{1,}$').match(cur):
    rev = project.revisionExpr
    if rev.startswith(R_HEADS):
      rev = rev[len(R_HEADS):]

    print >>sys.stderr
    print >>sys.stderr,\
      "warning: project '%s' branch '%s' is not signed" \
      % (project.name, rev)
    return False

  env = dict(os.environ)
  env['GIT_DIR'] = project.gitdir
  env['GNUPGHOME'] = gpg_dir

  cmd = [GIT, 'tag', '-v', cur]
  proc = subprocess.Popen(cmd,
                          stdout = subprocess.PIPE,
                          stderr = subprocess.PIPE,
                          env = env)
  out = proc.stdout.read()
  proc.stdout.close()

  err = proc.stderr.read()
  proc.stderr.close()

  if proc.wait() != 0:
    print >>sys.stderr
    print >>sys.stderr, out
    print >>sys.stderr, err
    print >>sys.stderr
    return False
  return True
