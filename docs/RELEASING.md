# Publishing a KM VMS release

This is the maintainer workflow, not an installation/update instruction for users. Normal installation and updates are described in [INSTALL.md](INSTALL.md).

1. Validate the complete product on the development NAS. Keep credentials, local infrastructure, reports, databases and video outside the product commit.
2. Run `sh scripts/km-vms-release-cycle.sh --prepare-version X.Y.Z --allow-dirty`. Fill the new title, summary and changelog, including complete `en`, `ru` and `zh-CN` maps in `release/km-vms-release.json`.
3. Run `sh scripts/km-vms-release-cycle.sh --check --allow-dirty` and the checks relevant to the changes. Commit the validated product, create an annotated tag (`git tag -a vX.Y.Z`), and push the commit and tag. Never rewrite an already published version tag.
4. Use `scripts/km-vms-publish-github-release.sh` after commit/tag/push to create the actual GitHub Release card. A Git tag alone is not a complete GitHub Release.

```sh
KM_VMS_GITHUB_RELEASE_TOKEN_FILE=/private/path/to/release-token \
  sh scripts/km-vms-publish-github-release.sh --publish --tag vX.Y.Z
```

The token is a maintainer-only secret with the necessary repository contents permission. It is not needed by users installing/updating from the public repository. Never put its value in Git, release notes, logs, screenshots or diagnostic reports.

When `--tag` is used, it must match the current release descriptor. The descriptor must agree on version, tag, repository and source reference. If `commit_sha` is present, it must match the resolved tag commit. The publisher validates this evidence and does not mutate product files after publication.

Finally verify the published Release, tag/commit and installed application identity, and complete the installation's supported release closeout. Use the existing deployment owner and preserve application state and every archive mount; changing identity text is not a substitute for deploying the matching application images.
