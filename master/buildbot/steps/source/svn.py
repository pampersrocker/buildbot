# This file is part of Buildbot.  Buildbot is free software: you can
# redistribute it and/or modify it under the terms of the GNU General Public
# License as published by the Free Software Foundation, version 2.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE.  See the GNU General Public License for more
# details.
#
# You should have received a copy of the GNU General Public License along with
# this program; if not, write to the Free Software Foundation, Inc., 51
# Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.
#
# Copyright Buildbot Team Members

from __future__ import annotations

import re
import xml.dom.minidom
import xml.parsers.expat
from typing import TYPE_CHECKING
from typing import Any
from urllib.parse import quote as urlquote
from urllib.parse import unquote as urlunquote
from urllib.parse import urlparse
from urllib.parse import urlunparse

from twisted.internet import defer
from twisted.internet import reactor
from twisted.python import log

from buildbot.config import ConfigErrors
from buildbot.interfaces import WorkerSetupError
from buildbot.process import buildstep
from buildbot.process import remotecommand
from buildbot.steps.source.base import Source

if TYPE_CHECKING:
    from collections.abc import Generator

    from buildbot.interfaces import IMaybeRenderableType
    from buildbot.util.twisted import InlineCallbacksType


class SVN(Source):
    """I perform Subversion checkout/update operations."""

    name = 'svn'

    renderables = ['repourl', 'branchPath', 'password']
    possible_methods = ('clean', 'fresh', 'clobber', 'copy', 'export', None)

    def __init__(
        self,
        repourl: IMaybeRenderableType[str] | None = None,
        branchPath: IMaybeRenderableType[str] | None = None,
        mode: str = 'incremental',
        method: str | None = None,
        username: str | None = None,
        password: str | None = None,
        extra_args: list[str] | None = None,
        keep_on_purge: list[str] | None = None,
        depth: str | None = None,
        preferLastChangedRev: bool = False,
        **kwargs: Any,
    ) -> None:
        self.repourl = repourl
        self.branchPath = branchPath
        self.username = username
        self.password = password
        self.extra_args = extra_args
        self.keep_on_purge = keep_on_purge or []
        self.depth = depth
        self.method = method
        self.mode = mode
        self.preferLastChangedRev = preferLastChangedRev
        self.targetRepourl: str | None = None
        super().__init__(**kwargs)
        errors = []
        if not self._hasAttrGroupMember('mode', self.mode):
            errors.append(f"mode {self.mode} is not one of {self._listAttrGroupMembers('mode')}")
        if self.method not in self.possible_methods:
            errors.append(f"method {self.method} is not one of {self.possible_methods}")

        if repourl is None:
            errors.append("you must provide repourl")

        if errors:
            raise ConfigErrors(errors)

    @defer.inlineCallbacks
    def run_vc(self, branch: str | None, revision: str | None, patch: Any) -> InlineCallbacksType[int]:
        self.revision = revision
        self.targetRepourl = self._computeTargetRepourl()
        self.method = self._getMethod()
        self.stdio_log = yield self.addLogForRemoteCommands("stdio")

        # if the version is new enough, and the password is set, then obfuscate
        # it
        if self.password is not None:
            if not self.workerVersionIsOlderThan('shell', '2.16'):
                self.password = ('obfuscated', self.password, 'XXXXXX')  # type: ignore[assignment]
            else:
                log.msg("Worker does not understand obfuscation; svn password will be logged")

        installed = yield self.checkSvn()
        if not installed:
            raise WorkerSetupError("SVN is not installed on worker")

        patched = yield self.sourcedirIsPatched()
        if patched:
            yield self.purge(False)

        yield self._getAttrGroupMember('mode', self.mode)()

        if patch:
            yield self.patch(patch)
        res = yield self.parseGotRevision()
        return res

    @defer.inlineCallbacks
    def mode_full(self) -> InlineCallbacksType[None]:
        if self.method == 'clobber':
            yield self.clobber()
            return
        elif self.method in ['copy', 'export']:
            yield self.copy()
            return

        action = yield self._getSourcedirAction()
        if action == 'clobber':
            # blow away the old (un-updatable) directory and checkout
            yield self.clobber()
            return
        if action == 'switch':
            yield self.switch()

        if self.method == 'clean':
            yield self.clean()
        elif self.method == 'fresh':
            yield self.fresh()

    @defer.inlineCallbacks
    def mode_incremental(self) -> InlineCallbacksType[None]:
        action = yield self._getSourcedirAction()

        if action == 'clobber':
            # blow away the old (un-updatable) directory and checkout
            yield self.clobber()
        else:
            if action == 'switch':
                yield self.switch()
            # otherwise, do an update
            yield self._update()

    @defer.inlineCallbacks
    def _update(self) -> InlineCallbacksType[int]:
        command = ['update']
        if self.revision:
            command.extend(['--revision', str(self.revision)])
        res = yield self._dovccmd(command)
        return res  # type: ignore[return-value]

    @defer.inlineCallbacks
    def switch(self) -> InlineCallbacksType[int]:
        command = ['switch', self._getTargetRepourl()]
        if self.revision:
            command.extend(['--revision', str(self.revision)])
        res = yield self._dovccmd(command)
        return res  # type: ignore[return-value]

    def _getTargetRepourl(self) -> str:
        repourl = self.targetRepourl if self.targetRepourl is not None else self.repourl
        assert repourl is not None
        return repourl

    def _computeTargetRepourl(self) -> str | None:
        if self.repourl is None:
            return None
        if self.branchPath is None:
            return self.repourl
        return f'{self.repourl}/{self.branchPath}'

    def _computeBranchRootRepourl(self) -> str | None:
        if self.repourl is None:
            return None
        return self.repourl

    def _isAtOrUnderRoot(self, value: str, root: str) -> bool:
        # Check if we are the root or a subpath of the root.
        return value == root or value.startswith(f'{root}/')

    def _branchSwitchAllowed(self, current_repourl: str | None) -> bool:
        # If we have no branchPath, don't allow switching between branches.
        if self.branchPath is None:
            return False
        if current_repourl is None:
            return False
        branch_root = self.svnUriCanonicalize(self._computeBranchRootRepourl())
        if branch_root is None:
            return False
        # Check if the extracted repourl is under the branch root.
        return self._isAtOrUnderRoot(current_repourl, branch_root)

    @defer.inlineCallbacks
    def _getSourcedirAction(self) -> InlineCallbacksType[str]:
        # first, perform a stat to ensure that this is really an svn directory
        res = yield self.pathExists(self.build.path_module.join(self.workdir, '.svn'))  # type: ignore[union-attr]
        if not res:
            return 'clobber'

        # then run 'svn info --xml' to check that the URL matches our expected url
        stdout, stderr = yield self._dovccmd(
            ['info', '--xml'], collectStdout=True, collectStderr=True, abandonOnFailure=False
        )

        # svn: E155037: Previous operation has not finished; run 'cleanup' if
        # it was interrupted
        if 'E155037:' in stderr:
            return 'clobber'

        try:
            stdout_xml = xml.dom.minidom.parseString(stdout)
            extractedurl = stdout_xml.getElementsByTagName('url')[0].firstChild.nodeValue  # type: ignore[union-attr]
        except xml.parsers.expat.ExpatError as e:
            yield self.stdio_log.addHeader('Corrupted xml, aborting step')  # type: ignore[attr-defined]
            raise buildstep.BuildStepFailed() from e

        current_repourl = self.svnUriCanonicalize(extractedurl)
        target_repourl = self.svnUriCanonicalize(self._getTargetRepourl())
        if current_repourl == target_repourl:
            return 'update'
        if self._branchSwitchAllowed(current_repourl):
            return 'switch'
        return 'clobber'

    @defer.inlineCallbacks
    def _sourcedirIsUpdatable(self) -> InlineCallbacksType[bool]:
        action = yield self._getSourcedirAction()
        return action != 'clobber'

    @defer.inlineCallbacks
    def clobber(self) -> InlineCallbacksType[None]:
        yield self.runRmdir(self.workdir, timeout=self.timeout)
        yield self._checkout()

    @defer.inlineCallbacks
    def fresh(self) -> InlineCallbacksType[None]:
        yield self.purge(True)
        cmd = ['update']
        if self.revision:
            cmd.extend(['--revision', str(self.revision)])
        yield self._dovccmd(cmd)

    @defer.inlineCallbacks
    def clean(self) -> InlineCallbacksType[None]:
        yield self.purge(False)
        cmd = ['update']
        if self.revision:
            cmd.extend(['--revision', str(self.revision)])
        yield self._dovccmd(cmd)

    @defer.inlineCallbacks
    def copy(self) -> InlineCallbacksType[None]:
        yield self.runRmdir(self.workdir, timeout=self.timeout)

        checkout_dir = 'source'
        if self.codebase:
            checkout_dir = self.build.path_module.join(checkout_dir, self.codebase)  # type: ignore[union-attr]
        # temporarily set workdir = checkout_dir and do an incremental checkout
        old_workdir = self.workdir
        try:
            self.workdir = checkout_dir
            yield self.mode_incremental()
        finally:
            self.workdir = old_workdir
        self.workdir = old_workdir

        # if we're copying, copy; otherwise, export from source to build
        if self.method == 'copy':
            cmd = remotecommand.RemoteCommand(
                'cpdir',
                {'fromdir': checkout_dir, 'todir': self.workdir, 'logEnviron': self.logEnviron},
            )
        else:
            export_cmd = ['svn', 'export']
            if self.revision:
                export_cmd.extend(["--revision", str(self.revision)])
            if self.username:
                export_cmd.extend(['--username', self.username])
            if self.password is not None:
                export_cmd.extend(['--password', self.password])
            if self.extra_args:
                export_cmd.extend(self.extra_args)
            export_cmd.extend([checkout_dir, self.workdir])

            cmd = remotecommand.RemoteShellCommand(
                '', export_cmd, env=self.env, logEnviron=self.logEnviron, timeout=self.timeout
            )
        cmd.useLog(self.stdio_log, False)

        yield self.runCommand(cmd)

        if cmd.didFail():
            raise buildstep.BuildStepFailed()

    @defer.inlineCallbacks
    def _dovccmd(
        self,
        command: list[str],
        collectStdout: bool = False,
        collectStderr: bool = False,
        abandonOnFailure: bool = True,
    ) -> InlineCallbacksType[str | tuple[str, str] | int]:
        assert command, "No command specified"
        command.extend(['--non-interactive', '--no-auth-cache'])
        if self.username:
            command.extend(['--username', self.username])
        if self.password is not None:
            command.extend(['--password', self.password])
        if self.depth:
            command.extend(['--depth', self.depth])
        if self.extra_args:
            command.extend(self.extra_args)

        cmd = remotecommand.RemoteShellCommand(
            self.workdir,
            ['svn', *command],
            env=self.env,
            logEnviron=self.logEnviron,
            timeout=self.timeout,
            collectStdout=collectStdout,
            collectStderr=collectStderr,
        )
        cmd.useLog(self.stdio_log, False)
        yield self.runCommand(cmd)

        if cmd.didFail() and abandonOnFailure:
            log.msg(f"Source step failed while running command {cmd}")
            raise buildstep.BuildStepFailed()
        if collectStdout and collectStderr:
            return (cmd.stdout, cmd.stderr)
        elif collectStdout:
            return cmd.stdout
        elif collectStderr:
            return cmd.stderr
        return cmd.rc  # type: ignore[return-value]

    def _getMethod(self) -> str | None:
        if self.method is not None and self.mode != 'incremental':
            return self.method
        elif self.mode == 'incremental':
            return None
        elif self.method is None and self.mode == 'full':
            return 'fresh'
        return None

    @defer.inlineCallbacks
    def parseGotRevision(self) -> InlineCallbacksType[int]:
        # if this was a full/export, then we need to check svnversion in the
        # *source* directory, not the build directory
        svnversion_dir = self.workdir
        if self.mode == 'full' and self.method == 'export':
            svnversion_dir = 'source'
        cmd = remotecommand.RemoteShellCommand(
            svnversion_dir,
            ['svn', 'info', '--xml'],
            env=self.env,
            logEnviron=self.logEnviron,
            timeout=self.timeout,
            collectStdout=True,
        )
        cmd.useLog(self.stdio_log, False)
        yield self.runCommand(cmd)

        stdout = cmd.stdout
        try:
            stdout_xml = xml.dom.minidom.parseString(stdout)
        except xml.parsers.expat.ExpatError as e:
            yield self.stdio_log.addHeader("Corrupted xml, aborting step")  # type: ignore[attr-defined]
            raise buildstep.BuildStepFailed() from e

        revision = None
        if self.preferLastChangedRev:
            try:
                revision = stdout_xml.getElementsByTagName('commit')[0].attributes['revision'].value
            except (KeyError, IndexError):
                msg = "SVN.parseGotRevision unable to detect Last Changed Rev in output of svn info"
                log.msg(msg)
                # fall through and try to get 'Revision' instead

        if revision is None:
            try:
                revision = stdout_xml.getElementsByTagName('entry')[0].attributes['revision'].value
            except (KeyError, IndexError) as e:
                msg = "SVN.parseGotRevision unable to detect revision in output of svn info"
                log.msg(msg)
                raise buildstep.BuildStepFailed() from e

        yield self.stdio_log.addHeader(f"Got SVN revision {revision}")  # type: ignore[attr-defined]
        self.updateSourceProperty('got_revision', revision)

        return cmd.rc  # type: ignore[return-value]

    @defer.inlineCallbacks
    def purge(self, ignore_ignores: bool) -> InlineCallbacksType[None]:
        """Delete everything that shown up on status."""
        command = ['status', '--xml']
        if ignore_ignores:
            command.append('--no-ignore')
        stdout = yield self._dovccmd(command, collectStdout=True)

        files = []
        for filename in self.getUnversionedFiles(stdout, self.keep_on_purge):
            filename = self.build.path_module.join(self.workdir, filename)  # type: ignore[union-attr]
            files.append(filename)
        if files:
            if self.workerVersionIsOlderThan('rmdir', '2.14'):
                rc = yield self.removeFiles(files)
            else:
                rc = yield self.runRmdir(files, abandonOnFailure=False, timeout=self.timeout)  # type: ignore[arg-type]
            if rc != 0:
                log.msg("Failed removing files")
                raise buildstep.BuildStepFailed()

    @staticmethod
    def getUnversionedFiles(xmlStr: str, keep_on_purge: list[str]) -> Generator[str, None, None]:
        try:
            result_xml = xml.dom.minidom.parseString(xmlStr)
        except xml.parsers.expat.ExpatError as e:
            log.err("Corrupted xml, aborting step")
            raise buildstep.BuildStepFailed() from e

        for entry in result_xml.getElementsByTagName('entry'):
            (wc_status,) = entry.getElementsByTagName('wc-status')
            if wc_status.getAttribute('item') == 'external':
                continue
            if wc_status.getAttribute('item') == 'missing':
                continue
            filename = entry.getAttribute('path')
            if filename in keep_on_purge or filename == '':
                continue
            yield filename

    @defer.inlineCallbacks
    def removeFiles(self, files: list[str]) -> InlineCallbacksType[int]:
        for filename in files:
            res = yield self.runRmdir(filename, abandonOnFailure=False, timeout=self.timeout)
            if res:
                return res
        return 0

    @defer.inlineCallbacks
    def checkSvn(self) -> InlineCallbacksType[bool]:
        cmd = remotecommand.RemoteShellCommand(
            self.workdir,
            ['svn', '--version'],
            env=self.env,
            logEnviron=self.logEnviron,
            timeout=self.timeout,
        )
        cmd.useLog(self.stdio_log, False)
        yield self.runCommand(cmd)
        return cmd.rc == 0

    def computeSourceRevision(self, changes: Any) -> int | None:
        if not changes or None in [c.revision for c in changes]:
            return None
        lastChange = max(int(c.revision) for c in changes)
        return lastChange

    @staticmethod
    def svnUriCanonicalize(uri: str | None) -> str | None:
        collapse = re.compile(r'([^/]+/\.\./?|/\./|//|/\.$|/\.\.$|^/\.\.)')
        server_authority = re.compile(r'^(?:([^@]+)@)?([^:]+)(?::(.+))?$')
        default_port = {'http': '80', 'https': '443', 'svn': '3690'}

        relative_schemes = ['http', 'https', 'svn']

        def quote(uri: str) -> str:
            return urlquote(uri, "!$&'()*+,-./:=@_~", encoding="latin-1")

        if not uri or uri == '/':
            return uri

        (scheme, authority, path, parameters, query, fragment) = urlparse(uri)
        scheme = scheme.lower()
        if authority:
            mo = server_authority.match(authority)
            if not mo:
                return uri  # give up
            userinfo, host, port = mo.groups()
            if host[-1] == '.':
                host = host[:-1]
            authority = host.lower()
            if userinfo:
                authority = f"{userinfo}@{authority}"
            if port and port != default_port.get(scheme, None):
                authority = f"{authority}:{port}"

        if scheme in relative_schemes:
            last_path = path
            while True:
                path = collapse.sub('/', path, 1)
                if last_path == path:
                    break
                last_path = path

        path = quote(urlunquote(path))
        canonical_uri = urlunparse((scheme, authority, path, parameters, query, fragment))
        if canonical_uri == '/':
            return canonical_uri
        elif canonical_uri[-1] == '/' and canonical_uri[-2] != '/':
            return canonical_uri[:-1]
        return canonical_uri

    @defer.inlineCallbacks
    def _checkout(self) -> InlineCallbacksType[None]:
        checkout_cmd = ['checkout', self._getTargetRepourl(), '.']
        if self.revision:
            checkout_cmd.extend(["--revision", str(self.revision)])
        if self.retry:
            abandonOnFailure = self.retry[1] <= 0
        else:
            abandonOnFailure = True
        res = yield self._dovccmd(checkout_cmd, abandonOnFailure=abandonOnFailure)  # type: ignore[arg-type]

        if self.retry:
            if self.stopped or res == 0:
                return
            delay, repeats = self.retry
            if repeats > 0:
                log.msg(f"Checkout failed, trying {repeats} more times after {delay} seconds")
                self.retry = (delay, repeats - 1)
                df: defer.Deferred[None] = defer.Deferred()
                df.addCallback(lambda _: self.runRmdir(self.workdir, timeout=self.timeout))
                df.addCallback(lambda _: self._checkout())
                reactor.callLater(delay, df.callback, None)  # type: ignore[attr-defined]
                yield df
