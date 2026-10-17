"""The task's working directory: git, read (:class:`baton_herdr.core.ports.Workspace`) and,
for a project's steps, written (:class:`baton_herdr.core.ports.Repository`)."""

from baton_herdr.workdir.git import GitWorkspace
from baton_herdr.workdir.repo import GitRepository

__all__ = ["GitRepository", "GitWorkspace"]
