### Fixed

- **Forks can build and publish their own Docker images.** CI image builds now
  mirror Python and kepubify into the building repository's own
  `ghcr.io/<owner>/pbs-cache` instead of the upstream owner's private package,
  which a fork cannot read. Image names are lowercased, so an owner with capital
  letters gets a valid `ghcr.io` reference, and the end-to-end tests run against
  the repository's own image. Local `docker build .` is unchanged.
