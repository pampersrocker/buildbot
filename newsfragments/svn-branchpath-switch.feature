The :bb:step:`SVN` step now supports a ``branchPath`` option to build checkout URLs from ``repourl`` and ``branchPath``.
When that path-derived URL changes between builds, the step now runs :command:`svn switch` and then updates the working copy instead of clobbering and checking out again.
If ``repourl`` changes, the existing clobber-and-checkout behavior is preserved.
