# Captur'd studio (work in progress)

This package is an untrusted Builds mini app. It currently implements only a
same-origin web bridge skeleton, a host-unavailable state, and basic job
status. It does not yet implement shot/take review, commands, export, native
transport, or the required host and device acceptance. Do not ship it as the
finished studio.

The bridge table is pinned to `rhobear75/rhobear-north-star` main `a9a9cb1`,
L8d CONTRACT section 9. The package pins family-kit 1.0.0 by release, source
SHA and tarball SHA-256 in `package.json`. The install requires Node 22+, `gh`, and access to the release asset; the
fetch script checks the tarball hash. On an empty npm cache, `npm ci` cannot
resolve the local tarball before its preinstall hook runs, so fetch it first.
The brief requires bare `npm ci`; that clean-install gate remains incomplete.

```sh
npm run fetch-kit
npm ci
npm test
npm run test:bridge
npm run build
```

The L4b host runner, real-device acceptance, and the contract binding for
loading a job's shot/take collection have not been supplied here. The L8d
bridge table specifies the envelope and command types but does not specify
that collection's location in an `EngineJob`; the engine manifest currently
has an open object output schema. Those details must be resolved before the
review UI can be wired without guessing. `npm audit` reports development-tool
dependency vulnerabilities; they require resolution before delivery.
